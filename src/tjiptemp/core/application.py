"""The application object: everything except the user interface.

The GUI, the headless API server and the test suite all drive this same object.
Keeping it Qt-free is what makes ``tjiptemp --headless`` a real mode rather than a
GUI with the window hidden, and it is what keeps the protocol honest -- if the
device layer needed the UI to function, the API would quietly diverge from it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from platformdirs import user_config_dir, user_data_dir, user_log_dir

from .. import APP_NAME
from ..device.device import Device, DeviceEvent, DeviceManager
from ..storage.db import Database
from ..storage.recorder import Recorder
from ..transport.discovery import (
    Candidate,
    build_transport,
    discover_all,
    discover_serial,
    parse_address,
)

log = logging.getLogger(__name__)


def config_dir() -> Path:
    """Where settings live.

    ``TJIPTEMP_CONFIG_DIR`` overrides it. Without that, anything that builds an
    Application and closes it -- the test suite, a throwaway script -- writes
    the real user's settings on the way out, because close() saves them. That
    is a nasty way to lose a configuration.
    """
    override = os.environ.get("TJIPTEMP_CONFIG_DIR")
    if override:
        return Path(override)
    return Path(user_config_dir(APP_NAME, appauthor=False))


def data_dir() -> Path:
    return Path(user_data_dir(APP_NAME, appauthor=False))


def log_dir() -> Path:
    """Where the rotating log file lives. ``TJIPTEMP_LOG_DIR`` overrides it."""
    override = os.environ.get("TJIPTEMP_LOG_DIR")
    if override:
        return Path(override)
    return Path(user_log_dir(APP_NAME, appauthor=False))


def log_path() -> Path:
    return log_dir() / "tjiptemp.log"


def default_db_path() -> Path:
    return data_dir() / "recordings.tjip"


#: Bump when changing a default that existing settings files already record, and
#: add the corresponding step to ``Settings._migrate``.
SETTINGS_VERSION = 1


@dataclass
class Settings:
    """User preferences. Stored as JSON next to the database."""

    stream_rate_hz: float = 10.0
    #: Off by default. Starting the program should not claim hardware: opening a
    #: serial port takes it away from whatever else is using it, and on an
    #: ESP32-S3 it moves the modem lines on a board that may be mid-measurement.
    #: "Find USB boards" in the toolbar does the same scan when it is wanted.
    auto_connect_usb: bool = False
    auto_reconnect: bool = True
    live_window_s: float = 300.0
    temperature_unit: str = "degC"      # degC | degF | K
    theme: str = "auto"                 # auto | light | dark
    api_enabled: bool = True
    api_host: str = "127.0.0.1"
    api_port: int = 8737
    api_token: str = ""                 # empty = no auth, only safe on loopback
    api_allow_control: bool = True      # allow writes, not just reads
    mdns_discovery: bool = True
    ble_discovery: bool = False

    # --- Network thermometer broadcast (VNA Studio and friends) ------------
    #: Off by default: it binds a routable address, and nothing should start
    #: listening on the network because the app was installed.
    broadcast_enabled: bool = False
    broadcast_host: str = "0.0.0.0"
    broadcast_port: int = 8738
    broadcast_path: str = "/temperature"
    broadcast_ws_path: str = "/ws"
    #: Which transport the advertisement recommends: "http" or "websocket".
    #: Both are always served; this only sets what a discovering client picks.
    broadcast_mode: str = "http"
    #: Shown in the client's device list. Blank derives one from the app name.
    broadcast_name: str = ""
    #: Which board, by serial. Blank means whichever is online.
    broadcast_serial: str = ""
    #: Which channel id, or -1 to pick the best probe the board actually has.
    broadcast_channel: int = -1
    broadcast_unit: str = "degC"
    #: WebSocket push period, seconds.
    broadcast_interval_s: float = 1.0
    #: Moving-average window in seconds; 0 publishes the instantaneous value.
    #: A dielectric sweep takes seconds, so a little smoothing is usually right.
    broadcast_average_s: float = 3.0
    #: A reading older than this is not published.
    broadcast_max_age_s: float = 10.0
    #: What to do when there is nothing fresh: "error" (503) or "last".
    broadcast_stale_policy: str = "error"
    broadcast_mdns: bool = True
    broadcast_beacon: bool = True
    #: Beacon period. The client only listens during a scan a few seconds long,
    #: so this has to be comfortably shorter than that to be caught.
    broadcast_beacon_interval_s: float = 2.0
    db_path: str = ""
    recent_addresses: list[str] = field(default_factory=list)
    #: Bumped when a default changes in a way a stored file would otherwise
    #: keep overriding. See ``_migrate``.
    settings_version: int = SETTINGS_VERSION

    @classmethod
    def load(cls, path: Path | None = None) -> Settings:
        path = path or (config_dir() / "settings.json")
        if not path.exists():
            settings = cls()
            settings._path = path
            return settings
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("could not read %s (%s); using defaults", path, exc)
            settings = cls()
            settings._path = path
            return settings
        known = {f for f in cls.__dataclass_fields__}
        settings = cls(**{k: v for k, v in raw.items() if k in known})
        settings._migrate(int(raw.get("settings_version", 0)))
        settings._path = path
        return settings

    def _migrate(self, was: int) -> None:
        """Apply default changes that a stored file would otherwise override.

        Every field is written out on save, so simply changing a default in this
        class does nothing for anyone who already has a settings file -- the old
        value is in it, and it wins. A default worth changing is usually one that
        was wrong for everybody, so it has to be applied once to the files that
        already exist as well.
        """
        if was < 1:
            # Auto-connect used to be on. Starting the program should not claim
            # hardware: opening a serial port takes it from whatever else has it,
            # and on an ESP32-S3 it moves the modem lines on a board that may be
            # mid-measurement. "Find USB boards" does the same scan on request.
            self.auto_connect_usb = False
        self.settings_version = SETTINGS_VERSION

    def save(self, path: Path | None = None) -> None:
        """Write back to wherever these were loaded from.

        Settings built in code rather than loaded -- a test, a script -- have no
        origin, and writing them to the real config path would overwrite a
        configuration nobody asked to change. Those are dropped instead.
        """
        path = path or getattr(self, "_path", None)
        if path is None:
            return
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    def remember_address(self, address: str) -> None:
        if not address:
            return
        if address in self.recent_addresses:
            self.recent_addresses.remove(address)
        self.recent_addresses.insert(0, address)
        del self.recent_addresses[8:]


class Application:
    """Owns the device manager, the database and the recorder."""

    def __init__(self, settings: Settings | None = None, db_path: Path | None = None) -> None:
        self.settings = settings or Settings.load()
        path = db_path or Path(self.settings.db_path or default_db_path())
        self.db = Database(path)
        self.devices = DeviceManager()
        self.devices.auto_reconnect = self.settings.auto_reconnect
        self.recorder = Recorder(self.db)
        self._api_server = None
        self._api_task: asyncio.Task | None = None
        from ..api.broadcast import TemperatureBroadcast
        self.broadcast = TemperatureBroadcast(self)
        self._listeners: list[Callable[[DeviceEvent], None]] = []
        #: Connects still in their handshake, by link key, so a second request
        #: for the same address joins the first instead of opening it again.
        self._connecting: dict[str, asyncio.Task] = {}
        self.devices.subscribe(self._on_device_event)

    # ------------------------------------------------------------------ events

    def subscribe(self, listener: Callable[[DeviceEvent], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def _on_device_event(self, event: DeviceEvent) -> None:
        if event.kind in ("info", "device_added") and event.device.serial:
            with contextlib.suppress(Exception):
                self.db.upsert_device(event.device.serial, event.device.info, event.device.name)
        if event.kind == "cal" and event.device.serial:
            cal = event.device.calibration
            if cal.rev:
                with contextlib.suppress(Exception):
                    self.db.record_calibration(event.device.serial, cal.to_json())
        for listener in list(self._listeners):
            with contextlib.suppress(Exception):
                listener(event)

    # -------------------------------------------------------------- connecting

    async def discover(
        self, *, usb: bool = True, bluetooth: bool | None = None
    ) -> list[Candidate]:
        return await discover_all(
            usb=usb,
            wifi=self.settings.mdns_discovery,
            bluetooth=self.settings.ble_discovery if bluetooth is None else bluetooth,
        )

    async def connect(self, target: str | Candidate) -> Device:
        """Connect to an address string or a discovered candidate.

        Asking for an address that is already connected returns that board, and
        asking while a connect to it is still in its handshake waits for that
        attempt. Clicking again during a slow connect used to open the port a
        second time; the first attempt already held it, so the second failed
        with "already open in another program" -- a failure reported for a
        board that was, in fact, connecting.
        """
        candidate = target if isinstance(target, Candidate) else parse_address(target)
        if candidate is None:
            raise ValueError(f"could not interpret {target!r} as a device address")
        key = candidate.key
        for device in self.devices.devices.values():
            if any(link.key == key and link.is_open for link in device.links.values()):
                return device
        inflight = self._connecting.get(key)
        if inflight is not None:
            log.info("already connecting to %s; waiting for that attempt", candidate.address)
            # Shielded: giving up on the wait must not cancel the attempt it joined.
            return await asyncio.shield(inflight)

        task = asyncio.ensure_future(self._connect(candidate))
        self._connecting[key] = task

        def forget(done: asyncio.Task) -> None:
            if self._connecting.get(key) is done:
                del self._connecting[key]

        task.add_done_callback(forget)
        return await task

    async def _connect(self, candidate: Candidate) -> Device:
        # The rate is a fallback, not an instruction: Device._adopt_board_settings
        # replaces it with the board's own acquire.rate_hz during the handshake,
        # and streaming then starts at whatever the board was already set to.
        self.devices.default_rate_hz = self.settings.stream_rate_hz
        device = await self.devices.connect(build_transport(candidate))
        # Mirror it back so the UI and the next connect show what the board says,
        # rather than a preference the board has already overruled.
        self.settings.stream_rate_hz = device.stream_rate_hz
        self.settings.remember_address(candidate.address)
        return device

    async def auto_connect(self) -> list[Device]:
        """Connect to every board we can find over USB. Best-effort and quiet.

        A serial port that turns out not to be a board is skipped without fuss:
        the whole point of requiring a DEVICE_INFO before believing anything is
        that probing is safe.

        Only USB is scanned: this used to run the network browse as well, which
        is three seconds of waiting for results that were then thrown away. And
        the ports are tried at once rather than in turn, because a port that is
        not a board uses up the whole handshake window before giving up, and a
        board further down the list should not have to wait for it.
        """
        candidates = discover_serial()
        log.info("USB scan: %s",
                 "; ".join(c.label for c in candidates) or "no candidate ports")
        outcomes = await asyncio.gather(
            *(self.connect(candidate) for candidate in candidates), return_exceptions=True
        )
        connected: list[Device] = []
        for candidate, outcome in zip(candidates, outcomes, strict=True):
            if isinstance(outcome, Device):
                if outcome not in connected:
                    connected.append(outcome)
            else:
                log.info("%s did not answer as a TjipTemp board: %s",
                         candidate.address, outcome)
        return connected

    async def disconnect(self, serial: str) -> None:
        with contextlib.suppress(Exception):
            await self.recorder.stop(serial)
        await self.devices.disconnect(serial)

    # -------------------------------------------------------------- API server

    async def start_api(self) -> str | None:
        """Start the REST/WebSocket server. Returns its base URL, or None if disabled."""
        if not self.settings.api_enabled:
            return None
        if self._api_task is not None and not self._api_task.done():
            return self.api_url

        from ..api.server import build_server

        server = build_server(self)
        self._api_server = server
        self._api_task = asyncio.create_task(server.serve(), name="tjiptemp-api")
        # Give uvicorn a moment to bind so a port conflict surfaces here rather
        # than as a mysterious silence later.
        for _ in range(50):
            await asyncio.sleep(0.02)
            if getattr(server, "started", False):
                return self.api_url
            if self._api_task.done():
                exc = self._api_task.exception()
                if exc:
                    raise exc
        return self.api_url

    async def stop_api(self) -> None:
        server, self._api_server = self._api_server, None
        task, self._api_task = self._api_task, None
        if server is not None:
            server.should_exit = True
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(task, timeout=5.0)

    # ------------------------------------------------- thermometer broadcast

    async def start_broadcast(self) -> str | None:
        """Publish a channel as a network thermometer. None if disabled."""
        return await self.broadcast.start()

    async def stop_broadcast(self) -> None:
        await self.broadcast.stop()

    async def restart_broadcast(self) -> str | None:
        return await self.broadcast.restart()

    @property
    def api_url(self) -> str:
        return f"http://{self.settings.api_host}:{self.settings.api_port}"

    @property
    def api_running(self) -> bool:
        return self._api_task is not None and not self._api_task.done()

    # ---------------------------------------------------------------- shutdown

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.stop_broadcast()
        await self.stop_api()
        with contextlib.suppress(Exception):
            await self.recorder.stop_all()
        await self.devices.close()
        with contextlib.suppress(Exception):
            self.settings.save()
        self.db.close()

    def health(self) -> dict:
        return {
            "app": APP_NAME,
            "devices": self.devices.health(),
            "recordings": self.recorder.status(),
            "database": self.db.stats(),
            "api": {"running": self.api_running, "url": self.api_url if self.api_running else None},
            "broadcast": self.broadcast.status(),
        }
