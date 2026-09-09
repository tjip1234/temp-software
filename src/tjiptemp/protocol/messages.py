"""TJIP-1 message identifiers and payload codecs.

Control messages are JSON (ESP-IDF bundles cJSON, Python has ``json`` in stdlib —
no new dependency on either side, and every payload stays readable in a terminal).
The single hot path, sample delivery, is packed binary because JSON at 100 Hz x 15
channels is not a serious proposition.

See ``docs/protocol.md`` §3 and §6.
"""

from __future__ import annotations

import json
import math
import struct
from dataclasses import dataclass, field

import numpy as np

from .framing import Frame

# ---------------------------------------------------------------- message types


class Msg:
    HELLO = 0x01
    DEVICE_INFO = 0x02

    GET_CONFIG = 0x10
    CONFIG = 0x11
    SET_CONFIG = 0x12

    GET_CAL = 0x20
    CAL = 0x21
    SET_CAL = 0x22

    STREAM_START = 0x30
    STREAM_STOP = 0x31
    SAMPLE_BLOCK = 0x32
    STATUS = 0x33

    TIME_SYNC = 0x40
    TIME_ECHO = 0x41

    GET_RANGE = 0x50
    RANGE_BLOCK = 0x51
    RANGE_END = 0x52

    DISPLAY_SET = 0x60
    LED_SET = 0x61
    IDENTIFY = 0x62

    # DIN-6 simulator control. Spec §13: new message ids do not bump `proto`,
    # and a device that does not implement them answers ERROR "unsupported".
    SIM_CALIBRATE = 0x68
    GET_SIM_CAL = 0x69
    SIM_CAL = 0x6A

    SELF_TEST = 0x70
    SELF_TEST_RESULT = 0x71

    WIFI_PROVISION = 0x78
    WIFI_STATUS = 0x79
    FACTORY_RESET = 0x7A

    LOG = 0x7E
    ERROR = 0x7F


MSG_NAMES: dict[int, str] = {
    value: name for name, value in vars(Msg).items() if isinstance(value, int) and not name.startswith("_")
}

#: Messages the device sends without being asked. These never carry a seq.
#: SIM_CAL is here because a finished sweep is broadcast to every session, not
#: only to whoever asked for it — another host watching the board needs to know
#: its wiper table changed underneath it.
UNSOLICITED = frozenset({Msg.SAMPLE_BLOCK, Msg.STATUS, Msg.LOG, Msg.DEVICE_INFO,
                         Msg.WIFI_STATUS, Msg.SIM_CAL})

#: For each request, the message type that answers it.
RESPONSE_FOR: dict[int, int] = {
    Msg.HELLO: Msg.DEVICE_INFO,
    Msg.GET_CONFIG: Msg.CONFIG,
    Msg.SET_CONFIG: Msg.CONFIG,
    Msg.GET_CAL: Msg.CAL,
    Msg.SET_CAL: Msg.CAL,
    Msg.TIME_SYNC: Msg.TIME_ECHO,
    Msg.GET_RANGE: Msg.RANGE_END,
    Msg.SELF_TEST: Msg.SELF_TEST_RESULT,
    Msg.WIFI_PROVISION: Msg.WIFI_STATUS,
    Msg.GET_SIM_CAL: Msg.SIM_CAL,
    # SIM_CALIBRATE answers with itself: an immediate acknowledgement that the
    # sweep started. The table arrives later, unsolicited.
    Msg.SIM_CALIBRATE: Msg.SIM_CALIBRATE,
}

PROTOCOL_VERSION = 1
DEFAULT_TCP_PORT = 3737
MDNS_SERVICE = "_tjiptemp._tcp.local."


class ProtocolError(Exception):
    """A well-formed frame carried a payload we cannot make sense of."""


# ------------------------------------------------------------------ JSON payloads

def json_payload(obj) -> bytes:
    # separators without spaces: on a 1024-byte BLE MTU budget the whitespace is real.
    return json.dumps(obj, separators=(",", ":"), allow_nan=False).encode("utf-8")


def parse_json(frame: Frame) -> dict:
    if not frame.payload:
        return {}
    try:
        obj = json.loads(frame.payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"{MSG_NAMES.get(frame.msg_type, frame.msg_type)}: bad JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ProtocolError("expected a JSON object at the top level")
    return obj


@dataclass(slots=True)
class DeviceError(Exception):
    """An ERROR frame from the device, raised at the call site that asked for it."""

    code: str
    message: str
    detail: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def parse_error(frame: Frame) -> DeviceError:
    obj = parse_json(frame)
    return DeviceError(
        code=str(obj.get("code", "unknown")),
        message=str(obj.get("msg", "")),
        detail=obj.get("detail") or {},
    )


# ----------------------------------------------------------------- sample blocks

# ver, n_ch, n_samples, first_seq, t0_us, dt_us, faults, flags, reserved, seq_step
_BLOCK_HEAD = struct.Struct("<BBHIQIIBBH")
BLOCK_HEAD_SIZE = _BLOCK_HEAD.size  # 28
BLOCK_VERSION = 1

#: Rows in this block are not consecutive acquisition samples -- the device
#: dropped rows to fit a bandwidth-limited link (BLE). Such a block is fine for a
#: live display but must never drive gap detection, because the "missing"
#: sequences were never going to be sent.
BLOCK_FLAG_DECIMATED = 0x01


@dataclass(slots=True)
class SampleBlock:
    """A run of evenly spaced samples straight off the device.

    ``data`` is shape ``(n_samples, n_channels)`` float32, NaN where a reading was
    invalid. Row *i* was taken at device time ``t0_us + i * dt_us`` and carries
    sequence ``first_seq + i * seq_step``; converting device time to UTC is the
    host timebase's job, not this layer's.

    ``seq_step`` is 1 for every normal block. It is greater only when the device
    decimated, which it flags explicitly rather than leaving the host to infer
    from a suspiciously round gap.
    """

    first_seq: int
    t0_us: int
    dt_us: int
    channel_ids: tuple[int, ...]
    data: np.ndarray
    fault_mask: int = 0
    flags: int = 0
    seq_step: int = 1

    @property
    def n_samples(self) -> int:
        return int(self.data.shape[0])

    @property
    def is_decimated(self) -> bool:
        return bool(self.flags & BLOCK_FLAG_DECIMATED) or self.seq_step != 1

    @property
    def last_seq(self) -> int:
        """Sequence of the final row (inclusive)."""
        return self.first_seq + (self.n_samples - 1) * self.seq_step

    @property
    def duration_us(self) -> int:
        return self.dt_us * max(0, self.n_samples - 1)

    def device_times_us(self) -> np.ndarray:
        return self.t0_us + np.arange(self.n_samples, dtype=np.int64) * self.dt_us

    def sequences(self) -> np.ndarray:
        return self.first_seq + np.arange(self.n_samples, dtype=np.int64) * self.seq_step

    def faulted_channels(self) -> tuple[int, ...]:
        return tuple(
            cid for i, cid in enumerate(self.channel_ids) if self.fault_mask & (1 << i)
        )

    def column(self, channel_id: int) -> np.ndarray:
        """Values for one channel, or an all-NaN column if the block lacks it."""
        try:
            idx = self.channel_ids.index(channel_id)
        except ValueError:
            return np.full(self.n_samples, np.nan, dtype=np.float32)
        return self.data[:, idx]

    def slice_seq(self, from_seq: int, to_seq: int) -> SampleBlock | None:
        """Sub-block covering [from_seq, to_seq] inclusive, or None if disjoint."""
        lo = max(from_seq, self.first_seq)
        hi = min(to_seq, self.last_seq)
        if hi < lo:
            return None
        step = self.seq_step
        # Round the start up to the next row that actually exists in this block.
        a = -(-(lo - self.first_seq) // step)
        b = (hi - self.first_seq) // step + 1
        if b <= a:
            return None
        return SampleBlock(
            first_seq=self.first_seq + a * step,
            t0_us=self.t0_us + a * self.dt_us,
            dt_us=self.dt_us,
            channel_ids=self.channel_ids,
            data=self.data[a:b],
            fault_mask=self.fault_mask,
            flags=self.flags,
            seq_step=step,
        )


def encode_sample_block(block: SampleBlock) -> bytes:
    """Pack a SampleBlock into the §6.1 payload layout."""
    n_ch = len(block.channel_ids)
    if block.data.shape[1] != n_ch:
        raise ProtocolError(
            f"block declares {n_ch} channels but data has {block.data.shape[1]} columns"
        )
    flags = block.flags | (BLOCK_FLAG_DECIMATED if block.seq_step != 1 else 0)
    head = _BLOCK_HEAD.pack(
        BLOCK_VERSION,
        n_ch,
        block.n_samples,
        block.first_seq & 0xFFFFFFFF,
        block.t0_us & 0xFFFFFFFFFFFFFFFF,
        block.dt_us & 0xFFFFFFFF,
        block.fault_mask & 0xFFFFFFFF,
        flags & 0xFF,
        0,
        max(1, block.seq_step) & 0xFFFF,
    )
    ids = bytes(block.channel_ids)
    pad = (-(len(head) + len(ids))) % 4
    values = np.ascontiguousarray(block.data, dtype="<f4")
    return head + ids + b"\x00" * pad + values.tobytes()


def decode_sample_block(payload: bytes) -> SampleBlock:
    """Unpack a SAMPLE_BLOCK / RANGE_BLOCK payload. Zero-copy where alignment allows."""
    if len(payload) < BLOCK_HEAD_SIZE:
        raise ProtocolError(f"sample block truncated at {len(payload)} bytes")
    (ver, n_ch, n_samples, first_seq, t0_us, dt_us, fault_mask,
     flags, _reserved, seq_step) = _BLOCK_HEAD.unpack_from(payload, 0)
    if ver != BLOCK_VERSION:
        raise ProtocolError(f"unsupported sample block version {ver}")
    if n_ch == 0:
        raise ProtocolError("sample block declares zero channels")
    ids_end = BLOCK_HEAD_SIZE + n_ch
    if len(payload) < ids_end:
        raise ProtocolError("sample block truncated inside the channel id table")
    channel_ids = tuple(payload[BLOCK_HEAD_SIZE:ids_end])
    data_start = ids_end + (-ids_end % 4)
    want = n_samples * n_ch * 4
    got = len(payload) - data_start
    if got < want:
        raise ProtocolError(
            f"sample block short by {want - got} bytes ({n_samples}x{n_ch} float32 expected)"
        )
    values = np.frombuffer(payload, dtype="<f4", count=n_samples * n_ch, offset=data_start)
    return SampleBlock(
        first_seq=first_seq,
        t0_us=t0_us,
        dt_us=dt_us,
        channel_ids=channel_ids,
        data=values.reshape(n_samples, n_ch),
        fault_mask=fault_mask,
        flags=flags,
        seq_step=max(1, seq_step),
    )


# ------------------------------------------------------------------- time sync

_TIME_SYNC = struct.Struct("<Q")
_TIME_ECHO = struct.Struct("<QQQ")


def encode_time_sync(t1_host_ns: int) -> bytes:
    return _TIME_SYNC.pack(t1_host_ns & 0xFFFFFFFFFFFFFFFF)


def decode_time_sync(payload: bytes) -> int:
    if len(payload) < _TIME_SYNC.size:
        raise ProtocolError("TIME_SYNC payload too short")
    return _TIME_SYNC.unpack_from(payload, 0)[0]


def encode_time_echo(t1_host_ns: int, t2_dev_us: int, t3_dev_us: int) -> bytes:
    return _TIME_ECHO.pack(t1_host_ns, t2_dev_us, t3_dev_us)


def decode_time_echo(payload: bytes) -> tuple[int, int, int]:
    if len(payload) < _TIME_ECHO.size:
        raise ProtocolError("TIME_ECHO payload too short")
    return _TIME_ECHO.unpack_from(payload, 0)


# ------------------------------------------------------------ request builders
# Small helpers so call sites read like the protocol document rather than like
# struct plumbing.


def hello(host_name: str = "tjiptemp-host") -> Frame:
    return Frame(Msg.HELLO, json_payload({"proto": PROTOCOL_VERSION, "host": host_name}))


def get_config() -> Frame:
    return Frame(Msg.GET_CONFIG)


def set_config(patch: dict, *, volatile: bool = False) -> Frame:
    body = dict(patch)
    if volatile:
        body["volatile"] = True
    return Frame(Msg.SET_CONFIG, json_payload(body))


def get_cal() -> Frame:
    return Frame(Msg.GET_CAL)


def set_cal(cal: dict) -> Frame:
    return Frame(Msg.SET_CAL, json_payload(cal))


def stream_start(rate_hz: float, channels: list[int] | str = "all") -> Frame:
    return Frame(Msg.STREAM_START, json_payload({"rate_hz": rate_hz, "channels": channels}))


def stream_stop() -> Frame:
    return Frame(Msg.STREAM_STOP)


def get_range(from_seq: int, to_seq: int) -> Frame:
    return Frame(Msg.GET_RANGE, json_payload({"from_seq": int(from_seq), "to_seq": int(to_seq)}))


def display_set(**kwargs) -> Frame:
    return Frame(Msg.DISPLAY_SET, json_payload({k: v for k, v in kwargs.items() if v is not None}))


def led_set(leds: list[bool] | None = None, pattern: str | None = None) -> Frame:
    body: dict = {}
    if leds is not None:
        body["led"] = [bool(x) for x in leds]
    if pattern is not None:
        body["pattern"] = pattern
    return Frame(Msg.LED_SET, json_payload(body))


def identify(seconds: float = 5.0) -> Frame:
    return Frame(Msg.IDENTIFY, json_payload({"seconds": seconds}))


def self_test() -> Frame:
    return Frame(Msg.SELF_TEST)


def sim_calibrate(utc: float | None = None) -> Frame:
    """Start a wiper sweep.

    The board has no clock, so it takes ours to stamp the table it is about to
    measure. It answers immediately — the sweep runs for ~15 s, reports progress
    in ``STATUS.sim.cal_progress`` and sends ``SIM_CAL`` when it finishes.
    """
    body: dict = {}
    if utc is not None:
        body["utc"] = int(utc)
    return Frame(Msg.SIM_CALIBRATE, json_payload(body))


def get_sim_cal() -> Frame:
    return Frame(Msg.GET_SIM_CAL)


def wifi_provision(ssid: str, psk: str) -> Frame:
    return Frame(Msg.WIFI_PROVISION, json_payload({"ssid": ssid, "psk": psk}))


def factory_reset() -> Frame:
    return Frame(Msg.FACTORY_RESET, json_payload({"confirm": "ERASE"}))


def error(code: str, message: str, detail: dict | None = None, seq: int = 0) -> Frame:
    from .framing import FLAG_ERROR, FLAG_RESPONSE

    return Frame(
        Msg.ERROR,
        json_payload({"code": code, "msg": message, "detail": detail or {}}),
        seq=seq,
        flags=FLAG_RESPONSE | FLAG_ERROR,
    )


# ------------------------------------------------------------------- utilities

def clean_floats(obj):
    """Recursively replace NaN/Inf with None so a payload survives strict JSON.

    The protocol forbids bare NaN in JSON (many parsers reject it); binary sample
    blocks are where NaN belongs. Status objects occasionally pick one up from a
    faulted sensor, and silently corrupting the frame over it would be worse.
    """
    if isinstance(obj, dict):
        return {k: clean_floats(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean_floats(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, (np.floating, np.integer)):
        value = obj.item()
        return clean_floats(value) if isinstance(value, float) else value
    return obj
