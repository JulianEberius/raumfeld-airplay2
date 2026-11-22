# SPDX-License-Identifier: GPL-3.0-or-later
"""shairport-sync, the AirPlay 2 receiver, as the bridge uses it: one instance per room.

nqptp, the clock it shares with AirPlay 2 senders, is run alongside: one for all instances.

An instance is driven through its documented interfaces. Its configuration file names the
speaker and where things go; it writes PCM into one FIFO and a metadata feed into another;
it calls hook commands on session and volume events; and it takes remote-control commands
for the sender over D-Bus.
"""

from __future__ import annotations

import base64
import re

DBUS_PREFIX = "org.gnome.ShairportSync"
DBUS_PATH = "/org/gnome/ShairportSync"
DBUS_REMOTE = "org.gnome.ShairportSync.RemoteControl"

# The metadata feed's items the bridge acts on, by their four-letter codes.
SENDER_ADDRESS = "clip"
REPORTS = {"pbeg": "play begins", "pres": "resume", "paus": "pause", "pend": "play ends",
           "pfls": "flush", "snam": "name", SENDER_ADDRESS: "address",
           "minm": "title", "asar": "artist", "asal": "album"}

_ITEM = re.compile(rb'<item><type>([0-9a-f]{8})</type><code>([0-9a-f]{8})</code><length>\d+</length>'
                   rb'(?:\s*<data encoding="base64">\s*([^<]*)</data>)?\s*</item>')


# How far ahead of playing it shairport-sync writes audio into the pipe. 1.0 is its own default
# for pipes; it is set here so that the archive's timing doesn't change with that default.
PIPE_AHEAD = 1.0


def configuration(name: str, port: int, device_id_offset: int, tag: str, fifo: str, metadata_fifo: str,
                  hook_dir: str) -> str:
    """The configuration file of one instance: the AirPlay speaker `name`, whose audio goes into `fifo`.
    `tag` names the instance on D-Bus and tells the hooks which sink they report for."""
    name = name.replace("\\", " ").replace('"', "'")
    return f'''// Written by the Raumfeld bridge for the room "{name}". Changes here are overwritten.
general =
{{
	name = "{name}";
	port = {port};
	airplay_device_id_offset = {device_id_offset};
	dbus_nametag = "{tag}";
	output_backend = "pipe";
	ignore_volume_control = "yes";
	audio_backend_buffer_desired_length_in_seconds = {PIPE_AHEAD};
	run_this_when_volume_is_set = "{hook_dir}/volume-hook {tag}";
}};
sessioncontrol =
{{
	active_state_timeout = 2.0;
	run_this_before_play_begins = "{hook_dir}/event-hook {tag} play-begins";
	run_this_after_play_ends = "{hook_dir}/event-hook {tag} play-ends";
	run_this_before_entering_active_state = "{hook_dir}/event-hook {tag} active-begins";
	run_this_after_exiting_active_state = "{hook_dir}/event-hook {tag} active-ends";
	wait_for_completion = "no";
}};
pipe =
{{
	name = "{fifo}";
	output_rate = 44100;
	output_format = "S16_LE";
	output_channels = 2;
}};
metadata =
{{
	enabled = "yes";
	include_cover_art = "no";
	pipe_name = "{metadata_fifo}";
}};
'''


class MetadataFeed:
    """Parses an instance's metadata feed, which arrives in arbitrary pieces."""

    def __init__(self) -> None:
        self._buffer = b""

    def feed(self, data: bytes) -> list[tuple[str, str]]:
        """Adds a piece of the feed and returns the complete items in it that the bridge knows:
        (code, value) pairs, e.g. ("paus", "") or ("clip", "192.168.0.56")."""
        self._buffer = (self._buffer + data)[-200_000:]
        items: list[tuple[str, str]] = []
        end = 0
        for match in _ITEM.finditer(self._buffer):
            end = match.end()
            code = bytes.fromhex(match.group(2).decode()).decode(errors="replace")
            if code not in REPORTS:
                continue
            try:
                value = base64.b64decode(match.group(3) or b"").decode(errors="replace")
            except ValueError:
                value = ""
            items.append((code, value))
        self._buffer = self._buffer[end:]
        return items


def nqptp_ports_taken(output: str) -> bool:
    """Whether what nqptp printed as it gave up says that its ports are in use: by another nqptp."""
    return "unable to listen on port" in output and "98" in output   # 98: address already in use


def remote_command(tag: str, method: str, *arguments: str) -> list[str]:
    """The command line that sends a remote-control command (Pause, Play, SetAirplayVolume, ...) to
    the sender of the instance `tag`. Arguments are given busctl-style, e.g. "d", "-15.00"."""
    return ["busctl", "--system", "call", "--", f"{DBUS_PREFIX}.{tag}", DBUS_PATH, DBUS_REMOTE, method, *arguments]
