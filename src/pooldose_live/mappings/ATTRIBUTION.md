# Herkunft dieser Mapping-Dateien

Die `model_*.json`-Dateien in diesem Verzeichnis stammen aus
[lmaertin/python-pooldose](https://github.com/lmaertin/python-pooldose)
(`src/pooldose/mappings/`), Stand 2026-08-12 (Commit `6985c30`, Version 0.9.6).

```
MIT License
Copyright (c) 2025 Lukas Maertin
```

Kopiert statt als Laufzeit-Abhängigkeit eingebunden — Begründung in
[docs/konzept.md, Abschnitt 5.2](../../../docs/konzept.md#52-umgang-mit-python-pooldose-mappings-kopieren-code-selbst-schreiben).
Kurzfassung: Der für uns nötige Zugriffspfad (Mapping aus rohem WebSocket-Dict
statt HTTP-Response) ist kein dokumentiertes Public API der Bibliothek, und
Core pinnt `python-pooldose==0.9.6` hart — ein eigener, abweichender Pin würde
mit der offiziellen HA-Integration im selben Environment kollidieren.

Diese Dateien driften vom Original weg, wenn dort neue Modelle/Firmwares
hinzukommen. Ein Sync-Check dagegen ist für P5/P6 vorgesehen (Konzept §7).

`model_aliases.py` in diesem Paket enthält zusätzlich die kleine
`MODEL_ALIASES`-Tabelle aus `src/pooldose/constants.py` derselben Quelle.
