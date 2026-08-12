# Konzept: `pooldose_live` — HACS-Integration mit WebSocket-Push

Stand: 2026-08-12
Zielgerät: SEKO PoolDose Double Spa, `PDPR1H04AW100`, FW `539292`

Dieses Dokument ist eine **Analyse + Konzept**, keine Implementierung. Alle Aussagen
sind gegen den Quellcode der bestehenden Lösungen bzw. gegen einen echten Payload
desselben Gerätemodells geprüft. Wo etwas nicht belegbar war, steht das explizit dabei.

---

## 1. Analysebasis

| Quelle | Version / Stand | Wie geprüft |
|---|---|---|
| `python-pooldose` | 0.9.6 (`src/pooldose/__init__.py`) | Repo geklont, Quellcode gelesen |
| HA-Core-Integration `pooldose` | `homeassistant/components/pooldose`, `requirements: python-pooldose==0.9.6` | Sparse-Checkout von `home-assistant/core`, Quellcode gelesen |
| Mapping des Zielgeräts | `model_PDPR1H04AW100_FW539292.json`, 64 Einträge | vollständig gelesen |
| Echter Geräte-Payload | `instantvalues.json` aus Issue lmaertin/python-pooldose#20 (exakt dieses Modell + FW) | heruntergeladen, maschinell ausgewertet |
| `websocker-spec.md` | Mitschnitt am eigenen Gerät | gegen Code + Payload gegengeprüft |
| Doku-Seite | home-assistant.io/integrations/pooldose | abgerufen |
| Issues | lmaertin/python-pooldose, home-assistant/core | gelistet, #20 und #51 im Volltext |

---

## 2. Wie die heutige Lösung funktioniert

```
HA-Coordinator (600 s)
  └─ PooldoseClient.instant_values_structured()
       └─ RequestHandler.get_values_raw()
            └─ POST http://<host>/api/v1/DWI/getInstantValues   → 13.6 KB JSON
       └─ InstantValues(raw, mapping, prefix, device_id)
            └─ mapping = model_<MODEL_ID>_FW<FW_CODE>.json  (strikt, kein Fallback)
       └─ to_structured_dict() → {"sensor": {...}, "number": {...}, ...}
  └─ Entities filtern: `if description.key in coordinator.data[platform]`
```

Schreibzugriffe laufen über `POST /api/v1/DWI/setInstantValues`.
Der WebSocket wird von der Bibliothek bereits benutzt — aber nur punktuell
(`get_cloud_status()`, `get_wifi_rssi()` in `request_handler.py:549-601`), und die
HA-Integration ruft beides **nirgends** auf. `instant_values` über WebSocket wird
heute von niemandem genutzt.

---

## 3. Befunde — was heute konkret schiefläuft

### B1 — 600 s Latenz, per Design

`coordinator.py:37` → `update_interval=timedelta(seconds=600)`.
Die Doku begründet das mit „The device does not support frequent requests and may
become unstable with shorter intervals". Das ist eine Aussage über **HTTP-Requests**,
nicht über den ohnehin laufenden Push-Stream. Der WebSocket-Tick liegt bei 4,2 s
→ **Faktor ~143** in der Aktualität.

Praktische Folge: Eine Dosierreaktion (pH-Pumpe an, ORP steigt) ist im Graphen
nicht mehr auflösbar. Bei 4,2 s ist sie es.

### B2 — Firmware-Update killt die gesamte Integration

`mapping_info.py:113` baut den Dateinamen strikt: `model_{MODEL_ID}_FW{FW_CODE}.json`.
Kein Fallback, kein „nächstbeste FW".

Kette bei fehlendem Mapping:
`MappingInfo.load()` → `MAPPING_NOT_FOUND`
→ `client.instant_values()` → `UNKNOWN_ERROR` (`client.py:279`)
→ `coordinator._async_update_data()` → `UpdateFailed` (`coordinator.py:65`)
→ **alle Entities weg, Config-Entry im Retry-Loop.**

Das ist kein Theoriefall: Issue #20 ist **genau dieses Gerätemodell**
(`PDPR1H04AW100 / FW539292`) und lautet wörtlich „Missing mapping file causes setup
failure in HA". Das Mapping kam später von einem Community-Beitragenden. Beim
nächsten FW-Stand passiert dasselbe wieder.

### B3 — `visible: false` wird ignoriert → Phantom-Entities

`InstantValues.to_structured_dict()` (`instant_values.py:108-176`) prüft nur, ob ein
Roh-Eintrag existiert — das Feld `visible` wird **an keiner Stelle der Bibliothek gelesen**.

Im echten Payload dieses Modells stehen **17 von 74 Wertobjekten auf `visible: false`**.
Darunter der Chlor-Kanal:

```json
"PDPR1H04AW100_FW539292_w_1eo03t46k": {
  "visible": false, "alarm": true, "current": 0, "magnitude": ["ppm","PPM"]
}
```

Das Mapping bildet genau diesen Key auf `cl` (type `sensor`) ab, `sensor.py` hat eine
`cl`-Description → HA legt einen Chlor-Sensor an, der **dauerhaft 0 ppm** meldet,
obwohl keine Sonde angeschlossen ist. Der Nutzer kann nicht unterscheiden zwischen
„0 ppm gemessen" und „gibt es hier gar nicht".

(Offenes Issue #51 „Chlorine reading is no longer available" liegt in derselben Ecke —
Ursache dort noch nicht bestätigt.)

### B4 — Das `alarm`-Flag pro Kanal wird komplett verworfen

Jedes Wertobjekt trägt ein `alarm: true|false`. Keine der `_process_*`-Methoden in
`instant_values.py` liest es.

Im echten Payload:

```json
"…w_1eklenb23": { "visible": true, "alarm": true, "current": 311,
                  "minT": 600, "maxT": 800, "magnitude": ["mV","MV"] }
```

ORP steht auf 311 mV bei einem Sollfenster von 600–800 mV — das Gerät meldet Alarm.
In HA sieht man nur die Zahl 311. Kein `binary_sensor`, keine Attribute, nichts.

### B5 — Alarmschwellen (`minT`/`maxT`) werden nicht exponiert

`_process_number_value()` liest `minT`/`maxT` nur, wenn der Mapping-Eintrag ein
`"field"` gesetzt hat. Das Mapping dieses Modells hat bei **keinem** Eintrag `field`.
Die Schwellen 6–8 pH / 600–800 mV / 10–41 °C liegen im Payload und kommen nie an.

Die HA-Integration hat zwar `ofa_ph_lower/upper`, `ofa_orp_lower/upper`,
`ofa_cl_lower/upper` als Number-Descriptions — für dieses Modell existieren die
Mapping-Einträge aber nicht, die Entities entstehen also nie.

### B6 — Ein großer Teil des Datenstroms wird nie gemappt

Maschinell ausgewertet (echter Payload gegen Mapping-Datei):

| | Anzahl |
|---|---|
| Roh-Keys mit Modell-Prefix | 77 |
| davon im Mapping | 56 |
| **nicht gemappt** | **21** |
| davon `visible: true` (also real aktiv) | 9 |

Ungenutzt bleiben u. a. vier Timer mit `visible: true` (`w_1eo1u7pjf` = 20 s,
`w_1eo1ucpcr` = 360 s, `w_1eo1uen7i` = 31 s, `w_1eo1uep02` = 360 s), zwei aktive
Dosiermodi (`w_1eklj12vv`, `w_1eo1v3q21`, beide `TIMED`) und acht Statusflags
`w_1fakp…/w_1fakq…`.

### B7 — 9 gemappte Werte haben gar keine HA-Entity

Diff Mapping ↔ `EntityDescription`-Listen der HA-Integration, für dieses Modell:

| Plattform | im Mapping, aber keine Entity |
|---|---|
| `number` | `time_on_ph_dosing`, `time_on_orp_dosing`, `time_on_cl_dosing` |
| `binary_sensor` | `alarm_cl_too_low`, `cl_level_alarm` |
| `sensor` | `cl_calibration_type/offset/slope`, `ofa_cl_time` |

Die `time_off_*_dosing` sind da, die `time_on_*_dosing` fehlen — d. h. man kann die
Aus-Zeit der Dosierung in HA setzen, die Ein-Zeit nicht. Von 64 Mapping-Einträgen
landen effektiv **55** als Entity.

### B8 — Jeder Schreibzugriff kostet einen zusätzlichen Vollabruf

`PooldoseClient.set_number/set_switch/set_select` (`client.py:315-338`) rufen jeweils
zuerst `self.instant_values()` — also einen kompletten `POST getInstantValues` (13.6 KB) —
nur um danach `set_value` abzusetzen. **Ein Klick = 2 HTTP-Requests.**
Genau das Verhalten, das die 600 s eigentlich vermeiden sollen.

Danach setzt `number.py:187` den Wert optimistisch (`_attr_native_value = value`)
ohne `async_request_refresh()`. Weicht die Geräteantwort ab (Rundung, Ablehnung,
Step-Korrektur), zeigt HA bis zu **10 Minuten** einen falschen Wert an.

### B9 — Ein einzelner fehlgeschlagener Poll macht alles unavailable

`_async_update_data()` wirft bei jedem Nicht-SUCCESS `UpdateFailed`. Der
`DataUpdateCoordinator` setzt dann `last_update_success = False`, und
`CoordinatorEntity.available` wird für **alle** Entities `False`. Nächster Versuch:
in bis zu 600 s.

Die Doku verspricht hier etwas anderes: „The system caches values for up to 300
seconds during temporary unresponsiveness". Im Code existiert dieser Puffer nicht.
`get_values_raw()` gibt bei Netzwerkfehlern zwar `RequestStatus.LAST_DATA` mit den
letzten Daten zurück (`request_handler.py:415`) — aber `client.instant_values()`
verwirft das eine Zeile später (`if status != RequestStatus.SUCCESS … return status, None`).
`LAST_DATA` kommt in `homeassistant/components/pooldose/` **nirgends** vor.
Der Last-Data-Fallback ist toter Code.

### B10 — Robustheits-Detail in der Bibliothek

`RequestHandler._get_websocket_data()` (`request_handler.py:562`) hat ein
`while True: msg = await ws.recv()` **ohne Timeout**. Kommt das erwartete Topic nie
(`wdp_status` ist laut Spec „selten/initial"), hängt der Task unbegrenzt. Für uns nur
relevant, falls wir diesen Codepfad wiederverwenden — tun wir nicht.

---

## 4. Prüfung der Spec — was bestätigt ist, was nicht

### Bestätigt

| Aussage der Spec | Beleg |
|---|---|
| Datenstruktur `devicedata.<SERIAL>_DEVICE.<MODEL>_FW<FW>_<hash>` | echter HTTP-Payload hat exakt diese Form → **WS- und HTTP-Payload sind unterhalb von `devicedata` strukturgleich** |
| Wertobjekt-Felder `current/set/absMin/absMax/minT/maxT/alarm/visible/magnitude/resolution` | alle im Payload vorhanden; `ph_target` trägt `current: 7.4` **und** `set: 7.4` |
| „Manche Einträge sind ein blanker Boolean" | bestätigt: `w_1emtltkel`, `w_1eklft47q`, `w_1eklft5qt` — ausgerechnet alle drei Switches |
| Sprechende Keys neben dem Hash-Schema | `Elapsed_PowerON_Delay`, `Elapsed_FlowON_Delay` im Payload |
| `visible: false` als Filter, ppm-Kanal mit Dauer-Alarm | exakt reproduziert (siehe B3) |
| „Keys nicht raten, `python-pooldose` als Quelle der Wahrheit" | Mapping für dieses Modell existiert und deckt 56 von 77 Roh-Keys ab |
| `pooldose --host … --analyze` | CLI-Flag existiert (`__main__.py:279`), zusätzlich `--analyze-all` für versteckte Widgets |
| Modell/FW/Serial aus den Daten-Keys ableitbar | `DeviceAnalyzer._extract_device_info()` macht genau das per Regex — **wir brauchen für den Betrieb keinen HTTP-Call, um den Prefix zu kennen** |

### Nicht bestätigt / zu korrigieren

**„Der WebSocket erzeugt weniger Last als der bisherige Poll."**
Das ist so nicht belegt und in einer Dimension vermutlich falsch.

- *Auf dem Dosierregler* (Modbus-Pfad): plausibel. `deviceInfo.modbus_status: "on"`
  und der feste 4,2-s-Scheduler sprechen dafür, dass das WiFi-Modul unabhängig von
  Zuhörern pollt. Ein Listener erzeugt dort **keine** Zusatzlast. Der HTTP-Poll
  dagegen löst mindestens Request-Handling aus.
- *Auf dem WiFi-Modul*: Zuhören kostet mehr, nicht weniger. Rechnung mit der echten
  Payload-Größe (13,6 KB pro Vollzyklus):

  | | Requests/Tag | Bytes/Tag |
  |---|---|---|
  | HTTP-Poll 600 s | 144 | ~1,9 MB |
  | WS-Stream 4,2 s | 0 (1 Verbindung) | ~267 MB |

  Das sind ~20.570 Serialisierungs- und Sendevorgänge pro Tag auf einem
  Embedded-Stack. Die in der Spec selbst beobachteten Tick-Aussetzer
  (16 von 24 Zyklen angekommen) sind ein Indiz, dass das Modul dabei nicht langweilt.

**Offen und messbar:** sendet das Gerät den Stream auch, wenn **niemand** verbunden
ist? Wenn ja, ist Zuhören tatsächlich gratis. Wenn nein, kaufen wir Aktualität mit
Modul-Last. Das lässt sich messen (Abschnitt 8) und sollte vor dem Rollout geklärt sein.

**Ehrliche Formulierung des Nutzens:** Wir gewinnen **Latenz** und vermeiden
**Request-Spitzen** auf dem HTTP-Server des Geräts. „Schont das Gerät" gilt belegbar
nur für den Regler, nicht pauschal.

---

## 5. Konzept der neuen Integration

### 5.1 Grundentscheidungen

| Entscheidung | Begründung |
|---|---|
| **Eigene Domain `pooldose_live`**, nicht `pooldose` | Ein Custom-Component mit Domain `pooldose` überschreibt die Core-Integration global. Eigene Domain → beide parallel installierbar, sauberer Vergleich, kein Reibungspunkt mit HA-Updates. |
| `iot_class: local_push` | fachlich korrekt und macht den Unterschied nach außen sichtbar |
| **Mapping-JSONs vendorn** statt `python-pooldose` als Requirement | siehe 5.2 — ausführlich begründet |
| **v1 liest nur über WS, schreibt über HTTP** | Das WS-Schreibformat ist unbekannt und die Spec warnt zu Recht: ein falsch geformter Frame verstellt Parameter einer realen Dosieranlage. `setInstantValues` ist bekannt und erprobt. |
| **Genau eine WS-Verbindung pro Gerät** | Spec-Empfehlung; Verbindung lebt im Config-Entry, nicht pro Entity |

### 5.2 Umgang mit `python-pooldose`: Mappings kopieren, Code selbst schreiben

Drei Optionen standen zur Wahl: (a) als Requirement einbinden, (b) Teile übernehmen,
(c) komplett eigenständig. Entscheidung: **(b), eng gefasst — nur die Mapping-JSONs.**

**Gegen (a) — Einbinden als Requirement:**

1. *Was wir bräuchten, ist kein Public API.* `docs/api-reference.md` dokumentiert
   `PooldoseClient`, `RequestStatus` und von `InstantValues` nur das Dict-Interface
   plus die Setter. Der Konstruktor
   `InstantValues(device_data, mapping, prefix, device_id, request_handler)` ist
   nirgends dokumentiert und wird nur bibliotheksintern aufgerufen
   (`client.py:288`, `mock_client.py:211`). Genau den bräuchten wir, um
   WS-Snapshots hineinzureichen — der dokumentierte Weg `client.instant_values()`
   holt zwingend per HTTP. Dasselbe gilt für `RequestHandler.set_value` beim
   Schreiben. Wir würden uns an zwei interne Signaturen hängen.
2. *Version-Pin-Konflikt.* Core pinnt `python-pooldose==0.9.6`. Laufen beide
   Integrationen im selben Env und wir pinnen abweichend, arbeiten die
   Requirement-Checks gegeneinander. Wir müssten dauerhaft Cores Pin folgen — und
   verlieren damit den einzigen Grund für eine Abhängigkeit.
3. *Wir brauchen ohnehin andere Semantik.* `visible`, `alarm`, `minT/maxT`,
   `set` vs. `current`, Raw-Fallback — nichts davon liefert `InstantValues`. Wir
   würden die Bibliothek einbinden und die Hälfte ihrer Verarbeitung umgehen.

**Gegen (c) — alles selbst:** Die 1.302 Zeilen Mapping-JSON sind der eigentliche
Wert des Repos. Die Hash-Keys sind opak und nicht herleitbar; die Tabellen sind von
Hand aus Debug-Dumps entstanden (unser Modell: Community-Beitrag in Issue #20).
Nachbauen wäre Wertvernichtung.

**Aufteilung:**

| Bestandteil | Herkunft |
|---|---|
| 6 Mapping-JSONs + `MODEL_ALIASES` | kopiert, MIT, Attribution im Dateikopf und in der README |
| WS-Transport, Reassembly, Watchdog | eigen — existiert dort nicht |
| Decoder Roh → `Channel` | eigen, ~150–200 Zeilen; weniger Aufwand als `InstantValues` zu beugen |
| Mapping-Loader mit FW-Fallback | eigen — der dortige ist strikt (Befund B2) |
| Schreiben `POST setInstantValues` | eigen, ~30 Zeilen: `{device_id: {full_key: [{"value": v, "type": "NUMBER"}]}}` |

Damit: keine Runtime-Dependency, kein Konflikt mit der Core-Integration, keine
Kopplung an undokumentierte Signaturen.

*Preis:* Die Mappings driften. Gegenmaßnahme: CI-Job, der wöchentlich gegen Upstream
diffed und bei Abweichung ein Issue öffnet — plus der Raw-Modus (5.5), der neue
Geräte ohnehin besser abfängt als Upstream.

*Bedingung für einen Wechsel zu (a):* wenn `python-pooldose` den
`InstantValues`-Konstruktor als API dokumentiert und die Konstruktion aus einem
rohen Dict ohne HTTP zusagt. Das ist ein sinnvoller Upstream-Vorschlag von uns —
dann bekäme auch die Bibliothek einen WS-Pfad.

### 5.3 Schichten

```
┌─ transport/socket.py ──────────────────────────────────────────┐
│  eine ws://<host>:1334 Verbindung                              │
│  · Reassembly nach progressInfo (offset==1 → Puffer leeren)    │
│  · Watchdog 30 s ohne Frame → Reconnect                        │
│  · Backoff 2 s → 60 s, exponentiell                            │
│  · unbekannte Topics werden verworfen, nicht geloggt-geflutet  │
│  → liefert vollständige Snapshots als Callback                 │
└────────────────────────────────────────────────────────────────┘
┌─ decode/channels.py ───────────────────────────────────────────┐
│  Roh-Dict → dict[hash, Channel]                                │
│  Channel = current | set | absMin absMax | minT maxT |         │
│            resolution | unit | alarm | visible | raw           │
│  · Prefix aus den Keys selbst ableiten (kein HTTP nötig)       │
│  · bare-bool-Einträge → Channel(current=bool)                  │
│  · Label-Dekodierung per removeprefix (nicht rsplit)           │
└────────────────────────────────────────────────────────────────┘
┌─ naming/mapping.py ────────────────────────────────────────────┐
│  hash → sprechender Name, aus vendorten JSONs                  │
│  · exakter Treffer model+fw                                    │
│  · sonst: gleiches Modell, andere FW  → laden + Warnung        │
│  · sonst: Raw-Modus (siehe 5.5)                                │
└────────────────────────────────────────────────────────────────┘
┌─ coordinator.py ───────────────────────────────────────────────┐
│  DataUpdateCoordinator(update_interval=None)                   │
│  Socket-Callback → decode → Throttle → async_set_updated_data  │
└────────────────────────────────────────────────────────────────┘
┌─ Plattformen: sensor / binary_sensor / number / select / switch│
└────────────────────────────────────────────────────────────────┘
```

### 5.4 Was die Befunde aus §3 löst

| Befund | Lösung |
|---|---|
| B1 Latenz | Push statt Poll. `update_interval=None`, `async_set_updated_data()` aus dem WS-Callback. |
| B2 FW-Cliff | Dreistufiger Mapping-Fallback (5.5). Ein FW-Update degradiert die Namen, tötet nicht die Integration. |
| B3 `visible:false` | Kanäle mit `visible: false` erzeugen **keine** Entity. Ändert sich `visible` zur Laufzeit (Sonde nachgerüstet), erscheint die Entity beim nächsten Reload — optional als Repair-Issue melden. |
| B4 `alarm` | Pro Kanal mit `alarm`-Feld ein `binary_sensor` (device_class `problem`, `entity_registry_enabled_default=False`), plus `alarm` als Attribut am Hauptsensor. |
| B5 Schwellen | `minT`/`maxT` als Attribute am Sensor und optional als `number`-Entities (Kategorie `config`, per Default deaktiviert). Kommen generisch aus dem Payload, kein Mapping-Eintrag nötig. |
| B6/B7 tote Werte | Alles, was `visible: true` ist und einen Wert hat, wird zur Entity — gemappte Namen sauber, ungemappte als `raw_<hash>`, per Default deaktiviert. Nichts geht mehr verloren. |
| B8 Schreib-Overhead | Direkt `POST setInstantValues` mit dem Prefix+Key aus dem eigenen Snapshot. **Kein Vorab-GET.** Keine optimistische Anzeige nötig — der nächste Tick bestätigt in ~4 s. Bleibt die Bestätigung aus, wird der Wert zurückgesetzt und die Aktion als fehlgeschlagen gemeldet. |
| B9 Unavailable-Flapping | Verfügbarkeit hängt an der WS-Liveness (letzter Frame < Watchdog-Fenster), nicht an einem Einzelabruf. Kurze Aussetzer (Spec: bis 16 s beobachtet) führen zu nichts. |

### 5.5 Mapping-Fallback in drei Stufen

1. **Exakt** `model_<MODEL>_FW<FW>.json` → volle sprechende Namen, alles wie gewohnt.
2. **Modell passt, FW nicht** → nächstliegende FW-Datei desselben Modells laden.
   Hash-Keys sind erfahrungsgemäß FW-stabil; unbekannte Hashes fallen in Stufe 3.
   Sichtbare Warnung im Log + Repair-Issue „Mapping für FW X fehlt".
3. **Raw-Modus** → Entities direkt aus dem Payload:
   - Typ aus dem Wertobjekt raten: `set` vorhanden → `number`; `comboitems` → `select`;
     `current` ist `"O"/"F"` oder bool → `binary_sensor`; sonst `sensor`.
   - Einheit aus `magnitude[0]`, Grenzen aus `absMin/absMax`, Schritt aus `resolution`.
   - Name `raw_<hash>`, `entity_registry_enabled_default=False`.

Damit ist der Fall aus Issue #20 (neues Gerät, kein Mapping) kein Totalausfall mehr,
sondern ein Komfortverlust — und der Nutzer kann sofort Daten sehen und daraus ein
Mapping beisteuern.

### 5.6 Entprellung / Recorder

Das ist bei 4,2 s **die** kritische Stelle. Bei ~40 Entities und einem Write pro Tick
wären das ~9,5 State-Writes/s. HA feuert bei jedem `async_write_ha_state()` ein
`state_changed`-Event, und der Recorder schreibt pro Event eine Zeile — auch bei
identischem Wert. Ungebremst: dreistellige Millionen Zeilen pro Jahr. Die Zahl
„~20 Mio." in der Spec ist deutlich zu niedrig angesetzt.

Zwei Bremsen, beide nötig:

1. **Koordinator-Ebene:** `async_set_updated_data()` nur, wenn sich mindestens ein
   Kanal relevant geändert hat.
2. **Entity-Ebene:** `_handle_coordinator_update()` überschreiben und
   `async_write_ha_state()` überspringen, wenn sich der *eigene* Wert nicht geändert hat.
   Ohne das schreiben bei jeder Änderung eines beliebigen Kanals alle 40 Entities.

Relevanzkriterium pro Kanal:
- numerisch: `abs(neu - alt) >= resolution` (fällt `resolution` als `"NA"` an: jede Änderung)
- sonst: Wertänderung
- zusätzlich **Heartbeat**: alle N Minuten (Default 5, konfigurierbar) einmal
  durchschreiben, damit Verläufe im Recorder nicht auseinanderreißen

Das ergibt für pH/ORP realistisch wenige Writes pro Minute statt 14/Minute.

### 5.7 Setup-Ablauf

```
config_flow: Host eingeben
  → einmalig HTTP: /api/v1/debug/config, /network/wifi/getStation, /network/info/getInfo
      (Name, Seriennummer, FW_REL, FW_CODE, MODEL_ID, IP → Device-Registry)
  → unique_id = Seriennummer
  → Testverbindung ws://<host>:1334, auf einen vollständigen instant_values-Zyklus warten
      (Timeout 30 s — deckt die beobachtete längste Lücke von 16 s ab)
  → Entry anlegen

Laufzeit: ausschließlich WebSocket. Kein periodischer HTTP-Verkehr.
HTTP nur noch bei Schreibzugriffen und beim Reload.
```

DHCP-Discovery (`hostname: kommspot`) lässt sich von der Core-Integration übernehmen.
Achtung: läuft beides parallel, konkurrieren die Flows um dasselbe Gerät — für die
Testphase eine bewusste Entscheidung, im Zielbild wird die Core-Integration deaktiviert.

### 5.8 Diagnostics

`diagnostics.py` soll den **kompletten letzten Roh-Snapshot** ausgeben (Serial und
WLAN-Daten redigiert), dazu Tick-Statistik: Frames/Minute, verpasste Ticks,
Reconnects, längste Lücke. Genau das Material, aus dem heute die Mapping-Beiträge
in Issue #20 entstanden sind — dann ohne CLI-Gefummel.

---

## 6. Vorteile gegenüber heute — nüchtern

**Belegt:**
- Aktualität 4,2 s statt 600 s (Faktor ~143). Direkt aus dem Tick der Spec.
- Kein periodischer HTTP-Request mehr (heute 144/Tag), keine Request-Spitzen auf dem Webserver des Geräts.
- Schreibvorgänge: 1 HTTP-Request statt 2, und Bestätigung nach ~4 s statt bis zu 600 s.
- Ein FW-Update legt die Integration nicht mehr lahm (B2).
- ~19 Werte mehr aus demselben Datenstrom (B6/B7), plus `alarm` und `minT/maxT` (B4/B5).
- Keine Phantom-Sensoren mehr für nicht bestückte Kanäle (B3).
- Kurze Aussetzer führen nicht mehr zu „alles unavailable" (B9).

**Plausibel, aber unbewiesen:**
- Geringere Last auf dem Dosierregler. Argument: Modbus läuft im Eigentakt.
  Das ist eine Herleitung aus `modbus_status: "on"`, keine Messung.

**Kosten, ehrlich:**
- Dauerhaft offene TCP-Verbindung zum Gerät.
- ~267 MB/Tag statt ~1,9 MB/Tag im LAN.
- Deutlich mehr Serialisierungsarbeit auf dem WiFi-Modul.
- Entprellung ist Pflicht, nicht Kür — ohne sie ruiniert man die Recorder-DB.
- Web-UI-Tabs im Browser sind ab jetzt ein echter Störfaktor (Extra-Client).

---

## 7. Phasenplan

| Phase | Inhalt | Ergebnis |
|---|---|---|
| **P0** | Standalone-Logger: WS mitschneiden, Ticks/Lücken/Reconnects zählen, Snapshots als JSONL | Datenbasis + Messungen aus §8 |
| **P1** | Transport + Decoder + Mapping-Loader, ohne HA | `python -m pooldose_live.probe --host …` zeigt aufgelöste Kanäle |
| **P2** | HA-Skelett: manifest, config_flow, coordinator, `sensor` + `binary_sensor`, read-only | Erste Live-Werte in HA, parallel zur Core-Integration |
| **P3** | Entprellung, Verfügbarkeitslogik, Diagnostics | Recorder-tauglich, alltagstauglich |
| **P4** | Schreiben: `number`, `select`, `switch` über HTTP `setInstantValues` | Funktionsgleichstand mit Core |
| **P5** | Raw-Modus + FW-Fallback, Repair-Issues, Übersetzungen (de/en) | Kein FW-Cliff mehr |
| **P6** | HACS-Konformität: `hacs.json`, `version` im Manifest, Release-Tags, README | Installierbar über HACS |
| **P7** | Optional: WS-Schreibformat erforschen — **getrennt, mit Bedacht, nicht am Produktivgerät** | offen |

Rückfluss nach oben: B3 (`visible`), B4 (`alarm`), B7 (fehlende Descriptions) und
B9 (toter `LAST_DATA`-Pfad) sind eigenständige, kleine Fixes für `python-pooldose`
bzw. die Core-Integration. Die sollten wir als Issues/PRs einreichen, unabhängig
von diesem Projekt — sie helfen auch allen, die beim Polling bleiben.

---

## 8. Zu messen, bevor wir bauen (P0)

1. **Sendet das Gerät ohne Zuhörer?** Traffic am Switch/Router beobachten, WS-Client
   aus. Entscheidet, ob „gerätefreundlich" hält.
2. **Tick-Ausfallrate** bei 0 / 1 / 2 gleichzeitigen Clients, je 30 min.
   Die Spec hat 16/24 Zyklen bei einem Client gesehen — reproduzierbar?
3. **Antwortzeit von `GET /`** und `POST getInstantValues` mit und ohne aktiven
   WS-Listener. Zeigt, ob der Listener den HTTP-Server ausbremst.
4. **Verhalten bei Reconnect:** kommt sofort ein `offset:1`-Frame oder mitten im Zyklus?
   Bestimmt, ob die Puffer-Regel reicht.
5. **Ändert sich `visible` zur Laufzeit?** Über 24 h beobachten. Entscheidet, ob
   Entity-Erzeugung nur beim Setup oder dynamisch laufen muss.
6. **Kommen Werte außerhalb des Ticks**, wenn man am Gerät etwas verstellt?
   Entscheidet, wie schnell Schreibbestätigungen realistisch sind.

---

## 9. Risiken

| Risiko | Wirkung | Gegenmaßnahme |
|---|---|---|
| WiFi-Modul kommt mit Dauerverbindung nicht klar | Tick-Aussetzer, Reboots | P0-Messung 2/3 vor dem Ausbau; Watchdog + Backoff; harte Ein-Verbindungs-Regel |
| Recorder-DB läuft voll | HA wird langsam | Entprellung ab P3, nicht später; Default-Heartbeat konservativ |
| Reassembly liefert gemischte Zyklen | falsche Werte | `offset==1` leert den Puffer; nur bei `offset==total` publizieren; Zyklus-Zähler in Diagnostics |
| Beide Integrationen parallel aktiv | 2 Clients + Polling, doppelte Entities | Getrennte Domain macht es sichtbar; README weist auf Deaktivierung hin |
| Vendorte Mappings veralten | neue Modelle fehlen | Sync-Skript + CI-Job, der gegen Upstream diffed |
| Schreiben ohne Authentifizierung | falscher Frame verstellt echte Dosierparameter | v1 schreibt nur über die bekannte HTTP-API; WS-Schreiben erst in P7, nie am Produktivgerät erforscht |

---

## 10. Offene Punkte

- WS-Schreibformat: unbekannt, bewusst nicht getestet (bleibt so bis P7).
- Topics `wdp_status`, `wifi_status` inhaltlich unklar — `wdp_status.connection`
  (Cloud) und `wifi_station.rssi` sind aus `python-pooldose` bekannt und lassen sich
  gratis aus dem laufenden Stream mitnehmen, statt wie dort je Abfrage eine eigene
  Verbindung zu öffnen.
- Verhalten bei mehreren Clients nicht systematisch getestet.
- Ob `visible` auch Sollwert-Kanäle betrifft, die man trotzdem schreiben können soll,
  ist noch offen — im vorliegenden Payload sind alle drei Switch-Kanäle bare booleans
  ohne `visible`-Feld.
