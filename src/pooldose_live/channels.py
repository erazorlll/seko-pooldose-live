"""Decoder: rohes devicedata-Dict -> dict[hash, Channel].

Reine Transformation, kein Netzwerk, keine Mapping-Datei nötig. Deckt die in
websocker-spec.md dokumentierte Struktur ab: Wertobjekte mit
current/set/absMin/absMax/minT/maxT/alarm/visible/magnitude/resolution/
comboitems, daneben bare Booleans, daneben Label-Strings in Pipes
(`|MODEL_FW_LABEL_hash_TEXT|` bzw. `..._COMBO_...` bei comboitems).

Der Präfix (Modell + FW-Code) wird aus den Keys selbst abgeleitet - dieselbe
Regex wie `DeviceAnalyzer._extract_device_info` in python-pooldose nutzt,
kein HTTP-Call nötig (siehe Konzept §4, "Bestätigt").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Felder, die nicht zu den eigentlichen Kanälen gehören.
_SKIP_KEYS = {"deviceInfo", "collapsed_bar"}

# Modell_FWCode_ Präfix + Rest als Key. Gleiche Grundidee wie
# DeviceAnalyzer._extract_device_info in python-pooldose.
_PREFIX_RE = re.compile(r"^([A-Z0-9]+)_(FW[A-Z0-9]+)_(.+)$")

# Einheiten, die keine echte Anzeigeeinheit sind.
_NO_UNIT = {"undefined", "ph"}
_CL2_ALIASES = {"cl2", "chlorine"}


@dataclass
class Channel:
    """Ein dekodierter Kanal - eine Zeile aus dem devicedata-Dict."""

    hash: str
    """Kurzer Schlüssel ohne Modell/FW-Präfix, z. B. 'w_1ekeigkin'."""

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
    """Aufgelöste comboitems: Index -> Klartext-Label (COMBO-Präfix entfernt)."""

    label: Any = None
    """`current`, falls es ein Pipe-Label war: mit entferntem LABEL-Präfix.
    Sonst identisch zu `current`. Generisch berechnet, ohne Mapping-Datei -
    siehe `decode_label()`. Kuratierte Mapping-Tabellen können `current`
    zusätzlich über ihre eigene `conversion`-Tabelle übersetzen."""

    raw: dict[str, Any] | bool | Any = field(default=None, repr=False)
    """Unverändertes Original-Wertobjekt (oder bare Bool), für Debug/Diagnostics."""

    @property
    def is_value_object(self) -> bool:
        """False bei bare Booleans (siehe websocker-spec.md, 'blanker Boolean')."""
        return isinstance(self.raw, dict)


def split_prefix(key: str) -> tuple[str, str, str] | None:
    """Zerlegt einen Roh-Key in (model, fw_code, hash). None bei Nicht-Treffer."""
    m = _PREFIX_RE.match(key)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)


def decode_label(value: Any, model: str, fw: str, hash_key: str) -> Any:
    """Entfernt LABEL-/COMBO-Präfixe generisch, ohne Mapping-Tabelle.

    Zwei Formen kommen vor: `current` bei Label-Feldern ist in Pipes
    eingeschlossen (`|MODEL_FW_LABEL_hash_TEXT|`), die Einträge innerhalb von
    `comboitems` dagegen NICHT (`MODEL_FW_COMBO_hash_TEXT`, ohne Pipes) - an
    echten Gerätedaten verifiziert. Beide Formen werden hier behandelt.

    Nicht von rechts splitten (Beispiele wie `_2_POINTS` oder `__C` brechen
    dabei, siehe websocker-spec.md) - stattdessen den bekannten Präfix
    entfernen, wie die Spec es vorschreibt.
    """
    if not isinstance(value, str):
        return value
    inner = value.strip("|") if value.startswith("|") else value
    for kind in ("LABEL", "COMBO"):
        prefix = f"{model}_{fw}_{kind}_{hash_key}_"
        if inner.startswith(prefix):
            return inner[len(prefix):].rstrip("_")
    # Kein erkennbarer Präfix (anderes Format oder gar kein Label) -
    # unverändert zurückgeben statt zu raten.
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
    """Baut einen Channel aus einem einzelnen Roh-Wertobjekt (oder bare Bool)."""
    if not isinstance(raw_entry, dict):
        # "Manche Einträge sind kein Objekt, sondern ein blanker Boolean"
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
    """Dekodiert das devicedata[<SERIAL>_DEVICE]-Dict eines einzelnen Geräts.

    Erwartet bereits das durch Reassembly gemergte, vollständige Dict (beide
    Chunks). Keys ohne erkennbaren Modell_FW-Präfix (z. B. künftige, uns
    unbekannte Metadatenfelder) werden übersprungen statt eine Exception zu
    werfen - robust gegen unbekannte Struktur, wie Konzept §5 fordert.
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
    """Leitet (model, fw_code) aus dem ersten passenden Key im Payload ab."""
    for key in payload:
        if key in _SKIP_KEYS:
            continue
        parsed = split_prefix(key)
        if parsed is not None:
            return parsed[0], parsed[1]
    return None
