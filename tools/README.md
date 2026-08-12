# tools/ws_probe.py — P0-Mitschnitt- und Messwerkzeug

Reines Diagnosewerkzeug für Phase P0 (Konzept §7/§8). Kein Teil der späteren
Integration, aber die Transportschicht (eine Verbindung, Reassembly, Watchdog,
Backoff) ist bewusst schon so gebaut, wie sie in P1 wiederverwendet werden soll.

Voraussetzung: `aiohttp` (liegt bei den meisten HA-Umgebungen ohnehin vor).

```bash
pip install aiohttp
```

## Aufzeichnen

```bash
python tools/ws_probe.py record --host 192.168.0.74 --duration 1800 \
    --out recordings/session1.jsonl.gz --http-probe 60
```

- `--duration` in Sekunden; ohne Angabe läuft es bis Strg+C.
- `--out` schreibt JSON Lines, `.gz`-Endung komprimiert automatisch.
  Ohne `--out` gibt es nur die Live-Statistik, keine Datei.
- `--http-probe SEK` misst nebenher die HTTP-Antwortzeit des Geräts
  (`GET /js_libs/params.js`, keine `getInstantValues`-Last).
- Seriennummern werden per Default im Mitschnitt ersetzt (`--keep-serial` schaltet ab),
  WLAN-Schlüssel werden immer redigiert. Ein Mitschnitt ist damit teilbar.
- `recordings/` und `*.jsonl(.gz)` sind über `.gitignore` vom Repo ausgeschlossen —
  das sind Rohdaten deines Geräts, kein Quellcode.

Am Ende der Aufzeichnung erscheint automatisch derselbe Bericht wie bei `report`.

## Auswerten

```bash
python tools/ws_probe.py report recordings/session1.jsonl.gz
```

Liefert: Frames/Bytes/Topics, hochgerechneten Tagesverkehr, Zyklen-Vollständigkeit,
geschätzten Grundtakt mit Abweichung, längste Frame-Lücke, HTTP-Latenzverteilung,
sowie — sofern mit Rohdaten aufgezeichnet (Vorgabe, `--no-raw` schaltet ab) — pro
Kanal: `visible`/`alarm`-Zustand, ob `visible` sich je geändert hat, und wie oft sich
jeder Kanal geändert hat (direkte Grundlage für die Entprellungsschwellen aus
Konzept 5.6).

## Bezug zu den P0-Messfragen (Konzept §8)

| Frage | Wie hier beantwortet |
|---|---|
| Sendet das Gerät ohne Zuhörer? | Nicht per Skript prüfbar — separat am Switch/Router beobachten |
| Tick-Ausfallrate bei 0/1/2 Clients | `record` parallel mit `--http-probe` je einmal starten, `Slot-Histogramm` im Bericht vergleichen |
| HTTP-Antwortzeit mit/ohne WS-Listener | `--http-probe` mit und ohne parallel laufenden zweiten `record`-Prozess |
| Reconnect-Verhalten (offset:1 sofort?) | `--watchdog` künstlich niedrig setzen, `Zyklen abgebrochen` beobachten |
| Ändert sich `visible` zur Laufzeit? | Langer Mitschnitt (≥24h), `visible gewechselt` im Bericht |
| Kommen Werte außerhalb des Ticks? | Während der Aufzeichnung am Gerät etwas verstellen, `frame_intervals` prüfen |
