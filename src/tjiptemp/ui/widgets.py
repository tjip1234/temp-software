"""Small reusable widgets: status pills, the channel table, fault banners.

The channel table is the accessible counterpart to the plots. Every trace on a
chart appears here as a row with its colour swatch, its name in text, and its
number -- so nothing on screen depends on being able to distinguish two hues.
"""

from __future__ import annotations

import math
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..protocol.channels import ChannelSpec
from .theme import Theme, state_color

UNIT_SYMBOL = {
    "degC": "°C", "degF": "°F", "K": "K",
    "V": "V", "%RH": "%RH", "ohm": "Ω", "uV": "µV",
}


def unit_symbol(unit: str) -> str:
    return UNIT_SYMBOL.get(unit, unit)


def convert_temperature(value: float, unit: str, target: str) -> tuple[float, str]:
    """Present a Celsius channel in the user's preferred unit."""
    if unit != "degC" or target == "degC" or not math.isfinite(value):
        return value, unit_symbol(unit)
    if target == "degF":
        return value * 9.0 / 5.0 + 32.0, "°F"
    if target == "K":
        return value + 273.15, "K"
    return value, "°C"


class StatusPill(QLabel):
    """A small coloured badge. Always carries a word, never colour alone."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._theme: Theme | None = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = self.font()
        font.setPointSizeF(max(8.0, font.pointSizeF() - 1))
        font.setWeight(QFont.Weight.DemiBold)
        self.setFont(font)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

    def set_state(self, theme: Theme, state: str, text: str | None = None) -> None:
        self._theme = theme
        color = state_color(theme, state)
        self.setText((text or state).upper())
        self.setStyleSheet(
            f"color: {color}; border: 1px solid {color}; border-radius: 8px;"
            f"padding: 2px 8px; background: transparent;"
        )


class Swatch(QWidget):
    """A colour chip for a channel, drawn dashed when the channel is a diagnostic."""

    def __init__(self, color: str, dashed: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.color = color
        self.dashed = dashed
        self.setFixedSize(18, 12)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(self.color), 2.4)
        if self.dashed:
            pen.setDashPattern([2.6, 2.0])
        painter.setPen(pen)
        y = self.height() / 2
        painter.drawLine(1, int(y), self.width() - 1, int(y))
        painter.end()


class FaultBanner(QFrame):
    """Shows decoded sensor faults verbatim.

    A MAX31865 fault register is the difference between "the probe is cold" and
    "the probe fell off", so it is shown in words rather than reduced to a
    red dot.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(10, 7, 10, 7)
        self._layout.setSpacing(3)
        self._label = QLabel()
        self._label.setWordWrap(True)
        self._layout.addWidget(self._label)
        self.hide()

    def update_faults(self, theme: Theme, faults: dict, flags: list[str] | None = None) -> None:
        lines = []
        for chip, entries in (faults or {}).items():
            for entry in entries:
                detail = entry.get("detail") or entry.get("name", "")
                lines.append(f"<b>{chip}</b> — {detail}")
        for flag in flags or []:
            if flag == "uncalibrated":
                lines.append(
                    "<b>Uncalibrated</b> — this board is running factory-nominal "
                    "coefficients. Readings are indicative, not traceable."
                )
            else:
                lines.append(f"<b>{flag}</b>")
        if not lines:
            self.hide()
            return
        color = theme.serious if faults else theme.warning
        self.setStyleSheet(
            f"background: {theme.surface_raised}; border: 1px solid {color};"
            f"border-left: 3px solid {color}; border-radius: 6px;"
        )
        self._label.setStyleSheet(f"color: {theme.text_primary};")
        self._label.setText("<br>".join(lines))
        self.show()


class LinkBar(QWidget):
    """One badge per open link, showing transport and round-trip time."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)
        self._layout.addStretch(1)

    def update_links(self, theme: Theme, links: list[dict]) -> None:
        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        icons = {"usb": "USB", "wifi": "WiFi", "ble": "BLE", "sim": "SIM"}
        for index, link in enumerate(links):
            label = QLabel()
            name = icons.get(link["kind"], link["kind"].upper())
            rtt = link.get("rtt_ms") or 0.0
            detail = f"{name} · {rtt:.1f} ms" if rtt else name
            if link.get("bad_frames"):
                detail += f" · {link['bad_frames']} bad"
            label.setText(detail)
            color = theme.good if link.get("open") else theme.text_muted
            label.setToolTip(link.get("address", ""))
            label.setStyleSheet(
                f"color: {color}; border: 1px solid {color}; border-radius: 8px;"
                f"padding: 1px 7px; font-size: 11px;"
            )
            self._layout.insertWidget(index, label)


class ChannelTable(QTableWidget):
    """Live values for every channel: swatch, name, value, unit, age.

    Doubles as the plot's table view -- the accessible alternative that makes the
    charts legible to someone who cannot separate two of the trace colours.
    """

    COLUMNS = ("Plot", "Channel", "Value", "Unit", "Age")
    #: (channel id, plotted) whenever the user ticks or unticks a row.
    visibility_changed = Signal(int, bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(0, len(self.COLUMNS), parent)
        self.setHorizontalHeaderLabels(self.COLUMNS)
        self.verticalHeader().setVisible(False)
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.setShowGrid(False)
        header = self.horizontalHeader()
        header.setStretchLastSection(False)
        self.setColumnWidth(0, 62)
        self.setColumnWidth(2, 100)
        self.setColumnWidth(3, 56)
        self.setColumnWidth(4, 66)
        header.setSectionResizeMode(1, header.ResizeMode.Stretch)

        self._specs: list[ChannelSpec] = []
        self._rows: dict[int, int] = {}
        self._boxes: dict[int, QCheckBox] = {}
        self._theme: Theme | None = None
        self._unit_preference = "degC"
        self._mono = QFont("monospace")
        self._mono.setStyleHint(QFont.StyleHint.Monospace)

    def set_theme(self, theme: Theme) -> None:
        self._theme = theme
        self.rebuild(self._specs)

    def set_unit_preference(self, unit: str) -> None:
        self._unit_preference = unit

    def rebuild(self, specs: list[ChannelSpec]) -> None:
        self._specs = list(specs)
        self._rows.clear()
        self._boxes.clear()
        self.setRowCount(len(self._specs))
        theme = self._theme
        for row, spec in enumerate(self._specs):
            self._rows[spec.id] = row
            color = spec.color_dark if (theme and theme.dark) else spec.color

            # Tick box plus colour swatch, so the row says both "is this plotted?"
            # and "which line is it?" without the user consulting the legend.
            cell = QWidget()
            layout = QHBoxLayout(cell)
            layout.setContentsMargins(6, 0, 2, 0)
            layout.setSpacing(4)
            box = QCheckBox()
            box.setToolTip(f"Plot {spec.name}")
            box.toggled.connect(
                lambda state, cid=spec.id: self.visibility_changed.emit(cid, state)
            )
            self._boxes[spec.id] = box
            layout.addWidget(box)
            layout.addWidget(Swatch(color, spec.dash == "dashed"))
            layout.addStretch(1)
            self.setCellWidget(row, 0, cell)

            name = QTableWidgetItem(spec.name)
            name.setToolTip(f"{spec.key} · channel {spec.id} · {spec.kind}")
            if spec.secondary and theme:
                name.setForeground(QColor(theme.text_secondary))
            self.setItem(row, 1, name)

            value = QTableWidgetItem("—")
            value.setFont(self._mono)
            value.setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            self.setItem(row, 2, value)
            self.setItem(row, 3, QTableWidgetItem(unit_symbol(spec.unit)))
            age = QTableWidgetItem("")
            age.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.setItem(row, 4, age)
            self.setRowHeight(row, 24)

    def update_values(self, values: dict[int, tuple[float, float]]) -> None:
        """``values`` maps channel id to (value, age in seconds)."""
        theme = self._theme
        for spec in self._specs:
            row = self._rows.get(spec.id)
            if row is None:
                continue
            value, age = values.get(spec.id, (float("nan"), float("inf")))
            cell = self.item(row, 2)
            unit_cell = self.item(row, 3)
            age_cell = self.item(row, 4)

            if not math.isfinite(value):
                cell.setText("fault")
                if theme:
                    cell.setForeground(QColor(theme.critical))
                age_cell.setText("")
                continue

            shown, symbol = convert_temperature(value, spec.unit, self._unit_preference)
            cell.setText(f"{shown:,.{spec.decimals}f}")
            unit_cell.setText(symbol)
            if theme:
                # A stale reading is dimmed rather than hidden: the last good value
                # with its age is more useful than a dash.
                stale = age > 3.0
                cell.setForeground(QColor(theme.text_muted if stale else theme.text_primary))
            age_cell.setText("" if age < 3.0 else f"{age:.0f}s ago")

    def set_checked(self, channel_ids: set[int]) -> None:
        """Reflect the plot's current selection without re-emitting signals."""
        for cid, box in self._boxes.items():
            box.blockSignals(True)
            box.setChecked(cid in channel_ids)
            box.blockSignals(False)

class MetricLabel(QWidget):
    """A caption above a value. Used for the small status readouts."""

    def __init__(self, caption: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._caption = QLabel(caption)
        self._value = QLabel("—")
        font = self._value.font()
        font.setPointSizeF(font.pointSizeF() + 1)
        font.setWeight(QFont.Weight.DemiBold)
        self._value.setFont(font)
        caption_font = self._caption.font()
        caption_font.setPointSizeF(max(8.0, caption_font.pointSizeF() - 2))
        self._caption.setFont(caption_font)
        layout.addWidget(self._caption)
        layout.addWidget(self._value)

    def set_theme(self, theme: Theme) -> None:
        self._caption.setStyleSheet(f"color: {theme.text_muted};")
        self._value.setStyleSheet(f"color: {theme.text_primary};")

    def set_value(self, text: str, tooltip: str = "") -> None:
        self._value.setText(text)
        if tooltip:
            self.setToolTip(tooltip)


def alive(widget) -> bool:
    """Whether a widget's underlying C++ object still exists.

    A coroutine started from a dialog keeps running after the dialog is closed:
    the ``await`` returns, and the continuation then writes to labels whose C++
    half Qt has already destroyed, raising ``RuntimeError: Internal C++ object
    already deleted`` from somewhere with no useful traceback. Every UI callback
    that touches widgets after an ``await`` checks this first.
    """
    try:
        from shiboken6 import isValid
    except ImportError:      # pragma: no cover - PySide6 always ships shiboken
        return True
    try:
        return bool(isValid(widget))
    except (RuntimeError, TypeError):
        return False


def message_later(kind: str, parent, title: str, text: str) -> None:
    """Show a modal message box *after* the current coroutine yields.

    Opening one directly from inside an ``async def`` deadlocks the two event
    loops against each other: ``QMessageBox.exec`` spins a nested Qt loop,
    qasync pumps asyncio from inside it, and asyncio then tries to resume some
    other task while the calling task is still marked as running —

        RuntimeError: Cannot enter into task <...> while another task
        <...> is being executed

    A zero-delay timer defers the dialog to a later turn of the Qt loop, by
    which time the coroutine has finished and there is no task to re-enter.
    Every modal shown from a coroutine must go through here.
    """
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QMessageBox

    show = {
        "critical": QMessageBox.critical,
        "warning": QMessageBox.warning,
        "information": QMessageBox.information,
    }[kind]
    QTimer.singleShot(0, lambda: show(parent, title, text))


def humanise_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m {seconds % 60:.0f}s"
    return f"{seconds / 3600:.0f}h {(seconds % 3600) / 60:.0f}m"


def format_utc(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
