"""The pooldose_live integration - own domain, installable in parallel with
the core integration `pooldose` (concept §5.1). Writable since P4
(number/select/switch, see entity.py/pooldose_live.write).
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

    Deliberately NOT `await coordinator.async_config_entry_first_refresh()`:
    that would wait for the first message, and per concept §8.2/§8.3 that
    can take several minutes. The coordinator starts its background task
    and entry setup returns immediately; entities appear dynamically once
    data arrives (see sensor.py/binary_sensor.py).
    """
    coordinator = PooldoseLiveCoordinator(hass, entry)
    entry.runtime_data = coordinator
    coordinator.start()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PooldoseLiveConfigEntry) -> bool:
    """Unload a config entry."""
    entry.runtime_data.cleanup_issues()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
