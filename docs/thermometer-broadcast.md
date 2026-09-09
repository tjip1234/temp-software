# Broadcasting a temperature to VNA Studio

TjipTemp can publish one channel as a **network thermometer**, so VNA Studio
overlays the live temperature on a dielectric recording and writes it to the
sidecar CSV next to the sweep. Anything else that speaks the same protocol
works too — the contract is a JSON object with a `temperature` key.

It is off by default. Turning it on binds a routable address, and nothing
should start listening on the network because the app was installed.

## Turning it on

**Toolbar → Broadcast temperature…**, tick *Publish a thermometer on the
network*, pick a channel, **Apply**. The status line at the bottom of the
dialog shows the URL and the value currently being served.

From the command line:

```sh
tjiptemp --broadcast                        # use the saved options
tjiptemp --broadcast --broadcast-channel 0  # publish the PT1000
tjiptemp --headless --broadcast --broadcast-name "Hotplate"
```

Then in VNA Studio: **Recording** tab → expand **Temperature (optional)** →
refresh → pick it from the dropdown → **Connect**.

## What it serves

| | |
|---|---|
| `GET /temperature` | `{"temperature": 23.5, "unit": "°C", ...}` |
| `WS /ws` | the same object, pushed every *push* interval |
| `GET /status` | what it is publishing right now, and any problem |
| `GET /` | a plain-text description, for whoever finds the port |

The extra keys — `name`, `serial`, `channel`, `channel_id`, `age_s`, `source` —
are for reading by hand. Clients that follow the contract ignore them.

This port is **read-only**. Board control stays on the main API port, which
binds loopback and wants a token.

## Options

### What to publish

| Option | Default | Notes |
|---|---|---|
| Board | whichever is online | Pin a serial when several boards are connected. |
| Channel | automatic | Automatic prefers PT1000, then type K, then the external NTCs — the first one actually reading. |
| Unit | Celsius | Also Fahrenheit and Kelvin; the `unit` field follows. |
| Smoothing | 3 s | Moving average. A dielectric sweep takes seconds, so an averaged temperature describes it better than one instant's sample. 0 publishes the instantaneous value. |
| Treat as stale after | 10 s | A reading older than this counts as no reading. |
| When stale | report unavailable | `error` answers 503 and the client shows the sensor as down. `last` keeps serving the last good value — convenient, and a good way to record a temperature that was never measured. |

### Where to serve it

| Option | Default | Notes |
|---|---|---|
| Service name | `TjipTemp thermometer` | What appears in the client's device list. |
| Bind address | `0.0.0.0` | Every interface, which is what a client on another machine needs. `127.0.0.1` restricts it to this computer. |
| Port | 8738 | Next to the main API on 8737. |
| HTTP path | `/temperature` | Clients default to this. |
| WebSocket path | `/ws` | Clients default to this. |
| Advertise as | HTTP polling | Both are always served; this only sets which one a discovering client picks. |
| Push every | 1 s | WebSocket push period. |

### How clients find it

| Option | Default | Notes |
|---|---|---|
| mDNS | on | Advertised as `_thermometer._tcp.local.` with `path`, `ws_path` and `mode` TXT records. |
| UDP beacon | on | JSON on port 5556. |
| Beacon every | 2 s | A client only listens while scanning, for a few seconds. Beacon less often than that and a scan can land between two and find nothing. |

The beacon goes to every interface's broadcast address, not only
255.255.255.255 — the kernel does not loop the limited broadcast address back
to sockets on this machine, so a client running on the same computer would
never see it.

## When it says nothing

`GET /status` names the reason, and so does the dialog:

- **no board online** — nothing is connected, or the pinned serial is not.
- **the selected channel is not on this board** — a channel id this board does not have.
- **no finite reading on that channel** — the sensor is open or faulted.
- **last reading is N s old** — the stream stopped; check the link.
