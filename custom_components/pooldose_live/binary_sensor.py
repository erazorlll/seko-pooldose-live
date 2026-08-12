"""Binary-Sensor-Plattform für pooldose_live: dynamisch aus aufgelösten Kanälen.

Siehe sensor.py für die Begründung des dynamischen (statt statischen)
Entity-Aufbaus.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
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
    """Richtet die Binary-Sensor-Plattform ein und hört auf neue Kanäle."""
    coordinator = config_entry.runtime_data
    added: set[str] = set()

    @callback
    def _add_new() -> None:
        new_names = [
            name
            for name, rc in coordinator.data.items()
            if rc.type == "binary_sensor" and name not in added
        ]
        if not new_names:
            return
        added.update(new_names)
        async_add_entities(
            PooldoseLiveBinarySensor(coordinator, name, _describe(name)) for name in new_names
        )

    config_entry.async_on_unload(coordinator.async_add_listener(_add_new))
    _add_new()


def _describe(name: str) -> BinarySensorEntityDescription:
    # Siehe sensor.py: kein translation_key moeglich fuer dynamische Namen.
    return BinarySensorEntityDescription(
        key=name,
        name=name.removeprefix("raw_").replace("_", " ").strip().capitalize(),
        entity_registry_enabled_default=not name.startswith("raw_"),
    )


class PooldoseLiveBinarySensor(PooldoseLiveEntity, BinarySensorEntity):
    """Binary-Sensor-Entity für einen aufgelösten pooldose_live-Kanal."""

    @property
    def is_on(self) -> bool | None:
        resolved = self.resolved
        return bool(resolved.display) if resolved else None
