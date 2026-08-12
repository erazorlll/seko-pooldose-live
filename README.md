# seko-pooldose-live

Home-Assistant-Integration (HACS) für SEKO PoolDose mit **Live-Daten über den lokalen
WebSocket** des Geräts statt HTTP-Polling.

Zielgerät: SEKO PoolDose Double Spa (`PDPR1H04AW100`, FW `539292`).

Status: **P1 — Transport/Decoder/Mapping-Bibliothek, ohne Home Assistant.**
P0 (Messungen, Architekturentscheidungen) ist abgeschlossen, siehe
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

Noch keine HA-Integration (kommt in P2) und noch kein Schreibzugriff (P4).

## Dokumente

- [`websocker-spec.md`](websocker-spec.md) — Reverse-Engineering-Ergebnisse vom echten Gerät
- [`docs/konzept.md`](docs/konzept.md) — Analyse der bestehenden Lösungen, Befunde, Architekturkonzept, Messergebnisse, Phasenplan
- [`tools/README.md`](tools/README.md) — P0-Diagnosewerkzeuge (Mitschnitt, HTTP-Basislinie)

## Credits

Die Mapping-Tabellen (Hash-Key → sprechender Name) stammen aus
[lmaertin/python-pooldose](https://github.com/lmaertin/python-pooldose) (MIT).
