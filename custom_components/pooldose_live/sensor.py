"""Sensor-Plattform für pooldose_live: Entities dynamisch aus aufgelösten Kanälen.

Anders als die statische EntityDescription-Tabelle der Core-Integration:
welche Kanäle es gibt, steht erst zur Laufzeit fest (Mapping-Treffer oder
Raw-Fallback, Konzept §5.5). Entities entstehen deshalb dynamisch, sobald ein
Kanalname zum ersten Mal im Coordinator auftaucht - nicht nur beim Setup,
da der erste Snapshot laut §8.2/§8.3 auch erst nach dem Start der
Plattform eintreffen kann.
"""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PooldoseLiveConfigEntry, PooldoseLiveCoordinator
from .entity import PooldoseLiveEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: PooldoseLiveConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Richtet die Sensor-Plattform ein und hört auf neue Kanäle."""
    coordinator = config_entry.runtime_data
    added: set[str] = set()

    @callback
    def _add_new() -> None:
        new_names = [
            name
            for name, rc in coordinator.data.items()
            if rc.type == "sensor" and name not in added
        ]
        if not new_names:
            return
        added.update(new_names)
        async_add_entities(
            PooldoseLiveSensor(coordinator, name, _describe(name)) for name in new_names
        )

    config_entry.async_on_unload(coordinator.async_add_listener(_add_new))
    _add_new()


def _describe(name: str) -> SensorEntityDescription:
    # Kein translation_key: die Kanalmenge ist dynamisch (Mapping-Treffer
    # oder Raw-Fallback mit hash-basiertem Namen, Konzept §5.5) und lässt
    # sich nicht vorab in einer strings.json abdecken - direkt ein lesbarer
    # Name statt eines Schlüssels ohne Übersetzungstreffer.
    # Raw-Fallback-Kanäle standardmäßig deaktiviert - Diagnose-Material für
    # unbekannte/neue Geräte, kein kuratiertes UI.
    return SensorEntityDescription(
        key=name,
        name=name.removeprefix("raw_").replace("_", " ").strip().capitalize(),
        entity_registry_enabled_default=not name.startswith("raw_"),
    )


class PooldoseLiveSensor(PooldoseLiveEntity, SensorEntity):
    """Sensor-Entity für einen aufgelösten pooldose_live-Kanal."""

    @property
    def native_value(self) -> object:
        resolved = self.resolved
        return resolved.display if resolved else None

    @property
    def native_unit_of_measurement(self) -> str | None:
        resolved = self.resolved
        return resolved.channel.unit if resolved else None

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        # Konzept B4: das alarm-Flag wird von der Core-Integration nirgends
        # sichtbar gemacht - hier als Attribut, statt es zu verwerfen.
        resolved = self.resolved
        if resolved and resolved.channel.alarm is not None:
            return {"alarm": resolved.channel.alarm}
        return None
