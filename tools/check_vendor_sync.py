#!/usr/bin/env python3
"""Prüft, ob custom_components/pooldose_live/vendor/pooldose_live/ noch mit
src/pooldose_live/ übereinstimmt (Inhalt, byte-genau, außer `probe.py`).

Exit 0 = synchron, Exit 1 = Drift gefunden (Details ausgegeben). Teil von
.github/workflows/validate.yml - läuft bei jedem Push/PR.

Ausführen: python tools/check_vendor_sync.py
Bei Drift: python tools/sync_vendor.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src" / "pooldose_live"
DEST = REPO_ROOT / "custom_components" / "pooldose_live" / "vendor" / "pooldose_live"

EXCLUDE_TOP_LEVEL = {"probe.py", "__pycache__"}
EXCLUDE_MAPPINGS = {"__pycache__"}


def _tracked_files(root: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for item in sorted(root.iterdir()):
        if item.name in EXCLUDE_TOP_LEVEL:
            continue
        if item.is_file():
            files[item.name] = item
        elif item.is_dir() and item.name == "mappings":
            for sub in sorted(item.iterdir()):
                if sub.name in EXCLUDE_MAPPINGS or not sub.is_file():
                    continue
                files[f"mappings/{sub.name}"] = sub
    return files


def main() -> int:
    if not SRC.is_dir() or not DEST.is_dir():
        print(f"Quelle oder Ziel fehlt: {SRC} / {DEST}", file=sys.stderr)
        return 1

    src_files = _tracked_files(SRC)
    dest_files = _tracked_files(DEST)

    problems: list[str] = []
    for name in sorted(src_files.keys() | dest_files.keys()):
        if name not in dest_files:
            problems.append(f"fehlt in vendor/: {name}")
            continue
        if name not in src_files:
            problems.append(f"nur in vendor/, nicht in src/: {name}")
            continue
        if src_files[name].read_bytes() != dest_files[name].read_bytes():
            problems.append(f"unterschiedlich: {name}")

    if problems:
        print("Vendorte Kopie ist NICHT synchron mit src/pooldose_live/:")
        for p in problems:
            print(f"  - {p}")
        print("\nFix: python tools/sync_vendor.py")
        return 1

    print(f"OK - {len(src_files)} Datei(en) synchron.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
