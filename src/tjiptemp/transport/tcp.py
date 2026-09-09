"""TCP transport for the WiFi link.

Byte-for-byte the same framing as USB, so the only real differences are latency
(milliseconds instead of microseconds, and far more jittery) and the fact that a
WiFi link disappears without warning far more often than a cable does. Both are
handled by treating this as a lower-priority link than USB and letting the device
layer's backfill sweep up whatever a dropout lost.

TCP_NODELAY is on: our frames are small and latency-sensitive, and Nagle's
algorithm would add up to 40 ms to a time-sync exchange for no benefit.
"""

from __future__ import annotations

import asyncio

from ..protocol.messages import DEFAULT_TCP_PORT
from .base import Transport, TransportError, TransportInfo

#: One attempt's budget. A board that is up answers in milliseconds; this only
#: has to cover a busy network.
CONNECT_TIMEOUT_S = 10.0

#: How long to keep retrying before giving up, and how long to wait between
#: attempts.
#:
#: A board that has just rebooted is not merely slow to answer, it is not
#: listening at all: it prints its boot report, may sweep its wiper table, then
#: brings up WiFi and has to associate and get a lease before the TCP server
#: exists. That is comfortably tens of seconds, and a single five-second
#: attempt lands in the middle of it and reports a board that is not there.
#: So a connect is a window, not one shot.
CONNECT_WINDOW_S = 45.0
CONNECT_RETRY_S = 2.0

READ_CHUNK = 8192


class TcpTransport(Transport):
    """TJIP-1 over TCP, as served by the board on port 3737."""

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_TCP_PORT,
        *,
        label: str = "",
        serial: str | None = None,
    ) -> None:
        super().__init__(
            TransportInfo(
                kind="wifi",
                address=f"{host}:{port}",
                label=label or f"WiFi {host}:{port}",
                serial=serial,
                typical_latency_s=0.005,
                throughput_bps=2_000_000,
                supports_backfill=True,
                priority=50,
            )
        )
        self.host = host
        self.port = port
        self._reader_stream: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None

    async def _open_impl(self) -> None:
        deadline = asyncio.get_running_loop().time() + CONNECT_WINDOW_S
        last: Exception | None = None
        attempts = 0

        while True:
            attempts += 1
            try:
                self._reader_stream, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port),
                    timeout=CONNECT_TIMEOUT_S,
                )
                break
            except (TimeoutError, OSError) as exc:
                # A refused connection is the normal answer from a board that
                # is booting: the host is up, the TCP server is not yet. It is
                # worth waiting for, not worth reporting.
                last = exc
                if asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(CONNECT_RETRY_S)

        if self._writer is None:
            raise TransportError(
                f"{self.host}:{self.port} did not answer in {CONNECT_WINDOW_S:.0f} s "
                f"({attempts} attempts). Check the board is on the same network, "
                f"that its WiFi is provisioned, and that it is not stuck in a "
                f"reboot loop — the last error was: {last}"
            ) from last

        sock = self._writer.get_extra_info("socket")
        if sock is not None:
            import socket as _socket

            try:
                sock.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)
                sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_KEEPALIVE, 1)
            except OSError:
                pass

        self._task = asyncio.create_task(self._read_loop(), name=f"tjip-tcp-{self.host}")

    async def _read_loop(self) -> None:
        stream = self._reader_stream
        assert stream is not None
        try:
            while True:
                data = await stream.read(READ_CHUNK)
                if not data:
                    self._closed_from_reader("peer closed the connection")
                    return
                self._ingest(data)
        except asyncio.CancelledError:
            raise
        except (OSError, ConnectionError) as exc:
            self._closed_from_reader(str(exc))

    async def _write_impl(self, data: bytes) -> None:
        writer = self._writer
        if writer is None or writer.is_closing():
            raise TransportError(f"{self.info.address} is closed")
        try:
            writer.write(data)
            await writer.drain()
        except (OSError, ConnectionError) as exc:
            raise TransportError(f"{self.info.address}: {exc}") from exc

    async def _close_impl(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        writer, self._writer = self._writer, None
        self._reader_stream = None
        if writer is not None:
            try:
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=2.0)
            except (OSError, TimeoutError, ConnectionError):
                pass


