# TjipTemp

Desktop control, logging and calibration software for the TjipTemp ESP32-S3
thermometer board.

The board has no buttons. Everything — what its 240×320 screen shows, how the
PT1000 is wired, what the sensors are calibrated against — is driven from here,
over USB, WiFi or Bluetooth.

```
┌─ board ──────────────────────┐        ┌─ this software ─────────────────┐
│ PT1000  2/3/4-wire  MAX31865 │  USB   │ live plots · recording          │
│ Type K              MAX31856 │◄─────► │ calibration wizard              │
│ 3 external NTC (CN1-3)       │  WiFi  │ CSV / Excel / figures           │
│ 3 on-board NTC               │◄─────► │ REST + WebSocket API            │
│ V_bat, V_cc, AHT20 (T + RH)  │  BLE   │ board screen and LED control    │
│ 240×320 SPI display, 3 LEDs  │◄─────► │                                 │
└──────────────────────────────┘        └─────────────────────────────────┘
```

## What is here

| Path | What it is |
|---|---|
| `docs/protocol.md` | **TJIP-1**, the wire protocol. Normative, and implementable from this file alone. |
| `firmware-ref/` | Portable C reference codec for the firmware side, plus its tests. |
| `src/tjiptemp/` | The application. |
| `packaging/` | AUR, DEB, macOS `.dmg`, Windows installer. |
| `tests/` | 128 tests, including a full simulated board. |

## Install

**Arch / AUR**

```sh
cd packaging/aur && makepkg -si
```

**Debian / Ubuntu**

```sh
./packaging/debian/build.sh && sudo dpkg -i build/tjiptemp_*.deb
```

**macOS / Windows** — download the `.dmg` or the installer from Releases, or
build with `./packaging/macos/build.sh` / `packaging\windows\build.ps1`.

**From source, any platform**

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ".[ble,dev]"
tjiptemp
```

On Linux, install `packaging/linux/99-tjiptemp.rules` to get access to the board
without joining the `dialout` group:

```sh
sudo cp packaging/linux/99-tjiptemp.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

## Try it without hardware

The firmware does not exist yet, so the simulator does. It is a complete TJIP-1
device: real physics with thermal time constants, sensors simulated at the
physical level (ohms, microvolts) with deliberate errors for calibration to find,
a ring buffer, backfill, fault injection and a drifting crystal.

```sh
tjiptemp-sim --scenario ramp --rate 50     # terminal 1
tjiptemp --connect 127.0.0.1:3737          # terminal 2
```

`tjiptemp-sim --pty` exposes it as a real serial device instead, which exercises
the USB code path. `--fault max31856` starts with the thermocouple open-circuit;
`--reboot-after 30` makes it reset every half minute so you can watch reconnect
and resync work.

## Usage

```sh
tjiptemp                      # GUI, connects to nothing until you ask
tjiptemp --list               # what can I see?
tjiptemp -c /dev/ttyACM0      # connect to a specific board
tjiptemp --auto-connect       # scan USB and connect to whatever answers
tjiptemp --headless --record  # no GUI: log and serve the API
tjiptemp --broadcast          # also publish a channel as a network thermometer
```

Headless mode does not import Qt at all, so it runs on a Raspberry Pi sitting
next to an experiment.

## Broadcasting to VNA Studio

TjipTemp can publish one channel as a network thermometer, discoverable over
mDNS and a UDP beacon, so VNA Studio overlays the live temperature on a
dielectric recording. Off by default; **Toolbar → Broadcast temperature…** or
`--broadcast`. See [docs/thermometer-broadcast.md](docs/thermometer-broadcast.md).

## The API

The app serves a local REST + WebSocket API on `127.0.0.1:8737`, with generated
OpenAPI docs at `/docs`. It reads from the same device layer the UI does, so the
numbers always agree.

```sh
curl localhost:8737/api/v1/devices/TJIP-…/readings
curl localhost:8737/api/v1/sessions/3/data?format=csv > run.csv
curl localhost:8737/api/v1/metrics                      # Prometheus
```

```python
import json, websockets, asyncio

async def watch():
    url = "ws://localhost:8737/api/v1/stream?rate_hz=2"
    async with websockets.connect(url) as ws:
        async for message in ws:
            data = json.loads(message)
            if data["type"] == "samples":
                print(data["devices"][0]["values"]["pt1000"])

asyncio.run(watch())
```

Writes (config, calibration, display) can be disabled independently of reads, so
a dashboard can be given a read-only endpoint. Binding to anything other than
loopback without a token is refused rather than warned about.

## How it works

A few decisions are worth knowing about, because they are the difference between
a datalogger and an instrument.

**Timestamps are fitted, not stamped on arrival.** The board has no RTC; it
counts microseconds since boot. Stamping samples when they arrive looks right and
is wrong — USB and WiFi deliver in bursts, so arrival times cluster and gap, and
you get several milliseconds of jitter on samples that were taken on a perfectly
regular tick. Instead the host runs SNTP-style exchanges, keeps the low-latency
ones, and weighted-least-squares fits `utc = a·device_us + b`. The slope is the
crystal error in ppm, which is genuinely useful: 40 ppm is a second of skew over
an eight-hour soak. Every recording stores its fit and its uncertainty.

**Redundant links deduplicate by sequence number.** Every row carries a
device-assigned sequence. Run USB and WiFi together and the host takes whichever
arrives first, ignores the duplicate, and notices any range neither delivered.
Gaps are backfilled from the board's own ring buffer with `GET_RANGE`, after a
short grace period — because on a healthy dual-link setup the "missing" rows are
usually about to arrive on the other link.

**Calibration is fitted on the host and evaluated on the board.** Coefficients
live in NVS, so the board's own display shows the same number the desktop does.
Channels are fitted against their *raw* physical measurement — the PT1000's
resistance, the thermocouple's microvolts — not against an already-linearised
temperature.

**The fitting tries not to flatter itself.** Three points fit Steinhart-Hart
exactly, so the wizard says the residuals are zero by construction and mean
nothing. A thermocouple's cold-junction offset is, to within about one percent,
indistinguishable from a plain voltage offset — the Seebeck coefficient barely
moves across any plausible cold-junction range — so the fitter refuses to report
one from hot-junction points and tells you to calibrate the cold junction
directly instead.

**Nothing is connected until you say so.** Starting the program does not scan
for boards: opening a serial port takes it away from whatever else has it open,
and on an ESP32-S3 opening the CDC port moves the modem lines on a board that
may be mid-measurement. "Find USB boards" in the toolbar runs the scan, Connect…
takes an address, and the checkbox in Settings turns startup scanning back on if
you want it. Headless: `--connect ADDRESS`, or `--auto-connect` to scan.

**The board's settings win on connect.** A board applies its stored
configuration the moment it powers up, host or no host — that is the point of a
box on a DIN rail. So connecting is one-way: the desktop reads the board's
sample rate, sensor setup and simulator table and shows those, rather than
pushing whatever it happened to remember from last time. Changing a setting
afterwards is an explicit push, because someone asked for it. The rate in
Settings is the starting point for a board that does not report one.

**Connecting does not reset the board.** An ESP32-S3's USB-Serial-JTAG resets
the chip when the host moves DTR and RTS in the sequence esptool uses, and it
watches for a *sequence*, so two ioctls — one per line — are enough to trigger
it by accident. Both lines are written in a single `TIOCMSET`, `HUPCL` is
cleared so closing the port does not drop them again, and a handshake that has
to be retried is retried on the port that is already open. Reopening per retry
reset the board per retry, which is a good way to keep a slow board from ever
answering.

**Gaps stay gaps.** A missing or faulted sample exports as an empty cell. Never
interpolated, never forward-filled, including when resampling.

## Charts

Live plotting is pyqtgraph (fast, MIT); report figures are matplotlib and seaborn
(pretty, BSD). Two rules are enforced throughout:

* **One unit per axis.** Temperature, volts, humidity and raw resistance each get
  their own stacked panel sharing the time axis. A dual-axis chart makes any two
  series look correlated purely by how you scaled them.
* **Zooming out must not lie.** Long spans are min/max decimated, so a
  single-sample spike stays visible instead of vanishing between the samples the
  renderer happened to keep.

The categorical palette is validated for colour-vision deficiency and for
contrast in both light and dark themes; the channel table beside every chart is
the table view, so identity never rests on hue alone.

## Building firmware for the board

`firmware-ref/` has a dependency-free C11 implementation of the framing, the CRC,
the sample-block layout and the time-sync payloads. Drop `tjip_proto.c/.h` into an
ESP-IDF component and it builds unchanged.

```sh
cd firmware-ref
make test          # C self-tests
make crosscheck    # prove the C and the Python emit identical bytes
```

That cross-check is what keeps `docs/protocol.md` honest — a specification alone
cannot catch a padding or endianness disagreement, and this does.

Control messages are JSON (ESP-IDF bundles cJSON, Python has `json` in stdlib —
no new dependency on either side, and every payload is readable in a terminal).
Only the hot path, sample delivery, is packed binary.

## Tests

```sh
pytest tests/ -q
```

128 tests, no hardware required. They cover the codecs, the sensor physics
against published reference values, the calibration fitting against deliberately
injected errors, dedup and backfill, the database and every export format, and
the API end to end against a real uvicorn server.

## Licence

MIT. Every dependency is permissive — see `LICENSE` for the list and a note on
the two weak-copyleft ones. Qt Charts and QCustomPlot were deliberately avoided;
both are GPL-or-commercial and would have forced this project's hand.
