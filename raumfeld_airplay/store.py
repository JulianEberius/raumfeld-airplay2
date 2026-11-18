"""What the bridge keeps between runs: where the Raumfeld host is, and each room's identity.

A room's identity is what a sender recognises its AirPlay speaker by: its AirPlay device ID
(the machine's own plus an offset) and port, and the name tag the bridge uses for its files
and its D-Bus name. It is tied to the room's UDN, so it survives restarts and renames.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import re
import unicodedata
from dataclasses import asdict, dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Identity:
    id: str        # name tag: lowercase letters and digits
    offset: int    # added to the machine's AirPlay device ID
    port: int      # where the room's shairport-sync listens


def slug(name: str) -> str:
    """A room name as a short identifier that is valid in file names and D-Bus names."""
    text = name.lower()
    for umlaut, plain in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        text = text.replace(umlaut, plain)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", "", text) or "room"
    return "r" + text if text[0].isdigit() else text


class Store:
    def __init__(self, directory: str, first_port: int = 7000, first_offset: int = 0) -> None:
        self._rooms_file = os.path.join(directory, "rooms.json")
        self._host_file = os.path.join(directory, "host")
        self._first_port = first_port
        self._first_offset = first_offset
        self._rooms: dict[str, Identity] = {}
        try:
            with open(self._rooms_file) as file:
                self._rooms = {udn: Identity(**entry) for udn, entry in json.load(file).items()}
        except (OSError, ValueError, TypeError):
            pass

    def identity(self, room_udn: str, room_name: str) -> Identity:
        """The room's lasting identity; a room seen for the first time gets the next free one."""
        if room_udn not in self._rooms:
            taken = {identity.id for identity in self._rooms.values()}
            base = slug(room_name)
            ident = next(candidate for candidate in itertools.chain([base], (f"{base}{n}" for n in itertools.count(2)))
                         if candidate not in taken)
            offsets = {identity.offset for identity in self._rooms.values()}
            offset = next(n for n in itertools.count(self._first_offset) if n not in offsets)
            self._rooms[room_udn] = Identity(ident, offset, self._first_port + offset)
            try:
                with open(self._rooms_file, "w") as file:
                    json.dump({udn: asdict(identity) for udn, identity in self._rooms.items()}, file, indent=1)
            except OSError as error:
                log.warning("couldn't store the room's identity: %s", error)
        return self._rooms[room_udn]

    def host(self) -> str | None:
        """The host's address as it was found last time."""
        try:
            with open(self._host_file) as file:
                return file.read().strip() or None
        except OSError:
            return None

    def remember_host(self, address: str) -> None:
        if address == self.host():
            return
        try:
            with open(self._host_file, "w") as file:
                file.write(address + "\n")
        except OSError as error:
            log.warning("couldn't remember the host: %s", error)
