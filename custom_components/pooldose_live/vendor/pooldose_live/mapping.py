"""Mapping loader: hash -> readable name, three-tier fallback (concept §5.5).

1. Exact match: model_<MODEL>_FW<FW>.json
2. Same model, different firmware available -> load + warn
3. No mapping file for the model -> raw mode: guess name/type per channel
   from the structure of the value object (see `infer_raw_type`)

The mapping tables themselves are vendored from python-pooldose (MIT
license, see mappings/ATTRIBUTION.md) - this loader and all of the fallback
logic are our own code (rationale: concept §5.2).
"""

from __future__ import annotations

import importlib.resources
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .channels import Channel

# Model alias table: the PRODUCT_CODE some devices report differs from the
# model ID that actually shows up in the data keys/mapping filenames.
# Taken from python-pooldose/src/pooldose/constants.py (see ATTRIBUTION.md).
MODEL_ALIASES: dict[str, str] = {
    "PDHC1H1HAR1V1": "PDPR1H1HAR1V0",
    "PDHC1H1HAR1V0": "PDPR1H1HAR1V0",
    "PDPR1H1HAW102": "PDPR1H1HAW100",
}

VALUE_TYPE_SENSOR = "sensor"
VALUE_TYPE_NUMBER = "number"
VALUE_TYPE_SWITCH = "switch"
VALUE_TYPE_BINARY_SENSOR = "binary_sensor"
VALUE_TYPE_SELECT = "select"

_FILENAME_RE = re.compile(r"^model_([A-Z0-9]+)_FW([A-Z0-9]+)\.json$")
_TRUE_STRINGS = {"O"}
_FALSE_STRINGS = {"F"}


class MappingStatus(str, Enum):
    EXACT = "exact"
    FW_FALLBACK = "fw_fallback"
    RAW = "raw"


@dataclass
class ResolvedChannel:
    """A channel enriched with a name/type/display value."""

    name: str
    type: str
    channel: Channel
    display: Any
    source: str  # "mapping" | "raw"
    min: Any = None
    max: Any = None
    step: Any = None


def _mappings_package() -> str:
    """Package name for the `mappings/` resources, relative to wherever this
    module itself actually lives.

    Deliberately NOT the literal string "pooldose_live.mappings": that only
    resolves when `pooldose_live` is pip-installed as a top-level package
    (true for local dev/tests, via `pip install -e .`) - not when vendored
    into custom_components/pooldose_live/vendor/pooldose_live/, where this
    module's real package is `custom_components.pooldose_live.vendor.
    pooldose_live` and there is no top-level `pooldose_live` at all. A
    hardcoded absolute name silently works in every local test (the dev
    environment always has the standalone package installed too) and raises
    `ModuleNotFoundError` only in a real HACS install - see the incident
    that prompted this fix. `__package__` always reflects the actual
    importing context, so this resolves correctly either way.
    """
    return f"{__package__}.mappings"


def _list_mapping_files() -> list[str]:
    files = importlib.resources.files(_mappings_package())
    return [f.name for f in files.iterdir() if f.name.endswith(".json")]


def _load_json(filename: str) -> dict[str, Any]:
    path = importlib.resources.files(_mappings_package()).joinpath(filename)
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _closest_fw(available: list[str], wanted: str) -> str:
    """Picks the "closest" FW file of the same model.

    Best effort: numeric distance if both FW codes parse as numbers (e.g.
    FW539292 -> 539292); otherwise the first one in sorted order. With only
    one available file this is unambiguous anyway - the exact selection
    mechanism with multiple candidates is a detail that can only really be
    validated once we hit a real multi-FW case for the same model.
    """
    def as_int(fw: str) -> int | None:
        digits = fw[2:] if fw.upper().startswith("FW") else fw
        return int(digits) if digits.isdigit() else None

    wanted_n = as_int(wanted)
    if wanted_n is not None:
        with_dist = [(abs(as_int(fw) - wanted_n), fw) for fw in available if as_int(fw) is not None]
        if with_dist:
            with_dist.sort(key=lambda x: x[0])
            return with_dist[0][1]
    return sorted(available)[0]


class ModelMapping:
    """Resolved mapping table for a model/FW, inverted by hash.

    A hash can point to several named entries (e.g. minT/maxT variants of
    the same raw value via the "field" attribute) - hence hash -> list, not
    hash -> one entry.
    """

    def __init__(self, model_id: str, fw_code: str, status: MappingStatus,
                matched_fw: str | None, table: dict[str, dict[str, Any]] | None) -> None:
        self.model_id = model_id
        self.fw_code = fw_code  # as passed in, usually WITH the "FW" prefix (from channels.detect_prefix)
        self.status = status
        self.matched_fw = matched_fw  # WITHOUT the "FW" prefix, None only for RAW - consistent
        # between EXACT and FW_FALLBACK (e.g. "539292"), for directly building the prefix
        # via f"{model_id}_FW{matched_fw}_" when writing (see write.py)
        self.table = table or {}
        self._by_hash: dict[str, list[tuple[str, dict]]] = {}
        for name, entry in self.table.items():
            self._by_hash.setdefault(entry["key"], []).append((name, entry))

    def resolve_channel(self, channel: Channel) -> list[ResolvedChannel]:
        """Resolves a single channel. Empty list only for the raw fallback
        with an undeterminable type (practically never happens, see
        `infer_raw_type`)."""
        entries = self._by_hash.get(channel.hash)
        if entries:
            return [_resolve_named(name, entry, channel) for name, entry in entries]
        return [_resolve_raw(channel)]

    def resolve_all(self, channels: dict[str, Channel]) -> list[ResolvedChannel]:
        resolved: list[ResolvedChannel] = []
        for channel in channels.values():
            resolved.extend(self.resolve_channel(channel))
        return resolved

    @property
    def coverage(self) -> tuple[int, int]:
        """(number of hashes in the mapping, number of unique hashes) - for diagnostics."""
        return len(self.table), len(self._by_hash)


def load(model_id: str, fw_code: str) -> ModelMapping:
    """Loads the mapping table for (model_id, fw_code) with the three-tier fallback."""
    resolved_model = MODEL_ALIASES.get(model_id, model_id)

    exact_name = f"model_{resolved_model}_FW{fw_code.removeprefix('FW')}.json"
    available = _list_mapping_files()
    if exact_name in available:
        return ModelMapping(model_id, fw_code, MappingStatus.EXACT,
                            fw_code.removeprefix("FW"), _load_json(exact_name))

    same_model_fws: list[str] = []
    for fname in available:
        m = _FILENAME_RE.match(fname)
        if m and m.group(1) == resolved_model:
            same_model_fws.append(m.group(2))

    if same_model_fws:
        chosen_fw = _closest_fw(same_model_fws, fw_code.removeprefix("FW"))
        chosen_name = f"model_{resolved_model}_FW{chosen_fw}.json"
        return ModelMapping(model_id, fw_code, MappingStatus.FW_FALLBACK, chosen_fw,
                            _load_json(chosen_name))

    return ModelMapping(model_id, fw_code, MappingStatus.RAW, None, None)


def infer_raw_type(channel: Channel) -> str:
    """Guesses the entity type from the structure of the value object (concept §5.5).

    `set` present -> number; `comboitems` -> select; `current` is "O"/"F"
    or bool -> binary_sensor (deliberately not "switch" - in raw mode we
    don't know whether a channel is writable); otherwise sensor.
    """
    if channel.set is not None:
        return VALUE_TYPE_NUMBER
    if channel.options:
        return VALUE_TYPE_SELECT
    if isinstance(channel.current, bool):
        return VALUE_TYPE_BINARY_SENSOR
    if isinstance(channel.current, str) and channel.current in (_TRUE_STRINGS | _FALSE_STRINGS):
        return VALUE_TYPE_BINARY_SENSOR
    return VALUE_TYPE_SENSOR


def _of_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value in _TRUE_STRINGS:
            return True
        if value in _FALSE_STRINGS:
            return False
    return None


def _resolve_raw(channel: Channel) -> ResolvedChannel:
    entry_type = infer_raw_type(channel)
    name = f"raw_{channel.hash}"
    if entry_type == VALUE_TYPE_NUMBER:
        display = channel.current
    elif entry_type == VALUE_TYPE_SELECT:
        display = (channel.options or {}).get(str(channel.current), channel.current)
    elif entry_type == VALUE_TYPE_BINARY_SENSOR:
        display = _of_bool(channel.current)
    else:
        display = channel.label
    return ResolvedChannel(name=name, type=entry_type, channel=channel, display=display,
                           source="raw", min=channel.abs_min, max=channel.abs_max,
                           step=channel.resolution)


def _raw_current(channel: Channel) -> Any:
    """The unprocessed current value as it appeared in the payload (with
    pipes, if it's a label) - for matching against curated conversion
    tables, which use exactly this format as their key."""
    if isinstance(channel.raw, dict):
        return channel.raw.get("current")
    return channel.raw


def _resolve_named(name: str, entry: dict[str, Any], channel: Channel) -> ResolvedChannel:
    entry_type = entry.get("type", VALUE_TYPE_SENSOR)
    conversion = entry.get("conversion")
    field = entry.get("field")

    if entry_type == VALUE_TYPE_NUMBER:
        value = getattr(channel, field) if field in ("min_t", "max_t") else channel.current
        return ResolvedChannel(name=name, type=entry_type, channel=channel, display=value,
                               source="mapping", min=channel.abs_min, max=channel.abs_max,
                               step=channel.resolution)

    if entry_type == VALUE_TYPE_SWITCH:
        display = _of_bool(_raw_current(channel))
        return ResolvedChannel(name=name, type=entry_type, channel=channel, display=display,
                               source="mapping")

    if entry_type == VALUE_TYPE_SELECT:
        options = entry.get("options") or {}
        text = options.get(str(channel.current))
        if text is not None and conversion:
            text = conversion.get(text, text)
        return ResolvedChannel(name=name, type=entry_type, channel=channel,
                               display=text if text is not None else channel.current,
                               source="mapping")

    # sensor / binary_sensor: prefer a curated conversion (keyed by the
    # original raw value), otherwise fall back to the generically decoded
    # label.
    display = channel.label
    if conversion:
        raw_current = _raw_current(channel)
        if raw_current in conversion:
            display = conversion[raw_current]
    if entry_type == VALUE_TYPE_BINARY_SENSOR:
        bool_display = _of_bool(display) if not isinstance(display, bool) else display
        display = bool_display if bool_display is not None else display

    return ResolvedChannel(name=name, type=entry_type, channel=channel, display=display,
                           source="mapping")
