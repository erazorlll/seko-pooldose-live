# Origin of these mapping files

The `model_*.json` files in this directory come from
[lmaertin/python-pooldose](https://github.com/lmaertin/python-pooldose)
(`src/pooldose/mappings/`), as of 2026-08-12 (commit `6985c30`, version
0.9.6).

```
MIT License
Copyright (c) 2025 Lukas Maertin
```

Copied instead of included as a runtime dependency — reasoning in
[docs/concept.md, section 5.2](../../../docs/concept.md#52-dealing-with-python-pooldose-copy-the-mappings-write-our-own-code).
Short version: the access path we need (a mapping from a raw WebSocket
dict instead of an HTTP response) isn't a documented public API of the
library, and core hard-pins `python-pooldose==0.9.6` — a different pin of
our own would collide with the official HA integration in the same
environment.

These files drift from the original as new models/firmwares get added
there. A sync check against that is planned for P5/P6 (concept §7).

The `MODEL_ALIASES` table (a small alias map for models whose reported
PRODUCT_CODE differs from their model ID) is taken from the same source's
`src/pooldose/constants.py` and lives directly in this package's
`mapping.py`.
