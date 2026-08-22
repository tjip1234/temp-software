"""The REST and WebSocket API.

These run against a real uvicorn server sharing the event loop with the device
layer, which is exactly how it works in the app. Starlette's TestClient cannot be
used here: it drives the ASGI app on its own loop, so any endpoint that awaits the
device layer would deadlock against a board living on a different loop — which is
a property of the test harness, not of the server.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket

import httpx
import pytest

from tjiptemp.api.server import build_server
from tjiptemp.core.application import Application, Settings
from tjiptemp.simulator.board import SimulatedBoard
from tjiptemp.simulator.server import LoopbackTransport, SimulatorRuntime

PREFIX = "/api/v1"
SERIAL = "TJIP-API00000001"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
async def app(tmp_path):
    settings = Settings(
        api_enabled=True, api_host="127.0.0.1", api_port=_free_port(),
        db_path=str(tmp_path / "api.tjip"),
    )
    application = Application(settings, db_path=tmp_path / "api.tjip")

    runtime = SimulatorRuntime(SimulatedBoard(serial=SERIAL, rate_hz=50.0, seed=5))
    await runtime.start()
    device = await application.devices.connect(LoopbackTransport(runtime))
    await device.start_streaming(20.0)

    await application.start_api()
    await asyncio.sleep(0.6)  # let a little data accumulate

    yield application

    await application.close()
    await runtime.stop()


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(base_url=app.api_url, timeout=20.0) as http:
        yield http


# ------------------------------------------------------------------- devices

async def test_server_starts_on_the_configured_port(app):
    assert app.api_running
    assert app.api_url.startswith("http://127.0.0.1:")


async def test_list_devices(client):
    response = await client.get(f"{PREFIX}/devices")
    assert response.status_code == 200
    devices = response.json()
    assert len(devices) == 1
    assert devices[0]["serial"] == SERIAL
    assert devices[0]["state"] in ("online", "degraded")
    assert len(devices[0]["channels"]) == 15


async def test_unknown_device_lists_what_is_available(client):
    response = await client.get(f"{PREFIX}/devices/NOPE")
    assert response.status_code == 404
    assert SERIAL in response.json()["detail"]


async def test_readings_include_units_and_age(client):
    readings = (await client.get(f"{PREFIX}/devices/{SERIAL}/readings")).json()
    by_key = {r["key"]: r for r in readings}
    assert by_key["pt1000"]["unit"] == "degC"
    assert 10.0 < by_key["pt1000"]["value"] < 60.0
    assert by_key["pt1000"]["age_s"] is not None
    assert by_key["v_bat"]["unit"] == "V"


async def test_history_returns_a_time_series(client):
    body = (await client.get(f"{PREFIX}/devices/{SERIAL}/history",
                             params={"seconds": 10, "channels": "0,1"})).json()
    assert len(body["t_utc"]) > 5
    assert set(body["channels"]) == {"0", "1"}
    assert len(body["channels"]["0"]) == len(body["t_utc"])


async def test_status_exposes_timebase_and_stream_health(client):
    body = (await client.get(f"{PREFIX}/devices/{SERIAL}/status")).json()
    assert body["timebase"]["n_points"] >= 4
    assert body["stream"]["completeness"] == 1.0


# ------------------------------------------------------------------- control

async def test_set_wire_mode(client):
    response = await client.post(f"{PREFIX}/devices/{SERIAL}/rtd/wires", json={"wires": 3})
    assert response.status_code == 200
    body = response.json()
    assert body["rtd"]["wires"] == 3
    assert "3-wire" in body["note"]


async def test_invalid_wire_mode_is_rejected(client):
    response = await client.post(f"{PREFIX}/devices/{SERIAL}/rtd/wires", json={"wires": 7})
    assert response.status_code == 400
    assert "2, 3 or 4" in response.json()["detail"]


async def test_display_control(client):
    response = await client.post(f"{PREFIX}/devices/{SERIAL}/display",
                                 json={"page": "graph", "source": 1, "backlight": 40})
    assert response.status_code == 200
    assert response.json()["applied"]["backlight"] == 40


async def test_empty_display_patch_is_rejected(client):
    response = await client.post(f"{PREFIX}/devices/{SERIAL}/display", json={})
    assert response.status_code == 400


async def test_calibration_roundtrip(client):
    cal = (await client.get(f"{PREFIX}/devices/{SERIAL}/calibration")).json()
    cal["channels"]["pt1000"]["r0"] = 1000.42
    response = await client.put(f"{PREFIX}/devices/{SERIAL}/calibration", json=cal)
    assert response.status_code == 200, response.text
    assert response.json()["channels"]["pt1000"]["r0"] == pytest.approx(1000.42)


async def test_implausible_calibration_is_refused_with_a_reason(client):
    cal = (await client.get(f"{PREFIX}/devices/{SERIAL}/calibration")).json()
    cal["channels"]["pt1000"]["r0"] = 2.0
    response = await client.put(f"{PREFIX}/devices/{SERIAL}/calibration", json=cal)
    assert response.status_code == 422
    assert "plausible range" in response.json()["detail"]


async def test_self_test(client):
    body = (await client.post(f"{PREFIX}/devices/{SERIAL}/self-test")).json()
    assert "checks" in body


async def test_read_only_mode_blocks_writes_but_not_reads(app, client):
    app.settings.api_allow_control = False
    try:
        assert (await client.get(f"{PREFIX}/devices/{SERIAL}/readings")).status_code == 200
        response = await client.post(f"{PREFIX}/devices/{SERIAL}/rtd/wires", json={"wires": 4})
        assert response.status_code == 403
        assert "read-only" in response.json()["detail"]
    finally:
        app.settings.api_allow_control = True


async def test_token_is_enforced_when_set(app, client):
    app.settings.api_token = "s3cret"
    try:
        assert (await client.get(f"{PREFIX}/devices")).status_code == 401
        ok = await client.get(f"{PREFIX}/devices",
                              headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200
    finally:
        app.settings.api_token = ""


# ----------------------------------------------------------------- recording

async def test_record_start_stop_and_export(client):
    started = await client.post(f"{PREFIX}/devices/{SERIAL}/record", json={"name": "api test"})
    assert started.status_code == 200
    session_id = started.json()["session_id"]

    again = await client.post(f"{PREFIX}/devices/{SERIAL}/record", json={"name": "again"})
    assert again.status_code == 409

    marker = await client.post(f"{PREFIX}/devices/{SERIAL}/record/mark", json={"label": "here"})
    assert marker.status_code == 200

    await asyncio.sleep(1.2)

    stopped = await client.post(f"{PREFIX}/devices/{SERIAL}/record/stop")
    assert stopped.status_code == 200
    assert stopped.json()["rows"] > 0

    detail = (await client.get(f"{PREFIX}/sessions/{session_id}")).json()
    assert detail["name"] == "api test"
    assert len(detail["markers"]) == 1

    csv = await client.get(f"{PREFIX}/sessions/{session_id}/data", params={"format": "csv"})
    assert csv.status_code == 200
    assert "PT1000 [degC]" in csv.text
    assert len(csv.text.splitlines()) > 10

    summary = (await client.get(f"{PREFIX}/sessions/{session_id}/summary")).json()
    assert any(row["Channel"] == "PT1000" for row in summary)


async def test_stopping_a_recording_that_is_not_running(client):
    response = await client.post(f"{PREFIX}/devices/{SERIAL}/record/stop")
    assert response.status_code == 409


# --------------------------------------------------------------------- other

async def test_metrics_are_prometheus_shaped(client):
    text = (await client.get(f"{PREFIX}/metrics")).text
    assert "# TYPE tjiptemp_reading gauge" in text
    assert f'serial="{SERIAL}"' in text
    assert 'channel="pt1000"' in text


async def test_openapi_document_is_generated(client):
    schema = (await client.get("/openapi.json")).json()
    assert schema["info"]["title"] == "TjipTemp API"
    assert f"{PREFIX}/devices" in schema["paths"]


async def test_websocket_streams_readings(app):
    import websockets

    url = app.api_url.replace("http://", "ws://") + f"{PREFIX}/stream?rate_hz=10"
    async with websockets.connect(url) as socket:
        import json

        hello = json.loads(await asyncio.wait_for(socket.recv(), 5.0))
        assert hello["type"] == "hello"
        assert hello["devices"][0]["serial"] == SERIAL

        message = json.loads(await asyncio.wait_for(socket.recv(), 5.0))
        assert message["type"] == "samples"
        values = message["devices"][0]["values"]
        assert 10.0 < values["pt1000"] < 60.0
        assert values["v_bat"] is not None


async def test_websocket_rejects_a_bad_token(app):
    import websockets

    app.settings.api_token = "s3cret"
    try:
        url = app.api_url.replace("http://", "ws://") + f"{PREFIX}/stream?token=wrong"
        # The server closes with code 4401 before the handshake completes, which
        # bleak-style clients surface as a connection error rather than a clean close.
        with pytest.raises((OSError, websockets.exceptions.WebSocketException)):
            async with websockets.connect(url) as socket:
                await asyncio.wait_for(socket.recv(), 5.0)
    finally:
        app.settings.api_token = ""


async def test_binding_off_loopback_without_a_token_is_refused(tmp_path):
    settings = Settings(api_host="0.0.0.0", api_token="", db_path=str(tmp_path / "x.tjip"))
    application = Application(settings, db_path=tmp_path / "x.tjip")
    try:
        with pytest.raises(RuntimeError, match="without a token"):
            build_server(application)
    finally:
        with contextlib.suppress(Exception):
            application.db.close()
