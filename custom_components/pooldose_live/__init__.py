"""Die pooldose_live-Integration - eigene Domain, parallel zur
Core-Integration `pooldose` installierbar (Konzept §5.1). Seit P4 auch
schreibend (number/select/switch, siehe entity.py/pooldose_live.write).
"""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import PooldoseLiveConfigEntry, PooldoseLiveCoordinator

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SWITCH,
]


async def async_setup_entry(hass: HomeAssistant, entry: PooldoseLiveConfigEntry) -> bool:
    """Set up pooldose_live from a config entry.

    Bewusst KEIN `await coordinator.async_config_entry_first_refresh()`: das
    würde auf die erste Nachricht warten, und die kann laut Konzept §8.2/§8.3
    mehrere Minuten auf sich warten lassen. Der Coordinator startet seinen
    Hintergrund-Task und der Entry-Setup kehrt sofort zurück; Entities
    entstehen dynamisch, sobald Daten da sind (siehe sensor.py/binary_sensor.py).
    """
    coordinator = PooldoseLiveCoordinator(hass, entry)
    entry.runtime_data = coordinator
    coordinator.start()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PooldoseLiveConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
