"""CLI: python -m pooldose_live.probe --host <ip>

Connects, derives the model/FW code directly from the first received data
keys (no HTTP call needed, see concept §4), loads the matching mapping
table (or falls back to raw mode, §5.5), and shows the resolved channels.
This is the P1 completion criterion from the phase plan (concept §7):
"python -m pooldose_live.probe --host … shows resolved channels".

Read-only - never sends a frame, only opens a connection.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime

from .channels import Channel, decode_devicedata, detect_prefix
from .mapping import ModelMapping, ResolvedChannel, load as load_mapping
from .transport import (
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
        bounds = f"  [{rc.min}..{rc.max}" + (f", step {rc.step}]" if rc.step is not None else "]")
    else:
        bounds = ""
    unit_str = f" {unit}" if unit else ""
    return f"{rc.display}{unit_str}{bounds}"


def print_table(mapping: ModelMapping, resolved: list[ResolvedChannel], *, show_invisible: bool) -> None:
    visible = [rc for rc in resolved if show_invisible or rc.channel.visible]
    visible.sort(key=lambda rc: (rc.source != "mapping", rc.name))

    print("=" * 100)
    print(f"Model {mapping.model_id}  FW {mapping.fw_code}  "
          f"Mapping: {mapping.status.value}"
          + (f" (using: FW{mapping.matched_fw})" if mapping.matched_fw and mapping.matched_fw != mapping.fw_code.removeprefix('FW') else ""))
    n_table, n_hashes = mapping.coverage
    n_named = sum(1 for rc in resolved if rc.source == "mapping")
    n_raw = sum(1 for rc in resolved if rc.source == "raw")
    n_hidden = sum(1 for rc in resolved if not rc.channel.visible)
    print(f"Channels: {len(resolved)} total, {n_named} named, {n_raw} raw fallback, "
          f"{n_hidden} visible=false" + ("" if show_invisible else " (hidden, --show-invisible reveals them)"))
    print("=" * 100)
    print(f"{'Name':38} {'Type':14} {'Value':30} {'Channel':16} Source")
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

    print(f"Connecting to ws://{args.host}:{args.port}/ ...")
    print(f"Connection watchdog {args.connection_watchdog:.0f}s   "
          f"Staleness timeout {args.staleness_timeout:.0f}s\n")

    # No `async for` directly over transport.events(): per concept
    # §8.2/§8.3 the device can go minutes without sending a single frame,
    # without the connection dropping. --duration has to take effect even
    # when no event arrives at all - so poll the iterator explicitly with a
    # timeout instead of checking reactively in the loop body (the same bug
    # originally lived in tools/ws_probe.py, see its commit history).
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
            print(f"[{_clock()}] connected")
        elif event.kind == "disconnected":
            retry_note = f" (next attempt in {event.retry_in:.0f}s)" if event.retry_in else ""
            print(f"[{_clock()}] connection lost: {event.reason}{retry_note}")
        elif event.kind == "watchdog":
            print(f"[{_clock()}] watchdog: {event.reason}")
        elif event.kind == "stale":
            print(f"[{_clock()}] ⚠ STALE: no complete instant_values cycle for "
                  f"{event.since:.0f}s (connection still up)")
        elif event.kind == "fresh":
            print(f"[{_clock()}] fresh again")
        elif event.kind == "snapshot":
            channels: dict[str, Channel] = decode_devicedata(event.devicedata or {})

            if mapping is None:
                prefix = detect_prefix(event.devicedata or {})
                if prefix is None:
                    print(f"[{_clock()}] Could not derive model/FW from the data keys, "
                          "skipping snapshot")
                    continue
                model_id, fw_code = prefix
                mapping = load_mapping(model_id, fw_code)

            resolved = mapping.resolve_all(channels)

            if not printed_table:
                print_table(mapping, resolved, show_invisible=args.show_invisible)
                printed_table = True
                if args.once:
                    return 0
                print(f"\n[{_clock()}] Live changes (Ctrl+C to stop):")
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
        description="Connects to the PoolDose, shows resolved channels (P1, without HA).")
    parser.add_argument("--host", required=True, help="IP or hostname of the device")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--connection-watchdog", type=float, default=DEFAULT_CONNECTION_WATCHDOG)
    parser.add_argument("--staleness-timeout", type=float, default=DEFAULT_STALENESS_TIMEOUT)
    parser.add_argument("--once", action="store_true",
                        help="Exit after the first complete table")
    parser.add_argument("--duration", type=float, default=0,
                        help="Total runtime in seconds; 0 = until Ctrl+C or --once")
    parser.add_argument("--show-invisible", action="store_true",
                        help="Also show channels with visible=false (concept B3)")

    args = parser.parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
