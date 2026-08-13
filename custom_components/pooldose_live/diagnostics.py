"""Diagnostics für pooldose_live (Konzept §5.8).

Liefert den letzten vollständigen Roh-Snapshot (Seriennummer redigiert) plus
eine kleine Sitzungsstatistik. Dasselbe Material, aus dem in Issue #20 von
lmaertin/python-pooldose ein community-beigesteuertes Mapping für ein
unbekanntes Gerät entstand - hier ohne CLI-Gefummel direkt aus HA exportierbar.

Nur die laufende Sitzung, kein Ersatz für einen echten Mitschnitt wie in P0
(tools/ws_probe.py) - für Ticks/Sekunde über Stunden hinweg bleibt das das
richtige Werkzeug.
"""

from __future__ import annotations

import time
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .coordinator import PooldoseLiveConfigEntry

TO_REDACT = {"device_id"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: PooldoseLiveConfigEntry
) -> dict[str, Any]:
    """Diagnostics für einen pooldose_live-Config-Entry."""
    coordinator = entry.runtime_data
    mapping = coordinator.mapping

    return {
        "host": coordinator.host,
        "mapping": {
            "status": mapping.status.value if mapping else None,
            "model_id": mapping.model_id if mapping else None,
            "fw_code": mapping.fw_code if mapping else None,
            "matched_fw": mapping.matched_fw if mapping else None,
            "coverage": mapping.coverage if mapping else None,
        },
        "session": {
            "uptime_s": round(time.monotonic() - coordinator.session_start, 1),
            "connects": coordinator.stats["connects"],
            "disconnects": coordinator.stats["disconnects"],
            "watchdog_trips": coordinator.stats["watchdog_trips"],
            "cycles": coordinator.stats["cycles"],
            "longest_gap_s": round(coordinator.longest_gap, 1),
            "is_stale": coordinator.is_stale,
            "last_known_standby": coordinator.last_known_standby,
        },
        "resolved_channels": {
            name: {
                "type": rc.type,
                "display": rc.display,
                "source": rc.source,
                "visible": rc.channel.visible,
                "alarm": rc.channel.alarm,
                "unit": rc.channel.unit,
            }
            for name, rc in coordinator.data.items()
        },
        "last_raw_snapshot": async_redact_data(
            {
                "device_id": coordinator.last_snapshot_device_id,
                "devicedata": coordinator.last_snapshot_raw,
            },
            TO_REDACT,
        ),
    }
