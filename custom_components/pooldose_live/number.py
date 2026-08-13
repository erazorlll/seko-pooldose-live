"""Number platform for pooldose_live: dynamic from resolved channels.

Writable (P4) - see entity.py._async_write_value() and pooldose_live.write
for validation/HTTP mechanics. Bounds/step come from the most recently
received snapshot (absMin/absMax/resolution), not a static table.
"""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PooldoseLiveConfigEntry, PooldoseLiveCoordinator
from .entity import PooldoseLiveEntity

PARALLEL_UPDATES = 1  # Writes not in parallel - easier on the connection


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: PooldoseLiveConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Sets up the number platform and listens for new channels."""
    coordinator = config_entry.runtime_data
    added: set[str] = set()

    @callback
    def _add_new() -> None:
        new_names = [
            name
            for name, rc in coordinator.data.items()
            if rc.type == "number" and name not in added
        ]
        if not new_names:
            return
        added.update(new_names)
        async_add_entities(
            PooldoseLiveNumber(coordinator, name, _describe(name)) for name in new_names
        )

    config_entry.async_on_unload(coordinator.async_add_listener(_add_new))
    _add_new()


def _describe(name: str) -> NumberEntityDescription:
    # See sensor.py: no translation_key possible for dynamic names.
    return NumberEntityDescription(
        key=name,
        name=name.removeprefix("raw_").replace("_", " ").strip().capitalize(),
        entity_registry_enabled_default=not name.startswith("raw_"),
    )


class PooldoseLiveNumber(PooldoseLiveEntity, NumberEntity):
    """Number entity for a resolved pooldose_live channel."""

    @property
    def native_value(self) -> float | None:
        resolved = self.resolved
        return resolved.display if resolved else None

    @property
    def native_unit_of_measurement(self) -> str | None:
        resolved = self.resolved
        return resolved.channel.unit if resolved else None

    @property
    def native_min_value(self) -> float:
        resolved = self.resolved
        value = resolved.min if resolved else None
        return value if isinstance(value, (int, float)) else 0.0

    @property
    def native_max_value(self) -> float:
        resolved = self.resolved
        value = resolved.max if resolved else None
        return value if isinstance(value, (int, float)) else 100.0

    @property
    def native_step(self) -> float:
        resolved = self.resolved
        value = resolved.step if resolved else None
        return value if isinstance(value, (int, float)) and value > 0 else 1.0

    async def async_set_native_value(self, value: float) -> None:
        await self._async_write_value(value)
