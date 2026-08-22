# TJIP-1 — TjipTemp Wire Protocol

Version 1.0 · Status: draft · Applies to the ESP32-S3 thermometer board and any host software.

This document is the normative description of the link between the board and a host.
It is deliberately implementable in a weekend in C, Python, Rust, or JavaScript, using
only standard building blocks (COBS, CRC-16/CCITT, JSON, IEEE-754 float32). There is
nothing proprietary here: a third party can write a complete client from this file
alone, and `firmware-ref/tjip_proto.c` is a portable, dependency-free reference codec.

---

## 1. Transports

| Transport | Carrier | Notes |
|-----------|---------|-------|
| USB | CDC-ACM (USB CDC class, native ESP32-S3 USB-OTG peripheral) | No vendor driver on Linux, macOS, or Windows 10+. Baud rate is ignored (it is a virtual UART); the host may still set 921600 as a hint. |
| WiFi | TCP, default port **3737** | Same frame format, byte-for-byte. Discovery via mDNS `_tjiptemp._tcp.local`. |
| BLE | GATT, see §9 | Control, configuration and low-rate telemetry only. Bulk history sync is not offered over BLE. |

Every transport carries the identical frame format defined in §2. A host implementation
therefore needs exactly one codec and one state machine, regardless of link.

The board MAY serve all three simultaneously. Each connected host is an independent
session with its own subscription state; the board's ring buffer (§6) is shared.

---

## 2. Framing

Frames are COBS-encoded (Consistent Overhead Byte Stuffing, Cheshire & Baker) and
delimited by a single `0x00` byte. `0x00` therefore never appears inside an encoded
frame and a receiver can always resynchronise by scanning to the next delimiter.

```
on the wire:   <COBS( frame )> 0x00  <COBS( frame )> 0x00  ...
```

The frame, before COBS encoding, is:

```
 offset  size  field         description
 ------  ----  ------------  ------------------------------------------------
      0     1  msg_type      message identifier, see §3
      1     1  flags         bit0 RESPONSE, bit1 ERROR, bit2 MORE, bits3-7 rsv
      2     2  seq           uint16 LE, request/response correlation
      4     2  payload_len   uint16 LE, length of payload in bytes
      6     N  payload       JSON (UTF-8) or packed binary, per message type
    6+N     2  crc           uint16 LE, CRC-16/CCITT-FALSE over offsets 0..6+N-1
```

* **CRC-16/CCITT-FALSE**: polynomial `0x1021`, init `0xFFFF`, no reflection, no final XOR.
* `payload_len` MUST be ≤ the negotiated `max_payload` (§4). Default before
  negotiation: **1024**. After `DEVICE_INFO` the host may use `caps.max_payload`.
* A frame whose CRC fails is silently discarded. It is not NAKed; the requester's
  timeout handles it. This keeps the firmware receive path free of retry state.

### 2.1 Flags

| Bit | Name | Meaning |
|-----|------|---------|
| 0 | `RESPONSE` | This frame answers the request bearing the same `seq`. |
| 1 | `ERROR` | Payload is a JSON error object (§3.9). Implies `RESPONSE`. |
| 2 | `MORE` | Part of a multi-frame reply; more frames with this `seq` follow. |

### 2.2 Sequence numbers

The host allocates `seq` for requests it initiates, incrementing modulo 2^16, skipping
0. The device echoes it in the response. Device-initiated frames (`SAMPLE_BLOCK`,
`STATUS`, `LOG`) use `seq = 0` and are never acknowledged.

---

## 3. Messages

Control messages carry **JSON**. This is a deliberate choice: ESP-IDF bundles cJSON, so
the firmware needs no new dependency, and the payloads stay inspectable with a terminal.
The one hot path — sample delivery — uses packed binary instead, because JSON overhead at
100 Hz × 15 channels is not acceptable.

| ID | Name | Dir | Payload |
|----|------|-----|---------|
| `0x01` | `HELLO` | H→D | JSON `{"proto":1,"host":"..."}` |
| `0x02` | `DEVICE_INFO` | D→H | JSON, §4 |
| `0x10` | `GET_CONFIG` | H→D | empty |
| `0x11` | `CONFIG` | D→H | JSON, §5 |
| `0x12` | `SET_CONFIG` | H→D | JSON, partial patch, §5 |
| `0x20` | `GET_CAL` | H→D | empty |
| `0x21` | `CAL` | D→H | JSON, §7 |
| `0x22` | `SET_CAL` | H→D | JSON, §7 — device persists to NVS |
| `0x30` | `STREAM_START` | H→D | JSON `{"rate_hz":10,"channels":"all"}` |
| `0x31` | `STREAM_STOP` | H→D | empty |
| `0x32` | `SAMPLE_BLOCK` | D→H | binary, §6.1 |
| `0x33` | `STATUS` | D→H | JSON, §8 |
| `0x40` | `TIME_SYNC` | H→D | binary, §6.2 |
| `0x41` | `TIME_ECHO` | D→H | binary, §6.2 |
| `0x50` | `GET_RANGE` | H→D | JSON `{"from_seq":N,"to_seq":M}` |
| `0x51` | `RANGE_BLOCK` | D→H | binary, same layout as `SAMPLE_BLOCK`, `MORE` set |
| `0x52` | `RANGE_END` | D→H | JSON `{"sent":N,"missing":[[a,b],...]}` |
| `0x60` | `DISPLAY_SET` | H→D | JSON, §10 |
| `0x61` | `LED_SET` | H→D | JSON `{"led":[b,b,b]}` or `{"pattern":"..."}` |
| `0x62` | `IDENTIFY` | H→D | JSON `{"seconds":5}` — blink LEDs to find the board |
| `0x70` | `SELF_TEST` | H→D | empty |
| `0x71` | `SELF_TEST_RESULT` | D→H | JSON, §11 |
| `0x78` | `WIFI_PROVISION` | H→D | JSON `{"ssid":"..","psk":".."}` |
| `0x79` | `WIFI_STATUS` | D→H | JSON `{"state":"..","ip":"..","rssi":-52}` |
| `0x7A` | `FACTORY_RESET` | H→D | JSON `{"confirm":"ERASE"}` |
| `0x7E` | `LOG` | D→H | JSON `{"level":"warn","msg":"..","t_us":N}` |
| `0x7F` | `ERROR` | D→H | JSON, §3.9 |
| `0x90`–`0x9F` | reserved for OTA | | not specified in 1.0 |

Unknown `msg_type`: the receiver replies `ERROR` with code `unsupported` if the frame
had a non-zero `seq`, otherwise ignores it. Unknown JSON keys are ignored; this is how
the protocol stays forward-compatible.

### 3.9 Error object

```json
{"code": "range_evicted", "msg": "seq 4000..4200 no longer buffered", "detail": {}}
```

Codes: `unsupported`, `bad_payload`, `busy`, `range_evicted`, `nvs_write_failed`,
`sensor_fault`, `not_calibrated`, `bad_state`, `crc` (informational only).

---

## 4. DEVICE_INFO

Sent unsolicited on connect, and in response to `HELLO`.

```json
{
  "proto": 1,
  "serial": "TJIP-A3F21C0E5B44",
  "model": "tjiptemp-s3",
  "hw_rev": "1.0",
  "fw_ver": "0.1.0",
  "chip": "esp32s3",
  "mac": "a3:f2:1c:0e:5b:44",
  "uptime_us": 91827364,
  "caps": {
    "max_payload": 4096,
    "max_rate_hz": 100,
    "ring_samples": 60000,
    "backfill": true,
    "display": {"w": 240, "h": 320, "backlight": true},
    "leds": 3,
    "transports": ["usb", "wifi", "ble"]
  },
  "channels": [
    {"id": 0,  "key": "pt1000",     "name": "PT1000",          "unit": "degC", "kind": "rtd"},
    {"id": 1,  "key": "typek",      "name": "Type K",          "unit": "degC", "kind": "tc"},
    {"id": 2,  "key": "typek_cj",   "name": "Type K cold jn",  "unit": "degC", "kind": "cj"},
    {"id": 3,  "key": "ntc_ext1",   "name": "NTC CN1",         "unit": "degC", "kind": "ntc"},
    {"id": 4,  "key": "ntc_ext2",   "name": "NTC CN2",         "unit": "degC", "kind": "ntc"},
    {"id": 5,  "key": "ntc_ext3",   "name": "NTC CN3",         "unit": "degC", "kind": "ntc"},
    {"id": 6,  "key": "ntc_brd_rtd","name": "Board: RTD area", "unit": "degC", "kind": "ntc"},
    {"id": 7,  "key": "ntc_brd_tc", "name": "Board: TC area",  "unit": "degC", "kind": "ntc"},
    {"id": 8,  "key": "ntc_brd_chg","name": "Board: charger",  "unit": "degC", "kind": "ntc"},
    {"id": 9,  "key": "v_bat",      "name": "Battery",         "unit": "V",    "kind": "volt"},
    {"id": 10, "key": "v_cc",       "name": "VCC",             "unit": "V",    "kind": "volt"},
    {"id": 11, "key": "aht20_t",    "name": "AHT20 temp",      "unit": "degC", "kind": "hygro"},
    {"id": 12, "key": "aht20_rh",   "name": "AHT20 RH",        "unit": "%RH",  "kind": "hygro"},
    {"id": 13, "key": "pt1000_r",   "name": "PT1000 raw",      "unit": "ohm",  "kind": "raw"},
    {"id": 14, "key": "typek_uv",   "name": "Type K raw",      "unit": "uV",   "kind": "raw"}
  ]
}
```

Channel IDs are stable across firmware versions. A host MUST key on `id`, and MAY use
`key`/`name` for display. A device that omits a channel simply does not list it.

The raw channels (13, 14) exist so a host can calibrate against the underlying physical
measurement rather than an already-linearised temperature.

---

## 5. CONFIG

`GET_CONFIG` returns the whole object. `SET_CONFIG` takes a partial patch: only the keys
present are changed, and the device replies with the resulting full `CONFIG`. Config is
persisted to NVS unless `"volatile": true` is included in the patch.

```json
{
  "rtd": {
    "wires": 4,                 // 2 | 3 | 4  -> MAX31865 config register
    "filter_hz": 50,            // 50 | 60 mains rejection
    "bias_mode": "auto",        // "auto" (bias off between conversions) | "always"
    "rref_ohm": 4000.0,         // reference resistor actually fitted
    "fault_thresholds": {"high_ohm": 4200.0, "low_ohm": 200.0}
  },
  "tc": {
    "type": "K",                // MAX31856 supports B,E,J,K,N,R,S,T
    "avg": 4,                   // 1,2,4,8,16 samples averaged
    "filter_hz": 50,
    "cj_source": "internal",    // "internal" | "ntc_brd_tc" | "fixed"
    "cj_fixed_c": 0.0,
    "open_detect": true
  },
  "ntc": {
    "ext": [
      {"enabled": true, "r_series_ohm": 10000.0, "v_ref": 3.300, "pullup_to": "vcc"},
      {"enabled": true, "r_series_ohm": 10000.0, "v_ref": 3.300, "pullup_to": "vcc"},
      {"enabled": true, "r_series_ohm": 10000.0, "v_ref": 3.300, "pullup_to": "vcc"}
    ],
    "board": [
      {"r_series_ohm": 10000.0}, {"r_series_ohm": 10000.0}, {"r_series_ohm": 10000.0}
    ],
    "adc_oversample": 64
  },
  "power": {
    "vbat_divider": 2.0,
    "vcc_divider": 2.0,
    "low_battery_v": 3.40,
    "critical_battery_v": 3.20
  },
  "aht20": {"enabled": true, "rate_hz": 1},
  "acquire": {
    "rate_hz": 10,              // internal acquisition rate, independent of streaming
    "ring_seconds": 600,        // how much history to retain on-board
    "autostart": true           // acquire even with no host connected
  },
  "display": {"page": "overview", "source": 0, "backlight": 80, "rotation": 0, "timeout_s": 0},
  "net": {"hostname": "tjiptemp-5b44", "mdns": true, "tcp_port": 3737},
  "volatile": false
}
```

Notably `acquire.rate_hz` is *not* the streaming rate. The board acquires and rings
continuously; `STREAM_START.rate_hz` only sets how often it forwards to this host, and
must be ≤ `acquire.rate_hz`. This is what makes redundant USB+WiFi delivery cheap and
makes backfill exact rather than approximate.

---

## 6. Sampling, time, and backfill

### 6.1 SAMPLE_BLOCK / RANGE_BLOCK payload

All integers little-endian. All values IEEE-754 binary32 little-endian.

```
 offset  size  field
      0     1  ver          = 1
      1     1  n_ch         number of channels in this block
      2     2  n_samples    number of sample rows
      4     4  first_seq    uint32, sample sequence number of row 0
      8     8  t0_us        uint64, device monotonic microseconds of row 0
     16     4  dt_us        uint32, nominal interval between rows
     20     4  fault_mask   uint32, bit i set if channel ch_ids[i] faulted in this block
     24     1  flags        bit0 DECIMATED, bits 1-7 reserved (zero)
     25     1  reserved     zero
     26     2  seq_step     uint16, sequence increment per row; 1 in normal blocks
     28  n_ch  ch_ids[]     uint8 channel ids, in column order
      ..  pad  zero pad to a multiple of 4 bytes
      ..     -  data         float32 [n_samples][n_ch], row-major (sample-major)
```

* Row *i* carries sequence `first_seq + i*seq_step` and device time `t0_us + i*dt_us`.
* An individual reading that is invalid is transmitted as **NaN**. `fault_mask` tells
  the host *which* channel to look at; the per-sample NaN tells it *when*.
* `first_seq` increments by 1 per acquired sample and never resets except on reboot.
  Reboot is visible to the host as `first_seq` going backwards plus a
  `STATUS.boot_id` change.
* `dt_us` is nominal. If the device cannot maintain a uniform interval it MUST close
  the block and start a new one.

### 6.1.1 Decimation, and why it is flagged

The board acquires on **one** schedule for all connected hosts — there is one SPI
conversion sequence, not one per session. `STREAM_START.rate_hz` is therefore a
ceiling on what a host *wants*, not a private sampling rate, and a device is always
free to send everything it has. Normally it should: fifteen channels at 100 Hz is
about 6 kB/s, which USB and WiFi absorb without noticing, and thinning for display
is the host's job anyway.

A device MAY thin the stream when the link genuinely cannot carry it — in practice
only BLE. When it does, it MUST set `DECIMATED` and `seq_step`, because a host
cannot otherwise distinguish "these rows were deliberately skipped" from "these rows
were lost". Getting that wrong is not cosmetic: the host would open a gap for every
skipped row and then try to backfill them over the very link that was too slow to
carry them in the first place.

A host receiving a decimated block MUST use it for display only: it must not drive
gap detection, and it must not be treated as a complete record for a recording.

### 6.2 Time synchronisation

The board has no RTC. It reports monotonic microseconds since boot and the host maps
that onto UTC. The exchange is SNTP-shaped:

```
TIME_SYNC (H→D):  uint64 t1_host_ns
TIME_ECHO (D→H):  uint64 t1_host_ns (echoed)
                  uint64 t2_dev_us   (device clock when frame was received)
                  uint64 t3_dev_us   (device clock when reply was queued)
```

The host records `t4_host_ns` on receipt and computes

```
offset = ((t2 - t1) + (t3 - t4)) / 2
rtt    = (t4 - t1) - (t3 - t2)
```

The host keeps the lowest-`rtt` exchanges from a rolling window and fits a weighted
least-squares line `utc_ns = a·dev_us + b`, where `(a - 1e3)/1e3` is the crystal drift
in ppm. Re-syncing every 30 s over USB holds ±200 µs comfortably; over WiFi expect a
few ms. Both are far below what a thermal measurement needs, and — crucially — the
timestamps stay *monotonic and evenly spaced*, which arrival-time stamping does not.

The host MUST discard exchanges whose `rtt` exceeds ~4× the running median, and MUST NOT
apply a step correction to already-stored samples; it re-fits and applies the new mapping
going forward, recording the fit parameters alongside the session.

### 6.3 Backfill

The board rings `caps.ring_samples` rows in PSRAM. A host that observes a gap in
`first_seq` (or that reconnects) issues:

```json
GET_RANGE {"from_seq": 10432, "to_seq": 10999}
```

The device replies with one or more `RANGE_BLOCK` frames (flag `MORE` set on all but the
last), then `RANGE_END`:

```json
{"sent": 512, "missing": [[10432, 10487]]}
```

`missing` reports ranges already evicted from the ring. Requesting more than the ring
holds is not an error; the device sends what it has and reports the rest as missing.

Because rows are keyed by `first_seq`, a host receiving the same rows over USB *and*
WiFi simply deduplicates by sequence number. That is the whole redundancy story: run
both links, take whichever arrives first, and the gaps of one are filled by the other
with no ambiguity about whether two samples are the same sample.

---

## 7. Calibration

Calibration lives in NVS on the board and is applied on-board, so the display and any
host see identical numbers. The host owns the *fitting*; the board owns the *evaluation*.

```json
{
  "rev": 7,
  "updated_utc": "2026-08-05T12:00:00Z",
  "by": "raaf",
  "reference": "Fluke 1524 / 5608, cert 2026-01-12",
  "channels": {
    "pt1000": {
      "model": "cvd",
      "r0": 1000.043, "a": 3.9083e-3, "b": -5.775e-7, "c": -4.183e-12,
      "lead_ohm": 0.021,
      "post": {"offset_c": 0.0, "gain": 1.0}
    },
    "typek": {
      "model": "nist_typek",
      "cj_offset_c": -0.14,
      "uv_offset": 1.8, "uv_gain": 1.0002,
      "post": {"offset_c": 0.0, "gain": 1.0}
    },
    "ntc_ext1": {
      "model": "steinhart",
      "a": 1.129241e-3, "b": 2.341077e-4, "c": 8.775468e-8,
      "r_series_ohm": 9998.2,
      "post": {"offset_c": 0.0, "gain": 1.0}
    },
    "v_bat": {"model": "linear", "offset": 0.0031, "gain": 0.99942},
    "aht20_t": {"model": "linear", "offset": -0.21, "gain": 1.0},
    "aht20_rh": {"model": "linear", "offset": 1.4, "gain": 1.0}
  }
}
```

Models:

| `model` | Evaluation |
|---------|-----------|
| `cvd` | Callendar–Van Dusen. Above 0 °C invert `R = R0(1 + A·t + B·t²)` in closed form; below 0 °C use Newton on the full quartic with the C term. Subtract `lead_ohm` from the measured resistance first (2-wire lead compensation). |
| `nist_typek` | ITS-90 type-K inverse polynomial on `E_total = E_measured + E(cj)`, where `E(cj)` uses the forward polynomial (including the exponential term above 0 °C) at the calibrated cold-junction temperature. |
| `steinhart` | `1/T = A + B·ln(R) + C·ln(R)³`, T in kelvin. |
| `beta` | `1/T = 1/T0 + (1/β)·ln(R/R0)` — accepted as a simpler alternative to `steinhart`. |
| `linear` | `y = gain·x + offset`. |
| `poly` | `y = Σ cᵢ·xⁱ`, `"coeffs": [c0, c1, ...]`, for arbitrary correction curves. |

`post` is an optional final linear trim applied after the physical model, so a user can
nudge a probe without refitting the sensor curve.

A device that has never been calibrated ships with nominal values and sets
`STATUS.flags.uncalibrated`. It still measures; the host shows the readings as
uncalibrated rather than hiding them.

`SET_CAL` is atomic: the device validates the whole object, writes NVS, and only then
replies. On validation failure nothing is written and `ERROR` is returned. `rev` is
incremented by the device on every successful write and is included in `STATUS`, so a
host can detect that another host recalibrated the board underneath it.

---

## 8. STATUS

Emitted every 1 s while any host is connected, and immediately on any flag change.

```json
{
  "t_us": 918273645,
  "boot_id": "9f2c",
  "seq": 91827,
  "ring": {"first_seq": 31827, "last_seq": 91827, "fill": 0.83},
  "battery": {"v": 3.92, "pct": 71, "charging": true, "fault": false},
  "vcc": 4.98,
  "temps": {"rtd_area": 31.2, "tc_area": 30.8, "charger": 44.9},
  "wifi": {"state": "connected", "ip": "192.168.1.44", "rssi": -57},
  "ble": {"state": "advertising", "peers": 0},
  "cal_rev": 7,
  "faults": {"max31865": null, "max31856": null, "aht20": null},
  "flags": ["uncalibrated"]
}
```

`faults.max31865` / `.max31856`, when non-null, carry the decoded fault register:
e.g. `{"reg": 4, "bits": ["rtd_low_threshold"]}`. Hosts should surface these verbatim —
they are the difference between "the probe is cold" and "the probe fell off".

---

## 9. BLE profile

Service UUID `f0f0tjip-0001-1000-8000-00805f9b34fb` (placeholder — assign a real 128-bit
UUID before production).

| Characteristic | Props | Purpose |
|---|---|---|
| `...-0002` RX | Write, Write-No-Rsp | Host → device frames, identical framing to §2, split across writes at MTU boundaries. The `0x00` delimiter reassembles them. |
| `...-0003` TX | Notify | Device → host frames, same reassembly rule. |
| `...-0004` Info | Read | Static: serial, fw, model. Lets a host identify a board without connecting a session. |

Advertising name is `net.hostname`; the manufacturer data carries the last 3 bytes of the
MAC so a host can match a BLE peer to a USB/WiFi device it already knows.

BLE MTU is negotiated; assume 23 bytes if negotiation fails. `STREAM_START` over BLE
SHOULD be limited to ≤ 2 Hz and a channel subset. `GET_RANGE` over BLE MAY be refused
with `ERROR{"code":"unsupported"}`.

---

## 10. DISPLAY_SET

The board has no buttons, so the display is entirely host-driven. This message sets what
the 240×320 panel shows, and the setting persists in NVS so it survives a reboot with no
host present.

```json
{
  "page": "graph",            // "overview" | "single" | "graph" | "status" | "blank" | "qr"
  "source": 0,                // channel id for "single" and "graph"
  "sources": [0, 1, 3],       // optional, for a multi-trace graph page
  "window_s": 300,            // graph time window
  "backlight": 80,            // 0-100
  "rotation": 0,              // 0 | 90 | 180 | 270
  "units": "degC",            // "degC" | "degF" | "K"
  "timeout_s": 0              // 0 = never blank
}
```

The `qr` page renders the device's WiFi address so a phone can reach the host API — it is
optional and a device may report `ERROR{"code":"unsupported"}`.

---

## 11. SELF_TEST

Runs a non-destructive check and reports. Takes up to ~2 s; the device keeps acquiring
throughout.

```json
{
  "pass": false,
  "checks": [
    {"name": "max31865_spi",   "pass": true,  "detail": "config readback 0xC3"},
    {"name": "max31865_rtd",   "pass": true,  "detail": "1082.4 ohm, in range"},
    {"name": "max31856_spi",   "pass": true,  "detail": "cr0 readback 0x91"},
    {"name": "max31856_oc",    "pass": false, "detail": "open circuit on TC input"},
    {"name": "aht20_i2c",      "pass": true,  "detail": "status 0x18"},
    {"name": "ntc_ext1",       "pass": false, "detail": "9.4 Mohm, probe absent?"},
    {"name": "vcc",            "pass": true,  "detail": "4.98 V"},
    {"name": "nvs",            "pass": true,  "detail": "cal rev 7, 412 bytes"},
    {"name": "psram",          "pass": true,  "detail": "8 MB, ring 60000 rows"}
  ]
}
```

---

## 12. Connection lifecycle

```
host                                   device
  |-- (open transport) ---------------->|
  |<-- DEVICE_INFO (unsolicited) -------|
  |-- HELLO --------------------------->|
  |<-- DEVICE_INFO (RESPONSE) ----------|
  |-- TIME_SYNC ×8 (burst) ------------>|      initial fit
  |<-- TIME_ECHO ×8 --------------------|
  |-- GET_CONFIG / GET_CAL ------------>|
  |<-- CONFIG / CAL --------------------|
  |-- STREAM_START -------------------->|
  |<-- SAMPLE_BLOCK ... ----------------|      continuous
  |<-- STATUS (1 Hz) -------------------|
  |-- TIME_SYNC (every 30 s) ---------->|      drift tracking
  |-- GET_RANGE (on gap) -------------->|      backfill
  |<-- RANGE_BLOCK... RANGE_END --------|
```

If the host sends nothing for 30 s the device SHOULD keep streaming anyway (the host may
legitimately be a passive logger); the device drops the session only on transport close.
The host treats 3 missed `STATUS` messages as a dead link and reconnects.

---

## 13. Version negotiation

`HELLO.proto` and `DEVICE_INFO.proto` are the protocol major version. A host that sees a
`proto` it does not implement MUST refuse to stream and MUST say so plainly rather than
guessing. Minor additions (new message IDs, new JSON keys, new channels) do not bump
`proto`; ignoring what you do not recognise is always the correct behaviour.
