"""Switch platform for pooldose_live: dynamic from resolved channels.

Writable (P4). Only mapped switch channels - in raw mode, bare booleans are
classified as binary_sensor, not switch (concept §5.5): without a mapping
table we don't know whether a channel is genuinely writable, and a wrongly
written frame can, per websocker-spec.md, change parameters on a real
dosing system.
"""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import PooldoseLiveConfigEntry, PooldoseLiveCoordinator
from .entity import PooldoseLiveEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: PooldoseLiveConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Sets up the switch platform and listens for new channels."""
    coordinator = config_entry.runtime_data
    added: set[str] = set()

    @callback
    def _add_new() -> None:
        new_names = [
            name
            for name, rc in coordinator.data.items()
            if rc.type == "switch" and name not in added
        ]
        if not new_names:
            return
        added.update(new_names)
        async_add_entities(
            PooldoseLiveSwitch(coordinator, name, _describe(name)) for name in new_names
        )

    config_entry.async_on_unload(coordinator.async_add_listener(_add_new))
    _add_new()


def _describe(name: str) -> SwitchEntityDescription:
    return SwitchEntityDescription(key=name, name=name.replace("_", " ").strip().capitalize())


class PooldoseLiveSwitch(PooldoseLiveEntity, SwitchEntity):
    """Switch entity for a resolved pooldose_live channel."""

    @property
    def is_on(self) -> bool | None:
        resolved = self.resolved
        return bool(resolved.display) if resolved else None

    async def async_turn_on(self, **kwargs: object) -> None:
        await self._async_write_value(True)

    async def async_turn_off(self, **kwargs: object) -> None:
        await self._async_write_value(False)
