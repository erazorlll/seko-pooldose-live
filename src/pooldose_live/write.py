"""Write access over the HTTP API (concept §5.1, §5.6).

v1 deliberately only writes through the known HTTP API (`setInstantValues`),
not over WebSocket - the WS write format is unexplored and stays that way
until P7, and the spec explicitly warns: "A malformed frame can change
parameters of a real dosing system" (websocker-spec.md).

NO pre-emptive GET before every write (concept B8): the reference library
fetches a complete snapshot over HTTP before every `set_*`, which needlessly
doubles every write. We already have the current channel state live from
the WS stream - validation (range/type/options) runs against THAT state.
The write action gets confirmed by the next WS tick (~4s), not by an
optimistic local assignment like the reference integration does (there, up
to 10 minutes of a wrong displayed value is possible if the device
disagrees).
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
    """Invalid write attempt - range/type/option not allowed.

    Checked LOCALLY, BEFORE a request goes out: a malformed frame can, per
    websocker-spec.md, change parameters of a real dosing system - client-
    side validation is meant to prevent that as best it can, even though
    the device itself, as far as we currently know, performs no validation
    of its own (unauthenticated, see the spec).
    """


def encode_number(channel: Channel, value: Any) -> float | int:
    """Validates against the current channel's absMin/absMax/resolution."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise WriteError(f"{value!r} is not a number")
    lo, hi, step = channel.abs_min, channel.abs_max, channel.resolution
    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and not (lo <= value <= hi):
        raise WriteError(f"{value} is outside the valid range [{lo}, {hi}]")
    if isinstance(step, (int, float)) and step > 0:
        base = lo if isinstance(lo, (int, float)) else 0
        steps = (value - base) / step
        if abs(round(steps) - steps) > _EPSILON:
            raise WriteError(f"{value} doesn't match the step size {step}")
    return value


def encode_switch(value: Any) -> str:
    if not isinstance(value, bool):
        raise WriteError(f"{value!r} is not a bool")
    return "O" if value else "F"


def encode_select(channel: Channel, value: Any) -> str:
    """Reverse lookup from the displayed text to the raw index, over the
    comboitems decoded generically by the channel itself (channels.py) -
    also works in raw mode without a mapping table, because
    `channel.options` comes directly from the most recently received
    snapshot, not from a separate table."""
    options = channel.options or {}
    for index, label in options.items():
        if label == value:
            return index
    valid = sorted(options.values())
    raise WriteError(f"{value!r} is not a valid option. Allowed: {valid}")


def _encode(entry_type: str, channel: Channel, value: Any) -> tuple[Any, str]:
    if entry_type == VALUE_TYPE_NUMBER:
        return encode_number(channel, value), "NUMBER"
    if entry_type == VALUE_TYPE_SWITCH:
        return encode_switch(value), "STRING"
    if entry_type == VALUE_TYPE_SELECT:
        return encode_select(channel, value), "NUMBER"
    raise WriteError(f"Channel type '{entry_type}' is not writable")


def build_prefix(mapping: ModelMapping) -> str:
    """Model_FW prefix from a resolved mapping instance.

    `matched_fw` is always set without the "FW" prefix for EXACT/FW_FALLBACK
    (see ModelMapping); in raw mode (matched_fw=None), the originally
    detected fw_code is used - there was no substitution there.
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
    """Writes a value via POST /api/v1/DWI/setInstantValues.

    Raises `WriteError` for invalid input (checked locally, no request
    sent), `aiohttp.ClientError`/`asyncio.TimeoutError` for network
    problems - deliberately not collapsed into one another, the caller
    should know the difference between "rejected" and "device unreachable".
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
