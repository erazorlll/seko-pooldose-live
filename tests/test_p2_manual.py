"""Manual behavioral tests for the pooldose_live HA integration (P2).

Not a regular pytest run: `pytest-homeassistant-custom-component` pulls in
`homeassistant.runner`, which imports `fcntl` - a Unix-only module that
doesn't exist on Windows (this repo's development environment). The full
pytest fixtures (`hass`, `enable_custom_integrations`, ...) therefore
aren't usable here.

Instead: a bare `HomeAssistant()` core object (that does NOT need `runner`)
plus `MockConfigEntry` from the same package (imports independently of
`runner`) - covers the actually risky logic (coordinator snapshot
processing, staleness transitions, dynamic entity addition) without
needing HA's full event loop/component loader machinery.

On a real (Linux) HA instance this should be replaced/supplemented by real
pytest tests with the full fixtures - documented here deliberately as a
stopgap, not a replacement for that.

Run: python tests/test_p2_manual.py
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

# A real value-object snapshot as it would arrive after reassembly from the
# WS transport layer (devicedata[<serial>_DEVICE]) - excerpt from a real
# recording (recordings/session_20260812_104627.jsonl.gz).
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
    "PDPR1H04AW100_FW539292_w_1eo03t46k": {  # cl, not connected (B3)
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

    # --- Test 1: process a snapshot --------------------------------------
    assert coordinator.data == {}, "Coordinator should be empty before the first snapshot"

    coordinator._handle_event(TransportEvent(
        kind="snapshot", t=1.0, device_id="TESTSERIAL_DEVICE", devicedata=SAMPLE_DEVICEDATA,
    ))

    assert coordinator.mapping is not None, "Mapping should have been derived from the keys"
    assert coordinator.mapping.model_id == "PDPR1H04AW100"
    assert "ph" in coordinator.data, f"'ph' missing in {list(coordinator.data)}"
    assert coordinator.data["ph"].display == 7.1
    assert coordinator.data["orp"].display == 850
    assert coordinator.data["orp"].channel.alarm is True

    # B3: visible=false must not create an entity
    assert "cl" not in coordinator.data, "visible=false channel should have been filtered (B3)"

    # bare bool -> switch, decoded correctly
    assert coordinator.data["pause_dosing"].display is False
    print("Test 1 (snapshot processing, B3 filter) OK")

    # --- Test 2: change gate - identical snapshot triggers no update -----
    updates_before = coordinator.data
    coordinator._handle_event(TransportEvent(
        kind="snapshot", t=5.2, device_id="TESTSERIAL_DEVICE", devicedata=SAMPLE_DEVICEDATA,
    ))
    assert coordinator.data is updates_before, (
        "An identical snapshot should NOT have triggered async_set_updated_data"
    )
    print("Test 2 (change gate suppresses duplicates) OK")

    # --- Test 3: a real change is detected --------------------------------
    changed = {**SAMPLE_DEVICEDATA}
    changed["PDPR1H04AW100_FW539292_w_1ekeigkin"] = {
        **SAMPLE_DEVICEDATA["PDPR1H04AW100_FW539292_w_1ekeigkin"], "current": 7.3,
    }
    coordinator._handle_event(TransportEvent(
        kind="snapshot", t=9.4, device_id="TESTSERIAL_DEVICE", devicedata=changed,
    ))
    assert coordinator.data["ph"].display == 7.3
    print("Test 3 (a real change passes through) OK")

    # --- Test 4: staleness transitions -------------------------------------
    assert coordinator.is_stale is False
    coordinator._handle_event(TransportEvent(kind="stale", t=100.0, since=95.0))
    assert coordinator.is_stale is True
    coordinator._handle_event(TransportEvent(kind="fresh", t=101.0))
    assert coordinator.is_stale is False
    print("Test 4 (staleness transitions) OK")

    # --- Test 5: dynamic entity addition (mirrors sensor.py's logic) -----
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
    assert add_calls, "The first call should have added 'ph' (type sensor)"
    assert "ph" in add_calls[0]
    fake_add_new()
    assert len(add_calls) == 1, "The second call with no new channels should have done nothing"
    print("Test 5 (dynamic entity addition, no duplicates) OK")

    print("\nAll P2 behavioral tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
