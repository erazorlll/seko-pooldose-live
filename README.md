# SEKO PoolDose (Live)

A [Home Assistant](https://www.home-assistant.io/) integration for the **SEKO PoolDose** pool/spa dosing controller, with **live updates** instead of a 10-minute wait.

Unofficial and community-maintained — not affiliated with SEKO.

## Why use this instead of the official integration?

The official `pooldose` integration polls the device's HTTP API every 600 seconds, so a change in pH, temperature, or an alarm can take up to 10 minutes to show up in Home Assistant. This integration instead listens to the device's own local WebSocket stream, which it already updates roughly every 4 seconds — so your dashboard and automations react in seconds, not minutes.

It runs **side by side** with the official integration if you want (it uses its own domain), so you can compare or switch at your own pace.

## Requirements

- A SEKO PoolDose device reachable on your local network (tested against a PoolDose Double Spa, model `PDPR1H04AW100`; other PoolDose models should work too — see [Unknown models & firmware](#unknown-models--firmware) below)
- Home Assistant with [HACS](https://hacs.xyz/) installed
- No cloud account, API key, or extra setup on the device itself — it just needs to be on your network

## Installation

This integration isn't in the HACS default store yet, so add it as a custom repository:

1. HACS → the **⋮** menu (top right) → **Custom repositories**
2. Repository: `https://github.com/erazorlll/seko-pooldose-live`, Type: **Integration**
3. Find **SEKO PoolDose (Live)** in HACS and install it
4. Restart Home Assistant
5. **Settings → Devices & Services → Add Integration**, search for **SEKO PoolDose (Live)**
6. Enter the IP address or hostname of your PoolDose device

That's it — no separate packages to install, everything the integration needs is bundled.

## What you get

- **Sensors** for pH, ORP/chlorine, temperature, and other readings your device reports
- **Binary sensors** for alarms and status flags (e.g. standby)
- **Adjustable setpoints** (e.g. target pH) as `number` entities
- **Options** (e.g. water meter unit) as `select` entities
- **Switches** (e.g. pause dosing) where the device supports them

Which entities you get depends on your specific model and firmware — see below.

Entity availability briefly pauses if the device goes quiet for a few minutes (this is normal PoolDose behavior, not a connection problem) and resumes automatically once fresh data arrives.

## Unknown models & firmware

Entity names are resolved from a mapping table specific to your device's model and firmware. If your exact combination isn't in that table yet, you'll still get working entities — just with less readable, hash-based names — and Home Assistant will show a **Repair** notification (Settings → System → Repairs) explaining what happened and how to help improve it (usually just sharing a diagnostics export on the [issue tracker](https://github.com/erazorlll/seko-pooldose-live/issues)).

## Diagnostics

Each configured device supports Home Assistant's built-in diagnostics download (device page → **Download diagnostics**), useful for troubleshooting or reporting an issue — the serial number is redacted automatically.

## Known limitations

- Writing values (setpoints, switches, options) has been thoroughly tested in isolation but not yet extensively on live hardware — go carefully with unfamiliar values, particularly dosing-related ones
- The device's unique ID is currently based on its network host, not its serial number

## Contributing

Bug reports, mapping contributions for new models/firmware, and pull requests are welcome — see the [issue tracker](https://github.com/erazorlll/seko-pooldose-live/issues).

## Credits

Channel name mappings are derived from [lmaertin/python-pooldose](https://github.com/lmaertin/python-pooldose) (MIT license) — see [`ATTRIBUTION.md`](src/pooldose_live/mappings/ATTRIBUTION.md) for details.

## License

MIT — see [`LICENSE`](LICENSE).
