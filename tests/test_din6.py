"""The DIN-6 board: extra channels, and the PT1000 simulator surface.

The DIN-6 speaks the same TJIP-1 as its predecessor with four channels added
and several omitted, so most of what is worth testing here is that the host
follows the *device's* declaration rather than its own default table.
"""

from __future__ import annotations

import json
import pathlib
import tempfile

import pytest

from tjiptemp.protocol import messages as M
from tjiptemp.protocol.channels import Ch, Kind, dash_for, spec_for, specs_from_device_info

#: What a DIN-6 announces: no battery, no AHT20, no raw microvolts, a fourth
#: external thermistor, and the three simulator channels.
DIN6_INFO = {
    "proto": 1,
    "model": "tjiptemp-din6",
    "caps": {
        "sim": {"pt1000_out": True, "codes": 256, "dither": True,
                "needs_loopback": True},
        "display_pages": ["overview", "single", "graph", "sim", "blank"],
    },
    "channels": [
        {"id": 0, "key": "pt1000", "name": "PT1000", "unit": "degC", "kind": "rtd"},
        {"id": 1, "key": "typek", "name": "Type K", "unit": "degC", "kind": "tc"},
        {"id": 2, "key": "typek_cj", "name": "Type K cold jn", "unit": "degC", "kind": "cj"},
        {"id": 3, "key": "ntc_ext1", "name": "NTC TEMP1", "unit": "degC", "kind": "ntc"},
        {"id": 6, "key": "ntc_brd_rtd", "name": "Board: RTD area", "unit": "degC", "kind": "ntc"},
        {"id": 13, "key": "pt1000_r", "name": "PT1000 raw", "unit": "ohm", "kind": "raw"},
        {"id": 15, "key": "ntc_ext4", "name": "NTC TEMP4", "unit": "degC", "kind": "ntc"},
        {"id": 16, "key": "sim_setpoint", "name": "Sim setpoint", "unit": "degC", "kind": "sim"},
        {"id": 17, "key": "sim_actual", "name": "Sim output", "unit": "degC", "kind": "sim"},
        {"id": 18, "key": "sim_r", "name": "Sim output raw", "unit": "ohm", "kind": "raw"},
    ],
}


def test_simulator_channels_are_known():
    for cid, key, unit in (
        (Ch.NTC_EXT4, "ntc_ext4", "degC"),
        (Ch.SIM_SETPOINT, "sim_setpoint", "degC"),
        (Ch.SIM_ACTUAL, "sim_actual", "degC"),
        (Ch.SIM_R, "sim_r", "ohm"),
    ):
        spec = spec_for(int(cid))
        assert spec.key == key
        assert spec.unit == unit


def test_setpoint_and_output_share_a_hue_and_separate_by_dash():
    # They are one quantity in two states, and are meant to be read against
    # each other; hue would say "two different subjects".
    assert spec_for(int(Ch.SIM_SETPOINT)).color == spec_for(int(Ch.SIM_ACTUAL)).color
    assert dash_for(int(Ch.SIM_SETPOINT)) == "dashed"
    assert dash_for(int(Ch.SIM_ACTUAL)) == "solid"


def test_board_thermistors_stay_recessive():
    # All three are diagnostics on either board, and never compete with a probe.
    for cid in (Ch.NTC_BRD_RTD, Ch.NTC_BRD_TC, Ch.NTC_BRD_CHG):
        assert dash_for(int(cid)) == "dashed"


def test_simulator_channels_are_not_calibratable():
    # They express what the board is presenting through its own measured wiper
    # table; a host-fitted correction on top would be fitting the same thing twice.
    for cid in (Ch.SIM_SETPOINT, Ch.SIM_ACTUAL, Ch.SIM_R):
        assert spec_for(int(cid)).cal_model is None


def test_device_declaration_wins_over_the_default_table():
    specs = specs_from_device_info(DIN6_INFO)
    assert set(specs) == {0, 1, 2, 3, 6, 13, 15, 16, 17, 18}
    # Omitted channels really are gone, not defaulted back in.
    assert int(Ch.V_BAT) not in specs
    assert int(Ch.AHT20_T) not in specs
    assert int(Ch.TYPEK_UV) not in specs
    # The board renames id 8's neighbourhood; names come from the device.
    assert specs[15].name == "NTC TEMP4"
    assert specs[17].kind == Kind.SIM


def test_simulator_channels_count_as_temperatures_for_plotting():
    specs = specs_from_device_info(DIN6_INFO)
    assert specs[16].is_temperature
    assert specs[17].is_temperature
    assert not specs[18].is_temperature   # ohms never share a temperature axis


def test_sim_message_ids_do_not_collide():
    ids = [v for k, v in vars(M.Msg).items() if isinstance(v, int) and not k.startswith("_")]
    assert len(ids) == len(set(ids))
    assert M.Msg.SIM_CALIBRATE == 0x68
    assert M.Msg.GET_SIM_CAL == 0x69
    assert M.Msg.SIM_CAL == 0x6A


def test_sim_calibrate_carries_our_clock():
    # The board has no RTC, so it stamps its table with the host's idea of now.
    frame = M.sim_calibrate(utc=1_700_000_000)
    assert frame.msg_type == M.Msg.SIM_CALIBRATE
    assert json.loads(frame.payload)["utc"] == 1_700_000_000

    bare = M.sim_calibrate()
    assert json.loads(bare.payload) == {}


def test_get_sim_cal_is_answered_by_sim_cal():
    assert M.RESPONSE_FOR[M.Msg.GET_SIM_CAL] == M.Msg.SIM_CAL


def test_sim_cal_is_unsolicited_because_a_sweep_is_broadcast():
    # A finished sweep goes to every session, not only whoever asked: another
    # host watching this board needs to know its table moved.
    assert M.Msg.SIM_CAL in M.UNSOLICITED


def test_sim_calibrate_acknowledges_itself():
    # It answers immediately that the sweep started; the table follows later.
    assert M.RESPONSE_FOR[M.Msg.SIM_CALIBRATE] == M.Msg.SIM_CALIBRATE


def test_unusable_wiper_codes_survive_json_as_null():
    # The board sends one entry per code with null where a code is unusable.
    # None must stay None: 0.0 would be a plausible resistance and is not one.
    table = {"ohms": [1200.0, None, 1180.5], "rev": 3}
    reloaded = json.loads(json.dumps(M.clean_floats(table)))
    assert reloaded["ohms"] == [1200.0, None, 1180.5]


@pytest.mark.parametrize("state", ["active", "cal_stale", "calibrating", "idle",
                                   "uncalibrated", "source_fault"])
def test_every_simulator_state_has_words_and_a_colour(state):
    from tjiptemp.ui.simpanel import STATE_TEXT, STATE_TONE

    # Status must never rest on colour alone, so both tables are total.
    assert state in STATE_TEXT and STATE_TEXT[state]
    assert state in STATE_TONE


# ------------------------------------------------ simulator sweep reporting

def test_failed_sweep_is_reported_not_swallowed():
    """A sweep that fails reports no verify points; the panel must still speak.

    The board answers a failed calibration with ``last_sweep: {ok: false,
    error: "loopback_absent"}`` and an empty ``verify`` list. The panel used to
    return early on the empty list, so a calibration that failed for a
    knowable reason looked exactly like one that never ran.
    """
    from tjiptemp.ui.simpanel import SWEEP_ERRORS

    # Every code the firmware can emit has a sentence here.
    firmware_codes = {"loopback_absent", "sweep_failed",
                      "nvs_write_failed", "out_of_memory"}
    assert firmware_codes <= set(SWEEP_ERRORS)
    for code, text in SWEEP_ERRORS.items():
        assert len(text) > 20, f"{code} needs a real explanation"


def test_panel_shows_the_sweep_error(qt_app_or_skip):

    from tjiptemp.device.device import Device
    from tjiptemp.ui.simpanel import SimulatorPanel
    from tjiptemp.ui.theme import resolve

    device = Device(serial="TJIP-SIMTEST0001", name="probe")
    device.info = {"caps": {"sim": {"pt1000_out": True}}}
    device.sim_cal = {
        "valid": False,
        "last_sweep": {"ok": False, "error": "loopback_absent", "verify": []},
    }
    panel = SimulatorPanel(device, resolve("dark"))
    try:
        panel.refresh()
        text = panel.verify_label.text()
        assert "failed" in text.lower()
        assert "loopback" in text.lower()
    finally:
        panel.close_panel()
        panel.deleteLater()


@pytest.fixture
def qt_app_or_skip():
    """A QApplication for the widget-level checks, or skip without PySide6."""
    pytest.importorskip("PySide6")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


# ------------------------------------------------- sensors tab capabilities

DIN6_SENSOR_CAPS = {
    "rtd_wires": [2],
    "rtd_rref": True,
    "tc_types": ["B", "E", "J", "K", "N", "R", "S", "T"],
    "tc_avg": [1, 2, 4, 8, 16],
    "cj_sources": ["internal"],
    "ring_seconds": False,
    "filter_hz": [50, 60],
}

SENSOR_CONFIG = {
    "rtd": {"wires": 2, "filter_hz": 50, "rref_ohm": 4300.0},
    "tc": {"type": "K", "avg": 4, "cj_source": "internal"},
    "acquire": {"rate_hz": 10.0, "ring_seconds": 600},
    "display": {"page": "overview", "source": 0, "sources": [],
                "window_s": 300, "backlight": 80, "rotation": 0},
}


def _tab(qt_app_or_skip, caps_sensors):
    from tjiptemp.core.application import Application, Settings
    from tjiptemp.device.device import Device
    from tjiptemp.ui.devicetab import DeviceTab
    from tjiptemp.ui.theme import resolve

    caps = {"ring_samples": 6000, "max_rate_hz": 100}
    if caps_sensors is not None:
        caps["sensors"] = caps_sensors
    device = Device(serial="TJIP-SENSORS0001", name="probe")
    device.info = {"model": "tjiptemp-din6", "channels": [], "caps": caps}
    device.config = SENSOR_CONFIG
    settings = Settings(api_enabled=False, auto_connect_usb=False)
    # A fresh temporary database per call: an absolute path would collide
    # between tests and would not exist on another machine or on CI.
    app = Application(settings,
                      db_path=pathlib.Path(tempfile.mkdtemp()) / "sensorstest.tjip")
    tab = DeviceTab(app, device, resolve("dark"))
    tab._sync_from_config()
    return tab


def _items(box):
    return [box.itemData(i) for i in range(box.count())]


def test_sensors_tab_locks_controls_the_board_cannot_honour(qt_app_or_skip):
    """A control that stores a value and does nothing is worse than no control.

    DIN-6 has no RTD wiring mux and uses the converter's own cold junction, so
    those two offered choices the firmware silently ignored.
    """
    tab = _tab(qt_app_or_skip, DIN6_SENSOR_CAPS)
    try:
        assert _items(tab.wires_box) == [2]
        assert not tab.wires_box.isEnabled()
        assert "no switching network" in tab.wires_note.text()

        assert _items(tab.cj_box) == ["internal"]
        assert not tab.cj_box.isEnabled()

        assert not tab.ring_input.isEnabled()
        assert "Fixed at" in tab.ring_note.text()
    finally:
        tab.close_tab()


def test_sensors_tab_offers_what_the_board_does_support(qt_app_or_skip):
    tab = _tab(qt_app_or_skip, DIN6_SENSOR_CAPS)
    try:
        assert _items(tab.tc_type_box) == list("BEJKNRST")
        assert tab.tc_type_box.isEnabled()
        assert _items(tab.tc_avg_box) == [1, 2, 4, 8, 16]
        assert tab.rref_input.isEnabled()
        assert tab.rref_input.value() == pytest.approx(4300.0)
    finally:
        tab.close_tab()


def test_sensors_tab_unchanged_on_firmware_without_caps(qt_app_or_skip):
    """Older firmware predates the distinction; it keeps the old behaviour."""
    tab = _tab(qt_app_or_skip, None)
    try:
        assert _items(tab.wires_box) == [2, 3, 4]
        assert tab.wires_box.isEnabled()
        assert _items(tab.cj_box) == ["internal", "ntc_brd_tc", "fixed"]
        assert tab.cj_box.isEnabled()
        assert tab.ring_input.isEnabled()
    finally:
        tab.close_tab()


def test_repopulate_keeps_the_selection_and_is_a_noop_when_equal(qt_app_or_skip):
    from PySide6.QtWidgets import QComboBox

    from tjiptemp.ui.devicetab import _repopulate

    box = QComboBox()
    entries = [("a", 1), ("b", 2), ("c", 3)]
    _repopulate(box, entries)
    box.setCurrentIndex(2)
    _repopulate(box, entries)          # identical list: must not reset
    assert box.currentData() == 3
    _repopulate(box, [("b", 2), ("c", 3)])
    assert box.currentData() == 3      # survived the shorter list
    _repopulate(box, [("z", 9)])
    assert box.currentData() == 9      # gone: falls back to what is there
