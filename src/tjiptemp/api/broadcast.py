"""Broadcast one channel as a network thermometer.

VNA Studio (and anything else speaking the same minimal protocol) can take a
live temperature from another machine and overlay it on a dielectric recording.
Its contract is deliberately tiny -- a JSON object with a ``temperature`` key,
reachable over HTTP or a WebSocket, advertised by mDNS or a UDP beacon -- so
this module implements exactly that and nothing more:

    GET  <path>      ->  {"temperature": 23.5, "unit": "°C", ...}
    WS   <ws_path>   ->  the same object, pushed every interval
    mDNS             ->  _thermometer._tcp.local. with path/ws_path/mode TXT
    UDP  :5556       ->  {"name", "host", "port", "path", "ws_path", "mode"}

Deliberately a separate server from the main REST API, on its own port and its
own switch. The API binds loopback and wants a token because it can recalibrate
a board; this one has to be reachable from whatever machine is driving the VNA,
and exposes a single float. Those are different security postures and they
should not share a socket.

Extra keys in the JSON (``serial``, ``channel``, ``age_s``) are there for
whoever reads it by hand. Clients that follow the contract ignore them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import socket
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse

from .. import APP_NAME, __version__
from ..protocol.channels import Ch

if TYPE_CHECKING:
    from ..core.application import Application
    from ..device.device import Device

log = logging.getLogger(__name__)

#: The service type VNA Studio browses for. Not ours to choose.
MDNS_SERVICE = "_thermometer._tcp.local."

#: Where VNA Studio listens for discovery beacons. Also not ours to choose.
BEACON_PORT = 5556

#: Unit codes as stored in settings, mapped to the strings the client displays.
UNIT_SYMBOLS = {"degC": "°C", "degF": "°F", "K": "K"}

#: ``channel = AUTO`` picks the most sensible probe the board actually has,
#: in this order, rather than making the user rediscover which id is which.
AUTO_CHANNEL = -1
AUTO_PREFERENCE = (Ch.PT1000, Ch.TYPEK, Ch.NTC_EXT1, Ch.NTC_EXT2,
                   Ch.NTC_EXT3, Ch.NTC_EXT4, Ch.AHT20_T)


def to_unit(celsius: float, unit: str) -> float:
    if unit == "degF":
        return celsius * 9.0 / 5.0 + 32.0
    if unit == "K":
        return celsius + 273.15
    return celsius


def broadcast_addresses() -> list[str]:
    """Every address a discovery beacon should go to.

    The limited broadcast address 255.255.255.255 is not enough on its own. The
    kernel sends it out one interface and does not loop it back to sockets on
    this machine, so a client running on the same computer -- which is the
    normal setup when one machine drives both the VNA and the thermometer --
    never sees a beacon at all. Per-interface broadcast addresses are delivered
    locally and reach every subnet the machine is on, so they are what actually
    works; the limited address stays in the list as a fallback for anything the
    enumeration missed.
    """
    targets: list[str] = []
    try:
        import fcntl
        import struct

        SIOCGIFBRDADDR = 0x8919
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for _index, name in socket.if_nameindex():
                try:
                    packed = fcntl.ioctl(
                        sock.fileno(), SIOCGIFBRDADDR,
                        struct.pack("256s", name.encode()[:15]),
                    )
                    address = socket.inet_ntoa(packed[20:24])
                except OSError:
                    continue
                if address not in ("0.0.0.0", "") and address not in targets:
                    targets.append(address)
        finally:
            sock.close()
    except (ImportError, AttributeError, OSError):
        pass          # not Linux, or no ioctl: the fallbacks below still work
    for fallback in ("255.255.255.255", "127.255.255.255"):
        if fallback not in targets:
            targets.append(fallback)
    return targets


def local_ip(peer: str = "8.8.8.8") -> str:
    """This machine's address on the interface that reaches the network.

    A UDP socket is connected but nothing is sent -- it exists only to make the
    kernel choose a route and tell us which source address it picked. Falls back
    to the hostname's own resolution, then to loopback.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((peer, 53))
        return sock.getsockname()[0]
    except OSError:
        pass
    finally:
        sock.close()
    with contextlib.suppress(OSError):
        return socket.gethostbyname(socket.gethostname())
    return "127.0.0.1"


@dataclass(frozen=True)
class Reading:
    """One value, ready to serialise."""

    celsius: float
    age_s: float
    serial: str
    channel_id: int
    channel_name: str
    device_label: str

    def payload(self, unit: str, name: str) -> dict:
        return {
            "temperature": round(to_unit(self.celsius, unit), 4),
            "unit": UNIT_SYMBOLS.get(unit, "°C"),
            "name": name,
            "serial": self.serial,
            "channel": self.channel_name,
            "channel_id": self.channel_id,
            "age_s": round(self.age_s, 3),
            "source": self.device_label,
        }


class Stale(Exception):
    """No reading fresh enough to publish."""


class TemperatureBroadcast:
    """Serves, advertises and beacons one channel of one board."""

    def __init__(self, app: Application) -> None:
        self.app = app
        self._server = None
        self._task: asyncio.Task | None = None
        self._beacon: asyncio.Task | None = None
        self._zc = None
        self._service = None
        self._last_good: Reading | None = None
        self._served = 0
        self._ws_clients = 0
        self._started_at = 0.0
        self._error: str | None = None

    # ------------------------------------------------------------- settings

    @property
    def settings(self):
        return self.app.settings

    @property
    def service_name(self) -> str:
        return self.settings.broadcast_name or f"{APP_NAME} thermometer"

    @property
    def url(self) -> str:
        host = self.settings.broadcast_host
        shown = local_ip() if host in ("0.0.0.0", "::", "") else host
        return f"http://{shown}:{self.settings.broadcast_port}{self.settings.broadcast_path}"

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # -------------------------------------------------------------- reading

    def _pick_device(self) -> Device | None:
        wanted = self.settings.broadcast_serial
        if wanted:
            device = self.app.devices.get(wanted)
            return device if device is not None and device.is_online else None
        online = self.app.devices.online
        return online[0] if online else None

    def _pick_channel(self, device: Device, values: dict) -> int | None:
        wanted = int(self.settings.broadcast_channel)
        if wanted != AUTO_CHANNEL:
            return wanted if wanted in device.channels else None
        for candidate in AUTO_PREFERENCE:
            cid = int(candidate)
            entry = values.get(cid)
            if cid in device.channels and entry is not None and math.isfinite(entry[0]):
                return cid
        return None

    def reading(self) -> Reading:
        """The value to publish right now, or raise Stale."""
        device = self._pick_device()
        if device is None:
            raise Stale("no board online")
        values = device.latest_with_age()
        cid = self._pick_channel(device, values)
        if cid is None:
            raise Stale("the selected channel is not on this board")
        value, age = values.get(cid, (float("nan"), float("inf")))

        window = float(self.settings.broadcast_average_s)
        if window > 0:
            _, series = device.series(cid, window)
            if series.size:
                mean = float(np.nanmean(series))
                if math.isfinite(mean):
                    value = mean

        spec = device.channels.get(cid)
        if not math.isfinite(value):
            raise Stale("no finite reading on that channel")
        if age > float(self.settings.broadcast_max_age_s):
            raise Stale(f"last reading is {age:.1f} s old")

        fresh = Reading(
            celsius=value,
            age_s=age,
            serial=device.serial,
            channel_id=cid,
            channel_name=spec.name if spec else str(cid),
            device_label=device.label,
        )
        self._last_good = fresh
        return fresh

    def _payload(self) -> dict:
        """A reading honouring the stale policy, or raise Stale."""
        try:
            reading = self.reading()
        except Stale:
            if self.settings.broadcast_stale_policy == "last" and self._last_good:
                reading = self._last_good
            else:
                raise
        return reading.payload(self.settings.broadcast_unit, self.service_name)

    # --------------------------------------------------------------- server

    def _build_app(self):
        # FastAPI is imported at module scope on purpose. This module uses
        # ``from __future__ import annotations``, so every annotation is a
        # string that FastAPI resolves against the *module* globals -- with
        # these names imported inside the function, ``websocket: WebSocket``
        # resolved to nothing, FastAPI read it as a missing query parameter and
        # closed every upgrade with 1008, which the client saw as HTTP 403.
        settings = self.settings
        api = FastAPI(
            title=f"{APP_NAME} thermometer",
            version=__version__,
            docs_url=None,
            redoc_url=None,
            openapi_url=None,
        )

        @api.get(settings.broadcast_path)
        async def temperature() -> dict:
            self._served += 1
            try:
                return self._payload()
            except Stale as exc:
                # 503 rather than a made-up number: a client that shows the
                # last value forever is worse than one that shows nothing.
                raise HTTPException(status_code=503, detail=str(exc)) from exc

        @api.get("/status")
        async def status() -> dict:
            return self.status()

        @api.get("/", response_class=PlainTextResponse)
        async def index() -> str:
            return self.describe()

        @api.websocket(settings.broadcast_ws_path)
        async def stream(websocket: WebSocket) -> None:
            await websocket.accept()
            self._ws_clients += 1
            try:
                while True:
                    with contextlib.suppress(Stale):
                        await websocket.send_json(self._payload())
                        self._served += 1
                    await asyncio.sleep(max(0.1, float(settings.broadcast_interval_s)))
            except (WebSocketDisconnect, ConnectionError, RuntimeError):
                pass
            finally:
                self._ws_clients -= 1
                with contextlib.suppress(Exception):
                    await websocket.close()

        return api

    async def start(self) -> str | None:
        if not self.settings.broadcast_enabled:
            return None
        if self.running:
            return self.url

        import uvicorn

        self._error = None
        config = uvicorn.Config(
            self._build_app(),
            host=self.settings.broadcast_host or "0.0.0.0",
            port=int(self.settings.broadcast_port),
            log_level="warning",
            access_log=False,
            loop="none",
            lifespan="off",
        )
        server = uvicorn.Server(config)
        self._server = server
        self._task = asyncio.create_task(server.serve(), name="tjiptemp-thermometer")

        # Surface a port clash here rather than as a silent absence later.
        for _ in range(50):
            await asyncio.sleep(0.02)
            if getattr(server, "started", False):
                break
            if self._task.done():
                exc = self._task.exception()
                self._error = str(exc) if exc else "server stopped immediately"
                self._task = None
                self._server = None
                if exc:
                    raise exc
                return None

        self._started_at = time.time()
        await self._advertise()
        if self.settings.broadcast_beacon:
            self._beacon = asyncio.create_task(self._beacon_loop(),
                                               name="tjiptemp-thermometer-beacon")
        log.info("thermometer broadcast on %s", self.url)
        return self.url

    async def stop(self) -> None:
        beacon, self._beacon = self._beacon, None
        if beacon is not None:
            beacon.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await beacon
        await self._unadvertise()
        server, self._server = self._server, None
        task, self._task = self._task, None
        if server is not None:
            server.should_exit = True
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(task, timeout=5.0)
        self._served = 0
        self._ws_clients = 0

    async def restart(self) -> str | None:
        await self.stop()
        return await self.start()

    # ------------------------------------------------------------ discovery

    def _beacon_message(self) -> bytes:
        settings = self.settings
        return json.dumps({
            "name": self.service_name,
            "host": local_ip(),
            "port": int(settings.broadcast_port),
            "path": settings.broadcast_path,
            "ws_path": settings.broadcast_ws_path,
            "mode": settings.broadcast_mode,
        }).encode("utf-8")

    async def _beacon_loop(self) -> None:
        """Announce ourselves on the broadcast address, forever.

        The client's scan window is a few seconds wide and it only listens
        while scanning, so the interval has to be comfortably shorter than that
        or a scan lands between two beacons and finds nothing.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setblocking(False)
        loop = asyncio.get_running_loop()
        targets = broadcast_addresses()
        log.debug("beaconing to %s", ", ".join(targets))
        delivered = False
        try:
            while True:
                message = self._beacon_message()
                sent = 0
                for target in targets:
                    try:
                        await loop.sock_sendto(sock, message, (target, BEACON_PORT))
                        sent += 1
                    except (OSError, NotImplementedError):
                        continue
                if sent and not delivered:
                    delivered = True
                elif not sent and delivered:
                    log.warning("discovery beacon could not be sent to any of %s",
                                ", ".join(targets))
                    delivered = False
                await asyncio.sleep(max(0.5, float(self.settings.broadcast_beacon_interval_s)))
        except asyncio.CancelledError:
            raise
        finally:
            sock.close()

    async def _advertise(self) -> None:
        if not self.settings.broadcast_mdns:
            return
        try:
            from zeroconf import ServiceInfo
            from zeroconf.asyncio import AsyncZeroconf
        except ImportError:
            log.debug("zeroconf not installed; mDNS advertisement skipped")
            return

        settings = self.settings
        safe = "".join(c if c.isalnum() or c in "- " else "-" for c in self.service_name)
        try:
            info = ServiceInfo(
                MDNS_SERVICE,
                f"{safe}.{MDNS_SERVICE}",
                addresses=[socket.inet_aton(local_ip())],
                port=int(settings.broadcast_port),
                properties={
                    "path": settings.broadcast_path,
                    "ws_path": settings.broadcast_ws_path,
                    "mode": settings.broadcast_mode,
                },
                server=f"{safe.replace(' ', '-')}.local.",
            )
            zc = AsyncZeroconf()
            await zc.async_register_service(info)
        except Exception as exc:      # noqa: BLE001 - advertisement is optional
            log.warning("mDNS advertisement failed (%s); the UDP beacon still works", exc)
            return
        self._zc, self._service = zc, info

    async def _unadvertise(self) -> None:
        zc, self._zc = self._zc, None
        info, self._service = self._service, None
        if zc is None:
            return
        with contextlib.suppress(Exception):
            if info is not None:
                await zc.async_unregister_service(info)
        with contextlib.suppress(Exception):
            await zc.async_close()

    # ----------------------------------------------------------- reporting

    def status(self) -> dict:
        settings = self.settings
        try:
            reading = self.reading()
            live = reading.payload(settings.broadcast_unit, self.service_name)
            problem = None
        except Stale as exc:
            live, problem = None, str(exc)
        return {
            "running": self.running,
            "url": self.url if self.running else None,
            "ws_url": (f"ws://{local_ip()}:{settings.broadcast_port}"
                       f"{settings.broadcast_ws_path}") if self.running else None,
            "name": self.service_name,
            "mode": settings.broadcast_mode,
            "mdns": bool(settings.broadcast_mdns and self._zc is not None),
            "beacon": bool(self._beacon is not None and not self._beacon.done()),
            "uptime_s": round(time.time() - self._started_at, 1) if self.running else 0.0,
            "readings_served": self._served,
            "ws_clients": self._ws_clients,
            "reading": live,
            "problem": problem or self._error,
        }

    def describe(self) -> str:
        """What this port is, for whoever finds it with a browser."""
        settings = self.settings
        return (
            f"{APP_NAME} {__version__} — network thermometer\n"
            f"\n"
            f"  GET  {settings.broadcast_path}   {{\"temperature\": <float>, \"unit\": \"...\"}}\n"
            f"  WS   {settings.broadcast_ws_path}   the same object, every "
            f"{settings.broadcast_interval_s:g} s\n"
            f"  GET  /status        what this is currently publishing\n"
            f"\n"
            f"Advertised as {MDNS_SERVICE} and by UDP beacon on port {BEACON_PORT}.\n"
            f"Publishing: {self.service_name}\n"
            f"This endpoint is read-only. Board control lives on the main API port.\n"
        )
