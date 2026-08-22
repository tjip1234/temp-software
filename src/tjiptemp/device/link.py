"""One connection to a board: request/response correlation over a transport.

A Link owns a transport, allocates sequence numbers, matches responses to the
requests that are waiting for them, and routes everything unsolicited to a
callback. Multi-frame replies (the MORE flag, used by GET_RANGE) are collected
into a list before the waiter is woken.

Errors surface where they were caused: an ERROR frame answering a request is
raised as :class:`DeviceError` from the ``request`` call, not logged somewhere
distant. That is the single most useful property of this layer.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ..protocol import messages as M
from ..protocol.framing import Frame
from ..transport.base import Transport, TransportError

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 4.0
#: GET_RANGE can legitimately take a while: it is reading a big ring buffer out.
RANGE_TIMEOUT_S = 30.0


class LinkClosed(Exception):
    """The link went away while a request was in flight."""


@dataclass
class _Pending:
    expect: int | None
    future: asyncio.Future
    frames: list[Frame] = field(default_factory=list)
    sent_at: float = field(default_factory=time.monotonic)


class Link:
    """Request/response session over one transport."""

    def __init__(
        self,
        transport: Transport,
        on_unsolicited: Callable[[Frame, Link], None] | None = None,
    ) -> None:
        self.transport = transport
        self.on_unsolicited = on_unsolicited
        self._pending: dict[int, _Pending] = {}
        self._seq = 0
        self._task: asyncio.Task | None = None
        self._closed = asyncio.Event()
        #: Round-trip times of recent requests, for the link quality display.
        self.rtt_samples: list[float] = []

    # ---------------------------------------------------------------- identity

    @property
    def info(self):
        return self.transport.info

    @property
    def kind(self) -> str:
        return self.transport.info.kind

    @property
    def key(self) -> str:
        return self.transport.info.key

    @property
    def is_open(self) -> bool:
        return self.transport.is_open and not self._closed.is_set()

    @property
    def median_rtt_ms(self) -> float:
        if not self.rtt_samples:
            return 0.0
        ordered = sorted(self.rtt_samples)
        return ordered[len(ordered) // 2] * 1000.0

    # --------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        await self.transport.open()
        self._closed.clear()
        self._task = asyncio.create_task(self._pump(), name=f"tjip-link-{self.key}")

    async def stop(self) -> None:
        self._closed.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self.transport.close()
        self._fail_pending(LinkClosed(f"{self.info} closed"))

    async def wait_closed(self) -> None:
        await self._closed.wait()

    def _fail_pending(self, exc: Exception) -> None:
        for pending in list(self._pending.values()):
            if not pending.future.done():
                pending.future.set_exception(exc)
        self._pending.clear()

    # ------------------------------------------------------------------- pump

    async def _pump(self) -> None:
        try:
            while True:
                frame = await self.transport.rx.get()
                if frame is None:
                    break
                self._dispatch(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("link pump failed on %s", self.info)
        finally:
            self._closed.set()
            self._fail_pending(LinkClosed(f"{self.info} closed"))

    def _dispatch(self, frame: Frame) -> None:
        pending = self._pending.get(frame.seq) if frame.seq else None
        if pending is None:
            if self.on_unsolicited is not None:
                try:
                    self.on_unsolicited(frame, self)
                except Exception:
                    log.exception("unsolicited handler raised for %r", frame)
            return

        if frame.is_error:
            self._pending.pop(frame.seq, None)
            if not pending.future.done():
                pending.future.set_exception(M.parse_error(frame))
            return

        pending.frames.append(frame)
        if frame.has_more:
            return  # more of this reply is coming

        self._pending.pop(frame.seq, None)
        self.rtt_samples.append(time.monotonic() - pending.sent_at)
        if len(self.rtt_samples) > 32:
            del self.rtt_samples[:-32]
        if not pending.future.done():
            pending.future.set_result(pending.frames)

    # --------------------------------------------------------------- requests

    def _next_seq(self) -> int:
        # seq 0 is reserved for unsolicited device frames.
        for _ in range(0x10000):
            self._seq = (self._seq + 1) & 0xFFFF
            if self._seq != 0 and self._seq not in self._pending:
                return self._seq
        raise RuntimeError("no free sequence numbers: too many requests in flight")

    async def request_all(
        self,
        frame: Frame,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
        expect: int | None = None,
    ) -> list[Frame]:
        """Send a request and wait for every frame of its reply."""
        if not self.is_open:
            raise LinkClosed(f"{self.info} is not open")
        seq = self._next_seq()
        frame.seq = seq
        expect = expect if expect is not None else M.RESPONSE_FOR.get(frame.msg_type)
        loop = asyncio.get_running_loop()
        pending = _Pending(expect=expect, future=loop.create_future())
        self._pending[seq] = pending
        try:
            await self.transport.send(frame)
        except TransportError:
            self._pending.pop(seq, None)
            raise
        try:
            return await asyncio.wait_for(pending.future, timeout)
        except TimeoutError as exc:
            self._pending.pop(seq, None)
            name = M.MSG_NAMES.get(frame.msg_type, hex(frame.msg_type))
            raise TimeoutError(
                f"{self.info}: no reply to {name} within {timeout:g} s"
            ) from exc

    async def request(self, frame: Frame, *, timeout: float = DEFAULT_TIMEOUT_S) -> Frame:
        """Send a request and return the single reply frame."""
        frames = await self.request_all(frame, timeout=timeout)
        if not frames:
            raise LinkClosed(f"{self.info}: empty reply")
        return frames[0]

    async def request_json(self, frame: Frame, *, timeout: float = DEFAULT_TIMEOUT_S) -> dict:
        return M.parse_json(await self.request(frame, timeout=timeout))

    async def send(self, frame: Frame) -> None:
        """Fire and forget, for messages the device does not answer."""
        await self.transport.send(frame)

    # ------------------------------------------------------- protocol helpers

    async def hello(self, host_name: str = "tjiptemp-host") -> dict:
        return await self.request_json(M.hello(host_name))

    async def get_config(self) -> dict:
        return await self.request_json(M.get_config())

    async def set_config(self, patch: dict, *, volatile: bool = False) -> dict:
        return await self.request_json(M.set_config(patch, volatile=volatile), timeout=8.0)

    async def get_cal(self) -> dict:
        return await self.request_json(M.get_cal())

    async def set_cal(self, cal: dict) -> dict:
        # NVS writes are slow and must not be interrupted; give them room.
        return await self.request_json(M.set_cal(cal), timeout=15.0)

    async def stream_start(self, rate_hz: float, channels="all") -> None:
        await self.send(M.stream_start(rate_hz, channels))

    async def stream_stop(self) -> None:
        await self.send(M.stream_stop())

    async def time_sync(self) -> tuple[int, int, int, int]:
        """One SNTP-style exchange. Returns (t1_host_ns, t2_dev_us, t3_dev_us, t4_host_ns)."""
        t1 = time.time_ns()
        reply = await self.request(
            Frame(M.Msg.TIME_SYNC, M.encode_time_sync(t1)), timeout=2.0
        )
        t4 = time.time_ns()
        echoed_t1, t2, t3 = M.decode_time_echo(reply.payload)
        if echoed_t1 != t1:
            raise M.ProtocolError("TIME_ECHO did not echo the timestamp we sent")
        return t1, t2, t3, t4

    async def get_range(self, from_seq: int, to_seq: int) -> tuple[list[M.SampleBlock], dict]:
        """Backfill request. Returns the recovered blocks and the RANGE_END summary."""
        frames = await self.request_all(
            M.get_range(from_seq, to_seq), timeout=RANGE_TIMEOUT_S, expect=M.Msg.RANGE_END
        )
        blocks: list[M.SampleBlock] = []
        summary: dict = {}
        for frame in frames:
            if frame.msg_type == M.Msg.RANGE_BLOCK:
                with contextlib.suppress(M.ProtocolError):
                    blocks.append(M.decode_sample_block(frame.payload))
            elif frame.msg_type == M.Msg.RANGE_END:
                summary = M.parse_json(frame)
        return blocks, summary

    async def display_set(self, **kwargs) -> None:
        await self.send(M.display_set(**kwargs))

    async def identify(self, seconds: float = 5.0) -> None:
        await self.send(M.identify(seconds))

    async def self_test(self) -> dict:
        return await self.request_json(M.self_test(), timeout=20.0)

    async def wifi_provision(self, ssid: str, psk: str) -> dict:
        return await self.request_json(M.wifi_provision(ssid, psk), timeout=30.0)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Link {self.info} {'open' if self.is_open else 'closed'}>"
