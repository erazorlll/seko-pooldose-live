"""WebSocket-Transport für den lokalen PoolDose-Stream (Konzept §5.3).

Eine Verbindung, automatischer Reconnect mit Backoff, Reassembly nach
`progressInfo`, und zwei separat geführte Watchdogs:

- **Verbindungs-Watchdog**: kein Frame irgendeines Topics innerhalb von
  `connection_watchdog` Sekunden -> Reconnect. Deckt echte Verbindungsabbrüche.
- **Staleness-Watchdog**: kein *vollständiger* `instant_values`-Zyklus
  innerhalb von `staleness_timeout` Sekunden -> `stale`-Event, **ohne** die
  Verbindung zu trennen.

Der zweite Watchdog ist kein Kür-Feature: Die 11h-Messung (Konzept §8.2) fand
wiederholte, bis zu 6,5-minütige Aussetzer *ausschließlich* bei
`instant_values`, während `wifi_station`/`time` normal weiterliefen und die
Verbindung nie abriss. Ein reiner Verbindungs-Watchdog hätte das nie bemerkt.

Reassembly-Logik und die Kernschleife sind aus `tools/ws_probe.py` übernommen
(dort an ca. 15h realer Gerätedaten validiert, inkl. des Fixes für den
`--duration`-Bug, siehe Commit-Historie), hier als wiederverwendbare
Bibliothek statt CLI-Skript mit Seiteneffekten.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal

import aiohttp

DEFAULT_PORT = 1334
DEFAULT_CONNECTION_WATCHDOG = 30.0
DEFAULT_STALENESS_TIMEOUT = 90.0
"""90s: liegt über der normalen Tick-Jitter-Bandbreite (Grundtakt ~4,2s, auch
mit ein paar ausgefallenen Ticks bleibt man i.d.R. deutlich darunter), aber
weit unter den in §8.2/§8.3 gemessenen echten Aussetzern (bis zu 9,7 Minuten)
- markiert Kanäle also zeitnah als stale, ohne bei normalem Jitter zu flackern."""
DEFAULT_STALENESS_CHECK_INTERVAL = 5.0
DEFAULT_BACKOFF_START = 2.0
DEFAULT_BACKOFF_MAX = 60.0

TOPIC_INSTANT_VALUES = "instant_values"

EventKind = Literal["connected", "disconnected", "watchdog", "snapshot", "stale", "fresh"]


@dataclass
class TransportEvent:
    """Ein Ereignis aus `PooldoseTransport.events()`."""

    kind: EventKind
    t: float
    """Sekunden seit Start von `events()` (monotonic)."""
    device_id: str | None = None
    devicedata: dict[str, Any] | None = None
    """Bei kind='snapshot': gemergtes, vollständiges devicedata[<device_id>]-Dict."""
    reason: str | None = None
    """Bei kind='disconnected'/'watchdog'."""
    since: float | None = None
    """Bei kind='stale': Sekunden seit dem letzten vollständigen Zyklus."""
    retry_in: float | None = None
    """Bei kind='disconnected': Backoff bis zum nächsten Verbindungsversuch."""


class Reassembler:
    """Setzt gechunkte instant_values-Zyklen nach den Regeln der Spec zusammen.

    Bei `offset == 1` wird der Puffer geleert (halbe Zyklen sind real, sonst
    entstehen Frankenstein-Datensätze aus zwei Runden), erst bei
    `offset == total` wird weiterverarbeitet.
    """

    def __init__(self) -> None:
        self._buf: dict[str, dict[str, Any]] = {}
        self._open_cycle = False

    def feed(self, data: dict[str, Any]) -> dict[str, dict[str, Any]] | None:
        progress = data.get("progressInfo") or {}
        offset = progress.get("offset", 1)
        total = progress.get("total", 1)

        if offset == 1:
            self._buf.clear()
            self._open_cycle = True

        for serial, payload in (data.get("devicedata") or {}).items():
            if isinstance(payload, dict):
                self._buf.setdefault(serial, {}).update(payload)

        if offset == total:
            self._open_cycle = False
            return dict(self._buf)
        return None


class PooldoseTransport:
    """Hält eine WebSocket-Verbindung zu einer PoolDose und liefert Snapshots.

    Läuft für immer (bis der Konsument die Iteration abbricht oder die Task
    gecancelt wird) - reconnected automatisch mit exponentiellem Backoff.
    Genau eine Verbindung pro Instanz, wie in Konzept §4 empfohlen.
    """

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        *,
        connection_watchdog: float = DEFAULT_CONNECTION_WATCHDOG,
        staleness_timeout: float = DEFAULT_STALENESS_TIMEOUT,
        staleness_check_interval: float = DEFAULT_STALENESS_CHECK_INTERVAL,
        backoff_start: float = DEFAULT_BACKOFF_START,
        backoff_max: float = DEFAULT_BACKOFF_MAX,
    ) -> None:
        self.host = host
        self.port = port
        self.connection_watchdog = connection_watchdog
        self.staleness_timeout = staleness_timeout
        self.staleness_check_interval = staleness_check_interval
        self.backoff_start = backoff_start
        self.backoff_max = backoff_max

    async def events(self) -> AsyncIterator[TransportEvent]:
        t0 = time.monotonic()
        url = f"ws://{self.host}:{self.port}/"
        queue: asyncio.Queue[TransportEvent] = asyncio.Queue()

        state = {"last_snapshot_t": None, "is_stale": False}

        async def staleness_checker() -> None:
            while True:
                await asyncio.sleep(self.staleness_check_interval)
                last = state["last_snapshot_t"]
                if last is None or state["is_stale"]:
                    continue
                elapsed = time.monotonic() - last
                if elapsed >= self.staleness_timeout:
                    state["is_stale"] = True
                    await queue.put(TransportEvent(kind="stale", t=time.monotonic() - t0,
                                                   since=elapsed))

        async def connection_loop() -> None:
            backoff = self.backoff_start
            async with aiohttp.ClientSession() as session:
                while True:
                    reasm = Reassembler()
                    try:
                        async with session.ws_connect(
                            url, heartbeat=None, autoping=True,
                            timeout=aiohttp.ClientWSTimeout(ws_close=10),
                        ) as ws:
                            await queue.put(TransportEvent(kind="connected", t=time.monotonic() - t0))
                            backoff = self.backoff_start
                            # Baseline für die Staleness-Prüfung auf den
                            # Verbindungszeitpunkt setzen, nicht auf None
                            # belassen: verbindet man sich mitten in einen
                            # Aussetzer (§8.2/§8.3) hinein, muss "noch nie
                            # einen Snapshot gesehen" ebenfalls als
                            # potenziell stale zählen - sonst prüft
                            # staleness_checker() nie, weil last_snapshot_t
                            # dauerhaft None bleibt.
                            if state["last_snapshot_t"] is None:
                                state["last_snapshot_t"] = time.monotonic()
                            reason = await self._receive_loop(ws, reasm, queue, t0, state)
                    except (aiohttp.ClientError, OSError) as err:
                        reason = f"{type(err).__name__}: {err}"

                    await queue.put(TransportEvent(kind="disconnected", t=time.monotonic() - t0,
                                                   reason=reason, retry_in=backoff))
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, self.backoff_max)

        checker_task = asyncio.create_task(staleness_checker())
        conn_task = asyncio.create_task(connection_loop())
        try:
            while True:
                yield await queue.get()
        finally:
            checker_task.cancel()
            conn_task.cancel()
            await asyncio.gather(checker_task, conn_task, return_exceptions=True)

    async def _receive_loop(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        reasm: Reassembler,
        queue: asyncio.Queue[TransportEvent],
        t0: float,
        state: dict[str, Any],
    ) -> str:
        """Liest Frames bis Verbindungsende. Gibt den Abbruchgrund zurück."""
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=self.connection_watchdog)
            except asyncio.TimeoutError:
                await queue.put(TransportEvent(kind="watchdog", t=time.monotonic() - t0,
                                               reason=f"{self.connection_watchdog:.0f}s ohne Frame"))
                return f"watchdog ({self.connection_watchdog:.0f}s ohne Frame)"

            if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSING):
                return f"Gegenstelle hat geschlossen ({msg.type.name})"
            if msg.type is aiohttp.WSMsgType.ERROR:
                return f"WS-Fehler: {ws.exception()}"
            if msg.type is not aiohttp.WSMsgType.TEXT:
                continue

            try:
                payload = json.loads(msg.data)
            except json.JSONDecodeError:
                continue

            if payload.get("topic") != TOPIC_INSTANT_VALUES:
                continue

            snapshot = reasm.feed(payload.get("data") or {})
            if snapshot is None:
                continue

            now = time.monotonic()
            was_stale = state["is_stale"]
            state["last_snapshot_t"] = now
            state["is_stale"] = False
            if was_stale:
                await queue.put(TransportEvent(kind="fresh", t=now - t0))

            for device_id, devicedata in snapshot.items():
                await queue.put(TransportEvent(kind="snapshot", t=now - t0,
                                               device_id=device_id, devicedata=devicedata))
