"""Manual behavioral tests for P4 (write access for number/select/switch).

Same approach as test_p2/p3_manual.py (see there for the rationale:
pytest-homeassistant-custom-component blocks on fcntl under Windows).

The HTTP layer is tested with a fake session - no real network access, and
certainly none against a real device (a write can, per websocker-spec.md,
change real dosing parameters, which has no place in an automated test run).

Run: python tests/test_p4_manual.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp

from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity import EntityDescription
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.pooldose_live.entity as entity_mod
from custom_components.pooldose_live.const import DOMAIN
from custom_components.pooldose_live.coordinator import PooldoseLiveCoordinator
from custom_components.pooldose_live.entity import PooldoseLiveEntity
from pooldose_live.transport import TransportEvent

DEVICE_DATA = {
    "PDPR1H04AW100_FW539292_w_1ekeiqfat": {  # ph_target: 6..8, step 0.1
        "visible": True, "current": 7.1, "set": 7.1, "absMin": 6, "absMax": 8,
        "resolution": 0.1, "magnitude": ["pH", "PH"],
    },
    "PDPR1H04AW100_FW539292_w_1eklinki6": {  # water_meter_unit (select)
        "visible": True, "current": "1",
        "comboitems": [[0, "PDPR1H04AW100_FW539292_COMBO_w_1eklinki6_M_"],
                       [1, "PDPR1H04AW100_FW539292_COMBO_w_1eklinki6_LITER"]],
    },
    "PDPR1H04AW100_FW539292_w_1emtltkel": False,  # pause_dosing (switch, bare bool)
}


class FakeResponse:
    def __init__(self, raise_exc: Exception | None = None) -> None:
        self._raise_exc = raise_exc

    async def __aenter__(self) -> "FakeResponse":
        if self._raise_exc:
            raise self._raise_exc
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def raise_for_status(self) -> None:
        pass


class FakeSession:
    def __init__(self, raise_exc: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._raise_exc = raise_exc

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append((url, kwargs))
        return FakeResponse(self._raise_exc)


async def main() -> None:
    hass = HomeAssistant(str(Path.cwd()))
    hass.config.config_dir = str(Path.cwd())
    hass.config_entries = ConfigEntries(hass, {})

    entry = MockConfigEntry(domain=DOMAIN, data={"host": "192.168.0.74"},
                            unique_id="192.168.0.74")
    entry.add_to_hass(hass)

    coordinator = PooldoseLiveCoordinator(hass, entry)
    entry.runtime_data = coordinator
    await coordinator._handle_event(TransportEvent(
        kind="snapshot", t=1.0, device_id="TESTSERIAL_DEVICE", devicedata=DEVICE_DATA,
    ))

    assert coordinator.data["ph_target"].type == "number"
    assert coordinator.data["water_meter_unit"].type == "select"
    assert coordinator.data["pause_dosing"].type == "switch"
    print("Test type detection (number/select/switch from real raw data) OK")

    # --- Test: dynamic addition (same pattern as number.py etc.) ---------
    for expected_type, name in [("number", "ph_target"), ("select", "water_meter_unit"),
                                ("switch", "pause_dosing")]:
        matches = [n for n, rc in coordinator.data.items() if rc.type == expected_type]
        assert name in matches, f"{name} should have been detected as {expected_type}"
    print("Test dynamic addition (all three new types) OK")

    # --- Test: successful write --------------------------------------------
    entity = PooldoseLiveEntity(coordinator, "ph_target", EntityDescription(key="ph_target"))
    fake_session = FakeSession()
    entity_mod.async_get_clientsession = lambda hass: fake_session  # type: ignore[assignment]

    await entity._async_write_value(7.4)
    assert len(fake_session.calls) == 1
    url, kwargs = fake_session.calls[0]
    assert url == "http://192.168.0.74:80/api/v1/DWI/setInstantValues"
    payload = kwargs["json"]
    assert payload == {
        "TESTSERIAL_DEVICE": {
            "PDPR1H04AW100_FW539292_w_1ekeiqfat": [{"value": 7.4, "type": "NUMBER"}]
        }
    }, payload
    print("Test successful write (correct payload) OK")

    # --- Test: invalid value -> ServiceValidationError, NO request ---------
    fake_session2 = FakeSession()
    entity_mod.async_get_clientsession = lambda hass: fake_session2  # type: ignore[assignment]
    try:
        await entity._async_write_value(99.0)  # outside [6, 8]
        raise AssertionError("should have raised ServiceValidationError")
    except ServiceValidationError:
        pass
    assert not fake_session2.calls, "an invalid value must NOT trigger a request"
    print("Test invalid value (no request, ServiceValidationError) OK")

    # --- Test: network error -> HomeAssistantError --------------------------
    fake_session3 = FakeSession(raise_exc=aiohttp.ClientConnectionError("nope"))
    entity_mod.async_get_clientsession = lambda hass: fake_session3  # type: ignore[assignment]
    try:
        await entity._async_write_value(7.2)
        raise AssertionError("should have raised HomeAssistantError")
    except HomeAssistantError as err:
        assert not isinstance(err, ServiceValidationError)
    print("Test network error (HomeAssistantError, not ServiceValidationError) OK")

    print("\nAll P4 behavioral tests passed.")


if __name__ == "__main__":
    asyncio.run(main())
