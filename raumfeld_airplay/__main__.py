"""Command line: python3 -m raumfeld_airplay"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import os

from .bridge import Bridge, Settings


def parse(arguments: list[str] | None = None) -> tuple[Settings, bool]:
    defaults = Settings(hook_dir=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    parser = argparse.ArgumentParser(prog="raumfeld_airplay",
                                     description="AirPlay to Raumfeld bridge: one AirPlay speaker per Raumfeld room.")
    add = parser.add_argument
    add("--run-dir", default=defaults.run_dir, help="where the sinks' FIFOs and configurations live")
    add("--state-dir", default=defaults.state_dir, help="where the host's address and the rooms' identities are kept")
    add("--hook-dir", default=defaults.hook_dir, help="where event-hook and volume-hook are")
    add("--shairport", default=defaults.shairport, help="the shairport-sync to run")
    add("--port", type=int, default=defaults.port, help="HTTP port the speakers fetch the streams from")
    add("--airplay-port", type=int, default=defaults.airplay_port, help="port of the first room's AirPlay speaker")
    add("--first-offset", type=int, default=defaults.first_offset, help="first AirPlay device ID offset to hand out")
    add("--host", default=defaults.host, help="the Raumfeld host's IP; found over SSDP if empty")
    add("--no-search", action="store_true", help="never search the network for the host (for tests)")
    add("--max-volume", type=int, default=defaults.max_volume, help="Raumfeld volume (0-100) at full AirPlay volume")
    add("--pause-after", type=float, default=defaults.pause_after,
        help="seconds without audio, once it was flowing, before the speakers are stopped")
    add("--idle-timeout", type=float, default=defaults.idle_timeout,
        help="seconds a new session may stay silent before the speakers are stopped again")
    add("--grace", type=float, default=defaults.grace,
        help="seconds a room may be missing before its AirPlay speaker is removed")
    add("--discover-interval", type=float, default=defaults.discover_interval, help="seconds between looks at the rooms")
    add("--title", default=defaults.title, help="what the speakers show as playing")
    add("--verbose", action="store_true",
        help="also log routine events: sender reports, volume steps, listeners, audio statistics")
    values = vars(parser.parse_args(arguments))
    verbose = bool(values.pop("verbose"))
    names = {field.name for field in dataclasses.fields(Settings)}
    return Settings(**{name: value for name, value in values.items() if name in names}), verbose


def main() -> None:
    settings, verbose = parse()
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(message)s")
    logging.getLogger("asyncio").setLevel(logging.INFO)
    asyncio.run(Bridge(settings).run())


if __name__ == "__main__":
    main()
