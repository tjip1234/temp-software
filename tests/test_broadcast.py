"""The network thermometer, checked against VNA Studio's own client.

The point of these is that they do not reimplement the protocol. Where VNA
Studio's client is importable they drive the real ``discover``, ``HttpPoller``
and endpoint parsing from ``vnastudio.temperature``, so a change here that
breaks the contract fails immediately rather than at the bench.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import socket
import sys

import pytest

from tjiptemp.api.broadcast import (
    AUTO_CHANNEL,
    BEACON_PORT,
    MDNS_SERVICE,
    Stale,
    broadcast_addresses,
    to_unit,
)
from tjiptemp.core.application import Application, Settings
from tjiptemp.protocol.channels import Ch
from tjiptemp.simulator.board import SimulatedBoard
from tjiptemp.simulator.server import LoopbackTransport, SimulatorRuntime

SERIAL = "TJIP-BROADCAST01"

#: VNA Studio, if it is checked out beside us. Its client is the reference.
VNA_STUDIO = pathlib.Path(
    "/home/raaf/Thesis-DielectricSpectroscopy/Dielectric-spectroscopy-software/vna-studio"
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
async def served(tmp_path):
    """A running broadcast with a simulated board behind it."""
    settings = Settings(
        api_enabled=False,
        auto_connect_usb=False,
        db_path=str(tmp_path / "b.tjip"),
        broadcast_enabled=True,
        broadcast_host="127.0.0.1",
        broadcast_port=_free_port(),
        broadcast_name="TjipTemp under test",
        broadcast_interval_s=0.2,
        broadcast_beacon_interval_s=0.5,
        broadcast_mdns=False,        # registering real mDNS in a test is rude
        broadcast_average_s=0.0,
    )
    app = Application(settings, db_path=tmp_path / "b.tjip")
    runtime = SimulatorRuntime(SimulatedBoard(serial=SERIAL, rate_hz=50.0, seed=5))
    await runtime.start()
    device = await app.devices.connect(LoopbackTransport(runtime))
    await device.start_streaming(20.0)
    await asyncio.sleep(0.6)
    await app.start_broadcast()
    yield app
    await app.close()
    await runtime.stop()


async def _get(port: int, path: str) -> tuple[int, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: localhost\r\n"
                 f"Connection: close\r\n\r\n".encode())
    await writer.drain()
    raw = await reader.read()
    writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    return int(head.split()[1]), body


# ------------------------------------------------------------------- the wire

async def test_http_endpoint_matches_the_contract(served):
    port = served.settings.broadcast_port
    code, body = await _get(port, "/temperature")
    assert code == 200
    payload = json.loads(body)
    # The two keys the client actually reads (docs/thermometer_integration.md).
    assert isinstance(payload["temperature"], float)
    assert payload["unit"] == "°C"
    assert 0.0 < payload["temperature"] < 100.0


async def test_websocket_pushes_readings(served):
    websockets = pytest.importorskip("websockets")
    port = served.settings.broadcast_port
    got = []
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as socket_:
        for _ in range(3):
            got.append(json.loads(await asyncio.wait_for(socket_.recv(), timeout=5)))
    assert len(got) == 3
    assert all("temperature" in m and "unit" in m for m in got)


async def test_status_and_index_are_readable(served):
    port = served.settings.broadcast_port
    code, body = await _get(port, "/status")
    assert code == 200
    assert json.loads(body)["running"] is True

    code, body = await _get(port, "/")
    assert code == 200
    text = body.decode()
    assert MDNS_SERVICE in text and str(BEACON_PORT) in text


async def test_no_board_is_a_503_not_a_made_up_number(served):
    port = served.settings.broadcast_port
    for serial in list(served.devices.devices):
        await served.devices.disconnect(serial)
    code, _ = await _get(port, "/temperature")
    assert code == 503


async def test_stale_policy_last_keeps_serving(served):
    port = served.settings.broadcast_port
    await _get(port, "/temperature")            # prime the last-good value
    served.settings.broadcast_stale_policy = "last"
    for serial in list(served.devices.devices):
        await served.devices.disconnect(serial)
    code, body = await _get(port, "/temperature")
    assert code == 200
    assert "temperature" in json.loads(body)


# ------------------------------------------------------------------- options

async def test_unit_conversion(served):
    port = served.settings.broadcast_port
    _, body = await _get(port, "/temperature")
    celsius = json.loads(body)["temperature"]

    served.settings.broadcast_unit = "degF"
    _, body = await _get(port, "/temperature")
    fahrenheit = json.loads(body)
    assert fahrenheit["unit"] == "°F"
    assert fahrenheit["temperature"] == pytest.approx(celsius * 9 / 5 + 32, abs=0.5)

    served.settings.broadcast_unit = "K"
    _, body = await _get(port, "/temperature")
    kelvin = json.loads(body)
    assert kelvin["unit"] == "K"
    assert kelvin["temperature"] == pytest.approx(celsius + 273.15, abs=0.5)


def test_unit_helper():
    assert to_unit(0.0, "degC") == 0.0
    assert to_unit(100.0, "degF") == pytest.approx(212.0)
    assert to_unit(0.0, "K") == pytest.approx(273.15)


async def test_explicit_channel_selection(served):
    port = served.settings.broadcast_port
    served.settings.broadcast_channel = int(Ch.TYPEK)
    _, body = await _get(port, "/temperature")
    assert json.loads(body)["channel_id"] == int(Ch.TYPEK)

    served.settings.broadcast_channel = AUTO_CHANNEL
    _, body = await _get(port, "/temperature")
    assert json.loads(body)["channel_id"] == int(Ch.PT1000)


async def test_a_channel_the_board_does_not_have_is_stale_not_wrong(served):
    served.settings.broadcast_channel = 200
    with pytest.raises(Stale):
        served.broadcast.reading()


async def test_averaging_smooths_the_published_value(served):
    served.settings.broadcast_average_s = 5.0
    port = served.settings.broadcast_port
    _, body = await _get(port, "/temperature")
    averaged = json.loads(body)["temperature"]
    device = next(iter(served.devices.devices.values()))
    instant = device.latest_values()[int(Ch.PT1000)]
    # Same physical quantity, but not the same number as one raw sample.
    assert abs(averaged - instant) < 5.0


async def test_selecting_a_board_by_serial(served):
    served.settings.broadcast_serial = SERIAL
    assert served.broadcast.reading().serial == SERIAL
    served.settings.broadcast_serial = "TJIP-NOT-A-BOARD"
    with pytest.raises(Stale):
        served.broadcast.reading()


def test_beacon_targets_include_a_locally_deliverable_address():
    """255.255.255.255 alone never reaches a client on this machine."""
    targets = broadcast_addresses()
    assert "255.255.255.255" in targets
    assert len(targets) > 1, "only the limited broadcast address; local clients would miss it"


async def test_disabled_broadcast_starts_nothing(tmp_path):
    settings = Settings(api_enabled=False, auto_connect_usb=False,
                        db_path=str(tmp_path / "off.tjip"))
    app = Application(settings, db_path=tmp_path / "off.tjip")
    assert await app.start_broadcast() is None
    assert not app.broadcast.running
    await app.close()


# ------------------------------------------- against VNA Studio's own client

@pytest.fixture
def vnastudio():
    if not VNA_STUDIO.is_dir():
        pytest.skip("vna-studio is not checked out beside this repo")
    if str(VNA_STUDIO) not in sys.path:
        sys.path.insert(0, str(VNA_STUDIO))
    return pytest.importorskip("vnastudio.temperature")


async def test_vna_studio_polls_us(served, vnastudio):
    """VNA Studio's HttpPoller, unmodified, against our endpoint."""
    endpoint = vnastudio.ThermometerEndpoint(
        name="under test", host="127.0.0.1",
        port=served.settings.broadcast_port, mode="http",
    )
    got: list = []
    poller = vnastudio.HttpPoller(endpoint, interval=0.3, on_reading=got.append)
    poller.start()
    try:
        for _ in range(40):
            await asyncio.sleep(0.1)
            if len(got) >= 2:
                break
    finally:
        poller.stop()
    assert len(got) >= 2, "VNA Studio's poller got nothing"
    assert got[-1].unit == "°C"
    assert 0.0 < got[-1].value < 100.0


async def test_vna_studio_discovers_our_beacon(served, vnastudio):
    """VNA Studio's UDP discovery, unmodified, against our beacon."""
    loop = asyncio.get_running_loop()
    found = await loop.run_in_executor(None, vnastudio.discover_udp, 4.0)
    ours = [e for e in found if e.port == served.settings.broadcast_port]
    assert ours, "VNA Studio's discovery did not see our beacon"
    endpoint = ours[0]
    assert endpoint.path == "/temperature"
    assert endpoint.ws_path == "/ws"
    assert endpoint.name == "TjipTemp under test"
