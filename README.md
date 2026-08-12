# seko-pooldose-live

Home-Assistant-Integration (HACS) für SEKO PoolDose mit **Live-Daten über den lokalen
WebSocket** des Geräts statt HTTP-Polling.

Zielgerät: SEKO PoolDose Double Spa (`PDPR1H04AW100`, FW `539292`).

Status: **P2 — erstes HA-Skelett (read-only), parallel zur Core-Integration
installierbar.** P0 (Messungen, Architekturentscheidungen) und P1
(Transport/Decoder/Mapping-Bibliothek) sind abgeschlossen, siehe
[docs/konzept.md](docs/konzept.md).

## Warum

Die offizielle HA-Integration (`pooldose`, Bibliothek `python-pooldose`) pollt alle
600 s eine HTTP-API. Das Gerät pusht dieselben Daten von sich aus im ~4,2-s-Takt über
`ws://<host>:1334/` — ohne Authentifizierung, ohne Subscribe-Frame. Zuhören statt
fragen bringt in der Praxis Faktor ~40 in der Aktualität (real gemessen, siehe
Konzept §8.2 — der anfangs theoretisch angenommene Faktor ~143 hielt der Messung
nicht stand) und beseitigt nebenbei eine Reihe konkreter Schwächen der heutigen
Lösung.

## Paket `pooldose_live` (P1)

```bash
pip install -e .
python -m pooldose_live.probe --host 192.168.0.74
```

Verbindet sich, leitet Modell/FW direkt aus den Daten-Keys ab (kein HTTP-Call
nötig), lädt die passende Mapping-Tabelle und zeigt die aufgelösten Kanäle —
danach live jede Wertänderung. `--once` beendet nach der ersten Tabelle,
`--show-invisible` zeigt auch `visible: false`-Kanäle.

| Modul | Zweck |
|---|---|
| `transport.py` | Eine WS-Verbindung, Reassembly, zwei Watchdogs (Verbindung + `instant_values`-Staleness, siehe Konzept §5.3), Backoff |
| `channels.py` | Rohes devicedata-Dict → `Channel`-Objekte (Label-Dekodierung, Einheiten, comboitems) |
| `mapping.py` | Dreistufiger Fallback: exaktes Modell+FW → gleiches Modell/andere FW → Raw-Modus (Konzept §5.5) |
| `mappings/` | Vendorte Mapping-Tabellen aus `python-pooldose` (MIT), siehe `ATTRIBUTION.md` |
| `probe.py` | CLI-Entry-Point für P1 |

Noch kein Schreibzugriff (P4).

## HA-Integration `custom_components/pooldose_live/` (P2)

Read-only: `sensor` + `binary_sensor`, dynamisch aus den aufgelösten Kanälen
erzeugt (nicht aus einer festen Tabelle wie die Core-Integration — die
Kanalmenge steht erst zur Laufzeit fest, je nach Mapping-Treffer oder
Raw-Fallback). `visible: false`-Kanäle erzeugen keine Entity (Konzept B3),
das `alarm`-Flag landet als Attribut am Sensor (Konzept B4).

**Bekannte Lücke:** `manifest.json` hat `"requirements": []` — das Paket
`pooldose_live` (siehe oben) muss aktuell im selben Python-Environment wie
Home Assistant installiert sein (`pip install -e .` in diesem Repo, im
venv von HA). Echte Distribution (PyPI-Release oder Vendoring in die
Komponente) ist P6-Scope.

**Setup wartet nicht auf Live-Daten:** Der Config Flow prüft nur, ob der
WebSocket-Port antwortet (10 s Timeout) — er wartet nicht auf einen
vollständigen `instant_values`-Zyklus. Das Gerät kann laut Konzept §8.2/§8.3
legitim mehrere Minuten schweigen; ein Setup, das darauf wartet, würde
regelmäßig grundlos fehlschlagen. Details in Konzept §5.7.

Tests: `tests/test_p2_manual.py` — kein regulärer `pytest`-Lauf, da
`pytest-homeassistant-custom-component` unter Windows an `homeassistant.runner`
(braucht `fcntl`, Unix-only) scheitert. Stattdessen ein eigenständiges Skript
mit echten HA-Kernklassen (`python tests/test_p2_manual.py`). Auf einer
echten (Linux-)HA-Instanz sollte das durch reguläre pytest-Fixtures
ersetzt/ergänzt werden.

## Dokumente

- [`websocker-spec.md`](websocker-spec.md) — Reverse-Engineering-Ergebnisse vom echten Gerät
- [`docs/konzept.md`](docs/konzept.md) — Analyse der bestehenden Lösungen, Befunde, Architekturkonzept, Messergebnisse, Phasenplan
- [`tools/README.md`](tools/README.md) — P0-Diagnosewerkzeuge (Mitschnitt, HTTP-Basislinie)

## Credits

Die Mapping-Tabellen (Hash-Key → sprechender Name) stammen aus
[lmaertin/python-pooldose](https://github.com/lmaertin/python-pooldose) (MIT).
