#!/usr/bin/env python3
"""AirPlay -> Raumfeld bridge, one AirPlay speaker per Raumfeld room.

The bridge asks the Raumfeld host which rooms exist and runs one shairport-sync instance (a
"sink") for each: an AirPlay 2 speaker named after the room. A room that leaves the Raumfeld
system loses its speaker; a new room gets one. Each room keeps its identity (name tag, port,
AirPlay device ID) in a small store, so senders recognise it across restarts and renames.

A sink receives AirPlay audio and writes raw PCM into a FIFO. The bridge reads the FIFOs,
serves each as an endless MP3 stream over HTTP, and tells Raumfeld (over UPnP, the same way
the Wren app does) to play it. Rooms that are played to by the same sender at the same time
are put into one Raumfeld zone and fed from a single stream, so that Raumfeld keeps them in
sync itself. Presses of the speakers' own keys are passed back to the sender.

Standard library only; ffmpeg does the MP3 encoding.
"""

import argparse
import array
import asyncio
import base64
import itertools
import json
import logging
import os
import re
import socket
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

log = logging.getLogger("raumfeld-airplay")

RATE, CHANNELS, BITS = 44100, 2, 16
BYTES_PER_SECOND = RATE * CHANNELS * BITS // 8
MP3_BYTES_PER_SECOND = 40_000   # 320 kbit/s
OPENING_SECONDS = 3.0           # how long after a session begins the sender's volume counts as its opening one
PROTOCOL_INFO = "http-get:*:audio/mpeg:DLNA.ORG_PN=MP3;DLNA.ORG_OP=00;DLNA.ORG_FLAGS=01700000000000000000000000000000"
AVT = "urn:schemas-upnp-org:service:AVTransport:1"
RC = "urn:schemas-upnp-org:service:RenderingControl:1"


# ---------------------------------------------------------------- Raumfeld / UPnP

def http_get(url, timeout=6):
    with urllib.request.urlopen(url, timeout=timeout) as response:  # follows the host's redirects
        return response.read()


def local_name(tag):
    return tag.rsplit("}", 1)[-1]


def discover_host(timeout=4):
    """The Raumfeld host's IP, found over SSDP (it runs the ConfigDevice)."""
    target = "urn:schemas-raumfeld-com:device:ConfigDevice:1"
    message = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
               f"MX: 2\r\nST: {target}\r\n\r\n").encode()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        for _ in range(2):
            sock.sendto(message, ("239.255.255.250", 1900))
        try:
            while True:
                data, address = sock.recvfrom(4096)
                if target.encode() in data:
                    return address[0]
        except socket.timeout:
            return None


class Raumfeld:
    """The host's web service (port 47365) and the zone renderers it creates."""

    def __init__(self, host):
        self.base = f"http://{host}:47365"
        self.host = host

    def _get(self, endpoint, **query):
        url = f"{self.base}/{endpoint}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return http_get(url)

    def zones(self):
        """([(zone_udn, [(room_udn, room_name)])], [(room_udn, room_name)] unassigned)"""
        root = ET.fromstring(self._get("getZones"))
        def rooms(element):
            return [(r.get("udn"), (r.get("name") or "").strip())
                    for r in (element if element is not None else []) if local_name(r.tag) == "room"]
        zones = [(z.get("udn"), rooms(z)) for z in root.iter() if local_name(z.tag) == "zone"]
        unassigned = next((rooms(e) for e in root.iter() if local_name(e.tag) == "unassignedRooms"), [])
        return zones, unassigned

    def room_details(self):
        """{room_udn: (name, power state, number of renderers)} of the rooms the host knows."""
        root = ET.fromstring(self._get("getZones"))
        return {room.get("udn"): ((room.get("name") or "").strip(), room.get("powerState"),
                                  sum(1 for child in room if local_name(child.tag) == "renderer"))
                for room in root.iter() if local_name(room.tag) == "room" and room.get("udn")}

    def device_location(self, udn):
        root = ET.fromstring(self._get("listDevices"))
        for device in root.iter():
            if local_name(device.tag) == "device" and device.get("udn") == udn:
                return device.get("location")
        return None

    def ensure_zone(self, room_udns):
        """Makes one zone hold exactly these rooms and returns its UDN, which changes whenever
        the zone's rooms change."""
        target = set(room_udns)
        for _ in range(30):
            zones, _ = self.zones()
            # The zone to build on: the one already holding most of the wanted rooms.
            best = max(zones, key=lambda z: len(target & {u for u, _ in z[1]}), default=None)
            if best is None or not (target & {u for u, _ in best[1]}):
                self._get("connectRoomToZone", roomUDN=sorted(target)[0])
            else:
                zone_udn, zone_rooms = best
                zone_ids = {u for u, _ in zone_rooms}
                missing, extra = sorted(target - zone_ids), sorted(zone_ids - target)
                if not missing and not extra:
                    return zone_udn
                if missing:
                    self._get("connectRoomToZone", roomUDN=missing[0], zoneUDN=zone_udn)
                else:
                    self._get("dropRoomJob", roomUDN=extra[0])
            time.sleep(0.5)
        raise RuntimeError("the Raumfeld host didn't apply the zone change")

    def renderer(self, zone_udn):
        for _ in range(20):
            location = self.device_location(zone_udn)
            if location:
                return Renderer(location)
            time.sleep(0.5)
        raise RuntimeError("the zone's renderer didn't appear")


class Renderer:
    """A zone's virtual media renderer: plays one stream in all of the zone's rooms."""

    def __init__(self, location):
        root = ET.fromstring(http_get(location))
        self.control = {}
        for service in root.iter():
            if local_name(service.tag) != "service":
                continue
            fields = {local_name(c.tag): (c.text or "") for c in service}
            if "serviceType" in fields and "controlURL" in fields:
                self.control[fields["serviceType"]] = urllib.parse.urljoin(location, fields["controlURL"])
        if AVT not in self.control:
            raise RuntimeError("renderer without AVTransport")

    def call(self, service, action, **arguments):
        body = "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in arguments.items())
        envelope = ('<?xml version="1.0" encoding="utf-8"?>'
                    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                    's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                    f'<u:{action} xmlns:u="{service}">{body}</u:{action}></s:Body></s:Envelope>')
        request = urllib.request.Request(self.control[service], data=envelope.encode(), method="POST", headers={
            "Content-Type": 'text/xml; charset="utf-8"', "SOAPACTION": f'"{service}#{action}"'})
        try:
            with urllib.request.urlopen(request, timeout=6) as response:
                data = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")
            raise RuntimeError(f"{action} failed: HTTP {error.code} {detail[:300]}") from None
        root = ET.fromstring(data)
        for element in root.iter():
            if local_name(element.tag) == f"{action}Response":
                return {local_name(c.tag): (c.text or "") for c in element}
        return {}

    def play_stream(self, url, title):
        didl = ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
                'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
                '<item id="airplay" parentID="0" restricted="1">'
                f'<dc:title>{escape(title)}</dc:title>'
                '<upnp:class>object.item.audioItem.audioBroadcast</upnp:class>'
                f'<res protocolInfo="{escape(PROTOCOL_INFO)}">{escape(url)}</res></item></DIDL-Lite>')
        self.call(AVT, "SetAVTransportURI", InstanceID=0, CurrentURI=url, CurrentURIMetaData=didl)
        self.call(AVT, "Play", InstanceID=0, Speed=1)

    def stop(self):
        self.call(AVT, "Stop", InstanceID=0)

    def state(self):
        return self.call(AVT, "GetTransportInfo", InstanceID=0).get("CurrentTransportState", "")

    def current_uri(self):
        return self.call(AVT, "GetMediaInfo", InstanceID=0).get("CurrentURI", "")

    def room_volume(self, room_udn):
        value = self.call(RC, "GetRoomVolume", InstanceID=0, Room=room_udn).get("CurrentVolume", "")
        return int(value) if value.isdigit() else None

    def set_room_volume(self, room_udn, volume):
        if RC in self.control:
            self.call(RC, "SetRoomVolume", InstanceID=0, Room=room_udn, DesiredVolume=int(volume))


# ---------------------------------------------------------------- sinks and zones

class Listener:
    """One HTTP connection fetching a stream."""

    def __init__(self, peer):
        self.peer = peer
        self.queue = asyncio.Queue()
        self.queued = 0   # bytes waiting to be sent
        self.sent = 0

    def abandon(self):
        """Drops what is still waiting and ends the connection."""
        while not self.queue.empty():
            self.queue.get_nowait()
        self.queued = 0
        self.queue.put_nowait(None)


class Sink:
    """One AirPlay speaker: a shairport-sync instance and the Raumfeld room it stands for."""

    def __init__(self, ident, room_udn, room_name, port, offset, run_dir):
        self.id = ident                         # also its D-Bus name tag
        self.room_udn = room_udn
        self.room_name = room_name              # also the AirPlay speaker's name
        self.port = port                        # where its shairport-sync listens
        self.offset = offset                    # added to the machine's AirPlay device ID
        self.conf = os.path.join(run_dir, ident + ".conf")
        self.process = None                     # its shairport-sync
        self.tasks = []
        self.fds = []
        self.missing_since = None               # since when the host hasn't listed the room
        self.gone = False                       # the room left: stop playing, then remove the sink
        self.renamed = False                    # its shairport-sync was stopped to take the room's new name
        self.yielded = False                    # somebody gave the room another source: hands off until the sender plays anew
        self.fifo = os.path.join(run_dir, ident)             # PCM from shairport-sync
        self.meta_fifo = os.path.join(run_dir, ident + ".meta")   # its metadata feed
        self.path = f"/stream/{ident}.mp3"
        self.listeners = set()
        self.encoder = None
        self.last_audio = 0.0        # monotonic time of the last PCM
        self.session_open = False    # between shairport-sync's "active-begins" and "active-ends"
        self.session_began = 0.0
        self.paused_at = 0.0         # when the sender reported a pause or the end of play; 0 if it hasn't
        self.resumed_at = 0.0        # when the sender last reported that play (re)started
        self.client = None           # the sender's IP address, once the metadata feed has told it
        self.volume = None           # last volume asked for over AirPlay (0-100 Raumfeld scale)
        self.volume_set_at = 0.0     # when the sender last set the volume
        self.connected_at = None     # when the sender began its play session; None between sessions
        self.tell_volume = False     # the sender is still to be told the speakers' volume, as the session opens
        self.speaker_volume = None   # the room's volume as the speakers last reported it
        self.play_asked_at = 0.0     # when the sender was last asked to play, after a press of the speakers' key
        self.told_sender = None      # (AirPlay volume, time) last passed on to the sender from the speakers' keys
        self.flushed = True          # the encoder holds no leftover audio from before a pause
        self.meta_buffer = b""
        self.stats = {"bytes": 0, "peak": 0}

    def play_reported(self, now):
        """The sender says play is starting or resuming: expect audio shortly."""
        self.paused_at, self.resumed_at = 0.0, now
        self.session_open, self.session_began = True, now
        self.yielded = False   # a deliberate play: the room is the sender's again

    def pause_reported(self, now, final):
        """The sender says play paused, or (final) ended."""
        if not final and now - self.resumed_at < 1.5:
            # A pause right after a resume: the sender is toggling, as on a skip or seek. Keep the
            # speakers' stream open for the audio that is about to come instead of restarting it twice.
            self.session_open, self.session_began = True, now
        else:
            self.paused_at = now

    def wants_play(self, now, pause_after, idle_timeout):
        """Whether the room should be playing: audio is arriving, or play has just (re)started and
        its audio is still to come. Never while the sender says it is paused."""
        if self.paused_at or self.gone or self.yielded:
            return False
        if now - self.last_audio < pause_after:
            return True
        return self.session_open and self.last_audio < self.session_began and now - self.session_began < idle_timeout


class Zone:
    """A Raumfeld zone this bridge is playing a stream in."""

    def __init__(self, key, leader, renderer, url):
        self.key = key              # frozenset of sink ids
        self.leader = leader        # id of the sink whose stream the zone plays
        self.renderer = renderer
        self.url = url
        self.restart = False        # the speakers stopped fetching the stream
        self.speaker_state = None   # the transport state the speakers last reported
        self.located_at = time.monotonic()   # when the rooms' whereabouts were last checked
        self.strays = set()         # sinks whose rooms weren't with the zone at that check
        self.last_listener = time.monotonic()


def same_sender(a, b):
    """Whether two sinks are played to by the same device; unknown counts as the same."""
    return a.client is None or b.client is None or a.client == b.client


def plan_zones(sinks, playing, now, pause_after, idle_timeout):
    """Which zones should exist: {frozenset of sink ids: leader's id}.

    Sinks that should play and share a sender form one zone. `playing` is {key: leader} of
    the zones that exist; a zone's leader is kept while it still plays, so that adding or
    removing another room doesn't change the stream the zone is fed from."""
    groups = []
    for sink in sinks:
        if not sink.wants_play(now, pause_after, idle_timeout):
            continue
        for group in groups:
            if all(same_sender(sink, other) for other in group):
                group.append(sink)
                break
        else:
            groups.append([sink])
    plan = {}
    for group in groups:
        ids = [sink.id for sink in group]
        kept = [leader for key, leader in playing.items() if leader in ids]
        with_audio = [sink.id for sink in group if now - sink.last_audio < pause_after]
        plan[frozenset(ids)] = (kept or with_audio or ids)[0]
    return plan


def slug(name):
    """A room name as a short identifier that is valid in file names and D-Bus names."""
    text = name.lower()
    for umlaut, plain in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        text = text.replace(umlaut, plain)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", "", text) or "room"
    return "r" + text if text[0].isdigit() else text


def shairport_config(sink, hook_dir):
    """The configuration of a sink's shairport-sync instance."""
    name = sink.room_name.replace("\\", " ").replace('"', "'")
    return f'''// Written by the Raumfeld bridge for the room "{name}". Changes here are overwritten.
general =
{{
	name = "{name}";
	port = {sink.port};
	airplay_device_id_offset = {sink.offset};
	dbus_nametag = "{sink.id}";
	output_backend = "pipe";
	ignore_volume_control = "yes";
	run_this_when_volume_is_set = "{hook_dir}/volume-hook {sink.id}";
}};
sessioncontrol =
{{
	active_state_timeout = 2.0;
	run_this_before_play_begins = "{hook_dir}/event-hook {sink.id} play-begins";
	run_this_after_play_ends = "{hook_dir}/event-hook {sink.id} play-ends";
	run_this_before_entering_active_state = "{hook_dir}/event-hook {sink.id} active-begins";
	run_this_after_exiting_active_state = "{hook_dir}/event-hook {sink.id} active-ends";
	wait_for_completion = "no";
}};
pipe =
{{
	name = "{sink.fifo}";
	output_rate = 44100;
	output_format = "S16_LE";
	output_channels = 2;
}};
metadata =
{{
	enabled = "yes";
	include_cover_art = "no";
	pipe_name = "{sink.meta_fifo}";
}};
'''


# ---------------------------------------------------------------- the bridge

METADATA_NAMES = {"pbeg": "play begins", "pres": "resume", "paus": "pause", "pend": "play ends",
                  "pfls": "flush", "snam": "name"}
METADATA_ITEM = re.compile(rb'<item><type>([0-9a-f]{8})</type><code>([0-9a-f]{8})</code><length>\d+</length>'
                           rb'(?:\s*<data encoding="base64">\s*([^<]*)</data>)?\s*</item>')


class Bridge:
    def __init__(self, args):
        self.args = args
        self.sinks = {}               # {sink id: Sink}, one per room the Raumfeld host lists
        self.zones = {}               # {frozenset of sink ids: Zone}
        self.store = self.load_store()   # {room udn: {"id", "port", "offset"}}: the rooms' lasting identities
        self.seen_rooms = {}          # {room udn: (name, power state, renderers)} as last listed, for the log
        self.host_reachable = None
        self.own_ip = None
        self.lock = asyncio.Lock()
        self.retry_after = 0.0        # no new attempt before this time, after a failed one
        self.stats_since = time.monotonic()
        # How far a listener may fall behind: six seconds of stream.
        self.backlog_limit = 6 * MP3_BYTES_PER_SECOND

    # --- audio and metadata in

    def open_fifo(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            os.mkfifo(path, 0o666)
        os.chmod(path, 0o666)
        # Read-write so the FIFO never reports end-of-file when shairport-sync closes its end.
        return os.open(path, os.O_RDWR | os.O_NONBLOCK)

    def on_audio(self, fd, sink):
        try:
            data = os.read(fd, 65536)
        except BlockingIOError:
            return
        if not data:
            return
        sink.last_audio = time.monotonic()
        if sink.paused_at and sink.last_audio - sink.paused_at > 1.0:
            sink.paused_at = 0.0   # audio long after a reported pause: a resume that wasn't reported
        sink.flushed = False
        sink.stats["bytes"] += len(data)
        samples = array.array("h", data[:len(data) & ~1][:8192])
        if samples:
            sink.stats["peak"] = max(sink.stats["peak"], max(samples), -min(samples))
        self.feed(sink, data)

    def on_metadata(self, fd, sink):
        try:
            data = os.read(fd, 65536)
        except BlockingIOError:
            return
        sink.meta_buffer = (sink.meta_buffer + data)[-200_000:]
        end = 0
        for match in METADATA_ITEM.finditer(sink.meta_buffer):
            end = match.end()
            code = bytes.fromhex(match.group(2).decode()).decode(errors="replace")
            if code not in ("clip", "snam", "paus", "pres", "pbeg", "pend", "pfls"):
                continue
            try:
                value = base64.b64decode(match.group(3) or b"").decode(errors="replace")
            except ValueError:
                value = ""
            now = time.monotonic()
            if code == "clip":
                if value != sink.client:
                    log.info("%s: sender is %s", sink.id, value)
                sink.client = value or None
                continue
            log.info("%s: sender reports %s %s", sink.id, METADATA_NAMES.get(code, code), value[:60])
            if code == "pbeg":
                # A new session: the speakers keep the volume they have, and the sender is told it.
                sink.connected_at, sink.tell_volume, sink.volume = now, True, None
            elif code == "pend":
                sink.connected_at = None
            if code in ("pbeg", "pres"):
                sink.play_reported(now)
            elif code in ("paus", "pend"):
                sink.pause_reported(now, final=code == "pend")
        sink.meta_buffer = sink.meta_buffer[end:]

    def feed(self, sink, pcm):
        """PCM on its way to the sink's listeners, through its encoder."""
        if sink.encoder and sink.encoder.returncode is None:
            sink.encoder.stdin.write(pcm)

    def broadcast(self, sink, data):
        for listener in list(sink.listeners):
            if listener.queued > self.backlog_limit:   # a listener that stopped reading
                log.warning("%s: listener %s stopped reading; dropping it", sink.id, listener.peer)
                sink.listeners.discard(listener)
                listener.abandon()
                for zone in self.zones.values():
                    if zone.leader == sink.id:
                        zone.restart = True
            else:
                listener.queued += len(data)
                listener.queue.put_nowait(data)

    async def run_encoder(self, sink):
        """Keeps ffmpeg running and passes what it produces on to the sink's listeners."""
        while True:
            sink.encoder = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                # Without these ffmpeg collects seconds of input before it produces anything.
                "-probesize", "32", "-analyzeduration", "0", "-fflags", "nobuffer", "-f", "s16le",
                "-ar", str(RATE), "-ac", str(CHANNELS), "-i", "pipe:0",
                "-c:a", "libmp3lame", "-b:a", "320k", "-f", "mp3", "-flush_packets", "1", "pipe:1",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
            while True:
                data = await sink.encoder.stdout.read(8192)
                if not data:
                    break
                self.broadcast(sink, data)
            log.error("%s: the encoder stopped (%s); restarting it", sink.id, await sink.encoder.wait())
            await asyncio.sleep(1)

    # --- HTTP

    async def handle_http(self, reader, writer):
        peer = writer.get_extra_info("peername")
        local = bool(peer) and peer[0] in ("127.0.0.1", "::1")
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
            line = request.split(b"\r\n", 1)[0].decode(errors="replace")
            method, target, *_ = line.split(" ") + ["", ""]
            path, _, query = target.partition("?")
            query = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
            stream_of = next((s for s in self.sinks.values() if s.path == path), None)
            sink = self.sinks.get(query.get("sink", ""))
            if stream_of:
                await self.serve_stream(stream_of, method, writer, peer)
            elif path == "/volume" and local and sink:
                asyncio.ensure_future(self.set_airplay_volume(sink, query.get("airplay", "")))
                writer.write(b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n")
            elif path == "/event" and local and sink:
                self.on_event(sink, query.get("name", "?"))
                writer.write(b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n")
            elif path == "/status":
                body = self.status().encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\nConnection: close\r\n"
                             + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            else:
                writer.write(b"HTTP/1.1 404 Not Found\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()

    async def serve_stream(self, sink, method, writer, peer):
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\nCache-Control: no-cache\r\n"
                     b"Connection: close\r\nAccept-Ranges: none\r\n\r\n")
        if method == "HEAD":
            return
        listener = Listener(peer[0] if peer else "?")
        log.info("%s: listener connected: %s", sink.id, listener.peer)
        now = time.monotonic()
        if (sink.paused_at and now - sink.play_asked_at > 3 and not self.lock.locked()
                and not any(zone.leader == sink.id for zone in self.zones.values())):
            # The speakers fetch the stream of a paused sink on their own: somebody pressed play on them.
            log.info("%s: play pressed on the speakers; asking the sender to play", sink.id)
            sink.play_asked_at = now
            # As if the sender had already resumed: the speakers get silence right away, until the audio comes.
            sink.play_reported(now)
            asyncio.ensure_future(self.remote(sink, "Play"))
        sink.listeners.add(listener)
        try:
            while True:
                data = await listener.queue.get()
                if data is None:
                    break
                listener.queued -= len(data)
                writer.write(data)
                await writer.drain()
                listener.sent += len(data)
        except ConnectionError:
            pass
        finally:
            sink.listeners.discard(listener)
            for zone in self.zones.values():
                if zone.leader == sink.id:
                    zone.last_listener = time.monotonic()
            log.info("%s: listener gone: %s after %d bytes", sink.id, listener.peer, listener.sent)

    def status(self):
        now = time.monotonic()
        lines = [f"{s.id}: room={s.room_name!r} port={s.port} audio_age={now - s.last_audio:.1f}s session_open={s.session_open} "
                 f"sender={s.client} volume={s.volume} listeners={len(s.listeners)}{' yielded' if s.yielded else ''}"
                 for s in self.sinks.values()]
        lines += [f"zone {'+'.join(sorted(z.key))}: stream of {z.leader}" for z in self.zones.values()]
        return "\n".join(lines) + "\n"

    def on_event(self, sink, name):
        """A session event from shairport-sync. Only the "active" pair is acted on: it is debounced
        (active_state_timeout) and fires on pause and resume, unlike play-begins and play-ends."""
        log.info("%s: event %s (last audio %.1fs ago)", sink.id, name, time.monotonic() - sink.last_audio)
        if name == "active-begins":
            sink.session_open, sink.session_began, sink.paused_at = True, time.monotonic(), 0.0
            sink.yielded = False
        elif name == "active-ends":
            sink.session_open, sink.client = False, None

    # --- rooms come and go

    def load_store(self):
        try:
            with open(os.path.join(self.args.state_dir, "rooms.json")) as file:
                return json.load(file)
        except (OSError, ValueError):
            return {}

    def identity(self, udn, name):
        """The room's lasting identity; a room seen for the first time gets the next free one."""
        if udn not in self.store:
            taken = {entry["id"] for entry in self.store.values()}
            ident = next(i for i in (slug(name) + suffix for suffix in itertools.chain([""], map(str, itertools.count(2))))
                         if i not in taken)
            offsets = {entry["offset"] for entry in self.store.values()}
            offset = next(i for i in itertools.count(self.args.first_offset) if i not in offsets)
            self.store[udn] = {"id": ident, "offset": offset, "port": self.args.airplay_port + offset}
            try:
                with open(os.path.join(self.args.state_dir, "rooms.json"), "w") as file:
                    json.dump(self.store, file, indent=1)
            except OSError as error:
                log.warning("couldn't store the room's identity: %s", error)
        return self.store[udn]

    def add_sink(self, udn, name):
        entry = self.identity(udn, name)
        sink = Sink(entry["id"], udn, name, entry["port"], entry["offset"], self.args.run_dir)
        loop = asyncio.get_running_loop()
        for path, handler in ((sink.fifo, self.on_audio), (sink.meta_fifo, self.on_metadata)):
            fd = self.open_fifo(path)
            sink.fds.append(fd)
            loop.add_reader(fd, handler, fd, sink)
        sink.tasks = [asyncio.ensure_future(self.run_encoder(sink)), asyncio.ensure_future(self.run_shairport(sink))]
        self.sinks[sink.id] = sink
        log.info("%s: AirPlay speaker %r added (port %d)", sink.id, name, sink.port)

    def remove_sink(self, sink):
        for task in sink.tasks:
            task.cancel()
        for process in (sink.process, sink.encoder):
            if process and process.returncode is None:
                process.terminate()
        loop = asyncio.get_running_loop()
        for fd in sink.fds:
            loop.remove_reader(fd)
            os.close(fd)
        for listener in list(sink.listeners):
            listener.abandon()
        del self.sinks[sink.id]
        log.info("%s: AirPlay speaker %r removed", sink.id, sink.room_name)

    async def run_shairport(self, sink):
        """Keeps the sink's shairport-sync running and passes what it prints on to the log."""
        while True:
            with open(sink.conf, "w") as file:
                file.write(shairport_config(sink, self.args.hook_dir))
            started = time.monotonic()
            sink.process = await asyncio.create_subprocess_exec(
                self.args.shairport, "-c", sink.conf, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            async for line in sink.process.stdout:
                log.info("%s: shairport-sync: %s", sink.id, line.decode(errors="replace").rstrip())
            code = await sink.process.wait()
            if sink.renamed:
                sink.renamed = False
                continue   # stopped by the bridge to take the room's new name
            log.error("%s: shairport-sync stopped (%s); starting it again", sink.id, code)
            await asyncio.sleep(1 if time.monotonic() - started > 30 else 5)

    def update_rooms(self, details, now):
        """Adds sinks for rooms the host lists and marks those it has stopped listing.
        `details` is None while the host can't be reached."""
        if (details is not None) != self.host_reachable:
            self.host_reachable = details is not None
            log.info("the Raumfeld host %s", "answers" if self.host_reachable else "doesn't answer")
        by_room = {sink.room_udn: sink for sink in self.sinks.values()}
        for udn, seen in sorted((details or {}).items(), key=lambda item: item[1][0]):
            name, power, renderers = seen
            if self.seen_rooms.get(udn) != seen:
                log.info("room %r: power state %s, %d device(s)", name, power, renderers)
                self.seen_rooms[udn] = seen
            sink = by_room.get(udn)
            if not renderers:
                continue   # a room without a device can't play
            if sink is None:
                self.add_sink(udn, name)
            elif sink.room_name != name and sink.process and sink.process.returncode is None:
                log.info("%s: the room is now called %r", sink.id, name)
                sink.room_name, sink.renamed = name, True
                sink.process.terminate()   # comes back under the new name
        for udn in [udn for udn in self.seen_rooms if details is not None and udn not in details]:
            log.info("room %r is no longer listed", self.seen_rooms.pop(udn)[0])
        for sink in self.sinks.values():
            if details is not None and details.get(sink.room_udn, ("", None, 0))[2]:
                sink.missing_since = None
                continue
            sink.missing_since = sink.missing_since or now
            if not sink.gone and now - sink.missing_since > self.args.grace:
                log.info("%s: room %r has been away for %d s", sink.id, sink.room_name, self.args.grace)
                sink.gone = True

    async def discover(self):
        while True:
            try:
                details = await asyncio.to_thread(lambda: self.connect().room_details())
            except Exception as error:
                log.debug("couldn't list the rooms: %s", error)
                details = None
            self.update_rooms(details, time.monotonic())
            await asyncio.sleep(self.args.discover_interval)

    # --- Raumfeld control (blocking; run in a thread, one at a time under self.lock)

    def known_host(self):
        try:
            with open(os.path.join(self.args.state_dir, "host")) as file:
                return file.read().strip() or None
        except OSError:
            return None

    def remember_host(self, host):
        if host == self.known_host():
            return
        try:
            with open(os.path.join(self.args.state_dir, "host"), "w") as file:
                file.write(host + "\n")
        except OSError as error:
            log.warning("couldn't remember the host: %s", error)

    def connect(self):
        """Finds the Raumfeld host."""
        # The host found last time first: it doesn't always answer the search, e.g. while waking up.
        for host in dict.fromkeys(h for h in (self.args.host, self.known_host(), "search") if h):
            if host == "search":
                host = None if self.args.no_search else discover_host()
                if not host:
                    continue
            try:
                raumfeld = Raumfeld(host)
                raumfeld.zones()
            except OSError as error:
                log.debug("no Raumfeld host at %s: %s", host, error)
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((host, 47365))
                self.own_ip = sock.getsockname()[0]
            self.remember_host(host)
            return raumfeld
        raise RuntimeError("no Raumfeld host found on the network")

    def start_zone(self, key, leader, force):
        """Groups the rooms of `key` into one zone and has it play the leader's stream."""
        raumfeld = self.connect()
        sinks = [self.sinks[ident] for ident in sorted(key)]
        renderer = raumfeld.renderer(raumfeld.ensure_zone([sink.room_udn for sink in sinks]))
        url = f"http://{self.own_ip}:{self.args.port}{self.sinks[leader].path}"
        for sink in sinks:
            if sink.volume is not None:
                renderer.set_room_volume(sink.room_udn, sink.volume)
        # A zone that just gained or lost a room may still be playing the stream: leave it be then.
        if force or renderer.current_uri() != url or renderer.state() not in ("PLAYING", "TRANSITIONING"):
            renderer.play_stream(url, self.args.title)
        log.info("playing %s's stream on %s", leader, " + ".join(sink.room_name for sink in sinks))
        return Zone(key, leader, renderer, url)

    def stop_zone(self, zone):
        # Leave the speakers alone if somebody switched them to another source meanwhile.
        if zone.renderer.current_uri() == zone.url:
            zone.renderer.stop()
        log.info("stopped %s", " + ".join(self.sinks[ident].room_name for ident in sorted(zone.key)))

    async def reconcile(self, now):
        """Brings the zones that play in line with the sinks that should."""
        playing = {key: zone.leader for key, zone in self.zones.items()}
        plan = plan_zones(self.sinks.values(), playing, now, self.args.pause_after, self.args.idle_timeout)
        if plan == playing and not any(zone.restart for zone in self.zones.values()):
            return
        if now < self.retry_after:
            return
        async with self.lock:
            forced = set()
            for key, zone in list(self.zones.items()):
                if plan.get(key) == zone.leader and not zone.restart:
                    continue
                del self.zones[key]
                if zone.restart and plan.get(key) == zone.leader:
                    log.warning("restarting playback on %s", "+".join(sorted(key)))
                    forced.add(key)
                # A planned zone that keeps this one's stream and some of its rooms takes over: no stop.
                elif not any(leader == zone.leader and key & other for other, leader in plan.items()):
                    try:
                        await asyncio.to_thread(self.stop_zone, zone)
                    except Exception as error:
                        log.error("couldn't stop playback: %s", error)
                    # The host can leave its connection open after a stop. Close it, so that it isn't
                    # mistaken for a stalled listener the next time this stream plays.
                    for listener in list(self.sinks[zone.leader].listeners):
                        listener.abandon()
            for key, leader in plan.items():
                if key in self.zones:
                    continue
                try:
                    self.zones[key] = await asyncio.to_thread(self.start_zone, key, leader, key in forced)
                except Exception as error:
                    log.error("couldn't start playback: %s", error)
                    self.retry_after = time.monotonic() + 5
                    break

    async def set_airplay_volume(self, sink, value):
        """AirPlay volume: -30 (quiet) to 0 (full) dB, -144 for mute."""
        try:
            airplay = float(value)
        except ValueError:
            return
        if sink.told_sender and abs(airplay - sink.told_sender[0]) < 0.06 and time.monotonic() - sink.told_sender[1] < 3:
            sink.told_sender = None   # the echo of a volume that came from the speakers: they already have it
            return
        if sink.connected_at is None or time.monotonic() - sink.connected_at < OPENING_SECONDS:
            # The volume a sender announces as it connects is whatever its slider happened to show.
            log.info("%s: ignoring the sender's opening volume (AirPlay %.1f)", sink.id, airplay)
            return
        fraction = 0.0 if airplay <= -100 else min(max((airplay + 30.0) / 30.0, 0.0), 1.0)
        sink.volume, sink.volume_set_at = round(fraction * self.args.max_volume), time.monotonic()
        log.info("%s: volume %s (AirPlay %.1f)", sink.id, sink.volume, airplay)
        zone = next((z for z in self.zones.values() if sink.id in z.key), None)
        if zone and sink.room_udn:
            try:
                await asyncio.to_thread(zone.renderer.set_room_volume, sink.room_udn, sink.volume)
            except Exception as error:
                log.error("couldn't set the volume: %s", error)

    def read_speakers(self, zone):
        """Blocking: the zone's transport state and its rooms' volumes, as the speakers report them."""
        volumes = {ident: zone.renderer.room_volume(self.sinks[ident].room_udn) for ident in zone.key}
        return zone.renderer.state(), volumes, zone.renderer.current_uri()

    def locate(self, zone):
        """Blocking: where the zone's rooms are now. Returns the renderer of the Raumfeld zone that holds
        the leader's room (None if it is in no zone) and the ids of the sinks whose rooms aren't with it."""
        raumfeld = self.connect()
        zones, _ = raumfeld.zones()
        where = {udn: zone_udn for zone_udn, rooms in zones for udn, _ in rooms}
        home = where.get(self.sinks[zone.leader].room_udn)
        strays = [ident for ident in zone.key if home is None or where.get(self.sinks[ident].room_udn) != home]
        return (raumfeld.renderer(home) if home else None), strays

    def let_go(self, zone, idents, reason):
        """Stops claiming these rooms of the zone: somebody chose something else for them."""
        for ident in idents:
            self.sinks[ident].yielded = True
        log.info("%s: %s; leaving %s alone until the sender plays again", "+".join(sorted(zone.key)), reason,
                 " + ".join(self.sinks[ident].room_name for ident in sorted(idents)))
        if set(idents) == set(zone.key):
            # Nothing of this session is heard any more: have the sender show that.
            asyncio.ensure_future(self.remote(self.sinks[zone.leader], "Pause"))

    async def remote(self, sink, method, *arguments):
        """Sends a remote-control command back to the sink's sender, through shairport-sync's D-Bus interface."""
        command = ["busctl", "--system", "call", "--", f"org.gnome.ShairportSync.{sink.id}", "/org/gnome/ShairportSync",
                   "org.gnome.ShairportSync.RemoteControl", method, *arguments]
        try:
            process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL,
                                                           stderr=asyncio.subprocess.PIPE)
            _, errors = await asyncio.wait_for(process.communicate(), timeout=5)
            if process.returncode:
                log.error("%s: remote command %s failed: %s", sink.id, method, errors.decode(errors="replace").strip())
        except (OSError, asyncio.TimeoutError) as error:
            log.error("%s: remote command %s failed: %s", sink.id, method, error)

    async def observe(self):
        """Watches the speakers for what the bridge didn't do itself, i.e. presses of their keys, and
        passes it back to the sender: a pause, or a changed volume."""
        while True:
            await asyncio.sleep(0.5)
            for zone in list(self.zones.values()):
                now = time.monotonic()
                try:
                    state, volumes, uri = await asyncio.to_thread(self.read_speakers, zone)
                except Exception as error:
                    log.debug("couldn't read the speakers: %s", error)
                    state = None
                if self.zones.get(zone.key) is not zone or self.lock.locked():
                    continue   # stopped, replaced or being rearranged meanwhile
                if state is None or now - zone.located_at > 3:
                    # Are the rooms still together? Somebody may have regrouped them, e.g. in the Raumfeld app.
                    zone.located_at = now
                    try:
                        renderer, strays = await asyncio.to_thread(self.locate, zone)
                    except Exception as error:
                        log.debug("couldn't locate the rooms: %s", error)
                        continue
                    if self.zones.get(zone.key) is not zone or self.lock.locked():
                        continue
                    # Acted on only when seen twice in a row: the host's list can be in flux for a moment.
                    confirmed, zone.strays = zone.strays & set(strays), set(strays)
                    if confirmed:
                        self.let_go(zone, confirmed, "rooms were regrouped")
                        continue
                    if strays or renderer is None:
                        continue
                    zone.renderer = renderer   # the zone's renderer changes when rooms are added to it
                if state is None:
                    continue
                leader = self.sinks[zone.leader]
                if uri and uri != zone.url:
                    self.let_go(zone, zone.key, "the speakers were given another source")
                    continue
                ours = True
                if state != zone.speaker_state:
                    log.info("speakers of %s report state %s (was %s)", "+".join(sorted(zone.key)), state, zone.speaker_state)
                    if state == "PAUSED_PLAYBACK" and zone.speaker_state in ("PLAYING", "TRANSITIONING") \
                            and not leader.paused_at:
                        log.info("%s: pause pressed on the speakers; asking the sender to pause", leader.id)
                        asyncio.ensure_future(self.remote(leader, "Pause"))
                    zone.speaker_state = state
                if state in ("STOPPED", "NO_MEDIA_PRESENT") and ours and now - leader.last_audio < 0.15:
                    # The speakers dropped the stream (the bridge restarted, a network hiccup) while the
                    # audio keeps coming: play it again.
                    log.warning("the speakers stopped playing %s's stream", leader.id)
                    zone.restart, zone.speaker_state = True, None
                for ident, volume in volumes.items():
                    sink = self.sinks[ident]
                    if sink.volume is None:
                        sink.volume = volume   # not set by a sender yet: what the speakers have counts
                    sink.speaker_volume = volume
                    if sink.tell_volume and volume is not None:
                        sink.tell_volume = False
                        airplay = round(-30.0 + 30.0 * min(volume, self.args.max_volume) / self.args.max_volume, 2)
                        sink.told_sender = (airplay, now)
                        log.info("%s: the speakers are at volume %s; telling the sender", ident, volume)
                        asyncio.ensure_future(self.remote(sink, "SetAirplayVolume", "d", f"{airplay:.2f}"))
                        continue
                    # A volume the sender didn't ask for, and not while the sender is still moving its own
                    # slider: the speakers' volume keys.
                    if (volume is not None and sink.volume is not None and volume != sink.volume
                            and now - sink.volume_set_at > 2):
                        airplay = round(-30.0 + 30.0 * min(volume, self.args.max_volume) / self.args.max_volume, 2)
                        sink.told_sender = (airplay, now)
                        log.info("%s: volume changed on the speakers to %s (the sender had set %s); telling the sender",
                                 ident, volume, sink.volume)
                        sink.volume = volume
                        asyncio.ensure_future(self.remote(sink, "SetAirplayVolume", "d", f"{airplay:.2f}"))

    async def supervise(self):
        silence = bytes(BYTES_PER_SECOND // 20)   # 50 ms
        while True:
            await asyncio.sleep(0.05)
            now = time.monotonic()
            if now - self.stats_since >= 5:
                for sink in self.sinks.values():
                    if sink.stats["bytes"]:
                        log.info("%s: audio in %d bytes/s (expected %d), peak %d of 32767, listeners %d, backlog %d bytes",
                                 sink.id, sink.stats["bytes"] / (now - self.stats_since), BYTES_PER_SECOND,
                                 sink.stats["peak"], len(sink.listeners),
                                 max((l.queued for l in sink.listeners), default=0))
                    sink.stats = {"bytes": 0, "peak": 0}
                self.stats_since = now
            await self.reconcile(now)
            for sink in [s for s in self.sinks.values() if s.gone]:
                if not any(sink.id in zone.key for zone in self.zones.values()):
                    self.remove_sink(sink)
            for sink in self.sinks.values():
                if not sink.flushed and now - sink.last_audio > 0.6 and not sink.listeners:
                    # Push what the encoder still holds from before the pause out while nobody listens.
                    self.feed(sink, bytes(BYTES_PER_SECOND // 2))
                    sink.flushed = True
            for zone in self.zones.values():
                leader = self.sinks[zone.leader]
                idle = now - leader.last_audio
                if idle > 0.15 and leader.listeners:
                    self.feed(leader, silence)   # keeps the speakers' stream alive until the audio arrives
                elif idle < 0.15 and not leader.listeners and now - zone.last_listener > 15:
                    # Audio is coming in but the speakers aren't fetching it (any more): start over.
                    log.warning("the speakers aren't fetching %s's stream", leader.id)
                    zone.restart, zone.last_listener = True, now

    async def run(self):
        os.makedirs(self.args.run_dir, exist_ok=True)
        server = await asyncio.start_server(self.handle_http, "0.0.0.0", self.args.port)
        log.info("serving on port %d; looking for Raumfeld rooms", self.args.port)
        async with server:
            asyncio.ensure_future(self.discover())
            asyncio.ensure_future(self.observe())
            await self.supervise()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-dir", default="/run/raumfeld-airplay", help="where the sinks' FIFOs and configurations live")
    parser.add_argument("--shairport", default="/usr/local/bin/shairport-sync", help="the shairport-sync to run")
    parser.add_argument("--hook-dir", default=os.path.dirname(os.path.abspath(__file__)),
                        help="where event-hook and volume-hook are")
    parser.add_argument("--airplay-port", type=int, default=7000, help="port of the first room's AirPlay speaker")
    parser.add_argument("--first-offset", type=int, default=0, help="first AirPlay device ID offset to hand out")
    parser.add_argument("--grace", type=float, default=60.0,
                        help="seconds a room may be missing before its AirPlay speaker is removed")
    parser.add_argument("--discover-interval", type=float, default=10.0, help="seconds between looks at the rooms")
    parser.add_argument("--port", type=int, default=8090, help="HTTP port the speakers fetch the streams from")
    parser.add_argument("--host", default="", help="the Raumfeld host's IP; found over SSDP if empty")
    parser.add_argument("--max-volume", type=int, default=50, help="Raumfeld volume (0-100) at full AirPlay volume")
    parser.add_argument("--pause-after", type=float, default=0.4,
                        help="seconds without audio, once it was flowing, before the speakers are stopped")
    parser.add_argument("--idle-timeout", type=float, default=8.0,
                        help="seconds a new session may stay silent before the speakers are stopped again")
    parser.add_argument("--no-search", action="store_true", help="never search the network for the host (for tests)")
    parser.add_argument("--title", default="AirPlay")
    parser.add_argument("--state-dir", default="/var/lib/raumfeld-airplay", help="where the host's address and the rooms' identities are kept")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(Bridge(args).run())


if __name__ == "__main__":
    main()
