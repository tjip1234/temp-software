"""A virtual TjipTemp board.

This exists so the desktop software can be developed and tested before the
firmware exists, and so the test suite can exercise reconnection, backfill and
calibration without anyone plugging anything in. It implements the device side of
TJIP-1 completely enough that the real firmware can be checked against it.

The physics is deliberately not a toy: each channel has a thermal time constant,
sensors are simulated at the *physical* level (a PT1000's resistance, a
thermocouple's microvolts) and then converted using the same reference functions
the host uses, with a deliberate error injected so that calibration has something
real to correct. Noise is per-sensor and roughly matches the converters -- the
MAX31865 at 16-bit with a 4k reference gives about 30 mK of quantisation, the
ESP32 ADC on an NTC divider gives rather more.
"""

from __future__ import annotations

import math
import random
import time
import uuid
from dataclasses import dataclass

import numpy as np

from ..protocol import messages as M
from ..protocol.channels import Ch
from ..protocol.framing import FLAG_MORE, FLAG_RESPONSE, Frame
from ..sensors import ntc as ntc_mod
from ..sensors import rtd as rtd_mod
from ..sensors import thermocouple as tc_mod

CHANNEL_TABLE = [
    {"id": 0, "key": "pt1000", "name": "PT1000", "unit": "degC", "kind": "rtd"},
    {"id": 1, "key": "typek", "name": "Type K", "unit": "degC", "kind": "tc"},
    {"id": 2, "key": "typek_cj", "name": "Type K cold jn", "unit": "degC", "kind": "cj"},
    {"id": 3, "key": "ntc_ext1", "name": "NTC CN1", "unit": "degC", "kind": "ntc"},
    {"id": 4, "key": "ntc_ext2", "name": "NTC CN2", "unit": "degC", "kind": "ntc"},
    {"id": 5, "key": "ntc_ext3", "name": "NTC CN3", "unit": "degC", "kind": "ntc"},
    {"id": 6, "key": "ntc_brd_rtd", "name": "Board: RTD area", "unit": "degC", "kind": "ntc"},
    {"id": 7, "key": "ntc_brd_tc", "name": "Board: TC area", "unit": "degC", "kind": "ntc"},
    {"id": 8, "key": "ntc_brd_chg", "name": "Board: charger", "unit": "degC", "kind": "ntc"},
    {"id": 9, "key": "v_bat", "name": "Battery", "unit": "V", "kind": "volt"},
    {"id": 10, "key": "v_cc", "name": "VCC", "unit": "V", "kind": "volt"},
    {"id": 11, "key": "aht20_t", "name": "AHT20 temp", "unit": "degC", "kind": "hygro"},
    {"id": 12, "key": "aht20_rh", "name": "AHT20 RH", "unit": "%RH", "kind": "hygro"},
    {"id": 13, "key": "pt1000_r", "name": "PT1000 raw", "unit": "ohm", "kind": "raw"},
    {"id": 14, "key": "typek_uv", "name": "Type K raw", "unit": "uV", "kind": "raw"},
]
CHANNEL_IDS: tuple[int, ...] = tuple(c["id"] for c in CHANNEL_TABLE)

DEFAULT_CONFIG = {
    "rtd": {"wires": 4, "filter_hz": 50, "bias_mode": "auto", "rref_ohm": 4000.0,
            "fault_thresholds": {"high_ohm": 4200.0, "low_ohm": 200.0}},
    "tc": {"type": "K", "avg": 4, "filter_hz": 50, "cj_source": "internal",
           "cj_fixed_c": 0.0, "open_detect": True},
    "ntc": {
        "ext": [{"enabled": True, "r_series_ohm": 10000.0, "v_ref": 3.3, "pullup_to": "vcc"}] * 3,
        "board": [{"r_series_ohm": 10000.0}] * 3,
        "adc_oversample": 64,
    },
    "power": {"vbat_divider": 2.0, "vcc_divider": 2.0,
              "low_battery_v": 3.40, "critical_battery_v": 3.20},
    "aht20": {"enabled": True, "rate_hz": 1},
    "acquire": {"rate_hz": 10, "ring_seconds": 600, "autostart": True},
    "display": {"page": "overview", "source": 0, "backlight": 80, "rotation": 0, "timeout_s": 0},
    "net": {"hostname": "tjiptemp-sim", "mdns": True, "tcp_port": M.DEFAULT_TCP_PORT},
}


@dataclass
class ThermalNode:
    """A first-order thermal mass chasing a target temperature."""

    value: float
    target: float
    tau_s: float = 30.0
    noise_c: float = 0.01
    #: Slow random walk of the target, so nothing is ever perfectly static.
    wander_c: float = 0.0

    def step(self, dt: float) -> float:
        if self.wander_c:
            self.target += random.gauss(0.0, self.wander_c * math.sqrt(dt))
        alpha = 1.0 - math.exp(-dt / max(self.tau_s, 1e-3))
        self.value += (self.target - self.value) * alpha
        return self.value + random.gauss(0.0, self.noise_c)


@dataclass
class SimulatedErrors:
    """Deliberate, *fixed* sensor errors for calibration to discover and remove.

    These are what makes the simulator useful for testing the calibration wizard:
    run the wizard against a simulated board and the fitted coefficients should
    recover these numbers. If they do not, the fitting is wrong.
    """

    pt1000_r0_error: float = 0.42        # ohms: a real PT1000 is rarely exactly 1000.000
    pt1000_lead_ohm: float = 0.0         # only visible in 2-wire mode
    cj_offset_c: float = 0.37            # the MAX31856's internal sensor reads high
    tc_uv_offset: float = 4.1            # amplifier offset and parasitic junctions
    ntc_beta_error: float = 18.0         # probe-to-probe beta spread
    aht20_t_offset: float = -0.24
    aht20_rh_offset: float = 1.9
    vbat_gain: float = 0.9987
    vcc_gain: float = 1.0009


class SimulatedBoard:
    """The device-side state machine and physics model."""

    def __init__(
        self,
        serial: str | None = None,
        *,
        rate_hz: float = 10.0,
        ring_seconds: float = 600.0,
        seed: int | None = None,
        errors: SimulatedErrors | None = None,
        scenario: str = "ambient",
    ) -> None:
        if seed is not None:
            random.seed(seed)
            np.random.seed(seed)
        tail = (serial or uuid.uuid4().hex)[-12:].upper()
        self.serial = serial or f"TJIP-SIM{tail}"
        self.boot_id = uuid.uuid4().hex[:4]
        self.t0_wall = time.monotonic()
        self.errors = errors or SimulatedErrors()
        self.scenario = scenario

        self.config = _deep_copy(DEFAULT_CONFIG)
        self.config["acquire"]["rate_hz"] = rate_hz
        self.config["acquire"]["ring_seconds"] = ring_seconds
        self.config["net"]["hostname"] = f"tjiptemp-{self.serial[-4:].lower()}"

        from ..calibration.models import default_calibration

        self.calibration = default_calibration().to_json()
        self.cal_rev = 0

        self.rate_hz = rate_hz
        self.dt_us = int(round(1e6 / rate_hz))
        self.ring_capacity = max(1000, int(ring_seconds * rate_hz))
        self._ring = np.full((self.ring_capacity, len(CHANNEL_IDS)), np.nan, dtype=np.float32)
        self._ring_seq0 = 0        # sequence of row 0 of the ring
        self._ring_t0_us = 0
        self._next_seq = 0
        self._cursor = 0
        self._filled = 0

        self.leds = [False, False, False]
        self.identify_until = 0.0
        self.faults: dict[str, dict | None] = {"max31865": None, "max31856": None, "aht20": None}
        self.wifi = {"state": "connected", "ip": "192.168.1.44", "rssi": -57}
        #: Simulated crystal error, so the host's drift fit has something to find.
        self.clock_ppm = random.uniform(-40.0, 40.0)

        self._build_model()
        self._last_step_us = 0

    # ------------------------------------------------------------------ model

    def _build_model(self) -> None:
        ambient = 21.5
        if self.scenario == "oven":
            probe_target, probe_tau = 180.0, 90.0
        elif self.scenario == "fridge":
            probe_target, probe_tau = 4.0, 120.0
        elif self.scenario == "ramp":
            probe_target, probe_tau = 80.0, 300.0
        else:
            probe_target, probe_tau = ambient + 1.5, 45.0

        self.nodes = {
            Ch.PT1000: ThermalNode(ambient, probe_target, probe_tau, 0.004, 0.02),
            Ch.TYPEK: ThermalNode(ambient, probe_target, probe_tau * 0.4, 0.08, 0.03),
            Ch.NTC_EXT1: ThermalNode(ambient, ambient + 0.4, 20.0, 0.02, 0.01),
            Ch.NTC_EXT2: ThermalNode(ambient, ambient - 0.3, 25.0, 0.02, 0.01),
            Ch.NTC_EXT3: ThermalNode(ambient, ambient + 2.0, 35.0, 0.02, 0.01),
            Ch.NTC_BRD_RTD: ThermalNode(ambient + 8.0, ambient + 9.5, 120.0, 0.02, 0.005),
            Ch.NTC_BRD_TC: ThermalNode(ambient + 7.0, ambient + 8.4, 120.0, 0.02, 0.005),
            Ch.NTC_BRD_CHG: ThermalNode(ambient + 15.0, ambient + 23.0, 200.0, 0.03, 0.01),
            Ch.AHT20_T: ThermalNode(ambient + 5.0, ambient + 6.0, 90.0, 0.03, 0.005),
        }
        self.rh = ThermalNode(43.0, 45.0, 200.0, 0.15, 0.05)
        self.v_bat = 4.02
        self.charging = True

    def _step_physics(self, dt: float) -> dict[int, float]:
        """Advance one tick and return the *true* physical values per channel."""
        truth = {cid: node.step(dt) for cid, node in self.nodes.items()}

        # Cold junction tracks the board temperature near the TC connector, which is
        # the whole reason that NTC exists on the PCB.
        truth[Ch.TYPEK_CJ] = truth[Ch.NTC_BRD_TC] + 0.15

        # Battery: charges toward 4.2 V, then discharges. The charger warms the board.
        if self.charging:
            self.v_bat = min(4.20, self.v_bat + 0.00004 * dt * 60)
            if self.v_bat >= 4.19:
                self.charging = False
        else:
            self.v_bat = max(3.30, self.v_bat - 0.00002 * dt * 60)
            if self.v_bat <= 3.45:
                self.charging = True
        truth[Ch.V_BAT] = self.v_bat + random.gauss(0, 0.0008)
        truth[Ch.V_CC] = 4.98 + random.gauss(0, 0.002)
        truth[Ch.AHT20_RH] = self.rh.step(dt)
        return truth

    def _measure(self, truth: dict[int, float]) -> np.ndarray:
        """Turn true temperatures into what the hardware would actually report.

        Physical simulation, then the sensor's own errors, then the converter's
        quantisation -- in that order, because that is the order reality applies
        them and it is what makes calibration meaningful.
        """
        err = self.errors
        row = np.full(len(CHANNEL_IDS), np.nan, dtype=np.float32)

        # --- PT1000 through the MAX31865
        r_true = float(rtd_mod.resistance(truth[Ch.PT1000], r0=1000.0 + err.pt1000_r0_error))
        if self.config["rtd"]["wires"] == 2:
            r_true += err.pt1000_lead_ohm
        rref = float(self.config["rtd"]["rref_ohm"])
        code = rtd_mod.ohm_to_adc_code(r_true, rref)
        r_meas = rtd_mod.adc_code_to_ohm(code, rref) + random.gauss(0, rref / 32768.0 * 0.4)
        row[Ch.PT1000_R] = r_meas
        row[Ch.PT1000] = rtd_mod.temperature(r_meas, r0=1000.0)

        # --- Type K through the MAX31856
        cj_reported = truth[Ch.TYPEK_CJ] + err.cj_offset_c + random.gauss(0, 0.015)
        uv_true = float(tc_mod.emf_uv(truth[Ch.TYPEK])) - float(tc_mod.emf_uv(truth[Ch.TYPEK_CJ]))
        uv_meas = uv_true + err.tc_uv_offset + random.gauss(0, 0.6)
        row[Ch.TYPEK_UV] = uv_meas
        row[Ch.TYPEK_CJ] = cj_reported
        row[Ch.TYPEK] = tc_mod.temperature(uv_meas, cj_reported)

        # --- NTCs through the ESP32 ADC
        ntc_map = {
            Ch.NTC_EXT1: 0, Ch.NTC_EXT2: 1, Ch.NTC_EXT3: 2,
            Ch.NTC_BRD_RTD: 3, Ch.NTC_BRD_TC: 4, Ch.NTC_BRD_CHG: 5,
        }
        for cid, index in ntc_map.items():
            beta = ntc_mod.DEFAULT_BETA + err.ntc_beta_error * (1 if index % 2 == 0 else -1)
            r = float(ntc_mod.resistance_beta(truth[cid], beta=beta))
            v = float(ntc_mod.divider_voltage(r, 3.3, 10000.0))
            # 12-bit ADC with oversampling: quantisation shrinks as sqrt(oversample).
            oversample = max(1, int(self.config["ntc"]["adc_oversample"]))
            lsb = 3.3 / 4096.0 / math.sqrt(oversample)
            v_meas = v + random.gauss(0, lsb)
            r_meas_ntc = float(ntc_mod.divider_resistance(v_meas, 3.3, 10000.0))
            row[cid] = ntc_mod.temperature_steinhart(r_meas_ntc)

        row[Ch.V_BAT] = truth[Ch.V_BAT] * err.vbat_gain
        row[Ch.V_CC] = truth[Ch.V_CC] * err.vcc_gain
        row[Ch.AHT20_T] = truth[Ch.AHT20_T] + err.aht20_t_offset + random.gauss(0, 0.01)
        row[Ch.AHT20_RH] = truth[Ch.AHT20_RH] + err.aht20_rh_offset + random.gauss(0, 0.05)

        self._apply_injected_faults(row)
        return row

    def _apply_injected_faults(self, row: np.ndarray) -> None:
        for name, fault in self.faults.items():
            if not fault:
                continue
            if name == "max31865":
                row[Ch.PT1000] = np.nan
                row[Ch.PT1000_R] = np.nan
            elif name == "max31856":
                row[Ch.TYPEK] = np.nan
                row[Ch.TYPEK_UV] = np.nan
            elif name == "aht20":
                row[Ch.AHT20_T] = np.nan
                row[Ch.AHT20_RH] = np.nan

    # ---------------------------------------------------------------- clocking

    def now_us(self) -> int:
        """Device monotonic microseconds, including the simulated crystal error."""
        elapsed = time.monotonic() - self.t0_wall
        return int(elapsed * 1e6 * (1.0 + self.clock_ppm * 1e-6))

    # ------------------------------------------------------------------- ring

    def acquire_due(self) -> list[int]:
        """Generate every sample whose time has come. Returns the new sequence numbers."""
        now = self.now_us()
        if self._last_step_us == 0:
            self._last_step_us = now
            self._ring_t0_us = now
        produced = []
        while now - self._last_step_us >= self.dt_us:
            self._last_step_us += self.dt_us
            truth = self._step_physics(self.dt_us / 1e6)
            row = self._measure(truth)
            self._push(row, self._last_step_us)
            produced.append(self._next_seq - 1)
        return produced

    def _push(self, row: np.ndarray, t_us: int) -> None:
        self._ring[self._cursor] = row
        if self._filled < self.ring_capacity:
            self._filled += 1
        else:
            self._ring_seq0 += 1
            self._ring_t0_us += self.dt_us
        self._cursor = (self._cursor + 1) % self.ring_capacity
        self._next_seq += 1

    def ring_bounds(self) -> tuple[int, int]:
        """(first_seq, last_seq) currently retained, inclusive."""
        return self._ring_seq0, self._next_seq - 1

    def read_range(self, from_seq: int, to_seq: int) -> tuple[np.ndarray, int, list[list[int]]]:
        """Rows for [from_seq, to_seq], plus the ranges that have aged out."""
        first, last = self.ring_bounds()
        missing: list[list[int]] = []
        if from_seq < first:
            missing.append([from_seq, min(first - 1, to_seq)])
            from_seq = first
        if to_seq > last:
            to_seq = last
        if to_seq < from_seq:
            return np.zeros((0, len(CHANNEL_IDS)), dtype=np.float32), from_seq, missing
        count = to_seq - from_seq + 1
        start = (self._cursor - (self._next_seq - from_seq)) % self.ring_capacity
        idx = (start + np.arange(count)) % self.ring_capacity
        return self._ring[idx], from_seq, missing

    def seq_to_us(self, seq: int) -> int:
        return self._ring_t0_us + (seq - self._ring_seq0) * self.dt_us

    def make_block(self, from_seq: int, to_seq: int) -> tuple[M.SampleBlock | None, list[list[int]]]:
        data, actual_first, missing = self.read_range(from_seq, to_seq)
        if data.shape[0] == 0:
            return None, missing
        fault_mask = 0
        for i in range(len(CHANNEL_IDS)):
            if np.all(np.isnan(data[:, i])):
                fault_mask |= 1 << i
        return (
            M.SampleBlock(
                first_seq=actual_first,
                t0_us=self.seq_to_us(actual_first),
                dt_us=self.dt_us,
                channel_ids=CHANNEL_IDS,
                data=data,
                fault_mask=fault_mask,
            ),
            missing,
        )

    # ------------------------------------------------------------- descriptions

    def device_info(self) -> dict:
        return {
            "proto": M.PROTOCOL_VERSION,
            "serial": self.serial,
            "model": "tjiptemp-s3-sim",
            "hw_rev": "1.0",
            "fw_ver": "0.1.0-sim",
            "chip": "esp32s3",
            "mac": ":".join(self.serial[-12:][i : i + 2].lower() for i in range(0, 12, 2)),
            "uptime_us": self.now_us(),
            "boot_id": self.boot_id,
            "caps": {
                "max_payload": 4096,
                "max_rate_hz": 100,
                "ring_samples": self.ring_capacity,
                "backfill": True,
                "display": {"w": 240, "h": 320, "backlight": True},
                "leds": 3,
                "transports": ["usb", "wifi", "ble"],
            },
            "channels": CHANNEL_TABLE,
        }

    def status(self) -> dict:
        first, last = self.ring_bounds()
        latest = self._ring[(self._cursor - 1) % self.ring_capacity] if self._filled else None
        pct = int(max(0.0, min(1.0, (self.v_bat - 3.3) / (4.2 - 3.3))) * 100)
        return M.clean_floats({
            "t_us": self.now_us(),
            "boot_id": self.boot_id,
            "seq": self._next_seq - 1,
            "ring": {"first_seq": first, "last_seq": last,
                     "fill": round(self._filled / self.ring_capacity, 3)},
            "battery": {"v": round(self.v_bat, 3), "pct": pct,
                        "charging": self.charging, "fault": False},
            "vcc": round(float(latest[Ch.V_CC]), 3) if latest is not None else None,
            "temps": {
                "rtd_area": round(float(latest[Ch.NTC_BRD_RTD]), 2) if latest is not None else None,
                "tc_area": round(float(latest[Ch.NTC_BRD_TC]), 2) if latest is not None else None,
                "charger": round(float(latest[Ch.NTC_BRD_CHG]), 2) if latest is not None else None,
            },
            "wifi": self.wifi,
            "ble": {"state": "advertising", "peers": 0},
            "cal_rev": self.cal_rev,
            "faults": self.faults,
            "flags": ["uncalibrated"] if self.cal_rev == 0 else [],
        })

    def self_test(self) -> dict:
        latest = self._ring[(self._cursor - 1) % self.ring_capacity] if self._filled else None
        r = float(latest[Ch.PT1000_R]) if latest is not None and np.isfinite(latest[Ch.PT1000_R]) else float("nan")
        checks = [
            {"name": "max31865_spi", "pass": True, "detail": "config readback 0xC3"},
            {"name": "max31865_rtd", "pass": bool(np.isfinite(r)),
             "detail": f"{r:.1f} ohm" if np.isfinite(r) else "no valid conversion"},
            {"name": "max31856_spi", "pass": True, "detail": "cr0 readback 0x91"},
            {"name": "max31856_oc", "pass": self.faults["max31856"] is None,
             "detail": "no open circuit" if self.faults["max31856"] is None else "open circuit"},
            {"name": "aht20_i2c", "pass": self.faults["aht20"] is None, "detail": "status 0x18"},
            {"name": "vcc", "pass": True, "detail": "4.98 V"},
            {"name": "nvs", "pass": True, "detail": f"cal rev {self.cal_rev}"},
            {"name": "psram", "pass": True, "detail": f"ring {self.ring_capacity} rows"},
        ]
        return {"pass": all(c["pass"] for c in checks), "checks": checks}

    # -------------------------------------------------------- fault injection

    def inject_fault(self, which: str, reg: int = 0x01) -> None:
        """Make a sensor fail, so the UI's fault handling can be exercised."""
        if which not in self.faults:
            raise ValueError(f"unknown sensor {which!r}")
        self.faults[which] = {"reg": reg, "bits": []}

    def clear_faults(self) -> None:
        self.faults = {k: None for k in self.faults}

    def reboot(self) -> None:
        """Simulate a reset: new boot id, sequence numbers restart, ring cleared."""
        self.boot_id = uuid.uuid4().hex[:4]
        self.t0_wall = time.monotonic()
        self._next_seq = 0
        self._ring_seq0 = 0
        self._cursor = 0
        self._filled = 0
        self._last_step_us = 0
        self._ring[:] = np.nan


def _deep_copy(obj):
    import copy

    return copy.deepcopy(obj)


def merge_patch(base: dict, patch: dict) -> dict:
    """Recursive dict merge, matching SET_CONFIG's partial-patch semantics."""
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge_patch(out[key], value)
        else:
            out[key] = value
    return out


class SimSession:
    """Per-connection protocol state: what this host subscribed to.

    Separate from the board because the board can serve several hosts at once and
    each has its own stream rate and channel selection, exactly as the real
    firmware must.
    """

    def __init__(self, board: SimulatedBoard, *, decimate: bool = False) -> None:
        self.board = board
        self.streaming = False
        self.rate_hz = 10.0
        self.channels: tuple[int, ...] = CHANNEL_IDS
        self.last_sent_seq = -1
        self.last_status_at = 0.0
        #: Only a bandwidth-limited link (BLE) thins the stream. See
        #: :meth:`pending_stream_frames`.
        self.decimate = decimate

    def handle(self, frame: Frame) -> list[Frame]:
        """Process one request, returning the frames to send back."""
        try:
            return self._handle(frame)
        except M.ProtocolError as exc:
            return [M.error("bad_payload", str(exc), seq=frame.seq)]
        except Exception as exc:  # a simulator bug should look like a device error
            return [M.error("internal", f"{type(exc).__name__}: {exc}", seq=frame.seq)]

    def _handle(self, frame: Frame) -> list[Frame]:
        board = self.board
        mt = frame.msg_type
        reply = lambda msg_type, payload: [  # noqa: E731
            Frame(msg_type, payload, seq=frame.seq, flags=FLAG_RESPONSE)
        ]

        if mt == M.Msg.HELLO:
            return reply(M.Msg.DEVICE_INFO, M.json_payload(board.device_info()))

        if mt == M.Msg.GET_CONFIG:
            return reply(M.Msg.CONFIG, M.json_payload(board.config))

        if mt == M.Msg.SET_CONFIG:
            patch = M.parse_json(frame)
            # The simulator has no NVS, so "volatile" changes nothing here. Real
            # firmware must honour it: a volatile patch applies now but must not
            # survive a reboot.
            patch.pop("volatile", None)
            board.config = merge_patch(board.config, patch)
            rate = float(board.config["acquire"]["rate_hz"])
            if rate != board.rate_hz:
                board.rate_hz = rate
                board.dt_us = int(round(1e6 / rate))
            return reply(M.Msg.CONFIG, M.json_payload(board.config))

        if mt == M.Msg.GET_CAL:
            return reply(M.Msg.CAL, M.json_payload(board.calibration))

        if mt == M.Msg.SET_CAL:
            cal = M.parse_json(frame)
            board.cal_rev += 1
            cal["rev"] = board.cal_rev
            board.calibration = cal
            return reply(M.Msg.CAL, M.json_payload(board.calibration))

        if mt == M.Msg.STREAM_START:
            body = M.parse_json(frame)
            self.rate_hz = min(float(body.get("rate_hz", 10.0)), board.rate_hz)
            chans = body.get("channels", "all")
            self.channels = CHANNEL_IDS if chans == "all" else tuple(
                c for c in CHANNEL_IDS if c in set(chans)
            )
            self.streaming = True
            self.last_sent_seq = board._next_seq - 1
            return []

        if mt == M.Msg.STREAM_STOP:
            self.streaming = False
            return []

        if mt == M.Msg.TIME_SYNC:
            t1 = M.decode_time_sync(frame.payload)
            t2 = board.now_us()
            t3 = board.now_us()
            return reply(M.Msg.TIME_ECHO, M.encode_time_echo(t1, t2, t3))

        if mt == M.Msg.GET_RANGE:
            body = M.parse_json(frame)
            return self._handle_range(frame, int(body["from_seq"]), int(body["to_seq"]))

        if mt == M.Msg.DISPLAY_SET:
            board.config["display"] = merge_patch(board.config["display"], M.parse_json(frame))
            return reply(M.Msg.CONFIG, M.json_payload(board.config))

        if mt == M.Msg.LED_SET:
            body = M.parse_json(frame)
            if "led" in body:
                board.leds = [bool(x) for x in body["led"]][:3]
            return []

        if mt == M.Msg.IDENTIFY:
            board.identify_until = time.monotonic() + float(M.parse_json(frame).get("seconds", 5))
            return []

        if mt == M.Msg.SELF_TEST:
            return reply(M.Msg.SELF_TEST_RESULT, M.json_payload(board.self_test()))

        if mt == M.Msg.WIFI_PROVISION:
            body = M.parse_json(frame)
            board.wifi = {"state": "connected", "ip": "192.168.1.44", "rssi": -57,
                          "ssid": body.get("ssid", "")}
            return reply(M.Msg.WIFI_STATUS, M.json_payload(board.wifi))

        if mt == M.Msg.FACTORY_RESET:
            if M.parse_json(frame).get("confirm") != "ERASE":
                return [M.error("bad_payload", "factory reset needs confirm=ERASE", seq=frame.seq)]
            from ..calibration.models import default_calibration

            board.calibration = default_calibration().to_json()
            board.cal_rev = 0
            board.config = _deep_copy(DEFAULT_CONFIG)
            return reply(M.Msg.CONFIG, M.json_payload(board.config))

        return [M.error("unsupported", f"message 0x{mt:02X} is not implemented", seq=frame.seq)]

    def _handle_range(self, frame: Frame, from_seq: int, to_seq: int) -> list[Frame]:
        board = self.board
        out: list[Frame] = []
        all_missing: list[list[int]] = []
        sent = 0
        # 4096-byte payload budget: header + ids + rows of float32.
        rows_per_frame = max(1, (4096 - 32) // (len(CHANNEL_IDS) * 4))
        cursor = from_seq
        while cursor <= to_seq:
            chunk_end = min(cursor + rows_per_frame - 1, to_seq)
            block, missing = board.make_block(cursor, chunk_end)
            all_missing.extend(missing)
            if block is not None:
                out.append(
                    Frame(M.Msg.RANGE_BLOCK, M.encode_sample_block(block),
                          seq=frame.seq, flags=FLAG_RESPONSE | FLAG_MORE)
                )
                sent += block.n_samples
                cursor = block.last_seq + 1
            else:
                cursor = chunk_end + 1
        out.append(
            Frame(M.Msg.RANGE_END,
                  M.json_payload({"sent": sent, "missing": _merge_ranges(all_missing)}),
                  seq=frame.seq, flags=FLAG_RESPONSE)
        )
        return out

    def pending_stream_frames(self) -> list[Frame]:
        """Sample blocks this session is due.

        The board acquires on one schedule for everyone -- there is one SPI
        conversion sequence, not one per host -- so a session's requested rate is
        a *ceiling on what it wants*, not a private sampling rate. Sending
        everything is normally right: 15 channels at 100 Hz is 6 kB/s, which USB
        and WiFi absorb without noticing, and the host can thin it for display.

        Decimation happens only when the link genuinely cannot carry the stream,
        and then the block is flagged so the host knows the missing sequence
        numbers were never coming and must not be chased with backfill.
        """
        board = self.board
        if not self.streaming:
            return []
        _, last = board.ring_bounds()
        if last <= self.last_sent_seq:
            return []

        first = max(self.last_sent_seq + 1, board.ring_bounds()[0])
        block, _ = board.make_block(first, last)
        if block is None:
            return []
        self.last_sent_seq = block.last_seq

        step = max(1, int(round(board.rate_hz / max(self.rate_hz, 0.01))))
        if self.decimate and step > 1:
            block = M.SampleBlock(
                first_seq=block.first_seq,
                t0_us=block.t0_us,
                dt_us=block.dt_us * step,
                channel_ids=block.channel_ids,
                data=block.data[::step],
                fault_mask=block.fault_mask,
                seq_step=step,
            )
        if self.channels != CHANNEL_IDS:
            cols = [block.channel_ids.index(c) for c in self.channels if c in block.channel_ids]
            block = M.SampleBlock(
                first_seq=block.first_seq, t0_us=block.t0_us, dt_us=block.dt_us,
                channel_ids=tuple(self.channels), data=block.data[:, cols],
                fault_mask=block.fault_mask,
            )
        return [Frame(M.Msg.SAMPLE_BLOCK, M.encode_sample_block(block))]

    def pending_status_frame(self, now: float) -> list[Frame]:
        if now - self.last_status_at < 1.0:
            return []
        self.last_status_at = now
        return [Frame(M.Msg.STATUS, M.json_payload(self.board.status()))]


def _merge_ranges(ranges: list[list[int]]) -> list[list[int]]:
    if not ranges:
        return []
    ordered = sorted(ranges)
    out = [list(ordered[0])]
    for lo, hi in ordered[1:]:
        if lo <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return out
