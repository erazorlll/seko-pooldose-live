"""Base entity for pooldose_live.

Also contains the debouncing at the entity level (concept §5.6): the
coordinator notifies on EVERY change to any channel, but without a second
filter here that would make every one of the ~40-70 entities write on any
single channel changing. `_handle_coordinator_update()` therefore checks
per entity whether its OWN value changed relevantly (resolution-aware for
numbers) before calling `async_write_ha_state()` - plus a heartbeat, so
recorder histories don't develop gaps over hours when a channel simply
stays constant.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import aiohttp

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .vendor.pooldose_live.mapping import ResolvedChannel
from .vendor.pooldose_live.write import WriteError, set_channel

from .const import DOMAIN, MANUFACTURER
from .coordinator import PooldoseLiveCoordinator

HEARTBEAT_INTERVAL = 300.0
"""5 minutes, concept §5.6 - forces a write even without a relevant change,
so recorder time series don't have unbounded gaps."""

# Channels whose last known value should count as "available" even during
# an instant_values staleness period - because they're themselves the
# diagnostic signal that might explain the staleness (concept §8.4:
# alarm_system_standby doesn't correlate with most dropouts, but is still
# the most relevant known context for them).
ALWAYS_AVAILABLE_WHEN_STALE = {"alarm_system_standby"}


def _device_info(coordinator: PooldoseLiveCoordinator) -> DeviceInfo:
    """Device name/model only available once the first snapshot arrived -
    a placeholder before that instead of an HTTP call (concept §4:
    model/FW are derivable from the data keys, no HTTP needed)."""
    mapping = coordinator.mapping
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.host)},
        manufacturer=MANUFACTURER,
        model=mapping.model_id if mapping else None,
        name=(f"PoolDose {mapping.model_id}" if mapping else f"PoolDose ({coordinator.host})"),
        sw_version=f"FW{mapping.fw_code}" if mapping else None,
        configuration_url=f"http://{coordinator.host}/index.html",
    )


def _is_relevant_change(old: Any, new: Any, resolution: Any) -> bool:
    """Numeric: at least one resolution step of difference. Otherwise: any
    change. `bool` is never treated as "numeric" despite inheriting from
    int - a flag flip is always relevant."""
    if old is None:
        return True
    is_num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)  # noqa: E731
    if is_num(old) and is_num(new) and is_num(resolution):
        return abs(new - old) >= resolution
    return old != new


class PooldoseLiveEntity(CoordinatorEntity[PooldoseLiveCoordinator]):
    """Base class for all pooldose_live entities.

    A channel name (`channel_name`) identifies the entity to the
    coordinator - the current value isn't frozen in the constructor, but
    re-read via `resolved` on every update.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PooldoseLiveCoordinator,
        channel_name: str,
        entity_description: EntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = entity_description
        self._channel_name = channel_name
        self._attr_unique_id = f"{coordinator.host}_{channel_name}"
        self._attr_device_info = _device_info(coordinator)
        self._last_written_display: Any = None
        self._last_written_available: bool | None = None
        self._last_written_time = 0.0

    @property
    def resolved(self) -> ResolvedChannel | None:
        return self.coordinator.data.get(self._channel_name)

    @property
    def available(self) -> bool:
        """Unavailable if the channel is missing or the coordinator has
        marked the instant_values data as stale (staleness watchdog,
        concept §5.3) - except for channels in `ALWAYS_AVAILABLE_WHEN_STALE`."""
        if not super().available or self.resolved is None:
            return False
        if self._channel_name in ALWAYS_AVAILABLE_WHEN_STALE:
            return True
        return not self.coordinator.is_stale

    @callback
    def _handle_coordinator_update(self) -> None:
        """Only write on a relevant change, an availability change, or a
        due heartbeat - see the module docstring."""
        now = time.monotonic()
        avail = self.available
        resolved = self.resolved
        display = resolved.display if resolved else None
        resolution = resolved.channel.resolution if resolved else None

        due_heartbeat = (now - self._last_written_time) >= HEARTBEAT_INTERVAL
        relevant = _is_relevant_change(self._last_written_display, display, resolution)
        avail_changed = avail != self._last_written_available

        if relevant or avail_changed or due_heartbeat:
            self._last_written_display = display
            self._last_written_available = avail
            self._last_written_time = now
            self.async_write_ha_state()

    async def _async_write_value(self, value: Any) -> None:
        """Writes a new value for this channel (concept §5.1/§5.6).

        No optimistic setting of local state - confirmation arrives with
        the next WS tick (~4s) through the normal coordinator update path,
        see pooldose_live.write for the reasoning.
        """
        resolved = self.resolved
        mapping = self.coordinator.mapping
        device_id = self.coordinator.last_snapshot_device_id
        if resolved is None or mapping is None or device_id is None:
            raise HomeAssistantError("No current channel state available")

        session = async_get_clientsession(self.hass)
        try:
            await set_channel(
                session, self.coordinator.host, device_id, mapping,
                resolved.channel, resolved.type, value,
            )
        except WriteError as err:
            raise ServiceValidationError(str(err)) from err
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise HomeAssistantError(f"Write failed: {err}") from err
