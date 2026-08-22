"""Serving the simulated board over TCP, a pty, or an in-process pipe.

Three ways to reach a simulated board:

* **TCP** -- exactly what the real board does over WiFi, so this is the highest
  fidelity option and works on every platform.
* **pty** -- a real character device the app opens like any serial port, which
  exercises the USB CDC code path. POSIX only; Windows has no equivalent that a
  serial library will open.
* **in-process** -- a direct transport with no OS involvement, used by the test
  suite because it is deterministic and instant.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time

from ..protocol.framing import FrameReader, encode_frame
from ..protocol.messages import DEFAULT_TCP_PORT
from ..transport.base import Transport, TransportInfo
from .board import SimSession, SimulatedBoard

log = logging.getLogger(__name__)

TICK_S = 0.05


class SimulatorRuntime:
    """Drives one board's physics and fans it out to every connected session."""

    def __init__(self, board: SimulatedBoard) -> None:
        self.board = board
        self.sessions: dict[int, tuple[SimSession, asyncio.Queue]] = {}
        self._next_id = 0
        self._task: asyncio.Task | None = None

    def attach(self, *, decimate: bool = False) -> tuple[int, SimSession, asyncio.Queue]:
        session_id = self._next_id
        self._next_id += 1
        session = SimSession(self.board, decimate=decimate)
        out: asyncio.Queue[bytes] = asyncio.Queue(maxsize=256)
        self.sessions[session_id] = (session, out)
        # Every connection gets DEVICE_INFO unsolicited, per protocol §12.
        from ..protocol.framing import Frame
        from ..protocol.messages import Msg, json_payload

        out.put_nowait(encode_frame(Frame(Msg.DEVICE_INFO, json_payload(self.board.device_info()))))
        return session_id, session, out

    def detach(self, session_id: int) -> None:
        self.sessions.pop(session_id, None)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="tjip-sim-runtime")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            self.board.acquire_due()
            now = time.monotonic()
            for session, out in list(self.sessions.values()):
                frames = session.pending_stream_frames() + session.pending_status_frame(now)
                for frame in frames:
                    data = encode_frame(frame)
                    try:
                        out.put_nowait(data)
                    except asyncio.QueueFull:
                        # A host that cannot keep up loses live data and backfills it,
                        # which is exactly what the real firmware does.
                        with contextlib.suppress(asyncio.QueueEmpty):
                            out.get_nowait()

    def handle_bytes(self, session_id: int, data: bytes, reader: FrameReader) -> None:
        entry = self.sessions.get(session_id)
        if entry is None:
            return
        session, out = entry
        for frame in reader.feed(data):
            for response in session.handle(frame):
                with contextlib.suppress(asyncio.QueueFull):
                    out.put_nowait(encode_frame(response))


# ---------------------------------------------------------------------- TCP

async def serve_tcp(
    board: SimulatedBoard,
    host: str = "127.0.0.1",
    port: int = DEFAULT_TCP_PORT,
    runtime: SimulatorRuntime | None = None,
) -> tuple[asyncio.AbstractServer, SimulatorRuntime]:
    runtime = runtime or SimulatorRuntime(board)
    await runtime.start()

    async def on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        session_id, _session, out = runtime.attach()
        frame_reader = FrameReader()
        log.info("simulator: client %s connected (session %d)", peer, session_id)

        async def pump_out() -> None:
            try:
                while True:
                    data = await out.get()
                    writer.write(data)
                    await writer.drain()
            except (ConnectionError, OSError):
                pass

        sender = asyncio.create_task(pump_out())
        try:
            while True:
                data = await reader.read(8192)
                if not data:
                    break
                runtime.handle_bytes(session_id, data, frame_reader)
        except (ConnectionError, OSError):
            pass
        finally:
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender
            runtime.detach(session_id)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            log.info("simulator: client %s disconnected", peer)

    server = await asyncio.start_server(on_client, host, port)
    return server, runtime


# ---------------------------------------------------------------------- pty

async def serve_pty(
    board: SimulatedBoard, runtime: SimulatorRuntime | None = None
) -> tuple[str, SimulatorRuntime, asyncio.Task]:
    """Create a pty pair and serve the board on it. Returns the slave device path.

    POSIX only. The returned path can be handed to ``SerialTransport`` unchanged,
    which makes this the way to test the USB code path without hardware.
    """
    if os.name != "posix":
        raise RuntimeError(
            "pty simulation needs POSIX. On Windows use the TCP simulator "
            "(tjiptemp-sim --tcp) or a null-modem tool such as com0com."
        )
    import pty
    import tty

    master, slave = pty.openpty()
    tty.setraw(master)
    slave_path = os.ttyname(slave)
    os.set_blocking(master, False)

    runtime = runtime or SimulatorRuntime(board)
    await runtime.start()
    session_id, _session, out = runtime.attach()
    frame_reader = FrameReader()
    loop = asyncio.get_running_loop()

    async def pump() -> None:
        pending = bytearray()
        try:
            while True:
                try:
                    data = os.read(master, 4096)
                    if data:
                        runtime.handle_bytes(session_id, data, frame_reader)
                except BlockingIOError:
                    pass
                except OSError:
                    break

                while not out.empty():
                    pending += out.get_nowait()
                if pending:
                    try:
                        written = os.write(master, bytes(pending))
                        del pending[:written]
                    except BlockingIOError:
                        pass
                    except OSError:
                        break
                await asyncio.sleep(0.005)
        finally:
            runtime.detach(session_id)
            with contextlib.suppress(OSError):
                os.close(master)
            with contextlib.suppress(OSError):
                os.close(slave)

    task = loop.create_task(pump(), name="tjip-sim-pty")
    return slave_path, runtime, task


# ------------------------------------------------------------- in-process

class LoopbackTransport(Transport):
    """Talks to a SimulatorRuntime directly, with no OS in the way.

    Used by the tests: deterministic, no ports, no sockets, and it still exercises
    the real framing and the real device state machine.
    """

    _instances = 0

    def __init__(self, runtime: SimulatorRuntime, label: str = "simulator",
                 *, decimate: bool = False) -> None:
        # Each loopback is a distinct link even though it reaches the same board,
        # so it needs its own address — otherwise two of them collide in the
        # device's link table and the second silently replaces the first.
        LoopbackTransport._instances += 1
        instance = LoopbackTransport._instances
        super().__init__(
            TransportInfo(
                kind="sim",
                address=f"{runtime.board.serial}#{instance}",
                label=f"{label} #{instance}",
                serial=runtime.board.serial,
                typical_latency_s=0.0001,
                throughput_bps=10_000_000,
                supports_backfill=True,
                priority=90,
            )
        )
        self.runtime = runtime
        self.decimate = decimate
        self._session_id: int | None = None
        self._out: asyncio.Queue[bytes] | None = None
        self._reader = FrameReader()
        self._task: asyncio.Task | None = None

    async def _open_impl(self) -> None:
        await self.runtime.start()
        self._session_id, _session, self._out = self.runtime.attach(decimate=self.decimate)
        self._task = asyncio.create_task(self._pump(), name="tjip-sim-loopback")

    async def _pump(self) -> None:
        assert self._out is not None
        try:
            while True:
                data = await self._out.get()
                self._ingest(data)
        except asyncio.CancelledError:
            raise

    async def _write_impl(self, data: bytes) -> None:
        if self._session_id is None:
            raise RuntimeError("loopback transport is not open")
        self.runtime.handle_bytes(self._session_id, data, self._reader)

    async def _close_impl(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._session_id is not None:
            self.runtime.detach(self._session_id)
            self._session_id = None


async def make_loopback(
    board: SimulatedBoard | None = None, **kwargs
) -> tuple[LoopbackTransport, SimulatorRuntime]:
    """Convenience for tests: a running simulator and a transport into it."""
    board = board or SimulatedBoard(**kwargs)
    runtime = SimulatorRuntime(board)
    await runtime.start()
    return LoopbackTransport(runtime), runtime
