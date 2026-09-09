"""The PT1000 simulator: what the board is telling a hotplate.

Only shown for boards that announce ``caps.sim.pt1000_out`` — its predecessor
has no digipot and would answer ERROR "unsupported" to everything here.

The panel is built around the three questions the simulator can be wrong in:
is it driving at all, is what it presents what we asked for, and is the wiper
table still trustworthy. Everything else is settings.
"""

from __future__ import annotations

import asyncio
import contextlib

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..device.device import Device, DeviceEvent
from ..protocol.channels import Ch
from .theme import Theme
from .widgets import alive

#: What each state means, in the words someone would use to describe the
#: problem. "idle" and "uncalibrated" both mean "not driving", and the
#: difference between them is the whole diagnosis, so neither is abbreviated.
STATE_TEXT = {
    "active": "Driving the output",
    "cal_stale": "Driving, but the wiper table has aged",
    "calibrating": "Sweeping the wiper table",
    "idle": "Not driving — no source channel selected",
    "uncalibrated": "Not driving — no wiper table",
    "source_fault": "Not driving — the source channel has faulted",
}

#: Which theme colour each state earns. Never the only signal: the text above
#: says the same thing.
STATE_TONE = {
    "active": "good",
    "cal_stale": "warning",
    "calibrating": "accent",
    "idle": "text_secondary",
    "uncalibrated": "critical",
    "source_fault": "critical",
}


#: What the board's sweep error codes mean, in words.
SWEEP_ERRORS = {
    "loopback_absent":
        "no loopback cable. Link the simulator OUT terminals to the PT1000 IN "
        "terminals and try again — without it the sweep would record whatever "
        "probe is on the input instead of the digipot.",
    "sweep_failed":
        "too few usable wiper codes. Check the loopback cable is making "
        "contact and that the digipot is responding.",
    "nvs_write_failed":
        "the table measured fine but could not be written to the board's "
        "memory, so it will not survive a reboot.",
    "out_of_memory":
        "the board did not have enough memory to run the sweep.",
}


class SimulatorPanel(QWidget):
    """Controls and state for the emulated PT1000 output."""

    def __init__(self, device: Device, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.device = device
        self.theme = theme
        self._loading = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        intro = QLabel(
            "This board presents a resistance to a hotplate that a PT1000 at the "
            "source channel's temperature would have. The output is deliberately "
            "lagged so it behaves like the sensor it is impersonating rather than "
            "the faster one it is reading."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {theme.text_secondary};")
        layout.addWidget(intro)

        layout.addWidget(self._build_state())
        layout.addWidget(self._build_settings())
        layout.addWidget(self._build_calibration())
        layout.addStretch(1)

        self._unsub = device.subscribe(self._on_event)
        self.refresh()

    # ------------------------------------------------------------------ build

    def _build_state(self) -> QWidget:
        box = QGroupBox("Output")
        form = QFormLayout(box)

        self.state_label = QLabel("—")
        font = self.state_label.font()
        font.setPointSize(font.pointSize() + 2)
        font.setBold(True)
        self.state_label.setFont(font)
        form.addRow(self.state_label)

        self.readout = QLabel("—")
        self.readout.setTextFormat(Qt.TextFormat.RichText)
        form.addRow("Presenting", self.readout)

        self.wiper_label = QLabel("—")
        form.addRow("Wiper", self.wiper_label)
        return box

    def _build_settings(self) -> QWidget:
        box = QGroupBox("Settings")
        form = QFormLayout(box)

        self.source_box = QComboBox()
        self.source_box.setToolTip(
            "Which measured channel drives the emulated PT1000.\n"
            "Off parks the output at the failsafe."
        )
        self.source_box.currentIndexChanged.connect(self._push)
        form.addRow("Driven from", self.source_box)

        self.tau = QDoubleSpinBox()
        self.tau.setRange(0.1, 120.0)
        self.tau.setSingleStep(0.5)
        self.tau.setSuffix(" s")
        self.tau.setToolTip(
            "First-order lag between the source and the output.\n"
            "A thermocouple responds in well under a second and the PT1000 it is "
            "impersonating does not; without this the plate's controller sees a "
            "step response its loop was never tuned for."
        )
        self.tau.editingFinished.connect(self._push)
        form.addRow("Lag", self.tau)

        dither_row = QHBoxLayout()
        self.dither = QCheckBox("Dither between adjacent wiper codes")
        self.dither.setToolTip(
            "Alternates between two codes so the plate's own filtering averages "
            "them, giving finer resolution than a single step.\n"
            "Off by default: in the 80–150 °C band a step is already 0.35–1.0 °C."
        )
        self.dither.toggled.connect(self._push)
        dither_row.addWidget(self.dither)
        self.dither_hz = QSpinBox()
        self.dither_hz.setRange(5, 200)
        self.dither_hz.setSuffix(" Hz")
        self.dither_hz.editingFinished.connect(self._push)
        dither_row.addWidget(self.dither_hz)
        dither_row.addStretch(1)
        form.addRow("Resolution", dither_row)

        limits = QHBoxLayout()
        self.min_c = QDoubleSpinBox()
        self.max_c = QDoubleSpinBox()
        for spin in (self.min_c, self.max_c):
            spin.setRange(-250.0, 400.0)
            spin.setSuffix(" °C")
            spin.editingFinished.connect(self._push)
        limits.addWidget(self.min_c)
        limits.addWidget(QLabel("to"))
        limits.addWidget(self.max_c)
        limits.addStretch(1)
        form.addRow("Clamp output", limits)

        self.drift_warn = QDoubleSpinBox()
        self.drift_warn.setRange(0.1, 50.0)
        self.drift_warn.setSuffix(" °C")
        self.drift_warn.setToolTip(
            "How far the board temperature may drift from where the wiper table "
            "was measured before it is called stale.\n"
            "The digipot's resistance moves with temperature, so a table is only "
            "valid near the temperature it was swept at."
        )
        self.drift_warn.editingFinished.connect(self._push)
        form.addRow("Stale after", self.drift_warn)
        return box

    def _build_calibration(self) -> QWidget:
        box = QGroupBox("Wiper table")
        layout = QVBoxLayout(box)

        self.table_label = QLabel("—")
        self.table_label.setWordWrap(True)
        layout.addWidget(self.table_label)

        self.drift_label = QLabel("")
        self.drift_label.setWordWrap(True)
        layout.addWidget(self.drift_label)

        note = QLabel(
            "Calibrating measures every wiper code against the board's own "
            "converter, so it needs a cable from the simulator OUT terminal to "
            "the RTD IN terminal. The board checks the cable is there and "
            "refuses without it — otherwise the sweep would record whatever "
            "probe is on the input instead of the digipot. Disconnect the "
            "hotplate first: the sweep drives the output across its whole range."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {self.theme.text_secondary};")
        layout.addWidget(note)

        row = QHBoxLayout()
        self.calibrate_button = QPushButton("Calibrate wiper table…")
        self.calibrate_button.clicked.connect(self._calibrate)
        row.addWidget(self.calibrate_button)
        row.addStretch(1)
        layout.addLayout(row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 255)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.verify_label = QLabel("")
        self.verify_label.setWordWrap(True)
        self.verify_label.setStyleSheet("font-family: monospace;")
        layout.addWidget(self.verify_label)
        return box

    # ------------------------------------------------------------------ state

    def _on_event(self, event: DeviceEvent) -> None:
        if event.kind in ("status", "config", "sim_cal", "info"):
            self.refresh()

    def refresh(self) -> None:
        self._loading = True
        try:
            self._refresh_state()
            self._refresh_settings()
            self._refresh_table()
        finally:
            self._loading = False

    def _refresh_state(self) -> None:
        sim = self.device.sim_status
        state = sim.get("state", "")
        tone = getattr(self.theme, STATE_TONE.get(state, "text_secondary"),
                       self.theme.text_secondary)
        self.state_label.setText(STATE_TEXT.get(state, state or "—"))
        self.state_label.setStyleSheet(f"color: {tone};")

        setpoint, output = sim.get("setpoint_c"), sim.get("output_c")
        ohms = sim.get("output_r")
        if output is None:
            self.readout.setText(
                f"<span style='color:{self.theme.text_secondary}'>"
                "nothing — the output is held at the failsafe</span>"
            )
        else:
            asked = f"{setpoint:.2f} °C" if setpoint is not None else "—"
            self.readout.setText(
                f"<b>{output:.2f} °C</b> ({ohms:.1f} Ω) &nbsp; asked for {asked}"
            )

        wiper = sim.get("wiper")
        if wiper is None:
            self.wiper_label.setText("—")
        elif wiper == 255:
            self.wiper_label.setText(
                "255 — the failsafe. A plate reads this as a shorted sensor."
            )
        else:
            self.wiper_label.setText(str(wiper))

        progress = sim.get("cal_progress")
        sweeping = progress is not None
        self.progress.setVisible(sweeping)
        if sweeping:
            self.progress.setValue(int(progress))
        self.calibrate_button.setEnabled(not sweeping and self.device.is_online)

    def _refresh_settings(self) -> None:
        cfg = (self.device.config or {}).get("sim") or {}
        if not cfg:
            return

        current = cfg.get("source")
        if self.source_box.count() == 0 or self.source_box.property("stale"):
            self._rebuild_sources()
        index = self.source_box.findData(current if current is not None else -1)
        if index >= 0:
            self.source_box.setCurrentIndex(index)

        self.tau.setValue(float(cfg.get("tau_s", 4.0)))
        self.dither.setChecked(bool(cfg.get("dither", False)))
        self.dither_hz.setValue(int(cfg.get("dither_hz", 30)))
        self.dither_hz.setEnabled(self.dither.isChecked())
        limits = cfg.get("limits") or {}
        self.min_c.setValue(float(limits.get("min_c", -50.0)))
        self.max_c.setValue(float(limits.get("max_c", 240.0)))
        self.drift_warn.setValue(float(cfg.get("drift_warn_c", 3.0)))

    def _rebuild_sources(self) -> None:
        self.source_box.clear()
        self.source_box.addItem("Off — park at the failsafe", -1)
        excluded = {int(Ch.SIM_SETPOINT), int(Ch.SIM_ACTUAL), int(Ch.SIM_R)}
        for spec in self.device.channels.values():
            if spec.id in excluded or not spec.is_temperature:
                continue
            self.source_box.addItem(spec.name, spec.id)
        self.source_box.setProperty("stale", False)

    def _refresh_table(self) -> None:
        sim = self.device.sim_status
        table = self.device.sim_cal or {}
        rev = sim.get("cal_rev", table.get("rev"))
        swept_at = sim.get("cal_ntc_rtd_c", table.get("ntc_rtd_c"))
        usable = table.get("n_usable")

        if not rev:
            self.table_label.setText(
                "No table on the board. The output stays at the failsafe until "
                "one is measured."
            )
        else:
            parts = [f"Table rev {rev}"]
            if usable:
                parts.append(f"{usable} usable codes")
            if table.get("r_min") is not None:
                parts.append(f"{table['r_min']:.0f}–{table['r_max']:.0f} Ω")
            self.table_label.setText(", ".join(parts) + ".")

        now = sim.get("ntc_rtd_c")
        drift = sim.get("drift_c")
        if swept_at is None:
            self.drift_label.setText(
                "Swept without a board-temperature reading, so staleness cannot "
                "be checked. Calibrate again to fix that."
            )
            self.drift_label.setStyleSheet(f"color: {self.theme.warning};")
        elif drift is None or now is None:
            self.drift_label.setText(f"Swept at {swept_at:.1f} °C.")
            self.drift_label.setStyleSheet(f"color: {self.theme.text_secondary};")
        else:
            stale = sim.get("state") == "cal_stale"
            self.drift_label.setText(
                f"Swept at {swept_at:.1f} °C; the board is now {now:.1f} °C, "
                f"a drift of {drift:+.2f} °C."
                + (" Recalibrate." if stale else "")
            )
            self.drift_label.setStyleSheet(
                f"color: {self.theme.warning if stale else self.theme.text_secondary};"
            )

        sweep = (self.device.sim_cal or {}).get("last_sweep") or {}
        error = sweep.get("error")
        if error:
            # A failed sweep reports no verification points, and this used to
            # return on that and say nothing at all -- so a calibration that
            # never had a loopback cable fitted looked identical to one that
            # simply did not happen.
            self.verify_label.setText(
                f"Last sweep failed: {SWEEP_ERRORS.get(error, error)}"
            )
            self.verify_label.setStyleSheet(f"color: {self.theme.critical};")
            return

        points = sweep.get("verify") or []
        if not points:
            self.verify_label.setText("")
            self.verify_label.setStyleSheet(f"color: {self.theme.text_secondary};")
            return
        self.verify_label.setStyleSheet(f"color: {self.theme.text_secondary};")
        lines = ["Last sweep checked itself against:"]
        for point in points:
            err = point["actual_c"] - point["target_c"]
            lines.append(
                f"  {point['target_c']:7.1f} °C  →  {point['actual_c']:7.2f} °C"
                f"   error {err:+.2f} °C"
            )
        self.verify_label.setText("\n".join(lines))

    # ----------------------------------------------------------------- actions

    def _push(self) -> None:
        if self._loading:
            return
        source = self.source_box.currentData()
        patch = {
            "source": None if source in (None, -1) else int(source),
            "tau_s": float(self.tau.value()),
            "dither": bool(self.dither.isChecked()),
            "dither_hz": int(self.dither_hz.value()),
            "limits": {"min_c": float(self.min_c.value()),
                       "max_c": float(self.max_c.value())},
            "drift_warn_c": float(self.drift_warn.value()),
        }
        self.dither_hz.setEnabled(self.dither.isChecked())

        async def run() -> None:
            with contextlib.suppress(Exception):
                await self.device.set_sim(**patch)

        asyncio.ensure_future(run())

    def _calibrate(self) -> None:
        self.calibrate_button.setEnabled(False)
        self.verify_label.setText("")

        async def run() -> None:
            try:
                await self.device.calibrate_simulator()
            except Exception as exc:  # noqa: BLE001 - surfaced in the UI
                if alive(self):
                    self.verify_label.setText(str(exc))
                    self.calibrate_button.setEnabled(True)

        asyncio.ensure_future(run())

    def set_theme(self, theme: Theme) -> None:
        self.theme = theme
        self.refresh()

    def close_panel(self) -> None:
        with contextlib.suppress(Exception):
            self._unsub()
