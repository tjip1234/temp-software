"""Publish a channel as a network thermometer.

VNA Studio overlays a live temperature on a dielectric recording and finds its
sensor by mDNS or a UDP beacon. Everything here configures what it sees: which
board and channel, in what unit, how smoothed, under what name, and how it is
advertised. The panel follows the running service at 1 Hz so a wrong setting
shows up as a stale reading here rather than as a gap in someone's experiment.
"""

from __future__ import annotations

import asyncio
import contextlib

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..api.broadcast import AUTO_CHANNEL, BEACON_PORT, MDNS_SERVICE
from .theme import Theme
from .widgets import alive

#: Stale-policy codes and what they mean to somebody reading a plot later.
STALE_TEXT = (
    ("error", "Report the sensor as unavailable (recommended)"),
    ("last", "Keep publishing the last good reading"),
)


class BroadcastDialog(QDialog):
    """Configure and control the network thermometer."""

    def __init__(self, app, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self.theme = theme
        self.setWindowTitle("Broadcast temperature")
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)

        intro = QLabel(
            "Publishes one channel as a network thermometer, so VNA Studio — or "
            "anything else that speaks the same protocol — can overlay it on a "
            "recording. Read-only: this port cannot change anything on a board."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {theme.text_secondary};")
        layout.addWidget(intro)

        self.enabled = QCheckBox("Publish a thermometer on the network")
        self.enabled.setToolTip(
            "Binds a routable address, so it is off until you ask for it."
        )
        layout.addWidget(self.enabled)

        layout.addWidget(self._build_source())
        layout.addWidget(self._build_network())
        layout.addWidget(self._build_discovery())

        self.status = QLabel("—")
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.status)

        buttons = QDialogButtonBox()
        self.apply_button = buttons.addButton(
            "Apply", QDialogButtonBox.ButtonRole.ApplyRole
        )
        buttons.addButton(QDialogButtonBox.StandardButton.Close)
        self.apply_button.clicked.connect(self._apply)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.enabled.toggled.connect(self._sync_enabled)
        self.board.currentIndexChanged.connect(lambda _: self._reload_channels())

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(1000)

        self._load()
        self._refresh()

    # ------------------------------------------------------------------ build

    def _build_source(self) -> QWidget:
        box = QGroupBox("What to publish")
        form = QFormLayout(box)

        self.board = QComboBox()
        self.board.setToolTip(
            "Which board the reading comes from. 'Whichever is online' follows "
            "the first connected board, which is what you want with one board."
        )
        form.addRow("Board", self.board)

        self.channel = QComboBox()
        self.channel.setToolTip(
            "Automatic prefers the PT1000, then the thermocouple, then the "
            "external NTCs — the first one actually reading on this board."
        )
        form.addRow("Channel", self.channel)

        self.unit = QComboBox()
        for label, value in (("Celsius", "degC"), ("Fahrenheit", "degF"), ("Kelvin", "K")):
            self.unit.addItem(label, value)
        form.addRow("Unit", self.unit)

        self.average = QDoubleSpinBox()
        self.average.setRange(0.0, 120.0)
        self.average.setSingleStep(0.5)
        self.average.setDecimals(1)
        self.average.setSuffix(" s")
        self.average.setSpecialValueText("off — publish the instantaneous value")
        self.average.setToolTip(
            "A dielectric sweep takes seconds, so an averaged temperature "
            "describes it better than whatever the sensor read at one instant."
        )
        form.addRow("Smoothing", self.average)

        self.max_age = QDoubleSpinBox()
        self.max_age.setRange(1.0, 600.0)
        self.max_age.setSuffix(" s")
        self.max_age.setToolTip("A reading older than this counts as no reading.")
        form.addRow("Treat as stale after", self.max_age)

        self.stale = QComboBox()
        for value, label in STALE_TEXT:
            self.stale.addItem(label, value)
        form.addRow("When stale", self.stale)
        return box

    def _build_network(self) -> QWidget:
        box = QGroupBox("Where to serve it")
        form = QFormLayout(box)

        self.name = QLineEdit()
        self.name.setPlaceholderText("TjipTemp thermometer")
        self.name.setToolTip("The name that appears in the client's device list.")
        form.addRow("Service name", self.name)

        self.host = QLineEdit()
        self.host.setToolTip(
            "0.0.0.0 serves every interface, which is what a client on another "
            "machine needs. 127.0.0.1 restricts it to this computer."
        )
        form.addRow("Bind address", self.host)

        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        form.addRow("Port", self.port)

        self.path = QLineEdit()
        self.path.setToolTip("Clients default to /temperature.")
        form.addRow("HTTP path", self.path)

        self.ws_path = QLineEdit()
        self.ws_path.setToolTip("Clients default to /ws.")
        form.addRow("WebSocket path", self.ws_path)

        self.mode = QComboBox()
        self.mode.addItem("HTTP polling", "http")
        self.mode.addItem("WebSocket push", "websocket")
        self.mode.setToolTip(
            "Both are always served. This only sets which one a client picks "
            "when it discovers us."
        )
        form.addRow("Advertise as", self.mode)

        self.interval = QDoubleSpinBox()
        self.interval.setRange(0.1, 60.0)
        self.interval.setSingleStep(0.5)
        self.interval.setDecimals(1)
        self.interval.setSuffix(" s")
        self.interval.setToolTip("How often the WebSocket pushes a reading.")
        form.addRow("Push every", self.interval)
        return box

    def _build_discovery(self) -> QWidget:
        box = QGroupBox("How clients find it")
        form = QFormLayout(box)

        self.mdns = QCheckBox(f"Advertise over mDNS as {MDNS_SERVICE}")
        form.addRow(self.mdns)

        self.beacon = QCheckBox(f"Send a UDP discovery beacon on port {BEACON_PORT}")
        form.addRow(self.beacon)

        self.beacon_interval = QDoubleSpinBox()
        self.beacon_interval.setRange(0.5, 60.0)
        self.beacon_interval.setSingleStep(0.5)
        self.beacon_interval.setDecimals(1)
        self.beacon_interval.setSuffix(" s")
        self.beacon_interval.setToolTip(
            "A client only listens while it is scanning, for a few seconds. "
            "Beacon less often than that and a scan can land between two and "
            "find nothing."
        )
        form.addRow("Beacon every", self.beacon_interval)

        copy = QPushButton("Copy the HTTP URL")
        copy.clicked.connect(self._copy_url)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(copy)
        form.addRow(row)

        self.beacon.toggled.connect(self.beacon_interval.setEnabled)
        return box

    # ------------------------------------------------------------------ state

    def _load(self) -> None:
        s = self.app.settings
        self.enabled.setChecked(s.broadcast_enabled)

        self.board.clear()
        self.board.addItem("Whichever is online", "")
        for serial, device in self.app.devices.devices.items():
            self.board.addItem(device.label, serial)
        self.board.setCurrentIndex(max(0, self.board.findData(s.broadcast_serial)))

        self._reload_channels()
        self.unit.setCurrentIndex(max(0, self.unit.findData(s.broadcast_unit)))
        self.average.setValue(s.broadcast_average_s)
        self.max_age.setValue(s.broadcast_max_age_s)
        self.stale.setCurrentIndex(max(0, self.stale.findData(s.broadcast_stale_policy)))

        self.name.setText(s.broadcast_name)
        self.host.setText(s.broadcast_host)
        self.port.setValue(s.broadcast_port)
        self.path.setText(s.broadcast_path)
        self.ws_path.setText(s.broadcast_ws_path)
        self.mode.setCurrentIndex(max(0, self.mode.findData(s.broadcast_mode)))
        self.interval.setValue(s.broadcast_interval_s)

        self.mdns.setChecked(s.broadcast_mdns)
        self.beacon.setChecked(s.broadcast_beacon)
        self.beacon_interval.setValue(s.broadcast_beacon_interval_s)
        self.beacon_interval.setEnabled(s.broadcast_beacon)
        self._sync_enabled(s.broadcast_enabled)

    def _reload_channels(self) -> None:
        """Offer the channels of the selected board, or of any board we have."""
        wanted = self.app.settings.broadcast_channel
        self.channel.clear()
        self.channel.addItem("Automatic — best available probe", AUTO_CHANNEL)

        serial = self.board.currentData()
        devices = (
            [self.app.devices.get(serial)] if serial
            else list(self.app.devices.devices.values())
        )
        seen: dict[int, str] = {}
        for device in devices:
            if device is None:
                continue
            for cid, spec in sorted(device.channels.items()):
                if spec.is_temperature and cid not in seen:
                    seen[cid] = spec.name
        for cid, label in seen.items():
            self.channel.addItem(label, cid)
        self.channel.setCurrentIndex(max(0, self.channel.findData(wanted)))

    def _sync_enabled(self, on: bool) -> None:
        for widget in (self.board, self.channel, self.unit, self.average,
                       self.max_age, self.stale, self.name, self.host, self.port,
                       self.path, self.ws_path, self.mode, self.interval,
                       self.mdns, self.beacon):
            widget.setEnabled(on)
        self.beacon_interval.setEnabled(on and self.beacon.isChecked())

    def _collect(self) -> None:
        s = self.app.settings
        s.broadcast_enabled = self.enabled.isChecked()
        s.broadcast_serial = self.board.currentData() or ""
        s.broadcast_channel = int(self.channel.currentData())
        s.broadcast_unit = self.unit.currentData()
        s.broadcast_average_s = float(self.average.value())
        s.broadcast_max_age_s = float(self.max_age.value())
        s.broadcast_stale_policy = self.stale.currentData()
        s.broadcast_name = self.name.text().strip()
        s.broadcast_host = self.host.text().strip() or "0.0.0.0"
        s.broadcast_port = int(self.port.value())
        s.broadcast_path = self._clean_path(self.path.text(), "/temperature")
        s.broadcast_ws_path = self._clean_path(self.ws_path.text(), "/ws")
        s.broadcast_mode = self.mode.currentData()
        s.broadcast_interval_s = float(self.interval.value())
        s.broadcast_mdns = self.mdns.isChecked()
        s.broadcast_beacon = self.beacon.isChecked()
        s.broadcast_beacon_interval_s = float(self.beacon_interval.value())
        s.save()

    @staticmethod
    def _clean_path(text: str, fallback: str) -> str:
        text = text.strip()
        if not text:
            return fallback
        return text if text.startswith("/") else "/" + text

    # ---------------------------------------------------------------- actions

    def _apply(self) -> None:
        self._collect()
        self.path.setText(self.app.settings.broadcast_path)
        self.ws_path.setText(self.app.settings.broadcast_ws_path)
        self.apply_button.setEnabled(False)
        self.status.setText("Restarting…")

        async def run() -> None:
            try:
                await self.app.restart_broadcast()
            except Exception as exc:  # noqa: BLE001 - surfaced in the dialog
                if alive(self):
                    self.status.setText(str(exc))
                    self.status.setStyleSheet(f"color: {self.theme.critical};")
            finally:
                if alive(self):
                    self.apply_button.setEnabled(True)
                    self._refresh()

        asyncio.ensure_future(run())

    def _copy_url(self) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(self.app.broadcast.url)

    def _refresh(self) -> None:
        state = self.app.broadcast.status()
        if not state["running"]:
            problem = state.get("problem")
            self.status.setText(
                f"Not publishing — {problem}" if problem else "Not publishing."
            )
            self.status.setStyleSheet(f"color: {self.theme.text_secondary};")
            return

        bits = [f"Publishing on {state['url']}"]
        if state["mdns"]:
            bits.append("mDNS")
        if state["beacon"]:
            bits.append("UDP beacon")
        if state["ws_clients"]:
            bits.append(f"{state['ws_clients']} WebSocket client(s)")

        reading = state.get("reading")
        if reading:
            bits.append(
                f"now {reading['temperature']:.2f} {reading['unit']} "
                f"from {reading['channel']}"
            )
            colour = self.theme.good
        else:
            bits.append(state.get("problem") or "no reading")
            colour = self.theme.warning

        self.status.setText(" · ".join(bits))
        self.status.setStyleSheet(f"color: {colour};")

    def done(self, result: int) -> None:
        with contextlib.suppress(Exception):
            self._timer.stop()
        super().done(result)
