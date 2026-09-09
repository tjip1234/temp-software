"""The main window: connection management, one tab per board, recordings, settings."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
)

from .. import APP_NAME, __version__
from ..core.application import Application
from ..device.device import Device, DeviceEvent
from ..transport.ble import availability_note
from . import theme as theme_module
from .devicetab import DeviceTab
from .sessions import SessionBrowser
from .theme import Theme, resolve
from .widgets import message_later

log = logging.getLogger(__name__)


class ConnectDialog(QDialog):
    """Discover boards, or type an address directly."""

    def __init__(self, app: Application, parent=None) -> None:
        super().__init__(parent)
        self.app = app
        self.setWindowTitle("Connect to a board")
        self.resize(560, 400)
        self._candidates = []

        layout = QVBoxLayout(self)
        self.status = QLabel("Scanning…")
        layout.addWidget(self.status)

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _: self.accept())
        layout.addWidget(self.list, 1)

        manual = QHBoxLayout()
        manual.addWidget(QLabel("Or address:"))
        self.address = QLineEdit()
        self.address.setPlaceholderText("/dev/ttyACM0 · COM7 · 192.168.1.44 · tjiptemp.local")
        if app.settings.recent_addresses:
            self.address.setText(app.settings.recent_addresses[0])
        manual.addWidget(self.address, 1)
        layout.addLayout(manual)

        options = QHBoxLayout()
        self.bluetooth = QCheckBox("Include Bluetooth (slow scan)")
        self.bluetooth.setToolTip(availability_note())
        self.bluetooth.setEnabled("unavailable" not in availability_note())
        options.addWidget(self.bluetooth)
        rescan = QPushButton("Scan again")
        rescan.clicked.connect(lambda: asyncio.ensure_future(self.scan()))
        options.addStretch(1)
        options.addWidget(rescan)
        layout.addLayout(options)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        asyncio.ensure_future(self.scan())

    async def scan(self) -> None:
        self.status.setText("Scanning for boards…")
        self.list.clear()
        try:
            self._candidates = await self.app.discover(bluetooth=self.bluetooth.isChecked())
        except Exception as exc:
            self.status.setText(f"Scan failed: {exc}")
            return
        for candidate in self._candidates:
            item = QListWidgetItem(f"{candidate.kind.upper()}   {candidate.label}")
            item.setData(Qt.ItemDataRole.UserRole, candidate)
            self.list.addItem(item)
        if not self._candidates:
            self.status.setText(
                "Nothing found. Plug a board in over USB, or type an address below. "
                "On Linux you may need to be in the 'dialout' group to see serial ports."
            )
        else:
            self.status.setText(
                f"{len(self._candidates)} candidate(s). A port is only confirmed as a "
                f"board once it answers."
            )

    def chosen(self):
        item = self.list.currentItem()
        if item is not None:
            return item.data(Qt.ItemDataRole.UserRole)
        return self.address.text().strip() or None


async def _push_rate(device, rate_hz: float) -> None:
    with contextlib.suppress(Exception):
        await device.set_config({"acquire": {"rate_hz": rate_hz}})
        await device.start_streaming(rate_hz)


class SettingsDialog(QDialog):
    def __init__(self, app: Application, parent=None) -> None:
        super().__init__(parent)
        self.app = app
        self.setWindowTitle("Settings")
        self.setMinimumWidth(460)
        settings = app.settings

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.unit = QComboBox()
        for label, value in (("Celsius", "degC"), ("Fahrenheit", "degF"), ("Kelvin", "K")):
            self.unit.addItem(label, value)
        self.unit.setCurrentIndex(max(0, self.unit.findData(settings.temperature_unit)))
        form.addRow("Temperature unit", self.unit)

        self.theme_box = QComboBox()
        for label, value in (("Follow system", "auto"), ("Light", "light"), ("Dark", "dark")):
            self.theme_box.addItem(label, value)
        self.theme_box.setCurrentIndex(max(0, self.theme_box.findData(settings.theme)))
        form.addRow("Appearance", self.theme_box)

        self.rate = QSpinBox()
        self.rate.setRange(1, 100)
        self.rate.setValue(int(settings.stream_rate_hz))
        self.rate.setSuffix(" Hz")
        self.rate.setToolTip(
            "Connecting adopts whatever rate the board is already set to, so this "
            "is only the starting point for a board that does not report one. "
            "Changing it here does apply to the boards that are connected now."
        )
        form.addRow("Sample rate", self.rate)

        self.auto_usb = QCheckBox("Connect to USB boards automatically at startup")
        self.auto_usb.setToolTip(
            "Off by default: starting the program should not claim a serial port "
            "out from under whatever else is using it. Use \"Find USB boards\" in "
            "the toolbar when you want the scan."
        )
        self.auto_usb.setChecked(settings.auto_connect_usb)
        form.addRow(self.auto_usb)

        self.auto_reconnect = QCheckBox("Reconnect automatically when a board drops out")
        self.auto_reconnect.setToolTip(
            "Retries the address a board was last reached on, backing off to once "
            "a minute. A board you disconnect yourself is left alone."
        )
        self.auto_reconnect.setChecked(settings.auto_reconnect)
        form.addRow(self.auto_reconnect)

        self.mdns = QCheckBox("Find boards on the network (mDNS)")
        self.mdns.setChecked(settings.mdns_discovery)
        form.addRow(self.mdns)

        self.api_enabled = QCheckBox("Run the local API server")
        self.api_enabled.setChecked(settings.api_enabled)
        form.addRow(self.api_enabled)

        self.api_host = QLineEdit(settings.api_host)
        form.addRow("API address", self.api_host)

        self.api_port = QSpinBox()
        self.api_port.setRange(1024, 65535)
        self.api_port.setValue(settings.api_port)
        form.addRow("API port", self.api_port)

        self.api_token = QLineEdit(settings.api_token)
        self.api_token.setPlaceholderText("empty = no authentication (loopback only)")
        form.addRow("API token", self.api_token)

        self.api_control = QCheckBox("Allow the API to change board settings and calibration")
        self.api_control.setChecked(settings.api_allow_control)
        form.addRow(self.api_control)
        layout.addLayout(form)

        note = QLabel(
            "Binding the API to anything other than 127.0.0.1 requires a token — "
            "otherwise anyone who can reach that address could recalibrate your boards."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def apply(self) -> None:
        settings = self.app.settings
        settings.temperature_unit = self.unit.currentData()
        settings.theme = self.theme_box.currentData()
        rate = float(self.rate.value())
        if rate != settings.stream_rate_hz:
            # Typed into a dialog and confirmed: an instruction, not a stale
            # preference, so it is the one case that overrules the board.
            for device in self.app.devices.online:
                asyncio.ensure_future(_push_rate(device, rate))
        settings.stream_rate_hz = rate
        settings.auto_connect_usb = self.auto_usb.isChecked()
        settings.auto_reconnect = self.auto_reconnect.isChecked()
        self.app.devices.auto_reconnect = settings.auto_reconnect
        settings.mdns_discovery = self.mdns.isChecked()
        settings.api_enabled = self.api_enabled.isChecked()
        settings.api_host = self.api_host.text().strip() or "127.0.0.1"
        settings.api_port = int(self.api_port.value())
        settings.api_token = self.api_token.text().strip()
        settings.api_allow_control = self.api_control.isChecked()
        settings.save()


class MainWindow(QMainWindow):
    def __init__(self, app: Application) -> None:
        super().__init__()
        self.app = app
        self.theme: Theme = resolve(app.settings.theme)
        self._tabs: dict[str, DeviceTab] = {}

        #: Set once the window has accepted a close. ``__main__.run_gui`` waits
        #: on this and then shuts the application down on a still-running event
        #: loop; Qt's own ``aboutToQuit`` fires too late to be useful for that.
        self.closed = asyncio.Event()

        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1420, 900)

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self._on_tab_close)
        self.setCentralWidget(self.tabs)

        self.sessions = SessionBrowser(app, self.theme)
        self.tabs.addTab(self.sessions, "Recordings")
        self.tabs.tabBar().setTabButton(0, self.tabs.tabBar().ButtonPosition.RightSide, None)

        self._build_toolbar()
        self._build_statusbar()

        app.subscribe(self._on_device_event)

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1000)
        self._status_timer.timeout.connect(self._refresh_status)
        self._status_timer.start()

    # ----------------------------------------------------------------- chrome

    def _build_toolbar(self) -> None:
        bar = self.addToolBar("Main")
        bar.setMovable(False)

        connect = QAction("Connect…", self)
        connect.setShortcut(QKeySequence("Ctrl+N"))
        connect.triggered.connect(self._on_connect)
        bar.addAction(connect)

        scan = QAction("Find USB boards", self)
        scan.triggered.connect(lambda: asyncio.ensure_future(self._auto_connect()))
        bar.addAction(scan)

        bar.addSeparator()

        disconnect = QAction("Disconnect", self)
        disconnect.triggered.connect(self._on_disconnect)
        bar.addAction(disconnect)

        rename = QAction("Rename board", self)
        rename.triggered.connect(self._on_rename)
        bar.addAction(rename)

        bar.addSeparator()

        settings = QAction("Settings…", self)
        settings.triggered.connect(self._on_settings)
        bar.addAction(settings)

        api = QAction("Copy API URL", self)
        api.triggered.connect(self._copy_api_url)
        bar.addAction(api)

        broadcast = QAction("Broadcast temperature…", self)
        broadcast.setToolTip(
            "Publish a channel as a network thermometer for VNA Studio and "
            "anything else that speaks the same protocol."
        )
        broadcast.triggered.connect(self._on_broadcast)
        bar.addAction(broadcast)

        about = QAction("About", self)
        about.triggered.connect(self._on_about)
        bar.addAction(about)

    def _build_statusbar(self) -> None:
        self.status_devices = QLabel("No boards connected")
        self.status_api = QLabel("")
        self.status_broadcast = QLabel("")
        self.status_db = QLabel("")
        bar = self.statusBar()
        bar.addWidget(self.status_devices, 1)
        bar.addPermanentWidget(self.status_api)
        bar.addPermanentWidget(self.status_broadcast)
        bar.addPermanentWidget(self.status_db)

    # ------------------------------------------------------------------ events

    def _on_device_event(self, event: DeviceEvent) -> None:
        if event.kind == "device_added":
            self._add_tab(event.device)
        elif event.kind == "device_removed":
            self._remove_tab(event.device.serial)

    def _add_tab(self, device: Device) -> None:
        if device.serial in self._tabs:
            return
        tab = DeviceTab(self.app, device, self.theme)
        tab.recording_changed.connect(self.sessions.reload)
        self._tabs[device.serial] = tab
        index = self.tabs.addTab(tab, device.label)
        self.tabs.setCurrentIndex(index)

    def _remove_tab(self, serial: str) -> None:
        tab = self._tabs.pop(serial, None)
        if tab is None:
            return
        index = self.tabs.indexOf(tab)
        if index >= 0:
            self.tabs.removeTab(index)
        tab.close_tab()
        tab.deleteLater()

    def _on_tab_close(self, index: int) -> None:
        widget = self.tabs.widget(index)
        for serial, tab in list(self._tabs.items()):
            if tab is widget:
                asyncio.ensure_future(self.app.disconnect(serial))
                return

    # ---------------------------------------------------------------- actions

    def _on_connect(self) -> None:
        dialog = ConnectDialog(self.app, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        target = dialog.chosen()
        if not target:
            return
        asyncio.ensure_future(self._connect(target))

    async def _connect(self, target) -> None:
        try:
            await self.app.connect(target)
        except Exception as exc:
            message_later("critical",
                self, "Could not connect",
                f"{exc}\n\nIf this is a serial port, check that no other program has "
                f"it open and that you have permission to use it."
            )

    async def _auto_connect(self) -> None:
        found = await self.app.auto_connect()
        if not found:
            message_later("information",
                self, "No boards found",
                "No TjipTemp board answered on any USB serial port.\n\n"
                "Check the cable, and on Linux that you are in the 'dialout' group "
                "(or 'uucp' on Arch)."
            )

    def _on_disconnect(self) -> None:
        serial = self._current_serial()
        if serial:
            asyncio.ensure_future(self.app.disconnect(serial))

    def _current_serial(self) -> str | None:
        widget = self.tabs.currentWidget()
        for serial, tab in self._tabs.items():
            if tab is widget:
                return serial
        return None

    def _on_rename(self) -> None:
        serial = self._current_serial()
        if serial is None:
            return
        device = self.app.devices.get(serial)
        if device is None:
            return
        name, ok = QInputDialog.getText(self, "Rename board", "Name:", text=device.name)
        if not ok or not name.strip():
            return
        device.name = name.strip()
        self.app.db.rename_device(serial, device.name)
        tab = self._tabs.get(serial)
        if tab is not None:
            self.tabs.setTabText(self.tabs.indexOf(tab), device.label)

    def _on_settings(self) -> None:
        dialog = SettingsDialog(self.app, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        previous_theme = self.app.settings.theme
        previous_api = (self.app.settings.api_enabled, self.app.settings.api_host,
                        self.app.settings.api_port, self.app.settings.api_token)
        dialog.apply()

        if self.app.settings.theme != previous_theme:
            self.apply_theme(resolve(self.app.settings.theme))
        for tab in self._tabs.values():
            tab.table.set_unit_preference(self.app.settings.temperature_unit)

        current_api = (self.app.settings.api_enabled, self.app.settings.api_host,
                       self.app.settings.api_port, self.app.settings.api_token)
        if current_api != previous_api:
            asyncio.ensure_future(self._restart_api())

    async def _restart_api(self) -> None:
        await self.app.stop_api()
        try:
            await self.app.start_api()
        except Exception as exc:
            message_later("warning", self, "API server not started", str(exc))

    def _on_broadcast(self) -> None:
        from .broadcastdialog import BroadcastDialog

        dialog = BroadcastDialog(self.app, self.theme, self)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def _copy_api_url(self) -> None:
        from PySide6.QtWidgets import QApplication

        if not self.app.api_running:
            QMessageBox.information(
                self, "API not running",
                "Enable the local API server in Settings first."
            )
            return
        url = f"{self.app.api_url}/docs"
        QApplication.clipboard().setText(url)
        self.statusBar().showMessage(f"Copied {url}", 4000)

    def _on_about(self) -> None:
        QMessageBox.about(
            self, f"About {APP_NAME}",
            f"<b>{APP_NAME} {__version__}</b><br><br>"
            f"Desktop control, logging and calibration for the ESP32-S3 thermometer "
            f"board.<br><br>"
            f"Speaks TJIP-1 over USB CDC, WiFi and BLE. The wire protocol is fully "
            f"documented in <code>docs/protocol.md</code>, and a portable C reference "
            f"codec ships in <code>firmware-ref/</code>."
        )

    # ------------------------------------------------------------------ status

    def _refresh_status(self) -> None:
        devices = list(self.app.devices.devices.values())
        online = [d for d in devices if d.is_online]
        recordings = self.app.recorder.active

        if not devices:
            text = "No boards connected"
        else:
            text = f"{len(online)}/{len(devices)} board(s) online"
            if recordings:
                rows = sum(r.stats.rows_written for r in recordings)
                text += f" · recording {len(recordings)} ({rows:,} rows written)"
        self.status_devices.setText(text)

        self.status_api.setText(
            f"API {self.app.api_url}" if self.app.api_running else "API off"
        )

        # Only shown while it is on: an always-visible "off" for a feature most
        # people never use is just noise in the status bar.
        if self.app.broadcast.running:
            state = self.app.broadcast.status()
            reading = state.get("reading")
            text = f"thermometer :{self.app.settings.broadcast_port}"
            if reading:
                text += f" {reading['temperature']:.1f}{reading['unit']}"
            self.status_broadcast.setText(text)
            self.status_broadcast.setToolTip(
                f"{state['url']}\n{state.get('problem') or 'publishing'}"
            )
        else:
            self.status_broadcast.setText("")
            self.status_broadcast.setToolTip("")
        stats = self.app.db.stats()
        self.status_db.setText(
            f"{stats['sessions']} recordings · {stats['rows']:,} samples"
        )

        for serial, tab in self._tabs.items():
            device = self.app.devices.get(serial)
            if device is not None:
                index = self.tabs.indexOf(tab)
                label = device.label
                if self.app.recorder.is_recording(serial):
                    label = "● " + label
                if self.tabs.tabText(index) != label:
                    self.tabs.setTabText(index, label)

    # ------------------------------------------------------------------- theme

    def apply_theme(self, theme: Theme) -> None:
        from PySide6.QtWidgets import QApplication

        self.theme = theme
        theme_module.apply(QApplication.instance(), theme)
        self.sessions.set_theme(theme)
        for tab in self._tabs.values():
            tab.set_theme(theme)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        recordings = self.app.recorder.active
        if recordings:
            answer = QMessageBox.question(
                self, "Recordings in progress",
                f"{len(recordings)} recording(s) are still running. Stop them and "
                f"quit?\n\nAnything already captured is safe on disk either way.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self._status_timer.stop()
        for tab in self._tabs.values():
            with contextlib.suppress(Exception):
                tab.close_tab()
        event.accept()
        self.closed.set()
