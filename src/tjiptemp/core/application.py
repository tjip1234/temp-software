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
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from platformdirs import user_config_dir, user_data_dir

from .. import APP_NAME
from ..device.device import Device, DeviceEvent, DeviceManager
from ..storage.db import Database
from ..storage.recorder import Recorder
from ..transport.discovery import Candidate, build_transport, discover_all, parse_address

log = logging.getLogger(__name__)


def config_dir() -> Path:
    return Path(user_config_dir(APP_NAME, appauthor=False))


def data_dir() -> Path:
    return Path(user_data_dir(APP_NAME, appauthor=False))


def default_db_path() -> Path:
    return data_dir() / "recordings.tjip"


@dataclass
class Settings:
    """User preferences. Stored as JSON next to the database."""

    stream_rate_hz: float = 10.0
    auto_connect_usb: bool = True
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
    db_path: str = ""
    recent_addresses: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path | None = None) -> Settings:
        path = path or (config_dir() / "settings.json")
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("could not read %s (%s); using defaults", path, exc)
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self, path: Path | None = None) -> None:
        path = path or (config_dir() / "settings.json")
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
        self.recorder = Recorder(self.db)
        self._api_server = None
        self._api_task: asyncio.Task | None = None
        self._listeners: list[Callable[[DeviceEvent], None]] = []
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

    async def discover(self, *, bluetooth: bool | None = None) -> list[Candidate]:
        return await discover_all(
            usb=True,
            wifi=self.settings.mdns_discovery,
            bluetooth=self.settings.ble_discovery if bluetooth is None else bluetooth,
        )

    async def connect(self, target: str | Candidate) -> Device:
        """Connect to an address string or a discovered candidate."""
        candidate = target if isinstance(target, Candidate) else parse_address(target)
        if candidate is None:
            raise ValueError(f"could not interpret {target!r} as a device address")
        device = await self.devices.connect(build_transport(candidate))
        await device.start_streaming(self.settings.stream_rate_hz)
        self.settings.remember_address(candidate.address)
        return device

    async def auto_connect(self) -> list[Device]:
        """Connect to every board we can find over USB. Best-effort and quiet.

        A serial port that turns out not to be a board is skipped without fuss:
        the whole point of requiring a DEVICE_INFO before believing anything is
        that probing is safe.
        """
        connected: list[Device] = []
        for candidate in await self.discover(bluetooth=False):
            if candidate.kind != "usb":
                continue
            try:
                connected.append(await self.connect(candidate))
            except Exception as exc:
                log.debug("%s is not a TjipTemp board: %s", candidate.address, exc)
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

    @property
    def api_url(self) -> str:
        return f"http://{self.settings.api_host}:{self.settings.api_port}"

    @property
    def api_running(self) -> bool:
        return self._api_task is not None and not self._api_task.done()

    # ---------------------------------------------------------------- shutdown

    async def close(self) -> None:
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
        }
