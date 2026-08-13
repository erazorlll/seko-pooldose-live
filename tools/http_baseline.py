#!/usr/bin/env python3
"""HTTP-only baseline (P0, concept §10): comparison measurement without a WS client.

The most important open question after the 11h WS measurement (see
docs/concept.md §8.2): the repeated multi-minute instant_values dropouts
found there, with an intact connection - are they caused by our own
WebSocket listener, or do they happen even with no WS client at all?

A true "nobody is connected" experiment isn't structurally possible over
WebSocket: without an open connection there's no frame to observe. This
baseline instead approaches the question like this:

    It polls exclusively via the existing HTTP API
    (POST /api/v1/DWI/getInstantValues, as today's official integration
    does), at a moderate interval, WITHOUT EVER touching port 1334.
    Afterward, it's checked whether the values freeze exactly the same
    way over multiple minutes as in the WS stream.

    - If the HTTP baseline NEVER freezes: suggests our WS listening
      itself is the stressor.
    - If it freezes JUST THE SAME: suggests the dropouts are inherent
      to the device, independent of the client.

No proof in either direction (polling is also load, just less frequent and
of a different kind than an open WS socket) - but the best practically
possible approximation without access to a mirror port on the switch/router.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

import aiohttp

DEFAULT_INTERVAL = 20.0
SENSITIVE_KEYS = {"key", "psk", "password", "passwd", "pwd", "wifi_key", "ap_key", "secret"}

# Known channels of this model (taken from the mapping) for a readable
# sample in the report. The freeze test itself doesn't need this - it runs
# over the hash of all visible values.
SAMPLE_PREFIX = "PDPR1H04AW100_FW539292_"
SAMPLE_CHANNELS = {
    "ph": "w_1ekeigkin",
    "orp": "w_1eklenb23",
    "temperature": "w_1eommf39k",
}


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("<redacted>" if k.lower() in SENSITIVE_KEYS else _redact(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def value_signature(raw: dict[str, Any]) -> tuple[str, dict[str, Any], int]:
    """Stable hash over all visible `current` values + channel count.

    `visible: false` channels are deliberately excluded (see concept B3) -
    a disconnected channel that never reliably changes anyway shouldn't
    dilute freeze detection.
    """
    flat: dict[str, Any] = {}
    for serial, payload in (raw.get("devicedata") or {}).items():
        if not isinstance(payload, dict):
            continue
        for key, val in payload.items():
            if key in ("deviceInfo", "collapsed_bar"):
                continue
            if isinstance(val, dict):
                if val.get("visible", True):
                    flat[key] = val.get("current")
            else:
                flat[key] = val
    blob = json.dumps(flat, sort_keys=True, default=str, ensure_ascii=False)
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    return digest, flat, len(flat)


def _sample(flat: dict[str, Any]) -> dict[str, Any]:
    return {name: flat.get(SAMPLE_PREFIX + key) for name, key in SAMPLE_CHANNELS.items()}


class Recorder:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._fh: TextIO | None = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = (gzip.open(path, "wt", encoding="utf-8", newline="\n")
                       if path.suffix == ".gz" else path.open("w", encoding="utf-8", newline="\n"))

    def write(self, t0: float, **fields: Any) -> None:
        if self._fh is None:
            return
        rec = {
            "t": round(time.monotonic() - t0, 3),
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            **fields,
        }
        self._fh.write(json.dumps(_redact(rec), ensure_ascii=False, separators=(",", ":")) + "\n")
        self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def _clock() -> str:
    return datetime.now().strftime("%H:%M:%S")


async def poll_once(session: aiohttp.ClientSession, url: str) -> tuple[int | None, dict | None, float, str | None]:
    started = time.monotonic()
    try:
        async with session.post(url, headers={"Content-Type": "application/json"},
                                timeout=aiohttp.ClientTimeout(total=15)) as resp:
            data = await resp.json(content_type=None)
            ms = (time.monotonic() - started) * 1000
            return resp.status, data, ms, None
    except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as err:
        ms = (time.monotonic() - started) * 1000
        return None, None, ms, f"{type(err).__name__}: {err}"


async def run_record(args: argparse.Namespace) -> int:
    scheme = "https" if args.ssl else "http"
    url = f"{scheme}://{args.host}:{args.port}/api/v1/DWI/getInstantValues"
    rec = Recorder(Path(args.out) if args.out else None)
    t0 = time.monotonic()
    deadline = t0 + args.duration if args.duration else None

    print(f"HTTP baseline {url}   Interval {args.interval:.0f}s   "
          f"Duration {'unlimited' if not args.duration else str(args.duration) + 's'}")
    print("Port 1334 (WebSocket) is not touched during this run.")
    if rec.path:
        print(f"Output: {rec.path}")
    print("Stop with Ctrl+C\n")

    polls = errors = 0
    last_hash: str | None = None
    streak_start = 0.0
    streak_len = 0
    frozen_events: list[tuple[float, float, int]] = []  # (start_t, duration, streak_len)
    latencies: list[float] = []

    try:
        async with aiohttp.ClientSession() as session:
            while True:
                now = time.monotonic()
                if deadline and now >= deadline:
                    break
                status, data, ms, err = await poll_once(session, url)
                t_rel = now - t0
                polls += 1

                if err is not None or data is None:
                    errors += 1
                    rec.write(t0, ev="error", error=err, status=status, ms=round(ms, 1))
                    print(f"[{_clock()}] {t_rel:8.0f}s  Error: {err}")
                    last_hash = None  # series interrupted, not counted as a freeze
                    streak_len = 0
                else:
                    latencies.append(ms)
                    digest, flat, n = value_signature(data)
                    rec.write(t0, ev="poll", status=status, ms=round(ms, 1),
                             hash=digest, channels=n, sample=_sample(flat))

                    if digest == last_hash:
                        if streak_len == 0:
                            streak_start = t_rel - args.interval
                        streak_len += 1
                    else:
                        if streak_len >= 2:
                            frozen_events.append((streak_start, t_rel - streak_start, streak_len))
                        streak_len = 0
                        last_hash = digest

                    if polls % 10 == 0 or streak_len >= 3:
                        note = f"  frozen for {streak_len} polls" if streak_len >= 3 else ""
                        print(f"[{_clock()}] {t_rel:8.0f}s  Poll #{polls:4d}  "
                              f"{ms:6.0f}ms  hash={digest}{note}")

                await asyncio.sleep(max(0.0, args.interval - (time.monotonic() - now)))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        if streak_len >= 2:
            frozen_events.append((streak_start, (time.monotonic() - t0) - streak_start, streak_len))
        rec.close()

    print()
    print_report(polls, errors, latencies, frozen_events, args.interval, time.monotonic() - t0)
    if rec.path:
        print(f"\nRecording: {rec.path}")
        print(f"Report: python tools/http_baseline.py report {rec.path}")
    return 0


def print_report(polls: int, errors: int, latencies: list[float],
                 frozen_events: list[tuple[float, float, int]], interval: float, duration: float) -> None:
    print("=" * 66)
    print("HTTP BASELINE - MEASUREMENT REPORT")
    print("=" * 66)
    print(f"Duration              {duration:.0f}s ({duration/60:.1f} min)")
    print(f"Polls                 {polls}   Errors {errors}")
    if latencies:
        s = sorted(latencies)
        median = s[len(s)//2]
        p95 = s[int(len(s)*0.95)] if len(s) > 1 else s[0]
        print(f"Latency               median {median:.0f}ms   p95 {p95:.0f}ms   max {max(s):.0f}ms")
    print()
    if not frozen_events:
        print("No frozen series found (>=2 consecutive polls "
              "with an identical value hash).")
        print("-> Suggests that the dropout from the WS measurement does NOT")
        print("  also occur without a WS connection - points to a link")
        print("  with WS listening itself.")
    else:
        total_frozen = sum(d for _, d, _ in frozen_events)
        print(f"Frozen series         {len(frozen_events)}   "
              f"Total {total_frozen:.0f}s ({total_frozen/60:.1f} min, "
              f"{total_frozen/duration*100:.1f}% of runtime)" if duration else "")
        print("Longest series:")
        for start, dur, n in sorted(frozen_events, key=lambda x: -x[1])[:10]:
            print(f"  from t={start:8.0f}s   duration {dur:6.0f}s   ({n} identical polls)")
        print()
        print("-> If these durations are in the same order of magnitude as the")
        print("  WS dropouts (concept §8.2: up to 393s, ~30% of the time): points")
        print("  to an effect inherent to the device, independent of WS listening.")
    print("=" * 66)


def run_report(args: argparse.Namespace) -> int:
    path = Path(args.recording)
    if not path.exists():
        print(f"File not found: {path}", file=sys.stderr)
        return 1

    opener = gzip.open if path.suffix == ".gz" else open
    polls = errors = 0
    latencies: list[float] = []
    last_hash: str | None = None
    streak_start = 0.0
    streak_len = 0
    frozen_events: list[tuple[float, float, int]] = []
    last_t = 0.0
    interval = DEFAULT_INTERVAL

    with opener(path, "rt", encoding="utf-8") as fh:  # type: ignore[operator]
        prev_t: float | None = None
        while True:
            try:
                line = fh.readline()
            except (EOFError, OSError) as err:
                print(f"Note: recording ends abruptly ({type(err).__name__}: {err}) "
                      "- evaluating the readable part.", file=sys.stderr)
                break
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            t = float(rec.get("t", 0.0))
            last_t = max(last_t, t)
            if prev_t is not None and t - prev_t > 0:
                interval = t - prev_t  # most recent observed interval as an estimate
            prev_t = t

            if rec.get("ev") == "error":
                errors += 1
                last_hash = None
                streak_len = 0
                continue

            polls += 1
            latencies.append(float(rec.get("ms", 0)))
            digest = rec.get("hash")
            if digest == last_hash:
                if streak_len == 0:
                    streak_start = t - interval
                streak_len += 1
            else:
                if streak_len >= 2:
                    frozen_events.append((streak_start, t - streak_start, streak_len))
                streak_len = 0
                last_hash = digest

    if streak_len >= 2:
        frozen_events.append((streak_start, last_t - streak_start, streak_len))

    print_report(polls, errors, latencies, frozen_events, interval, last_t)
    return 0


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(
        prog="http_baseline",
        description="HTTP-only comparison measurement without a WebSocket client (P0, concept §10).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    rec = sub.add_parser("record", help="Poll and record")
    rec.add_argument("--host", required=True)
    rec.add_argument("--port", type=int, default=80)
    rec.add_argument("--ssl", action="store_true")
    rec.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    rec.add_argument("--duration", type=float, default=0, help="Seconds; 0 = until Ctrl+C")
    rec.add_argument("--out", default=None)

    rep = sub.add_parser("report", help="Evaluate a recording")
    rep.add_argument("recording")

    args = parser.parse_args(argv)
    if args.cmd == "record":
        try:
            return asyncio.run(run_record(args))
        except KeyboardInterrupt:
            return 130
    return run_report(args)


if __name__ == "__main__":
    sys.exit(main())
