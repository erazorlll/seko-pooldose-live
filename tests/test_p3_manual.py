"""Manuelle Verhaltenstests für P3 (Entprellung, Verfügbarkeit, Diagnostics).

Gleiches Vorgehen wie test_p2_manual.py: kein regulärer pytest-Lauf möglich
(pytest-homeassistant-custom-component -> homeassistant.runner -> fcntl,
Unix-only). Bare HomeAssistant()-Core-Objekt + MockConfigEntry, echte
pooldose_live/HA-Klassen, `async_write_ha_state` wird für die Entity-Tests
gestubbt (wir testen unsere eigene Entscheidungslogik, nicht HAs
State-Machine-Schreibpfad - der ist bereits durch HA selbst getestet).

Ausführen: python tests/test_p3_manual.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityDescription
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pooldose_live.const import DOMAIN
from custom_components.pooldose_live.coordinator import PooldoseLiveCoordinator
from custom_components.pooldose_live.entity import (
    ALWAYS_AVAILABLE_WHEN_STALE,
    HEARTBEAT_INTERVAL,
    PooldoseLiveEntity,
    _is_relevant_change,
)
from pooldose_live.transport import TransportEvent

PH_DATA = {
    "PDPR1H04AW100_FW539292_w_1ekeigkin": {  # ph
        "visible": True, "alarm": False, "current": 7.1, "resolution": 0.1,
        "magnitude": ["pH", "PH"], "absMin": 0, "absMax": 14,
    },
    "PDPR1H04AW100_FW539292_w_1fai1n09b": {  # alarm_system_standby
        "visible": True, "current": "F",
    },
}


def test_is_relevant_change() -> None:
    assert _is_relevant_change(None, 7.1, 0.1) is True, "erster Wert ist immer relevant"
    assert _is_relevant_change(7.1, 7.15, 0.1) is False, "unter Aufloesung -> nicht relevant"
    assert _is_relevant_change(7.1, 7.3, 0.1) is True, "ueber Aufloesung -> relevant"
    assert _is_relevant_change(True, False, None) is True, "bool-Wechsel immer relevant"
    assert _is_relevant_change("a", "a", None) is False, "identischer String -> nicht relevant"
    assert _is_relevant_change("a", "b", None) is True, "anderer String -> relevant"
    print("Test is_relevant_change OK")


async def main() -> None:
    test_is_relevant_change()

    hass = HomeAssistant(str(Path.cwd()))
    hass.config.config_dir = str(Path.cwd())
    hass.config_entries = ConfigEntries(hass, {})

    entry = MockConfigEntry(domain=DOMAIN, data={"host": "192.168.0.74"},
                            unique_id="192.168.0.74")
    entry.add_to_hass(hass)

    coordinator = PooldoseLiveCoordinator(hass, entry)
    entry.runtime_data = coordinator

    coordinator._handle_event(TransportEvent(
        kind="snapshot", t=1.0, device_id="TESTSERIAL_DEVICE", devicedata=PH_DATA,
    ))
    assert "ph" in coordinator.data

    # --- Test: Diagnostics-Statistik -------------------------------------
    coordinator._handle_event(TransportEvent(kind="connected", t=0.0))
    coordinator._handle_event(TransportEvent(kind="watchdog", t=2.0, reason="test"))
    assert coordinator.stats["cycles"] == 1
    assert coordinator.stats["connects"] == 1
    assert coordinator.stats["watchdog_trips"] == 1
    assert coordinator.last_snapshot_device_id == "TESTSERIAL_DEVICE"
    assert coordinator.last_snapshot_raw is PH_DATA
    assert coordinator.last_known_standby is False, "alarm_system_standby=F -> False"
    print("Test Diagnostics-Statistik OK")

    # --- Test: Entity-Entprellung ------------------------------------------
    entity = PooldoseLiveEntity(coordinator, "ph", EntityDescription(key="ph"))
    write_calls: list[int] = []
    entity.async_write_ha_state = lambda: write_calls.append(1)  # type: ignore[method-assign]

    entity._handle_coordinator_update()
    assert len(write_calls) == 1, "erstes Update (Heartbeat=faellig) haette schreiben muessen"

    # Identischer Wert, kein Heartbeat faellig -> kein Schreiben
    entity._handle_coordinator_update()
    assert len(write_calls) == 1, "unveraenderter Wert haette NICHT schreiben duerfen"

    # Aenderung unterhalb der Aufloesung (0.1) -> kein Schreiben
    coordinator.data["ph"].display = 7.12
    entity._handle_coordinator_update()
    assert len(write_calls) == 1, "Aenderung unter Aufloesung haette NICHT schreiben duerfen"

    # Aenderung oberhalb der Aufloesung -> Schreiben
    coordinator.data["ph"].display = 7.4
    entity._handle_coordinator_update()
    assert len(write_calls) == 2, "Aenderung ueber Aufloesung haette schreiben muessen"

    # Heartbeat: Zeit zuruecksetzen, um faelligen Heartbeat zu simulieren
    entity._last_written_time -= HEARTBEAT_INTERVAL + 1
    entity._handle_coordinator_update()
    assert len(write_calls) == 3, "faelliger Heartbeat haette schreiben muessen"
    print("Test Entity-Entprellung (Aufloesung + Heartbeat) OK")

    # --- Test: Verfuegbarkeit + Standby-Ausnahme ----------------------------
    ph_entity = entity
    standby_entity = PooldoseLiveEntity(coordinator, "alarm_system_standby",
                                        EntityDescription(key="alarm_system_standby"))
    assert "alarm_system_standby" in ALWAYS_AVAILABLE_WHEN_STALE

    assert ph_entity.available is True
    assert standby_entity.available is True

    coordinator.is_stale = True
    assert ph_entity.available is False, "normaler Kanal muss bei Staleness unavailable werden"
    assert standby_entity.available is True, (
        "alarm_system_standby soll auch bei Staleness verfuegbar bleiben (Konzept §8.4)"
    )
    coordinator.is_stale = False
    print("Test Verfuegbarkeit + Standby-Ausnahme OK")

    print("\nAlle P3-Verhaltenstests bestanden.")


if __name__ == "__main__":
    asyncio.run(main())
