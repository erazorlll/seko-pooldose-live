"""Schreibzugriff über die HTTP-API (Konzept §5.1, §5.6).

v1 schreibt bewusst nur über die bekannte HTTP-API (`setInstantValues`), nicht
über WebSocket - das WS-Schreibformat ist unerforscht und bleibt es bis P7,
und die Spec warnt ausdrücklich: "Ein falsch geformter Frame kann Parameter
einer realen Dosieranlage verstellen" (websocker-spec.md).

KEIN Vorab-GET vor jedem Schreibvorgang (Konzept B8): die Referenzbibliothek
holt vor jedem `set_*` einen kompletten Snapshot per HTTP, das verdoppelt
jeden Schreibzugriff unnötig. Wir haben den aktuellen Kanal-Zustand über den
WS-Stream ohnehin schon live - Validierung (Bereich/Typ/Optionen) läuft
gegen DIESEN Zustand. Bestätigt wird die Schreibaktion durch den nächsten
WS-Tick (~4s), nicht durch eine optimistische lokale Zuweisung wie bei der
Referenzintegration (dort bis zu 10 Minuten falscher Anzeigewert möglich,
wenn das Gerät abweicht).
"""

from __future__ import annotations

from typing import Any

import aiohttp

from .channels import Channel
from .mapping import (
    VALUE_TYPE_NUMBER,
    VALUE_TYPE_SELECT,
    VALUE_TYPE_SWITCH,
    ModelMapping,
)

_EPSILON = 1e-6


class WriteError(ValueError):
    """Ungültiger Schreibversuch - Bereich/Typ/Option nicht erlaubt.

    Wird LOKAL geprüft, BEVOR ein Request rausgeht: ein falsch geformter
    Frame kann laut websocker-spec.md Parameter einer echten Dosieranlage
    verstellen, das soll clientseitige Validierung so gut wie möglich
    verhindern - auch wenn das Gerät selbst nach aktuellem Kenntnisstand
    keine eigene Prüfung durchführt (unauthentifiziert, siehe Spec).
    """


def encode_number(channel: Channel, value: Any) -> float | int:
    """Validiert gegen absMin/absMax/resolution des aktuellen Kanal-Stands."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise WriteError(f"{value!r} ist keine Zahl")
    lo, hi, step = channel.abs_min, channel.abs_max, channel.resolution
    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and not (lo <= value <= hi):
        raise WriteError(f"{value} liegt außerhalb des gültigen Bereichs [{lo}, {hi}]")
    if isinstance(step, (int, float)) and step > 0:
        base = lo if isinstance(lo, (int, float)) else 0
        steps = (value - base) / step
        if abs(round(steps) - steps) > _EPSILON:
            raise WriteError(f"{value} passt nicht zur Schrittweite {step}")
    return value


def encode_switch(value: Any) -> str:
    if not isinstance(value, bool):
        raise WriteError(f"{value!r} ist kein bool")
    return "O" if value else "F"


def encode_select(channel: Channel, value: Any) -> str:
    """Reverse-Lookup Anzeigetext -> Roh-Index über die vom Kanal selbst
    generisch dekodierten comboitems (channels.py) - funktioniert auch im
    Raw-Modus ohne Mapping-Tabelle, weil `channel.options` direkt aus dem
    zuletzt empfangenen Snapshot kommt, nicht aus einer separaten Tabelle."""
    options = channel.options or {}
    for index, label in options.items():
        if label == value:
            return index
    valid = sorted(options.values())
    raise WriteError(f"{value!r} ist keine gültige Option. Erlaubt: {valid}")


def _encode(entry_type: str, channel: Channel, value: Any) -> tuple[Any, str]:
    if entry_type == VALUE_TYPE_NUMBER:
        return encode_number(channel, value), "NUMBER"
    if entry_type == VALUE_TYPE_SWITCH:
        return encode_switch(value), "STRING"
    if entry_type == VALUE_TYPE_SELECT:
        return encode_select(channel, value), "NUMBER"
    raise WriteError(f"Kanaltyp '{entry_type}' ist nicht schreibbar")


def build_prefix(mapping: ModelMapping) -> str:
    """Modell_FW-Präfix aus einer aufgelösten Mapping-Instanz.

    `matched_fw` ist bei EXACT/FW_FALLBACK immer ohne "FW"-Präfix gesetzt
    (siehe ModelMapping); im Raw-Modus (matched_fw=None) wird der
    ursprünglich erkannte fw_code verwendet - es gab dort keine Ersetzung.
    """
    fw = mapping.matched_fw or mapping.fw_code.removeprefix("FW")
    return f"{mapping.model_id}_FW{fw}_"


async def set_channel(
    session: aiohttp.ClientSession,
    host: str,
    device_id: str,
    mapping: ModelMapping,
    channel: Channel,
    entry_type: str,
    value: Any,
    *,
    port: int = 80,
    use_ssl: bool = False,
    timeout: float = 15.0,
) -> None:
    """Schreibt einen Wert über POST /api/v1/DWI/setInstantValues.

    Wirft `WriteError` bei ungültiger Eingabe (lokal geprüft, kein Request),
    `aiohttp.ClientError`/`asyncio.TimeoutError` bei Netzwerkproblemen -
    beides bewusst nicht ineinander verschluckt, der Aufrufer soll den
    Unterschied zwischen "abgelehnt" und "Gerät nicht erreichbar" kennen.
    """
    raw_value, value_type = _encode(entry_type, channel, value)

    full_key = f"{build_prefix(mapping)}{channel.hash}"
    payload = {device_id: {full_key: [{"value": raw_value, "type": value_type}]}}

    scheme = "https" if use_ssl else "http"
    url = f"{scheme}://{host}:{port}/api/v1/DWI/setInstantValues"
    async with session.post(
        url, json=payload, headers={"Content-Type": "application/json"},
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as resp:
        resp.raise_for_status()
