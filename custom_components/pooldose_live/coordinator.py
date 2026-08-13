"""Push-Coordinator: hält den zuletzt aufgelösten Kanal-Stand.

Bewusst KEIN Poll-Coordinator: `update_interval=None`, Daten kommen
ausschließlich per Push aus `PooldoseTransport.events()`. Setup blockiert
nicht auf die erste Nachricht - das Gerät kann laut Konzept §8.2/§8.3
minutenlang schweigen, ohne dass etwas kaputt ist.

Trackt nebenbei eine kleine Sitzungsstatistik (Reconnects, längste Lücke,
letzter Roh-Snapshot, zuletzt bekannter Standby-Zustand) für diagnostics.py -
das leichtgewichtige Live-Pendant zu tools/ws_probe.py's Stats-Klasse aus P0.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from pooldose_live.channels import decode_devicedata, detect_prefix
from pooldose_live.mapping import ModelMapping, ResolvedChannel
from pooldose_live.mapping import load as load_mapping
from pooldose_live.transport import PooldoseTransport, TransportEvent

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

type PooldoseLiveConfigEntry = ConfigEntry[PooldoseLiveCoordinator]


class PooldoseLiveCoordinator(DataUpdateCoordinator[dict[str, ResolvedChannel]]):
    """Hält den zuletzt aufgelösten Kanal-Stand, aktualisiert per WS-Push."""

    config_entry: PooldoseLiveConfigEntry

    def __init__(self, hass: HomeAssistant, config_entry: PooldoseLiveConfigEntry) -> None:
        super().__init__(
            hass, _LOGGER, name=DOMAIN, config_entry=config_entry, update_interval=None
        )
        self.host: str = config_entry.data["host"]
        self.transport = PooldoseTransport(self.host)
        self.mapping: ModelMapping | None = None
        self.is_stale = False
        self.last_known_standby: bool | None = None
        self._last_display: dict[str, object] | None = None
        self.data = {}

        # Diagnostics (diagnostics.py) - nur für die laufende Sitzung, kein
        # Ersatz für einen echten Mitschnitt wie in P0.
        self.session_start = time.monotonic()
        self.stats = {"connects": 0, "disconnects": 0, "watchdog_trips": 0, "cycles": 0}
        self.last_snapshot_raw: dict[str, Any] | None = None
        self.last_snapshot_device_id: str | None = None
        self.last_snapshot_time: float | None = None
        self.longest_gap: float = 0.0

    def start(self) -> None:
        """Startet den Hintergrund-Task, an die Lebenszeit des Entries gebunden."""
        self.config_entry.async_create_background_task(
            self.hass, self._run(), name=f"{DOMAIN}_{self.host}_transport"
        )

    async def _run(self) -> None:
        async for event in self.transport.events():
            self._handle_event(event)

    @callback
    def _handle_event(self, event: TransportEvent) -> None:
        if event.kind == "snapshot":
            self._handle_snapshot(event)
        elif event.kind == "stale":
            if not self.is_stale:
                self.is_stale = True
                self.async_update_listeners()
        elif event.kind == "fresh":
            if self.is_stale:
                self.is_stale = False
                self.async_update_listeners()
        elif event.kind == "connected":
            self.stats["connects"] += 1
        elif event.kind == "disconnected":
            self.stats["disconnects"] += 1
        elif event.kind == "watchdog":
            self.stats["watchdog_trips"] += 1
        if event.kind in ("connected", "disconnected", "watchdog"):
            _LOGGER.debug(
                "%s: %s%s", self.host, event.kind, f" ({event.reason})" if event.reason else ""
            )

    def _handle_snapshot(self, event: TransportEvent) -> None:
        now = time.monotonic()
        if self.last_snapshot_time is not None:
            self.longest_gap = max(self.longest_gap, now - self.last_snapshot_time)
        self.last_snapshot_time = now
        self.stats["cycles"] += 1
        self.last_snapshot_raw = event.devicedata
        self.last_snapshot_device_id = event.device_id

        channels = decode_devicedata(event.devicedata or {})

        if self.mapping is None:
            prefix = detect_prefix(event.devicedata or {})
            if prefix is None:
                return
            self.mapping = load_mapping(*prefix)

        resolved = self.mapping.resolve_all(channels)
        # visible=false erzeugt keine Entity (Konzept B3) - hier gefiltert,
        # damit sensor.py/binary_sensor.py sich nicht selbst darum kümmern
        # müssen.
        by_name = {rc.name: rc for rc in resolved if rc.channel.visible}

        # Standby-Kontext (Konzept §8.4) unabhängig vom Änderungs-Filter
        # unten mitführen - er soll auch dann aktuell bleiben, wenn sich
        # sonst nichts geändert hat.
        standby = by_name.get("alarm_system_standby")
        if standby is not None:
            self.last_known_standby = bool(standby.display)

        # Minimaler Änderungs-Filter: nur schreiben, wenn sich mindestens ein
        # Anzeigewert geändert hat. Die volle Entprellung mit
        # Resolution-Schwellen und Heartbeat (Konzept §5.6) läuft zusätzlich
        # pro Entity (siehe entity.py) - das hier verhindert nur den
        # trivialen Fall "kompletter Zyklus ohne jede Änderung", der laut
        # §8.2 die deutliche Mehrheit der Zyklen ist.
        display = {name: rc.display for name, rc in by_name.items()}
        if display == self._last_display:
            return
        self._last_display = display

        self.async_set_updated_data(by_name)
