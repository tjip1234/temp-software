"""Merging sample blocks arriving over several links at once.

This is the piece that makes "USB and WiFi for redundancy" mean something. Because
every row carries a device-assigned sequence number, two links delivering the same
data are trivially reconcilable: take whichever arrives first, ignore the duplicate,
and note any sequence range that neither link delivered so it can be backfilled.

Three things are tracked:

* ``delivered`` -- the contiguous run from the session start that is complete.
* ``pending`` -- rows that arrived out of order, held until the hole before them
  fills in. Bounded, because a hole that never fills must not leak memory.
* ``gaps`` -- ranges known to be missing, offered to the device layer for GET_RANGE.

A gap is only requested after a short grace period. On a healthy dual-link setup
the "missing" rows are usually about to arrive on the other link a few tens of
milliseconds later, and asking for them immediately would double the traffic for
nothing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from ..protocol.messages import SampleBlock

#: Wait this long after noticing a hole before asking the device to resend it.
GAP_GRACE_S = 0.75
#: Never hold more than this many out-of-order rows.
MAX_PENDING_ROWS = 20_000
#: Never chase a gap larger than this in one request.
MAX_BACKFILL_SPAN = 5_000


@dataclass(slots=True)
class Gap:
    first_seq: int
    last_seq: int          # inclusive
    noticed_at: float = field(default_factory=time.monotonic)
    requested_at: float = 0.0
    attempts: int = 0
    unrecoverable: bool = False

    @property
    def length(self) -> int:
        return self.last_seq - self.first_seq + 1

    def ready(self, now: float) -> bool:
        if self.unrecoverable:
            return False
        if self.requested_at:
            # Exponential backoff: a device that could not answer once will often
            # need a moment, and hammering it helps nobody.
            return now - self.requested_at > GAP_GRACE_S * (2 ** self.attempts)
        return now - self.noticed_at > GAP_GRACE_S


@dataclass(slots=True)
class AggregatorStats:
    rows_accepted: int = 0
    rows_duplicate: int = 0
    rows_backfilled: int = 0
    rows_lost: int = 0
    blocks_seen: int = 0
    reorder_events: int = 0
    per_link: dict[str, int] = field(default_factory=dict)

    @property
    def completeness(self) -> float:
        total = self.rows_accepted + self.rows_lost
        return self.rows_accepted / total if total else 1.0


class SampleAggregator:
    """Order, deduplicate and gap-track sample blocks from any number of links."""

    def __init__(self, *, max_pending: int = MAX_PENDING_ROWS) -> None:
        self.next_seq: int | None = None
        self.stats = AggregatorStats()
        self._pending: dict[int, SampleBlock] = {}   # first_seq -> block
        self._pending_rows = 0
        self._max_pending = max_pending
        self._gaps: dict[int, Gap] = {}              # first_seq -> gap
        self._last_seq_seen: int = -1

    # ------------------------------------------------------------------ input

    def reset(self, next_seq: int | None = None) -> None:
        """Start over. Called on a device reboot, or when a new session begins."""
        self.next_seq = next_seq
        self._pending.clear()
        self._pending_rows = 0
        self._gaps.clear()
        self._last_seq_seen = -1 if next_seq is None else next_seq - 1

    def feed(self, block: SampleBlock, *, link: str = "", backfill: bool = False) -> list[SampleBlock]:
        """Absorb one block; return whatever is now deliverable, in sequence order."""
        self.stats.blocks_seen += 1
        if link:
            self.stats.per_link[link] = self.stats.per_link.get(link, 0) + block.n_samples

        if self.next_seq is None:
            # First block ever: this is where the session starts. Anything earlier is
            # history we did not ask for, and is ignored rather than back-dated.
            self.next_seq = block.first_seq
            self._last_seq_seen = block.first_seq - 1

        # Trim rows we already delivered.
        if block.last_seq < self.next_seq:
            self.stats.rows_duplicate += block.n_samples
            self._resolve_gap(block.first_seq, block.last_seq)
            return []
        if block.first_seq < self.next_seq:
            duplicated = self.next_seq - block.first_seq
            self.stats.rows_duplicate += duplicated
            trimmed = block.slice_seq(self.next_seq, block.last_seq)
            if trimmed is None:
                return []
            block = trimmed

        if backfill:
            self.stats.rows_backfilled += block.n_samples
        self._resolve_gap(block.first_seq, block.last_seq)

        if block.first_seq > self.next_seq:
            # A hole. Hold this block and remember what is missing.
            self._note_gap(self.next_seq, block.first_seq - 1)
            self.stats.reorder_events += 1
            self._hold(block)
            return []

        return self._drain(block)

    def _drain(self, block: SampleBlock) -> list[SampleBlock]:
        """Deliver ``block`` and any held blocks that now follow contiguously."""
        out = [block]
        self.next_seq = block.last_seq + 1
        self.stats.rows_accepted += block.n_samples
        self._last_seq_seen = max(self._last_seq_seen, block.last_seq)

        while True:
            held = self._pending.pop(self.next_seq, None)
            if held is None:
                # Also accept a held block that overlaps rather than abuts exactly.
                candidates = [s for s in self._pending if s <= self.next_seq]
                if not candidates:
                    break
                start = max(candidates)
                held = self._pending.pop(start)
                trimmed = held.slice_seq(self.next_seq, held.last_seq)
                self._pending_rows -= held.n_samples
                if trimmed is None:
                    continue
                held = trimmed
            else:
                self._pending_rows -= held.n_samples
            out.append(held)
            self.next_seq = held.last_seq + 1
            self.stats.rows_accepted += held.n_samples
            self._last_seq_seen = max(self._last_seq_seen, held.last_seq)
        return out

    def _hold(self, block: SampleBlock) -> None:
        existing = self._pending.get(block.first_seq)
        if existing is not None and existing.n_samples >= block.n_samples:
            self.stats.rows_duplicate += block.n_samples
            return
        if existing is not None:
            self._pending_rows -= existing.n_samples
        self._pending[block.first_seq] = block
        self._pending_rows += block.n_samples
        self._evict_if_needed()

    def _evict_if_needed(self) -> None:
        """Give up on the oldest hole when held data grows unreasonable.

        Holding forever would be a slow memory leak on a link that is dropping
        every other block. Declaring the gap unrecoverable and moving on keeps the
        live view alive; the rows are counted as lost, and the UI says so.
        """
        while self._pending_rows > self._max_pending and self._pending:
            oldest = min(self._pending)
            if self.next_seq is not None and oldest > self.next_seq:
                lost = oldest - self.next_seq
                self.stats.rows_lost += lost
                for gap in self._gaps.values():
                    if gap.first_seq < oldest:
                        gap.unrecoverable = True
                self.next_seq = oldest
            block = self._pending.pop(oldest)
            self._pending_rows -= block.n_samples
            self.stats.rows_accepted += block.n_samples
            self.next_seq = block.last_seq + 1

    # ------------------------------------------------------------------- gaps

    def _note_gap(self, first: int, last: int) -> None:
        if last < first:
            return
        # Merge into an adjacent known gap rather than fragmenting.
        for gap in self._gaps.values():
            if gap.first_seq <= first and last <= gap.last_seq:
                return
        self._gaps[first] = Gap(first_seq=first, last_seq=last)

    def _resolve_gap(self, first: int, last: int) -> None:
        """Shrink or clear gaps covered by newly arrived rows."""
        for key in list(self._gaps):
            gap = self._gaps[key]
            if last < gap.first_seq or first > gap.last_seq:
                continue
            if first <= gap.first_seq and last >= gap.last_seq:
                del self._gaps[key]
            elif first <= gap.first_seq:
                del self._gaps[key]
                gap.first_seq = last + 1
                if gap.first_seq <= gap.last_seq:
                    self._gaps[gap.first_seq] = gap
            elif last >= gap.last_seq:
                gap.last_seq = first - 1

    def due_gaps(self, now: float | None = None, limit: int = 4) -> list[Gap]:
        """Gaps that have waited out the grace period and should be requested."""
        now = time.monotonic() if now is None else now
        ready = [g for g in self._gaps.values() if g.ready(now)]
        ready.sort(key=lambda g: g.first_seq)
        out = []
        for gap in ready[:limit]:
            if gap.length > MAX_BACKFILL_SPAN:
                # Chip away at an enormous gap rather than asking for all of it.
                out.append(Gap(gap.first_seq, gap.first_seq + MAX_BACKFILL_SPAN - 1,
                               noticed_at=gap.noticed_at, attempts=gap.attempts))
            else:
                out.append(gap)
        return out

    def mark_requested(self, gap: Gap, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        stored = self._gaps.get(gap.first_seq)
        target = stored or gap
        target.requested_at = now
        target.attempts += 1
        if target.attempts >= 4:
            # The device has had four chances; the rows have almost certainly aged
            # out of its ring buffer. Stop asking and record the loss honestly.
            target.unrecoverable = True
            self.stats.rows_lost += target.length

    def mark_unrecoverable(self, first_seq: int, last_seq: int) -> None:
        """Called when the device reports rows as evicted from its ring."""
        for gap in self._gaps.values():
            if gap.first_seq >= first_seq and gap.last_seq <= last_seq:
                if not gap.unrecoverable:
                    gap.unrecoverable = True
                    self.stats.rows_lost += gap.length

    @property
    def open_gaps(self) -> list[Gap]:
        return sorted(self._gaps.values(), key=lambda g: g.first_seq)

    @property
    def missing_rows(self) -> int:
        return sum(g.length for g in self._gaps.values() if not g.unrecoverable)

    @property
    def held_rows(self) -> int:
        return self._pending_rows

    def health(self) -> dict:
        """Summary for the status bar and the API."""
        return {
            "next_seq": self.next_seq,
            "rows": self.stats.rows_accepted,
            "duplicates": self.stats.rows_duplicate,
            "backfilled": self.stats.rows_backfilled,
            "lost": self.stats.rows_lost,
            "held": self._pending_rows,
            "open_gaps": len(self._gaps),
            "missing_rows": self.missing_rows,
            "completeness": round(self.stats.completeness, 6),
            "per_link": dict(self.stats.per_link),
        }


