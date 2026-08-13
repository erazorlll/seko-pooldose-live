"""Basis-Entity für pooldose_live.

Enthält auch die Entprellung auf Entity-Ebene (Konzept §5.6): Der
Coordinator benachrichtigt bei JEDER Änderung irgendeines Kanals, aber ohne
einen zweiten Filter hier würde das jede der ~40-70 Entities zum Schreiben
bringen, sobald sich auch nur ein einziger Kanal ändert. `_handle_coordinator_
update()` prüft deshalb pro Entity, ob der EIGENE Wert sich relevant
geändert hat (resolution-bewusst bei Zahlen), bevor `async_write_ha_state()`
aufgerufen wird - plus ein Heartbeat, damit Recorder-Verläufe nicht über
Stunden abreißen, wenn ein Kanal einfach konstant bleibt.
"""

from __future__ import annotations

import time
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from pooldose_live.mapping import ResolvedChannel

from .const import DOMAIN, MANUFACTURER
from .coordinator import PooldoseLiveCoordinator

HEARTBEAT_INTERVAL = 300.0
"""5 Minuten, Konzept §5.6 - erzwingt einen Schreibvorgang auch ohne
relevante Änderung, damit Recorder-Zeitreihen nicht unbegrenzt Lücken haben."""

# Kanäle, deren letzter bekannter Wert auch während einer instant_values-
# Staleness-Phase als "verfügbar" gelten soll - weil sie selbst das
# Diagnose-Signal sind, das die Staleness erklären könnte (Konzept §8.4:
# alarm_system_standby korreliert zwar nicht mit den meisten Aussetzern,
# ist aber trotzdem der relevanteste bekannte Kontext dafür).
ALWAYS_AVAILABLE_WHEN_STALE = {"alarm_system_standby"}


def _device_info(coordinator: PooldoseLiveCoordinator) -> DeviceInfo:
    """Gerätename/-modell erst vorhanden, sobald der erste Snapshot ankam -
    vorher Platzhalter statt eines HTTP-Calls (Konzept §4: Modell/FW sind
    aus den Daten-Keys ableitbar, kein HTTP nötig)."""
    mapping = coordinator.mapping
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.host)},
        manufacturer=MANUFACTURER,
        model=mapping.model_id if mapping else None,
        name=(f"PoolDose {mapping.model_id}" if mapping else f"PoolDose ({coordinator.host})"),
        sw_version=f"FW{mapping.fw_code}" if mapping else None,
        configuration_url=f"http://{coordinator.host}/index.html",
    )


def _is_relevant_change(old: Any, new: Any, resolution: Any) -> bool:
    """Numerisch: mindestens eine Auflösungsstufe Unterschied. Sonst: jede
    Änderung. `bool` wird trotz int-Erbschaft nie als "numerisch" behandelt -
    ein Flag-Wechsel ist immer relevant."""
    if old is None:
        return True
    is_num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)  # noqa: E731
    if is_num(old) and is_num(new) and is_num(resolution):
        return abs(new - old) >= resolution
    return old != new


class PooldoseLiveEntity(CoordinatorEntity[PooldoseLiveCoordinator]):
    """Basisklasse für alle pooldose_live-Entities.

    Ein Kanalname (`channel_name`) identifiziert die Entity gegenüber dem
    Coordinator - der aktuelle Wert wird nicht im Konstruktor eingefroren,
    sondern bei jedem Update über `resolved` neu gelesen.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PooldoseLiveCoordinator,
        channel_name: str,
        entity_description: EntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = entity_description
        self._channel_name = channel_name
        self._attr_unique_id = f"{coordinator.host}_{channel_name}"
        self._attr_device_info = _device_info(coordinator)
        self._last_written_display: Any = None
        self._last_written_available: bool | None = None
        self._last_written_time = 0.0

    @property
    def resolved(self) -> ResolvedChannel | None:
        return self.coordinator.data.get(self._channel_name)

    @property
    def available(self) -> bool:
        """Nicht verfügbar, wenn der Kanal fehlt oder der Coordinator die
        instant_values-Daten als veraltet markiert hat (Staleness-Watchdog,
        Konzept §5.3) - außer für Kanäle in `ALWAYS_AVAILABLE_WHEN_STALE`."""
        if not super().available or self.resolved is None:
            return False
        if self._channel_name in ALWAYS_AVAILABLE_WHEN_STALE:
            return True
        return not self.coordinator.is_stale

    @callback
    def _handle_coordinator_update(self) -> None:
        """Nur schreiben bei relevanter Änderung, Verfügbarkeitswechsel oder
        fälligem Heartbeat - siehe Modul-Docstring."""
        now = time.monotonic()
        avail = self.available
        resolved = self.resolved
        display = resolved.display if resolved else None
        resolution = resolved.channel.resolution if resolved else None

        due_heartbeat = (now - self._last_written_time) >= HEARTBEAT_INTERVAL
        relevant = _is_relevant_change(self._last_written_display, display, resolution)
        avail_changed = avail != self._last_written_available

        if relevant or avail_changed or due_heartbeat:
            self._last_written_display = display
            self._last_written_available = avail
            self._last_written_time = now
            self.async_write_ha_state()
