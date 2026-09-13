"""Calibration: a tab on each board, for the probes you actually measure with.

Only the PT1000, the Type K and the external NTCs are offered -- the other
channels are board diagnostics nobody calibrates against a reference. The panel
is one column: pick a channel, type what the reference reads, capture, repeat.
The fit's worst error is one line and a small plot; the coefficients and any fit
notes are in that line's tooltip rather than on the page.

Two things are deliberate and worth keeping:

* Channels are fitted against their *raw* measurement -- the PT1000's
  resistance, the thermocouple's microvolts -- not an already-linearised
  temperature. Fitting a correction on top of a conversion is how small errors
  become mysterious ones.
* Nothing is written until "Write to board", and the new fits are laid over the
  board's calibration as it is at that moment. The tab outlives any one visit,
  so a copy taken when it was built would overwrite channels someone else has
  recalibrated since.
"""

from __future__ import annotations

import asyncio
import math

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..calibration import fitting
from ..calibration.models import CalibrationSet, validate
from ..device.device import Device
from ..protocol.channels import Ch, ChannelSpec
from .theme import Theme
from .widgets import message_later, unit_symbol

#: How long to average when capturing a reference point.
SETTLE_SECONDS = 10.0

#: The channels offered, in this order: the probes, not the board diagnostics.
CALIBRATED: tuple[int, ...] = (
    int(Ch.PT1000),
    int(Ch.TYPEK),
    int(Ch.NTC_EXT1),
    int(Ch.NTC_EXT2),
    int(Ch.NTC_EXT3),
    int(Ch.NTC_EXT4),
)

#: Which raw channel each calibratable channel is fitted against, and in what unit.
RAW_SOURCE: dict[int, tuple[int, str]] = {
    int(Ch.PT1000): (int(Ch.PT1000_R), "ohm"),
    int(Ch.TYPEK): (int(Ch.TYPEK_UV), "uV"),
}

#: Shown until the first point is captured: how many points the model wants.
POINTS_HINT = {
    "cvd": "1 point fits R0; 4 or more also fit the curve.",
    "steinhart": "Needs 3 points; 4 or more to check the fit.",
    "nist_typek": "1 point fits the offset; 3 over 100 °C also fit the gain.",
}


def _mono() -> QFont:
    font = QFont("monospace")
    font.setStyleHint(QFont.StyleHint.Monospace)
    return font


class PointTable(QTableWidget):
    """Captured (raw, reference) pairs for one channel."""

    COLUMNS = ("Use", "Raw", "Reference", "Error")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(0, len(self.COLUMNS), parent)
        self.setHorizontalHeaderLabels(self.COLUMNS)
        self.verticalHeader().setVisible(False)
        self.setAlternatingRowColors(True)
        header = self.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.points: list[dict] = []

    def bind(self, points: list[dict]) -> None:
        """Show another channel's points; edits go straight into that list."""
        self.points = points
        self._rebuild()

    def add_point(self, raw: float, std: float, reference: float) -> None:
        self.points.append({"raw": raw, "std": std, "reference": reference, "use": True})
        self._rebuild()

    def remove_selected(self) -> None:
        row = self.currentRow()
        if 0 <= row < len(self.points):
            del self.points[row]
            self._rebuild()

    def clear_points(self) -> None:
        self.points.clear()
        self._rebuild()

    def active(self) -> list[dict]:
        return [p for p in self.points if p.get("use", True)]

    def set_residuals(self, residuals: list[float] | None) -> None:
        for point in self.points:
            point["residual"] = None
        for index, point in enumerate(self.active()):
            if residuals and index < len(residuals):
                point["residual"] = residuals[index]
        self._rebuild()

    def _rebuild(self) -> None:
        self.blockSignals(True)
        self.setRowCount(len(self.points))
        mono = _mono()
        for row, point in enumerate(self.points):
            box = QCheckBox()
            box.setChecked(point.get("use", True))
            box.toggled.connect(lambda state, p=point: p.__setitem__("use", state))
            holder = QWidget()
            layout = QHBoxLayout(holder)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(box)
            self.setCellWidget(row, 0, holder)

            raw = f"{point['raw']:.5g}"
            if point["std"]:
                raw += f" ±{point['std']:.2g}"
            for column, text in (
                (1, raw),
                (2, f"{point['reference']:.3f}"),
                (3, "" if point.get("residual") is None
                    else f"{point['residual'] * 1000:+.1f} mK"),
            ):
                item = QTableWidgetItem(text)
                item.setFont(mono)
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
                self.setItem(row, column, item)
        self.blockSignals(False)


class CalibrationPanel(QWidget):
    """Capture reference points for a probe, fit them, and write to the board."""

    def __init__(self, device: Device, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.device = device
        self.theme = theme
        #: Captured points per channel id, kept while you switch between channels.
        self._points: dict[int, list[dict]] = {}
        #: Fits waiting to be written, by channel key.
        self._fits: dict[str, fitting.FitResult] = {}
        self._problems: list[str] = []
        self._capture_task: asyncio.Task | None = None
        self._writing = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        form = QFormLayout()
        form.setSpacing(6)
        self.channel_box = QComboBox()
        self.channel_box.currentIndexChanged.connect(self._on_channel_changed)
        form.addRow("Channel", self.channel_box)

        self.live_label = QLabel("—")
        self.live_label.setFont(_mono())
        form.addRow("Board reads", self.live_label)

        self.settle_input = QDoubleSpinBox()
        self.settle_input.setRange(0.5, 300.0)
        self.settle_input.setValue(SETTLE_SECONDS)
        self.settle_input.setSuffix(" s")
        self.settle_input.setToolTip("How long to average the reading for each point.")
        form.addRow("Average", self.settle_input)

        entry = QHBoxLayout()
        entry.setSpacing(6)
        self.reference_input = QDoubleSpinBox()
        self.reference_input.setRange(-300.0, 2000.0)
        self.reference_input.setDecimals(3)
        self.reference_input.setSuffix(" °C")
        entry.addWidget(self.reference_input, 1)
        self.capture_button = QPushButton("Capture")
        self.capture_button.setProperty("primary", True)
        self.capture_button.clicked.connect(self._on_capture)
        entry.addWidget(self.capture_button)
        form.addRow("Reference", entry)
        layout.addLayout(form)

        self.points = PointTable()
        self.points.setMinimumHeight(110)
        layout.addWidget(self.points, 1)

        self.residual_plot = pg.PlotWidget()
        self.residual_plot.setLabel("left", "Error [mK]")
        self.residual_plot.showGrid(x=True, y=True, alpha=0.25)
        self.residual_plot.setMinimumHeight(110)
        self.residual_plot.setMaximumHeight(170)
        layout.addWidget(self.residual_plot)

        self.result_label = QLabel("")
        self.result_label.setWordWrap(True)
        layout.addWidget(self.result_label)

        self.problems = QLabel("")
        self.problems.setWordWrap(True)
        self.problems.setStyleSheet(f"color: {theme.serious};")
        self.problems.setVisible(False)
        layout.addWidget(self.problems)

        buttons = QHBoxLayout()
        remove = QPushButton("Remove")
        remove.clicked.connect(lambda: (self.points.remove_selected(), self._refit()))
        clear = QPushButton("Clear")
        clear.clicked.connect(lambda: (self.points.clear_points(), self._refit()))
        buttons.addWidget(remove)
        buttons.addWidget(clear)
        buttons.addStretch(1)
        self.apply_button = QPushButton("Write to board")
        self.apply_button.setProperty("primary", True)
        self.apply_button.clicked.connect(self._on_apply)
        buttons.addWidget(self.apply_button)
        layout.addLayout(buttons)

        self.refresh_channels()
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(400)
        self._live_timer.timeout.connect(self._update_live)
        self._live_timer.start()
        self._update_live()

    # -------------------------------------------------------------- channels

    def refresh_channels(self) -> None:
        """Offer the calibratable probes this board actually has."""
        specs = [
            self.device.channels[cid] for cid in CALIBRATED
            if cid in self.device.channels and self.device.channels[cid].cal_model
        ]
        offered = [self.channel_box.itemData(i) for i in range(self.channel_box.count())]
        if [spec.id for spec in specs] == offered:
            return
        current = self.channel_box.currentData()
        self.channel_box.blockSignals(True)
        self.channel_box.clear()
        for spec in specs:
            self.channel_box.addItem(spec.name, spec.id)
        self.channel_box.setCurrentIndex(max(self.channel_box.findData(current), 0))
        self.channel_box.blockSignals(False)
        self._on_channel_changed()

    @property
    def current_spec(self) -> ChannelSpec | None:
        cid = self.channel_box.currentData()
        return self.device.channels.get(cid) if cid is not None else None

    def _raw_channel(self) -> tuple[int, str]:
        """Which channel and unit this calibration actually fits against."""
        spec = self.current_spec
        if spec is None:
            return -1, ""
        return RAW_SOURCE.get(spec.id, (spec.id, spec.unit))

    def _on_channel_changed(self, *_args) -> None:
        spec = self.current_spec
        if spec is None:
            self.points.bind([])
            self._say("This board has no probes to calibrate.", self.theme.text_secondary)
            return
        self.points.bind(self._points.setdefault(spec.id, []))
        self.reference_input.setSuffix(f" {unit_symbol(spec.unit)}")
        self._refit()

    # ---------------------------------------------------------------- capture

    def _capturing(self) -> bool:
        return self._capture_task is not None and not self._capture_task.done()

    def _update_live(self) -> None:
        online = self.device.is_online
        spec = self.current_spec
        self.capture_button.setEnabled(online and spec is not None and not self._capturing())
        self.apply_button.setEnabled(
            online and bool(self._fits) and not self._problems and not self._writing
        )
        if spec is None or not self.isVisible():
            return
        latest = self.device.latest_values()
        cooked = latest.get(spec.id, float("nan"))
        text = f"{cooked:.4f} {unit_symbol(spec.unit)}" if math.isfinite(cooked) else "fault"
        raw_id, raw_unit = self._raw_channel()
        raw = latest.get(raw_id, float("nan"))
        if raw_id != spec.id and math.isfinite(raw):
            text += f"  ·  {raw:.5g} {unit_symbol(raw_unit)}"
        self.live_label.setText(text)

    def _on_capture(self) -> None:
        if self._capturing():
            return
        self._capture_task = asyncio.ensure_future(self._capture())

    async def _capture(self) -> None:
        spec = self.current_spec
        if spec is None:
            return
        points = self.points.points
        raw_id, _raw_unit = self._raw_channel()
        seconds = float(self.settle_input.value())
        reference = float(self.reference_input.value())

        self.capture_button.setEnabled(False)
        try:
            step = 0.25
            elapsed = 0.0
            while elapsed < seconds:
                await asyncio.sleep(step)
                elapsed += step
                self.capture_button.setText(f"Capturing… {seconds - elapsed:.0f}s")

            _t, values = self.device.series(raw_id, seconds)
            good = values[np.isfinite(values)]
            if good.size == 0:
                message_later("warning",
                    self, "No usable samples",
                    f"{spec.name} gave no valid readings while capturing. Check the "
                    f"probe is connected."
                )
                return
            points.append({"raw": float(np.mean(good)), "std": float(np.std(good)),
                           "reference": reference, "use": True})
            if self.current_spec is spec:
                self.points.bind(points)
                self._refit()
        finally:
            self.capture_button.setText("Capture")
            self._update_live()

    # -------------------------------------------------------------------- fit

    def _say(self, text: str, colour: str, tooltip: str = "") -> None:
        self.result_label.setText(text)
        self.result_label.setStyleSheet(f"color: {colour};")
        self.result_label.setToolTip(tooltip)

    def _clear_plot(self) -> None:
        self.residual_plot.clear()
        self.residual_plot.addLine(y=0, pen=pg.mkPen(self.theme.display_dim, width=1))

    def _refit(self) -> None:
        spec = self.current_spec
        if spec is None:
            return
        points = self.points.active()
        self._clear_plot()
        if not points:
            self._fits.pop(spec.key, None)
            self.points.set_residuals(None)
            self._say(POINTS_HINT.get(spec.cal_model or "", ""), self.theme.text_secondary)
            self._validate()
            return

        raw = np.array([p["raw"] for p in points])
        reference = np.array([p["reference"] for p in points])
        try:
            result = self._run_fit(spec, raw, reference)
        except (fitting.FitError, ValueError, np.linalg.LinAlgError) as exc:
            self._fits.pop(spec.key, None)
            self.points.set_residuals(None)
            self._say(str(exc), self.theme.serious)
            self._validate()
            return

        self._fits[spec.key] = result
        self.points.set_residuals([float(r) for r in result.residuals])

        n = len(points)
        colour = self.theme.text_secondary
        if result.exactly_determined:
            text = (f"Exact fit through {n} point{'s' if n > 1 else ''} — "
                    f"add one more to see the error.")
        else:
            text = f"Worst error {result.max_abs * 1000:.1f} mK over {n} points."
            if result.max_abs > 0.5:
                colour = self.theme.warning
        details = [f"{result.model}:"]
        details += [f"  {key} = {value!r}" if isinstance(value, list)
                    else f"  {key} = {value:.9g}" for key, value in result.params.items()]
        if result.message:
            details += ["", result.message]
        self._say(text, colour, "\n".join(details))
        self._plot_residuals(result)
        self._validate()

    def _run_fit(self, spec: ChannelSpec, raw, reference) -> fitting.FitResult:
        model = spec.cal_model
        if model == "cvd":
            return fitting.fit_cvd(raw, reference)
        if model == "steinhart":
            # Below three points Steinhart-Hart is not solvable; Beta is, and saying
            # so beats an error message that just refuses.
            if len(raw) < 3:
                return fitting.fit_beta(raw, reference)
            return fitting.fit_steinhart(raw, reference)
        if model == "nist_typek":
            cj_spec = self.device.channels.get(int(Ch.TYPEK_CJ))
            cj_cal = self.device.calibration.get(cj_spec.key) if cj_spec else None
            cj_offset = (cj_cal.params.get("offset", 0.0)
                         if cj_cal is not None and cj_cal.model == "linear" else 0.0)
            _t, cj_series = self.device.series(int(Ch.TYPEK_CJ), 60.0)
            cj_now = float(np.nanmean(cj_series)) if cj_series.size else 25.0
            cj = np.full(len(raw), cj_now)
            return fitting.fit_typek(raw, cj, reference, cj_offset_c=cj_offset)
        return fitting.fit_linear(raw, reference)

    def _plot_residuals(self, result: fitting.FitResult) -> None:
        residuals_mk = result.residuals * 1000.0
        scatter = pg.ScatterPlotItem(
            x=result.reference, y=residuals_mk, size=9,
            brush=pg.mkBrush(self.theme.display_text), pen=pg.mkPen(self.theme.display, width=2),
        )
        self.residual_plot.addItem(scatter)
        if residuals_mk.size:
            span = max(float(np.max(np.abs(residuals_mk))) * 1.6, 1.0)
            self.residual_plot.setYRange(-span, span)

    # ------------------------------------------------------------------ write

    def _merged(self) -> CalibrationSet:
        """The board's calibration as it is now, with the pending fits laid over it."""
        merged = CalibrationSet.from_json(self.device.calibration.to_json())
        for key, result in self._fits.items():
            merged.set(key, result.to_channel_cal())
        return merged

    def _validate(self) -> bool:
        self._problems = validate(self._merged()) if self._fits else []
        self.problems.setText("\n".join(self._problems))
        self.problems.setVisible(bool(self._problems))
        self._update_live()
        return not self._problems

    def _pending_names(self) -> list[str]:
        names = {spec.key: spec.name for spec in self.device.channels.values()}
        return [names.get(key, key) for key in sorted(self._fits)]

    def _on_apply(self) -> None:
        if not self._fits or not self._validate():
            return
        answer = QMessageBox.question(
            self, "Write calibration?",
            f"Write {', '.join(self._pending_names())} to the board? This replaces "
            f"what it has stored for {'that channel' if len(self._fits) == 1 else 'those'}.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            asyncio.ensure_future(self._write())

    async def _write(self) -> None:
        written = set(self._fits)
        self._writing = True
        self.apply_button.setText("Writing…")
        self._update_live()
        try:
            await self.device.write_calibration(self._merged().touched())
        except Exception as exc:
            message_later("critical",
                self, "Calibration not written",
                f"The board did not accept it:\n\n{exc}\n\nNothing was changed."
            )
            return
        finally:
            self._writing = False
            self.apply_button.setText("Write to board")
        for key in written:
            self._fits.pop(key, None)
        for cid in list(self._points):
            spec = self.device.channels.get(cid)
            if spec is not None and spec.key in written:
                self._points[cid].clear()
        self._on_channel_changed()
        self._say(f"Written to the board (revision {self.device.calibration.rev}).",
                  self.theme.text_secondary)

    def close_panel(self) -> None:
        """Stop the live timer and any capture still in flight."""
        self._live_timer.stop()
        if self._capturing():
            self._capture_task.cancel()
