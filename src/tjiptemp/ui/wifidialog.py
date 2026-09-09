"""Put a board on WiFi.

The board cannot be asked to scan — TJIP-1 has no message for it — so the SSID
is typed. Credentials are sent over whichever link is already up (normally USB)
and stored on the board, so it reconnects by itself afterwards with no computer
attached.
"""

from __future__ import annotations

import asyncio
import contextlib

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from ..device.device import Device
from .theme import Theme
from .widgets import alive

#: How the board describes its own radio, and what that means to a person.
STATE_TEXT = {
    "connected": "Connected",
    "connecting": "Connecting…",
    "disconnected": "Not connected",
    "failed": "Could not connect — check the password",
    "off": "Radio off — no credentials stored",
}


class WifiDialog(QDialog):
    """Provision one board's WiFi credentials and watch it connect."""

    def __init__(self, device: Device, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.device = device
        self.theme = theme
        self.setWindowTitle(f"WiFi — {device.label}")
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)

        intro = QLabel(
            "The board stores these and reconnects on its own, so it can be used "
            "over the network with nothing plugged in. 2.4 GHz only."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {theme.text_secondary};")
        layout.addWidget(intro)

        form = QFormLayout()
        self.ssid = QLineEdit()
        self.ssid.setPlaceholderText("Network name")
        self.ssid.textChanged.connect(self._validate)
        form.addRow("Network", self.ssid)

        self.psk = QLineEdit()
        self.psk.setEchoMode(QLineEdit.EchoMode.Password)
        self.psk.setPlaceholderText("Leave empty for an open network")
        form.addRow("Password", self.psk)

        self.show_psk = QCheckBox("Show password")
        self.show_psk.toggled.connect(
            lambda on: self.psk.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
            )
        )
        form.addRow("", self.show_psk)
        layout.addLayout(form)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.buttons = QDialogButtonBox()
        self.connect_button = self.buttons.addButton(
            "Connect", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self.buttons.addButton(QDialogButtonBox.StandardButton.Close)
        self.connect_button.clicked.connect(self._connect)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        # The board reports its radio in STATUS at 1 Hz; follow that rather
        # than only reporting the one answer to WIFI_PROVISION, because
        # association can fail seconds after the board accepted the password.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(1000)

        self._prefill()
        self._refresh()
        self._validate()

    def _prefill(self) -> None:
        wifi = (self.device.status or {}).get("wifi") or {}
        if wifi.get("ssid"):
            self.ssid.setText(str(wifi["ssid"]))

    def _validate(self) -> None:
        self.connect_button.setEnabled(bool(self.ssid.text().strip()))

    def _refresh(self) -> None:
        wifi = (self.device.status or {}).get("wifi") or {}
        state = wifi.get("state", "")
        text = STATE_TEXT.get(state, state or "—")
        colour = self.theme.text_secondary
        if state == "connected":
            colour = self.theme.good
            ip = wifi.get("ip")
            rssi = wifi.get("rssi")
            bits = [text]
            if ip:
                bits.append(f"at {ip}")
            if rssi:
                bits.append(f"{rssi} dBm")
            text = " · ".join(bits)
            host = (self.device.config or {}).get("net", {}).get("hostname")
            if host:
                text += f"\nAlso reachable as {host}.local on port 3737."
        elif state == "failed":
            colour = self.theme.critical
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {colour};")

    def _connect(self) -> None:
        ssid = self.ssid.text().strip()
        if not ssid:
            return
        self.connect_button.setEnabled(False)
        self.status.setText("Sending credentials…")
        self.status.setStyleSheet(f"color: {self.theme.text_secondary};")

        async def run() -> None:
            try:
                await self.device.provision_wifi(ssid, self.psk.text())
            except Exception as exc:  # noqa: BLE001 - surfaced in the dialog
                if alive(self):
                    self.status.setText(str(exc))
                    self.status.setStyleSheet(f"color: {self.theme.critical};")
            finally:
                if alive(self):
                    self.connect_button.setEnabled(True)

        asyncio.ensure_future(run())

    def done(self, result: int) -> None:
        with contextlib.suppress(Exception):
            self._timer.stop()
        super().done(result)
