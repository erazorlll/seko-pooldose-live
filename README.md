# seko-pooldose-live

Home Assistant integration (HACS) for SEKO PoolDose with **live data over the
local WebSocket** instead of HTTP polling.

Target device: SEKO PoolDose Double Spa (`PDPR1H04AW100`, FW `539292`).

Status: **P6 — HACS-compliant, actually installable.** P0–P5 are complete
(write access not yet tested live against the real device), see
[docs/concept.md](docs/concept.md). Only P7 remains, optionally (exploring the
WS write format, deliberately cautious).

## Installing via HACS

1. HACS → Integrations → ⋮ → Custom repositories
2. Add this repo as type "Integration"
3. Install "SEKO PoolDose (Live)", restart Home Assistant
4. Settings → Devices & Services → Add Integration → "SEKO PoolDose (Live)"

Self-contained — `custom_components/pooldose_live/` vendors the library
(`vendor/pooldose_live/`), no separate `pip install` needed.

## Why

The official HA integration (`pooldose`, library `python-pooldose`) polls an
HTTP API every 600 s. The device pushes the same data on its own over
`ws://<host>:1334/` at a ~4.2 s tick — no authentication, no subscribe frame.
Listening instead of asking brings a real-world factor of ~40 in freshness
(measured, see concept §8.2 — the initial theoretical factor of ~143 didn't
survive the measurement) and incidentally fixes a number of concrete
weaknesses in the current solution.

## Package `pooldose_live` (P1)

```bash
pip install -e .
python -m pooldose_live.probe --host 192.168.0.74
```

Connects, derives model/firmware directly from the data keys (no HTTP call
needed), loads the matching mapping table, and shows the resolved channels —
then every value change live. `--once` exits after the first table,
`--show-invisible` also shows `visible: false` channels.

| Module | Purpose |
|---|---|
| `transport.py` | One WS connection, reassembly, two watchdogs (connection + `instant_values` staleness, see concept §5.3), backoff |
| `channels.py` | Raw devicedata dict → `Channel` objects (label decoding, units, comboitems) |
| `mapping.py` | Three-tier fallback: exact model+FW → same model/different FW → raw mode (concept §5.5) |
| `mappings/` | Vendored mapping tables from `python-pooldose` (MIT), see `ATTRIBUTION.md` |
| `probe.py` | CLI entry point for P1 |
| `write.py` | Write access (P4): validation + `POST setInstantValues`, no pre-emptive GET |

## HA integration `custom_components/pooldose_live/` (P2)

Read-only: `sensor` + `binary_sensor`, created dynamically from the resolved
channels (not from a fixed table like the core integration — the set of
channels is only known at runtime, depending on a mapping hit or the raw
fallback). `visible: false` channels don't create an entity (concept B3), the
`alarm` flag ends up as an attribute on the sensor (concept B4).

**Setup doesn't wait for live data:** the config flow only checks whether the
WebSocket port responds (10 s timeout) — it does not wait for a complete
`instant_values` cycle. Per concept §8.2/§8.3 the device can legitimately stay
silent for several minutes; a setup step that waited for that would fail for
no reason on a regular basis. Details in concept §5.7.

## Debouncing, availability, diagnostics (P3)

- **Debouncing** (`entity.py`): each entity only writes state on a relevant
  change (resolution-aware for numbers) or every 5 minutes (heartbeat), on
  top of the coarse coordinator-wide equality check from P2. Without this,
  every one of the ~40–70 entities would rewrite state on any single channel
  changing — see concept §5.6/§8.2 for the numbers (422,600 vs. 6,200
  writes/day).
- **Availability**: entities become `unavailable` when the staleness watchdog
  (concept §5.3) trips — except `alarm_system_standby`, which deliberately
  stays visible during staleness because it's itself the most likely
  diagnostic signal for the cause (concept §8.4).
- **`diagnostics.py`**: last raw snapshot (serial number redacted) plus
  session statistics (reconnects, longest gap, mapping status/coverage) —
  exportable directly from HA, the same material that issue #20 at
  lmaertin/python-pooldose grew out of, without CLI fiddling.

## Write access (P4)

`number` + `select` + `switch`, dynamic like the read-only platforms. No
pre-emptive GET before writing (avoids B8) — validation (range/step/options)
runs against the most recently received WS snapshot. Confirmation arrives
with the next tick (~4s) through the normal coordinator path, no optimistic
setting of the displayed value. `switch` only exists for an actual mapping
hit — in raw mode, bare booleans are classified as `binary_sensor`, not
`switch` (unclear whether they're actually writable).

**Not yet tested live against the real device** — deliberately, see concept
§5.9: encoding/validation are fully verified offline, but a first real write
attempt should happen deliberately with a low-risk value, not just
automatically along the way.

## Repair issues, translations (P5)

Visibly surfaces in HA (Settings → System → Repairs) when name resolution
isn't optimal:

- **FW fallback**: exact model match, but no matching firmware mapping file
  found — a different FW revision of the same model was used.
- **Raw mode**: no mapping found for this model at all — all channels run
  under generic `raw_*` names.

Both cases are functional (the concept's core idea against B2, no total
outage like the core integration), but the user should see it and know how to
contribute a mapping (the same path as issue #20 at lmaertin/python-pooldose).
One issue per device, removed again when the config entry is unloaded.
Translated (de/en), like the config flow.

Tests: `tests/test_p2_manual.py` … `test_p5_manual.py` — no regular `pytest`
run for the HA tests, since `pytest-homeassistant-custom-component` fails on
Windows on `homeassistant.runner` (needs `fcntl`, Unix-only). Instead,
standalone scripts using real HA core classes (`python tests/test_p2_manual.py`
etc.). `tests/test_write.py` is pure library logic without an HA dependency
and runs regularly via
`python -m pytest tests/test_write.py -p no:homeassistant` (the
`-p no:homeassistant` only disables the globally registered plugin that's
blocked on Windows). On a real (Linux) HA instance, the manual scripts should
be replaced/supplemented by regular pytest fixtures.

## HACS compliance (P6)

- **Vendoring instead of PyPI**: `custom_components/pooldose_live/vendor/pooldose_live/`
  is a 1:1 copy of `src/pooldose_live/` (minus `probe.py`, pure P1 CLI
  tooling). Fixes the previous "known gap" — a component installed via HACS
  wasn't runnable before P6, because the actual transport/decoder/mapping
  logic had to be `pip install`-ed separately. `manifest.json`'s
  `"requirements": []` is now actually correct, not just a placeholder.
  Details: [`custom_components/pooldose_live/vendor/README.md`](custom_components/pooldose_live/vendor/README.md).
- **Sync check**: `tools/check_vendor_sync.py` (also `tests/test_vendor_sync.py`,
  part of CI) ensures the vendored copy doesn't drift from
  `src/pooldose_live/`. After changing the library:
  `python tools/sync_vendor.py`.
- **CI** (`.github/workflows/validate.yml`): `hacs/action`, `hassfest`, plus
  all of our own tests — runs on Linux runners, where the plugin blocked on
  Windows loads without issue.
- `hacs.json`, `LICENSE` (MIT), version bump to `0.2.0` (package + manifest
  in sync).

## Documents

- [`websocker-spec.md`](websocker-spec.md) — reverse-engineering results from the real device
- [`docs/concept.md`](docs/concept.md) — analysis of existing solutions, findings, architecture concept, measurement results, phase plan
- [`tools/README.md`](tools/README.md) — P0 diagnostic tools (recording, HTTP baseline)

## Credits

The mapping tables (hash key → readable name) come from
[lmaertin/python-pooldose](https://github.com/lmaertin/python-pooldose) (MIT).
