"""Config Flow für pooldose_live: Host eingeben, WS-Erreichbarkeit prüfen.

Bewusst KEIN Warten auf einen vollständigen instant_values-Zyklus während
der Einrichtung: Konzept §8.2/§8.3 haben gezeigt, dass das Gerät legitim
minutenlang (bis zu 9,7 Minuten gemessen) schweigen kann, während die
Verbindung intakt bleibt. Ein Setup-Schritt, der auf Daten wartet, würde bei
einem erheblichen Teil der Versuche grundlos fehlschlagen oder sehr lange
hängen - das haben wir beim P1-Test live selbst erlebt. Geprüft wird deshalb
nur, ob der WebSocket-Port den Handshake beantwortet; das genügt als
Existenznachweis für "hier läuft eine PoolDose".
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
    """Prüft nur, ob ws://<host>:1334/ den WebSocket-Handshake beantwortet.

    Sendet nichts über den Handshake hinaus und wartet nicht auf Daten -
    reines Verbindungs-Ja/Nein.
    """
    session = async_get_clientsession(hass)
    url = f"ws://{host}:1334/"
    try:
        async with asyncio.timeout(CONNECT_TIMEOUT):
            async with session.ws_connect(url) as ws:
                await ws.close()
        return True
    except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as err:
        _LOGGER.debug("Verbindungstest zu %s fehlgeschlagen: %s", host, err)
        return False


class PooldoseLiveConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config Flow für die pooldose_live-Integration."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Einziger Schritt: Host eingeben, Erreichbarkeit prüfen."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST]
            if await _check_reachable(self.hass, host):
                # Vorläufig: Host als unique_id. Die Seriennummer steht erst
                # nach dem ersten instant_values-Zyklus fest (Teil des
                # devicedata-Keys, siehe Konzept §4/§5.7) - der hier bewusst
                # nicht abgewartet wird. Migration auf die Seriennummer als
                # stabile unique_id ist ein bekannter, offener Folgeschritt,
                # kein P2-Scope.
                await self.async_set_unique_id(host)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"PoolDose ({host})", data={CONF_HOST: host}
                )
            errors["base"] = "cannot_connect"

        return self.async_show_form(step_id="user", data_schema=SCHEMA, errors=errors)
