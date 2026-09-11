"""One tab per board: live charts, channel table, board settings, screen control.

Everything a user does to a *single* board lives here. The main window owns the
list of boards and anything that spans them.
"""

from __future__ import annotations

import asyncio
import contextlib

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
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
from .calibration import CalibrationPanel
from .liveview import LiveView
from .simpanel import SimulatorPanel
from .theme import Theme
from .widgets import (
    ChannelTable,
    FaultBanner,
    LinkBar,
    MetricLabel,
    StatusPill,
    humanise_duration,
    message_later,
)
from .wifidialog import WifiDialog

#: Human labels for the page names a board reports in ``caps.display_pages``.
#: A board is authoritative about what it implements — asking for a page it
#: does not have earns ERROR "unsupported", so the list is built from what it
#: says rather than from what firmware used to have.
PAGE_LABELS = {
    "overview": "Overview — all channels",
    "single": "Single channel, large",
    "graph": "Graph",
    "sim": "Simulator",
    "status": "Status",
    "blank": "Blank",
}

#: Used only for a board that reports no page list at all.
FALLBACK_PAGES = ("overview", "single", "blank")

#: How many traces a board's graph page accepts (spec §10 ``sources``).
MAX_GRAPH_TRACES = 3


#: Cold-junction source codes and what they mean to a person.
CJ_LABELS = {
    "internal": "MAX31856 internal sensor",
    "ntc_brd_tc": "Board NTC beside the connector",
    "fixed": "Fixed value",
}


def _repopulate(box, entries: list[tuple[str, object]]) -> None:
    """Refill a combo box, keeping the current selection where it survives.

    Called from inside the blockSignals window in _sync_from_config, so
    rebuilding the list never looks like the user changing it -- which would
    push a config patch back at the board for a value it just reported.
    """
    wanted = box.currentData()
    if [(box.itemText(i), box.itemData(i)) for i in range(box.count())] == entries:
        return
    box.clear()
    for label, value in entries:
        box.addItem(label, value)
    index = box.findData(wanted)
    if index >= 0:
        box.setCurrentIndex(index)


class DeviceTab(QWidget):
    """The full view of one board."""

    recording_changed = Signal()

    def __init__(self, app, device: Device, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self.device = device
        self.theme = theme
        self._unsubscribe = device.subscribe(self._on_event)
        #: Set while the board-screen controls are being filled in from CONFIG,
        #: so populating them does not push the values straight back.
        self._loading_display = True

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        root.addLayout(self._build_header())
        self.faults = FaultBanner()
        root.addWidget(self.faults)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.live = LiveView(device, theme)
        splitter.addWidget(self.live)

        self.side = side = QTabWidget()
        side.addTab(self._build_channels_tab(), "Channels")
        side.addTab(self._build_settings_tab(), "Sensors")
        self.calibration = CalibrationPanel(device, theme)
        side.addTab(self.calibration, "Calibration")
        side.addTab(self._build_display_tab(), "Board screen")
        side.addTab(self._build_diagnostics_tab(), "Diagnostics")
        #: Added when DEVICE_INFO says this board emulates a PT1000. It arrives
        #: after construction, so the tab cannot simply be built here.
        self.sim_panel: SimulatorPanel | None = None
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
        # The handshake fetched CONFIG before this tab existed, so the "config"
        # event that fills the controls has already been and gone. Without this
        # the Page and Channel lists stayed empty, and the sensor controls
        # showed defaults rather than the board's values, until some unrelated
        # change happened to fetch CONFIG again.
        self._sync_from_config()
        self._loading_display = False
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

        self.wifi_button = QPushButton("WiFi…")
        self.wifi_button.setToolTip(
            "Give this board WiFi credentials. It stores them and reconnects on "
            "its own, so it can be used with nothing plugged in."
        )
        self.wifi_button.clicked.connect(self._open_wifi)
        layout.addWidget(self.wifi_button)
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

    def _sensor_caps(self) -> dict:
        """What this board says it can actually act on.

        Firmware without ``caps.sensors`` predates the distinction, so the
        fallbacks are the old behaviour: offer everything. A board that does
        advertise gets its controls shaped to what it will honour.
        """
        caps = (self.device.info.get("caps") or {}).get("sensors")
        if not isinstance(caps, dict):
            return {}
        return caps

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
            "The board has no buttons, so its screen is driven entirely from here. "
            "These settings persist on the board and survive a reboot with no "
            "computer attached."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {self.theme.text_secondary};")
        layout.addWidget(hint)

        form = QFormLayout()
        self.page_box = QComboBox()
        self.page_box.currentIndexChanged.connect(self._on_page_changed)
        form.addRow("Page", self.page_box)

        self.source_box = QComboBox()
        self.source_box.currentIndexChanged.connect(self._push_display)
        self.source_row_label = QLabel("Channel")
        form.addRow(self.source_row_label, self.source_box)

        # Graph traces. A list of checkboxes rather than three combos: the
        # board takes a set, the order does not matter to it, and a set is
        # what the user is actually choosing.
        self.trace_list = QListWidget()
        self.trace_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.trace_list.setMaximumHeight(140)
        self.trace_list.itemChanged.connect(self._on_traces_changed)
        self.trace_row_label = QLabel("Traces")
        form.addRow(self.trace_row_label, self.trace_list)

        self.trace_hint = QLabel("")
        self.trace_hint.setWordWrap(True)
        self.trace_hint.setStyleSheet(f"color: {self.theme.text_secondary};")
        form.addRow("", self.trace_hint)

        self.display_window = QComboBox()
        for label, seconds in (("1 min", 60), ("5 min", 300),
                               ("15 min", 900), ("1 hour", 3600)):
            self.display_window.addItem(label, seconds)
        self.display_window.setCurrentIndex(1)
        self.display_window.currentIndexChanged.connect(self._push_display)
        self.window_row_label = QLabel("Graph window")
        form.addRow(self.window_row_label, self.display_window)

        self.rotation_box = QComboBox()
        for label, value in (("0°", 0), ("90°", 90), ("180°", 180), ("270°", 270)):
            self.rotation_box.addItem(label, value)
        self.rotation_box.currentIndexChanged.connect(self._push_display)
        form.addRow("Rotation", self.rotation_box)

        self.backlight = QSlider(Qt.Orientation.Horizontal)
        self.backlight.setRange(0, 100)
        self.backlight.setValue(80)
        # On release after a drag, and on a click in the groove or a key press,
        # which move the value without any release -- those used to go nowhere.
        # Not on every step of a drag: each push is an NVS write on the board.
        self.backlight.sliderReleased.connect(self._push_display)
        self.backlight.valueChanged.connect(
            lambda _value: None if self.backlight.isSliderDown() else self._push_display()
        )
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
        self._update_display_rows()
        return panel

    def _sync_page_box(self, current: str) -> None:
        """Offer exactly the pages this board says it implements."""
        pages = tuple(self.device.info.get("caps", {}).get("display_pages")
                      or FALLBACK_PAGES)
        if tuple(self.page_box.itemData(i) for i in range(self.page_box.count())) == pages:
            index = self.page_box.findData(current)
            if index >= 0 and index != self.page_box.currentIndex():
                self.page_box.setCurrentIndex(index)
            return
        blocked = self.page_box.blockSignals(True)
        self.page_box.clear()
        for name in pages:
            self.page_box.addItem(PAGE_LABELS.get(name, name.title()), name)
        index = self.page_box.findData(current)
        if index >= 0:
            self.page_box.setCurrentIndex(index)
        self.page_box.blockSignals(blocked)

    def _sync_trace_list(self, chosen: list[int]) -> None:
        """Rebuild the trace checkboxes, preserving what is ticked."""
        want = [(spec.id, spec.name) for spec in self.device.channels.values()
                if spec.is_temperature or spec.unit == "ohm"]
        have = [(self.trace_list.item(i).data(Qt.ItemDataRole.UserRole),
                 self.trace_list.item(i).text()) for i in range(self.trace_list.count())]
        blocked = self.trace_list.blockSignals(True)
        if want != have:
            self.trace_list.clear()
            for cid, name in want:
                item = QListWidgetItem(name)
                item.setData(Qt.ItemDataRole.UserRole, cid)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Unchecked)
                self.trace_list.addItem(item)
        for i in range(self.trace_list.count()):
            item = self.trace_list.item(i)
            cid = item.data(Qt.ItemDataRole.UserRole)
            item.setCheckState(Qt.CheckState.Checked if cid in chosen
                               else Qt.CheckState.Unchecked)
        self.trace_list.blockSignals(blocked)
        self._update_trace_hint()

    def _checked_traces(self) -> list[int]:
        return [self.trace_list.item(i).data(Qt.ItemDataRole.UserRole)
                for i in range(self.trace_list.count())
                if self.trace_list.item(i).checkState() == Qt.CheckState.Checked]

    def _update_trace_hint(self) -> None:
        chosen = self._checked_traces()
        if not chosen:
            self.trace_hint.setText(
                f"Nothing ticked — the board plots whatever drives it. "
                f"Up to {MAX_GRAPH_TRACES}."
            )
        else:
            self.trace_hint.setText(
                f"{len(chosen)} of {MAX_GRAPH_TRACES}. The board autoscales them "
                "together, so mixing units makes a misleading plot."
            )

    def _on_traces_changed(self, item: QListWidgetItem) -> None:
        """Hold the selection to what the board can actually draw."""
        if item.checkState() == Qt.CheckState.Checked:
            chosen = self._checked_traces()
            if len(chosen) > MAX_GRAPH_TRACES:
                blocked = self.trace_list.blockSignals(True)
                item.setCheckState(Qt.CheckState.Unchecked)
                self.trace_list.blockSignals(blocked)
                self._update_trace_hint()
                return
        self._update_trace_hint()
        self._push_display()

    def _on_page_changed(self) -> None:
        self._update_display_rows()
        self._push_display()

    def _update_display_rows(self) -> None:
        """Show only the controls the selected page actually uses."""
        page = self.page_box.currentData() or "overview"
        single = page == "single"
        graph = page == "graph"
        for widget in (self.source_row_label, self.source_box):
            widget.setVisible(single)
        for widget in (self.trace_row_label, self.trace_list, self.trace_hint):
            widget.setVisible(graph)
        for widget in (self.window_row_label, self.display_window):
            widget.setVisible(graph)

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
            # A board whose CONFIG never arrived still has pages and channels
            # to offer; an empty list is not something anyone can pick from.
            blocked = self.source_box.blockSignals(True)
            try:
                self._sync_page_box(self.page_box.currentData() or "overview")
                self._sync_source_box(int(self.source_box.currentData() or 0))
                self._update_display_rows()
            finally:
                self.source_box.blockSignals(blocked)
            return
        blockers = (self.wires_box, self.filter_box, self.rref_input, self.tc_type_box,
                    self.cj_box, self.tc_avg_box, self.rate_input, self.ring_input,
                    self.page_box, self.source_box, self.rotation_box,
                    self.display_window, self.backlight)
        for widget in blockers:
            widget.blockSignals(True)
        try:
            caps = self._sensor_caps()

            rtd = config.get("rtd", {})
            wires = int(rtd.get("wires", 4))
            allowed_wires = caps.get("rtd_wires")
            if allowed_wires:
                _repopulate(self.wires_box,
                            [(f"{n}-wire", int(n)) for n in allowed_wires])
                fixed = len(allowed_wires) == 1
                self.wires_box.setEnabled(not fixed)
                if fixed:
                    self.wires_note.setText(
                        f"This board's PT1000 terminals are wired for "
                        f"{int(allowed_wires[0])}-wire and there is no switching "
                        f"network, so the mode is fixed."
                    )
            self.wires_box.setCurrentIndex(max(0, self.wires_box.findData(wires)))
            if not allowed_wires or len(allowed_wires) > 1:
                self.wires_note.setText(wire_mode_note(wires))
            self.filter_box.setCurrentIndex(
                max(0, self.filter_box.findData(int(rtd.get("filter_hz", 50))))
            )
            rref = float(rtd.get("rref_ohm", 4000.0))
            self.rref_input.setValue(rref)
            if caps and not caps.get("rtd_rref", True):
                self.rref_input.setEnabled(False)
                self.rref_input.setToolTip(
                    "This board's reference resistor is fixed in firmware."
                )
            self.resolution_note.setText(
                f"One converter step is {resolution_c(1000.0, rref, 25.0) * 1000:.1f} mK "
                f"at 25 °C — noise below that is not real."
            )

            tc = config.get("tc", {})
            if caps.get("tc_types"):
                from ..sensors.thermocouple import SUPPORTED_TYPES
                _repopulate(self.tc_type_box, [
                    (f"Type {k}" + ("" if k in SUPPORTED_TYPES
                                    else "  (chip linearisation)"), k)
                    for k in caps["tc_types"]
                ])
            self.tc_type_box.setCurrentIndex(
                max(0, self.tc_type_box.findData(tc.get("type", "K")))
            )

            allowed_cj = caps.get("cj_sources")
            if allowed_cj:
                _repopulate(self.cj_box,
                            [(CJ_LABELS.get(k, k), k) for k in allowed_cj])
                fixed = len(allowed_cj) == 1
                self.cj_box.setEnabled(not fixed)
                if fixed:
                    self.cj_box.setToolTip(
                        "The converter's own cold junction is the reference on "
                        "this board. The NTC beside the connector cross-checks "
                        "it and warns if the two disagree."
                    )
            self.cj_box.setCurrentIndex(
                max(0, self.cj_box.findData(tc.get("cj_source", "internal")))
            )

            if caps.get("tc_avg"):
                _repopulate(self.tc_avg_box, [
                    (f"{int(n)} sample{'s' if int(n) > 1 else ''}", int(n))
                    for n in caps["tc_avg"]
                ])
            self.tc_avg_box.setCurrentIndex(
                max(0, self.tc_avg_box.findData(int(tc.get("avg", 4))))
            )

            acquire = config.get("acquire", {})
            self.rate_input.setMaximum(self.device.max_rate_hz)
            self.rate_input.setValue(float(acquire.get("rate_hz", 10.0)))
            ring_seconds = int(acquire.get("ring_seconds", 600))
            self.ring_input.setValue(ring_seconds)
            rows = int(self.device.ring_samples)
            adjustable = caps.get("ring_seconds", True) if caps else True
            self.ring_input.setEnabled(bool(adjustable))
            rate = float(acquire.get("rate_hz", 10.0)) or 10.0
            if adjustable:
                self.ring_note.setText(
                    f"{rows:,} rows on the board. If a link drops, this is how far "
                    f"back the missing samples can still be recovered from."
                )
            else:
                self.ring_note.setText(
                    f"Fixed at {rows:,} rows — one allocation made when the board "
                    f"boots. At {rate:g} Hz that is about {rows / rate / 60:.0f} "
                    f"minutes of history to recover a dropped link from."
                )

            display = config.get("display", {})
            self._sync_page_box(display.get("page", "overview"))
            self._sync_source_box(int(display.get("source", 0)))
            sources = display.get("sources") or []
            self._sync_trace_list([int(x) for x in sources])
            window_index = self.display_window.findData(int(display.get("window_s", 300)))
            if window_index >= 0:
                self.display_window.setCurrentIndex(window_index)
            self.rotation_box.setCurrentIndex(
                max(0, self.rotation_box.findData(int(display.get("rotation", 0))))
            )
            self.backlight.setValue(int(display.get("backlight", 80)))
            self._update_display_rows()
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
                message_later("warning", self, "Setting not applied",
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
        if self._loading_display:
            return
        page = self.page_box.currentData() or "overview"
        payload: dict = {
            "page": page,
            "rotation": int(self.rotation_box.currentData()),
            "backlight": int(self.backlight.value()),
            "units": self.app.settings.temperature_unit,
        }
        # Only what the page uses. Sending a stale `sources` alongside a
        # single-channel page would silently reset the graph selection the
        # next time the user switched back to it.
        if page == "single":
            payload["source"] = int(self.source_box.currentData() or 0)
        elif page == "graph":
            traces = self._checked_traces()
            payload["window_s"] = int(self.display_window.currentData())
            # Sent even when empty: leaving it out kept the board's last
            # selection, so unticking the last trace was undone the moment the
            # board's CONFIG came back.
            payload["sources"] = traces
            if traces:
                payload["source"] = int(traces[0])

        async def run() -> None:
            with contextlib.suppress(Exception):
                await self.device.set_display(**payload)

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

    def _sync_simulator_tab(self) -> None:
        if self.sim_panel is not None or not self.device.has_simulator:
            return
        self.sim_panel = SimulatorPanel(self.device, self.theme)
        # Second, right after Channels: on this board the simulator is the
        # point, not an accessory.
        self.side.insertTab(1, self.sim_panel, "Simulator")

    def _open_wifi(self) -> None:
        dialog = WifiDialog(self.device, self.theme, self)
        try:
            dialog.exec()
        finally:
            # Parented to the tab, so nothing else would ever destroy it: each
            # open would leave another hidden dialog behind, polling STATUS.
            dialog.deleteLater()

    # ------------------------------------------------------------------ events

    def _on_event(self, event: DeviceEvent) -> None:
        if event.kind in ("config", "info"):
            self._sync_from_config()
        if event.kind == "info":
            self._rebuild_channels()
            self.calibration.refresh_channels()

    def _refresh(self) -> None:
        device = self.device
        self._sync_simulator_tab()
        self.wifi_button.setEnabled(device.is_online)
        self.title.setText(device.label)
        self.pill.set_state(self.theme, device.state.value)
        self.pill.setToolTip(device.error or device.state.value.capitalize())

        self.table.update_values(device.latest_with_age())
        self.faults.update_faults(self.theme, device.faults(),
                                  device.status.get("flags") or [])
        self.links.update_links(self.theme, device.health()["links"])

        battery = device.status.get("battery") or {}
        self.metric_battery.setVisible(bool(battery))
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
        # The simulator panel holds its own subscription. Without this the
        # device keeps a reference to a bound method of a widget that is on its
        # way out, so the panel is never freed and goes on rebuilding itself on
        # every event -- once more per reconnect, for the life of the process.
        if self.sim_panel is not None:
            self.sim_panel.close_panel()
            self.sim_panel = None
        self.calibration.close_panel()
