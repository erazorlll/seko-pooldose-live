#!/usr/bin/env python3
"""Kopiert src/pooldose_live/ nach custom_components/pooldose_live/vendor/pooldose_live/.

Für eine echte HACS-Installation muss die Komponente self-contained sein
(siehe custom_components/pooldose_live/vendor/README.md) - dieses Skript hält
die vendorte Kopie synchron, nachdem an der Bibliothek etwas geändert wurde.

`probe.py` wird bewusst ausgelassen: reines P1-CLI-Tooling, HA braucht es nicht.

Ausführen: python tools/sync_vendor.py
Danach pruefen: python tools/check_vendor_sync.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src" / "pooldose_live"
DEST = REPO_ROOT / "custom_components" / "pooldose_live" / "vendor" / "pooldose_live"

EXCLUDE_TOP_LEVEL = {"probe.py", "__pycache__"}
EXCLUDE_MAPPINGS = {"__pycache__"}


def main() -> int:
    if not SRC.is_dir():
        print(f"Quelle nicht gefunden: {SRC}", file=sys.stderr)
        return 1

    DEST.mkdir(parents=True, exist_ok=True)
    copied = []

    for item in sorted(SRC.iterdir()):
        if item.name in EXCLUDE_TOP_LEVEL:
            continue
        if item.is_file():
            shutil.copy2(item, DEST / item.name)
            copied.append(item.name)
        elif item.is_dir() and item.name == "mappings":
            dest_mappings = DEST / "mappings"
            dest_mappings.mkdir(exist_ok=True)
            for sub in sorted(item.iterdir()):
                if sub.name in EXCLUDE_MAPPINGS or not sub.is_file():
                    continue
                shutil.copy2(sub, dest_mappings / sub.name)
                copied.append(f"mappings/{sub.name}")

    print(f"{len(copied)} Datei(en) kopiert nach {DEST}:")
    for name in copied:
        print(f"  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
