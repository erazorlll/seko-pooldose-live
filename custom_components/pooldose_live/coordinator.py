"""Push coordinator: holds the most recently resolved channel state.

Deliberately NOT a poll coordinator: `update_interval=None`, data arrives
exclusively via push from `PooldoseTransport.events()`. Setup doesn't block
on the first message - per concept §8.2/§8.3 the device can stay silent for
minutes without anything being broken.

Also tracks a small session statistics set (reconnects, longest gap, last
raw snapshot, last known standby state) for diagnostics.py - the
lightweight live counterpart to tools/ws_probe.py's Stats class from P0.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .vendor.pooldose_live.channels import decode_devicedata, detect_prefix
from .vendor.pooldose_live.mapping import MappingStatus, ModelMapping, ResolvedChannel
from .vendor.pooldose_live.mapping import load as load_mapping
from .vendor.pooldose_live.transport import PooldoseTransport, TransportEvent

from .const import DOMAIN, ISSUE_FW_FALLBACK, ISSUE_RAW_MODE, ISSUES_URL

_LOGGER = logging.getLogger(__name__)

type PooldoseLiveConfigEntry = ConfigEntry[PooldoseLiveCoordinator]


class PooldoseLiveCoordinator(DataUpdateCoordinator[dict[str, ResolvedChannel]]):
    """Holds the most recently resolved channel state, updated via WS push."""

    config_entry: PooldoseLiveConfigEntry

    def __init__(self, hass: HomeAssistant, config_entry: PooldoseLiveConfigEntry) -> None:
        super().__init__(
            hass, _LOGGER, name=DOMAIN, config_entry=config_entry, update_interval=None
        )
        self.host: str = config_entry.data["host"]
        self.transport = PooldoseTransport(self.host)
        self.mapping: ModelMapping | None = None
        self.is_stale = False
        self.last_known_standby: bool | None = None
        self._last_display: dict[str, object] | None = None
        self.data = {}

        # Diagnostics (diagnostics.py) - only for the running session, not a
        # replacement for a real recording like in P0.
        self.session_start = time.monotonic()
        self.stats = {"connects": 0, "disconnects": 0, "watchdog_trips": 0, "cycles": 0}
        self.last_snapshot_raw: dict[str, Any] | None = None
        self.last_snapshot_device_id: str | None = None
        self.last_snapshot_time: float | None = None
        self.longest_gap: float = 0.0

    def start(self) -> None:
        """Starts the background task, tied to the entry's lifetime."""
        self.config_entry.async_create_background_task(
            self.hass, self._run(), name=f"{DOMAIN}_{self.host}_transport"
        )

    async def _run(self) -> None:
        async for event in self.transport.events():
            self._handle_event(event)

    @callback
    def _handle_event(self, event: TransportEvent) -> None:
        if event.kind == "snapshot":
            self._handle_snapshot(event)
        elif event.kind == "stale":
            if not self.is_stale:
                self.is_stale = True
                self.async_update_listeners()
        elif event.kind == "fresh":
            if self.is_stale:
                self.is_stale = False
                self.async_update_listeners()
        elif event.kind == "connected":
            self.stats["connects"] += 1
        elif event.kind == "disconnected":
            self.stats["disconnects"] += 1
        elif event.kind == "watchdog":
            self.stats["watchdog_trips"] += 1
        if event.kind in ("connected", "disconnected", "watchdog"):
            _LOGGER.debug(
                "%s: %s%s", self.host, event.kind, f" ({event.reason})" if event.reason else ""
            )

    def _handle_snapshot(self, event: TransportEvent) -> None:
        now = time.monotonic()
        if self.last_snapshot_time is not None:
            self.longest_gap = max(self.longest_gap, now - self.last_snapshot_time)
        self.last_snapshot_time = now
        self.stats["cycles"] += 1
        self.last_snapshot_raw = event.devicedata
        self.last_snapshot_device_id = event.device_id

        channels = decode_devicedata(event.devicedata or {})

        if self.mapping is None:
            prefix = detect_prefix(event.devicedata or {})
            if prefix is None:
                return
            self.mapping = load_mapping(*prefix)
            self._report_mapping_status()

        resolved = self.mapping.resolve_all(channels)
        # visible=false doesn't create an entity (concept B3) - filtered
        # here so sensor.py/binary_sensor.py don't have to deal with it
        # themselves.
        by_name = {rc.name: rc for rc in resolved if rc.channel.visible}

        # Carry the standby context (concept §8.4) along independent of the
        # change filter below - it should stay current even if nothing else
        # changed.
        standby = by_name.get("alarm_system_standby")
        if standby is not None:
            self.last_known_standby = bool(standby.display)

        # Minimal change filter: only write if at least one displayed value
        # changed. The full debouncing with resolution thresholds and a
        # heartbeat (concept §5.6) runs additionally per entity (see
        # entity.py) - this here only prevents the trivial case of "a
        # complete cycle with no change at all", which per §8.2 is the
        # clear majority of cycles.
        display = {name: rc.display for name, rc in by_name.items()}
        if display == self._last_display:
            return
        self._last_display = display

        self.async_set_updated_data(by_name)

    def _report_mapping_status(self) -> None:
        """Repair issue on FW fallback/raw mode (concept §5.5/§5.9).

        Both cases are functional (raw mode delivers generically named
        channels instead of a total outage, the concept's core idea against
        B2) - but the user should visibly find out that name resolution
        isn't optimal, and how they can contribute to better coverage
        (the same path as issue #20 at lmaertin/python-pooldose).
        """
        mapping = self.mapping
        assert mapping is not None
        if mapping.status == MappingStatus.FW_FALLBACK:
            ir.async_create_issue(
                self.hass, DOMAIN, f"{ISSUE_FW_FALLBACK}_{self.host}",
                is_fixable=False, severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_FW_FALLBACK,
                translation_placeholders={
                    "model": mapping.model_id,
                    "fw_code": mapping.fw_code.removeprefix("FW"),
                    "used_fw": mapping.matched_fw or "?",
                },
                learn_more_url=ISSUES_URL,
            )
        elif mapping.status == MappingStatus.RAW:
            ir.async_create_issue(
                self.hass, DOMAIN, f"{ISSUE_RAW_MODE}_{self.host}",
                is_fixable=False, severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_RAW_MODE,
                translation_placeholders={
                    "model": mapping.model_id,
                    "fw_code": mapping.fw_code.removeprefix("FW"),
                },
                learn_more_url=ISSUES_URL,
            )

    def cleanup_issues(self) -> None:
        """Removes repair issues, e.g. when the config entry is unloaded."""
        ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_FW_FALLBACK}_{self.host}")
        ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_RAW_MODE}_{self.host}")
