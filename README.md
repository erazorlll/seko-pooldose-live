# seko-pooldose-live

Home-Assistant-Integration (HACS) für SEKO PoolDose mit **Live-Daten über den lokalen
WebSocket** des Geräts statt HTTP-Polling.

Zielgerät: SEKO PoolDose Double Spa (`PDPR1H04AW100`, FW `539292`).

Status: **Konzeptphase — noch keine Implementierung.**

## Warum

Die offizielle HA-Integration (`pooldose`, Bibliothek `python-pooldose`) pollt alle
600 s eine HTTP-API. Das Gerät pusht dieselben Daten von sich aus im ~4,2-s-Takt über
`ws://<host>:1334/` — ohne Authentifizierung, ohne Subscribe-Frame. Zuhören statt
fragen bringt Faktor ~143 in der Aktualität und beseitigt nebenbei eine Reihe
konkreter Schwächen der heutigen Lösung.

## Dokumente

- [`websocker-spec.md`](websocker-spec.md) — Reverse-Engineering-Ergebnisse vom echten Gerät
- [`docs/konzept.md`](docs/konzept.md) — Analyse der bestehenden Lösungen, Befunde, Architekturkonzept, Phasenplan

## Credits

Die Mapping-Tabellen (Hash-Key → sprechender Name) stammen aus
[lmaertin/python-pooldose](https://github.com/lmaertin/python-pooldose) (MIT).
