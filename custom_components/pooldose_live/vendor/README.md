# Vendorte Kopie von `pooldose_live`

**Nicht direkt bearbeiten.** Dies ist eine 1:1-Kopie von
[`src/pooldose_live/`](../../../src/pooldose_live/) (ohne `probe.py`, das ist
reines P1-CLI-Tooling, das HA nicht braucht).

## Warum überhaupt eine Kopie?

Eine über HACS installierte Kopie von `custom_components/pooldose_live/` bringt
sonst nichts weiter mit — die eigentliche Transport-/Decoder-/Mapping-Logik läge
dann nur in `src/pooldose_live/`, das separat `pip install`-iert werden müsste.
Das ist für eine echte Standalone-Installation nicht praktikabel (siehe Konzept §5,
Entscheidung P6). Vendoring macht die Komponente self-contained: alles, was HACS
kopiert, reicht zum Laufen.

## Warum funktioniert das ohne Import-Umschreiben?

`src/pooldose_live/` verwendet ausschließlich **relative Imports**
(`from .channels import ...`, nicht `from pooldose_live.channels import ...`).
Dadurch ist der Code an keine bestimmte Position in der Modul-Hierarchie
gebunden — dieselben Dateien funktionieren unverändert sowohl als
top-level-installiertes Paket (`pip install -e .`, für `tools/`/`probe.py`/Tests)
als auch als Unterpaket hier unter `custom_components.pooldose_live.vendor.pooldose_live`.

## Synchronisation

`tools/check_vendor_sync.py` vergleicht beide Kopien und schlägt fehl, wenn sie
auseinanderlaufen — lokal ausführbar, außerdem Teil der CI
(`.github/workflows/validate.yml`). Bei einer Änderung an `src/pooldose_live/`:

```bash
python tools/sync_vendor.py   # kopiert src/pooldose_live/ hierher (außer probe.py)
python tools/check_vendor_sync.py   # zur Kontrolle
```
