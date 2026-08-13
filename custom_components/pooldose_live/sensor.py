"""Sensor platform for pooldose_live: entities dynamically from resolved channels.

Unlike the core integration's static EntityDescription table: which channels
exist is only known at runtime (mapping hit or raw fallback, concept §5.5).
Entities are therefore created dynamically as soon as a channel name first
appears in the coordinator - not only at setup, since per §8.2/§8.3 the
first snapshot can also arrive only after the platform has started.
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
    """Sets up the sensor platform and listens for new channels."""
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
    # No translation_key: the set of channels is dynamic (mapping hit or
    # raw fallback with a hash-based name, concept §5.5) and can't be
    # covered in a strings.json ahead of time - a readable name directly
    # instead of a key without a translation match.
    # Raw-fallback channels disabled by default - diagnostic material for
    # unknown/new devices, not a curated UI.
    return SensorEntityDescription(
        key=name,
        name=name.removeprefix("raw_").replace("_", " ").strip().capitalize(),
        entity_registry_enabled_default=not name.startswith("raw_"),
    )


class PooldoseLiveSensor(PooldoseLiveEntity, SensorEntity):
    """Sensor entity for a resolved pooldose_live channel."""

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
        # Concept B4: the alarm flag isn't surfaced anywhere by the core
        # integration - exposed here as an attribute instead of discarding it.
        resolved = self.resolved
        if resolved and resolved.channel.alarm is not None:
            return {"alarm": resolved.channel.alarm}
        return None
