#!/usr/bin/env python3
"""Mitschnitt- und Messwerkzeug für den lokalen PoolDose-WebSocket (P0).

Zwei Betriebsarten:

    record   verbindet sich auf ws://<host>:1334/, hört zu und schreibt jeden
             Frame als JSONL. Sendet selbst nichts (siehe Anmerkung unten).
    report   wertet einen Mitschnitt offline aus.

Die Transportschicht hier ist bewusst schon so gebaut, wie sie später in der
Integration laufen soll: eine Verbindung, Watchdog, exponentielles Backoff,
Reassembly nach progressInfo. Was hier stabil läuft, wandert nach P1 hinüber.

Anmerkung zum "sendet nichts": auf Anwendungsebene wird kein einziger Frame
erzeugt. `autoping=True` beantwortet lediglich PING-Frames des Geräts mit PONG
— das ist Protokollpflicht, keine Anfrage. `heartbeat=None` stellt sicher, dass
wir keine eigenen Pings anstoßen.
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

# Felder, die niemals in einen Mitschnitt gehören. wifi_station kann den
# WLAN-Schlüssel führen; ein Dump soll teilbar bleiben.
SENSITIVE_KEYS = {"key", "psk", "password", "passwd", "pwd", "wifi_key", "ap_key", "secret"}

TOPIC_VALUES = "instant_values"


# ---------------------------------------------------------------- Aufzeichnung


def _redact(obj: Any) -> Any:
    """Ersetzt Werte sensibler Schlüssel rekursiv durch '<redacted>'."""
    if isinstance(obj, dict):
        return {
            k: ("<redacted>" if k.lower() in SENSITIVE_KEYS else _redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


class Recorder:
    """Schreibt Ereignisse als JSON Lines, optional gzip-komprimiert."""

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
    """Laufende Kennzahlen, für die Live-Anzeige und den Abschlussbericht."""

    def __init__(self) -> None:
        self.topics: Counter[str] = Counter()
        self.frames = 0
        self.bytes = 0
        self.cycles_done = 0
        self.cycles_aborted = 0
        self.connects = 0
        self.watchdog_trips = 0
        self.errors: Counter[str] = Counter()
        self.cycle_times: list[float] = []   # monoton, Start jedes Zyklus
        self.frame_times: list[float] = []   # monoton, jeder Frame
        self.connect_times: list[float] = []
        self.disconnect_times: list[float] = []
        self.last_frame: float | None = None
        self.http_latency: list[float] = []

    @property
    def longest_gap(self) -> float:
        ts = self.frame_times
        return max((b - a for a, b in zip(ts, ts[1:])), default=0.0)


class Reassembler:
    """Setzt gechunkte instant_values-Zyklen nach den Regeln der Spec zusammen."""

    def __init__(self) -> None:
        self.buf: dict[str, dict[str, Any]] = {}
        self.open_cycle = False

    def feed(self, data: dict[str, Any]) -> tuple[dict | None, bool]:
        """Gibt (Snapshot|None, abgebrochener_Zyklus) zurück."""
        progress = data.get("progressInfo") or {}
        offset = progress.get("offset", 1)
        total = progress.get("total", 1)

        aborted = False
        if offset == 1:
            # Ein neuer Zyklus beginnt. Lag noch ein halber im Puffer, ist der
            # verloren - genau das verhindert Frankenstein-Datensätze.
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
    """Misst nebenher die Antwortzeit des HTTP-Servers am Gerät.

    Nutzt eine kleine statische Datei, kein getInstantValues - wir wollen die
    Latenz messen, nicht selbst Last erzeugen.
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
    """Wartet auf den nächsten Frame oder auf `stop`, je nachdem was zuerst kommt.

    Ohne dieses Rennen würde `_listen` unten ausschließlich auf `ws.receive()`
    warten. Bei einem ~4,2s-Grundtakt kommt dabei nie eine 30s-Stille zusammen,
    der Watchdog löst also nie aus — `stop` (z. B. von `--duration`) würde erst
    nach dem nächsten Verbindungsabbruch bemerkt, praktisch nie.

    Rückgabe `(msg, False)` bei einem Frame, `(None, True)` wenn `stop` zuerst
    kam. Löst `asyncio.TimeoutError` aus, wenn `timeout` Sekunden lang weder
    noch eintraf (das ist der eigentliche Watchdog-Fall).
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
    """Eine Verbindungssitzung. Kehrt mit dem Abbruchgrund zurück."""
    reasm = Reassembler()
    async with session.ws_connect(url, heartbeat=None, autoping=True,
                                  timeout=aiohttp.ClientWSTimeout(ws_close=10)) as ws:
        stats.connects += 1
        stats.connect_times.append(time.monotonic() - t0)
        rec.write("connect", t0, url=url)
        print(f"[{_clock()}] verbunden mit {url}")

        while True:
            try:
                msg, stopped = await _receive_or_stop(ws, stop, watchdog)
            except asyncio.TimeoutError:
                stats.watchdog_trips += 1
                rec.write("watchdog", t0, after_s=watchdog)
                return f"watchdog ({watchdog:.0f}s ohne Frame)"
            if stopped:
                return "stop angefordert"
            assert msg is not None

            if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSING):
                return f"Gegenstelle hat geschlossen ({msg.type.name})"
            if msg.type is aiohttp.WSMsgType.ERROR:
                return f"WS-Fehler: {ws.exception()}"
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

            topic = payload.get("topic", "<kein topic>")
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

    print(f"Mitschnitt {url}   Watchdog {args.watchdog:.0f}s   "
          f"Dauer {'unbegrenzt' if not args.duration else str(args.duration) + 's'}")
    if rec.path:
        print(f"Ausgabe: {rec.path}")
    print("Abbruch mit Strg+C\n")

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
                print(f"[{_clock()}] Verbindung weg: {reason} — neuer Versuch in {backoff:.0f}s")
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
        print(f"\nMitschnitt: {rec.path}")
        print(f"Auswertung: python tools/ws_probe.py report {rec.path}")
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
              f"Zyklen {stats.cycles_done:4d}  abgebrochen {stats.cycles_aborted:3d}  "
              f"Reconnects {max(0, stats.connects - 1):2d}  letzter Frame vor {since:4.1f}s")


def _clock() -> str:
    return datetime.now().strftime("%H:%M:%S")


# ----------------------------------------------------------------- Auswertung


def estimate_tick(intervals: list[float]) -> tuple[float | None, list[float]]:
    """Schätzt den Grundtakt als Modalwert der (auf 0,1s gerundeten) Abstände.

    Ein früherer Ansatz nahm den Median des *kleinsten* Abstands-Clusters als
    Basis. Über einen längeren Mitschnitt mit mehreren Reconnects kippte das:
    ein einzelner, sehr kurzer Abstand direkt nach einem Wiederverbinden (z. B.
    ein Rest-Zyklus, der mitten im Takt aufschlägt) wurde als "kleinstes
    Cluster" genommen, und daraus ergab sich ein Grundtakt von 0,67s statt der
    tatsächlichen ~4,1s — mit Folgefehlern in Slot-Histogramm und Ausfallrate.
    Der Modalwert ist robust dagegen: er nimmt den häufigsten Abstand, nicht
    den kleinsten, und der ist bei einem festen Geräte-Scheduler klar dominant.
    """
    usable = [x for x in intervals if x > 0]
    if len(usable) < 5:
        return None, []
    buckets = Counter(round(x, 1) for x in usable)
    mode_val, _ = buckets.most_common(1)[0]
    if mode_val <= 0:
        return None, []
    # Feinjustierung mit Abständen nahe eines kleinen ganzzahligen Vielfachen
    # des Modalwerts (deckt einzelne ausgefallene Ticks ab). Reconnect-Lücken
    # sind hier idealerweise schon nicht mehr enthalten - die Aufrufer filtern
    # sie vorher über intra_session_intervals() anhand echter Verbindungs-
    # grenzen heraus, statt sie über Vielfache erraten zu müssen.
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
    """Aufeinanderfolgende Abstände in `times`, aber nur innerhalb derselben
    Verbindungssitzung.

    Ein Abstand, der einen Reconnect überspannt, ist keine ausgefallene
    Gerätesekunde, sondern eine selbstverursachte Backoff-Pause - und kann
    Minuten dauern. Das exakt zu wissen (statt über "passt zu einem Vielfachen
    des Takts?" zu raten) macht `connect_times` möglich: jede Grenze ist ein
    echter Verbindungsaufbau, kein geschätzter Schwellwert.
    """
    boundaries = sorted(connect_times)
    out = []
    for a, b in zip(times, times[1:]):
        i = bisect.bisect_right(boundaries, a)
        if i < len(boundaries) and boundaries[i] < b:
            continue  # ein Reconnect liegt zwischen a und b -> nicht vergleichbar
        out.append(round(b - a, 3))
    return out


def pair_outages(disconnect_times: list[float], connect_times: list[float]) -> list[float]:
    """Ordnet jedem `disconnect` das nächste `connect` danach zu und gibt die
    jeweilige Ausfalldauer zurück. Direkte Messung statt Ableitung aus
    Zyklus-Lücken."""
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
    """Liest JSON Lines, tolerant gegenüber einem hart abgebrochenen Mitschnitt.

    `Recorder.write()` flusht nach jeder Zeile mit Z_SYNC_FLUSH (gzip-Default) -
    bereits geschriebene Zeilen sind also immer vollständig auf der Platte. Fehlt
    nur der abschließende gzip-Trailer (Prozess getötet statt sauber beendet),
    bricht die Auswertung hier ab statt den gesamten Mitschnitt zu verwerfen.
    """
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:  # type: ignore[operator]
        while True:
            try:
                line = fh.readline()
            except (EOFError, OSError) as err:
                print(f"Hinweis: Mitschnitt endet abrupt ({type(err).__name__}: {err}) "
                      "— werte den lesbaren Teil aus.", file=sys.stderr)
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
    """Baut aus einem Mitschnitt dieselbe Struktur, die record live erzeugt."""
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
            topic = rec.get("topic", "<kein topic>")
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
    # cycle_intervals kommt bereits sitzungssicher an (siehe
    # intra_session_intervals) - jeder Abstand hier liegt innerhalb einer
    # ununterbrochenen Verbindung, keine Reconnect-/Backoff-Pause ist mehr
    # dabei. Das Slot-Histogramm braucht also keine nachträgliche Bereinigung.
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
    """Wertet Kanäle über den Mitschnitt aus: Sichtbarkeit, Alarme, Änderungsrate."""
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
    print("MESSBERICHT")
    print("=" * 66)
    print(f"Dauer                 {dur:.0f}s ({dur / 60:.1f} min)")
    print(f"Frames                {rep['frames']}  ({rep['bytes'] / 1024:.0f} KiB)")
    if dur > 0 and rep["bytes"]:
        print(f"Hochgerechnet         {rep['bytes'] / dur * 86400 / 1024 / 1024:.0f} MB/Tag")
    print(f"Verbindungsaufbauten  {rep['connects']}  "
          f"(davon Reconnects {max(0, rep['connects'] - 1)})")
    print(f"Watchdog ausgelöst    {rep['watchdog_trips']}")
    print()

    print("Topics:")
    for topic, count in sorted(rep["topics"].items(), key=lambda kv: -kv[1]):
        per = f"{dur / count:.1f}s" if count else "-"
        print(f"  {topic:20} {count:6d}   alle {per}")
    print()

    print(f"Zyklen vollständig    {rep['cycles_done']}")
    print(f"Zyklen abgebrochen    {rep['cycles_aborted']}")
    base = rep.get("tick_base")
    if base:
        print(f"Grundtakt (geschätzt) {base:.2f}s   "
              f"max. Abweichung {rep['tick_residual_max']:.2f}s")
        exp, missed = rep["slots_expected"], rep["slots_missed"]
        rate = missed / exp * 100 if exp else 0
        print(f"Erwartete Slots       {exp}   ausgefallen {missed} ({rate:.1f}%)"
              "   [nur waehrend bestehender Verbindung]")
        print(f"Slot-Histogramm       {rep['slot_histogram']}"
              "   (1 = kein Ausfall)")
    if rep.get("outage_count"):
        print(f"Reconnect-Ausfallzeit {rep['outage_seconds']:.0f}s "
              f"in {rep['outage_count']} Lücke(n)   "
              "[separat von der Tick-Ausfallrate oben, per connect/disconnect gemessen]")
    print(f"Längste Frame-Lücke   {rep['longest_gap']:.1f}s")
    print()

    if rep.get("http_latency"):
        lat = rep["http_latency"]
        print(f"HTTP-Latenz (n={len(lat)})  median {_pct(lat, 50) * 1000:.0f}ms   "
              f"p95 {_pct(lat, 95) * 1000:.0f}ms   max {max(lat) * 1000:.0f}ms")
        print()

    if rep.get("errors"):
        print("Fehler:")
        for key, count in sorted(rep["errors"].items(), key=lambda kv: -kv[1]):
            print(f"  {key:30} {count}")
        print()

    if channels:
        print("-" * 66)
        print(f"Kanäle gesamt         {channels['channels']}")
        print(f"  visible: true       {channels['visible_true']}")
        print(f"  visible: false      {len(channels['visible_false'])}")
        for key in channels["visible_false"]:
            print(f"      {key}")
        if channels["non_dict"]:
            print(f"  kein Wertobjekt     {len(channels['non_dict'])}")
            for key in channels["non_dict"]:
                print(f"      {key}")
        if channels["alarm_true"]:
            print(f"  alarm: true         {len(channels['alarm_true'])}")
            for key in channels["alarm_true"]:
                print(f"      {key}")
        if channels["visible_flips"]:
            print(f"  visible gewechselt  {channels['visible_flips']}")
        else:
            print("  visible gewechselt  keine")
        print()

        snaps = channels["snapshots"]
        changed = channels["changes"]
        print(f"Wertänderungen über {snaps} Zyklen "
              f"(entscheidet die Entprellung, Konzept 5.6):")
        if not changed:
            print("  kein Kanal hat sich geändert")
        for key, count in changed.most_common(15):
            print(f"  {key:44} {count:5d}  ({count / snaps * 100:5.1f}% der Zyklen)")
        quiet = channels["channels"] - len(changed)
        print(f"  … {quiet} Kanäle unverändert")
        if dur > 0:
            naive = channels["channels"] * snaps
            real = sum(changed.values())
            print()
            print(f"  Ohne Entprellung  {naive / dur * 86400:,.0f} State-Writes/Tag")
            print(f"  Nur bei Änderung  {real / dur * 86400:,.0f} State-Writes/Tag"
                  f"   ({real / naive * 100:.1f}%)" if naive else "")
    print("=" * 66)


def run_report(args: argparse.Namespace) -> int:
    path = Path(args.recording)
    if not path.exists():
        print(f"Datei nicht gefunden: {path}", file=sys.stderr)
        return 1
    raw = load_recording(path)
    snapshots = raw.pop("_snapshots", [])
    channels = analyse_channels(snapshots) if snapshots else None
    if snapshots and not channels:
        channels = None
    print_report(build_report(raw), channels)
    if not snapshots:
        print("\nHinweis: Mitschnitt ohne Rohdaten (--no-raw) — "
              "Kanalauswertung nicht möglich.")
    return 0


# ----------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    # Ohne das bleibt stdout blockgepuffert, sobald es in eine Datei umgeleitet
    # oder von einem Wrapper mitgeschnitten wird - die Statuszeilen von
    # _status_loop kämen dann erst beim Prozessende an, nicht laufend.
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(
        prog="ws_probe",
        description="Mitschnitt und Auswertung des PoolDose-WebSockets.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    rec = sub.add_parser("record", help="Verbinden, zuhören, mitschneiden")
    rec.add_argument("--host", required=True, help="IP oder Hostname des Geräts")
    rec.add_argument("--port", type=int, default=DEFAULT_PORT)
    rec.add_argument("--duration", type=float, default=0,
                     help="Sekunden; 0 = bis Strg+C (Vorgabe)")
    rec.add_argument("--out", default=None,
                     help="Zieldatei (.jsonl oder .jsonl.gz). Ohne Angabe nur Statistik.")
    rec.add_argument("--watchdog", type=float, default=DEFAULT_WATCHDOG,
                     help=f"Sekunden ohne Frame bis Reconnect (Vorgabe {DEFAULT_WATCHDOG:.0f})")
    rec.add_argument("--http-probe", type=float, default=0, metavar="SEK",
                     help="HTTP-Latenz alle SEK Sekunden mitmessen; 0 = aus")
    rec.add_argument("--no-raw", dest="raw", action="store_false",
                     help="Nur Metadaten mitschneiden, keine Nutzdaten (kleine Dateien)")
    rec.add_argument("--keep-serial", dest="redact_serial", action="store_false",
                     help="Seriennummer im Mitschnitt belassen (Vorgabe: ersetzen)")
    rec.set_defaults(raw=True, redact_serial=True)

    rep = sub.add_parser("report", help="Mitschnitt auswerten")
    rep.add_argument("recording", help="Pfad zur .jsonl oder .jsonl.gz")

    args = parser.parse_args(argv)
    if args.cmd == "record":
        try:
            return asyncio.run(run_record(args))
        except KeyboardInterrupt:
            return 130
    return run_report(args)


if __name__ == "__main__":
    sys.exit(main())
