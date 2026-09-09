"""The live strip charts.

pyqtgraph rather than matplotlib for this view: it renders straight through Qt's
scene graph and handles a million points at interactive frame rates, which is what
100 Hz across several boards actually needs. matplotlib does the report figures,
where quality matters more than latency.

Two rules from the project's chart conventions shape the layout:

* **One unit per axis.** Temperatures, volts, humidity and raw resistance each get
  their own stacked plot with a shared time axis. A dual-axis chart makes any two
  series look correlated purely by the arbitrary choice of scaling, which for
  measurement data is worse than useless.
* **Never lie when zoomed out.** Long spans are min/max decimated rather than
  stride-sampled, so a one-sample spike stays visible at any zoom level instead of
  disappearing because it fell between two samples the renderer happened to keep.
"""

from __future__ import annotations

import contextlib
import time

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ..core.ringbuffer import DecimatingView
from ..device.device import Device
from ..protocol.channels import ChannelSpec
from .theme import Theme
from .widgets import unit_symbol

#: Units that may legitimately share a y-axis.
AXIS_GROUP = {
    "degC": "Temperature", "degF": "Temperature", "K": "Temperature",
    "V": "Voltage", "%RH": "Humidity", "ohm": "Resistance", "uV": "Thermo-voltage",
}

WINDOWS = [
    ("30 s", 30.0), ("1 min", 60.0), ("5 min", 300.0), ("15 min", 900.0),
    ("1 hour", 3600.0), ("All", 0.0),
]

#: Points per trace handed to the GPU. Beyond this the eye gains nothing and the
#: frame rate suffers.
TARGET_POINTS = 1500


class TimeAxis(pg.AxisItem):
    """Wall-clock tick labels, at a resolution that suits the visible span."""

    def tickStrings(self, values, scale, spacing):  # noqa: N802 - pyqtgraph naming
        if spacing >= 86400:
            fmt = "%d %b"
        elif spacing >= 60:
            fmt = "%H:%M"
        elif spacing >= 1:
            fmt = "%H:%M:%S"
        else:
            return [f"{time.strftime('%M:%S', time.localtime(v))}.{int((v % 1) * 10)}"
                    for v in values]
        return [time.strftime(fmt, time.localtime(v)) for v in values]


class GroupPlot(pg.PlotWidget):
    """One plot for one unit group."""

    def __init__(self, group: str, unit: str, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent, axisItems={"bottom": TimeAxis(orientation="bottom")})
        self.group = group
        self.unit = unit
        self.theme = theme
        self.curves: dict[int, pg.PlotDataItem] = {}

        self.setMinimumHeight(150)
        self.showGrid(x=True, y=True, alpha=0.25)
        self.setLabel("left", group, units=None)
        self.getAxis("left").setLabel(f"{group} [{unit_symbol(unit)}]")
        self.setMouseEnabled(x=True, y=True)
        self.setClipToView(True)
        self.setDownsampling(auto=False)   # we decimate ourselves, truthfully

        legend = self.addLegend(offset=(8, 6), labelTextColor=theme.text_secondary)
        legend.setBrush(pg.mkBrush(QColor(theme.surface_raised)))
        legend.setPen(pg.mkPen(QColor(theme.border)))
        self._legend = legend

        self._crosshair_v = pg.InfiniteLine(angle=90, movable=False,
                                            pen=pg.mkPen(theme.text_muted, width=1,
                                                         style=Qt.PenStyle.DashLine))
        self.addItem(self._crosshair_v, ignoreBounds=True)
        self._crosshair_v.hide()

        self._readout = pg.TextItem(anchor=(0, 1), color=theme.text_primary)
        font = QFont("monospace")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSizeF(9)
        self._readout.setFont(font)
        self.addItem(self._readout, ignoreBounds=True)
        self._readout.hide()

        self.scene().sigMouseMoved.connect(self._on_mouse)
        self._specs: dict[int, ChannelSpec] = {}
        self._data: dict[int, tuple[np.ndarray, np.ndarray]] = {}

    def set_legend_columns(self, columns: int) -> None:
        """Wrap the legend so a long series list does not cover the data."""
        with contextlib.suppress(AttributeError, TypeError):
            self._legend.setColumnCount(max(1, columns))

    def add_channel(self, spec: ChannelSpec) -> None:
        color = spec.color_dark if self.theme.dark else spec.color
        pen = pg.mkPen(
            color=color,
            width=1.6 if spec.dash == "solid" else 1.3,
            style=Qt.PenStyle.SolidLine if spec.dash == "solid" else Qt.PenStyle.DashLine,
        )
        curve = self.plot(pen=pen, name=spec.name, connect="finite", autoDownsample=False)
        curve.setSkipFiniteCheck(False)  # NaN must break the line, showing the gap
        self.curves[spec.id] = curve
        self._specs[spec.id] = spec

    def remove_channel(self, channel_id: int) -> None:
        curve = self.curves.pop(channel_id, None)
        if curve is not None:
            self.removeItem(curve)
            self._legend.removeItem(curve)
        self._specs.pop(channel_id, None)
        self._data.pop(channel_id, None)

    def set_data(self, channel_id: int, t: np.ndarray, v: np.ndarray) -> None:
        curve = self.curves.get(channel_id)
        if curve is None:
            return
        self._data[channel_id] = (t, v)
        curve.setData(t, v)

    def _on_mouse(self, position) -> None:
        if not self.sceneBoundingRect().contains(position):
            self._crosshair_v.hide()
            self._readout.hide()
            return
        point = self.getPlotItem().vb.mapSceneToView(position)
        x = float(point.x())
        self._crosshair_v.setPos(x)
        self._crosshair_v.show()

        lines = [time.strftime("%H:%M:%S", time.localtime(x))]
        for cid, (t, v) in self._data.items():
            if t.size == 0:
                continue
            index = int(np.searchsorted(t, x))
            index = min(max(index, 0), t.size - 1)
            value = v[index]
            spec = self._specs[cid]
            text = "fault" if not np.isfinite(value) else f"{value:,.{spec.decimals}f}"
            lines.append(f"{spec.name}: {text}")
        self._readout.setText("\n".join(lines))
        view = self.getPlotItem().vb.viewRange()
        self._readout.setPos(x, view[1][1])
        self._readout.show()


class LiveView(QWidget):
    """Stacked strip charts plus the channel selector."""

    channel_toggled = Signal(int, bool)

    def __init__(self, device: Device, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.device = device
        self.theme = theme
        self.window_s = 300.0
        self.paused = False
        self._plots: dict[str, GroupPlot] = {}
        self._visible: set[int] = set()

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        controls.addWidget(QLabel("Window"))
        self.window_box = QComboBox()
        for label, seconds in WINDOWS:
            self.window_box.addItem(label, seconds)
        self.window_box.setCurrentIndex(2)
        self.window_box.currentIndexChanged.connect(self._on_window_changed)
        controls.addWidget(self.window_box)

        self.pause_box = QCheckBox("Freeze")
        self.pause_box.setToolTip(
            "Stop updating the view. Recording and acquisition continue regardless."
        )
        self.pause_box.toggled.connect(self._on_pause)
        controls.addWidget(self.pause_box)

        self.autoscale_box = QCheckBox("Auto-scale Y")
        self.autoscale_box.setChecked(True)
        controls.addWidget(self.autoscale_box)

        controls.addStretch(1)
        self.status_label = QLabel("")
        self.status_label.setStyleSheet(f"color: {theme.text_muted};")
        controls.addWidget(self.status_label)
        root.addLayout(controls)

        self._plot_area = QVBoxLayout()
        self._plot_area.setSpacing(4)
        root.addLayout(self._plot_area, 1)

        self._last_generation = -1

        self._timer = QTimer(self)
        self._timer.setInterval(100)  # 10 fps: plenty for a thermometer
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

    # ----------------------------------------------------------------- layout

    def set_channels(self, channel_ids: list[int]) -> None:
        """Choose which channels are plotted, grouping them by unit."""
        self._visible = set(channel_ids)
        wanted_groups: dict[str, list[ChannelSpec]] = {}
        for cid in channel_ids:
            spec = self.device.channels.get(cid)
            if spec is None:
                continue
            group = AXIS_GROUP.get(spec.unit, spec.unit or "Other")
            wanted_groups.setdefault(group, []).append(spec)

        for group in list(self._plots):
            if group not in wanted_groups:
                plot = self._plots.pop(group)
                self._plot_area.removeWidget(plot)
                plot.deleteLater()

        first_plot = None
        for group, specs in wanted_groups.items():
            plot = self._plots.get(group)
            if plot is None:
                plot = GroupPlot(group, specs[0].unit, self.theme, self)
                self._plots[group] = plot
            else:
                self._plot_area.removeWidget(plot)
            # A panel carrying nine traces needs more room than one carrying a
            # single voltage, so stretch follows series count rather than being
            # split evenly.
            self._plot_area.addWidget(plot, max(2, len(specs)))
            for cid in list(plot.curves):
                if cid not in self._visible:
                    plot.remove_channel(cid)
            for spec in specs:
                if spec.id not in plot.curves:
                    plot.add_channel(spec)
            plot.set_legend_columns(1 if len(specs) <= 4 else 2 if len(specs) <= 8 else 3)
            # Time is the same for every panel, so link them: pan one, pan all.
            if first_plot is None:
                first_plot = plot
            else:
                plot.setXLink(first_plot)

        self.refresh(force=True)

    def visible_channels(self) -> set[int]:
        return set(self._visible)

    # ---------------------------------------------------------------- updates

    def _on_window_changed(self, index: int) -> None:
        self.window_s = float(self.window_box.itemData(index))
        self.refresh(force=True)

    def _on_pause(self, paused: bool) -> None:
        self.paused = paused
        if not paused:
            self.refresh(force=True)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Catch up the moment this board's tab comes back on screen."""
        super().showEvent(event)
        self.refresh(force=True)

    def refresh(self, force: bool = False) -> None:
        if self.paused and not force:
            return
        if not self._plots:
            return
        # Every connected board owns a LiveView with its own timer, but only one
        # tab is on screen. Decimating and redrawing the other boards' traces
        # produces pixels nobody can see, and with several boards that was the
        # largest single cost in the UI. showEvent brings a tab up to date when
        # it is selected, so nothing is stale by the time it is looked at.
        if not force and not self.isVisible():
            return

        gen = self.device.live.generation
        if gen == self._last_generation and not force:
            return
        self._last_generation = gen

        span = self.window_s if self.window_s > 0 else None
        if span is None:
            t_all, v_all = self.device.live.view()
        else:
            t_all, v_all = self.device.live.window(span)

        total_points = 0
        for plot in self._plots.values():
            plot.blockSignals(True)
            for cid in plot.curves:
                index = self.device.channel_index(cid)
                if index is None:
                    continue
                if t_all.size == 0:
                    plot.set_data(cid, t_all, np.zeros(0))
                    continue
                column = v_all[:, index]
                t_dec, v_dec = DecimatingView.decimate(t_all, column, TARGET_POINTS)
                plot.set_data(cid, t_dec, v_dec)
                total_points += t_dec.size
            plot.blockSignals(False)
            if self.autoscale_box.isChecked():
                plot.enableAutoRange(axis="y")

        buffered = self.device.live.size
        health = self.device.aggregator.health()
        note = f"{buffered:,} samples buffered · {total_points:,} plotted"
        if health["lost"]:
            note += f" · {health['lost']:,} rows lost"
        elif health["backfilled"]:
            note += f" · {health['backfilled']:,} backfilled"
        self.status_label.setText(note)

    def set_theme(self, theme: Theme) -> None:
        self.theme = theme
        channels = list(self._visible)
        for group in list(self._plots):
            plot = self._plots.pop(group)
            self._plot_area.removeWidget(plot)
            plot.deleteLater()
        self.status_label.setStyleSheet(f"color: {theme.text_muted};")
        self.set_channels(channels)

    def stop(self) -> None:
        self._timer.stop()
