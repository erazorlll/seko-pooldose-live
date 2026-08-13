"""Select-Plattform für pooldose_live: dynamisch aus aufgelösten Kanälen.

Schreibend (P4). Die Optionsliste kommt aus `channel.options` - generisch
aus den `comboitems` des zuletzt empfangenen Snapshots dekodiert
(channels.py), nicht aus einer Mapping-Tabelle. Funktioniert deshalb auch
im Raw-Modus.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity, SelectEntityDescription
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
    """Richtet die Select-Plattform ein und hört auf neue Kanäle."""
    coordinator = config_entry.runtime_data
    added: set[str] = set()

    @callback
    def _add_new() -> None:
        new_names = [
            name
            for name, rc in coordinator.data.items()
            if rc.type == "select" and name not in added
        ]
        if not new_names:
            return
        added.update(new_names)
        async_add_entities(
            PooldoseLiveSelect(coordinator, name, _describe(name)) for name in new_names
        )

    config_entry.async_on_unload(coordinator.async_add_listener(_add_new))
    _add_new()


def _describe(name: str) -> SelectEntityDescription:
    return SelectEntityDescription(
        key=name,
        name=name.removeprefix("raw_").replace("_", " ").strip().capitalize(),
        entity_registry_enabled_default=not name.startswith("raw_"),
    )


class PooldoseLiveSelect(PooldoseLiveEntity, SelectEntity):
    """Select-Entity für einen aufgelösten pooldose_live-Kanal."""

    @property
    def current_option(self) -> str | None:
        resolved = self.resolved
        return resolved.display if resolved else None

    @property
    def options(self) -> list[str]:
        resolved = self.resolved
        if resolved and resolved.channel.options:
            return sorted(resolved.channel.options.values())
        return []

    async def async_select_option(self, option: str) -> None:
        await self._async_write_value(option)
