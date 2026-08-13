"""Regression test for the "ModuleNotFoundError: pooldose_live" incident.

`mapping.py` used to hardcode the absolute package name "pooldose_live.mappings"
in its `importlib.resources.files()` calls. That resolves fine everywhere in
this repo's own tests and dev environment, because `pooldose_live` is always
also pip-installed as a top-level package here (`pip install -e .`, used by
the P1 CLI/pytest workflow) - even when the code under test is reached
through the *vendored* copy (custom_components/pooldose_live/vendor/
pooldose_live/), Python happily resolves the hardcoded absolute name against
the unrelated top-level installed package instead. A real HACS install has
no such top-level package - only the vendored copy - so the exact same call
raised ModuleNotFoundError there and silently killed the coordinator's
background task on the very first snapshot (zero entities, ever, no error
visible without debug logging).

This test reproduces that condition for real: it runs in a *subprocess*
started with `-S` (skip `site` - no site-packages on sys.path at all, so the
top-level `pooldose_live` package cannot be found even though it's
pip-installed in this dev environment), registers namespace-package stand-ins
for custom_components.pooldose_live(.vendor.pooldose_live) so the *real*
vendored mapping.py/channels.py load with their true package name and
relative imports resolve correctly - without executing custom_components/
pooldose_live/__init__.py (which imports `homeassistant`, unavailable and
irrelevant here). If this regresses to a hardcoded absolute package name
again, this test fails with the same ModuleNotFoundError users would hit.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_SCRIPT = """
import sys, importlib, importlib.util
from pathlib import Path

assert not any("site-packages" in p or "dist-packages" in p for p in sys.path), (
    "isolation failed: site-packages still on sys.path, this run wouldn't "
    "have caught the bug"
)

repo_root = Path(r"{repo_root}")
vendor_root = repo_root / "custom_components" / "pooldose_live" / "vendor" / "pooldose_live"

def register_namespace_package(name, path):
    # Namespace package stand-in: correct __path__ so relative imports
    # inside the real vendored modules resolve, but no __init__.py of any
    # ancestor ever actually executes (the real
    # custom_components/pooldose_live/__init__.py imports `homeassistant`,
    # which has nothing to do with what this test is checking).
    spec = importlib.util.spec_from_loader(name, loader=None, is_package=True)
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(path)]
    sys.modules[name] = module

register_namespace_package("custom_components", repo_root / "custom_components")
register_namespace_package("custom_components.pooldose_live", repo_root / "custom_components" / "pooldose_live")
register_namespace_package("custom_components.pooldose_live.vendor", repo_root / "custom_components" / "pooldose_live" / "vendor")
register_namespace_package("custom_components.pooldose_live.vendor.pooldose_live", vendor_root)

mapping_mod = importlib.import_module("custom_components.pooldose_live.vendor.pooldose_live.mapping")
mapping = mapping_mod.load("PDPR1H04AW100", "FW539292")
assert mapping.status.value == "exact", mapping.status
assert mapping.coverage[0] > 0, "expected a non-empty mapping table"
print("OK")
""".format(repo_root=REPO_ROOT)


def test_mapping_loads_from_vendored_copy_without_toplevel_package():
    result = subprocess.run(
        [sys.executable, "-S", "-c", _SCRIPT],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert result.returncode == 0, (
        f"loading the mapping via the vendored import path failed "
        f"(this is exactly how it breaks in a real HACS install):\n"
        f"{result.stdout}{result.stderr}"
    )
    assert "OK" in result.stdout
