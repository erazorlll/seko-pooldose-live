"""Decoder: raw devicedata dict -> dict[hash, Channel].

Pure transformation, no network, no mapping file needed. Covers the
structure documented in websocker-spec.md: value objects with
current/set/absMin/absMax/minT/maxT/alarm/visible/magnitude/resolution/
comboitems, plus bare booleans, plus label strings wrapped in pipes
(`|MODEL_FW_LABEL_hash_TEXT|` or `..._COMBO_...` for comboitems).

The prefix (model + FW code) is derived from the keys themselves - the same
regex that `DeviceAnalyzer._extract_device_info` uses in python-pooldose, no
HTTP call needed (see concept §4, "Confirmed").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Fields that aren't actual channels.
_SKIP_KEYS = {"deviceInfo", "collapsed_bar"}

# Model_FWCode_ prefix + the rest as the key. Same basic idea as
# DeviceAnalyzer._extract_device_info in python-pooldose.
_PREFIX_RE = re.compile(r"^([A-Z0-9]+)_(FW[A-Z0-9]+)_(.+)$")

# Units that aren't a real display unit.
_NO_UNIT = {"undefined", "ph"}
_CL2_ALIASES = {"cl2", "chlorine"}


@dataclass
class Channel:
    """A decoded channel - one row from the devicedata dict."""

    hash: str
    """Short key without the model/FW prefix, e.g. 'w_1ekeigkin'."""

    current: Any = None
    set: Any = None
    abs_min: Any = None
    abs_max: Any = None
    min_t: Any = None
    max_t: Any = None
    resolution: Any = None
    unit: str | None = None
    alarm: bool | None = None
    visible: bool = True
    options: dict[str, str] | None = None
    """Decoded comboitems: index -> plain-text label (COMBO prefix stripped)."""

    label: Any = None
    """`current`, with the LABEL prefix stripped if it was a pipe-wrapped
    label. Otherwise identical to `current`. Computed generically, without a
    mapping file - see `decode_label()`. Curated mapping tables can
    additionally translate `current` through their own `conversion` table."""

    raw: dict[str, Any] | bool | Any = field(default=None, repr=False)
    """Unmodified original value object (or bare bool), for debug/diagnostics."""

    @property
    def is_value_object(self) -> bool:
        """False for bare booleans (see websocker-spec.md, 'bare boolean')."""
        return isinstance(self.raw, dict)


def split_prefix(key: str) -> tuple[str, str, str] | None:
    """Splits a raw key into (model, fw_code, hash). None on no match."""
    m = _PREFIX_RE.match(key)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)


def decode_label(value: Any, model: str, fw: str, hash_key: str) -> Any:
    """Strips LABEL/COMBO prefixes generically, without a mapping table.

    Two forms occur: `current` on label fields is wrapped in pipes
    (`|MODEL_FW_LABEL_hash_TEXT|`), while entries inside `comboitems` are
    NOT (`MODEL_FW_COMBO_hash_TEXT`, no pipes) - verified against real
    device data. Both forms are handled here.

    Don't split from the right (examples like `_2_POINTS` or `__C` break
    that way, see websocker-spec.md) - instead strip the known prefix, as
    the spec prescribes.
    """
    if not isinstance(value, str):
        return value
    inner = value.strip("|") if value.startswith("|") else value
    for kind in ("LABEL", "COMBO"):
        prefix = f"{model}_{fw}_{kind}_{hash_key}_"
        if inner.startswith(prefix):
            return inner[len(prefix):].rstrip("_")
    # No recognizable prefix (different format, or not a label at all) -
    # return unchanged instead of guessing.
    return value


def _normalize_unit(magnitude: Any) -> str | None:
    if not isinstance(magnitude, (list, tuple)) or not magnitude:
        return None
    unit = magnitude[0]
    if not isinstance(unit, str) or unit.lower() in _NO_UNIT:
        return None
    if unit.lower() in _CL2_ALIASES:
        return "ppm"
    return unit


def _decode_comboitems(raw_items: Any, model: str, fw: str, hash_key: str) -> dict[str, str] | None:
    if not isinstance(raw_items, list):
        return None
    options: dict[str, str] = {}
    for entry in raw_items:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            continue
        index, labelkey = entry
        options[str(index)] = decode_label(labelkey, model, fw, hash_key)
    return options or None


def decode_value(raw_entry: Any, model: str, fw: str, hash_key: str) -> Channel:
    """Builds a Channel from a single raw value object (or bare bool)."""
    if not isinstance(raw_entry, dict):
        # "Some entries aren't an object, but a bare boolean"
        return Channel(hash=hash_key, current=raw_entry, label=raw_entry,
                       visible=True, raw=raw_entry)

    current = decode_label(raw_entry.get("current"), model, fw, hash_key)
    return Channel(
        hash=hash_key,
        current=raw_entry.get("current"),
        set=raw_entry.get("set"),
        abs_min=raw_entry.get("absMin"),
        abs_max=raw_entry.get("absMax"),
        min_t=raw_entry.get("minT"),
        max_t=raw_entry.get("maxT"),
        resolution=raw_entry.get("resolution"),
        unit=_normalize_unit(raw_entry.get("magnitude")),
        alarm=raw_entry.get("alarm"),
        visible=bool(raw_entry.get("visible", True)),
        options=_decode_comboitems(raw_entry.get("comboitems"), model, fw, hash_key),
        label=current,
        raw=raw_entry,
    )


def decode_devicedata(payload: dict[str, Any]) -> dict[str, Channel]:
    """Decodes the devicedata[<SERIAL>_DEVICE] dict of a single device.

    Expects the already-merged, complete dict from reassembly (both
    chunks). Keys without a recognizable model_FW prefix (e.g. future
    metadata fields we don't know about) are skipped instead of raising an
    exception - robust against unknown structure, as concept §5 requires.
    """
    channels: dict[str, Channel] = {}
    for key, raw_entry in payload.items():
        if key in _SKIP_KEYS:
            continue
        parsed = split_prefix(key)
        if parsed is None:
            continue
        model, fw, hash_key = parsed
        channels[hash_key] = decode_value(raw_entry, model, fw, hash_key)
    return channels


def detect_prefix(payload: dict[str, Any]) -> tuple[str, str] | None:
    """Derives (model, fw_code) from the first matching key in the payload."""
    for key in payload:
        if key in _SKIP_KEYS:
            continue
        parsed = split_prefix(key)
        if parsed is not None:
            return parsed[0], parsed[1]
    return None
