"""Binary sensor platform for pooldose_live: dynamic from resolved channels.

See sensor.py for the rationale behind the dynamic (rather than static)
entity setup.
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
    """Sets up the binary sensor platform and listens for new channels."""
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
    # See sensor.py: no translation_key possible for dynamic names.
    return BinarySensorEntityDescription(
        key=name,
        name=name.removeprefix("raw_").replace("_", " ").strip().capitalize(),
        entity_registry_enabled_default=not name.startswith("raw_"),
    )


class PooldoseLiveBinarySensor(PooldoseLiveEntity, BinarySensorEntity):
    """Binary sensor entity for a resolved pooldose_live channel."""

    @property
    def is_on(self) -> bool | None:
        resolved = self.resolved
        return bool(resolved.display) if resolved else None
