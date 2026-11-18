#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""A stand-in for a Raumfeld system, for trying the bridge without real speakers.

It answers the host's web service on port 47365 (rooms, zones, devices) and plays the part of
every zone's renderer: it takes the UPnP actions the bridge sends, fetches the stream it is told
to play, and writes what it was asked to do to standard output.

    python3 tests/fake_raumfeld.py "Test A" "Test B"

Run the bridge against it with --host 127.0.0.1 --no-search and its own --port, --airplay-port,
--first-offset, --run-dir and --state-dir, so that it neither finds the real host nor collides
with a running bridge.
"""

from __future__ import annotations

import http.server
import re
import sys
import threading
import urllib.parse
import urllib.request
from typing import ClassVar

PORT = 47365


class System:
    """The fake system's state."""

    zones: ClassVar[dict[str, set[str]]] = {}      # {zone udn: room udns}
    rooms: ClassVar[dict[str, str]] = {}           # {room udn: name}
    uri: ClassVar[dict[str, str]] = {}             # {zone udn: what it was told to play}
    state: ClassVar[dict[str, str]] = {}           # {zone udn: transport state}
    volume: ClassVar[dict[str, int]] = {}          # {room udn: volume}
    counter: ClassVar[int] = 0

    @classmethod
    def remove(cls, room: str) -> None:
        for zone in list(cls.zones):
            cls.zones[zone].discard(room)
            if not cls.zones[zone]:
                del cls.zones[zone]

    @classmethod
    def zone_config(cls) -> str:
        def rooms(udns: set[str]) -> str:
            return "".join(f"<room name='{cls.rooms[u]}' udn='{u}' powerState='ACTIVE'><renderer udn='{u}-r'/></room>"
                           for u in sorted(udns))
        grouped = {room for members in cls.zones.values() for room in members}
        zones = "".join(f"<zone udn='{zone}'>{rooms(members)}</zone>" for zone, members in cls.zones.items())
        return (f"<zoneConfig><zones>{zones}</zones>"
                f"<unassignedRooms>{rooms(set(cls.rooms) - grouped)}</unassignedRooms></zoneConfig>")


def say(text: str) -> None:
    print(text, flush=True)


def fetch(zone: str, url: str) -> None:
    """Fetches the stream like a speaker would, and reports how much arrived."""
    received = 0
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            while System.uri.get(zone) == url and System.state.get(zone) == "PLAYING":
                data = response.read(4096)
                if not data:
                    break
                received += len(data)
    except OSError as error:
        say(f"{zone}: stream ended: {error}")
    say(f"{zone}: fetched {received} bytes of {url}")


class Handler(http.server.BaseHTTPRequestHandler):
    def answer(self, body: str, content_type: str = "text/xml") -> None:
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        path, _, query_string = self.path.partition("?")
        query = {key: values[0] for key, values in urllib.parse.parse_qs(query_string).items()}
        if path == "/connectRoomToZone":
            room, zone = query["roomUDN"], query.get("zoneUDN")
            System.remove(room)
            members = (System.zones.pop(zone, set()) if zone else set()) | {room}
            System.counter += 1
            System.zones[f"uuid:zone-{System.counter}"] = members   # a zone's UDN changes with its rooms
            say(f"host: zone-{System.counter} now holds {sorted(System.rooms[r] for r in members)}")
        elif path == "/dropRoomJob":
            System.remove(query["roomUDN"])
            say(f"host: {System.rooms[query['roomUDN']]} left its zone")
        if path == "/listDevices":
            devices = "".join(f"<device udn='{zone}' location='http://127.0.0.1:{PORT}/renderer/{zone}.xml'>zone</device>"
                              for zone in System.zones)
            self.answer(f"<devices>{devices}</devices>")
        elif path.startswith("/renderer/"):
            zone = path[len("/renderer/"):-len(".xml")]
            services = "".join(
                f"<service><serviceType>urn:schemas-upnp-org:service:{name}:1</serviceType>"
                f"<controlURL>/control/{zone}/{name}</controlURL></service>" for name in ("AVTransport", "RenderingControl"))
            self.answer(f"<root><device><serviceList>{services}</serviceList></device></root>")
        else:
            self.answer(System.zone_config())

    def do_POST(self) -> None:
        zone = self.path.split("/")[2]
        body = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
        action = self.headers.get("SOAPACTION", "").strip('"').split("#")[-1]

        def argument(name: str) -> str:
            match = re.search(f"<{name}>(.*?)</{name}>", body, re.S)
            return match.group(1).replace("&amp;", "&") if match else ""

        outputs = ""
        if action == "SetAVTransportURI":
            System.uri[zone], System.state[zone] = argument("CurrentURI"), "STOPPED"
            say(f"{zone}: told to load {argument('CurrentURI')}")
        elif action == "Play":
            System.state[zone] = "PLAYING"
            say(f"{zone}: told to play")
            threading.Thread(target=fetch, args=(zone, System.uri.get(zone, "")), daemon=True).start()
        elif action == "Stop":
            System.state[zone] = "STOPPED"
            say(f"{zone}: told to stop")
        elif action == "GetTransportInfo":
            outputs = f"<CurrentTransportState>{System.state.get(zone, 'NO_MEDIA_PRESENT')}</CurrentTransportState>"
        elif action == "GetMediaInfo":
            outputs = f"<CurrentURI>{System.uri.get(zone, '').replace('&', '&amp;')}</CurrentURI>"
        elif action == "GetRoomVolume":
            outputs = f"<CurrentVolume>{System.volume.get(argument('Room'), 30)}</CurrentVolume>"
        elif action == "SetRoomVolume":
            System.volume[argument("Room")] = int(argument("DesiredVolume"))
            say(f"{zone}: {System.rooms.get(argument('Room'), '?')} set to volume {argument('DesiredVolume')}")
        self.answer(f'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                    f'<u:{action}Response xmlns:u="urn:x">{outputs}</u:{action}Response></s:Body></s:Envelope>')

    def log_message(self, format: str, *args: object) -> None:
        pass


def main() -> None:
    for number, name in enumerate(sys.argv[1:] or ["Test A", "Test B"]):
        System.rooms[f"uuid:room-{number}"] = name
    say(f"fake Raumfeld host on port {PORT} with rooms {sorted(System.rooms.values())}")
    http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
