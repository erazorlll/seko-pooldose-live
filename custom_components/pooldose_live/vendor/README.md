# Vendored copy of `pooldose_live`

**Don't edit directly.** This is a 1:1 copy of
[`src/pooldose_live/`](../../../src/pooldose_live/) (minus `probe.py`, which
is pure P1 CLI tooling that HA doesn't need).

## Why a copy at all?

A copy of `custom_components/pooldose_live/` installed via HACS doesn't bring
anything else along — the actual transport/decoder/mapping logic would then
only live in `src/pooldose_live/`, which would have to be `pip install`-ed
separately. That's not practical for a real standalone installation (see
concept §5, decision P6). Vendoring makes the component self-contained:
whatever HACS copies is enough to run.

## Why does this work without rewriting imports?

`src/pooldose_live/` uses exclusively **relative imports**
(`from .channels import ...`, not `from pooldose_live.channels import ...`).
That means the code isn't tied to any particular position in the module
hierarchy — the same files work unchanged both as a top-level installed
package (`pip install -e .`, for `tools/`/`probe.py`/tests) and as a
subpackage here under
`custom_components.pooldose_live.vendor.pooldose_live`.

## Synchronization

`tools/check_vendor_sync.py` compares both copies and fails if they've
drifted apart — runnable locally, and also part of CI
(`.github/workflows/validate.yml`). After a change to `src/pooldose_live/`:

```bash
python tools/sync_vendor.py   # copies src/pooldose_live/ here (except probe.py)
python tools/check_vendor_sync.py   # to double-check
```
