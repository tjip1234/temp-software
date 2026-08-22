"""Finding boards: serial ports, mDNS on the LAN, and BLE advertisements.

Discovery produces *candidates*, never confirmed devices. A candidate becomes a
device only once it answers HELLO with a DEVICE_INFO carrying a serial number.
That distinction matters because a USB-serial bridge chip looks identical to a
board until you talk to it, and guessing wrong means opening someone's 3D printer.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field

from ..protocol.messages import DEFAULT_TCP_PORT, MDNS_SERVICE
from . import ble as ble_mod
from .serial_cdc import list_serial_candidates, make_label

try:
    from zeroconf import ServiceStateChange
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

    MDNS_AVAILABLE = True
except ImportError:  # pragma: no cover
    AsyncZeroconf = None  # type: ignore[assignment]
    MDNS_AVAILABLE = False


@dataclass(slots=True)
class Candidate:
    """Something that might be a board."""

    kind: str          # "usb" | "wifi" | "ble"
    address: str       # port path, "host:port", BLE address
    label: str
    #: How likely this is a real board: 0 = certain, higher = less so.
    rank: int = 0
    serial: str | None = None
    detail: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.address}"


# ------------------------------------------------------------------------ USB

def discover_serial(*, all_ports: bool = False) -> list[Candidate]:
    return [
        Candidate(
            kind="usb",
            address=c["port"],
            label=make_label(c),
            rank=c["rank"],
            serial=c.get("serial_number") or None,
            detail=c,
        )
        for c in list_serial_candidates(all_ports=all_ports)
    ]


# ----------------------------------------------------------------------- mDNS

async def discover_mdns(timeout: float = 3.0) -> list[Candidate]:
    """Browse for ``_tjiptemp._tcp.local.`` advertisements.

    The board publishes its serial in a TXT record, which lets the host match a
    WiFi endpoint to a board it already knows over USB without connecting first.
    """
    if not MDNS_AVAILABLE:
        return []

    found: dict[str, Candidate] = {}
    loop = asyncio.get_running_loop()
    pending: list[asyncio.Task] = []

    async def resolve(zc: AsyncZeroconf, service_type: str, name: str) -> None:
        info = AsyncServiceInfo(service_type, name)
        if not await info.async_request(zc.zeroconf, 2500):
            return
        addresses = info.parsed_scoped_addresses() or []
        if not addresses:
            return
        txt = {
            k.decode("utf-8", "replace"): (v or b"").decode("utf-8", "replace")
            for k, v in (info.properties or {}).items()
        }
        host = addresses[0]
        port = info.port or DEFAULT_TCP_PORT
        serial = txt.get("serial") or txt.get("sn")
        pretty = name.removesuffix("." + service_type).removesuffix(".")
        cand = Candidate(
            kind="wifi",
            address=f"{host}:{port}",
            label=f"{pretty} — {host}:{port}",
            rank=0,
            serial=serial,
            detail={"txt": txt, "hostname": info.server, "addresses": addresses},
        )
        found[cand.key] = cand

    def on_change(zeroconf, service_type, name, state_change, **_kwargs):
        if state_change is ServiceStateChange.Added:
            pending.append(loop.create_task(resolve(azc, service_type, name)))

    azc = AsyncZeroconf()
    try:
        browser = AsyncServiceBrowser(azc.zeroconf, MDNS_SERVICE, handlers=[on_change])
        await asyncio.sleep(timeout)
        await browser.async_cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    except Exception:
        return list(found.values())
    finally:
        with contextlib.suppress(Exception):
            await azc.async_close()
    return list(found.values())


# ------------------------------------------------------------------------ BLE

async def discover_ble(timeout: float = 5.0) -> list[Candidate]:
    return [
        Candidate(
            kind="ble",
            address=entry["address"],
            label=f"{entry['name'] or 'TjipTemp'} — {entry['address']} ({entry['rssi']} dBm)",
            rank=0 if entry["matched"] == "service-uuid" else 1,
            detail=entry,
        )
        for entry in await ble_mod.scan(timeout)
    ]


# ------------------------------------------------------------------------- all

async def discover_all(
    *,
    usb: bool = True,
    wifi: bool = True,
    bluetooth: bool = False,
    all_ports: bool = False,
    mdns_timeout: float = 3.0,
    ble_timeout: float = 5.0,
) -> list[Candidate]:
    """Run every enabled discovery method concurrently and merge the results.

    BLE is off by default: scanning takes seconds and on some platforms it
    interferes with an already-connected BLE peripheral, so it should be an
    explicit user action rather than something that happens on every refresh.
    """
    results: list[Candidate] = []
    tasks: list[asyncio.Task] = []

    if usb:
        results.extend(discover_serial(all_ports=all_ports))
    if wifi and MDNS_AVAILABLE:
        tasks.append(asyncio.create_task(discover_mdns(mdns_timeout)))
    if bluetooth and ble_mod.BLE_AVAILABLE:
        tasks.append(asyncio.create_task(discover_ble(ble_timeout)))

    for outcome in await asyncio.gather(*tasks, return_exceptions=True):
        if isinstance(outcome, list):
            results.extend(outcome)

    # Deduplicate by key, preferring the better-ranked sighting.
    best: dict[str, Candidate] = {}
    for cand in results:
        existing = best.get(cand.key)
        if existing is None or cand.rank < existing.rank:
            best[cand.key] = cand

    order = {"usb": 0, "wifi": 1, "ble": 2}
    return sorted(best.values(), key=lambda c: (c.rank, order.get(c.kind, 9), c.address))


def parse_address(text: str) -> Candidate | None:
    """Interpret whatever the user typed into the 'connect to' box.

    Accepts ``/dev/ttyACM0``, ``COM7``, ``192.168.1.44``, ``192.168.1.44:3737``,
    ``tjiptemp-5b44.local``, and a bare BLE MAC.
    """
    text = text.strip()
    if not text:
        return None
    if text.startswith("/dev/") or text.upper().startswith("COM"):
        return Candidate("usb", text, f"USB {text}")
    if text.count(":") == 5 and all(len(p) == 2 for p in text.split(":")):
        return Candidate("ble", text.upper(), f"BLE {text.upper()}")
    host, _, port_text = text.rpartition(":")
    if host and port_text.isdigit():
        return Candidate("wifi", f"{host}:{port_text}", f"WiFi {host}:{port_text}")
    return Candidate("wifi", f"{text}:{DEFAULT_TCP_PORT}", f"WiFi {text}:{DEFAULT_TCP_PORT}")


def build_transport(candidate: Candidate):
    """Instantiate the right transport for a candidate."""
    from .serial_cdc import SerialTransport
    from .tcp import TcpTransport

    if candidate.kind == "usb":
        return SerialTransport(candidate.address, label=candidate.label)
    if candidate.kind == "wifi":
        host, _, port = candidate.address.rpartition(":")
        return TcpTransport(
            host or candidate.address,
            int(port) if port.isdigit() else DEFAULT_TCP_PORT,
            label=candidate.label,
            serial=candidate.serial,
        )
    if candidate.kind == "ble":
        return ble_mod.BleTransport(candidate.address, label=candidate.label, serial=candidate.serial)
    raise ValueError(f"unknown transport kind {candidate.kind!r}")
