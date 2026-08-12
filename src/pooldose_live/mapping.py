"""Mapping-Loader: hash -> sprechender Name, dreistufiger Fallback (Konzept §5.5).

1. Exakter Treffer: model_<MODEL>_FW<FW>.json
2. Gleiches Modell, andere Firmware vorhanden -> laden + Warnung
3. Kein Mapping-File für das Modell -> Raw-Modus: Name/Typ pro Kanal aus der
   Struktur des Wertobjekts geraten (siehe `infer_raw_type`)

Die Mapping-Tabellen selbst sind aus python-pooldose vendort (MIT-Lizenz,
siehe mappings/ATTRIBUTION.md) - dieser Loader und die gesamte Fallback-Logik
sind eigener Code (Begründung: Konzept §5.2).
"""

from __future__ import annotations

import importlib.resources
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from pooldose_live.channels import Channel

# Modell-Alias-Tabelle: PRODUCT_CODE, wie ihn manche Geräte melden, weicht vom
# Modell-ID ab, das tatsächlich in den Daten-Keys/Mapping-Dateinamen steckt.
# Übernommen aus python-pooldose/src/pooldose/constants.py (siehe ATTRIBUTION.md).
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
    """Ein Channel, angereichert um Name/Typ/Anzeigewert."""

    name: str
    type: str
    channel: Channel
    display: Any
    source: str  # "mapping" | "raw"
    min: Any = None
    max: Any = None
    step: Any = None


def _list_mapping_files() -> list[str]:
    files = importlib.resources.files("pooldose_live.mappings")
    return [f.name for f in files.iterdir() if f.name.endswith(".json")]


def _load_json(filename: str) -> dict[str, Any]:
    path = importlib.resources.files("pooldose_live.mappings").joinpath(filename)
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _closest_fw(available: list[str], wanted: str) -> str:
    """Wählt die 'nächstliegende' FW-Datei desselben Modells.

    Best effort: numerischer Abstand, falls beide FW-Codes als Zahl lesbar
    sind (z. B. FW539292 -> 539292); sonst die erste in sortierter Reihenfolge.
    Bei nur einer verfügbaren Datei ist das ohnehin eindeutig - der genaue
    Auswahlmechanismus bei mehreren Kandidaten ist ein Detail, das erst mit
    einem echten Mehr-FW-Fall am selben Modell validiert werden kann.
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
    """Aufgelöste Mapping-Tabelle für ein Modell/FW, invertiert nach Hash.

    Ein Hash kann auf mehrere benannte Einträge zeigen (z. B. minT/maxT-
    Varianten desselben Rohwerts über das "field"-Attribut) - deshalb
    hash -> Liste, nicht hash -> ein Eintrag.
    """

    def __init__(self, model_id: str, fw_code: str, status: MappingStatus,
                matched_fw: str | None, table: dict[str, dict[str, Any]] | None) -> None:
        self.model_id = model_id
        self.fw_code = fw_code
        self.status = status
        self.matched_fw = matched_fw
        self.table = table or {}
        self._by_hash: dict[str, list[tuple[str, dict]]] = {}
        for name, entry in self.table.items():
            self._by_hash.setdefault(entry["key"], []).append((name, entry))

    def resolve_channel(self, channel: Channel) -> list[ResolvedChannel]:
        """Löst einen einzelnen Channel auf. Leere Liste nur bei Raw-Fallback
        mit unbestimmbarem Typ (kommt praktisch nicht vor, siehe `infer_raw_type`)."""
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
        """(Anzahl Hashes im Mapping, Anzahl eindeutiger Hashes) - zur Diagnose."""
        return len(self.table), len(self._by_hash)


def load(model_id: str, fw_code: str) -> ModelMapping:
    """Lädt die Mapping-Tabelle für (model_id, fw_code) mit Drei-Stufen-Fallback."""
    resolved_model = MODEL_ALIASES.get(model_id, model_id)

    exact_name = f"model_{resolved_model}_FW{fw_code.removeprefix('FW')}.json"
    available = _list_mapping_files()
    if exact_name in available:
        return ModelMapping(model_id, fw_code, MappingStatus.EXACT, fw_code,
                            _load_json(exact_name))

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
    """Rät den Entity-Typ aus der Struktur des Wertobjekts (Konzept §5.5).

    set vorhanden -> number; comboitems -> select; current ist "O"/"F" oder
    bool -> binary_sensor (bewusst nicht "switch" - im Raw-Modus wissen wir
    nicht, ob ein Kanal schreibbar ist); sonst sensor.
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
    """Der unbearbeitete current-Wert, wie er im Payload stand (mit Pipes,
    falls Label) - für den Abgleich gegen kuratierte conversion-Tabellen,
    die genau dieses Format als Schlüssel verwenden."""
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

    # sensor / binary_sensor: kuratierte conversion (Original-Rohwert als
    # Schlüssel) bevorzugen, sonst generisch dekodiertes Label als Fallback.
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
