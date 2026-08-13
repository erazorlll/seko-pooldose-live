# Concept: `pooldose_live` — HACS integration with WebSocket push

As of: 2026-08-12
Target device: SEKO PoolDose Double Spa, `PDPR1H04AW100`, FW `539292`

This document is an **analysis + concept**, not an implementation. All claims
are checked against the source code of the existing solutions or against a
real payload from the same device model. Where something couldn't be
substantiated, that's stated explicitly.

---

## 1. Basis of analysis

| Source | Version / state | How checked |
|---|---|---|
| `python-pooldose` | 0.9.6 (`src/pooldose/__init__.py`) | repo cloned, source read |
| HA core integration `pooldose` | `homeassistant/components/pooldose`, `requirements: python-pooldose==0.9.6` | sparse checkout of `home-assistant/core`, source read |
| Mapping of the target device | `model_PDPR1H04AW100_FW539292.json`, 64 entries | read in full |
| Real device payload | `instantvalues.json` from issue lmaertin/python-pooldose#20 (exactly this model + FW) | downloaded, evaluated programmatically |
| `websocker-spec.md` | recording from our own device | cross-checked against code + payload |
| Documentation page | home-assistant.io/integrations/pooldose | fetched |
| Issues | lmaertin/python-pooldose, home-assistant/core | listed, #20 and #51 read in full |

---

## 2. How the current solution works

```
HA coordinator (600 s)
  └─ PooldoseClient.instant_values_structured()
       └─ RequestHandler.get_values_raw()
            └─ POST http://<host>/api/v1/DWI/getInstantValues   → 13.6 KB JSON
       └─ InstantValues(raw, mapping, prefix, device_id)
            └─ mapping = model_<MODEL_ID>_FW<FW_CODE>.json  (strict, no fallback)
       └─ to_structured_dict() → {"sensor": {...}, "number": {...}, ...}
  └─ Filter entities: `if description.key in coordinator.data[platform]`
```

Write access goes through `POST /api/v1/DWI/setInstantValues`.
The library already uses the WebSocket — but only selectively
(`get_cloud_status()`, `get_wifi_rssi()` in `request_handler.py:549-601`), and
the HA integration calls **neither of them**. `instant_values` over WebSocket
is currently used by nobody.

---

## 3. Findings — what's actually broken today

### B1 — 600 s latency, by design

`coordinator.py:37` → `update_interval=timedelta(seconds=600)`.
The docs justify this with "The device does not support frequent requests and
may become unstable with shorter intervals". That's a statement about
**HTTP requests**, not about the push stream that's running anyway. The
WebSocket tick is 4.2 s → **factor ~143** in freshness.

Practical consequence: a dosing reaction (pH pump on, ORP rising) can no
longer be resolved on the graph. At 4.2 s, it can.

### B2 — A firmware update kills the entire integration

`mapping_info.py:113` builds the filename strictly:
`model_{MODEL_ID}_FW{FW_CODE}.json`. No fallback, no "closest FW".

Chain when a mapping is missing:
`MappingInfo.load()` → `MAPPING_NOT_FOUND`
→ `client.instant_values()` → `UNKNOWN_ERROR` (`client.py:279`)
→ `coordinator._async_update_data()` → `UpdateFailed` (`coordinator.py:65`)
→ **all entities gone, config entry stuck in a retry loop.**

This isn't a theoretical case: issue #20 is **exactly this device model**
(`PDPR1H04AW100 / FW539292`) and literally says "Missing mapping file causes
setup failure in HA". The mapping was later contributed by a community
member. The same thing happens again on the next FW revision.

### B3 — `visible: false` is ignored → phantom entities

`InstantValues.to_structured_dict()` (`instant_values.py:108-176`) only
checks whether a raw entry exists — the `visible` field is **never read
anywhere in the library**.

In the real payload of this model, **17 of 74 value objects are
`visible: false`**. Among them the chlorine channel:

```json
"PDPR1H04AW100_FW539292_w_1eo03t46k": {
  "visible": false, "alarm": true, "current": 0, "magnitude": ["ppm","PPM"]
}
```

The mapping maps exactly this key to `cl` (type `sensor`), `sensor.py` has a
`cl` description → HA creates a chlorine sensor that **permanently reports
0 ppm**, even though no probe is connected. The user can't tell "0 ppm
measured" apart from "doesn't exist here at all".

(Open issue #51 "Chlorine reading is no longer available" is in the same
corner — cause not yet confirmed there.)

### B4 — The per-channel `alarm` flag is completely discarded

Every value object carries an `alarm: true|false`. None of the `_process_*`
methods in `instant_values.py` reads it.

In the real payload:

```json
"…w_1eklenb23": { "visible": true, "alarm": true, "current": 311,
                  "minT": 600, "maxT": 800, "magnitude": ["mV","MV"] }
```

ORP sits at 311 mV against a target window of 600–800 mV — the device
reports an alarm. In HA you only see the number 311. No `binary_sensor`, no
attributes, nothing.

### B5 — Alarm thresholds (`minT`/`maxT`) aren't exposed

`_process_number_value()` only reads `minT`/`maxT` when the mapping entry has
a `"field"` set. The mapping for this model has `field` on **no** entry. The
6–8 pH / 600–800 mV / 10–41 °C thresholds are in the payload and never make
it through.

The HA integration does have `ofa_ph_lower/upper`, `ofa_orp_lower/upper`,
`ofa_cl_lower/upper` as number descriptions — but the mapping entries for
this model don't exist, so the entities never appear.

### B6 — A large part of the data stream is never mapped

Evaluated programmatically (real payload against the mapping file):

| | Count |
|---|---|
| Raw keys with model prefix | 77 |
| of which in the mapping | 56 |
| **unmapped** | **21** |
| of which `visible: true` (i.e. actually active) | 9 |

Among the unused entries: four timers with `visible: true` (`w_1eo1u7pjf` =
20 s, `w_1eo1ucpcr` = 360 s, `w_1eo1uen7i` = 31 s, `w_1eo1uep02` = 360 s), two
active dosing modes (`w_1eklj12vv`, `w_1eo1v3q21`, both `TIMED`), and eight
status flags `w_1fakp…/w_1fakq…`.

### B7 — 9 mapped values don't have an HA entity at all

Diffing the mapping against the HA integration's `EntityDescription` lists,
for this model:

| Platform | in the mapping, but no entity |
|---|---|
| `number` | `time_on_ph_dosing`, `time_on_orp_dosing`, `time_on_cl_dosing` |
| `binary_sensor` | `alarm_cl_too_low`, `cl_level_alarm` |
| `sensor` | `cl_calibration_type/offset/slope`, `ofa_cl_time` |

The `time_off_*_dosing` entries exist, the `time_on_*_dosing` ones are
missing — meaning you can set the dosing off-time in HA, but not the on-time.
Of 64 mapping entries, only **55** effectively become entities.

### B8 — Every write costs one extra full fetch

`PooldoseClient.set_number/set_switch/set_select` (`client.py:315-338`) each
first call `self.instant_values()` — i.e. a full `POST getInstantValues`
(13.6 KB) — just to then issue `set_value`. **One click = 2 HTTP requests.**
Exactly the behavior the 600 s interval is supposed to avoid.

Afterward, `number.py:187` sets the value optimistically
(`_attr_native_value = value`) without `async_request_refresh()`. If the
device's response differs (rounding, rejection, step correction), HA shows
a wrong value for up to **10 minutes**.

### B9 — A single failed poll makes everything unavailable

`_async_update_data()` raises `UpdateFailed` on every non-SUCCESS. The
`DataUpdateCoordinator` then sets `last_update_success = False`, and
`CoordinatorEntity.available` becomes `False` for **all** entities. Next
attempt: up to 600 s later.

The docs promise something different here: "The system caches values for up
to 300 seconds during temporary unresponsiveness". That buffer doesn't exist
in the code. `get_values_raw()` does return `RequestStatus.LAST_DATA` with
the last data on network errors (`request_handler.py:415`) — but
`client.instant_values()` discards that one line later
(`if status != RequestStatus.SUCCESS … return status, None`). `LAST_DATA`
appears **nowhere** in `homeassistant/components/pooldose/`. The last-data
fallback is dead code.

### B10 — A robustness detail in the library

`RequestHandler._get_websocket_data()` (`request_handler.py:562`) has a
`while True: msg = await ws.recv()` **without a timeout**. If the expected
topic never arrives (`wdp_status` is "rare/initial" per the spec), the task
hangs indefinitely. Only relevant to us if we reused this code path — we
don't.

---

## 4. Checking the spec — what's confirmed, what isn't

### Confirmed

| Claim in the spec | Evidence |
|---|---|
| Data structure `devicedata.<SERIAL>_DEVICE.<MODEL>_FW<FW>_<hash>` | real HTTP payload has exactly this shape → **WS and HTTP payloads are structurally identical below `devicedata`** |
| Value object fields `current/set/absMin/absMax/minT/maxT/alarm/visible/magnitude/resolution` | all present in the payload; `ph_target` carries `current: 7.4` **and** `set: 7.4` |
| "Some entries are a bare boolean" | confirmed: `w_1emtltkel`, `w_1eklft47q`, `w_1eklft5qt` — of all things, all three switches |
| Human-readable keys alongside the hash scheme | `Elapsed_PowerON_Delay`, `Elapsed_FlowON_Delay` in the payload |
| `visible: false` as a filter, ppm channel with a permanent alarm | reproduced exactly (see B3) |
| "Don't guess the keys, use `python-pooldose` as the source of truth" | mapping for this model exists and covers 56 of 77 raw keys |
| `pooldose --host … --analyze` | CLI flag exists (`__main__.py:279`), plus `--analyze-all` for hidden widgets |
| Model/FW/serial derivable from the data keys | `DeviceAnalyzer._extract_device_info()` does exactly that via regex — **we don't need an HTTP call at runtime to know the prefix** |

### Not confirmed / needs correcting

**"The WebSocket generates less load than the previous poll."**
That's not substantiated, and likely wrong along one dimension.

- *On the dosing controller* (Modbus path): plausible. `deviceInfo.modbus_status: "on"`
  and the fixed 4.2 s scheduler suggest the WiFi module polls independently of
  listeners. A listener creates **no** extra load there. The HTTP poll, by
  contrast, at least triggers request handling.
- *On the WiFi module*: listening costs more, not less. A calculation with the
  real payload size (13.6 KB per full cycle):

  | | Requests/day | Bytes/day |
  |---|---|---|
  | HTTP poll, 600 s | 144 | ~1.9 MB |
  | WS stream, 4.2 s | 0 (1 connection) | ~267 MB |

  That's ~20,570 serialization and send operations per day on an embedded
  stack. The tick dropouts observed in the spec itself (16 of 24 cycles
  arrived) are a hint that the module isn't just idling there.

**Open and measurable:** does the device send the stream even when
**nobody** is connected? If so, listening really is free. If not, we're
buying freshness with module load. That's measurable (section 8) and should
be settled before rollout.

**Honest framing of the benefit:** we gain **latency** and avoid
**request spikes** on the device's HTTP server. "Goes easy on the device" is
demonstrably true only for the controller, not across the board.

**"Factor ~143 in freshness" — too optimistic after the first 11h
measurement.** That was a theoretical calculation from the nominal 4.2 s
tick. An 11-hour recording (§8.2) shows two things: the nominal full cycle
(both chunks) really does run at ~4.0–4.2 s as described in the spec — but
`instant_values` drops out much more often during an otherwise intact
connection than the short spec recording suggested, and on top of that there
are recurring multi-minute total outages with an otherwise intact
connection. The real average cycle time over 11h was ~14.9 s. The factor
against 600 s stays large (~40×), but shouldn't be confused with the
theoretical value of ~143×. Details and raw numbers in §8.2.

---

## 5. Concept for the new integration

### 5.1 Core decisions

| Decision | Rationale |
|---|---|
| **Own domain `pooldose_live`**, not `pooldose` | A custom component with domain `pooldose` globally overrides the core integration. Own domain → both installable in parallel, clean comparison, no friction with HA updates. |
| `iot_class: local_push` | technically correct and makes the difference visible externally |
| **Vendor the mapping JSONs** instead of `python-pooldose` as a requirement | see 5.2 — reasoned in detail |
| **v1 reads only via WS, writes via HTTP** | The WS write format is unknown, and the spec rightly warns that a malformed frame can change parameters of a real dosing system. `setInstantValues` is known and proven. |
| **Exactly one WS connection per device** | spec recommendation; the connection lives in the config entry, not per entity |

### 5.2 Dealing with `python-pooldose`: copy the mappings, write our own code

Three options were on the table: (a) include it as a requirement, (b) take
parts of it, (c) go fully standalone. Decision: **(b), narrowly scoped — only
the mapping JSONs.**

**Against (a) — including it as a requirement:**

1. *What we'd need isn't a public API.* `docs/api-reference.md` documents
   `PooldoseClient`, `RequestStatus`, and of `InstantValues` only the dict
   interface plus the setters. The constructor
   `InstantValues(device_data, mapping, prefix, device_id, request_handler)`
   is documented nowhere and is only called internally within the library
   (`client.py:288`, `mock_client.py:211`). That's exactly what we'd need to
   feed in WS snapshots — the documented path `client.instant_values()`
   always fetches over HTTP. The same applies to `RequestHandler.set_value`
   for writing. We'd be tying ourselves to two internal signatures.
2. *Version pin conflict.* Core pins `python-pooldose==0.9.6`. If both
   integrations run in the same environment and we pin something different,
   the requirement checks fight each other. We'd have to permanently follow
   core's pin — losing the one reason to have a dependency in the first
   place.
3. *We need different semantics anyway.* `visible`, `alarm`, `minT/maxT`,
   `set` vs. `current`, raw fallback — `InstantValues` provides none of that.
   We'd be including the library and bypassing half of its processing.

**Against (c) — build everything ourselves:** the 1,302 lines of mapping
JSON are the actual value in that repo. The hash keys are opaque and can't be
derived; the tables were built by hand from debug dumps (our model: a
community contribution in issue #20). Rebuilding them would just destroy
value.

**Split:**

| Component | Origin |
|---|---|
| 6 mapping JSONs + `MODEL_ALIASES` | copied, MIT, attribution in the file header and in the README |
| WS transport, reassembly, watchdog | ours — doesn't exist there |
| Decoder raw → `Channel` | ours, ~150–200 lines; less effort than bending `InstantValues` to our needs |
| Mapping loader with FW fallback | ours — the one there is strict (finding B2) |
| Writing `POST setInstantValues` | ours, ~30 lines: `{device_id: {full_key: [{"value": v, "type": "NUMBER"}]}}` |

Result: no runtime dependency, no conflict with the core integration, no
coupling to undocumented signatures.

*Cost:* the mappings will drift. Mitigation: a CI job that diffs against
upstream weekly and opens an issue on divergence — plus the raw mode (5.5),
which handles new devices better than upstream does anyway.

*Condition for switching to (a):* if `python-pooldose` documents the
`InstantValues` constructor as an API and commits to constructing it from a
raw dict without HTTP. That's a reasonable upstream proposal from us — the
library would then get a WS path too.

**Addendum from P6:** the same copy-instead-of-depend logic eventually hit us
with our OWN library `pooldose_live` too — a component installed via HACS
only ships `custom_components/pooldose_live/`, not the separately
`pip install`-ed `src/pooldose_live/`. Solved through vendoring
(`vendor/pooldose_live/`, a 1:1 copy kept in sync via a script) instead of a
PyPI release — details in
`custom_components/pooldose_live/vendor/README.md`.

### 5.3 Layers

```
┌─ transport/socket.py ──────────────────────────────────────────┐
│  one ws://<host>:1334 connection                                │
│  · reassembly by progressInfo (offset==1 → clear the buffer)   │
│  · connection watchdog: 30 s without ANY frame → reconnect      │
│  · staleness watchdog separately on instant_values (§8.2/§9):   │
│    wifi_station/time keep running even when instant_values      │
│    drops out for minutes - a plain frame watchdog isn't         │
│    enough to catch that                                         │
│  · backoff 2 s → 60 s, exponential                              │
│  · unknown topics are dropped, not logged/flooded              │
│  → delivers complete snapshots as a callback                   │
└────────────────────────────────────────────────────────────────┘
┌─ decode/channels.py ───────────────────────────────────────────┐
│  raw dict → dict[hash, Channel]                                │
│  Channel = current | set | absMin absMax | minT maxT |         │
│            resolution | unit | alarm | visible | raw           │
│  · derive the prefix from the keys themselves (no HTTP needed) │
│  · bare-bool entries → Channel(current=bool)                   │
│  · label decoding via removeprefix (not rsplit)                │
└────────────────────────────────────────────────────────────────┘
┌─ naming/mapping.py ────────────────────────────────────────────┐
│  hash → readable name, from vendored JSONs                     │
│  · exact model+fw match                                        │
│  · else: same model, different FW → load + warn                │
│  · else: raw mode (see 5.5)                                    │
└────────────────────────────────────────────────────────────────┘
┌─ coordinator.py ───────────────────────────────────────────────┐
│  DataUpdateCoordinator(update_interval=None)                   │
│  socket callback → decode → throttle → async_set_updated_data  │
└────────────────────────────────────────────────────────────────┘
┌─ platforms: sensor / binary_sensor / number / select / switch  │
└────────────────────────────────────────────────────────────────┘
```

### 5.4 What this resolves from the findings in §3

| Finding | Resolution |
|---|---|
| B1 latency | push instead of poll. `update_interval=None`, `async_set_updated_data()` from the WS callback. |
| B2 FW cliff | three-tier mapping fallback (5.5). A FW update degrades the names, doesn't kill the integration. |
| B3 `visible:false` | channels with `visible: false` create **no** entity. If `visible` changes at runtime (a probe gets retrofitted), the entity appears on the next reload — optionally reported as a repair issue. |
| B4 `alarm` | a `binary_sensor` per channel with an `alarm` field (device_class `problem`, `entity_registry_enabled_default=False`), plus `alarm` as an attribute on the main sensor. |
| B5 thresholds | `minT`/`maxT` as attributes on the sensor and optionally as `number` entities (category `config`, disabled by default). Come generically from the payload, no mapping entry needed. |
| B6/B7 dead values | anything that's `visible: true` and has a value becomes an entity — mapped names cleanly, unmapped ones as `raw_<hash>`, disabled by default. Nothing gets lost anymore. |
| B8 write overhead | direct `POST setInstantValues` with the prefix+key from our own snapshot. **No pre-emptive GET.** No optimistic display needed — the next tick confirms within ~4 s. If confirmation doesn't arrive, the value is reverted and the action reported as failed. |
| B9 unavailable flapping | availability hangs off WS liveness (last frame < watchdog window), not off a single fetch. Brief dropouts (spec: up to 16 s observed) don't do anything. |

### 5.5 Three-tier mapping fallback

1. **Exact** `model_<MODEL>_FW<FW>.json` → full readable names, everything as
   usual.
2. **Model matches, FW doesn't** → load the closest FW file of the same
   model. Hash keys are empirically FW-stable; unknown hashes fall through to
   tier 3. Visible warning in the log + a repair issue "no mapping for FW X".
3. **Raw mode** → entities built directly from the payload:
   - guess the type from the value object's structure: `set` present →
     `number`; `comboitems` → `select`; `current` is `"O"/"F"` or bool →
     `binary_sensor`; otherwise `sensor`.
   - unit from `magnitude[0]`, bounds from `absMin/absMax`, step from
     `resolution`.
   - name `raw_<hash>`, `entity_registry_enabled_default=False`.

That makes the situation from issue #20 (new device, no mapping) no longer a
total outage, just a loss of convenience — and the user can see data
immediately and contribute a mapping from it.

**Repair issue implemented in P5** (`coordinator._report_mapping_status()`),
broader than originally planned here: not just on FW fallback (tier 2), but
also in raw mode (tier 3) — that's where name resolution is worst, so it's
the most important to surface. One issue per host (multiple devices don't
collide), removed again when the config entry is unloaded.

### 5.6 Debouncing / recorder

**Implemented in P3** (`custom_components/pooldose_live/entity.py`): a
resolution-aware change filter per entity plus a 5-minute heartbeat, on top
of the coarse coordinator-wide equality check from P2. The details below are
the original design, which was implemented as described.

This is **the** critical spot at 4.2 s. With ~40 entities and one write per
tick, that would be ~9.5 state writes/s. HA fires a `state_changed` event on
every `async_write_ha_state()`, and the recorder writes one row per event —
even for an identical value. Unthrottled: hundreds of millions of rows per
year. The "~20 million" figure in the spec is set considerably too low.

Two brakes, both necessary:

1. **Coordinator level:** `async_set_updated_data()` only if at least one
   channel changed relevantly.
2. **Entity level:** override `_handle_coordinator_update()` and skip
   `async_write_ha_state()` if the entity's *own* value hasn't changed.
   Without this, every one of the 40 entities writes on any change to any
   channel.

Relevance criterion per channel:
- numeric: `abs(new - old) >= resolution` (if `resolution` comes in as
  `"NA"`: every change counts)
- otherwise: any value change
- plus a **heartbeat**: write through once every N minutes (default 5,
  configurable) so recorder histories don't develop gaps

For pH/ORP that realistically works out to a handful of writes per minute
instead of 14/minute.

### 5.7 Setup flow — revised in P2 (2026-08-13)

The original plan here assumed "30 s timeout, covers the observed longest gap
of 16 s" — that was the state before P0. The 11h measurement (§8.2) and the
HTTP baseline (§8.3) have since shown: real dropouts run up to 9.7 minutes,
occurring 30–70% of the time. A setup step that waits for a complete cycle
would fail for no reason on a regular basis, or hang for minutes — exactly
what we experienced ourselves during the P1 live test (§8.4). Rebuilt
accordingly, implemented in P2, and verified against the real device
(including while the system was in standby):

```
config_flow: enter host
  → test connection to ws://<host>:1334 - only the WS handshake is checked
      (10 s timeout), it does NOT wait for an instant_values cycle
  → unique_id = host (provisional, see below)
  → create the entry, setup returns immediately

Runtime: WebSocket only. No periodic HTTP traffic.
HTTP only for write access (P4).

Entities appear dynamically as soon as the first instant_values cycle
arrives - not during setup itself. Model/FW/channel names come from the
data keys as described in §4, no HTTP call needed.
```

**Deliberate simplification, still open:** `unique_id` is provisionally the
host, not the serial number — per the spec that's only known after the first
cycle (part of the devicedata key), which isn't waited for here. Migrating to
the serial number (once known) later is an open follow-up, not P2 scope.
Downside: if the device's IP changes, this currently creates a second config
entry instead of updating the existing one — the official integration solves
this via `async_step_reconfigure` using the stable serial number.

DHCP discovery (`hostname: kommspot`) can be taken over from the core
integration. Caveat: if both run in parallel, the flows compete for the same
device — a deliberate choice for the testing phase; in the target picture the
core integration gets disabled.

### 5.8 Diagnostics

`diagnostics.py` should output the **complete last raw snapshot** (serial and
WiFi data redacted), plus tick statistics: frames/minute, missed ticks,
reconnects, longest gap. Exactly the material that today's mapping
contributions in issue #20 grew out of — without the CLI fiddling.

**Implemented in P3** (`custom_components/pooldose_live/diagnostics.py`):
mapping status and coverage, session statistics (connects/disconnects/
watchdog trips/cycles, longest gap, last known standby state), resolved
channels, last raw snapshot (device ID redacted). Only for the running
session, not a replacement for a real recording like in P0 — `tools/ws_probe.py`
remains the right tool for that.

### 5.9 Writing (P4)

Implemented in `pooldose_live/write.py` (library) +
`number.py`/`select.py`/`switch.py` (HA platforms). Core decisions:

- **No pre-emptive GET** (avoids B8): validation runs against the most
  recently received WS snapshot, not against a freshly fetched one.
  Confirmation arrives with the next tick (~4s) through the normal
  coordinator path — no optimistic setting of local state like the reference
  integration (there, up to 10 minutes of a wrong displayed value is
  possible if the device disagrees, see B8).
- **Validation before every request**: range/step for `number`
  (`absMin`/`absMax`/`resolution` from the current channel), valid options
  for `select` (reverse lookup over the generically decoded `comboitems`,
  works in raw mode too). Invalid input is rejected locally, no request goes
  out — verified with 5 tests (`tests/test_write.py`,
  `tests/test_p4_manual.py`), including the payload structure
  (`{device_id: {full_key: [{"value", "type"}]}}`).
- **`switch` only on an actual mapping hit**: in raw mode, bare booleans are
  classified as `binary_sensor`, not `switch` (concept §5.5) — without a
  mapping table it's unknown whether a channel is actually writable.
- **Bug found while implementing this**: `ModelMapping.matched_fw` was
  formatted inconsistently (with an `"FW"` prefix in the EXACT branch,
  without one in the FW_FALLBACK branch) — safety-relevant for building the
  prefix when writing, since a wrong prefix could hit the wrong channel.
  Fixed and covered by a regression test
  (`tests/test_write.py::test_build_prefix_exact/raw`). Never noticed
  before because `matched_fw` had only shown up cosmetically in `probe.py`'s
  table output until then.

**Not yet tested live against the real device.** Unlike reading, writing
isn't risk-free — the spec explicitly warns that a malformed frame can
change parameters of a real dosing system. Encoding and validation are
fully tested offline (see above); a first real write attempt on the device
should happen deliberately, with a low-risk target value (e.g. setting a
target value to its own current value), not automated.

---

## 6. Advantages over today — soberly assessed

**Substantiated (updated after the 11h measurement, §8.2):**
- Freshness: real average cycle time ~14.9 s instead of 600 s (factor ~40,
  not the theoretical ~143 from the nominal 4.2 s tick — details in §4 and
  §8.2).
- No more periodic HTTP requests (currently 144/day), no request spikes on
  the device's web server.
- Writes: 1 HTTP request instead of 2, and confirmation after ~4 s instead
  of up to 600 s.
- A FW update no longer breaks the integration (B2).
- ~19 more values from the same data stream (B6/B7), plus `alarm` and
  `minT/maxT` (B4/B5) — `alarm` isn't a niche case: the ORP channel alone
  changed value 1,937 times in 11h.
- No more phantom sensors for unpopulated channels (B3).
- Brief dropouts no longer cause "everything unavailable" (B9).

**Plausible, but unproven:**
- Lower load on the dosing controller. Argument: Modbus runs on its own
  schedule. This is a deduction from `modbus_status: "on"`, not a
  measurement.

**Subsequently supported by §8.3:** the recurring, multi-minute
`instant_values` dropouts with an intact connection (§8.2) occurred in a
108-minute baseline **just as much, if anything more, entirely without a WS
client** (§8.3) — pointing toward a device-inherent effect, not one caused
by our listening. Not a strict disproof of the opposite (different time of
day, no mirror-port test), but no longer an argument against the persistent
connection as the core idea either.

**Costs, honestly stated:**
- A permanently open TCP connection to the device.
- ~51 MB/day measured (extrapolated from 11h, including all topics) —
  considerably less than the initial worst-case estimate of ~267 MB/day, but
  still ~27× more than today's HTTP poll (~1.9 MB/day).
- Considerably more serialization work on the WiFi module.
- Debouncing is mandatory, not optional — without it you ruin the recorder
  DB (measured: 422,600 vs. 6,200 writes/day, §8.2).
- Web UI tabs in the browser are now a genuine disruptive factor (extra
  client).
- **New:** `instant_values` can be stale for minutes despite an open
  connection (§8.2/§9) — the integration needs an additional staleness
  check that the original watchdog idea alone doesn't cover.

---

## 7. Phase plan

| Phase | Content | Result |
|---|---|---|
| **P0** ✅ | Standalone logger: record WS traffic, count ticks/gaps/reconnects, snapshots as JSONL | data basis + measurements from §8, including the HTTP baseline (§8.3) |
| **P1** ✅ | Transport + decoder + mapping loader, without HA — including raw mode + FW fallback (pulled forward from P5, needed anyway to test the loader) | `python -m pooldose_live.probe --host …` shows resolved channels. Package `src/pooldose_live/` |
| **P2** ✅ | HA skeleton: manifest, config_flow, coordinator, `sensor` + `binary_sensor`, read-only. Setup flow revised versus the original plan (§5.7) | `custom_components/pooldose_live/`, installable in parallel with the core integration |
| **P3** ✅ | Debouncing (resolution-aware + heartbeat), availability logic (standby exception, §8.4), `diagnostics.py` | recorder-friendly, ready for everyday use |
| **P4** ✅ | Writing: `number`, `select`, `switch` over HTTP `setInstantValues`, without a pre-emptive GET (B8 avoided) | functional parity with core. Not yet tested live against the real device (see §5.9) |
| **P5** ✅ | Repair issues on FW fallback AND raw mode (broader than originally planned), translations (de/en) | the user visibly sees when name resolution isn't optimal, with a pointer to contributing a mapping |
| **P6** ✅ | HACS compliance: `hacs.json`, vendoring (fixes the P2 gap), CI validation, `LICENSE`, release tag | actually installable via HACS, not just structurally prepared |
| **P7** | Optional: explore the WS write format — **separately, deliberately, not on the production device** | open |

Feeding back upstream: B3 (`visible`), B4 (`alarm`), B7 (missing
descriptions), and B9 (dead `LAST_DATA` path) are self-contained, small fixes
for `python-pooldose` or the core integration. We should file those as
issues/PRs independent of this project — they'd help everyone who stays on
polling, too.

---

## 8. Measurements (P0)

### 8.1 Question catalog

| # | Question | Status |
|---|---|---|
| 1 | **Does the device send without a listener?** Determines whether "goes easy on the device" holds up — and whether the dropouts found in 8.2 are self-inflicted. | **answered (approximately)** — an HTTP baseline without a WS client shows the same phenomenon, if anything stronger. See 8.3. A definitive answer without any client at all would need a mirror port |
| 2 | **Tick dropout rate** with 0/1/2 simultaneous clients. The spec saw 16/24 cycles with one client — reproducible? | **answered** for 1 client over 11h (8.2) and for "0 WS clients" via the HTTP approximation (8.3). Still open for 2 clients |
| 3 | **Response time of `GET /`** with and without an active WS listener. Shows whether the listener slows down the HTTP server. | **still open** — 8.2 measured a static file (`params.js`, ~230ms), 8.3 the actual data endpoint (`getInstantValues`, ~455ms). Different endpoints, not a fair comparison |
| 4 | **Reconnect behavior:** does an `offset:1` frame arrive immediately, or mid-cycle? | **answered**, see 8.2 |
| 5 | **Does `visible` change at runtime?** Observe over 24h. | not observed over 11h (see 8.2) — a 24h repeat would make sense, but is no longer top priority |
| 6 | **Do values arrive outside the tick**, if you change something on the device? | open |

### 8.2 Results: 11h recording (2026-08-12, 1 client)

Tool: [`tools/ws_probe.py`](../tools/ws_probe.py). Recorded 10:46–21:48,
39,744 s of effective runtime, 10,186 frames, 2,663 complete cycles, 8
reconnects, 0 watchdog trips. Raw data lives locally under `recordings/`
(git-ignored, not in the repo — contains device data).

**Chunking/reassembly (question 4):** across all 8 reconnects and the entire
recording, only **3 aborted cycles** (0.1%) — the buffer rule "clear on
`offset==1`" holds up. Both chunks of a cycle arrive practically
simultaneously (median gap < 100 ms), not spread across two ticks — the full
cycle is nominally **~4.0–4.2 s**, not ~8.4 s as originally assumed.

**Tick dropout rate within a connection (question 2):** of the cycle
intervals *within* the same connection (reconnect gaps excluded exactly, not
estimated — see `intra_session_intervals()` in the tool), **47.5% were not**
the nominal ~4-second tick. This confirms and sharpens the observation from
the original spec (there, 8/24 = 33% on a short recording).

**New finding — `instant_values` repeatedly stops completely for minutes,
with an otherwise intact connection:** 86 times in 11h a cycle gap > 60 s,
the largest 393 s (6.5 min). Total: **~200 of 662 minutes of total runtime
(≈ 30%)**. In all cases examined (a sample of the 5 largest gaps checked in
detail):

- no `disconnect`/`connect` event during the gap — the WebSocket connection
  stays open throughout
- `wifi_station` (~every 17 s) and `time` (~every 25 s) keep running
  **completely unchanged, at the usual pace**
- after the gap, two cycles arrive close together (~4 s apart), then normal
  operation

This narrows down the cause considerably: not a network problem (otherwise
`wifi_station`/`time` would drop out too), not a WebSocket problem (the
connection stays open). The device is deliberately, internally suspending
just the `instant_values` generation for that time.

**Impact on the watchdog design (§5.3):** an "any frame within 30 s" watchdog
(as originally planned) does **not** catch this — `wifi_station`/`time` keep
it satisfied throughout, while `instant_values` is stale for up to 6.5
minutes. HA would show a sensor as "available" with a silent, stale value
during that time instead of marking it unavailable. **Consequence: the
integration needs a second, topic-specific staleness check on
`instant_values` itself**, independent of the connection watchdog (see the
risk in §9).

**Reconnects (question 4):** 8 over 11h, all cheap — on average only 2.25 s
until reconnection (backoff stayed at the 2 s starting tier almost every
time, since the next attempt almost always succeeded immediately). Pure
reconnect downtime: 18 s total — negligible against the 200 minutes of
`instant_values` silence above. This confirms: the problem isn't connection
stability, it's something internal to the device.

**`visible` (question 5):** unchanged for all 73 observed channels over 11h.
No conclusion possible for the full 24h cycle, but no counter-evidence
either.

**`alarm` as a live signal:** the ORP channel (`w_1eklenb23`) changed value
in 1,937 of 2,663 cycles (72.7%) and was in an alarm state for parts of the
measurement — a signal the current integration (finding B4) never shows.

**Debouncing (§5.6), now with real long-term numbers instead of a 45-second
sample:** ~422,600 state writes/day extrapolated unthrottled; ~6,200/day
(1.5% of that) with change-only writes. Confirmed: two-tier debouncing isn't
an optimization, it's a requirement.

**HTTP latency with an active WS listener (question 3, partial):** median
230 ms, p95 333 ms, n=133 (measured every 5 min, `GET /js_libs/params.js`).
A comparison value without a listener is still missing.

**Tool fixes along the way:** two bugs in `ws_probe.py` found and fixed
during the evaluation — `--duration` was ignored by the receive loop (the
watchdog never fires while the device is active, see commit history), and the
first baseline-tick estimate wrongly used the *smallest* interval cluster
instead of the *most frequent* one as the basis, which for an 11h recording
with several reconnects led to a false 0.67 s base tick and a misleading
"95.5% dropped" figure. Both numbers in this section are from the corrected
run.

### 8.3 Results: HTTP baseline without a WS client (2026-08-12, 22:08–23:57)

Tool: [`tools/http_baseline.py`](../tools/http_baseline.py). Answers the
causality question flagged in §10 as the most important open question: are
the `instant_values` dropouts found in 8.2 caused by our own WS listening?

**Method:** polled exclusively via `POST /api/v1/DWI/getInstantValues`, every
20 s, for 108.7 minutes (stopped early — 4h were planned, but the data was
already conclusive after ~1.5h). Port 1334 (WebSocket) was never touched
during this run. "Frozen" = the hash over all 52 visible `current` values
stays identical across multiple polls — not just a single channel, the
complete state.

| | HTTP-only, 108.7 min (8.3) | WS, 11h/662 min (8.2) |
|---|---|---|
| Events > 60 s | 24 | 86 |
| Share of frozen time | **65.0%** | 30.1% |
| Rate | 1 every 4.5 min | 1 every 7.7 min |
| Longest single run | **580 s (9.7 min)** | 393 s (6.5 min) |

**Finding: without any WS client, the dropout rate is at least as high as
with one — if anything higher, with a longer single run than anything in the
WS measurement.** If our WS listening were the stressor, the condition
without any WS client would have to be calmer. The opposite is the case.

**Limitations, stated honestly:**
- Different time of day (night instead of morning/afternoon) and therefore
  potentially a different operating state of the system — a possible
  confound, independent of the transport method.
- No genuine same-day, same-time-of-day comparison.
- Not proof of "nobody is connected" in the strict sense — HTTP polling is
  also load, just a different kind and at a lower frequency than an open WS
  socket. The one truly conclusive test (a mirror port on the switch/router,
  passively observing traffic) is still outstanding.
- Considerably shorter observation period (108.7 min vs. 662 min).

**Still a solid approximation:** the numbers were still unstable after the
first 17 minutes (86% frozen) and settled at ~65–69% over the full run, not
drifting further toward the WS numbers. Combined with the mechanism
described in §8.2 — WS simply stays quiet when there's no new data (push),
while HTTP has to answer every request and returns the last known state when
it does (pull) — this adds up to a coherent picture: **an internal
`instant_values` generation process that periodically pauses, independent of
the client.**

**Practical consequence:** question 1 counts as answered well enough not to
change the architecture — a persistent WS connection is no worse than the
status quo, if anything better (push silence instead of actively requested
stale data). The topic-specific staleness check on `instant_values` already
planned in §5.3/§9 remains mandatory either way, regardless of the cause.

### 8.4 Standby hypothesis checked (2026-08-13) — explains part of it, not the pattern from §8.2

An obvious guess during P1 development: if circulation isn't running, the
device can't validly measure pH/ORP and therefore pauses `instant_values`.
Checked concretely, with a mixed result.

**Confirmed live:** the `alarm_system_standby` channel (`w_1fai1n09b`,
already in the mapping as a `binary_sensor`) was `true` at the time of
checking — matching the multi-minute `instant_values` silence observed at
the same time in the running P1 probe test. User clarification: in this
case, standby was **not** triggered by the device's own flow-detection
logic, but by the user's own HA automation, which actively puts the system
into standby. `alarm_system_standby` is thus (at least also)
externally controlled, not a purely autonomous signal from the device.

**Disproven historically:** a systematic comparison of all 86 gaps > 60 s
from the 11h measurement (§8.2) against the same channel: **0 of 86** gaps
coincided with `alarm_system_standby = true`. The flag was active for only
~67 s in the entire 11h recording (9 of 2,646 cycles), outside every gap
found.

**Conclusion:** standby is a real, now-confirmed phenomenon that plausibly
pauses `instant_values` — but it does **not** explain the pattern
characterized in §8.2. These are two separate causes: deliberate
(externally triggered) standby on one hand, and the still-unexplained, more
frequent dropouts on the other. For the architecture, that means:
`alarm_system_standby` should be carried along as context information in P3
(availability logic) — "stale because standby is active" is a different
situation for the user than "stale, cause unknown" — but it doesn't solve
the actual mystery from §8.2.

---

## 9. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| `instant_values` stops for minutes, connection stays intact (§8.2: 86× in 11h WS, ~30%; §8.3: also ~65% without any WS client, so device-inherent). Standby (§8.4) demonstrably explains only a fraction of the cases (0/86 historical gaps correlate) | HA shows stale values as "available" without a plain connection watchdog noticing | second staleness check specifically on the `instant_values` timestamp, independent of the frame watchdog; carry `alarm_system_standby` along as context (§8.4); mark entities unavailable after a configurable timeout |
| WiFi module can't handle a persistent connection | tick dropouts, reboots | *De-risked* — §8.3 shows the phenomenon happens without a WS connection too, arguing against it being worsened by the persistent connection. Watchdog + backoff stay in place regardless; strict single-connection rule |
| Recorder DB fills up | HA gets slow | debouncing from P3 onward, not later; conservative default heartbeat |
| Reassembly delivers mixed-up cycles | wrong values | `offset==1` clears the buffer; only publish on `offset==total`; cycle counter in diagnostics |
| Both integrations active in parallel | 2 clients + polling, duplicate entities | separate domain makes it visible; README points out how to disable |
| Vendored mappings go stale | new models missing | sync script + CI job that diffs against upstream |
| Writing without authentication | a malformed frame changes real dosing parameters | v1 only writes through the known HTTP API; WS writing not until P7, never explored on the production device |

---

## 10. Open items

- ~~Does our own listening cause the `instant_values` dropouts from §8.2, or
  does that happen without a client too?~~ **Resolved (approximately), see
  §8.3:** a 108-minute HTTP-only baseline entirely without a WS connection
  shows the same phenomenon — if anything more often, and with a longer
  single run (9.7 min) than in the 11h WS data. Points toward
  device-inherent, not self-inflicted. Not strict proof (no mirror-port
  test, different time of day than the WS measurement), but solid enough to
  stick with the core idea (persistent connection).
- WS write format: unknown, deliberately untested (stays that way until P7).
- Topics `wdp_status`, `wifi_status` unclear in content —
  `wdp_status.connection` (cloud) and `wifi_station.rssi` are known from
  `python-pooldose` and can be picked up for free from the running stream,
  instead of opening a separate connection per query like that library does.
- Behavior with multiple simultaneous clients not systematically tested.
- Whether `visible` also applies to setpoint channels that should still be
  writable is still open — in the payload examined, all three switch
  channels are bare booleans without a `visible` field.
