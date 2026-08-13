"""Tests für die Schreib-Validierung (P4, pooldose_live/write.py).

Reine Bibliothekslogik ohne Home-Assistant-Abhängigkeit - anders als
test_p2/p3_manual.py läuft das als regulärer pytest-Test (kein
homeassistant.runner/fcntl-Problem, siehe README).

Ausführen: python -m pytest tests/test_write.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pooldose_live.channels import Channel
from pooldose_live.mapping import MappingStatus, ModelMapping
from pooldose_live.write import (
    WriteError,
    build_prefix,
    encode_number,
    encode_select,
    encode_switch,
)

# ph_target des Zielgeräts: min=6, max=8, step=0.1 (aus dem echten Mapping)
PH_TARGET = Channel(hash="w_1ekeiqfat", current=7.1, abs_min=6, abs_max=8, resolution=0.1)

WATER_METER = Channel(
    hash="w_1eklinki6", current="1",
    options={"0": "m3", "1": "L"},
)


def test_encode_number_valid() -> None:
    assert encode_number(PH_TARGET, 7.4) == 7.4
    assert encode_number(PH_TARGET, 6.0) == 6.0  # Untergrenze
    assert encode_number(PH_TARGET, 8.0) == 8.0  # Obergrenze


def test_encode_number_out_of_range() -> None:
    with pytest.raises(WriteError, match="außerhalb"):
        encode_number(PH_TARGET, 8.5)
    with pytest.raises(WriteError, match="außerhalb"):
        encode_number(PH_TARGET, 5.9)


def test_encode_number_wrong_step() -> None:
    with pytest.raises(WriteError, match="Schrittweite"):
        encode_number(PH_TARGET, 7.03)


def test_encode_number_not_a_number() -> None:
    with pytest.raises(WriteError):
        encode_number(PH_TARGET, "7.1")
    with pytest.raises(WriteError):
        encode_number(PH_TARGET, True)  # bool ist technisch int, muss abgelehnt werden


def test_encode_switch_valid() -> None:
    assert encode_switch(True) == "O"
    assert encode_switch(False) == "F"


def test_encode_switch_invalid() -> None:
    with pytest.raises(WriteError):
        encode_switch("on")
    with pytest.raises(WriteError):
        encode_switch(1)


def test_encode_select_valid() -> None:
    assert encode_select(WATER_METER, "L") == "1"
    assert encode_select(WATER_METER, "m3") == "0"


def test_encode_select_invalid() -> None:
    with pytest.raises(WriteError, match="Erlaubt"):
        encode_select(WATER_METER, "gallons")


def test_build_prefix_exact() -> None:
    mapping = ModelMapping("PDPR1H04AW100", "FW539292", MappingStatus.EXACT, "539292", {})
    assert build_prefix(mapping) == "PDPR1H04AW100_FW539292_"


def test_build_prefix_raw() -> None:
    mapping = ModelMapping("TESTMODEL", "FW1", MappingStatus.RAW, None, None)
    assert build_prefix(mapping) == "TESTMODEL_FW1_"
