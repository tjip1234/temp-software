"""BLE GATT transport — control, configuration and low-rate telemetry.

The ESP32-S3 has BLE only (no Bluetooth Classic, so no SPP virtual serial port),
which sets the ceiling here: a well-behaved BLE link with a 512-byte MTU and a
7.5 ms connection interval tops out around 40-60 kB/s in ideal conditions, and
more like 5-20 kB/s in practice on a laptop with a busy radio. That is fine for
provisioning WiFi, pushing a calibration, changing what the screen shows, and
watching a handful of channels at 1-2 Hz. It is not a sensible way to move an
hour of 100 Hz history, so backfill over BLE is declined rather than attempted
slowly and badly.

``bleak`` is an optional dependency; importing this module without it succeeds and
:data:`BLE_AVAILABLE` is False, so the rest of the app degrades cleanly.
"""

from __future__ import annotations

import asyncio

from .base import Transport, TransportError, TransportInfo

try:  # pragma: no cover - depends on the install extra
    from bleak import BleakClient, BleakScanner

    BLE_AVAILABLE = True
    BLE_IMPORT_ERROR = ""
except ImportError as exc:  # pragma: no cover
    BleakClient = BleakScanner = None  # type: ignore[assignment]
    BLE_AVAILABLE = False
    BLE_IMPORT_ERROR = str(exc)


# Placeholder UUIDs. Assign a real 128-bit base before production hardware ships;
# the firmware and this constant must agree, and nothing else depends on them.
SERVICE_UUID = "6f0d0001-b5a3-f393-e0a9-e50e24dcca9e"
CHAR_RX_UUID = "6f0d0002-b5a3-f393-e0a9-e50e24dcca9e"  # host -> device (write)
CHAR_TX_UUID = "6f0d0003-b5a3-f393-e0a9-e50e24dcca9e"  # device -> host (notify)
CHAR_INFO_UUID = "6f0d0004-b5a3-f393-e0a9-e50e24dcca9e"  # static identity (read)

#: Conservative default; renegotiated upward on connect where the OS allows it.
FALLBACK_MTU = 20
SCAN_SECONDS = 5.0


class BleTransport(Transport):
    """TJIP-1 over BLE GATT, with frames split across writes at MTU boundaries."""

    def __init__(self, address: str, *, label: str = "", serial: str | None = None) -> None:
        super().__init__(
            TransportInfo(
                kind="ble",
                address=address,
                label=label or f"BLE {address}",
                serial=serial,
                typical_latency_s=0.05,
                throughput_bps=10_000,
                supports_backfill=False,  # see module docstring
                priority=10,
            )
        )
        self.address = address
        self._client: BleakClient | None = None
        self._chunk = FALLBACK_MTU

    async def _open_impl(self) -> None:
        if not BLE_AVAILABLE:
            raise TransportError(
                "BLE support needs the 'bleak' package. Install the extra:\n"
                "    pip install 'tjiptemp[ble]'\n"
                f"(import failed: {BLE_IMPORT_ERROR})"
            )
        client = BleakClient(self.address, disconnected_callback=self._on_disconnect)
        try:
            await client.connect()
        except Exception as exc:  # bleak raises a wide variety of backend errors
            raise TransportError(f"BLE connect to {self.address} failed: {exc}") from exc
        if not client.is_connected:
            raise TransportError(f"BLE connect to {self.address} did not establish")

        # bleak exposes the negotiated ATT MTU on most backends; 3 bytes go to the
        # ATT write header, so the usable payload is mtu - 3.
        mtu = getattr(client, "mtu_size", None)
        self._chunk = max(FALLBACK_MTU, int(mtu) - 3) if mtu else FALLBACK_MTU

        try:
            await client.start_notify(CHAR_TX_UUID, self._on_notify)
        except Exception as exc:
            await client.disconnect()
            raise TransportError(
                f"{self.address} connected but has no TjipTemp TX characteristic "
                f"({CHAR_TX_UUID}). Is this actually a TjipTemp board? ({exc})"
            ) from exc
        self._client = client
        self.info.extra["mtu"] = self._chunk + 3

    def _on_notify(self, _sender, data: bytearray) -> None:
        self._ingest(bytes(data))

    def _on_disconnect(self, _client) -> None:
        self._closed_from_reader("BLE peer disconnected")

    async def _write_impl(self, data: bytes) -> None:
        client = self._client
        if client is None or not client.is_connected:
            raise TransportError(f"BLE {self.address} is not connected")
        try:
            for start in range(0, len(data), self._chunk):
                await client.write_gatt_char(
                    CHAR_RX_UUID, data[start : start + self._chunk], response=False
                )
        except Exception as exc:
            raise TransportError(f"BLE write to {self.address} failed: {exc}") from exc

    async def _close_impl(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            if client.is_connected:
                await client.stop_notify(CHAR_TX_UUID)
        except Exception:
            pass
        try:
            await asyncio.wait_for(client.disconnect(), timeout=5.0)
        except Exception:
            pass


async def scan(timeout: float = SCAN_SECONDS) -> list[dict]:
    """Scan for advertising TjipTemp boards.

    Matches on the service UUID; falls back to a name prefix for a board whose
    advertisement is too full to carry the 128-bit UUID.
    """
    if not BLE_AVAILABLE:
        return []
    found: list[dict] = []
    try:
        devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
    except Exception:
        return []

    for address, (device, adv) in devices.items():
        uuids = {u.lower() for u in (adv.service_uuids or [])}
        name = adv.local_name or device.name or ""
        matches_uuid = SERVICE_UUID.lower() in uuids
        matches_name = name.lower().startswith("tjiptemp")
        if not (matches_uuid or matches_name):
            continue
        # Manufacturer data carries the last three MAC bytes so the same board can be
        # recognised as the one already connected over USB or WiFi.
        mac_tail = None
        for payload in (adv.manufacturer_data or {}).values():
            if len(payload) >= 3:
                mac_tail = payload[-3:].hex()
                break
        found.append({
            "address": address,
            "name": name,
            "rssi": adv.rssi,
            "mac_tail": mac_tail,
            "matched": "service-uuid" if matches_uuid else "name",
        })
    found.sort(key=lambda d: -(d["rssi"] or -127))
    return found


def availability_note() -> str:
    """One line for the UI explaining why BLE is or is not offered."""
    if BLE_AVAILABLE:
        return "BLE available. Suitable for setup and slow telemetry; use USB or WiFi for logging."
    return "BLE unavailable — install the 'ble' extra (pip install 'tjiptemp[ble]') to enable it."
