"""REST and WebSocket API, so other programs can use the boards.

Runs inside the desktop app on the same event loop as the device layer, which
means an external script sees exactly the values the UI is showing -- there is no
second acquisition path to drift out of step.

Design decisions worth knowing:

* **Loopback by default.** The server binds 127.0.0.1 unless told otherwise. A
  thermometer that anyone on the coffee-shop WiFi can recalibrate is a bad
  thermometer.
* **Token optional, but required off loopback.** Binding to a routable address
  without a token is refused rather than warned about.
* **Reads and writes are separated.** ``api_allow_control`` gates everything that
  changes the board, so a dashboard can be given a read-only endpoint.
* **OpenAPI is generated**, so any language's client generator works, and
  ``/docs`` is a usable console for someone exploring the API by hand.

The WebSocket carries the live sample stream as JSON. That is the wrong format for
the *device* link and the right one here: consumers are scripts and browsers, the
data is already decimated to human rates, and self-describing beats compact.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import secrets
import time
from typing import TYPE_CHECKING

import numpy as np
from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from .. import __version__
from ..device.device import Device
from ..protocol.channels import ChannelSpec

if TYPE_CHECKING:
    from ..core.application import Application

log = logging.getLogger(__name__)

API_PREFIX = "/api/v1"


# ------------------------------------------------------------------- schemas

class ChannelOut(BaseModel):
    id: int
    key: str
    name: str
    unit: str
    kind: str
    color: str


class ReadingOut(BaseModel):
    channel: int
    key: str
    name: str
    unit: str
    value: float | None = Field(None, description="null when the sensor is faulted")
    age_s: float | None = None


class DeviceOut(BaseModel):
    serial: str
    name: str
    state: str
    error: str | None = None
    model: str = ""
    fw_ver: str = ""
    links: list[dict] = []
    channels: list[ChannelOut] = []
    calibrated: bool = False
    cal_rev: int = 0
    recording: bool = False


class ConfigPatch(BaseModel):
    patch: dict = Field(..., description="Partial CONFIG object, merged into the device's")
    volatile: bool = False


class DisplayPatch(BaseModel):
    page: str | None = Field(None, description="overview | single | graph | status | blank")
    source: int | None = None
    sources: list[int] | None = None
    window_s: float | None = None
    backlight: int | None = Field(None, ge=0, le=100)
    rotation: int | None = None
    units: str | None = None


class WireMode(BaseModel):
    wires: int = Field(..., description="2, 3 or 4")


class RecordStart(BaseModel):
    name: str = ""
    notes: str = ""


class MarkerIn(BaseModel):
    label: str
    notes: str = ""


class ConnectIn(BaseModel):
    address: str = Field(..., description="/dev/ttyACM0, COM7, 192.168.1.44, or a BLE MAC")


def _finite(value: float) -> float | None:
    """JSON has no NaN. A faulted reading is null, not zero and not omitted."""
    return None if value is None or not math.isfinite(value) else round(float(value), 6)


def _channel_out(spec: ChannelSpec) -> ChannelOut:
    return ChannelOut(id=spec.id, key=spec.key, name=spec.name, unit=spec.unit,
                      kind=spec.kind, color=spec.color)


def _device_out(app: Application, device: Device) -> DeviceOut:
    health = device.health()
    return DeviceOut(
        serial=device.serial,
        name=device.name,
        state=device.state.value,
        error=device.error,
        model=device.info.get("model", ""),
        fw_ver=device.info.get("fw_ver", ""),
        links=health["links"],
        channels=[_channel_out(s) for s in device.channels.values()],
        calibrated=not health["uncalibrated"],
        cal_rev=device.calibration.rev,
        recording=app.recorder.is_recording(device.serial),
    )


# --------------------------------------------------------------------- server

def build_app(app: Application) -> FastAPI:
    """Construct the FastAPI application bound to a running :class:`Application`."""

    api = FastAPI(
        title="TjipTemp API",
        version=__version__,
        description=(
            "Live and recorded measurements from TjipTemp thermometer boards.\n\n"
            "Everything here reads from the same device layer the desktop UI uses, "
            "so values match what is on screen and on the board's own display."
        ),
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    api.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["*"],
    )

    def require_token(authorization: str | None = None) -> None:
        token = app.settings.api_token
        if not token:
            return
        if not authorization or not secrets.compare_digest(
            authorization.removeprefix("Bearer ").strip(), token
        ):
            raise HTTPException(401, "invalid or missing API token")

    async def auth(authorization: str | None = Header(None)) -> None:
        # FastAPI maps the parameter name onto the Authorization header.
        require_token(authorization)

    def require_control() -> None:
        if not app.settings.api_allow_control:
            raise HTTPException(
                403,
                "This API is running in read-only mode. Enable 'Allow control via API' "
                "in settings to permit changes to the board.",
            )

    def get_device(serial: str) -> Device:
        device = app.devices.get(serial)
        if device is None:
            known = ", ".join(app.devices.devices) or "none connected"
            raise HTTPException(404, f"no device {serial!r}. Known devices: {known}")
        return device

    def online_device(serial: str) -> Device:
        device = get_device(serial)
        if not device.is_online:
            raise HTTPException(409, f"{device.label} is {device.state.value}")
        return device

    guard = [Depends(auth)]

    # ------------------------------------------------------------------ meta

    @api.get(f"{API_PREFIX}/health", tags=["meta"])
    async def health() -> dict:
        return app.health()

    @api.get(f"{API_PREFIX}/version", tags=["meta"])
    async def version() -> dict:
        from ..protocol.messages import PROTOCOL_VERSION

        return {"app": __version__, "protocol": PROTOCOL_VERSION}

    # --------------------------------------------------------------- devices

    @api.get(f"{API_PREFIX}/devices", tags=["devices"], dependencies=guard)
    async def list_devices() -> list[DeviceOut]:
        return [_device_out(app, d) for d in app.devices.devices.values()]

    @api.get(f"{API_PREFIX}/devices/{{serial}}", tags=["devices"], dependencies=guard)
    async def get_device_info(serial: str) -> DeviceOut:
        return _device_out(app, get_device(serial))

    @api.get(f"{API_PREFIX}/devices/{{serial}}/readings", tags=["devices"], dependencies=guard)
    async def readings(serial: str) -> list[ReadingOut]:
        """The current value of every channel, with how stale each one is."""
        device = get_device(serial)
        out = []
        for cid, (value, age) in device.latest_with_age().items():
            spec = device.channels.get(cid)
            if spec is None:
                continue
            out.append(ReadingOut(
                channel=cid, key=spec.key, name=spec.name, unit=spec.unit,
                value=_finite(value),
                age_s=None if not math.isfinite(age) else round(age, 3),
            ))
        return out

    @api.get(f"{API_PREFIX}/devices/{{serial}}/status", tags=["devices"], dependencies=guard)
    async def device_status(serial: str) -> dict:
        device = get_device(serial)
        return {
            "status": device.status,
            "faults": device.faults(),
            "timebase": device.timebase.fit.to_json(),
            "stream": device.aggregator.health(),
        }

    @api.get(f"{API_PREFIX}/devices/{{serial}}/history", tags=["devices"], dependencies=guard)
    async def history(
        serial: str,
        seconds: float = Query(60.0, gt=0, le=86400),
        channels: str | None = Query(None, description="comma-separated channel ids"),
        max_points: int = Query(5000, ge=10, le=200_000),
    ) -> dict:
        """Recent samples from the live buffer, without touching the database."""
        device = get_device(serial)
        wanted = (
            [int(c) for c in channels.split(",") if c.strip()]
            if channels else list(device.channel_order)
        )
        times, values = device.live.window(seconds)
        if times.size > max_points:
            step = int(np.ceil(times.size / max_points))
            times, values = times[::step], values[::step]
        return {
            "serial": serial,
            "t_utc": times.tolist(),
            "channels": {
                str(cid): [_finite(v) for v in values[:, device.channel_index(cid)]]
                for cid in wanted
                if device.channel_index(cid) is not None
            },
        }

    @api.post(f"{API_PREFIX}/devices/connect", tags=["devices"], dependencies=guard)
    async def connect(body: ConnectIn) -> DeviceOut:
        require_control()
        try:
            device = await app.connect(body.address)
        except Exception as exc:
            raise HTTPException(400, f"could not connect to {body.address}: {exc}") from exc
        return _device_out(app, device)

    @api.post(f"{API_PREFIX}/devices/{{serial}}/disconnect", tags=["devices"], dependencies=guard)
    async def disconnect(serial: str) -> dict:
        require_control()
        get_device(serial)
        await app.disconnect(serial)
        return {"disconnected": serial}

    @api.get(f"{API_PREFIX}/discover", tags=["devices"], dependencies=guard)
    async def discover(bluetooth: bool = False) -> list[dict]:
        found = await app.discover(bluetooth=bluetooth)
        return [
            {"kind": c.kind, "address": c.address, "label": c.label, "serial": c.serial}
            for c in found
        ]

    # ---------------------------------------------------------------- control

    @api.get(f"{API_PREFIX}/devices/{{serial}}/config", tags=["control"], dependencies=guard)
    async def get_config(serial: str) -> dict:
        return get_device(serial).config

    @api.post(f"{API_PREFIX}/devices/{{serial}}/config", tags=["control"], dependencies=guard)
    async def set_config(serial: str, body: ConfigPatch) -> dict:
        require_control()
        device = online_device(serial)
        try:
            return await device.set_config(body.patch, volatile=body.volatile)
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.post(f"{API_PREFIX}/devices/{{serial}}/rtd/wires", tags=["control"], dependencies=guard)
    async def set_wires(serial: str, body: WireMode) -> dict:
        """Switch the PT1000 between 2-, 3- and 4-wire measurement."""
        require_control()
        device = online_device(serial)
        try:
            config = await device.set_wire_mode(body.wires)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        from ..sensors.rtd import wire_mode_note

        return {"rtd": config["rtd"], "note": wire_mode_note(body.wires)}

    @api.get(f"{API_PREFIX}/devices/{{serial}}/calibration", tags=["control"], dependencies=guard)
    async def get_calibration(serial: str) -> dict:
        return get_device(serial).calibration.to_json()

    @api.put(f"{API_PREFIX}/devices/{{serial}}/calibration", tags=["control"], dependencies=guard)
    async def put_calibration(serial: str, body: dict) -> dict:
        """Validate and write a calibration to the board's NVS."""
        require_control()
        device = online_device(serial)
        from ..calibration.models import CalibrationSet

        try:
            written = await device.write_calibration(CalibrationSet.from_json(body))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(500, f"the board refused the calibration: {exc}") from exc
        return written.to_json()

    @api.get(f"{API_PREFIX}/devices/{{serial}}/calibration/history",
             tags=["control"], dependencies=guard)
    async def calibration_history(serial: str) -> list[dict]:
        return app.db.calibration_history(serial)

    @api.post(f"{API_PREFIX}/devices/{{serial}}/display", tags=["control"], dependencies=guard)
    async def set_display(serial: str, body: DisplayPatch) -> dict:
        """Choose what the board's 240x320 screen shows."""
        require_control()
        device = online_device(serial)
        fields = {k: v for k, v in body.model_dump().items() if v is not None}
        if not fields:
            raise HTTPException(400, "no display fields supplied")
        await device.set_display(**fields)
        return {"applied": fields}

    @api.post(f"{API_PREFIX}/devices/{{serial}}/identify", tags=["control"], dependencies=guard)
    async def identify(serial: str, seconds: float = 5.0) -> dict:
        require_control()
        await online_device(serial).identify(seconds)
        return {"identifying_for_s": seconds}

    @api.post(f"{API_PREFIX}/devices/{{serial}}/self-test", tags=["control"], dependencies=guard)
    async def self_test(serial: str) -> dict:
        require_control()
        return await online_device(serial).run_self_test()

    # -------------------------------------------------------------- recording

    @api.get(f"{API_PREFIX}/sessions", tags=["recording"], dependencies=guard)
    async def list_sessions(device: str | None = None, limit: int = 100) -> list[dict]:
        return [s.to_json() for s in app.db.list_sessions(device, limit)]

    @api.get(f"{API_PREFIX}/sessions/{{session_id}}", tags=["recording"], dependencies=guard)
    async def get_session(session_id: int) -> dict:
        info = app.db.get_session(session_id)
        if info is None:
            raise HTTPException(404, f"no session {session_id}")
        payload = info.to_json()
        payload["calibration"] = info.cal
        payload["timebase"] = info.timebase
        payload["markers"] = app.db.list_markers(session_id)
        return payload

    @api.post(f"{API_PREFIX}/devices/{{serial}}/record", tags=["recording"], dependencies=guard)
    async def start_recording(serial: str, body: RecordStart) -> dict:
        require_control()
        device = online_device(serial)
        try:
            recording = app.recorder.start(device, name=body.name, notes=body.notes)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"session_id": recording.session_id, "device": serial}

    @api.post(f"{API_PREFIX}/devices/{{serial}}/record/stop", tags=["recording"], dependencies=guard)
    async def stop_recording(serial: str) -> dict:
        require_control()
        recording = await app.recorder.stop(serial)
        if recording is None:
            raise HTTPException(409, f"{serial} is not recording")
        return {"session_id": recording.session_id, "rows": recording.stats.rows_written}

    @api.post(f"{API_PREFIX}/devices/{{serial}}/record/mark", tags=["recording"], dependencies=guard)
    async def add_marker(serial: str, body: MarkerIn) -> dict:
        require_control()
        marker_id = app.recorder.mark(serial, body.label, body.notes)
        if marker_id is None:
            raise HTTPException(409, f"{serial} is not recording")
        return {"marker_id": marker_id}

    @api.delete(f"{API_PREFIX}/sessions/{{session_id}}", tags=["recording"], dependencies=guard)
    async def delete_session(session_id: int) -> dict:
        require_control()
        if app.db.get_session(session_id) is None:
            raise HTTPException(404, f"no session {session_id}")
        app.db.delete_session(session_id)
        return {"deleted": session_id}

    # ------------------------------------------------------------------ data

    @api.get(f"{API_PREFIX}/sessions/{{session_id}}/data", tags=["data"], dependencies=guard)
    async def session_data(
        session_id: int,
        format: str = Query("json", pattern="^(json|csv)$"),
        t_from: float | None = None,
        t_to: float | None = None,
        channels: str | None = None,
        max_rows: int = Query(100_000, ge=1, le=5_000_000),
    ):
        """Recorded samples, as JSON or streamed CSV."""
        info = app.db.get_session(session_id)
        if info is None:
            raise HTTPException(404, f"no session {session_id}")
        times, values, ids = app.db.read_session(
            session_id, t_from=t_from, t_to=t_to, max_rows=max_rows
        )
        wanted = [int(c) for c in channels.split(",")] if channels else ids
        columns = [(cid, ids.index(cid)) for cid in wanted if cid in ids]

        if format == "csv":
            from ..protocol.channels import spec_for

            def rows():
                header = ["timestamp_utc"] + [
                    f"{spec_for(cid).name} [{spec_for(cid).unit}]" for cid, _ in columns
                ]
                yield ",".join(header) + "\n"
                for index in range(len(times)):
                    cells = [f"{times[index]:.6f}"]
                    for _, col in columns:
                        value = values[index, col]
                        cells.append("" if not math.isfinite(value) else f"{value:.6g}")
                    yield ",".join(cells) + "\n"

            return StreamingResponse(
                rows(), media_type="text/csv",
                headers={"Content-Disposition":
                         f'attachment; filename="session-{session_id}.csv"'},
            )

        return {
            "session": info.to_json(),
            "t_utc": times.tolist(),
            "channels": {
                str(cid): [_finite(v) for v in values[:, col]] for cid, col in columns
            },
        }

    @api.get(f"{API_PREFIX}/sessions/{{session_id}}/summary", tags=["data"], dependencies=guard)
    async def session_summary(session_id: int) -> list[dict]:
        from ..storage.export import summary_table

        if app.db.get_session(session_id) is None:
            raise HTTPException(404, f"no session {session_id}")
        return summary_table(app.db, session_id).to_dict(orient="records")

    @api.get(f"{API_PREFIX}/metrics", response_class=PlainTextResponse, tags=["data"])
    async def metrics() -> str:
        """Prometheus exposition, so a board can feed an existing monitoring stack."""
        lines = [
            "# HELP tjiptemp_reading Current sensor reading",
            "# TYPE tjiptemp_reading gauge",
        ]
        for device in app.devices.devices.values():
            for cid, (value, age) in device.latest_with_age().items():
                spec = device.channels.get(cid)
                if spec is None or not math.isfinite(value):
                    continue
                labels = (f'serial="{device.serial}",channel="{spec.key}",'
                          f'unit="{spec.unit}"')
                lines.append(f"tjiptemp_reading{{{labels}}} {value:.6g}")
                lines.append(f"tjiptemp_reading_age_seconds{{{labels}}} {age:.3f}")
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------- websocket

    @api.websocket(f"{API_PREFIX}/stream")
    async def stream(websocket: WebSocket, serial: str | None = None, rate_hz: float = 4.0):
        """Live readings, pushed as JSON.

        Deliberately rate-limited and snapshot-based rather than forwarding every
        sample block: a browser dashboard wants a few updates a second, not 100.
        Anything that genuinely needs every sample should read the recording.
        """
        token = app.settings.api_token
        if token:
            supplied = websocket.query_params.get("token", "")
            if not secrets.compare_digest(supplied, token):
                await websocket.close(code=4401, reason="invalid token")
                return

        await websocket.accept()
        interval = 1.0 / max(0.2, min(rate_hz, 20.0))
        try:
            await websocket.send_json({
                "type": "hello",
                "app": __version__,
                "devices": [
                    {"serial": d.serial, "name": d.name,
                     "channels": [
                         {"id": s.id, "key": s.key, "name": s.name, "unit": s.unit}
                         for s in d.channels.values()
                     ]}
                    for d in app.devices.devices.values()
                    if serial is None or d.serial == serial
                ],
            })
            while True:
                await asyncio.sleep(interval)
                payload = []
                for device in app.devices.devices.values():
                    if serial is not None and device.serial != serial:
                        continue
                    latest = device.live.latest()
                    if latest is None:
                        continue
                    t, row = latest
                    payload.append({
                        "serial": device.serial,
                        "t_utc": round(float(t), 6),
                        "state": device.state.value,
                        "values": {
                            device.channels[cid].key: _finite(float(row[i]))
                            for i, cid in enumerate(device.channel_order)
                            if cid in device.channels
                        },
                    })
                if payload:
                    await websocket.send_json({"type": "samples",
                                               "sent_at": time.time(),
                                               "devices": payload})
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("websocket stream failed")
            with contextlib.suppress(Exception):
                await websocket.close()

    return api


def build_server(app: Application):
    """Wrap the FastAPI app in a uvicorn server bound to the current event loop."""
    import uvicorn

    settings = app.settings
    host = settings.api_host
    if host not in ("127.0.0.1", "::1", "localhost") and not settings.api_token:
        raise RuntimeError(
            f"Refusing to serve the API on {host} without a token. Anyone who can "
            f"reach that address could recalibrate your boards. Set an API token in "
            f"settings, or bind to 127.0.0.1."
        )

    config = uvicorn.Config(
        build_app(app),
        host=host,
        port=settings.api_port,
        log_level="warning",
        access_log=False,
        loop="none",       # use the loop we are already running on
        lifespan="off",
    )
    return uvicorn.Server(config)
