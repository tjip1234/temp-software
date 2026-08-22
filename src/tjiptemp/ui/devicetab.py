"""One tab per board: live charts, channel table, board settings, screen control.

Everything a user does to a *single* board lives here. The main window owns the
list of boards and anything that spans them.
"""

from __future__ import annotations

import asyncio
import contextlib

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..device.device import Device, DeviceEvent
from ..protocol.channels import Ch
from ..sensors.rtd import resolution_c, wire_mode_note
from .calibration import CalibrationDialog
from .liveview import LiveView
from .theme import Theme
from .widgets import ChannelTable, FaultBanner, LinkBar, MetricLabel, StatusPill, humanise_duration

DISPLAY_PAGES = [
    ("Overview — all channels", "overview"),
    ("Single channel, large", "single"),
    ("Graph", "graph"),
    ("Status and battery", "status"),
    ("Blank", "blank"),
]


class DeviceTab(QWidget):
    """The full view of one board."""

    recording_changed = Signal()

    def __init__(self, app, device: Device, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self.device = device
        self.theme = theme
        self._unsubscribe = device.subscribe(self._on_event)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        root.addLayout(self._build_header())
        self.faults = FaultBanner()
        root.addWidget(self.faults)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.live = LiveView(device, theme)
        splitter.addWidget(self.live)

        side = QTabWidget()
        side.addTab(self._build_channels_tab(), "Channels")
        side.addTab(self._build_settings_tab(), "Sensors")
        side.addTab(self._build_display_tab(), "Board screen")
        side.addTab(self._build_diagnostics_tab(), "Diagnostics")
        side.setMinimumWidth(340)
        splitter.addWidget(side)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)

        self._timer = QTimer(self)
        self._timer.setInterval(300)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()

        self._rebuild_channels()
        self._refresh()

    # ------------------------------------------------------------------ header

    def _build_header(self) -> QHBoxLayout:
        layout = QHBoxLayout()
        layout.setSpacing(12)

        self.title = QLabel(self.device.label)
        font = self.title.font()
        font.setPointSizeF(font.pointSizeF() + 3)
        font.setWeight(font.Weight.DemiBold)
        self.title.setFont(font)
        layout.addWidget(self.title)

        self.pill = StatusPill()
        layout.addWidget(self.pill)
        layout.addStretch(1)

        self.metric_battery = MetricLabel("Battery")
        self.metric_time = MetricLabel("Timestamps")
        self.metric_stream = MetricLabel("Stream")
        self.metric_recording = MetricLabel("Recording")
        for metric in (self.metric_battery, self.metric_time,
                       self.metric_stream, self.metric_recording):
            metric.set_theme(self.theme)
            layout.addWidget(metric)

        self.links = LinkBar()
        layout.addWidget(self.links)

        self.record_button = QPushButton("Record")
        self.record_button.setProperty("primary", True)
        self.record_button.clicked.connect(self._toggle_recording)
        layout.addWidget(self.record_button)

        self.calibrate_button = QPushButton("Calibrate…")
        self.calibrate_button.clicked.connect(self._open_calibration)
        layout.addWidget(self.calibrate_button)
        return layout

    # ---------------------------------------------------------------- channels

    def _build_channels_tab(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 8, 8, 8)

        hint = QLabel("Tick a channel to plot it. Values update live regardless.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {self.theme.text_muted};")
        layout.addWidget(hint)

        self.table = ChannelTable()
        self.table.set_theme(self.theme)
        self.table.set_unit_preference(self.app.settings.temperature_unit)
        self.table.visibility_changed.connect(self._on_channel_toggled)
        layout.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        for label, action in (
            ("Probes", self._show_probes),
            ("All temperatures", self._show_temperatures),
            ("Everything", self._show_all),
        ):
            button = QPushButton(label)
            button.clicked.connect(action)
            buttons.addWidget(button)
        layout.addLayout(buttons)
        return panel

    def _rebuild_channels(self) -> None:
        specs = list(self.device.channels.values())
        self.table.rebuild(specs)
        self._show_probes()

    def _on_channel_toggled(self, channel_id: int, plotted: bool) -> None:
        """A tick box changed: add or remove that one trace, leaving the rest alone."""
        visible = self.live.visible_channels()
        if plotted:
            visible.add(channel_id)
        else:
            visible.discard(channel_id)
        # Preserve the canonical channel order so grouping stays stable.
        self.live.set_channels([c for c in self.device.channel_order if c in visible])

    def _set_plotted(self, channel_ids: list[int]) -> None:
        self.live.set_channels(channel_ids)
        self.table.set_checked(set(channel_ids))

    def _show_probes(self) -> None:
        from ..protocol.channels import PROBE_CHANNELS

        available = [c for c in PROBE_CHANNELS if c in self.device.channels]
        self._set_plotted(available or list(self.device.channel_order)[:4])

    def _show_temperatures(self) -> None:
        wanted = [
            spec.id for spec in self.device.channels.values()
            if spec.unit in ("degC", "degF", "K")
        ]
        if len(wanted) > 8:
            # More temperature traces than there are distinguishable colours. Rather
            # than inventing a ninth hue, drop the diagnostics that are least likely
            # to be the subject of the measurement.
            diagnostics = {int(Ch.NTC_BRD_RTD), int(Ch.NTC_BRD_TC)}
            wanted = [c for c in wanted if c not in diagnostics]
        self._set_plotted(wanted)

    def _show_all(self) -> None:
        self._set_plotted(list(self.device.channel_order))

    # ---------------------------------------------------------------- settings

    def _build_settings_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 8, 8, 8)

        rtd = QGroupBox("PT1000 (MAX31865)")
        rtd_form = QFormLayout(rtd)
        self.wires_box = QComboBox()
        for label, value in (("2-wire", 2), ("3-wire", 3), ("4-wire", 4)):
            self.wires_box.addItem(label, value)
        self.wires_box.currentIndexChanged.connect(self._on_wires_changed)
        rtd_form.addRow("Wiring", self.wires_box)

        self.wires_note = QLabel()
        self.wires_note.setWordWrap(True)
        self.wires_note.setStyleSheet(f"color: {self.theme.text_secondary};")
        rtd_form.addRow(self.wires_note)

        self.filter_box = QComboBox()
        self.filter_box.addItem("50 Hz (Europe, Asia)", 50)
        self.filter_box.addItem("60 Hz (Americas)", 60)
        self.filter_box.currentIndexChanged.connect(self._on_filter_changed)
        rtd_form.addRow("Mains rejection", self.filter_box)

        self.rref_input = QDoubleSpinBox()
        self.rref_input.setRange(100.0, 20000.0)
        self.rref_input.setDecimals(1)
        self.rref_input.setSuffix(" Ω")
        self.rref_input.editingFinished.connect(self._on_rref_changed)
        rtd_form.addRow("Reference resistor", self.rref_input)

        self.resolution_note = QLabel()
        self.resolution_note.setStyleSheet(f"color: {self.theme.text_muted};")
        rtd_form.addRow(self.resolution_note)
        layout.addWidget(rtd)

        tc = QGroupBox("Thermocouple (MAX31856)")
        tc_form = QFormLayout(tc)
        self.tc_type_box = QComboBox()
        from ..sensors.thermocouple import CHIP_TYPES, SUPPORTED_TYPES

        for kind in CHIP_TYPES:
            label = f"Type {kind}" + ("" if kind in SUPPORTED_TYPES else "  (chip linearisation)")
            self.tc_type_box.addItem(label, kind)
        self.tc_type_box.currentIndexChanged.connect(self._on_tc_type_changed)
        tc_form.addRow("Type", self.tc_type_box)

        self.cj_box = QComboBox()
        self.cj_box.addItem("MAX31856 internal sensor", "internal")
        self.cj_box.addItem("Board NTC beside the connector", "ntc_brd_tc")
        self.cj_box.addItem("Fixed value", "fixed")
        self.cj_box.currentIndexChanged.connect(self._on_cj_changed)
        tc_form.addRow("Cold junction", self.cj_box)

        self.tc_avg_box = QComboBox()
        for count in (1, 2, 4, 8, 16):
            self.tc_avg_box.addItem(f"{count} sample{'s' if count > 1 else ''}", count)
        self.tc_avg_box.currentIndexChanged.connect(self._on_tc_avg_changed)
        tc_form.addRow("Averaging", self.tc_avg_box)
        layout.addWidget(tc)

        acquire = QGroupBox("Acquisition")
        acquire_form = QFormLayout(acquire)
        self.rate_input = QDoubleSpinBox()
        self.rate_input.setRange(0.1, 100.0)
        self.rate_input.setDecimals(1)
        self.rate_input.setSuffix(" Hz")
        self.rate_input.editingFinished.connect(self._on_rate_changed)
        acquire_form.addRow("Sample rate", self.rate_input)

        self.ring_input = QSpinBox()
        self.ring_input.setRange(10, 86400)
        self.ring_input.setSuffix(" s")
        self.ring_input.editingFinished.connect(self._on_ring_changed)
        acquire_form.addRow("On-board buffer", self.ring_input)

        self.ring_note = QLabel()
        self.ring_note.setWordWrap(True)
        self.ring_note.setStyleSheet(f"color: {self.theme.text_muted};")
        acquire_form.addRow(self.ring_note)
        layout.addWidget(acquire)

        layout.addStretch(1)
        scroll.setWidget(panel)
        return scroll

    # ----------------------------------------------------------------- display

    def _build_display_tab(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 8, 8, 8)

        hint = QLabel(
            "The board has no buttons, so its 240×320 screen is driven entirely from "
            "here. These settings persist on the board and survive a reboot with no "
            "computer attached."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {self.theme.text_secondary};")
        layout.addWidget(hint)

        form = QFormLayout()
        self.page_box = QComboBox()
        for label, value in DISPLAY_PAGES:
            self.page_box.addItem(label, value)
        self.page_box.currentIndexChanged.connect(self._push_display)
        form.addRow("Page", self.page_box)

        self.source_box = QComboBox()
        self.source_box.currentIndexChanged.connect(self._push_display)
        form.addRow("Channel", self.source_box)

        self.display_window = QComboBox()
        for label, seconds in (("1 min", 60), ("5 min", 300), ("15 min", 900), ("1 hour", 3600)):
            self.display_window.addItem(label, seconds)
        self.display_window.setCurrentIndex(1)
        self.display_window.currentIndexChanged.connect(self._push_display)
        form.addRow("Graph window", self.display_window)

        self.rotation_box = QComboBox()
        for label, value in (("0°", 0), ("90°", 90), ("180°", 180), ("270°", 270)):
            self.rotation_box.addItem(label, value)
        self.rotation_box.currentIndexChanged.connect(self._push_display)
        form.addRow("Rotation", self.rotation_box)

        self.backlight = QSlider(Qt.Orientation.Horizontal)
        self.backlight.setRange(0, 100)
        self.backlight.setValue(80)
        self.backlight.sliderReleased.connect(self._push_display)
        form.addRow("Backlight", self.backlight)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        identify = QPushButton("Identify (blink LEDs)")
        identify.setToolTip("Blink this board's LEDs so you can tell it from the others.")
        identify.clicked.connect(lambda: asyncio.ensure_future(self._identify()))
        buttons.addWidget(identify)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        layout.addStretch(1)
        return panel

    # ------------------------------------------------------------ diagnostics

    def _build_diagnostics_tab(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 8, 8, 8)

        self.diag_grid = QGridLayout()
        layout.addLayout(self.diag_grid)

        self.diag_label = QLabel("")
        self.diag_label.setWordWrap(True)
        self.diag_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.diag_label.setStyleSheet(
            f"background: {self.theme.surface_sunken}; border: 1px solid {self.theme.border};"
            f"border-radius: 6px; padding: 8px; color: {self.theme.text_secondary};"
        )
        layout.addWidget(self.diag_label, 1)

        buttons = QHBoxLayout()
        self_test = QPushButton("Run self-test")
        self_test.clicked.connect(lambda: asyncio.ensure_future(self._self_test()))
        buttons.addWidget(self_test)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        return panel

    # ------------------------------------------------------------------ config

    def _sync_from_config(self) -> None:
        config = self.device.config
        if not config:
            return
        blockers = (self.wires_box, self.filter_box, self.rref_input, self.tc_type_box,
                    self.cj_box, self.tc_avg_box, self.rate_input, self.ring_input,
                    self.page_box, self.source_box, self.rotation_box)
        for widget in blockers:
            widget.blockSignals(True)
        try:
            rtd = config.get("rtd", {})
            wires = int(rtd.get("wires", 4))
            self.wires_box.setCurrentIndex(max(0, self.wires_box.findData(wires)))
            self.wires_note.setText(wire_mode_note(wires))
            self.filter_box.setCurrentIndex(
                max(0, self.filter_box.findData(int(rtd.get("filter_hz", 50))))
            )
            rref = float(rtd.get("rref_ohm", 4000.0))
            self.rref_input.setValue(rref)
            self.resolution_note.setText(
                f"One converter step is {resolution_c(1000.0, rref, 25.0) * 1000:.1f} mK "
                f"at 25 °C — noise below that is not real."
            )

            tc = config.get("tc", {})
            self.tc_type_box.setCurrentIndex(
                max(0, self.tc_type_box.findData(tc.get("type", "K")))
            )
            self.cj_box.setCurrentIndex(
                max(0, self.cj_box.findData(tc.get("cj_source", "internal")))
            )
            self.tc_avg_box.setCurrentIndex(
                max(0, self.tc_avg_box.findData(int(tc.get("avg", 4))))
            )

            acquire = config.get("acquire", {})
            self.rate_input.setMaximum(self.device.max_rate_hz)
            self.rate_input.setValue(float(acquire.get("rate_hz", 10.0)))
            ring_seconds = int(acquire.get("ring_seconds", 600))
            self.ring_input.setValue(ring_seconds)
            rows = int(self.device.ring_samples)
            self.ring_note.setText(
                f"{rows:,} rows on the board. If a link drops, this is how far back "
                f"the missing samples can still be recovered from."
            )

            display = config.get("display", {})
            self.page_box.setCurrentIndex(
                max(0, self.page_box.findData(display.get("page", "overview")))
            )
            self._sync_source_box(int(display.get("source", 0)))
            self.rotation_box.setCurrentIndex(
                max(0, self.rotation_box.findData(int(display.get("rotation", 0))))
            )
            self.backlight.setValue(int(display.get("backlight", 80)))
        finally:
            for widget in blockers:
                widget.blockSignals(False)

    def _sync_source_box(self, current: int) -> None:
        if self.source_box.count() != len(self.device.channels):
            self.source_box.clear()
            for spec in self.device.channels.values():
                self.source_box.addItem(spec.name, spec.id)
        index = self.source_box.findData(current)
        if index >= 0:
            self.source_box.setCurrentIndex(index)

    def _apply_config(self, patch: dict) -> None:
        async def run() -> None:
            try:
                await self.device.set_config(patch)
            except Exception as exc:
                QMessageBox.warning(self, "Setting not applied",
                                    f"The board refused the change:\n\n{exc}")
                self._sync_from_config()

        asyncio.ensure_future(run())

    def _on_wires_changed(self) -> None:
        wires = int(self.wires_box.currentData())
        self.wires_note.setText(wire_mode_note(wires))
        self._apply_config({"rtd": {"wires": wires}})

    def _on_filter_changed(self) -> None:
        value = int(self.filter_box.currentData())
        self._apply_config({"rtd": {"filter_hz": value}, "tc": {"filter_hz": value}})

    def _on_rref_changed(self) -> None:
        self._apply_config({"rtd": {"rref_ohm": float(self.rref_input.value())}})

    def _on_tc_type_changed(self) -> None:
        self._apply_config({"tc": {"type": self.tc_type_box.currentData()}})

    def _on_cj_changed(self) -> None:
        self._apply_config({"tc": {"cj_source": self.cj_box.currentData()}})

    def _on_tc_avg_changed(self) -> None:
        self._apply_config({"tc": {"avg": int(self.tc_avg_box.currentData())}})

    def _on_rate_changed(self) -> None:
        self._apply_config({"acquire": {"rate_hz": float(self.rate_input.value())}})

    def _on_ring_changed(self) -> None:
        self._apply_config({"acquire": {"ring_seconds": int(self.ring_input.value())}})

    def _push_display(self) -> None:
        async def run() -> None:
            with contextlib.suppress(Exception):
                await self.device.set_display(
                    page=self.page_box.currentData(),
                    source=int(self.source_box.currentData() or 0),
                    window_s=int(self.display_window.currentData()),
                    rotation=int(self.rotation_box.currentData()),
                    backlight=int(self.backlight.value()),
                    units=self.app.settings.temperature_unit,
                )

        asyncio.ensure_future(run())

    async def _identify(self) -> None:
        with contextlib.suppress(Exception):
            await self.device.identify(5.0)

    async def _self_test(self) -> None:
        try:
            result = await self.device.run_self_test()
        except Exception as exc:
            self.diag_label.setText(f"Self-test failed to run: {exc}")
            return
        lines = []
        for check in result.get("checks", []):
            mark = "PASS" if check.get("pass") else "FAIL"
            lines.append(f"[{mark}] {check['name']:<16} {check.get('detail', '')}")
        verdict = "All checks passed." if result.get("pass") else "One or more checks FAILED."
        self.diag_label.setText(verdict + "\n\n" + "\n".join(lines))

    # ---------------------------------------------------------------- recording

    def _toggle_recording(self) -> None:
        serial = self.device.serial
        if self.app.recorder.is_recording(serial):
            async def stop() -> None:
                await self.app.recorder.stop(serial)
                self.recording_changed.emit()

            asyncio.ensure_future(stop())
        else:
            try:
                self.app.recorder.start(self.device)
            except RuntimeError as exc:
                QMessageBox.warning(self, "Cannot start recording", str(exc))
                return
            self.recording_changed.emit()
        self._refresh()

    def _open_calibration(self) -> None:
        if not self.device.is_online:
            QMessageBox.information(
                self, "Board offline",
                "Calibration writes to the board's memory, so the board has to be "
                "connected."
            )
            return
        dialog = CalibrationDialog(self.device, self.theme, self)
        dialog.exec()

    # ------------------------------------------------------------------ events

    def _on_event(self, event: DeviceEvent) -> None:
        if event.kind in ("config", "info"):
            self._sync_from_config()
        if event.kind == "info":
            self._rebuild_channels()

    def _refresh(self) -> None:
        device = self.device
        self.title.setText(device.label)
        self.pill.set_state(self.theme, device.state.value)
        self.pill.setToolTip(device.error or device.state.value.capitalize())

        self.table.update_values(device.latest_with_age())
        self.faults.update_faults(self.theme, device.faults(),
                                  device.status.get("flags") or [])
        self.links.update_links(self.theme, device.health()["links"])

        battery = device.status.get("battery") or {}
        if battery:
            charging = " ⚡" if battery.get("charging") else ""
            self.metric_battery.set_value(
                f"{battery.get('v', 0):.2f} V{charging}",
                f"{battery.get('pct', 0)}% — "
                + ("charging" if battery.get("charging") else "discharging"),
            )

        fit = device.timebase.fit
        if fit.n_points:
            self.metric_time.set_value(
                f"±{fit.uncertainty_ns / 1000:.0f} µs",
                device.timebase.quality_text(),
            )

        health = device.aggregator.health()
        rate = device._stream_rate_hz
        self.metric_stream.set_value(
            f"{rate:g} Hz",
            f"{health['rows']:,} rows · {health['lost']:,} lost · "
            f"{health['backfilled']:,} backfilled",
        )

        recording = next(
            (r for r in self.app.recorder.active if r.device.serial == device.serial), None
        )
        if recording:
            self.metric_recording.set_value(
                humanise_duration(recording.duration_s),
                f"{recording.stats.rows_written:,} rows written",
            )
            self.record_button.setText("Stop recording")
            self.record_button.setProperty("primary", False)
            self.record_button.setProperty("destructive", True)
        else:
            self.metric_recording.set_value("—")
            self.record_button.setText("Record")
            self.record_button.setProperty("primary", True)
            self.record_button.setProperty("destructive", False)
        self.record_button.style().polish(self.record_button)
        self.record_button.setEnabled(device.is_online)
        self.calibrate_button.setEnabled(device.is_online)

    def set_theme(self, theme: Theme) -> None:
        self.theme = theme
        self.table.set_theme(theme)
        self.live.set_theme(theme)
        for metric in (self.metric_battery, self.metric_time,
                       self.metric_stream, self.metric_recording):
            metric.set_theme(theme)

    def close_tab(self) -> None:
        self._timer.stop()
        self.live.stop()
        with contextlib.suppress(Exception):
            self._unsubscribe()
