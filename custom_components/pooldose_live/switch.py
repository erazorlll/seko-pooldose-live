"""Switch-Plattform für pooldose_live: dynamisch aus aufgelösten Kanälen.

Schreibend (P4). Nur gemappte switch-Kanäle - im Raw-Modus werden bare
Booleans als binary_sensor klassifiziert, nicht als switch (Konzept §5.5):
ohne Mapping-Tabelle wissen wir nicht, ob ein Kanal wirklich schreibbar ist,
und ein falsch geschriebener Frame kann laut websocker-spec.md Parameter
einer echten Dosieranlage verstellen.
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
    """Richtet die Switch-Plattform ein und hört auf neue Kanäle."""
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
    """Switch-Entity für einen aufgelösten pooldose_live-Kanal."""

    @property
    def is_on(self) -> bool | None:
        resolved = self.resolved
        return bool(resolved.display) if resolved else None

    async def async_turn_on(self, **kwargs: object) -> None:
        await self._async_write_value(True)

    async def async_turn_off(self, **kwargs: object) -> None:
        await self._async_write_value(False)
