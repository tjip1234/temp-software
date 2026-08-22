"""Transport abstraction: one interface over USB CDC, TCP/WiFi and BLE.

Every transport is a byte pipe that carries TJIP-1 frames. The differences that
actually matter to the layers above are captured in :class:`TransportInfo` --
throughput, latency, and whether bulk backfill is worth attempting -- rather than
being scattered through the code as ``if isinstance(t, SerialTransport)``.

Frames arrive on an ``asyncio.Queue``; a ``None`` sentinel means the link closed.
Transports never retry or reconnect on their own: that policy belongs to the
device layer, which knows whether another link is already carrying the data.
"""

from __future__ import annotations

import abc
import asyncio
import time
from dataclasses import dataclass, field

from ..protocol.framing import Frame, FrameReader, encode_frame


class TransportError(Exception):
    """The link failed. The device layer decides whether to reconnect."""


@dataclass(slots=True)
class TransportInfo:
    """Everything the layers above need to know about a link without special-casing it."""

    kind: str                      # "usb" | "wifi" | "ble" | "sim"
    address: str                   # port path, host:port, or BLE address
    label: str = ""                # human-facing
    serial: str | None = None      # device serial, once known
    #: Rough one-way latency, used to weight time-sync fits.
    typical_latency_s: float = 0.001
    #: Rough usable throughput in bytes/second.
    throughput_bps: int = 500_000
    #: Whether GET_RANGE backfill over this link is a good idea.
    supports_backfill: bool = True
    #: Preference when several links reach the same board; higher wins.
    priority: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.address}"

    def __str__(self) -> str:
        return self.label or self.key


@dataclass(slots=True)
class TransportStats:
    bytes_rx: int = 0
    bytes_tx: int = 0
    frames_rx: int = 0
    frames_tx: int = 0
    bad_frames: int = 0
    dropped_bytes: int = 0
    opened_at: float = 0.0
    last_rx_at: float = 0.0

    @property
    def uptime_s(self) -> float:
        return time.monotonic() - self.opened_at if self.opened_at else 0.0

    @property
    def silent_for_s(self) -> float:
        return time.monotonic() - self.last_rx_at if self.last_rx_at else 0.0

    @property
    def error_rate(self) -> float:
        total = self.frames_rx + self.bad_frames
        return self.bad_frames / total if total else 0.0


class Transport(abc.ABC):
    """Base class. Subclasses implement ``_open``, ``_close`` and ``_write``."""

    def __init__(self, info: TransportInfo, *, queue_size: int = 512) -> None:
        self.info = info
        self.stats = TransportStats()
        #: Inbound frames. A ``None`` item means the transport has closed.
        self.rx: asyncio.Queue[Frame | None] = asyncio.Queue(maxsize=queue_size)
        self._reader = FrameReader()
        self._open = False
        self._closing = False
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- lifecycle

    @property
    def is_open(self) -> bool:
        return self._open

    async def open(self) -> None:
        if self._open:
            return
        self._closing = False
        self._reader.reset()
        await self._open_impl()
        self._open = True
        self.stats.opened_at = time.monotonic()
        self.stats.last_rx_at = time.monotonic()

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        try:
            await self._close_impl()
        finally:
            self._open = False
            # Wake anyone blocked on rx, without blocking ourselves if it is full.
            try:
                self.rx.put_nowait(None)
            except asyncio.QueueFull:
                pass

    async def __aenter__(self) -> Transport:
        await self.open()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # ------------------------------------------------------------------- io

    async def send(self, frame: Frame) -> None:
        """Serialise and write one frame. Serialised by a lock so frames never interleave."""
        if not self._open:
            raise TransportError(f"{self.info} is not open")
        data = encode_frame(frame)
        async with self._lock:
            await self._write_impl(data)
        self.stats.bytes_tx += len(data)
        self.stats.frames_tx += 1

    def _ingest(self, data: bytes) -> None:
        """Called by subclasses with raw received bytes."""
        if not data:
            return
        self.stats.bytes_rx += len(data)
        self.stats.last_rx_at = time.monotonic()
        before_bad = self._reader.bad_frames
        before_dropped = self._reader.dropped_bytes
        for frame in self._reader.feed(data):
            self.stats.frames_rx += 1
            try:
                self.rx.put_nowait(frame)
            except asyncio.QueueFull:
                # Backpressure: the consumer is not keeping up. Dropping the oldest
                # frame is the right call for a live stream -- stale samples are less
                # useful than fresh ones, and backfill can recover anything essential.
                try:
                    self.rx.get_nowait()
                    self.rx.put_nowait(frame)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
        self.stats.bad_frames += self._reader.bad_frames - before_bad
        self.stats.dropped_bytes += self._reader.dropped_bytes - before_dropped

    def _closed_from_reader(self, error: str | None = None) -> None:
        """Called by a subclass's reader when the underlying pipe went away."""
        self._open = False
        if error:
            self.info.extra["last_error"] = error
        try:
            self.rx.put_nowait(None)
        except asyncio.QueueFull:
            pass

    # ------------------------------------------------------------- subclass api

    @abc.abstractmethod
    async def _open_impl(self) -> None: ...

    @abc.abstractmethod
    async def _close_impl(self) -> None: ...

    @abc.abstractmethod
    async def _write_impl(self, data: bytes) -> None: ...

    def __repr__(self) -> str:  # pragma: no cover
        state = "open" if self._open else "closed"
        return f"<{type(self).__name__} {self.info} {state}>"
