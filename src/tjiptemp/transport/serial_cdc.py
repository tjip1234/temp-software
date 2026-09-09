"""USB CDC-ACM transport.

The board enumerates as a standard USB CDC device using the ESP32-S3's native
USB-OTG peripheral, so there is no vendor driver to install on any of the three
platforms. Baud rate is meaningless on a virtual UART but we set a high nominal
value anyway, because some USB-serial stacks size their buffers from it.

pyserial is blocking, so reads run on a dedicated thread and hand bytes back to
the event loop with ``call_soon_threadsafe``. Writes go through the loop's default
executor. This is deliberately not pyserial-asyncio: that package's Windows
support has historically been the weak spot, and a reader thread is both portable
and easy to reason about.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import struct
import threading

import serial
from serial.tools import list_ports

from .base import Transport, TransportError, TransportInfo

#: Espressif's vendor id, plus the USB JTAG/serial and CDC product ids the S3 uses.
ESPRESSIF_VID = 0x303A
KNOWN_IDS: tuple[tuple[int, int], ...] = (
    (0x303A, 0x1001),  # ESP32-S2/S3 native USB CDC
    (0x303A, 0x4001),  # ESP32-S3 USB JTAG/serial debug unit
    (0x303A, 0x0002),  # generic Espressif CDC
)

#: USB-serial bridge chips, in case a board is wired through one instead of native USB.
BRIDGE_IDS: tuple[tuple[int, int], ...] = (
    (0x10C4, 0xEA60),  # CP2102
    (0x1A86, 0x7523),  # CH340
    (0x1A86, 0x55D4),  # CH9102
    (0x0403, 0x6001),  # FT232
)

NOMINAL_BAUD = 921600
READ_CHUNK = 4096


def _quiet_modem_lines(ser: serial.Serial) -> None:
    """Put DTR and RTS low without producing an edge sequence, and keep them there.

    Two things are needed and neither is portable, so both are best-effort:

    ``TIOCMSET`` writes the whole modem-line word at once, where pyserial's
    ``ser.dtr = False`` / ``ser.rts = False`` are two ioctls and two edges.

    ``HUPCL`` is what makes the kernel drop the lines again when the port is
    closed. Left on, every close is another pair of edges -- which matters
    because connecting is not one open: a handshake that has to be retried, or
    a scan across several ports, opens and closes repeatedly.
    """
    if os.name != "posix":
        # Windows has no TIOCMSET and no HUPCL; pyserial's own setters are the
        # only option, and the CDC stack there does not pulse on close.
        with contextlib.suppress(OSError, ValueError):
            ser.dtr = False
            ser.rts = False
        return

    import fcntl
    import termios

    fd = ser.fileno()
    with contextlib.suppress(OSError, AttributeError):
        fcntl.ioctl(fd, termios.TIOCMSET, struct.pack("I", 0))
    with contextlib.suppress(OSError, termios.error):
        attrs = termios.tcgetattr(fd)
        attrs[2] &= ~termios.HUPCL
        termios.tcsetattr(fd, termios.TCSANOW, attrs)


class SerialTransport(Transport):
    """TJIP-1 over a serial port (native USB CDC, or a bridge chip)."""

    def __init__(self, port: str, *, baud: int = NOMINAL_BAUD, label: str = "") -> None:
        super().__init__(
            TransportInfo(
                kind="usb",
                address=port,
                label=label or f"USB {port}",
                typical_latency_s=0.0005,
                throughput_bps=1_000_000,
                supports_backfill=True,
                priority=100,  # USB wins over WiFi: lower latency, better time sync
            )
        )
        self.port = port
        self.baud = baud
        self._serial: serial.Serial | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    async def _open_impl(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop.clear()
        try:
            self._serial = await self._loop.run_in_executor(None, self._blocking_open)
        except serial.SerialException as exc:
            raise TransportError(self._explain(exc)) from exc
        self._thread = threading.Thread(
            target=self._read_loop, name=f"tjip-serial-{self.port}", daemon=True
        )
        self._thread.start()

    def _blocking_open(self) -> serial.Serial:
        # An ESP32-S3's USB-Serial-JTAG resets the chip when it sees DTR and RTS
        # move in the sequence esptool uses to enter the bootloader. It watches
        # for a *sequence*, so what matters is not where the lines end up but how
        # many separate edges the host produces getting there. Two ioctls -- one
        # for DTR, one for RTS -- are two USB control transfers, and that is the
        # sequence. The board comes back with rst:0x15 (USB_UART_CHIP_RESET): no
        # panic, no backtrace, just a reboot whenever this software says hello.
        #
        # So every line change here is one TIOCMSET, which carries both bits in
        # a single transfer and cannot be read as an edge pattern.
        ser = serial.Serial()
        ser.port = self.port
        ser.baudrate = self.baud
        ser.timeout = 0.1
        ser.write_timeout = 2.0
        with contextlib.suppress(AttributeError, ValueError):
            ser.exclusive = True  # POSIX only

        ser.open()
        _quiet_modem_lines(ser)
        ser.reset_input_buffer()
        return ser

    def _explain(self, exc: Exception) -> str:
        """Turn pyserial's terse errors into something actionable."""
        text = str(exc)
        low = text.lower()
        if "permission" in low or "access is denied" in low:
            return (
                f"{self.port}: permission denied. On Linux add yourself to the "
                f"'dialout' group (or 'uucp' on Arch) and log out and back in:\n"
                f"    sudo usermod -aG dialout $USER\n"
                f"Or install the udev rule shipped in packaging/ to grant access "
                f"without group membership."
            )
        if "could not open" in low or "no such file" in low:
            return f"{self.port}: no such port. Is the board plugged in and powered?"
        if "busy" in low or "resource temporarily unavailable" in low:
            return (
                f"{self.port} is already open in another program. Close any serial "
                f"monitor (idf.py monitor, screen, Arduino IDE) and try again."
            )
        return f"{self.port}: {text}"

    def _read_loop(self) -> None:
        ser = self._serial
        loop = self._loop
        assert ser is not None and loop is not None

        def to_loop(fn, *args) -> bool:
            """Hand work back to the event loop; False once it has gone away.

            On an abrupt exit the loop can close while this thread is still in
            a read, and call_soon_threadsafe then raises RuntimeError out of a
            daemon thread -- a traceback printed after the window has closed,
            with nothing left to catch it.
            """
            try:
                loop.call_soon_threadsafe(fn, *args)
                return True
            except RuntimeError:
                self._stop.set()
                return False

        try:
            while not self._stop.is_set():
                try:
                    waiting = ser.in_waiting or 1
                    data = ser.read(min(waiting, READ_CHUNK))
                except (serial.SerialException, OSError, TypeError) as exc:
                    if not self._stop.is_set():
                        to_loop(self._closed_from_reader, str(exc))
                    return
                if data and not to_loop(self._ingest, data):
                    return
        finally:
            if not self._stop.is_set():
                to_loop(self._closed_from_reader, "serial reader stopped")

    async def _write_impl(self, data: bytes) -> None:
        ser = self._serial
        if ser is None or not ser.is_open:
            raise TransportError(f"{self.port} is closed")
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, ser.write, data)
        except (serial.SerialException, serial.SerialTimeoutException, OSError) as exc:
            raise TransportError(self._explain(exc)) from exc

    async def _close_impl(self) -> None:
        self._stop.set()
        ser, self._serial = self._serial, None
        if ser is not None:
            try:
                ser.cancel_read()
            except (AttributeError, OSError):
                pass
            try:
                ser.close()
            except (serial.SerialException, OSError):
                pass
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            await asyncio.get_running_loop().run_in_executor(None, thread.join, 2.0)


# ------------------------------------------------------------------- discovery

def list_serial_candidates(*, all_ports: bool = False) -> list[dict]:
    """Enumerate serial ports that might be a TjipTemp board.

    Ranked: native Espressif USB first, then known bridge chips, then everything
    else. We never claim certainty from the VID/PID alone -- the only proof that a
    port is a board is a DEVICE_INFO frame coming back out of it.
    """
    out: list[dict] = []
    for port in list_ports.comports():
        vid_pid = (port.vid, port.pid)
        if vid_pid in KNOWN_IDS:
            rank, why = 0, "Espressif native USB"
        elif port.vid == ESPRESSIF_VID:
            rank, why = 1, "Espressif device"
        elif vid_pid in BRIDGE_IDS:
            rank, why = 2, "USB-serial bridge"
        elif all_ports:
            rank, why = 3, "serial port"
        else:
            continue
        out.append({
            "port": port.device,
            "rank": rank,
            "reason": why,
            "description": port.description or "",
            "manufacturer": port.manufacturer or "",
            "serial_number": port.serial_number or "",
            "vid": port.vid,
            "pid": port.pid,
            "hwid": port.hwid or "",
        })
    out.sort(key=lambda d: (d["rank"], d["port"]))
    return out


def make_label(candidate: dict) -> str:
    bits = [candidate["port"]]
    if candidate.get("description"):
        bits.append(candidate["description"])
    return " — ".join(bits)
