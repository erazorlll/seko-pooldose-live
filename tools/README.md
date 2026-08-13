# tools/ws_probe.py — P0 recording and measurement tool

Pure diagnostic tool for phase P0 (concept §7/§8). Not part of the later
integration, but the transport layer (one connection, reassembly, watchdog,
backoff) is deliberately already built the way it's meant to be reused in
P1.

Requirement: `aiohttp` (already present in most HA environments anyway).

```bash
pip install aiohttp
```

## Recording

```bash
python tools/ws_probe.py record --host 192.168.0.74 --duration 1800 \
    --out recordings/session1.jsonl.gz --http-probe 60
```

- `--duration` in seconds; without it, runs until Ctrl+C.
- `--out` writes JSON Lines, a `.gz` extension compresses automatically.
  Without `--out`, you only get the live statistics, no file.
- `--http-probe SEC` measures the device's HTTP response time on the side
  (`GET /js_libs/params.js`, no `getInstantValues` load).
- Serial numbers are redacted in the recording by default (`--keep-serial`
  turns that off), WiFi keys are always redacted. That makes a recording
  shareable.
- `recordings/` and `*.jsonl(.gz)` are excluded from the repo via
  `.gitignore` — that's raw data from your device, not source code.

At the end of a recording, the same report as `report` appears
automatically.

## Analyzing

```bash
python tools/ws_probe.py report recordings/session1.jsonl.gz
```

Delivers: frames/bytes/topics, extrapolated daily traffic, cycle
completeness, an estimated base tick with deviation, longest frame gap,
HTTP latency distribution, and — if recorded with raw data (the default,
`--no-raw` turns it off) — per channel: `visible`/`alarm` state, whether
`visible` ever changed, and how often each channel changed (direct basis
for the debouncing thresholds from concept 5.6).

## Relation to the P0 measurement questions (concept §8)

| Question | How it's answered here |
|---|---|
| Does the device send without a listener? | Not directly testable (no mirror port). Approximated via `http_baseline.py`, see below |
| Tick dropout rate with 0/1/2 clients | run `record` once in parallel with `--http-probe`, compare the `slot histogram` in the report |
| HTTP response time with/without a WS listener | `--http-probe` with and without a second `record` process running in parallel |
| Reconnect behavior (offset:1 immediately?) | set `--watchdog` artificially low, observe `cycles aborted` |
| Does `visible` change at runtime? | a long recording (≥24h), `visible changed` in the report |
| Do values arrive outside the tick? | change something on the device during the recording, check `frame_intervals` |

Results from the first 11h run: [`docs/concept.md` §8.2](../docs/concept.md#82-results-11h-recording-2026-08-12-1-client).

## tools/http_baseline.py — comparison measurement without a WS client (concept §10)

Answers the most important open question after the 11h WS measurement: are
the multi-minute `instant_values` dropouts found there caused by our own WS
listening, or are they device-inherent? Polls exclusively over the HTTP API
(`POST /api/v1/DWI/getInstantValues`), port 1334 stays untouched.

```bash
python tools/http_baseline.py record --host 192.168.0.74 --interval 20 \
    --duration 14400 --out recordings/http_baseline.jsonl.gz
python tools/http_baseline.py report recordings/http_baseline.jsonl.gz
```

Detects "frozen runs": consecutive polls with an identical hash across all
visible `current` values. At short intervals (< 2× the device tick, ~4.2s),
short runs are expected from polling/tick aliasing and aren't a finding —
only runs on the order of minutes are meaningful compared to the WS
dropouts (up to 393s, see concept §8.2).
