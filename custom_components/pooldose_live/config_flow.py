"""Config flow for pooldose_live: enter a host, check WS reachability.

Deliberately does NOT wait for a complete instant_values cycle during
setup: concept §8.2/§8.3 showed that the device can legitimately stay
silent for minutes at a time (up to 9.7 minutes measured) while the
connection stays intact. A setup step that waits for data would fail for
no reason on a significant fraction of attempts, or hang for a long time -
we experienced that ourselves live during the P1 test. So all that's
checked is whether the WebSocket port answers the handshake; that's enough
proof of existence for "there's a PoolDose here".
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CONNECT_TIMEOUT = 10.0

SCHEMA = vol.Schema({vol.Required(CONF_HOST): cv.string})


async def _check_reachable(hass: Any, host: str) -> bool:
    """Only checks whether ws://<host>:1334/ answers the WebSocket handshake.

    Sends nothing beyond the handshake and doesn't wait for data - a plain
    yes/no on connectivity.
    """
    session = async_get_clientsession(hass)
    url = f"ws://{host}:1334/"
    try:
        async with asyncio.timeout(CONNECT_TIMEOUT):
            async with session.ws_connect(url) as ws:
                await ws.close()
        return True
    except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as err:
        _LOGGER.debug("Connectivity test to %s failed: %s", host, err)
        return False


class PooldoseLiveConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config flow for the pooldose_live integration."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Single step: enter a host, check reachability."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST]
            if await _check_reachable(self.hass, host):
                # Provisional: host as the unique_id. The serial number is
                # only known after the first instant_values cycle (part of
                # the devicedata key, see concept §4/§5.7), which is
                # deliberately not waited for here. Migrating to the serial
                # number as a stable unique_id is a known, open follow-up,
                # not P2 scope.
                await self.async_set_unique_id(host)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"PoolDose ({host})", data={CONF_HOST: host}
                )
            errors["base"] = "cannot_connect"

        return self.async_show_form(step_id="user", data_schema=SCHEMA, errors=errors)
