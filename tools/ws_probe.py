#!/usr/bin/env python3
"""Recording and measurement tool for the local PoolDose WebSocket (P0).

Two modes:

    record   connects to ws://<host>:1334/, listens, and writes every frame
             as JSONL. Sends nothing itself (see note below).
    report   evaluates a recording offline.

The transport layer here is deliberately already built the way it's meant
to run in the integration later: one connection, watchdog, exponential
backoff, reassembly per progressInfo. What runs stable here migrates over
to P1.

Note on "sends nothing": no application-level frame is ever generated.
`autoping=True` merely answers the device's PING frames with PONG - that's
a protocol obligation, not a request. `heartbeat=None` ensures we never
trigger our own pings.
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import gzip
import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, TextIO

import aiohttp

DEFAULT_PORT = 1334
DEFAULT_WATCHDOG = 30.0
BACKOFF_START = 2.0
BACKOFF_MAX = 60.0
STATUS_EVERY = 15.0

# Fields that must never end up in a recording. wifi_station can carry the
# WLAN key; a dump should stay shareable.
SENSITIVE_KEYS = {"key", "psk", "password", "passwd", "pwd", "wifi_key", "ap_key", "secret"}

TOPIC_VALUES = "instant_values"


# -------------------------------------------------------------------- Recording


def _redact(obj: Any) -> Any:
    """Recursively replaces values of sensitive keys with '<redacted>'."""
    if isinstance(obj, dict):
        return {
            k: ("<redacted>" if k.lower() in SENSITIVE_KEYS else _redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


class Recorder:
    """Writes events as JSON Lines, optionally gzip-compressed."""

    def __init__(self, path: Path | None, redact_serial: bool) -> None:
        self.path = path
        self.redact_serial = redact_serial
        self._serial: str | None = None
        self._fh: TextIO | None = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.suffix == ".gz":
                self._fh = gzip.open(path, "wt", encoding="utf-8", newline="\n")
            else:
                self._fh = path.open("w", encoding="utf-8", newline="\n")

    def note_serial(self, serial: str) -> None:
        self._serial = serial

    def write(self, ev: str, t0: float, **fields: Any) -> None:
        if self._fh is None:
            return
        rec = {
            "t": round(time.monotonic() - t0, 4),
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "ev": ev,
            **fields,
        }
        line = json.dumps(_redact(rec), ensure_ascii=False, separators=(",", ":"))
        if self.redact_serial and self._serial:
            line = line.replace(self._serial, "REDACTED_SERIAL")
        self._fh.write(line + "\n")
        self._fh.flush()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


class Stats:
    """Running metrics, for the live display and the final report."""

    def __init__(self) -> None:
        self.topics: Counter[str] = Counter()
        self.frames = 0
        self.bytes = 0
        self.cycles_done = 0
        self.cycles_aborted = 0
        self.connects = 0
        self.watchdog_trips = 0
        self.errors: Counter[str] = Counter()
        self.cycle_times: list[float] = []   # monotonic, start of each cycle
        self.frame_times: list[float] = []   # monotonic, every frame
        self.connect_times: list[float] = []
        self.disconnect_times: list[float] = []
        self.last_frame: float | None = None
        self.http_latency: list[float] = []

    @property
    def longest_gap(self) -> float:
        ts = self.frame_times
        return max((b - a for a, b in zip(ts, ts[1:])), default=0.0)


class Reassembler:
    """Assembles chunked instant_values cycles per the spec's rules."""

    def __init__(self) -> None:
        self.buf: dict[str, dict[str, Any]] = {}
        self.open_cycle = False

    def feed(self, data: dict[str, Any]) -> tuple[dict | None, bool]:
        """Returns (snapshot|None, cycle_aborted)."""
        progress = data.get("progressInfo") or {}
        offset = progress.get("offset", 1)
        total = progress.get("total", 1)

        aborted = False
        if offset == 1:
            # A new cycle starts. If half of one was still buffered, it's
            # lost - that's exactly what prevents Frankenstein datasets.
            aborted = self.open_cycle
            self.buf.clear()
            self.open_cycle = True

        for serial, payload in (data.get("devicedata") or {}).items():
            if isinstance(payload, dict):
                self.buf.setdefault(serial, {}).update(payload)

        if offset == total:
            self.open_cycle = False
            return dict(self.buf), aborted
        return None, aborted


async def _http_probe(session: aiohttp.ClientSession, host: str, interval: float,
                      stats: Stats, rec: Recorder, t0: float, stop: asyncio.Event) -> None:
    """Measures the device's HTTP server response time on the side.

    Uses a small static file, not getInstantValues - we want to measure
    latency, not generate load ourselves.
    """
    url = f"http://{host}/js_libs/params.js"
    while not stop.is_set():
        started = time.monotonic()
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                await resp.read()
                dt = time.monotonic() - started
                stats.http_latency.append(dt)
                rec.write("http", t0, ms=round(dt * 1000, 1), status=resp.status)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            rec.write("http_error", t0, error=f"{type(err).__name__}: {err}")
            stats.errors[f"http:{type(err).__name__}"] += 1
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def _receive_or_stop(
    ws: aiohttp.ClientWebSocketResponse, stop: asyncio.Event, timeout: float
) -> tuple[aiohttp.WSMessage | None, bool]:
    """Waits for the next frame or for `stop`, whichever comes first.

    Without this race, `_listen` below would wait exclusively on
    `ws.receive()`. With a ~4.2s base tick, a 30s silence never accumulates,
    so the watchdog never fires - `stop` (e.g. from `--duration`) would only
    be noticed after the next connection drop, practically never.

    Returns `(msg, False)` on a frame, `(None, True)` if `stop` fired first.
    Raises `asyncio.TimeoutError` if neither happened for `timeout` seconds
    (that's the actual watchdog case).
    """
    recv_task = asyncio.ensure_future(ws.receive())
    stop_task = asyncio.ensure_future(stop.wait())
    try:
        done, _ = await asyncio.wait(
            {recv_task, stop_task}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if not done:
            raise asyncio.TimeoutError
        if stop_task in done:
            return None, True
        return recv_task.result(), False
    finally:
        for task in (recv_task, stop_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(recv_task, stop_task, return_exceptions=True)


async def _listen(session: aiohttp.ClientSession, url: str, watchdog: float,
                  stats: Stats, rec: Recorder, t0: float, keep_raw: bool,
                  stop: asyncio.Event) -> str:
    """One connection session. Returns with the reason it ended."""
    reasm = Reassembler()
    async with session.ws_connect(url, heartbeat=None, autoping=True,
                                  timeout=aiohttp.ClientWSTimeout(ws_close=10)) as ws:
        stats.connects += 1
        stats.connect_times.append(time.monotonic() - t0)
        rec.write("connect", t0, url=url)
        print(f"[{_clock()}] connected to {url}")

        while True:
            try:
                msg, stopped = await _receive_or_stop(ws, stop, watchdog)
            except asyncio.TimeoutError:
                stats.watchdog_trips += 1
                rec.write("watchdog", t0, after_s=watchdog)
                return f"watchdog ({watchdog:.0f}s without a frame)"
            if stopped:
                return "stop requested"
            assert msg is not None

            if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSING):
                return f"remote side closed ({msg.type.name})"
            if msg.type is aiohttp.WSMsgType.ERROR:
                return f"WS error: {ws.exception()}"
            if msg.type is not aiohttp.WSMsgType.TEXT:
                stats.errors[f"frametype:{msg.type.name}"] += 1
                continue

            now = time.monotonic()
            stats.frames += 1
            stats.bytes += len(msg.data)
            stats.frame_times.append(now)
            stats.last_frame = now

            try:
                payload = json.loads(msg.data)
            except json.JSONDecodeError as err:
                stats.errors["json_decode"] += 1
                rec.write("bad_json", t0, error=str(err), head=msg.data[:200])
                continue

            topic = payload.get("topic", "<no topic>")
            stats.topics[topic] += 1
            data = payload.get("data") or {}

            if topic != TOPIC_VALUES:
                rec.write("frame", t0, topic=topic,
                          data=data if keep_raw else None, size=len(msg.data))
                continue

            progress = data.get("progressInfo") or {}
            offset, total = progress.get("offset", 1), progress.get("total", 1)
            if offset == 1:
                stats.cycle_times.append(now)

            snapshot, aborted = reasm.feed(data)
            if aborted:
                stats.cycles_aborted += 1

            rec.write("frame", t0, topic=topic, offset=offset, total=total,
                      data=data if keep_raw else None, size=len(msg.data))

            if snapshot is not None:
                stats.cycles_done += 1
                for serial in snapshot:
                    rec.note_serial(serial)
                rec.write("cycle", t0, serials=list(snapshot),
                          channels=sum(len(v) for v in snapshot.values()))


async def run_record(args: argparse.Namespace) -> int:
    url = f"ws://{args.host}:{args.port}/"
    rec = Recorder(Path(args.out) if args.out else None, args.redact_serial)
    stats = Stats()
    t0 = time.monotonic()
    stop = asyncio.Event()
    backoff = BACKOFF_START

    print(f"Recording {url}   Watchdog {args.watchdog:.0f}s   "
          f"Duration {'unlimited' if not args.duration else str(args.duration) + 's'}")
    if rec.path:
        print(f"Output: {rec.path}")
    print("Stop with Ctrl+C\n")

    async with aiohttp.ClientSession() as session:
        tasks = []
        if args.http_probe:
            tasks.append(asyncio.create_task(
                _http_probe(session, args.host, args.http_probe, stats, rec, t0, stop)))
        if args.duration:
            tasks.append(asyncio.create_task(_deadline(args.duration, stop)))
        tasks.append(asyncio.create_task(_status_loop(stats, t0, stop)))

        try:
            while not stop.is_set():
                try:
                    reason = await _listen(session, url, args.watchdog, stats, rec, t0,
                                           keep_raw=args.raw, stop=stop)
                    backoff = BACKOFF_START
                except (aiohttp.ClientError, OSError) as err:
                    reason = f"{type(err).__name__}: {err}"
                    stats.errors[type(err).__name__] += 1

                if stop.is_set():
                    break
                stats.disconnect_times.append(time.monotonic() - t0)
                rec.write("disconnect", t0, reason=reason, retry_in=backoff)
                print(f"[{_clock()}] Connection lost: {reason} - retrying in {backoff:.0f}s")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, BACKOFF_MAX)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            stop.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    rec.close()
    print()
    print_report(build_report(stats_to_dict(stats, time.monotonic() - t0)))
    if rec.path:
        print(f"\nRecording: {rec.path}")
        print(f"Report: python tools/ws_probe.py report {rec.path}")
    return 0


async def _deadline(seconds: float, stop: asyncio.Event) -> None:
    await asyncio.sleep(seconds)
    stop.set()


async def _status_loop(stats: Stats, t0: float, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=STATUS_EVERY)
            return
        except asyncio.TimeoutError:
            pass
        elapsed = time.monotonic() - t0
        since = (time.monotonic() - stats.last_frame) if stats.last_frame else float("nan")
        print(f"[{_clock()}] {elapsed:6.0f}s  Frames {stats.frames:5d}  "
              f"Cycles {stats.cycles_done:4d}  aborted {stats.cycles_aborted:3d}  "
              f"Reconnects {max(0, stats.connects - 1):2d}  last frame {since:4.1f}s ago")


def _clock() -> str:
    return datetime.now().strftime("%H:%M:%S")


# -------------------------------------------------------------------- Reporting


def estimate_tick(intervals: list[float]) -> tuple[float | None, list[float]]:
    """Estimates the base tick as the mode of the (0.1s-rounded) intervals.

    An earlier approach took the median of the *smallest* interval cluster
    as the basis. Over a longer recording with several reconnects, that
    tipped over: a single, very short interval right after a reconnect
    (e.g. a leftover cycle landing mid-tick) was picked up as the "smallest
    cluster", yielding a base tick of 0.67s instead of the actual ~4.1s -
    with knock-on errors in the slot histogram and outage rate. The mode is
    robust against this: it takes the most frequent interval, not the
    smallest, and that's clearly dominant with a fixed device scheduler.
    """
    usable = [x for x in intervals if x > 0]
    if len(usable) < 5:
        return None, []
    buckets = Counter(round(x, 1) for x in usable)
    mode_val, _ = buckets.most_common(1)[0]
    if mode_val <= 0:
        return None, []
    # Fine-tuning with intervals close to a small integer multiple of the
    # mode (covers individual dropped ticks). Reconnect gaps should ideally
    # already be excluded here - callers filter them out beforehand via
    # intra_session_intervals() based on real connection boundaries, rather
    # than having to guess them via multiples.
    refined = [
        x / round(x / mode_val)
        for x in usable
        if 1 <= round(x / mode_val) <= 3
        and abs(x / mode_val - round(x / mode_val)) < 0.15
    ]
    base = statistics.median(refined) if refined else mode_val
    residuals = [abs(x - round(x / base) * base) for x in usable if round(x / base) <= 3]
    return base, residuals


def intra_session_intervals(times: list[float], connect_times: list[float]) -> list[float]:
    """Consecutive intervals in `times`, but only within the same connection
    session.

    An interval spanning a reconnect isn't a dropped device second, but a
    self-inflicted backoff pause - and can last minutes. Knowing this
    exactly (instead of guessing via "does it fit a multiple of the tick?")
    is what `connect_times` enables: every boundary is a real connection
    setup, not an estimated threshold.
    """
    boundaries = sorted(connect_times)
    out = []
    for a, b in zip(times, times[1:]):
        i = bisect.bisect_right(boundaries, a)
        if i < len(boundaries) and boundaries[i] < b:
            continue  # a reconnect lies between a and b -> not comparable
        out.append(round(b - a, 3))
    return out


def pair_outages(disconnect_times: list[float], connect_times: list[float]) -> list[float]:
    """Pairs each `disconnect` with the next `connect` after it and returns
    the respective outage duration. Direct measurement instead of deriving
    it from cycle gaps."""
    connects = sorted(connect_times)
    durations = []
    for d in sorted(disconnect_times):
        i = bisect.bisect_right(connects, d)
        if i < len(connects):
            durations.append(connects[i] - d)
    return durations


def stats_to_dict(stats: Stats, duration: float) -> dict[str, Any]:
    return {
        "duration_s": duration,
        "frames": stats.frames,
        "bytes": stats.bytes,
        "topics": dict(stats.topics),
        "cycles_done": stats.cycles_done,
        "cycles_aborted": stats.cycles_aborted,
        "connects": stats.connects,
        "watchdog_trips": stats.watchdog_trips,
        "errors": dict(stats.errors),
        "cycle_intervals": intra_session_intervals(stats.cycle_times, stats.connect_times),
        "frame_intervals": [round(b - a, 3) for a, b in
                            zip(stats.frame_times, stats.frame_times[1:])],
        "http_latency": stats.http_latency,
        "outage_durations": pair_outages(stats.disconnect_times, stats.connect_times),
    }


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Reads JSON Lines, tolerant of a hard-aborted recording.

    `Recorder.write()` flushes after every line with Z_SYNC_FLUSH (gzip
    default) - lines already written are therefore always complete on
    disk. If only the closing gzip trailer is missing (process killed
    instead of shut down cleanly), evaluation stops here instead of
    discarding the whole recording.
    """
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:  # type: ignore[operator]
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
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def load_recording(path: Path) -> dict[str, Any]:
    """Builds the same structure from a recording that record produces live."""
    topics: Counter[str] = Counter()
    cycle_times: list[float] = []
    frame_times: list[float] = []
    connect_times: list[float] = []
    disconnect_times: list[float] = []
    errors: Counter[str] = Counter()
    http: list[float] = []
    frames = total_bytes = cycles_done = cycles_aborted = connects = watchdogs = 0
    last_t = 0.0
    snapshots: list[tuple[float, dict[str, Any]]] = []
    reasm = Reassembler()

    for rec in _iter_jsonl(path):
        ev, t = rec.get("ev"), float(rec.get("t", 0.0))
        last_t = max(last_t, t)
        if ev == "frame":
            frames += 1
            total_bytes += int(rec.get("size") or 0)
            frame_times.append(t)
            topic = rec.get("topic", "<no topic>")
            topics[topic] += 1
            if topic == TOPIC_VALUES:
                if rec.get("offset", 1) == 1:
                    cycle_times.append(t)
                data = rec.get("data")
                if isinstance(data, dict):
                    snap, aborted = reasm.feed(data)
                    if aborted:
                        cycles_aborted += 1
                    if snap is not None:
                        cycles_done += 1
                        snapshots.append((t, snap))
        elif ev == "connect":
            connects += 1
            connect_times.append(t)
        elif ev == "disconnect":
            disconnect_times.append(t)
        elif ev == "watchdog":
            watchdogs += 1
        elif ev == "http":
            http.append(float(rec.get("ms", 0)) / 1000.0)
        elif ev in ("http_error", "bad_json"):
            errors[ev] += 1

    return {
        "duration_s": last_t,
        "frames": frames,
        "bytes": total_bytes,
        "topics": dict(topics),
        "cycles_done": cycles_done,
        "cycles_aborted": cycles_aborted,
        "connects": connects,
        "watchdog_trips": watchdogs,
        "errors": dict(errors),
        "cycle_intervals": intra_session_intervals(cycle_times, connect_times),
        "frame_intervals": [round(b - a, 3) for a, b in zip(frame_times, frame_times[1:])],
        "http_latency": http,
        "outage_durations": pair_outages(disconnect_times, connect_times),
        "_snapshots": snapshots,
    }


def build_report(raw: dict[str, Any]) -> dict[str, Any]:
    # cycle_intervals already arrives session-safe (see
    # intra_session_intervals) - every interval here lies within an
    # uninterrupted connection, no reconnect/backoff pause is included
    # anymore. The slot histogram therefore needs no further cleanup.
    intervals = raw["cycle_intervals"]
    base, residuals = estimate_tick(intervals)
    report = dict(raw)
    report["tick_base"] = base
    report["tick_residual_max"] = max(residuals) if residuals else None

    if base:
        slots = [max(1, round(iv / base)) for iv in intervals]
        report["slots_expected"] = sum(slots)
        report["slots_missed"] = sum(s - 1 for s in slots)
        report["slot_histogram"] = dict(sorted(Counter(slots).items()))

    outages = raw.get("outage_durations") or []
    report["outage_count"] = len(outages)
    report["outage_seconds"] = sum(outages)
    report["longest_gap"] = max(raw["frame_intervals"], default=0.0)
    return report


def analyse_channels(snapshots: list[tuple[float, dict[str, Any]]]) -> dict[str, Any]:
    """Evaluates channels across the recording: visibility, alarms, change rate."""
    if not snapshots:
        return {}

    changes: Counter[str] = Counter()
    visible_flips: dict[str, int] = defaultdict(int)
    last_value: dict[str, Any] = {}
    last_visible: dict[str, Any] = {}
    seen: set[str] = set()
    visible_now: dict[str, bool] = {}
    alarm_now: dict[str, bool] = {}
    non_dict: set[str] = set()

    for _, snap in snapshots:
        for serial, channels in snap.items():
            for key, val in channels.items():
                if key in ("deviceInfo", "collapsed_bar"):
                    continue
                seen.add(key)
                if not isinstance(val, dict):
                    non_dict.add(key)
                    current, visible, alarm = val, True, None
                else:
                    current = val.get("current")
                    visible = val.get("visible", True)
                    alarm = val.get("alarm")
                visible_now[key] = bool(visible)
                if alarm is not None:
                    alarm_now[key] = bool(alarm)
                if key in last_value and last_value[key] != current:
                    changes[key] += 1
                if key in last_visible and last_visible[key] != visible:
                    visible_flips[key] += 1
                last_value[key] = current
                last_visible[key] = visible

    return {
        "channels": len(seen),
        "non_dict": sorted(non_dict),
        "visible_true": sum(1 for v in visible_now.values() if v),
        "visible_false": sorted(k for k, v in visible_now.items() if not v),
        "alarm_true": sorted(k for k, v in alarm_now.items() if v),
        "visible_flips": dict(visible_flips),
        "changes": changes,
        "snapshots": len(snapshots),
    }


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))
    return s[idx]


def print_report(rep: dict[str, Any], channels: dict[str, Any] | None = None) -> None:
    dur = rep["duration_s"]
    print("=" * 66)
    print("MEASUREMENT REPORT")
    print("=" * 66)
    print(f"Duration              {dur:.0f}s ({dur / 60:.1f} min)")
    print(f"Frames                {rep['frames']}  ({rep['bytes'] / 1024:.0f} KiB)")
    if dur > 0 and rep["bytes"]:
        print(f"Extrapolated          {rep['bytes'] / dur * 86400 / 1024 / 1024:.0f} MB/day")
    print(f"Connections opened    {rep['connects']}  "
          f"(of which reconnects {max(0, rep['connects'] - 1)})")
    print(f"Watchdog triggered    {rep['watchdog_trips']}")
    print()

    print("Topics:")
    for topic, count in sorted(rep["topics"].items(), key=lambda kv: -kv[1]):
        per = f"{dur / count:.1f}s" if count else "-"
        print(f"  {topic:20} {count:6d}   every {per}")
    print()

    print(f"Cycles complete       {rep['cycles_done']}")
    print(f"Cycles aborted        {rep['cycles_aborted']}")
    base = rep.get("tick_base")
    if base:
        print(f"Base tick (estimated) {base:.2f}s   "
              f"max. deviation {rep['tick_residual_max']:.2f}s")
        exp, missed = rep["slots_expected"], rep["slots_missed"]
        rate = missed / exp * 100 if exp else 0
        print(f"Expected slots        {exp}   missed {missed} ({rate:.1f}%)"
              "   [only while connected]")
        print(f"Slot histogram        {rep['slot_histogram']}"
              "   (1 = no drop)")
    if rep.get("outage_count"):
        print(f"Reconnect outage time {rep['outage_seconds']:.0f}s "
              f"in {rep['outage_count']} gap(s)   "
              "[separate from the tick outage rate above, measured via connect/disconnect]")
    print(f"Longest frame gap     {rep['longest_gap']:.1f}s")
    print()

    if rep.get("http_latency"):
        lat = rep["http_latency"]
        print(f"HTTP latency (n={len(lat)})  median {_pct(lat, 50) * 1000:.0f}ms   "
              f"p95 {_pct(lat, 95) * 1000:.0f}ms   max {max(lat) * 1000:.0f}ms")
        print()

    if rep.get("errors"):
        print("Errors:")
        for key, count in sorted(rep["errors"].items(), key=lambda kv: -kv[1]):
            print(f"  {key:30} {count}")
        print()

    if channels:
        print("-" * 66)
        print(f"Channels total        {channels['channels']}")
        print(f"  visible: true       {channels['visible_true']}")
        print(f"  visible: false      {len(channels['visible_false'])}")
        for key in channels["visible_false"]:
            print(f"      {key}")
        if channels["non_dict"]:
            print(f"  no value object     {len(channels['non_dict'])}")
            for key in channels["non_dict"]:
                print(f"      {key}")
        if channels["alarm_true"]:
            print(f"  alarm: true         {len(channels['alarm_true'])}")
            for key in channels["alarm_true"]:
                print(f"      {key}")
        if channels["visible_flips"]:
            print(f"  visible changed     {channels['visible_flips']}")
        else:
            print("  visible changed     none")
        print()

        snaps = channels["snapshots"]
        changed = channels["changes"]
        print(f"Value changes over {snaps} cycles "
              f"(decides the debouncing, concept 5.6):")
        if not changed:
            print("  no channel changed")
        for key, count in changed.most_common(15):
            print(f"  {key:44} {count:5d}  ({count / snaps * 100:5.1f}% of cycles)")
        quiet = channels["channels"] - len(changed)
        print(f"  ... {quiet} channels unchanged")
        if dur > 0:
            naive = channels["channels"] * snaps
            real = sum(changed.values())
            print()
            print(f"  Without debouncing  {naive / dur * 86400:,.0f} state writes/day")
            print(f"  Only on change      {real / dur * 86400:,.0f} state writes/day"
                  f"   ({real / naive * 100:.1f}%)" if naive else "")
    print("=" * 66)


def run_report(args: argparse.Namespace) -> int:
    path = Path(args.recording)
    if not path.exists():
        print(f"File not found: {path}", file=sys.stderr)
        return 1
    raw = load_recording(path)
    snapshots = raw.pop("_snapshots", [])
    channels = analyse_channels(snapshots) if snapshots else None
    if snapshots and not channels:
        channels = None
    print_report(build_report(raw), channels)
    if not snapshots:
        print("\nNote: recording without raw data (--no-raw) - "
              "channel evaluation not possible.")
    return 0


# ------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    # Without this, stdout stays block-buffered as soon as it's redirected
    # to a file or captured by a wrapper - the status lines from
    # _status_loop would then only arrive at process exit, not continuously.
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(
        prog="ws_probe",
        description="Recording and evaluation of the PoolDose WebSocket.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    rec = sub.add_parser("record", help="Connect, listen, record")
    rec.add_argument("--host", required=True, help="IP or hostname of the device")
    rec.add_argument("--port", type=int, default=DEFAULT_PORT)
    rec.add_argument("--duration", type=float, default=0,
                     help="Seconds; 0 = until Ctrl+C (default)")
    rec.add_argument("--out", default=None,
                     help="Target file (.jsonl or .jsonl.gz). Without it, statistics only.")
    rec.add_argument("--watchdog", type=float, default=DEFAULT_WATCHDOG,
                     help=f"Seconds without a frame until reconnect (default {DEFAULT_WATCHDOG:.0f})")
    rec.add_argument("--http-probe", type=float, default=0, metavar="SEC",
                     help="Measure HTTP latency every SEC seconds; 0 = off")
    rec.add_argument("--no-raw", dest="raw", action="store_false",
                     help="Record metadata only, no payload (small files)")
    rec.add_argument("--keep-serial", dest="redact_serial", action="store_false",
                     help="Keep the serial number in the recording (default: replace)")
    rec.set_defaults(raw=True, redact_serial=True)

    rep = sub.add_parser("report", help="Evaluate a recording")
    rep.add_argument("recording", help="Path to the .jsonl or .jsonl.gz")

    args = parser.parse_args(argv)
    if args.cmd == "record":
        try:
            return asyncio.run(run_record(args))
        except KeyboardInterrupt:
            return 130
    return run_report(args)


if __name__ == "__main__":
    sys.exit(main())
