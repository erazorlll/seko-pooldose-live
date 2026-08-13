"""Prüft, dass die vendorte Kopie (custom_components/pooldose_live/vendor/)
synchron mit src/pooldose_live/ ist - siehe tools/check_vendor_sync.py und
custom_components/pooldose_live/vendor/README.md für die Begründung.

Reine Bibliothekslogik ohne Home-Assistant-Abhängigkeit, läuft als regulärer
pytest-Test (kein fcntl-Problem, siehe README).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_vendor_is_in_sync() -> None:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "check_vendor_sync.py")],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert result.returncode == 0, (
        f"vendor/ ist nicht synchron mit src/pooldose_live/ - "
        f"python tools/sync_vendor.py ausfuehren.\n{result.stdout}{result.stderr}"
    )
