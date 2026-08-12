# PoolDose WebSocket — Reverse-Engineering-Ergebnisse

Kontext für die Implementierung eines lokalen Push-Clients für die SEKO PoolDose.
Alle Angaben stammen aus einem Mitschnitt am realen Gerät (Stand: siehe Git-History).

## Ziel

Die offizielle Home-Assistant-Core-Integration (`pooldose`, Lib `python-pooldose`)
pollt eine HTTP-API alle **600 s**. Begründung in der Doku: Das Gerät verträgt
keine häufigen Anfragen.

Das Gerät bietet daneben einen **lokalen WebSocket-Server**, der Daten von sich
aus im ~4-Sekunden-Takt pusht. Der Client soll diesen Stream abonnieren statt zu
pollen — das ist ~140× aktueller **und** erzeugt weniger Last als der bisherige
Poll.

## Gerät

| | |
|---|---|
| Modell | SEKO PoolDose Double **Spa** |
| PRODUCT_CODE | `PDPR1H04AW100` |
| Firmware | `539292` |
| Serial / Device-Key | `012600002BB3_DEVICE` |
| IP | `192.168.0.74` (konfigurierbar machen) |
| WebSocket | `ws://192.168.0.74:1334/` |
| Web-UI / HTTP-API | Port 80 |

## Protokoll

### Verbindung

- **Keine Authentifizierung.** Verbindung öffnen genügt.
- **Kein Subscribe-/Login-Frame nötig.** Der Client sendet nichts; das Gerät
  pusht von sich aus. Verifiziert: ein reiner Listener ohne einen einzigen
  ausgehenden Frame erhält den vollständigen Datenstrom.
- Kein Keepalive vom Client nötig.

### Frame-Format

JSON-Textframes:

```json
{ "topic": "<name>", "data": { ... } }
```

Beobachtete Topics:

| Topic | Frequenz | Inhalt |
|---|---|---|
| `instant_values` | jeder Tick (~4,2 s) | Messwerte, Konfiguration, Status — **das Nutzsignal** |
| `wifi_station` | ~16,8 s (jeder 4. Tick) | WLAN-Verbindungsdaten |
| `time` | ~25,2 s (jeder 6. Tick) | Gerätezeit |
| `wifi_status` | selten / initial | |
| `wdp_status` | selten | |

### Timing

Fester Scheduler-Tick von **4,2 s**; alle beobachteten Abstände sind exakte
Vielfache davon.

**Wichtig:** Ticks fallen aus. Im Mitschnitt kamen nur 16 von 24 erwarteten
`instant_values`-Zyklen an (effektiv ~5,8 s Durchschnitt, längste Lücke 16 s).
Die Aussetzer korrelieren mit `wifi_station`/`time`-Frames — das Gerät verwirft
offenbar eigene Sendeslots unter Last.

→ Kein festes Intervall annehmen. **Watchdog: 30 s ohne Frame → Reconnect.**

### Chunking

`instant_values` wird auf **2 Frames** aufgeteilt:

```json
"progressInfo": { "total": 2, "offset": 1 }   // bzw. offset: 2
```

- **Chunk 1 (offset 1):** `deviceInfo`, Sensoren (pH, ORP, ppm, Temperatur),
  Sollwerte, Kalibrierung, Dosier-Konfiguration
- **Chunk 2 (offset 2):** Timer und ~30 Statusflags

Beide sind flache Dicts unter demselben Serial-Key und **überschneidungsfrei** —
ein `dict.update()` reicht zum Mergen.

Reassembly-Regeln:
1. Bei `offset == 1` den Puffer **leeren** (halbe Zyklen sind real, sonst
   entstehen Frankenstein-Datensätze aus zwei Runden).
2. Erst bei `offset == total` weiterverarbeiten.

```python
buf = {}

def on_message(raw):
    msg = json.loads(raw)
    if msg.get("topic") != "instant_values":
        return None
    p = msg["data"]["progressInfo"]
    if p["offset"] == 1:
        buf.clear()
    for serial, payload in msg["data"]["devicedata"].items():
        buf.setdefault(serial, {}).update(payload)
    return dict(buf) if p["offset"] == p["total"] else None
```

## Datenstruktur

```
data.devicedata.<SERIAL>_DEVICE.<PRODUCT_CODE>_FW<FW>_<typ>_<hash>
```

Zusätzlich unter demselben Serial-Key:
- `deviceInfo`: `{"dwi_status": "ok", "modbus_status": "on"}`
- `collapsed_bar`: `[]`

Einige Keys weichen vom Hash-Schema ab und sind sprechend, z. B.
`PDPR1H04AW100_FW539292_Elapsed_PowerON_Delay`.

### Wertobjekte

```json
{
  "visible": true,
  "alarm": false,
  "current": 7.2,
  "resolution": 0.1,
  "magnitude": ["pH", "PH"],
  "absMin": 0, "absMax": 14,
  "minT": 6, "maxT": 8,
  "set": 7.1
}
```

| Feld | Bedeutung |
|---|---|
| `current` | Istwert |
| `set` | Sollwert (nur bei einstellbaren Parametern) |
| `absMin` / `absMax` | technischer Wertebereich |
| `minT` / `maxT` | Alarmschwellen |
| `alarm` | Alarmzustand des Kanals |
| `visible` | ob der Kanal am Gerät konfiguriert/aktiv ist |
| `magnitude` | `[Anzeigeeinheit, Einheitskonstante]` |
| `resolution` | Schrittweite, kann `"NA"` sein |
| `comboitems` | bei Auswahlfeldern: Liste `[[index, labelkey], ...]` |

**`visible: false` als Filter benutzen.** Beispiel aus dem Mitschnitt: Der
ppm-Kanal (freies Chlor) steht auf `visible: false`, `current: 0`, `alarm: true`
— es hängt keine Sonde dran. Ohne Filter entstehen Entities mit permanentem,
nie verschwindendem Alarm.

Achtung: Manche Einträge sind **kein Objekt**, sondern ein blanker Boolean
(z. B. `..._w_1emtltkel: false`). Beim Parsen auf `isinstance(v, dict)` prüfen.

### Bekannte Keys (aus dem Mitschnitt abgeleitet)

| Hash-Key | Bedeutung | Beispielwert |
|---|---|---|
| `w_1ekeigkin` | pH Istwert | 7.2 |
| `w_1ekeiqfat` | pH Sollwert | 7.1 |
| `w_1eklenb23` | ORP/Redox Istwert | 845 mV |
| `w_1eklgnjk2` | ORP Sollwert | 675 mV |
| `w_1eo03t46k` | Freies Chlor (ppm), inaktiv | 0 |
| `w_1eommf39k` | Wassertemperatur | 34.5 °C |
| `w_1eklg44ro` | pH-Dosierrichtung | `ACID` |
| `w_1eklgnolb` | ORP-Dosierrichtung | `LOW` |
| `w_1eklh8gb7` | pH-Kalibrierart | `2_POINTS` |
| `w_1eklhs3b4` / `w_1eklhs65u` | pH-Kalibrierpunkte | 4 / 58 mV |
| `w_1eklh8i5t` | ORP-Kalibrierart | `1_POINT` |
| `w_1eklhs8r3` / `w_1eklhsase` | ORP-Kalibrierpunkte | 0 / 1.04 |
| `w_1eklj6euj`, `w_1eo1s18s8`, `w_1eklj12vv`, `w_1eo1v3q21` | Dosiermodi | `PROPORTIONAL` |

**Die Keys nicht selbst raten.** `python-pooldose` enthält vollständige
Mapping-Tabellen für dieses Modell. Auflösen über:

```bash
pooldose --host 192.168.0.74 --analyze
# oder direkt im Paketverzeichnis nach einem Hash greppen
```

Diese Mappings als Quelle der Wahrheit verwenden statt eigener Tabellen.

### Label-Dekodierung

Enum-artige Werte kommen als i18n-Schlüssel in Pipes:

```
"|PDPR1H04AW100_FW539292_LABEL_w_1eklg44ro_ACID|"
```

**Nicht von rechts splitten** — `_2_POINTS` und `__C` (= °C) brechen dabei.
Stattdessen den bekannten Präfix entfernen:

```python
def label(key, value, model, fw):
    if isinstance(value, str) and value.startswith("|"):
        inner = value.strip("|")
        return inner.removeprefix(f"{model}_{fw}_LABEL_{key}_").rstrip("_")
    return value
```

`comboitems` verwenden dasselbe Schema mit `COMBO` statt `LABEL`.

## Warum das gerätefreundlich ist

`deviceInfo.modbus_status: "on"` — das WiFi-Modul spricht intern per Modbus mit
dem Dosierregler und tut das **in seinem eigenen Takt, unabhängig von
Zuhörern**. Ein zusätzlicher WebSocket-Listener erzeugt daher **keine
zusätzliche Modbus-Last** auf dem Regler. Der einzige Pfad, über den die
eigentliche Dosierregelung hätte gestört werden können, ist damit ausgeschlossen.

Trotzdem: **genau eine Verbindung halten.** Embedded-Stack mit wenigen Sockets;
jeder Extra-Client verschärft die oben beobachteten Tick-Aussetzer. Web-UI-Tabs
während des Betriebs schließen.

## Schreibzugriffe — Vorsicht

Die Wertobjekte enthalten `set`, `absMin`, `absMax`, `resolution`, also
Schreib-Metadaten. Über diesen Kanal lassen sich mit hoher Wahrscheinlichkeit
auch Sollwerte verändern — **ohne erkennbare Authentifizierung**.

Der Client soll zunächst **rein lesend** implementiert werden. Schreiben ist ein
separates, bewusstes Feature. Ein falsch geformter Frame kann Parameter einer
realen Dosieranlage verstellen.

## Anforderungen an die Implementierung

- Python, async (`websockets` oder `aiohttp`)
- Reconnect mit exponentiellem Backoff (Start ~2 s, Deckel ~60 s)
- Watchdog: 30 s ohne Frame → Verbindung neu aufbauen
- Host/Port konfigurierbar, keine Hardcodes
- Robust gegen unvollständige Zyklen und unbekannte Topics
- Ausgabe nach MQTT mit Home-Assistant-Discovery
- **Entprellung/Throttling vor MQTT:** bei ~40 Entities à 4 s entstehen sonst
  ~20 Mio. Recorder-Zeilen pro Jahr. Nur bei Wertänderung publizieren, plus
  Heartbeat alle paar Minuten. Für Sensoren mit Rauschen (pH, ORP) ggf.
  Mindest-Delta an `resolution` koppeln.
- Availability-Topic für MQTT setzen (Last Will), damit HA den Ausfall sieht

## Offene Punkte

- Verhalten bei mehreren gleichzeitigen Clients nicht systematisch getestet
- Frames anderer Topics (`wdp_status`, `wifi_status`) noch nicht ausgewertet
- Ob das Gerät bei Wertänderung außerhalb des Ticks sofort sendet: unbekannt
- Schreib-Frame-Format: unbekannt (bewusst nicht getestet)