# PoolDose WebSocket — reverse-engineering results

Context for implementing a local push client for the SEKO PoolDose.
All details come from a recording on the real device (see git history for
the date).

## Goal

The official Home Assistant core integration (`pooldose`, lib
`python-pooldose`) polls an HTTP API every **600 s**. Rationale in the docs:
the device can't handle frequent requests.

The device also offers a **local WebSocket server** that pushes data on its
own at a ~4-second tick. The client should subscribe to this stream instead
of polling — that's ~140× fresher **and** generates less load than the
current poll.

## Device

| | |
|---|---|
| Model | SEKO PoolDose Double **Spa** |
| PRODUCT_CODE | `PDPR1H04AW100` |
| Firmware | `539292` |
| Serial / device key | `012600002BB3_DEVICE` |
| IP | `192.168.0.74` (make configurable) |
| WebSocket | `ws://192.168.0.74:1334/` |
| Web UI / HTTP API | port 80 |

## Protocol

### Connection

- **No authentication.** Opening the connection is enough.
- **No subscribe/login frame needed.** The client sends nothing; the device
  pushes on its own. Verified: a pure listener that never sends a single
  outgoing frame receives the full data stream.
- No keepalive needed from the client.

### Frame format

JSON text frames:

```json
{ "topic": "<name>", "data": { ... } }
```

Observed topics:

| Topic | Frequency | Content |
|---|---|---|
| `instant_values` | every tick (~4.2 s) | readings, configuration, status — **the payload signal** |
| `wifi_station` | ~16.8 s (every 4th tick) | WiFi connection data |
| `time` | ~25.2 s (every 6th tick) | device time |
| `wifi_status` | rare / initial | |
| `wdp_status` | rare | |

### Timing

Fixed scheduler tick of **4.2 s**; all observed intervals are exact
multiples of it.

**Important:** ticks get dropped. In the recording, only 16 of 24 expected
`instant_values` cycles arrived (effectively ~5.8 s average, longest gap
16 s). The dropouts correlate with `wifi_station`/`time` frames — the device
apparently discards its own send slots under load.

→ Don't assume a fixed interval. **Watchdog: 30 s without a frame →
reconnect.**

### Chunking

`instant_values` is split into **2 frames**:

```json
"progressInfo": { "total": 2, "offset": 1 }   // or offset: 2
```

- **Chunk 1 (offset 1):** `deviceInfo`, sensors (pH, ORP, ppm, temperature),
  target values, calibration, dosing configuration
- **Chunk 2 (offset 2):** timers and ~30 status flags

Both are flat dicts under the same serial key and **non-overlapping** — a
`dict.update()` is enough to merge them.

Reassembly rules:
1. On `offset == 1`, **clear** the buffer (half-cycles are real; otherwise
   you get Frankenstein records mixing two rounds).
2. Only process once `offset == total`.

```python
buf = {}

def on_message(raw):
    msg = json.loads(raw)
    if msg.get("topic") != "instant_values":
        return None
    p = msg["data"]["progressInfo"]
    if p["offset"] == 1:
        buf.clear()
    for serial, payload in msg["data"]["devicedata"].items():
        buf.setdefault(serial, {}).update(payload)
    return dict(buf) if p["offset"] == p["total"] else None
```

## Data structure

```
data.devicedata.<SERIAL>_DEVICE.<PRODUCT_CODE>_FW<FW>_<type>_<hash>
```

Also under the same serial key:
- `deviceInfo`: `{"dwi_status": "ok", "modbus_status": "on"}`
- `collapsed_bar`: `[]`

Some keys deviate from the hash scheme and are human-readable, e.g.
`PDPR1H04AW100_FW539292_Elapsed_PowerON_Delay`.

### Value objects

```json
{
  "visible": true,
  "alarm": false,
  "current": 7.2,
  "resolution": 0.1,
  "magnitude": ["pH", "PH"],
  "absMin": 0, "absMax": 14,
  "minT": 6, "maxT": 8,
  "set": 7.1
}
```

| Field | Meaning |
|---|---|
| `current` | actual value |
| `set` | target value (only for adjustable parameters) |
| `absMin` / `absMax` | technical value range |
| `minT` / `maxT` | alarm thresholds |
| `alarm` | alarm state of the channel |
| `visible` | whether the channel is configured/active on the device |
| `magnitude` | `[display unit, unit constant]` |
| `resolution` | step size, can be `"NA"` |
| `comboitems` | for selection fields: list `[[index, labelkey], ...]` |

**Use `visible: false` as a filter.** Example from the recording: the ppm
channel (free chlorine) is `visible: false`, `current: 0`, `alarm: true` —
no probe is attached. Without filtering, you get entities with a permanent,
never-clearing alarm.

Careful: some entries are **not an object**, but a bare boolean (e.g.
`..._w_1emtltkel: false`). Check with `isinstance(v, dict)` while parsing.

### Known keys (derived from the recording)

| Hash key | Meaning | Example value |
|---|---|---|
| `w_1ekeigkin` | pH actual value | 7.2 |
| `w_1ekeiqfat` | pH target value | 7.1 |
| `w_1eklenb23` | ORP/redox actual value | 845 mV |
| `w_1eklgnjk2` | ORP target value | 675 mV |
| `w_1eo03t46k` | free chlorine (ppm), inactive | 0 |
| `w_1eommf39k` | water temperature | 34.5 °C |
| `w_1eklg44ro` | pH dosing direction | `ACID` |
| `w_1eklgnolb` | ORP dosing direction | `LOW` |
| `w_1eklh8gb7` | pH calibration type | `2_POINTS` |
| `w_1eklhs3b4` / `w_1eklhs65u` | pH calibration points | 4 / 58 mV |
| `w_1eklh8i5t` | ORP calibration type | `1_POINT` |
| `w_1eklhs8r3` / `w_1eklhsase` | ORP calibration points | 0 / 1.04 |
| `w_1eklj6euj`, `w_1eo1s18s8`, `w_1eklj12vv`, `w_1eo1v3q21` | dosing modes | `PROPORTIONAL` |

**Don't guess the keys yourself.** `python-pooldose` contains complete
mapping tables for this model. Resolve via:

```bash
pooldose --host 192.168.0.74 --analyze
# or grep for a hash directly in the package directory
```

Use these mappings as the source of truth instead of building our own
tables.

### Label decoding

Enum-like values come as i18n keys wrapped in pipes:

```
"|PDPR1H04AW100_FW539292_LABEL_w_1eklg44ro_ACID|"
```

**Don't split from the right** — `_2_POINTS` and `__C` (= °C) break that
way. Instead, strip the known prefix:

```python
def label(key, value, model, fw):
    if isinstance(value, str) and value.startswith("|"):
        inner = value.strip("|")
        return inner.removeprefix(f"{model}_{fw}_LABEL_{key}_").rstrip("_")
    return value
```

`comboitems` use the same scheme with `COMBO` instead of `LABEL`.

## Why this is easy on the device

`deviceInfo.modbus_status: "on"` — the WiFi module talks to the dosing
controller internally over Modbus, and does so **on its own schedule,
independent of listeners**. An additional WebSocket listener therefore
creates **no additional Modbus load** on the controller. That rules out the
one path through which the actual dosing control could have been disturbed.

Still: **keep exactly one connection.** This is an embedded stack with few
sockets; every extra client makes the tick dropouts observed above worse.
Close web UI tabs while it's running.

## Write access — caution

The value objects contain `set`, `absMin`, `absMax`, `resolution` — i.e.
write metadata. This channel very likely also allows changing target
values — **with no apparent authentication**.

The client should initially be implemented **read-only**. Writing is a
separate, deliberate feature. A malformed frame can change parameters of a
real dosing system.

## Implementation requirements

- Python, async (`websockets` or `aiohttp`)
- Reconnect with exponential backoff (start ~2 s, cap ~60 s)
- Watchdog: 30 s without a frame → rebuild the connection
- Host/port configurable, no hardcoding
- Robust against incomplete cycles and unknown topics
- Output to MQTT with Home Assistant discovery
- **Debouncing/throttling before MQTT:** with ~40 entities at 4 s intervals,
  you'd otherwise get ~20 million recorder rows per year. Only publish on a
  value change, plus a heartbeat every few minutes. For noisy sensors (pH,
  ORP), consider tying the minimum delta to `resolution`.
- Set an availability topic for MQTT (last will) so HA can see an outage

## Open items

- Behavior with multiple simultaneous clients not systematically tested
- Frames of other topics (`wdp_status`, `wifi_status`) not yet evaluated
- Whether the device sends immediately on a value change outside the tick:
  unknown
- Write frame format: unknown (deliberately untested)
