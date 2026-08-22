"""Capturing a device's sample stream into the database.

The recorder sits on ``Device.on_blocks`` and batches writes. Batching matters:
at 100 Hz the device layer delivers a block every fraction of a second, and one
SQLite transaction per block would spend more time on fsync than on data. Blocks
are accumulated and flushed either when enough rows have piled up or when a short
timer expires, so a slow trickle at 1 Hz still lands on disk promptly.

Two details worth knowing:

* Backfilled blocks arrive *after* newer live blocks. They are written as they
  come and sorted on read, rather than being held back — holding them would risk
  losing them entirely if the app closed.
* Stopping a recording flushes, records the final timebase fit, and stores the
  gap list. A session that ends with unrecoverable gaps says so in its metadata
  rather than quietly presenting incomplete data as complete.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field

import numpy as np

from ..device.device import Device
from ..protocol.messages import SampleBlock
from .db import Database, pack_block

log = logging.getLogger(__name__)

#: Flush when this many rows are buffered...
FLUSH_ROWS = 2000
#: ...or when this long has passed, whichever comes first.
FLUSH_INTERVAL_S = 2.0


@dataclass
class RecorderStats:
    rows_written: int = 0
    blocks_written: int = 0
    flushes: int = 0
    last_flush_at: float = 0.0
    write_seconds: float = 0.0
    errors: int = 0

    @property
    def rows_per_second(self) -> float:
        return self.rows_written / self.write_seconds if self.write_seconds > 0 else 0.0


@dataclass
class Recording:
    """One active capture of one device."""

    session_id: int
    device: Device
    started_at: float = field(default_factory=time.time)
    stats: RecorderStats = field(default_factory=RecorderStats)
    name: str = ""

    @property
    def duration_s(self) -> float:
        return time.time() - self.started_at


class Recorder:
    """Manages every active recording.

    One instance per application. Recordings are independent: stopping one board's
    capture does not disturb another's, and a database error on one is contained.
    """

    def __init__(self, db: Database) -> None:
        self.db = db
        self._active: dict[str, Recording] = {}
        self._pending: dict[str, list[dict]] = {}
        self._pending_rows: dict[str, int] = {}
        self._flush_task: asyncio.Task | None = None
        self._listeners: list = []

    # ---------------------------------------------------------------- control

    def is_recording(self, serial: str) -> bool:
        return serial in self._active

    @property
    def active(self) -> list[Recording]:
        return list(self._active.values())

    def start(self, device: Device, *, name: str = "", notes: str = "") -> Recording:
        """Begin capturing a device. Snapshots its calibration and config."""
        if device.serial in self._active:
            raise RuntimeError(f"{device.label} is already recording")
        if not device.channel_order:
            raise RuntimeError(
                f"{device.label} has not reported its channels yet — wait for it to "
                "come online before recording"
            )

        self.db.upsert_device(device.serial, device.info, device.name)
        session_id = self.db.create_session(
            device.serial,
            list(device.channel_order),
            name=name,
            rate_hz=device._stream_rate_hz,
            cal=device.calibration.to_json(),
            config=device.config,
            notes=notes,
        )
        recording = Recording(session_id=session_id, device=device, name=name)
        self._active[device.serial] = recording
        self._pending[device.serial] = []
        self._pending_rows[device.serial] = 0

        previous = device.on_blocks
        if previous is not None:
            log.warning("%s already had a block consumer; replacing it", device.label)
        device.on_blocks = lambda blocks, times, s=device.serial: self._on_blocks(s, blocks, times)

        self._ensure_flusher()
        log.info("recording %s into session %d", device.label, session_id)
        self._notify("started", recording)
        return recording

    async def stop(self, serial: str) -> Recording | None:
        recording = self._active.get(serial)
        if recording is None:
            return None
        recording.device.on_blocks = None
        await self.flush(serial)

        aggregator = recording.device.aggregator
        gaps = [
            {"first_seq": g.first_seq, "last_seq": g.last_seq, "rows": g.length,
             "recovered": False}
            for g in aggregator.open_gaps
        ]
        self.db.close_session(
            recording.session_id,
            timebase=recording.device.timebase.fit.to_json(),
            gaps=gaps,
        )
        self._active.pop(serial, None)
        self._pending.pop(serial, None)
        self._pending_rows.pop(serial, None)
        log.info(
            "stopped session %d: %d rows over %.1f s%s",
            recording.session_id, recording.stats.rows_written, recording.duration_s,
            f", {len(gaps)} unfilled gap(s)" if gaps else "",
        )
        self._notify("stopped", recording)
        return recording

    async def stop_all(self) -> None:
        for serial in list(self._active):
            with contextlib.suppress(Exception):
                await self.stop(serial)
        task, self._flush_task = self._flush_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # ------------------------------------------------------------------ input

    def _on_blocks(self, serial: str, blocks: list[SampleBlock], times_utc: np.ndarray) -> None:
        """Called from the device layer for every ordered run of samples."""
        recording = self._active.get(serial)
        if recording is None:
            return
        pending = self._pending.setdefault(serial, [])
        offset = 0
        for block in blocks:
            n = block.n_samples
            t0 = float(times_utc[offset]) if offset < len(times_utc) else time.time()
            dt_s = block.dt_us / 1e6
            # Use the fitted timebase's own spacing rather than the nominal dt, so a
            # board whose crystal is 40 ppm fast has that reflected in the record.
            if n > 1 and offset + n <= len(times_utc):
                dt_s = float((times_utc[offset + n - 1] - times_utc[offset]) / (n - 1))
            values = recording.device._reindex(block)
            pending.append(
                pack_block(block.first_seq, values, t0, dt_s, fault_mask=block.fault_mask)
            )
            offset += n
        self._pending_rows[serial] = self._pending_rows.get(serial, 0) + sum(
            b.n_samples for b in blocks
        )
        if self._pending_rows[serial] >= FLUSH_ROWS:
            self._spawn_flush(serial)

    def _spawn_flush(self, serial: str) -> None:
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().create_task(self.flush(serial))

    async def flush(self, serial: str) -> int:
        """Write buffered blocks for one device. Returns rows written."""
        recording = self._active.get(serial)
        blocks = self._pending.get(serial)
        if recording is None or not blocks:
            return 0
        self._pending[serial] = []
        self._pending_rows[serial] = 0

        started = time.perf_counter()
        loop = asyncio.get_running_loop()
        try:
            # SQLite writes are blocking; keep them off the event loop so the live
            # plot does not stutter every time a batch lands.
            rows = await loop.run_in_executor(
                None, self.db.append_blocks, recording.session_id, blocks
            )
        except Exception:
            log.exception("failed writing %d blocks for session %d",
                          len(blocks), recording.session_id)
            recording.stats.errors += 1
            # Put them back; the next flush retries rather than dropping data.
            self._pending[serial] = blocks + self._pending.get(serial, [])
            self._pending_rows[serial] = sum(b["n_rows"] for b in self._pending[serial])
            return 0

        elapsed = time.perf_counter() - started
        recording.stats.rows_written += rows
        recording.stats.blocks_written += len(blocks)
        recording.stats.flushes += 1
        recording.stats.last_flush_at = time.time()
        recording.stats.write_seconds += elapsed
        self._notify("flushed", recording)
        return rows

    def _ensure_flusher(self) -> None:
        if self._flush_task is not None and not self._flush_task.done():
            return
        with contextlib.suppress(RuntimeError):
            self._flush_task = asyncio.get_running_loop().create_task(
                self._flush_loop(), name="tjip-recorder-flush"
            )

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_INTERVAL_S)
            if not self._active:
                continue
            for serial in list(self._active):
                if self._pending_rows.get(serial):
                    with contextlib.suppress(Exception):
                        await self.flush(serial)

    # --------------------------------------------------------------- markers

    def mark(self, serial: str, label: str, notes: str = "") -> int | None:
        """Annotate the current recording at this instant."""
        recording = self._active.get(serial)
        if recording is None:
            return None
        return self.db.add_marker(recording.session_id, time.time(), label, notes)

    # -------------------------------------------------------------- listeners

    def subscribe(self, listener) -> None:
        self._listeners.append(listener)

    def _notify(self, kind: str, recording: Recording) -> None:
        for listener in list(self._listeners):
            with contextlib.suppress(Exception):
                listener(kind, recording)

    def status(self) -> list[dict]:
        return [
            {
                "session_id": r.session_id,
                "device": r.device.serial,
                "name": r.name,
                "duration_s": round(r.duration_s, 1),
                "rows": r.stats.rows_written,
                "pending_rows": self._pending_rows.get(r.device.serial, 0),
                "errors": r.stats.errors,
            }
            for r in self._active.values()
        ]
