"""Checks that the vendored copy (custom_components/pooldose_live/vendor/) is
in sync with src/pooldose_live/ - see tools/check_vendor_sync.py and
custom_components/pooldose_live/vendor/README.md for the rationale.

Pure library logic without a Home Assistant dependency, runs as a regular
pytest test (no fcntl problem, see README).
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
        f"vendor/ is not in sync with src/pooldose_live/ - "
        f"run python tools/sync_vendor.py.\n{result.stdout}{result.stderr}"
    )
