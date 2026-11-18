# SPDX-License-Identifier: GPL-3.0-or-later
"""The Raumfeld host: the device that runs the system and knows its rooms and zones.

It is found over SSDP and answers plain HTTP requests on port 47365. A zone is a set of rooms
that play in sync; the host creates a virtual UPnP renderer for each. All calls block.
"""

from __future__ import annotations

import socket
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .upnp import Fetch, Renderer, http_get, local_name

HOST_PORT = 47365
CONFIG_DEVICE = "urn:schemas-raumfeld-com:device:ConfigDevice:1"   # what the host announces over SSDP
SSDP_ADDRESS = ("239.255.255.250", 1900)


@dataclass(frozen=True)
class Room:
    udn: str
    name: str
    power_state: str | None    # e.g. ACTIVE, AUTOMATIC_STANDBY, MANUAL_STANDBY
    devices: int               # how many renderers the room has; none means it can't play


@dataclass(frozen=True)
class ZoneConfig:
    """The host's answer to getZones."""

    zones: dict[str, tuple[Room, ...]]   # {zone udn: its rooms}
    unassigned: tuple[Room, ...]         # rooms in no zone

    def rooms(self) -> dict[str, Room]:
        """{room udn: room} of all rooms the host lists."""
        listed = [room for rooms in self.zones.values() for room in rooms] + list(self.unassigned)
        return {room.udn: room for room in listed}

    def zone_of(self, room_udn: str) -> str | None:
        return next((zone for zone, rooms in self.zones.items() if any(r.udn == room_udn for r in rooms)), None)


def parse_zone_config(data: bytes) -> ZoneConfig:
    def rooms(element: ET.Element | None) -> tuple[Room, ...]:
        return tuple(
            Room(room.get("udn", ""), (room.get("name") or "").strip(), room.get("powerState"),
                 sum(1 for child in room if local_name(child.tag) == "renderer"))
            for room in (element if element is not None else []) if local_name(room.tag) == "room" and room.get("udn"))

    root = ET.fromstring(data)
    zones = {zone.get("udn", ""): rooms(zone) for zone in root.iter() if local_name(zone.tag) == "zone" and zone.get("udn")}
    unassigned = next((element for element in root.iter() if local_name(element.tag) == "unassignedRooms"), None)
    return ZoneConfig(zones, rooms(unassigned))


def parse_device_location(data: bytes, udn: str) -> str | None:
    """The description URL of the device `udn` in the host's answer to listDevices."""
    for device in ET.fromstring(data).iter():
        if local_name(device.tag) == "device" and device.get("udn") == udn:
            return device.get("location")
    return None


def discover_host(timeout: float = 4) -> str | None:
    """The Raumfeld host's IP address, found over SSDP."""
    message = (f"M-SEARCH * HTTP/1.1\r\nHOST: {SSDP_ADDRESS[0]}:{SSDP_ADDRESS[1]}\r\nMAN: \"ssdp:discover\"\r\n"
               f"MX: 2\r\nST: {CONFIG_DEVICE}\r\n\r\n").encode()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        for _ in range(2):
            sock.sendto(message, SSDP_ADDRESS)
        try:
            while True:
                data, address = sock.recvfrom(4096)
                if CONFIG_DEVICE.encode() in data:
                    return str(address[0])
        except socket.timeout:
            return None


class RaumfeldHost:
    """The host's web service."""

    def __init__(self, address: str, fetch: Fetch = http_get, sleep: Callable[[float], None] = time.sleep) -> None:
        self.address = address
        self._fetch = fetch
        self._sleep = sleep

    def _get(self, endpoint: str, **query: str) -> bytes:
        url = f"http://{self.address}:{HOST_PORT}/{endpoint}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return self._fetch(url)

    def zone_config(self) -> ZoneConfig:
        return parse_zone_config(self._get("getZones"))

    def connect_room(self, room_udn: str, zone_udn: str | None = None) -> None:
        """Puts the room into the zone, or into a new zone of its own."""
        if zone_udn:
            self._get("connectRoomToZone", roomUDN=room_udn, zoneUDN=zone_udn)
        else:
            self._get("connectRoomToZone", roomUDN=room_udn)

    def drop_room(self, room_udn: str) -> None:
        """Takes the room out of its zone."""
        self._get("dropRoomJob", roomUDN=room_udn)

    def ensure_zone(self, room_udns: Iterable[str]) -> str:
        """Makes one zone hold exactly these rooms and returns its UDN, which changes whenever
        the zone's rooms change. The host applies one change at a time, so this takes a few rounds."""
        target = set(room_udns)
        for _ in range(30):
            zones = self.zone_config().zones
            # The zone to build on: the one already holding most of the wanted rooms.
            best = max(zones.items(), key=lambda item: len(target & {room.udn for room in item[1]}), default=None)
            members = {room.udn for room in best[1]} if best else set()
            if best is None or not target & members:
                self.connect_room(sorted(target)[0])
            else:
                missing, extra = sorted(target - members), sorted(members - target)
                if not missing and not extra:
                    return best[0]
                if missing:
                    self.connect_room(missing[0], best[0])
                else:
                    self.drop_room(extra[0])
            self._sleep(0.5)
        raise RuntimeError("the Raumfeld host didn't apply the zone change")

    def renderer(self, zone_udn: str) -> Renderer:
        """The zone's virtual renderer, which appears a moment after the zone is formed."""
        for _ in range(20):
            location = parse_device_location(self._get("listDevices"), zone_udn)
            if location:
                return Renderer(location, self._fetch)
            self._sleep(0.5)
        raise RuntimeError("the zone's renderer didn't appear")
