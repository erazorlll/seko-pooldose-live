"""Manuelle Verhaltenstests für P5 (Repair-Issues bei FW-Fallback/Raw-Modus).

Gleiches Vorgehen wie test_p2/p3/p4_manual.py (siehe dort für die
Begründung: pytest-homeassistant-custom-component blockiert unter Windows
an fcntl).

Ausführen: python tests/test_p5_manual.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pooldose_live.const import DOMAIN, ISSUE_FW_FALLBACK, ISSUE_RAW_MODE
from custom_components.pooldose_live.coordinator import PooldoseLiveCoordinator
from pooldose_live.transport import TransportEvent

# Bekanntes Modell/FW (exakter Mapping-Treffer)
EXACT_DATA = {
    "PDPR1H04AW100_FW539292_w_1ekeigkin": {"visible": True, "current": 7.1},
}
# Bekanntes Modell, unbekannte FW -> FW_FALLBACK
FALLBACK_DATA = {
    "PDPR1H04AW100_FW999999_w_1ekeigkin": {"visible": True, "current": 7.1},
}
# Unbekanntes Modell -> RAW
RAW_DATA = {
    "UNKNOWNMODEL_FW1_wtest": {"visible": True, "current": 1},
}


def _make_coordinator(hass: HomeAssistant, host: str) -> PooldoseLiveCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={"host": host}, unique_id=host)
    entry.add_to_hass(hass)
    coordinator = PooldoseLiveCoordinator(hass, entry)
    entry.runtime_data = coordinator
    return coordinator


async def main() -> None:
    hass = HomeAssistant(str(Path.cwd()))
    hass.config.config_dir = str(Path.cwd())
    hass.config_entries = ConfigEntries(hass, {})
    registry = ir.async_get(hass)

    # --- Test: EXACT -> kein Repair-Issue ----------------------------------
    c1 = _make_coordinator(hass, "10.0.0.1")
    c1._handle_event(TransportEvent(kind="snapshot", t=1.0, device_id="D1",
                                    devicedata=EXACT_DATA))
    assert registry.async_get_issue(DOMAIN, f"{ISSUE_FW_FALLBACK}_10.0.0.1") is None
    assert registry.async_get_issue(DOMAIN, f"{ISSUE_RAW_MODE}_10.0.0.1") is None
    print("Test EXACT -> kein Repair-Issue OK")

    # --- Test: FW_FALLBACK -> Repair-Issue mit korrekten Platzhaltern ------
    c2 = _make_coordinator(hass, "10.0.0.2")
    c2._handle_event(TransportEvent(kind="snapshot", t=1.0, device_id="D2",
                                    devicedata=FALLBACK_DATA))
    issue = registry.async_get_issue(DOMAIN, f"{ISSUE_FW_FALLBACK}_10.0.0.2")
    assert issue is not None, "FW_FALLBACK haette ein Repair-Issue erzeugen muessen"
    assert issue.translation_key == ISSUE_FW_FALLBACK
    assert issue.translation_placeholders == {
        "model": "PDPR1H04AW100", "fw_code": "999999", "used_fw": "539292",
    }, issue.translation_placeholders
    assert registry.async_get_issue(DOMAIN, f"{ISSUE_RAW_MODE}_10.0.0.2") is None
    print("Test FW_FALLBACK -> Repair-Issue mit korrekten Platzhaltern OK")

    # --- Test: RAW -> anderes Repair-Issue ---------------------------------
    c3 = _make_coordinator(hass, "10.0.0.3")
    c3._handle_event(TransportEvent(kind="snapshot", t=1.0, device_id="D3",
                                    devicedata=RAW_DATA))
    issue = registry.async_get_issue(DOMAIN, f"{ISSUE_RAW_MODE}_10.0.0.3")
    assert issue is not None, "RAW haette ein Repair-Issue erzeugen muessen"
    assert issue.translation_key == ISSUE_RAW_MODE
    assert issue.translation_placeholders == {"model": "UNKNOWNMODEL", "fw_code": "1"}
    assert registry.async_get_issue(DOMAIN, f"{ISSUE_FW_FALLBACK}_10.0.0.3") is None
    print("Test RAW -> Repair-Issue mit korrekten Platzhaltern OK")

    # --- Test: cleanup_issues raeumt auf -----------------------------------
    c2.cleanup_issues()
    assert registry.async_get_issue(DOMAIN, f"{ISSUE_FW_FALLBACK}_10.0.0.2") is None
    # Andere Coordinators/Issues bleiben unberuehrt
    assert registry.async_get_issue(DOMAIN, f"{ISSUE_RAW_MODE}_10.0.0.3") is not None
    print("Test cleanup_issues raeumt nur eigene Issues auf OK")

    print("\nAlle P5-Verhaltenstests bestanden.")


if __name__ == "__main__":
    asyncio.run(main())
