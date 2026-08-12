"""Manuelle Verhaltenstests für die pooldose_live-HA-Integration (P2).

Kein regulärer pytest-Testlauf: `pytest-homeassistant-custom-component`
zieht `homeassistant.runner` nach sich, das `fcntl` importiert - ein
Unix-only-Modul, das unter Windows nicht existiert (Entwicklungsumgebung
dieses Repos). Die vollen pytest-Fixtures (`hass`,
`enable_custom_integrations`, ...) sind hier also nicht nutzbar.

Stattdessen: ein bloßes `HomeAssistant()`-Core-Objekt (das braucht `runner`
NICHT) plus `MockConfigEntry` aus demselben Paket (importiert unabhängig von
`runner`) - deckt die eigentlich riskante Logik ab (Coordinator-
Snapshot-Verarbeitung, Staleness-Übergänge, dynamisches Entity-
Hinzufügen), ohne die volle Event-Loop/Component-Loader-Maschinerie von HA
selbst zu benötigen.

Auf einer echten (Linux-)HA-Instanz sollte das durch echte pytest-Tests mit
den vollen Fixtures ersetzt/ergänzt werden - hier bewusst als Notlösung
dokumentiert, nicht als Ersatz dafür.

Ausführen: python tests/test_p2_manual.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pooldose_live.const import DOMAIN
from custom_components.pooldose_live.coordinator import PooldoseLiveCoordinator
from pooldose_live.transport import TransportEvent

# Ein reales Wertobjekt-Snapshot, wie es nach der Reassembly aus der
# WS-Transportschicht käme (devicedata[<serial>_DEVICE]) - Auszug aus einem
# echten Mitschnitt (recordings/session_20260812_104627.jsonl.gz).
SAMPLE_DEVICEDATA = {
    "deviceInfo": {"dwi_status": "ok", "modbus_status": "on"},
    "collapsed_bar": [],
    "PDPR1H04AW100_FW539292_w_1ekeigkin": {  # ph
        "visible": True, "alarm": False, "current": 7.1, "resolution": 0.1,
        "magnitude": ["pH", "PH"], "absMin": 0, "absMax": 14,
    },
    "PDPR1H04AW100_FW539292_w_1eklenb23": {  # orp
        "visible": True, "alarm": True, "current": 850, "resolution": 1,
        "magnitude": ["mV", "MV"], "absMin": -99, "absMax": 999,
    },
    "PDPR1H04AW100_FW539292_w_1eo03t46k": {  # cl, nicht angeschlossen (B3)
        "visible": False, "alarm": True, "current": 0, "resolution": 0.1,
        "magnitude": ["ppm", "PPM"],
    },
    "PDPR1H04AW100_FW539292_w_1emtltkel": False,  # pause_dosing, bare bool
}


async def main() -> None:
    hass = HomeAssistant(str(Path.cwd()))
    hass.config.config_dir = str(Path.cwd())
    hass.config_entries = ConfigEntries(hass, {})

    entry = MockConfigEntry(domain=DOMAIN, data={"host": "192.168.0.74"},
                            unique_id="192.168.0.74")
    entry.add_to_hass(hass)

    coordinator = PooldoseLiveCoordinator(hass, entry)
    entry.runtime_data = coordinator

    # --- Test 1: Snapshot verarbeiten -----------------------------------
    assert coordinator.data == {}, "Coordinator sollte vor dem ersten Snapshot leer sein"

    coordinator._handle_event(TransportEvent(
        kind="snapshot", t=1.0, device_id="TESTSERIAL_DEVICE", devicedata=SAMPLE_DEVICEDATA,
    ))

    assert coordinator.mapping is not None, "Mapping sollte aus den Keys abgeleitet worden sein"
    assert coordinator.mapping.model_id == "PDPR1H04AW100"
    assert "ph" in coordinator.data, f"'ph' fehlt in {list(coordinator.data)}"
    assert coordinator.data["ph"].display == 7.1
    assert coordinator.data["orp"].display == 850
    assert coordinator.data["orp"].channel.alarm is True

    # B3: visible=false darf keine Entity erzeugen
    assert "cl" not in coordinator.data, "visible=false-Kanal haette gefiltert werden muessen (B3)"

    # bare bool -> switch, korrekt dekodiert
    assert coordinator.data["pause_dosing"].display is False
    print("Test 1 (Snapshot-Verarbeitung, B3-Filter) OK")

    # --- Test 2: Änderungs-Gate - identischer Snapshot löst kein Update aus
    updates_before = coordinator.data
    coordinator._handle_event(TransportEvent(
        kind="snapshot", t=5.2, device_id="TESTSERIAL_DEVICE", devicedata=SAMPLE_DEVICEDATA,
    ))
    assert coordinator.data is updates_before, (
        "Identischer Snapshot haette KEIN async_set_updated_data ausloesen duerfen"
    )
    print("Test 2 (Aenderungs-Gate unterdrueckt Duplikate) OK")

    # --- Test 3: Aenderung wird erkannt -----------------------------------
    changed = {**SAMPLE_DEVICEDATA}
    changed["PDPR1H04AW100_FW539292_w_1ekeigkin"] = {
        **SAMPLE_DEVICEDATA["PDPR1H04AW100_FW539292_w_1ekeigkin"], "current": 7.3,
    }
    coordinator._handle_event(TransportEvent(
        kind="snapshot", t=9.4, device_id="TESTSERIAL_DEVICE", devicedata=changed,
    ))
    assert coordinator.data["ph"].display == 7.3
    print("Test 3 (echte Aenderung wird durchgelassen) OK")

    # --- Test 4: Staleness-Uebergaenge -------------------------------------
    assert coordinator.is_stale is False
    coordinator._handle_event(TransportEvent(kind="stale", t=100.0, since=95.0))
    assert coordinator.is_stale is True
    coordinator._handle_event(TransportEvent(kind="fresh", t=101.0))
    assert coordinator.is_stale is False
    print("Test 4 (Staleness-Uebergaenge) OK")

    # --- Test 5: Dynamisches Entity-Hinzufuegen (sensor.py-Logik nachgebaut)
    added: set[str] = set()
    add_calls: list[list[str]] = []

    def fake_add_new() -> None:
        new_names = [
            name for name, rc in coordinator.data.items()
            if rc.type == "sensor" and name not in added
        ]
        if not new_names:
            return
        added.update(new_names)
        add_calls.append(sorted(new_names))

    fake_add_new()
    assert add_calls, "Erster Aufruf haette 'ph' (Typ sensor) hinzufuegen muessen"
    assert "ph" in add_calls[0]
    fake_add_new()
    assert len(add_calls) == 1, "Zweiter Aufruf ohne neue Kanaele haette nichts tun sollen"
    print("Test 5 (dynamisches Entity-Hinzufuegen, keine Duplikate) OK")

    print("\nAlle P2-Verhaltenstests bestanden.")


if __name__ == "__main__":
    asyncio.run(main())
