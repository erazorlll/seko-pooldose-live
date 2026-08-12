"""CLI: python -m pooldose_live.probe --host <ip>

Verbindet sich, leitet Modell/FW-Code direkt aus den ersten empfangenen
Daten-Keys ab (kein HTTP-Call nötig, siehe Konzept §4), lädt die passende
Mapping-Tabelle (oder fällt in den Raw-Modus, §5.5) und zeigt die
aufgelösten Kanäle. Das ist der P1-Abschlusspunkt aus dem Phasenplan
(Konzept §7): "python -m pooldose_live.probe --host … zeigt aufgelöste Kanäle".

Rein lesend - sendet nie einen Frame, öffnet nur eine Verbindung.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime

from pooldose_live.channels import Channel, decode_devicedata, detect_prefix
from pooldose_live.mapping import ModelMapping, ResolvedChannel, load as load_mapping
from pooldose_live.transport import (
    DEFAULT_CONNECTION_WATCHDOG,
    DEFAULT_PORT,
    DEFAULT_STALENESS_TIMEOUT,
    PooldoseTransport,
    TransportEvent,
)


def _clock() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _format_value(rc: ResolvedChannel) -> str:
    unit = rc.channel.unit
    if rc.type == "number" and (rc.min is not None or rc.max is not None):
        bounds = f"  [{rc.min}..{rc.max}" + (f", Schritt {rc.step}]" if rc.step is not None else "]")
    else:
        bounds = ""
    unit_str = f" {unit}" if unit else ""
    return f"{rc.display}{unit_str}{bounds}"


def print_table(mapping: ModelMapping, resolved: list[ResolvedChannel], *, show_invisible: bool) -> None:
    visible = [rc for rc in resolved if show_invisible or rc.channel.visible]
    visible.sort(key=lambda rc: (rc.source != "mapping", rc.name))

    print("=" * 100)
    print(f"Modell {mapping.model_id}  FW {mapping.fw_code}  "
          f"Mapping: {mapping.status.value}"
          + (f" (genutzt: FW{mapping.matched_fw})" if mapping.matched_fw and mapping.matched_fw != mapping.fw_code.removeprefix('FW') else ""))
    n_table, n_hashes = mapping.coverage
    n_named = sum(1 for rc in resolved if rc.source == "mapping")
    n_raw = sum(1 for rc in resolved if rc.source == "raw")
    n_hidden = sum(1 for rc in resolved if not rc.channel.visible)
    print(f"Kanäle: {len(resolved)} gesamt, {n_named} benannt, {n_raw} raw-Fallback, "
          f"{n_hidden} visible=false" + ("" if show_invisible else " (ausgeblendet, --show-invisible zeigt sie)"))
    print("=" * 100)
    print(f"{'Name':38} {'Typ':14} {'Wert':30} {'Kanal':16} Quelle")
    print("-" * 100)
    for rc in visible:
        alarm = "  ⚠ ALARM" if rc.channel.alarm else ""
        print(f"{rc.name:38} {rc.type:14} {_format_value(rc):30} {rc.channel.hash:16} {rc.source}{alarm}")
    print("=" * 100)


async def run(args: argparse.Namespace) -> int:
    transport = PooldoseTransport(
        args.host, args.port,
        connection_watchdog=args.connection_watchdog,
        staleness_timeout=args.staleness_timeout,
    )

    loop = asyncio.get_event_loop()
    deadline = loop.time() + args.duration if args.duration else None

    print(f"Verbinde zu ws://{args.host}:{args.port}/ ...")
    print(f"Verbindungs-Watchdog {args.connection_watchdog:.0f}s   "
          f"Staleness-Timeout {args.staleness_timeout:.0f}s\n")

    # Kein `async for` direkt über transport.events(): das Gerät kann laut
    # Konzept §8.2/§8.3 minutenlang keinen einzigen Frame schicken, ohne die
    # Verbindung zu trennen. --duration muss auch dann greifen, wenn gar
    # kein Event ankommt - also den Iterator explizit mit einem Timeout
    # abfragen statt reaktiv im Schleifenkörper zu prüfen (derselbe Fehler
    # steckte anfangs in tools/ws_probe.py, siehe dessen Commit-Historie).
    events = transport.events()
    try:
        return await _consume(events, args, deadline, loop)
    finally:
        await events.aclose()


async def _consume(events, args: argparse.Namespace, deadline: float | None, loop) -> int:
    mapping: ModelMapping | None = None
    last_display: dict[str, object] = {}
    printed_table = False

    while True:
        if deadline is not None:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                event = await asyncio.wait_for(events.__anext__(), timeout=remaining)
            except asyncio.TimeoutError:
                break
        else:
            event = await events.__anext__()

        if event.kind == "connected":
            print(f"[{_clock()}] verbunden")
        elif event.kind == "disconnected":
            retry_note = f" (nächster Versuch in {event.retry_in:.0f}s)" if event.retry_in else ""
            print(f"[{_clock()}] Verbindung weg: {event.reason}{retry_note}")
        elif event.kind == "watchdog":
            print(f"[{_clock()}] Watchdog: {event.reason}")
        elif event.kind == "stale":
            print(f"[{_clock()}] ⚠ STALE: seit {event.since:.0f}s kein vollständiger "
                  f"instant_values-Zyklus mehr (Verbindung bleibt bestehen)")
        elif event.kind == "fresh":
            print(f"[{_clock()}] wieder aktuell")
        elif event.kind == "snapshot":
            channels: dict[str, Channel] = decode_devicedata(event.devicedata or {})

            if mapping is None:
                prefix = detect_prefix(event.devicedata or {})
                if prefix is None:
                    print(f"[{_clock()}] Konnte Modell/FW nicht aus den Daten-Keys ableiten, "
                          "überspringe Snapshot")
                    continue
                model_id, fw_code = prefix
                mapping = load_mapping(model_id, fw_code)

            resolved = mapping.resolve_all(channels)

            if not printed_table:
                print_table(mapping, resolved, show_invisible=args.show_invisible)
                printed_table = True
                if args.once:
                    return 0
                print(f"\n[{_clock()}] Live-Änderungen (Strg+C zum Beenden):")
                last_display = {rc.name: rc.display for rc in resolved}
            else:
                changed = [rc for rc in resolved if last_display.get(rc.name) != rc.display]
                for rc in changed:
                    old = last_display.get(rc.name)
                    print(f"  [{event.t:8.1f}s] {rc.name:38} {old!r} -> {rc.display!r}")
                last_display = {rc.name: rc.display for rc in resolved}

    return 0


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(
        prog="pooldose_live.probe",
        description="Verbindet zur PoolDose, zeigt aufgelöste Kanäle (P1, ohne HA).")
    parser.add_argument("--host", required=True, help="IP oder Hostname des Geräts")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--connection-watchdog", type=float, default=DEFAULT_CONNECTION_WATCHDOG)
    parser.add_argument("--staleness-timeout", type=float, default=DEFAULT_STALENESS_TIMEOUT)
    parser.add_argument("--once", action="store_true",
                        help="Nach der ersten vollständigen Tabelle beenden")
    parser.add_argument("--duration", type=float, default=0,
                        help="Sekunden Gesamtlaufzeit; 0 = bis Strg+C oder --once")
    parser.add_argument("--show-invisible", action="store_true",
                        help="Auch Kanäle mit visible=false anzeigen (Konzept B3)")

    args = parser.parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
