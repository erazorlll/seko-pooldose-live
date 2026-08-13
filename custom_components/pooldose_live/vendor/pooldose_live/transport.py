"""WebSocket transport for the local PoolDose stream (concept §5.3).

One connection, automatic reconnect with backoff, reassembly by
`progressInfo`, and two separately maintained watchdogs:

- **Connection watchdog**: no frame of any topic within
  `connection_watchdog` seconds -> reconnect. Catches actual connection
  drops.
- **Staleness watchdog**: no *complete* `instant_values` cycle within
  `staleness_timeout` seconds -> `stale` event, **without** dropping the
  connection.

The second watchdog isn't a nice-to-have: the 11h measurement (concept
§8.2) found recurring dropouts of up to 6.5 minutes *exclusively* on
`instant_values`, while `wifi_station`/`time` kept running normally and the
connection never dropped. A plain connection watchdog would never have
noticed.

The reassembly logic and the core loop are taken from `tools/ws_probe.py`
(validated there against roughly 15h of real device data, including the fix
for the `--duration` bug, see the commit history), here as a reusable
library instead of a CLI script with side effects.
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
"""90s: above the normal tick-jitter range (base tick ~4.2s, even with a few
dropped ticks you usually stay well under this), but far below the real
dropouts measured in §8.2/§8.3 (up to 9.7 minutes) - so channels get marked
stale promptly, without flapping on normal jitter."""
DEFAULT_STALENESS_CHECK_INTERVAL = 5.0
DEFAULT_BACKOFF_START = 2.0
DEFAULT_BACKOFF_MAX = 60.0

TOPIC_INSTANT_VALUES = "instant_values"

EventKind = Literal["connected", "disconnected", "watchdog", "snapshot", "stale", "fresh"]


@dataclass
class TransportEvent:
    """An event from `PooldoseTransport.events()`."""

    kind: EventKind
    t: float
    """Seconds since `events()` started (monotonic)."""
    device_id: str | None = None
    devicedata: dict[str, Any] | None = None
    """For kind='snapshot': the merged, complete devicedata[<device_id>] dict."""
    reason: str | None = None
    """For kind='disconnected'/'watchdog'."""
    since: float | None = None
    """For kind='stale': seconds since the last complete cycle."""
    retry_in: float | None = None
    """For kind='disconnected': backoff until the next connection attempt."""


class Reassembler:
    """Reassembles chunked instant_values cycles per the rules in the spec.

    On `offset == 1` the buffer is cleared (half-cycles are real, otherwise
    you get Frankenstein records mixing two rounds); only on
    `offset == total` does it get processed further.
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
    """Holds a WebSocket connection to a PoolDose and delivers snapshots.

    Runs forever (until the consumer stops iterating or the task gets
    cancelled) - reconnects automatically with exponential backoff. Exactly
    one connection per instance, as recommended in concept §4.
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
                            # Set the staleness baseline to the connection
                            # time instead of leaving it at None: if we
                            # connect right in the middle of a dropout
                            # (§8.2/§8.3), "never seen a snapshot yet" also
                            # has to count as potentially stale - otherwise
                            # staleness_checker() never checks, because
                            # last_snapshot_t stays None forever.
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
        """Reads frames until the connection ends. Returns the reason it ended."""
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=self.connection_watchdog)
            except asyncio.TimeoutError:
                await queue.put(TransportEvent(kind="watchdog", t=time.monotonic() - t0,
                                               reason=f"{self.connection_watchdog:.0f}s without a frame"))
                return f"watchdog ({self.connection_watchdog:.0f}s without a frame)"

            if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSING):
                return f"remote side closed ({msg.type.name})"
            if msg.type is aiohttp.WSMsgType.ERROR:
                return f"WS error: {ws.exception()}"
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
