"""The calibration wizard.

Calibration is the feature that turns this from a datalogger into an instrument,
so the dialog is built around one idea: **show the user how good the fit actually
is, and refuse to flatter it.**

Concretely:

* Reference points are captured from the live stream, averaged over a settling
  window, with the standard deviation shown. A point taken while the probe was
  still moving is visibly worse than one taken at equilibrium.
* Channels are calibrated against their *raw* physical measurement -- the PT1000's
  resistance, the thermocouple's microvolts -- not against an already-linearised
  temperature. Fitting a correction on top of a conversion is how small errors
  become mysterious ones.
* Residuals are plotted and stated in millikelvin. When the points exactly
  determine the coefficients, the dialog says the residuals are zero by
  construction and mean nothing.
* Nothing is written to the board until the user presses Apply, and the whole set
  is validated first.
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
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
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

#: Which raw channel each calibratable channel is fitted against, and in what unit.
RAW_SOURCE: dict[int, tuple[int, str]] = {
    int(Ch.PT1000): (int(Ch.PT1000_R), "ohm"),
    int(Ch.TYPEK): (int(Ch.TYPEK_UV), "uV"),
}

#: The one thing you need before capturing: how many points this model wants.
#: The reasoning behind each lives in HELP, one click away — it is worth
#: reading once and then never again, which is exactly what a disclosure is for.
SHORT_HELP = {
    "cvd": "Fitted against measured resistance. 1 point fits R0, 2–3 add the slope, 4+ add curvature.",
    "steinhart": "Needs 3 points minimum. 3 fit exactly, so residuals mean nothing; 4+ give a real error estimate.",
    "nist_typek": "Fitted against measured microvolts. Calibrate the cold junction separately.",
    "linear": "1 point fits the offset, 2 or more also fit the gain.",
}

HELP = {
    "cvd": (
        "The PT1000 is fitted against its measured <b>resistance</b>, using the "
        "Callendar–Van Dusen equation. One point fits R0 (the ice-point "
        "calibration, and usually the largest single error). Two or three also fit "
        "A, the slope. Four or more also fit B, the curvature."
    ),
    "steinhart": (
        "The NTC is fitted with Steinhart–Hart, which needs at least three points. "
        "Three gives an exact fit — accurate at those points, unverified between "
        "them. Four or more gives a real error estimate."
    ),
    "nist_typek": (
        "The thermocouple is fitted against its measured <b>microvolts</b>. Note "
        "that a cold-junction offset is indistinguishable from a voltage offset "
        "from hot-junction points alone: the Seebeck coefficient barely changes "
        "across any plausible cold-junction range. Calibrate the cold junction "
        "separately (channel “Type K cold junction”) if you need them separated."
    ),
    "linear": (
        "A straight-line correction. One point fits the offset; two or more also "
        "fit the gain."
    ),
}


class PointTable(QTableWidget):
    """Captured (raw, reference) pairs."""

    COLUMNS = ("Use", "Raw", "Std dev", "Reference", "Residual")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(0, len(self.COLUMNS), parent)
        self.setHorizontalHeaderLabels(self.COLUMNS)
        self.verticalHeader().setVisible(False)
        self.setAlternatingRowColors(True)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.setColumnWidth(0, 36)
        self.points: list[dict] = []

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
        for index, point in enumerate(self.active()):
            point["residual"] = residuals[index] if residuals and index < len(residuals) else None
        self._rebuild()

    def _rebuild(self) -> None:
        self.blockSignals(True)
        self.setRowCount(len(self.points))
        mono = QFont("monospace")
        mono.setStyleHint(QFont.StyleHint.Monospace)
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

            for column, text in (
                (1, f"{point['raw']:.5g}"),
                (2, f"±{point['std']:.4g}" if point["std"] else ""),
                (3, f"{point['reference']:.4f}"),
                (4, "" if point.get("residual") is None
                    else f"{point['residual'] * 1000:+.1f} mK"),
            ):
                item = QTableWidgetItem(text)
                item.setFont(mono)
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
                self.setItem(row, column, item)
        self.blockSignals(False)


class CalibrationDialog(QDialog):
    """Per-channel calibration against reference points."""

    def __init__(self, device: Device, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.device = device
        self.theme = theme
        self.working = CalibrationSet.from_json(device.calibration.to_json())
        self._fits: dict[str, fitting.FitResult] = {}
        self._capture_task: asyncio.Task | None = None

        self.setWindowTitle(f"Calibrate — {device.label}")
        self.resize(900, 600)

        root = QVBoxLayout(self)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_left())
        splitter.addWidget(self._build_right())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 4)
        root.addWidget(splitter, 1)

        self.problems = QLabel("")
        self.problems.setWordWrap(True)
        self.problems.setStyleSheet(f"color: {theme.serious};")
        root.addWidget(self.problems)

        buttons = QDialogButtonBox()
        self.apply_button = buttons.addButton("Write to board",
                                              QDialogButtonBox.ButtonRole.AcceptRole)
        self.apply_button.setProperty("primary", True)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._on_apply)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._populate_channels()
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(400)
        self._live_timer.timeout.connect(self._update_live)
        self._live_timer.start()

    # ------------------------------------------------------------------- build

    def _build_left(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 6, 0)
        layout.setSpacing(6)

        picker = QHBoxLayout()
        picker.addWidget(QLabel("Channel"))
        self.channel_box = QComboBox()
        self.channel_box.currentIndexChanged.connect(self._on_channel_changed)
        picker.addWidget(self.channel_box, 1)
        layout.addLayout(picker)

        self.short_help = QLabel()
        self.short_help.setWordWrap(True)
        self.short_help.setStyleSheet(f"color: {self.theme.text_secondary};")
        layout.addWidget(self.short_help)

        # The long note is read once per channel type and then never again, so
        # it does not get to hold four lines of the dialog permanently.
        self.help_toggle = QToolButton()
        self.help_toggle.setText("Why these points?")
        self.help_toggle.setCheckable(True)
        self.help_toggle.setAutoRaise(True)
        self.help_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.help_toggle.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.help_toggle.toggled.connect(self._toggle_help)
        layout.addWidget(self.help_toggle, 0, Qt.AlignmentFlag.AlignLeft)

        self.help_label = QLabel()
        self.help_label.setWordWrap(True)
        self.help_label.setVisible(False)
        self.help_label.setStyleSheet(
            f"color: {self.theme.text_secondary}; background: {self.theme.surface_raised};"
            f"border: 1px solid {self.theme.border}; border-radius: 6px; padding: 8px;"
        )
        layout.addWidget(self.help_label)

        capture = QGroupBox("Capture a reference point")
        capture_form = QFormLayout(capture)
        capture_form.setContentsMargins(8, 6, 8, 6)
        capture_form.setSpacing(6)

        self.live_label = QLabel("—")
        mono = QFont("monospace")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.live_label.setFont(mono)
        capture_form.addRow("Board reads", self.live_label)

        # Reference value, averaging window and the button on one line: it is
        # one action, and stacking it over three rows made it look like three.
        entry = QHBoxLayout()
        entry.setSpacing(6)
        self.reference_input = QDoubleSpinBox()
        self.reference_input.setRange(-300.0, 2000.0)
        self.reference_input.setDecimals(4)
        self.reference_input.setValue(0.0)
        entry.addWidget(self.reference_input, 1)
        self.reference_unit = QLabel("°C")
        entry.addWidget(self.reference_unit)
        entry.addSpacing(8)
        self.settle_input = QDoubleSpinBox()
        self.settle_input.setRange(0.5, 300.0)
        self.settle_input.setValue(SETTLE_SECONDS)
        self.settle_input.setSuffix(" s avg")
        self.settle_input.setToolTip(
            "How long to average the board's reading while capturing.\n"
            "Longer beats down noise; too long drifts if the bath is not settled."
        )
        entry.addWidget(self.settle_input)
        self.capture_button = QPushButton("Capture")
        self.capture_button.setProperty("primary", True)
        self.capture_button.clicked.connect(self._on_capture)
        entry.addWidget(self.capture_button)
        capture_form.addRow("Reference reads", entry)
        layout.addWidget(capture)

        self.points = PointTable()
        layout.addWidget(self.points, 1)

        actions = QHBoxLayout()
        remove = QPushButton("Remove point")
        remove.clicked.connect(lambda: (self.points.remove_selected(), self._refit()))
        clear = QPushButton("Clear all")
        clear.clicked.connect(lambda: (self.points.clear_points(), self._refit()))
        actions.addWidget(remove)
        actions.addWidget(clear)
        actions.addStretch(1)
        layout.addLayout(actions)
        return panel

    def _toggle_help(self, shown: bool) -> None:
        self.help_label.setVisible(shown)
        self.help_toggle.setArrowType(
            Qt.ArrowType.DownArrow if shown else Qt.ArrowType.RightArrow
        )

    def _build_right(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(6, 0, 0, 0)

        self.residual_plot = pg.PlotWidget()
        self.residual_plot.setLabel("left", "Residual [mK]")
        self.residual_plot.setLabel("bottom", "Reference")
        self.residual_plot.showGrid(x=True, y=True, alpha=0.25)
        self.residual_plot.addLine(y=0, pen=pg.mkPen(self.theme.text_muted, width=1))
        self.residual_plot.setMinimumHeight(160)
        layout.addWidget(self.residual_plot, 1)

        self.quality_label = QLabel("Capture at least one point, then fit.")
        self.quality_label.setWordWrap(True)
        layout.addWidget(self.quality_label)

        self.coefficients = QLabel("")
        self.coefficients.setWordWrap(True)
        self.coefficients.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        font = QFont("monospace")
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.coefficients.setFont(font)
        self.coefficients.setStyleSheet(
            f"background: {self.theme.surface_sunken}; border: 1px solid {self.theme.border};"
            f"border-radius: 6px; padding: 8px; color: {self.theme.text_primary};"
        )
        layout.addWidget(self.coefficients)

        # Provenance is two fields. It had a group box, a title and its own
        # vertical layout for that, which is more chrome than content.
        meta = QFormLayout()
        meta.setContentsMargins(0, 4, 0, 0)
        meta.setSpacing(4)
        self.by_input = QLineEdit(self.working.by)
        self.by_input.setPlaceholderText("Who performed this calibration")
        self.reference_note = QLineEdit(self.working.reference)
        self.reference_note.setPlaceholderText(
            "e.g. Fluke 1524 / 5608, cert 2026-01-12"
        )
        meta.addRow("By", self.by_input)
        meta.addRow("Against", self.reference_note)
        layout.addLayout(meta)
        return panel

    # -------------------------------------------------------------- channels

    def _populate_channels(self) -> None:
        self.channel_box.blockSignals(True)
        self.channel_box.clear()
        for spec in self.device.channels.values():
            if spec.cal_model is None:
                continue
            self.channel_box.addItem(f"{spec.name}  ({spec.key})", spec.id)
        self.channel_box.blockSignals(False)
        if self.channel_box.count():
            self._on_channel_changed(0)

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

    def _on_channel_changed(self, _index: int) -> None:
        spec = self.current_spec
        if spec is None:
            return
        self.points.clear_points()
        model = spec.cal_model or ""
        self.short_help.setText(SHORT_HELP.get(model, ""))
        self.help_label.setText(HELP.get(model, ""))
        self.help_toggle.setVisible(bool(HELP.get(model)))
        self.reference_unit.setText(unit_symbol(spec.unit))
        existing = self.working.get(spec.key)
        self.coefficients.setText(f"Currently on the board:\n  {existing.describe()}")
        self._refit()

    # ---------------------------------------------------------------- capture

    def _update_live(self) -> None:
        spec = self.current_spec
        if spec is None:
            return
        raw_id, raw_unit = self._raw_channel()
        raw_index = self.device.channel_index(raw_id)
        cooked = self.device.latest_values().get(spec.id, float("nan"))
        raw_value = float("nan")
        if raw_index is not None:
            raw_value = self.device.latest_values().get(raw_id, float("nan"))

        cooked_text = "fault" if not math.isfinite(cooked) else f"{cooked:.4f} {unit_symbol(spec.unit)}"
        if raw_id != spec.id and math.isfinite(raw_value):
            self.live_label.setText(
                f"{cooked_text}   (raw {raw_value:.5g} {unit_symbol(raw_unit)})"
            )
        else:
            self.live_label.setText(cooked_text)

    def _on_capture(self) -> None:
        if self._capture_task and not self._capture_task.done():
            return
        self._capture_task = asyncio.ensure_future(self._capture())

    async def _capture(self) -> None:
        spec = self.current_spec
        if spec is None:
            return
        raw_id, raw_unit = self._raw_channel()
        seconds = float(self.settle_input.value())
        reference = float(self.reference_input.value())

        self.capture_button.setEnabled(False)
        try:
            deadline = seconds
            step = 0.25
            elapsed = 0.0
            while elapsed < deadline:
                await asyncio.sleep(step)
                elapsed += step
                self.capture_button.setText(f"Capturing… {deadline - elapsed:.0f}s")

            _t, values = self.device.series(raw_id, seconds)
            good = values[np.isfinite(values)]
            if good.size == 0:
                message_later("warning",
                    self, "No usable samples",
                    f"{spec.name} produced no valid readings during the settling "
                    f"window. Check the probe is connected and the channel is not "
                    f"faulted."
                )
                return
            self.points.add_point(float(np.mean(good)), float(np.std(good)), reference)
            self._refit()
        finally:
            self.capture_button.setEnabled(True)
            self.capture_button.setText("Capture")

    # -------------------------------------------------------------------- fit

    def _refit(self) -> None:
        spec = self.current_spec
        if spec is None:
            return
        points = self.points.active()
        if not points:
            self.residual_plot.clear()
            self.residual_plot.addLine(y=0, pen=pg.mkPen(self.theme.text_muted, width=1))
            self.quality_label.setText("Capture at least one point, then a fit appears here.")
            return

        raw = np.array([p["raw"] for p in points])
        reference = np.array([p["reference"] for p in points])

        try:
            result = self._run_fit(spec, raw, reference)
        except (fitting.FitError, ValueError, np.linalg.LinAlgError) as exc:
            self.quality_label.setText(str(exc))
            self.quality_label.setStyleSheet(f"color: {self.theme.serious};")
            self.points.set_residuals(None)
            self.residual_plot.clear()
            return

        self._fits[spec.key] = result
        self.working.set(spec.key, result.to_channel_cal())
        self.points.set_residuals([float(r) for r in result.residuals])

        colour = self.theme.text_secondary
        if not result.exactly_determined and result.max_abs > 0.5:
            colour = self.theme.warning
        self.quality_label.setStyleSheet(f"color: {colour};")
        text = result.quality_note()
        if result.message:
            text += f"\n\n{result.message}"
        self.quality_label.setText(text)

        lines = [f"{spec.name} — model {result.model}"]
        for key, value in result.params.items():
            lines.append(f"  {key:<12} {value!r}" if isinstance(value, list)
                         else f"  {key:<12} {value:.9g}")
        self.coefficients.setText("\n".join(lines))
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
            cj_key = self.device.channels[int(Ch.TYPEK_CJ)].key
            cj_cal = self.working.get(cj_key)
            cj_offset = cj_cal.params.get("offset", 0.0) if cj_cal.model == "linear" else 0.0
            _t, cj_series = self.device.series(int(Ch.TYPEK_CJ), 60.0)
            cj_now = float(np.nanmean(cj_series)) if cj_series.size else 25.0
            cj = np.full(len(raw), cj_now)
            return fitting.fit_typek(raw, cj, reference, cj_offset_c=cj_offset)
        return fitting.fit_linear(raw, reference)

    def _plot_residuals(self, result: fitting.FitResult) -> None:
        self.residual_plot.clear()
        self.residual_plot.addLine(y=0, pen=pg.mkPen(self.theme.text_muted, width=1))
        residuals_mk = result.residuals * 1000.0
        scatter = pg.ScatterPlotItem(
            x=result.reference, y=residuals_mk, size=10,
            brush=pg.mkBrush(self.theme.accent), pen=pg.mkPen(self.theme.surface_sunken, width=2),
        )
        self.residual_plot.addItem(scatter)
        if residuals_mk.size:
            span = max(float(np.max(np.abs(residuals_mk))) * 1.6, 1.0)
            self.residual_plot.setYRange(-span, span)

    # ------------------------------------------------------------------ apply

    def _validate(self) -> bool:
        problems = validate(self.working)
        self.problems.setText("\n".join(problems))
        self.apply_button.setEnabled(not problems)
        return not problems

    def _on_apply(self) -> None:
        if not self._validate():
            return
        if not self._fits:
            QMessageBox.information(
                self, "Nothing to write",
                "No channel has been fitted yet. Capture reference points and the "
                "fit appears automatically."
            )
            return
        names = ", ".join(sorted(self._fits))
        answer = QMessageBox.question(
            self, "Write calibration to the board?",
            f"This writes new coefficients for <b>{names}</b> into the board's "
            f"non-volatile memory, replacing what is there now.<br><br>"
            f"The board applies them itself, so its own display and every connected "
            f"program will immediately show the corrected values.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        asyncio.ensure_future(self._write())

    async def _write(self) -> None:
        self.apply_button.setEnabled(False)
        self.apply_button.setText("Writing…")
        try:
            stamped = self.working.touched(
                by=self.by_input.text().strip(),
                reference=self.reference_note.text().strip(),
            )
            await self.device.write_calibration(stamped)
        except Exception as exc:
            message_later("critical",
                self, "Calibration not written",
                f"The board did not accept the calibration:\n\n{exc}\n\n"
                f"Nothing was changed."
            )
            self.apply_button.setEnabled(True)
            self.apply_button.setText("Write to board")
            return
        message_later("information",
            self, "Calibration written",
            f"Stored as revision {self.device.calibration.rev}."
        )
        self.accept()

    def _teardown(self) -> None:
        self._live_timer.stop()
        if self._capture_task and not self._capture_task.done():
            self._capture_task.cancel()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._teardown()
        super().closeEvent(event)

    def done(self, result: int) -> None:
        # Cancel, Esc and accept() all go through done() and none of them
        # produce a close event, so closeEvent alone left the 400 ms live timer
        # running on a hidden dialog and a capture coroutine still in flight.
        self._teardown()
        super().done(result)
