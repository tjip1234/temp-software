"""A board, as the rest of the application sees it.

A Device is identified by its serial number, not by how it happens to be plugged
in. It may hold several links at once -- USB and WiFi typically -- and the whole
point of that arrangement is that the layers above never need to know which one a
particular sample arrived on. Blocks from every link go through one aggregator,
which deduplicates by sequence number, and the resulting stream is contiguous,
in order, and stamped with a fitted UTC timebase.

Responsibilities, in order of importance:

* keep the sample stream complete (dedup, gap detection, backfill),
* keep the timebase honest (sync burst on connect, drift tracking after),
* hold the authoritative copy of the device's config and calibration,
* tell interested parties what happened, via plain callbacks.

Callbacks rather than Qt signals: this layer runs on an asyncio loop and is used
unchanged by the headless API server and the test suite. The UI adapts it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

import numpy as np

from ..calibration.models import CalibrationSet
from ..core.aggregator import SampleAggregator
from ..core.ringbuffer import ChannelRing
from ..core.timebase import SyncSample, SyncScheduler, Timebase
from ..protocol import messages as M
from ..protocol.channels import ChannelSpec, specs_from_device_info
from ..protocol.framing import Frame
from ..transport.base import Transport, TransportError
from ..transport.discovery import Candidate, build_transport
from .link import Link, LinkClosed

log = logging.getLogger(__name__)

#: How long without a STATUS before a link is presumed dead.
#:
#: The board sends one per second, so three missed is the spec's rule of thumb
#: (§12). Over WiFi that is too tight: a board doing something long and
#: uninterruptible — a wiper sweep is fifteen seconds — is not dead, and
#: declaring it so tears down a link that was about to come back.
STATUS_TIMEOUT_S = 20.0
#: Live buffer depth, in samples. 120k at 100 Hz is 20 minutes.
LIVE_CAPACITY = 120_000

#: How long to keep re-attempting a handshake, and how long to wait between.
#:
#: Connecting is not one event. A board can accept the TCP connection and then
#: go away before it answers HELLO — it is listening, and then it reboots, and
#: the socket dies mid-handshake. Retrying the whole cycle (fresh link, fresh
#: HELLO) covers that, where retrying only the connect does not.
#:
#: Only a link that *died* is retried. A board that answers with the wrong
#: protocol version, or refuses, has given a real answer and gets reported.
#: How long to keep asking a newly opened link to identify itself. A board that
#: is simply booting answers within a second or two; this used to be far longer
#: only because each retry reopened the port and reset the board, so the window
#: had to outlast a loop it was itself feeding.
HANDSHAKE_WINDOW_S = 12.0
HANDSHAKE_RETRY_S = 1.5
#: How long one HELLO waits before it is sent again, growing by this much per
#: attempt. A running board answers in milliseconds; one that does not has
#: usually not heard the HELLO at all -- it was still booting, because it was
#: just plugged in or opening the port reset it -- and waiting longer will not
#: make it hear it. This used to be the link's 12 s default, which is the whole
#: window: one HELLO lost to a boot meant a 12 s hang and then a failure,
#: without the retry below ever getting a turn.
HELLO_TIMEOUT_S = 1.0


class DeviceState(Enum):
    OFFLINE = "offline"
    CONNECTING = "connecting"
    ONLINE = "online"
    DEGRADED = "degraded"   # connected, but losing data or badly out of sync
    ERROR = "error"


@dataclass
class DeviceEvent:
    """Something worth telling the UI about."""

    kind: str        # "state" | "samples" | "status" | "info" | "config" | "cal" | "log" | "error"
    device: Device
    data: dict = field(default_factory=dict)


class Device:
    """One physical board, reachable over one or more links."""

    def __init__(self, serial: str | None = None, name: str = "") -> None:
        self.serial = serial or ""
        self.name = name
        self.state = DeviceState.OFFLINE
        self.error: str | None = None

        self.info: dict = {}
        self.config: dict = {}
        self.calibration = CalibrationSet()
        self.status: dict = {}
        self.self_test_result: dict = {}
        #: Last SIM_CAL from the board: the measured wiper table and the
        #: residuals of the sweep that produced it. Empty on a board with no
        #: simulator, which is how the UI decides whether to offer the panel.
        self.sim_cal: dict = {}

        self.channels: dict[int, ChannelSpec] = {}
        self.channel_order: tuple[int, ...] = ()
        #: Column permutation per source channel set, see _reindex_plan.
        self._reindex_cache: dict[tuple[int, ...], tuple[np.ndarray, np.ndarray]] = {}

        self.timebase = Timebase()
        self.aggregator = SampleAggregator()
        self.live = ChannelRing(0, LIVE_CAPACITY)

        self.links: dict[str, Link] = {}
        self._sync = SyncScheduler()
        self._listeners: list[Callable[[DeviceEvent], None]] = []
        self._tasks: set[asyncio.Task] = set()
        self._stream_rate_hz = 10.0
        self._streaming = False
        self._last_status_at = 0.0
        self._closing = False
        self._backfill_inflight: set[int] = set()
        self._log_lines: list[dict] = []

        #: Set by the recorder when a session is capturing this device.
        self.on_blocks: Callable[[list[M.SampleBlock], np.ndarray], None] | None = None

    # ------------------------------------------------------------------ events

    def subscribe(self, listener: Callable[[DeviceEvent], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def _emit(self, kind: str, **data) -> None:
        event = DeviceEvent(kind=kind, device=self, data=data)
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                log.exception("device listener failed on %s event", kind)

    def _set_state(self, state: DeviceState, error: str | None = None) -> None:
        if state == self.state and error == self.error:
            return
        self.state = state
        self.error = error
        self._emit("state", state=state.value, error=error)

    # ------------------------------------------------------------------- links

    @property
    def is_online(self) -> bool:
        return self.state in (DeviceState.ONLINE, DeviceState.DEGRADED)

    @property
    def primary_link(self) -> Link | None:
        """Best available link: highest priority, then lowest round trip."""
        candidates = [link for link in self.links.values() if link.is_open]
        if not candidates:
            return None
        return max(candidates, key=lambda link: (link.info.priority, -link.median_rtt_ms))

    @property
    def backfill_link(self) -> Link | None:
        candidates = [
            link for link in self.links.values() if link.is_open and link.info.supports_backfill
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda link: link.info.priority)

    @property
    def link_kinds(self) -> list[str]:
        return sorted({link.kind for link in self.links.values() if link.is_open})

    async def add_transport(self, transport: Transport, *, start_stream: bool = True) -> Link:
        """Attach a transport, handshake over it, and fold it into the sample stream."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + HANDSHAKE_WINDOW_S
        attempt = 0

        def elapsed_ms() -> float:
            return (loop.time() - started) * 1000.0

        link = Link(transport, on_unsolicited=self._on_unsolicited)
        key = link.key
        if key in self.links:
            await self.remove_link(key)
        self.links[key] = link
        if self.state is DeviceState.OFFLINE:
            self._set_state(DeviceState.CONNECTING)

        log.info("connecting to %s", transport.info)
        while True:
            attempt += 1
            try:
                # Only open when there is nothing open. Opening a USB CDC port
                # moves the modem lines, and on an ESP32-S3 that is enough to
                # reset the board -- so reopening once per retry turned "this
                # board is slow to answer" into "this board is reset every two
                # seconds and never gets far enough to answer". A board that is
                # merely quiet gets asked again on the link that is already up.
                if not transport.is_open:
                    await link.start()
                    log.info("%s: port open after %.0f ms", transport.info, elapsed_ms())
                await self._handshake(link, hello_budget_s=deadline - loop.time())
                break
            except (TransportError, LinkClosed, TimeoutError,
                    M.DeviceError, M.ProtocolError) as exc:
                # A wrong protocol version or a rejected request is an answer,
                # not a failure to reach the board. Retrying cannot change it.
                fatal = isinstance(exc, (M.DeviceError, M.ProtocolError))
                if fatal or loop.time() >= deadline:
                    self.links.pop(key, None)
                    with contextlib.suppress(Exception):
                        await link.stop()
                    if not self.links:
                        self._set_state(
                            DeviceState.OFFLINE,
                            str(exc) if fatal else
                            f"{exc} (gave up after {attempt} attempts in "
                            f"{HANDSHAKE_WINDOW_S:.0f} s)",
                        )
                    log.warning("%s: could not connect (%d attempt(s), %.0f ms): %s",
                                transport.info, attempt, elapsed_ms(), exc)
                    raise
                log.info("%s: handshake attempt %d failed after %.0f ms (%s); retrying",
                         transport.info, attempt, elapsed_ms(), exc)
                await asyncio.sleep(HANDSHAKE_RETRY_S)

        self._spawn(self._watch_link(link))
        if start_stream and self._streaming:
            await self._start_stream_on(link)
        elif start_stream:
            await self.start_streaming(self._stream_rate_hz)
        self._set_state(DeviceState.ONLINE)
        log.info("%s: online over %s after %.0f ms", self.label, transport.info, elapsed_ms())
        self._emit("links")
        return link

    async def remove_link(self, key: str) -> None:
        link = self.links.pop(key, None)
        if link is None:
            return
        with contextlib.suppress(Exception):
            await link.stop()
        self._emit("links")
        if not self.links and not self._closing:
            self._set_state(DeviceState.OFFLINE)

    async def _watch_link(self, link: Link) -> None:
        await link.wait_closed()
        if self._closing:
            return
        self.links.pop(link.key, None)
        reason = link.info.extra.get("last_error")
        log.info("%s: link %s closed%s", self.label, link.info,
                 f" ({reason})" if reason else "")
        self._emit("links", closed=link.key)
        if not self.links:
            self._set_state(DeviceState.OFFLINE, "all links closed")

    # --------------------------------------------------------------- handshake

    async def _hello(self, link: Link, budget_s: float) -> dict:
        """HELLO, sent again every HELLO_TIMEOUT_S until one of them is answered.

        The earlier ones keep waiting while the next goes out. A board that
        missed a HELLO because it was still booting answers the next one; a
        board that is merely slow answers the first one, late -- which a fixed
        per-attempt timeout would have abandoned every time.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget_s
        asking: set[asyncio.Task] = set()
        sent = 0
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError(
                        f"{link.info}: no reply to HELLO within {budget_s:.0f} s "
                        f"(sent {sent} times)"
                    )
                asking.add(asyncio.ensure_future(link.hello(timeout=remaining)))
                sent += 1
                if sent > 1:
                    log.info("%s: no reply to HELLO yet; sending it again", link.info)
                done, asking = await asyncio.wait(
                    asking, timeout=min(HELLO_TIMEOUT_S, remaining),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                failed: BaseException | None = None
                for task in done:
                    if task.exception() is None:
                        return task.result()
                    failed = task.exception()
                if failed is not None and not isinstance(failed, TimeoutError):
                    raise failed
        finally:
            for task in asking:
                task.cancel()
            if asking:
                await asyncio.gather(*asking, return_exceptions=True)

    async def _handshake(self, link: Link, *, hello_budget_s: float = HANDSHAKE_WINDOW_S) -> None:
        started = time.monotonic()
        info = await self._hello(link, hello_budget_s)
        log.info("%s: HELLO answered after %.0f ms", link.info,
                 (time.monotonic() - started) * 1000.0)
        proto = int(info.get("proto", 0))
        if proto != M.PROTOCOL_VERSION:
            raise M.ProtocolError(
                f"board speaks TJIP protocol version {proto}, this software implements "
                f"version {M.PROTOCOL_VERSION}. Update one of them; guessing would "
                f"silently corrupt your measurements."
            )
        serial = str(info.get("serial") or "")
        if self.serial and serial and serial != self.serial:
            raise M.ProtocolError(
                f"{link.info} is board {serial}, not {self.serial}"
            )
        self.serial = self.serial or serial
        link.info.serial = self.serial
        self._apply_info(info)

        self.timebase.note_boot_id(str(info.get("boot_id") or "") or None)
        await self._sync_burst(link)

        config = await self._timed(link, "GET_CONFIG", link.get_config)
        if config is not None:
            self.config = config
            self._adopt_board_settings()
            self._emit("config", config=self.config)
        cal = await self._timed(link, "GET_CAL", link.get_cal)
        if cal is not None:
            self.calibration = CalibrationSet.from_json(cal)
            self._emit("cal", calibration=self.calibration)

        # The board keeps its simulator sweep in NVS and only pushes SIM_CAL
        # unsolicited when a new sweep finishes. Without asking for it, a board
        # that was calibrated last week showed the panel an empty table and an
        # unknown state until someone ran another sweep -- the board knew, and
        # the desktop never asked. It is asked after going online, though, not
        # before: nothing about connecting needs it, and a board that did not
        # answer used to hold every connect for the whole 15 s timeout.
        if self.has_simulator:
            self._spawn(self._fetch_sim_cal(link))

    async def _timed(self, link: Link, what: str, request):
        """One handshake request, timed; None if the board refused or never answered.

        A running board answers in milliseconds, so one that takes seconds is
        the whole story of a slow connect -- and it used to leave no trace.
        """
        started = time.monotonic()
        try:
            result = await request()
        except (M.DeviceError, TimeoutError) as exc:
            log.warning("%s: %s failed after %.0f ms: %s", link.info, what,
                        (time.monotonic() - started) * 1000.0, exc)
            return None
        log.info("%s: %s answered after %.0f ms", link.info, what,
                 (time.monotonic() - started) * 1000.0)
        return result

    async def _fetch_sim_cal(self, link: Link) -> None:
        try:
            table = await self._timed(link, "GET_SIM_CAL", link.get_sim_cal)
        except (LinkClosed, TransportError):
            return
        if table is not None:
            self.sim_cal = table
            self._emit("sim_cal", table=self.sim_cal)
        elif self.sim_cal:
            log.info("%s: the simulator table arrived unsolicited instead", link.info)

    def _apply_info(self, info: dict) -> None:
        self.info = info
        self.channels = specs_from_device_info(info)
        self.channel_order = tuple(sorted(self.channels))
        self._reindex_cache.clear()   # the canonical order just changed
        if self.live.n_channels != len(self.channel_order):
            self.live = ChannelRing(len(self.channel_order), LIVE_CAPACITY)
        if not self.name:
            self.name = info.get("model", "TjipTemp")
        self._emit("info", info=info)

    def _adopt_board_settings(self) -> None:
        """On connect, the board's stored settings win.

        The board keeps its configuration in NVS and applies it the moment it
        powers up, host or no host -- that is the whole point of a DIN-rail box
        in a cabinet. If this software pushed its own remembered values over the
        top on connect, whatever was set at the board would be overwritten by a
        program that just walked in, and the panel would change the instant
        someone plugged a laptop in.

        So connecting is one-way: board -> desktop. The desktop's own value is a
        fallback for a board that does not report one, and changing a setting
        afterwards is an explicit push the user asked for.
        """
        rate = (self.config.get("acquire") or {}).get("rate_hz")
        if isinstance(rate, (int, float)) and rate > 0:
            self._stream_rate_hz = min(float(rate), self.max_rate_hz)

    @property
    def stream_rate_hz(self) -> float:
        return self._stream_rate_hz

    @property
    def label(self) -> str:
        base = self.name or "TjipTemp"
        return f"{base} ({self.serial[-6:]})" if self.serial else base

    @property
    def max_rate_hz(self) -> float:
        return float(self.info.get("caps", {}).get("max_rate_hz", 100.0))

    @property
    def ring_samples(self) -> int:
        return int(self.info.get("caps", {}).get("ring_samples", 0))

    # ------------------------------------------------------------- time sync

    async def _sync_burst(self, link: Link) -> None:
        """Get an immediately usable timebase before any samples arrive."""
        self._sync.reset()
        accepted = 0
        for i in range(self._sync.burst_count):
            if i:
                # Between exchanges only: a sleep after the last one was just
                # time added to every connect.
                await asyncio.sleep(self._sync.burst_interval_s)
            try:
                t1, t2, t3, t4 = await link.time_sync()
            except (TimeoutError, M.ProtocolError, LinkClosed):
                break
            if self.timebase.add(SyncSample(t1, t2, t3, t4)):
                accepted += 1
            self._sync.mark_sent()
        log.info("%s: %d/%d sync exchanges accepted", self.label, accepted, self._sync.burst_count)
        self._emit("timebase", fit=self.timebase.fit.to_json())

    async def _sync_tick(self) -> None:
        link = self.primary_link
        if link is None or not self._sync.due():
            return
        try:
            t1, t2, t3, t4 = await link.time_sync()
        except (TimeoutError, M.ProtocolError, LinkClosed, TransportError):
            self._sync.mark_sent()
            return
        self.timebase.add(SyncSample(t1, t2, t3, t4))
        self._sync.mark_sent()
        self._emit("timebase", fit=self.timebase.fit.to_json())

    # ------------------------------------------------------------- streaming

    async def start_streaming(self, rate_hz: float | None = None, channels="all") -> None:
        if rate_hz is not None:
            self._stream_rate_hz = min(float(rate_hz), self.max_rate_hz)
        self._streaming = True
        for link in list(self.links.values()):
            if link.is_open:
                await self._start_stream_on(link, channels)
        self._spawn(self._housekeeping())

    async def _start_stream_on(self, link: Link, channels="all") -> None:
        with contextlib.suppress(TransportError, LinkClosed):
            await link.stream_start(self._stream_rate_hz, channels)

    async def stop_streaming(self) -> None:
        self._streaming = False
        for link in list(self.links.values()):
            if link.is_open:
                with contextlib.suppress(TransportError, LinkClosed):
                    await link.stream_stop()

    # ------------------------------------------------------ inbound dispatch

    def _on_unsolicited(self, frame: Frame, link: Link) -> None:
        try:
            if frame.msg_type == M.Msg.SAMPLE_BLOCK:
                self._on_sample_block(frame, link)
            elif frame.msg_type == M.Msg.STATUS:
                self._on_status(M.parse_json(frame))
            elif frame.msg_type == M.Msg.DEVICE_INFO:
                self._apply_info(M.parse_json(frame))
            elif frame.msg_type == M.Msg.LOG:
                self._on_log(M.parse_json(frame))
            elif frame.msg_type == M.Msg.CONFIG:
                # The board answers DISPLAY_SET with its whole CONFIG, and since
                # that request is fire-and-forget the answer lands here. It used
                # to be dropped, which left this copy of the display settings
                # stale -- and the next sync from it put old values back on the
                # controls.
                self.config = M.parse_json(frame)
                self._emit("config", config=self.config)
            elif frame.msg_type == M.Msg.WIFI_STATUS:
                self.status["wifi"] = M.parse_json(frame)
                self._emit("status", status=self.status)
            elif frame.msg_type == M.Msg.SIM_CAL:
                # Broadcast to every session when a sweep finishes, not only to
                # whoever asked — another host watching this board needs to know
                # its wiper table moved underneath it.
                self.sim_cal = M.parse_json(frame)
                self._emit("sim_cal", table=self.sim_cal)
            elif frame.msg_type == M.Msg.ERROR:
                err = M.parse_error(frame)
                log.warning("%s: unsolicited error %s", self.label, err)
                self._emit("error", code=err.code, message=err.message)
        except M.ProtocolError as exc:
            log.warning("%s: bad %s frame: %s", self.label,
                        M.MSG_NAMES.get(frame.msg_type, frame.msg_type), exc)

    def _on_sample_block(self, frame: Frame, link: Link, *, backfill: bool = False) -> None:
        block = M.decode_sample_block(frame.payload)
        self.ingest_block(block, link_key=link.key, backfill=backfill)

    def ingest_block(self, block: M.SampleBlock, *, link_key: str = "", backfill: bool = False) -> None:
        """Feed one block through dedup and into the live buffer and any recorder."""
        if not self.timebase.ready:
            self.timebase.bootstrap(block.t0_us)

        if block.is_decimated:
            # A bandwidth-limited link (BLE) sent a thinned-out view. It is good
            # enough to watch, but feeding it to the aggregator would manufacture
            # gaps for rows the device never intended to send, and then chase them
            # with backfill requests over the slowest link in the system.
            self._ingest_live_only(block)
            return

        ordered = self.aggregator.feed(block, link=link_key, backfill=backfill)
        if not ordered:
            return

        # Convert device time to UTC once per block. The recorder wants the same
        # timestamps the live buffer got, and fitting them twice was not only
        # wasted work but a way for the two to disagree if the timebase fit were
        # updated in between.
        per_block_times = []
        for ready in ordered:
            times_s = self.timebase.to_utc_ns(ready.device_times_us().astype(np.float64)) / 1e9
            per_block_times.append(times_s)
            self.live.append(times_s, self._reindex(ready), ready.sequences())

        if self.on_blocks is not None:
            try:
                merged_times = (per_block_times[0] if len(per_block_times) == 1
                                else np.concatenate(per_block_times))
                self.on_blocks(ordered, merged_times)
            except Exception:
                log.exception("recorder callback failed")

        self._emit("samples", rows=sum(b.n_samples for b in ordered), backfill=backfill)

    def _ingest_live_only(self, block: M.SampleBlock) -> None:
        """Display a block without recording it or letting it drive gap detection."""
        times_s = self.timebase.to_utc_ns(block.device_times_us().astype(np.float64)) / 1e9
        last = self.live.latest()
        if last is not None and times_s.size and times_s[-1] <= last[0]:
            return  # older than what the primary link already delivered
        self.live.append(times_s, self._reindex(block), block.sequences())
        self._emit("samples", rows=block.n_samples, backfill=False, decimated=True)

    def _reindex(self, block: M.SampleBlock) -> np.ndarray:
        """Map a block's columns onto this device's canonical channel order.

        The mapping only changes when the board reports a different channel set,
        which is once per connection — so it is worked out once and cached, and
        the per-block cost is a single fancy-index. The loop this replaces did a
        linear scan of the id tuple twice per output column, on every block.
        """
        if block.channel_ids == self.channel_order:
            return block.data
        take, missing = self._reindex_plan(block.channel_ids)
        out = block.data[:, take]
        if missing.size:
            out[:, missing] = np.nan
        return out

    def _reindex_plan(self, source_ids: tuple[int, ...]) -> tuple[np.ndarray, np.ndarray]:
        """(column to take per output channel, output columns with no source)."""
        cached = self._reindex_cache.get(source_ids)
        if cached is not None:
            return cached
        where = {cid: i for i, cid in enumerate(source_ids)}
        take = np.zeros(len(self.channel_order), dtype=np.intp)
        absent = []
        for col, cid in enumerate(self.channel_order):
            index = where.get(cid)
            if index is None:
                absent.append(col)     # take[col] stays 0; overwritten with NaN
            else:
                take[col] = index
        plan = (take, np.asarray(absent, dtype=np.intp))
        # Bounded by how many distinct channel sets one board can report, which
        # in practice is one or two.
        if len(self._reindex_cache) > 8:
            self._reindex_cache.clear()
        self._reindex_cache[source_ids] = plan
        return plan

    def _on_status(self, status: dict) -> None:
        self.status = status
        self._last_status_at = time.monotonic()
        self.timebase.note_boot_id(status.get("boot_id"))
        if self.timebase.reboot_detected:
            log.warning("%s rebooted; resetting the sample stream", self.label)
            self.timebase.clear_reboot_flag()
            self.aggregator.reset()
            self.live.clear()
            self._emit("reboot")
            self._spawn(self._resync_after_reboot())
        cal_rev = status.get("cal_rev")
        if cal_rev is not None and cal_rev != self.calibration.rev:
            # Another host recalibrated the board underneath us.
            self._spawn(self.refresh_calibration())
        self._emit("status", status=status)
        self._update_health()

    async def _resync_after_reboot(self) -> None:
        link = self.primary_link
        if link is None:
            return
        await self._sync_burst(link)
        if self._streaming:
            await self._start_stream_on(link)

    def _on_log(self, entry: dict) -> None:
        self._log_lines.append(entry)
        del self._log_lines[:-500]
        level = str(entry.get("level", "info")).lower()
        message = f"{self.label}: {entry.get('msg', '')}"
        getattr(log, level if level in ("debug", "info", "warning", "error") else "info")(message)
        self._emit("log", entry=entry)

    @property
    def log_lines(self) -> list[dict]:
        return list(self._log_lines)

    # ------------------------------------------------------------ housekeeping

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _housekeeping(self) -> None:
        """Periodic time sync, gap backfill and health assessment."""
        if getattr(self, "_housekeeping_running", False):
            return
        self._housekeeping_running = True
        try:
            while self._streaming and not self._closing:
                await asyncio.sleep(0.25)
                if not self.links:
                    continue
                with contextlib.suppress(Exception):
                    await self._sync_tick()
                with contextlib.suppress(Exception):
                    await self._backfill_tick()
                self._update_health()
        finally:
            self._housekeeping_running = False

    async def _backfill_tick(self) -> None:
        link = self.backfill_link
        if link is None:
            return
        for gap in self.aggregator.due_gaps():
            if gap.first_seq in self._backfill_inflight:
                continue
            self._backfill_inflight.add(gap.first_seq)
            self.aggregator.mark_requested(gap)
            self._spawn(self._run_backfill(link, gap.first_seq, gap.last_seq))

    async def _run_backfill(self, link: Link, first_seq: int, last_seq: int) -> None:
        try:
            log.info("%s: backfilling %d..%d over %s", self.label, first_seq, last_seq, link.kind)
            blocks, summary = await link.get_range(first_seq, last_seq)
            for block in blocks:
                self.ingest_block(block, link_key=link.key, backfill=True)
            for missing in summary.get("missing") or []:
                if isinstance(missing, (list, tuple)) and len(missing) == 2:
                    self.aggregator.mark_unrecoverable(int(missing[0]), int(missing[1]))
                    log.warning(
                        "%s: rows %d..%d have aged out of the board's ring buffer and are "
                        "gone for good", self.label, int(missing[0]), int(missing[1])
                    )
            self._emit("backfill", first_seq=first_seq, last_seq=last_seq,
                       recovered=sum(b.n_samples for b in blocks),
                       missing=summary.get("missing") or [])
        except (TimeoutError, LinkClosed, TransportError) as exc:
            log.warning("%s: backfill %d..%d failed: %s", self.label, first_seq, last_seq, exc)
        except M.DeviceError as exc:
            if exc.code == "range_evicted":
                self.aggregator.mark_unrecoverable(first_seq, last_seq)
            log.warning("%s: backfill refused: %s", self.label, exc)
        finally:
            self._backfill_inflight.discard(first_seq)

    def _update_health(self) -> None:
        if not self.links:
            return
        problems = []
        if self._last_status_at and time.monotonic() - self._last_status_at > STATUS_TIMEOUT_S:
            problems.append("no status from the board")
        if self.aggregator.stats.rows_lost:
            problems.append(f"{self.aggregator.stats.rows_lost} rows lost")
        if self.timebase.fit.n_points and self.timebase.fit.uncertainty_ns > 50e6:
            problems.append("time sync poor")
        self._set_state(
            DeviceState.DEGRADED if problems else DeviceState.ONLINE,
            "; ".join(problems) or None,
        )

    # ----------------------------------------------------------------- control

    async def _on_primary(self, coro_factory, what: str):
        link = self.primary_link
        if link is None:
            raise LinkClosed(f"{self.label} has no open link, cannot {what}")
        return await coro_factory(link)

    async def refresh_config(self) -> dict:
        self.config = await self._on_primary(lambda link: link.get_config(), "read config")
        self._emit("config", config=self.config)
        return self.config

    async def set_config(self, patch: dict, *, volatile: bool = False) -> dict:
        self.config = await self._on_primary(
            lambda link: link.set_config(patch, volatile=volatile), "write config"
        )
        self._emit("config", config=self.config)
        return self.config

    async def refresh_calibration(self) -> CalibrationSet:
        raw = await self._on_primary(lambda link: link.get_cal(), "read calibration")
        self.calibration = CalibrationSet.from_json(raw)
        self._emit("cal", calibration=self.calibration)
        return self.calibration

    async def write_calibration(self, cal: CalibrationSet) -> CalibrationSet:
        """Validate, write to NVS, and read back what the device actually stored."""
        from ..calibration.models import validate

        problems = validate(cal)
        if problems:
            raise ValueError("Calibration rejected:\n  " + "\n  ".join(problems))
        raw = await self._on_primary(lambda link: link.set_cal(cal.to_json()), "write calibration")
        self.calibration = CalibrationSet.from_json(raw)
        self._emit("cal", calibration=self.calibration)
        return self.calibration

    async def set_display(self, **kwargs) -> None:
        await self._on_primary(lambda link: link.display_set(**kwargs), "set the display")
        # Keep this copy current without waiting for the board's CONFIG answer,
        # which a firmware is not obliged to send; when it does, it replaces this.
        patch = {k: v for k, v in kwargs.items() if v is not None}
        self.config = {**self.config, "display": {**(self.config.get("display") or {}), **patch}}
        self._emit("config", config=self.config)

    async def identify(self, seconds: float = 5.0) -> None:
        await self._on_primary(lambda link: link.identify(seconds), "identify")

    async def run_self_test(self) -> dict:
        self.self_test_result = await self._on_primary(lambda link: link.self_test(), "self test")
        self._emit("self_test", result=self.self_test_result)
        return self.self_test_result

    async def provision_wifi(self, ssid: str, psk: str) -> dict:
        return await self._on_primary(
            lambda link: link.wifi_provision(ssid, psk), "provision WiFi"
        )

    # ------------------------------------------------------------- simulator

    @property
    def has_simulator(self) -> bool:
        """Whether this board emulates a PT1000 output.

        Announced in DEVICE_INFO caps, so the UI never offers the controls to a
        board that would answer ERROR "unsupported".
        """
        return bool(self.info.get("caps", {}).get("sim", {}).get("pt1000_out"))

    @property
    def sim_status(self) -> dict:
        return self.status.get("sim") or {}

    async def calibrate_simulator(self) -> dict:
        """Start a wiper sweep and return the board's acknowledgement.

        Deliberately does not wait for the sweep: it takes ~15 s, and a caller
        that blocks for that long cannot show the progress the board is
        reporting meanwhile in STATUS. Watch for the ``sim_cal`` event.
        """
        ack = await self._on_primary(
            lambda link: link.sim_calibrate(time.time()), "calibrate the simulator"
        )
        self._emit("sim_cal_started")
        return ack

    async def refresh_sim_cal(self) -> dict:
        self.sim_cal = await self._on_primary(
            lambda link: link.get_sim_cal(), "read the simulator table"
        )
        self._emit("sim_cal", table=self.sim_cal)
        return self.sim_cal

    async def set_sim(self, **fields) -> dict:
        """Patch the ``sim`` config block. ``source=None`` parks the output."""
        return await self.set_config({"sim": {k: v for k, v in fields.items()}})

    async def set_wire_mode(self, wires: int) -> dict:
        if wires not in (2, 3, 4):
            raise ValueError("PT1000 wire count must be 2, 3 or 4")
        return await self.set_config({"rtd": {"wires": wires}})

    # ------------------------------------------------------------- read models

    def latest_values(self) -> dict[int, float]:
        """Most recent reading per channel, NaN where unavailable."""
        latest = self.live.latest()
        if latest is None:
            return {cid: float("nan") for cid in self.channel_order}
        _, row = latest
        return {cid: float(row[i]) for i, cid in enumerate(self.channel_order)}

    def latest_with_age(self) -> dict[int, tuple[float, float]]:
        """Per channel: (value, age in seconds). Falls back to the last good reading."""
        out: dict[int, tuple[float, float]] = {}
        t, v = self.live.view(400)
        if t.size == 0:
            return {cid: (float("nan"), float("inf")) for cid in self.channel_order}
        now = float(t[-1])
        for i, cid in enumerate(self.channel_order):
            col = v[:, i]
            good = np.flatnonzero(np.isfinite(col))
            if good.size:
                out[cid] = (float(col[good[-1]]), now - float(t[good[-1]]))
            else:
                out[cid] = (float("nan"), float("inf"))
        return out

    def channel_index(self, channel_id: int) -> int | None:
        try:
            return self.channel_order.index(channel_id)
        except ValueError:
            return None

    def series(self, channel_id: int, seconds: float | None = None):
        """(times, values) for one channel from the live buffer."""
        idx = self.channel_index(channel_id)
        if idx is None:
            return np.zeros(0), np.zeros(0)
        if seconds is None:
            t, v = self.live.view()
        else:
            t, v = self.live.window(seconds)
        return t, v[:, idx]

    def faults(self) -> dict:
        """Decoded sensor faults from the last STATUS, ready to display."""
        from ..sensors.rtd import decode_max31865_fault
        from ..sensors.thermocouple import decode_max31856_fault

        raw = self.status.get("faults") or {}
        out: dict[str, list[dict]] = {}
        rtd = raw.get("max31865")
        if isinstance(rtd, dict) and rtd.get("reg"):
            out["max31865"] = decode_max31865_fault(int(rtd["reg"]))
        tc = raw.get("max31856")
        if isinstance(tc, dict) and tc.get("reg"):
            out["max31856"] = decode_max31856_fault(int(tc["reg"]))
        aht = raw.get("aht20")
        if aht:
            out["aht20"] = [{"name": "aht20", "detail": str(aht)}]
        return out

    def health(self) -> dict:
        return {
            "serial": self.serial,
            "name": self.name,
            "state": self.state.value,
            "error": self.error,
            "links": [
                {
                    "kind": link.kind,
                    "address": link.info.address,
                    "open": link.is_open,
                    "rtt_ms": round(link.median_rtt_ms, 2),
                    "bytes_rx": link.transport.stats.bytes_rx,
                    "bad_frames": link.transport.stats.bad_frames,
                }
                for link in self.links.values()
            ],
            "stream": {"active": self._streaming, "rate_hz": self._stream_rate_hz},
            "timebase": self.timebase.fit.to_json(),
            "aggregator": self.aggregator.health(),
            "battery": self.status.get("battery", {}),
            "cal_rev": self.calibration.rev,
            "uncalibrated": "uncalibrated" in (self.status.get("flags") or []),
        }

    # ---------------------------------------------------------------- shutdown

    async def close(self) -> None:
        self._closing = True
        self._streaming = False
        with contextlib.suppress(Exception):
            await self.stop_streaming()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        for link in list(self.links.values()):
            with contextlib.suppress(Exception):
                await link.stop()
        self.links.clear()
        self._set_state(DeviceState.OFFLINE)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Device {self.label} {self.state.value} links={len(self.links)}>"


#: Backoff between reconnect attempts. The last value repeats forever: a board
#: powered down overnight should be picked up when it comes back, without a
#: retry storm in the meantime.
RECONNECT_DELAYS_S = (2.0, 5.0, 10.0, 20.0, 30.0, 60.0)


class DeviceManager:
    """Owns every known device and the reconnect policy.

    Devices are keyed by serial number, so unplugging USB and plugging it into a
    different port -- or the board reappearing on WiFi with a new IP -- reattaches
    to the same Device object with its history intact, rather than spawning a
    duplicate.

    When a link dies on its own -- WiFi dropped, cable pulled, board rebooted --
    and it was the device's last one, the address it was reached on is retried
    with a backoff until it answers again. A link the user closed is not retried:
    that was an instruction, not a failure.
    """

    def __init__(self) -> None:
        self.devices: dict[str, Device] = {}
        self._listeners: list[Callable[[DeviceEvent], None]] = []
        #: Used only for a board whose CONFIG carries no acquire.rate_hz.
        self.default_rate_hz = 10.0
        self._auto_reconnect = True
        self._reconnect_tasks: dict[str, asyncio.Task] = {}
        self._attempts: dict[str, int] = defaultdict(int)
        #: Where each device was last reachable, per link key.
        self._addresses: dict[str, dict[str, Candidate]] = defaultdict(dict)
        self._closing = False

    @property
    def auto_reconnect(self) -> bool:
        return self._auto_reconnect

    @auto_reconnect.setter
    def auto_reconnect(self, on: bool) -> None:
        self._auto_reconnect = bool(on)
        if not on:
            for task in self._reconnect_tasks.values():
                task.cancel()

    def _remember_address(self, device: Device, link: Link) -> None:
        if not device.serial:
            return
        self._addresses[device.serial][link.key] = Candidate(
            kind=link.info.kind,
            address=link.info.address,
            label=link.info.label,
            serial=device.serial or None,
        )

    def _on_link_event(self, event: DeviceEvent) -> None:
        """Start a reconnect when a device loses its last link unexpectedly."""
        if event.kind != "links" or not event.data.get("closed"):
            return
        device = event.device
        if device.links or device._closing or self._closing:
            return
        if not self._auto_reconnect or self.devices.get(device.serial) is not device:
            return
        self._start_reconnect(device)

    def _start_reconnect(self, device: Device) -> None:
        serial = device.serial
        existing = self._reconnect_tasks.get(serial)
        if existing is not None and not existing.done():
            return
        addresses = list(self._addresses.get(serial, {}).values())
        if not addresses:
            return
        task = asyncio.ensure_future(self._reconnect_loop(device, addresses))
        self._reconnect_tasks[serial] = task

    async def _reconnect_loop(self, device: Device, addresses: list[Candidate]) -> None:
        serial = device.serial
        try:
            while (
                self._auto_reconnect
                and not self._closing
                and not device._closing
                and not device.links
                and self.devices.get(serial) is device
            ):
                attempt = self._attempts[serial]
                self._attempts[serial] = attempt + 1
                delay = RECONNECT_DELAYS_S[min(attempt, len(RECONNECT_DELAYS_S) - 1)]
                await asyncio.sleep(delay)
                if device.links or self.devices.get(serial) is not device:
                    return
                for candidate in addresses:
                    try:
                        link = await device.add_transport(build_transport(candidate))
                    except Exception as exc:
                        log.info("%s: reconnect to %s failed: %s",
                                  device.label, candidate.address, exc)
                        continue
                    self._attempts[serial] = 0
                    self._remember_address(device, link)
                    log.info("%s: reconnected over %s", device.label, candidate.address)
                    self._emit_manager("reconnected", device)
                    return
        except asyncio.CancelledError:
            raise
        finally:
            self._reconnect_tasks.pop(serial, None)

    def subscribe(self, listener: Callable[[DeviceEvent], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        for device in self.devices.values():
            device.subscribe(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    async def connect(self, transport: Transport, *, start_stream: bool = True) -> Device:
        """Bring up a transport and attach it to the right Device.

        The serial is unknown until the handshake completes, so a provisional
        Device is used and merged into an existing one if the serial turns out to
        already be known.
        """
        provisional = Device()
        provisional._stream_rate_hz = self.default_rate_hz
        for listener in self._listeners:
            provisional.subscribe(listener)
        await provisional.add_transport(transport, start_stream=start_stream)

        serial = provisional.serial
        existing = self.devices.get(serial)
        if existing is not None and existing is not provisional:
            # Move the link across and discard the provisional wrapper.
            link = next(iter(provisional.links.values()))
            provisional.links.clear()
            provisional._closing = True
            link.on_unsolicited = existing._on_unsolicited
            existing.links[link.key] = link
            existing._spawn(existing._watch_link(link))
            if existing._streaming:
                await existing._start_stream_on(link)
            existing._set_state(DeviceState.ONLINE)
            self._remember_address(existing, link)
            existing._emit("links")
            return existing

        self.devices[serial] = provisional
        provisional.subscribe(self._on_link_event)
        for link in provisional.links.values():
            self._remember_address(provisional, link)
        self._emit_manager("device_added", provisional)
        return provisional

    def _emit_manager(self, kind: str, device: Device) -> None:
        event = DeviceEvent(kind=kind, device=device)
        for listener in list(self._listeners):
            with contextlib.suppress(Exception):
                listener(event)

    def get(self, serial: str) -> Device | None:
        return self.devices.get(serial)

    @property
    def online(self) -> list[Device]:
        return [d for d in self.devices.values() if d.is_online]

    async def disconnect(self, serial: str) -> None:
        # Drop the remembered addresses first: a user-initiated disconnect must
        # not be undone a couple of seconds later by the reconnect loop.
        self._addresses.pop(serial, None)
        self._attempts.pop(serial, None)
        task = self._reconnect_tasks.pop(serial, None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        device = self.devices.pop(serial, None)
        if device is not None:
            await device.close()
            self._emit_manager("device_removed", device)

    async def close(self) -> None:
        self._closing = True
        tasks = list(self._reconnect_tasks.values())
        self._reconnect_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            # Awaiting them is what stops "Task was destroyed but it is pending"
            # on the way out, and guarantees none is mid-handshake on a
            # transport we are about to close underneath it.
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(
            *(device.close() for device in self.devices.values()), return_exceptions=True
        )
        self.devices.clear()
        self._addresses.clear()

    def health(self) -> list[dict]:
        return [device.health() for device in self.devices.values()]
