"""The bridge: ties the AirPlay side (one shairport-sync per room) to the Raumfeld side.

It keeps a sink for every room the Raumfeld host lists, moves each sink's audio from its FIFO
through an encoder to the HTTP stream the speakers fetch, and keeps the zones that play in
line with what the decision rules in `model` ask for. Everything runs on one asyncio loop;
calls to the Raumfeld system block and therefore run in threads.
"""

from __future__ import annotations

import array
import asyncio
import logging
import os
import socket
import time
import urllib.parse
from dataclasses import dataclass

from . import shairport, streaming
from .model import Sink, plan_zones
from .raumfeld import HOST_PORT, RaumfeldHost, Room, discover_host
from .store import Identity, Store
from .streaming import Listener
from .upnp import PAUSED_STATE, PLAYING_STATES, STOPPED_STATES, Renderer

log = logging.getLogger("raumfeld-airplay")

LOCAL_ADDRESSES = ("127.0.0.1", "::1")
NO_CONTENT = b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n"
NOT_FOUND = b"HTTP/1.1 404 Not Found\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"


@dataclass(frozen=True)
class Settings:
    run_dir: str = "/run/raumfeld-airplay"       # the sinks' FIFOs and configuration files
    state_dir: str = "/var/lib/raumfeld-airplay"  # the host's address and the rooms' identities
    hook_dir: str = "/opt/raumfeld-airplay"       # where event-hook and volume-hook are
    shairport: str = "/usr/local/bin/shairport-sync"
    port: int = 8090                # HTTP: streams for the speakers, hooks and status locally
    airplay_port: int = 7000        # port of the first room's AirPlay speaker
    first_offset: int = 0           # first AirPlay device ID offset to hand out
    host: str = ""                  # the Raumfeld host's address; found over SSDP if empty
    no_search: bool = False         # never search the network for the host (for tests)
    max_volume: int = 50            # Raumfeld volume (0-100) at full AirPlay volume
    pause_after: float = 0.4        # seconds without audio, once it was flowing, before the speakers are stopped
    idle_timeout: float = 8.0       # seconds a new session may stay silent before the speakers are stopped again
    grace: float = 60.0             # seconds a room may be missing before its AirPlay speaker is removed
    discover_interval: float = 10.0  # seconds between looks at the rooms
    title: str = "AirPlay"          # what the speakers show as playing
    format: str = "mp3"             # what the speakers are sent: one of streaming.FORMATS


class LiveSink(Sink):
    """A sink with what it takes to run it: its shairport-sync, FIFOs, encoder and listeners."""

    def __init__(self, identity: Identity, room: Room, run_dir: str, fmt: streaming.Format) -> None:
        super().__init__(identity.id, room.udn, room.name)
        self.port = identity.port
        self.offset = identity.offset
        self.fifo = os.path.join(run_dir, identity.id)                  # PCM from shairport-sync
        self.metadata_fifo = os.path.join(run_dir, identity.id + ".meta")
        self.conf = os.path.join(run_dir, identity.id + ".conf")
        self.path = streaming.stream_path(identity.id, fmt)
        self.stream_header = b""    # what the encoder's output began with, for formats that have a header
        self.metadata = shairport.MetadataFeed()
        self.process: asyncio.subprocess.Process | None = None   # its shairport-sync
        self.encoder: asyncio.subprocess.Process | None = None   # its ffmpeg
        self.tasks: list[asyncio.Task[None]] = []
        self.fds: list[int] = []
        self.listeners: set[Listener] = set()
        self.renamed = False        # its shairport-sync was stopped to take the room's new name
        self.flushed = True         # the encoder holds no leftover audio from before a pause
        self.stats = {"bytes": 0, "peak": 0}


class Zone:
    """A Raumfeld zone this bridge is playing a stream in."""

    def __init__(self, key: frozenset[str], leader: str, renderer: Renderer, url: str) -> None:
        self.key = key                # ids of the sinks whose rooms form the zone
        self.leader = leader          # id of the sink whose stream the zone plays
        self.renderer = renderer
        self.url = url
        self.restart = False          # the speakers stopped fetching or playing the stream
        self.speaker_state: str | None = None   # the transport state the speakers last reported
        self.last_listener = time.monotonic()
        self.located_at = time.monotonic()      # when the rooms' whereabouts were last checked
        self.strays: set[str] = set()           # sinks whose rooms weren't with the zone at that check


class Bridge:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = Store(settings.state_dir, settings.airplay_port, settings.first_offset)
        self.format = streaming.FORMATS[settings.format]
        self.sinks: dict[str, LiveSink] = {}            # one per room the Raumfeld host lists
        self.zones: dict[frozenset[str], Zone] = {}
        self.seen_rooms: dict[str, Room] = {}           # rooms as last listed, for the log
        self.host_reachable: bool | None = None
        self.own_ip = ""
        self.lock = asyncio.Lock()                      # held while zones are rearranged
        self.retry_after = 0.0                          # no new attempt before this time, after a failed one
        self.stats_since = time.monotonic()

    # ------------------------------------------------------------ audio and metadata in

    @staticmethod
    def open_fifo(path: str) -> int:
        if not os.path.exists(path):
            os.mkfifo(path, 0o666)
        os.chmod(path, 0o666)
        # Read-write so the FIFO never reports end-of-file when shairport-sync closes its end.
        return os.open(path, os.O_RDWR | os.O_NONBLOCK)

    def on_audio(self, fd: int, sink: LiveSink) -> None:
        try:
            data = os.read(fd, 65536)
        except BlockingIOError:
            return
        if not data:
            return
        sink.audio_arrived(time.monotonic())
        sink.flushed = False
        sink.stats["bytes"] += len(data)
        if log.isEnabledFor(logging.DEBUG):   # the level is only reported in the verbose statistics
            samples = array.array("h", data[:len(data) & ~1][:8192])
            if samples:
                sink.stats["peak"] = max(sink.stats["peak"], max(samples), -min(samples))
        self.feed(sink, data)

    def on_metadata(self, fd: int, sink: LiveSink) -> None:
        try:
            data = os.read(fd, 65536)
        except BlockingIOError:
            return
        for code, value in sink.metadata.feed(data):
            log.debug("%s: sender reports %s %s", sink.id, shairport.REPORTS[code], value[:60])
            sink.sender_report(code, value, time.monotonic())

    def feed(self, sink: LiveSink, pcm: bytes) -> None:
        """PCM on its way to the sink's listeners, through its encoder."""
        if sink.encoder and sink.encoder.returncode is None and sink.encoder.stdin:
            sink.encoder.stdin.write(pcm)

    def broadcast(self, sink: LiveSink, data: bytes) -> None:
        for listener in list(sink.listeners):
            if not listener.offer(data, sink.stream_header):
                log.warning("%s: listener %s stopped reading; dropping it", sink.id, listener.peer)
                sink.listeners.discard(listener)
                listener.abandon()
                for zone in self.zones.values():
                    if zone.leader == sink.id:
                        zone.restart = True

    async def run_encoder(self, sink: LiveSink) -> None:
        """Keeps the sink's ffmpeg running and passes what it produces on to the listeners."""
        while True:
            sink.encoder = await asyncio.create_subprocess_exec(
                *streaming.encoder_command(self.format), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
            assert sink.encoder.stdout
            sink.stream_header, beginning = b"", b""
            # Large reads, so that what is passed on begins where the encoder began a frame.
            while data := await sink.encoder.stdout.read(65536):
                if self.format.has_header and not sink.stream_header:
                    # Listeners can join at any time, and each needs the header the stream began with.
                    beginning += data
                    length = streaming.flac_header_length(beginning)
                    if length is None:
                        continue
                    sink.stream_header, data = beginning[:length], beginning[length:]
                    if not data:
                        continue
                self.broadcast(sink, data)
            log.error("%s: the encoder stopped (%s); restarting it", sink.id, await sink.encoder.wait())
            await asyncio.sleep(1)

    # ------------------------------------------------------------ HTTP

    async def handle_http(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peername = writer.get_extra_info("peername")
        peer = str(peername[0]) if peername else "?"
        local = peer in LOCAL_ADDRESSES
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
            method, target, *_ = request.split(b"\r\n", 1)[0].decode(errors="replace").split(" ") + ["", ""]
            path, _, query_string = target.partition("?")
            query = {key: values[0] for key, values in urllib.parse.parse_qs(query_string).items()}
            stream_of = next((sink for sink in self.sinks.values() if sink.path == path), None)
            sink = self.sinks.get(query.get("sink", ""))
            if stream_of:
                await self.serve_stream(stream_of, method, writer, peer, streaming.wants_titles(request))
            elif path == "/volume" and local and sink:      # from volume-hook
                asyncio.ensure_future(self.sender_set_volume(sink, query.get("airplay", "")))
                writer.write(NO_CONTENT)
            elif path == "/event" and local and sink:       # from event-hook
                name = query.get("name", "?")
                log.debug("%s: event %s", sink.id, name)
                sink.session_event(name, time.monotonic())
                writer.write(NO_CONTENT)
            elif path == "/status":
                body = self.status().encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\nConnection: close\r\n"
                             + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            else:
                writer.write(NOT_FOUND)
            await writer.drain()
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()

    async def serve_stream(self, sink: LiveSink, method: str, writer: asyncio.StreamWriter, peer: str,
                           titles: bool) -> None:
        writer.write(streaming.stream_response(self.format, titles, self.settings.title))
        if method == "HEAD":
            return
        listener = Listener(peer, self.format, titles)
        log.debug("%s: listener connected: %s%s", sink.id, peer, ", wants track titles" if titles else "")
        playing = any(zone.leader == sink.id for zone in self.zones.values())
        if not playing and not self.lock.locked() and sink.play_key_pressed(time.monotonic()):
            log.info("%s: play pressed on the speakers; asking the sender to play", sink.id)
            asyncio.ensure_future(self.remote(sink, "Play"))
        sink.listeners.add(listener)
        try:
            while (data := await listener.queue.get()) is not None:
                listener.queued -= len(data)
                if listener.titles:
                    data = listener.titles.weave(data, sink.now_playing.text(time.monotonic()))
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
            log.debug("%s: listener gone: %s after %d bytes", sink.id, peer, listener.sent)

    def status(self) -> str:
        now = time.monotonic()
        lines = [f"{s.id}: room={s.room_name!r} port={s.port} audio_age={now - s.last_audio:.1f}s "
                 f"session_open={s.session_open} sender={s.client} volume={s.volume} listeners={len(s.listeners)}"
                 f"{' with titles' if any(l.titles for l in s.listeners) else ''} track={s.now_playing.text(now)!r}"
                 f"{' yielded' if s.yielded else ''}" for s in self.sinks.values()]
        lines += [f"zone {'+'.join(sorted(zone.key))}: stream of {zone.leader}" for zone in self.zones.values()]
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------ rooms come and go

    def add_sink(self, room: Room) -> None:
        sink = LiveSink(self.store.identity(room.udn, room.name), room, self.settings.run_dir, self.format)
        loop = asyncio.get_running_loop()
        for path, handler in ((sink.fifo, self.on_audio), (sink.metadata_fifo, self.on_metadata)):
            fd = self.open_fifo(path)
            sink.fds.append(fd)
            loop.add_reader(fd, handler, fd, sink)
        sink.tasks = [asyncio.ensure_future(self.run_encoder(sink)), asyncio.ensure_future(self.run_shairport(sink))]
        self.sinks[sink.id] = sink
        log.info("%s: AirPlay speaker %r added (port %d)", sink.id, room.name, sink.port)

    def remove_sink(self, sink: LiveSink) -> None:
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

    async def run_shairport(self, sink: LiveSink) -> None:
        """Keeps the sink's shairport-sync running and passes what it prints on to the log."""
        while True:
            with open(sink.conf, "w") as file:
                file.write(shairport.configuration(sink.room_name, sink.port, sink.offset, sink.id, sink.fifo,
                                                   sink.metadata_fifo, self.settings.hook_dir))
            started = time.monotonic()
            sink.process = await asyncio.create_subprocess_exec(
                self.settings.shairport, "-c", sink.conf, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            assert sink.process.stdout
            async for line in sink.process.stdout:
                if line.strip():
                    log.info("%s: shairport-sync: %s", sink.id, line.decode(errors="replace").rstrip())
            code = await sink.process.wait()
            if sink.renamed:
                sink.renamed = False
                continue   # stopped by the bridge to take the room's new name
            log.error("%s: shairport-sync stopped (%s); starting it again", sink.id, code)
            await asyncio.sleep(1 if time.monotonic() - started > 30 else 5)

    def update_rooms(self, rooms: dict[str, Room] | None, now: float) -> None:
        """Adds sinks for the rooms the host lists and marks those it has stopped listing.
        `rooms` is None while the host can't be reached."""
        if (rooms is not None) != self.host_reachable:
            self.host_reachable = rooms is not None
            log.info("the Raumfeld host %s", "answers" if self.host_reachable else "doesn't answer")
        by_room = {sink.room_udn: sink for sink in self.sinks.values()}
        for room in sorted((rooms or {}).values(), key=lambda room: room.name):
            if self.seen_rooms.get(room.udn) != room:
                log.debug("room %r: power state %s, %d device(s)", room.name, room.power_state, room.devices)
                self.seen_rooms[room.udn] = room
            sink = by_room.get(room.udn)
            if not room.devices:
                continue   # a room without a device can't play
            if sink is None:
                self.add_sink(room)
            elif sink.room_name != room.name and sink.process and sink.process.returncode is None:
                log.info("%s: the room is now called %r", sink.id, room.name)
                sink.room_name, sink.renamed = room.name, True
                sink.process.terminate()   # comes back under the new name
        if rooms is not None:
            for udn in [udn for udn in self.seen_rooms if udn not in rooms]:
                log.info("room %r is no longer listed", self.seen_rooms.pop(udn).name)
        for sink in self.sinks.values():
            room_now = (rooms or {}).get(sink.room_udn)
            if room_now and room_now.devices:
                sink.room_listed()
            elif sink.room_missing(now, self.settings.grace):
                log.info("%s: room %r has been away for %d s", sink.id, sink.room_name, self.settings.grace)

    async def discover(self) -> None:
        while True:
            rooms: dict[str, Room] | None
            try:
                rooms = await asyncio.to_thread(lambda: self.connect().zone_config().rooms())
            except Exception as error:
                log.debug("couldn't list the rooms: %s", error)
                rooms = None
            self.update_rooms(rooms, time.monotonic())
            await asyncio.sleep(self.settings.discover_interval)

    # ------------------------------------------------------------ Raumfeld control (blocking)

    def connect(self) -> RaumfeldHost:
        """Finds the Raumfeld host: where it was last time first, since it doesn't always answer
        the search, e.g. while waking up."""
        candidates = [self.settings.host, self.store.host(), "search"]
        for address in dict.fromkeys(candidate for candidate in candidates if candidate):
            if address == "search":
                found = None if self.settings.no_search else discover_host()
                if not found:
                    continue
                address = found
            host = RaumfeldHost(address)
            try:
                host.zone_config()
            except OSError as error:
                log.debug("no Raumfeld host at %s: %s", address, error)
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((address, HOST_PORT))
                self.own_ip = str(sock.getsockname()[0])
            self.store.remember_host(address)
            return host
        raise RuntimeError("no Raumfeld host found on the network")

    def start_zone(self, key: frozenset[str], leader: str, force: bool) -> Zone:
        """Groups the rooms of `key` into one zone and has it play the leader's stream."""
        host = self.connect()
        sinks = [self.sinks[ident] for ident in sorted(key)]
        renderer = host.renderer(host.ensure_zone(sink.room_udn for sink in sinks))
        url = f"http://{self.own_ip}:{self.settings.port}{self.sinks[leader].path}"
        for sink in sinks:
            if sink.volume is not None:
                renderer.set_room_volume(sink.room_udn, sink.volume)
        # A zone that just gained or lost a room may still be playing the stream: leave it be then.
        if force or renderer.current_uri() != url or renderer.state() not in PLAYING_STATES:
            renderer.play_stream(url, self.settings.title, self.format.protocol_info)
        log.info("playing %s's stream on %s", leader, " + ".join(sink.room_name for sink in sinks))
        return Zone(key, leader, renderer, url)

    def stop_zone(self, zone: Zone) -> None:
        # Leave the speakers alone if somebody switched them to another source meanwhile.
        if zone.renderer.current_uri() == zone.url:
            zone.renderer.stop()
        log.info("stopped %s", " + ".join(self.sinks[ident].room_name for ident in sorted(zone.key)))

    def read_speakers(self, zone: Zone) -> tuple[str, dict[str, int | None], str]:
        """The zone's transport state, its rooms' volumes and what it plays, as the speakers report them."""
        volumes = {ident: zone.renderer.room_volume(self.sinks[ident].room_udn) for ident in zone.key}
        return zone.renderer.state(), volumes, zone.renderer.current_uri()

    def locate(self, zone: Zone) -> tuple[Renderer | None, list[str]]:
        """Where the zone's rooms are now: the renderer of the Raumfeld zone that holds the leader's
        room (None if it is in no zone), and the ids of the sinks whose rooms aren't with it."""
        host = self.connect()
        config = host.zone_config()
        home = config.zone_of(self.sinks[zone.leader].room_udn)
        strays = [ident for ident in zone.key if home is None or config.zone_of(self.sinks[ident].room_udn) != home]
        return (host.renderer(home) if home else None), strays

    # ------------------------------------------------------------ keeping zones in line with the plan

    async def reconcile(self, now: float) -> None:
        """Brings the zones that play in line with the sinks that should."""
        playing = {key: zone.leader for key, zone in self.zones.items()}
        plan = plan_zones(self.sinks.values(), playing, now, self.settings.pause_after, self.settings.idle_timeout)
        if plan == playing and not any(zone.restart for zone in self.zones.values()):
            return
        if now < self.retry_after:
            return
        async with self.lock:
            forced: set[frozenset[str]] = set()
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
                    except OSError as error:
                        # Raumfeld has dissolved the zone already, as when its rooms were regrouped.
                        log.debug("the zone to stop is gone: %s", error)
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

    def let_go(self, zone: Zone, idents: set[str] | frozenset[str], reason: str) -> None:
        """Stops claiming these rooms of the zone: somebody chose something else for them."""
        for ident in idents:
            self.sinks[ident].let_go()
        log.info("%s: %s; leaving %s alone until the sender plays again", "+".join(sorted(zone.key)), reason,
                 " + ".join(self.sinks[ident].room_name for ident in sorted(idents)))
        if set(idents) == set(zone.key):
            # Nothing of this session is heard any more: have the sender show that.
            asyncio.ensure_future(self.remote(self.sinks[zone.leader], "Pause"))

    # ------------------------------------------------------------ volume and the speakers' keys

    async def sender_set_volume(self, sink: LiveSink, value: str) -> None:
        """The sender moved its volume: -30 (quiet) to 0 (full) dB, -144 for mute."""
        try:
            airplay = float(value)
        except ValueError:
            return
        volume = sink.sender_set_volume(airplay, time.monotonic(), self.settings.max_volume)
        if volume is None:
            log.debug("%s: not applying the sender's volume (AirPlay %.1f)", sink.id, airplay)
            return
        log.debug("%s: volume %s (AirPlay %.1f)", sink.id, volume, airplay)
        zone = next((zone for zone in self.zones.values() if sink.id in zone.key), None)
        if zone:
            try:
                await asyncio.to_thread(zone.renderer.set_room_volume, sink.room_udn, volume)
            except Exception as error:
                log.error("couldn't set the volume: %s", error)

    async def remote(self, sink: LiveSink, method: str, *arguments: str) -> None:
        """Sends a remote-control command back to the sink's sender."""
        try:
            process = await asyncio.create_subprocess_exec(
                *shairport.remote_command(sink.id, method, *arguments),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, errors = await asyncio.wait_for(process.communicate(), timeout=5)
            if process.returncode:
                log.error("%s: remote command %s failed: %s", sink.id, method, errors.decode(errors="replace").strip())
        except (OSError, asyncio.TimeoutError) as error:
            log.error("%s: remote command %s failed: %s", sink.id, method, error)

    async def observe(self) -> None:
        """Watches the speakers of the playing zones for what the bridge didn't do itself: a press
        of their keys, which is passed back to the sender, and another source or grouping chosen
        for them, which the bridge yields to."""
        while True:
            await asyncio.sleep(0.5)
            for zone in list(self.zones.values()):
                await self.observe_zone(zone, time.monotonic())

    async def observe_zone(self, zone: Zone, now: float) -> None:
        def current() -> bool:
            return self.zones.get(zone.key) is zone and not self.lock.locked()   # not stopped or being rearranged

        reading: tuple[str, dict[str, int | None], str] | None
        try:
            reading = await asyncio.to_thread(self.read_speakers, zone)
        except Exception as error:
            log.debug("couldn't read the speakers: %s", error)
            reading = None
        if not current():
            return
        if reading is None or now - zone.located_at > 3:
            # Are the rooms still together? Somebody may have regrouped them, e.g. in the Raumfeld app.
            zone.located_at = now
            try:
                renderer, strays = await asyncio.to_thread(self.locate, zone)
            except Exception as error:
                log.debug("couldn't locate the rooms: %s", error)
                return
            if not current():
                return
            # Acted on only when seen twice in a row: the host's list can be in flux for a moment.
            confirmed, zone.strays = zone.strays & set(strays), set(strays)
            if confirmed:
                self.let_go(zone, confirmed, "rooms were regrouped")
                return
            if strays or renderer is None:
                return
            zone.renderer = renderer   # the zone's renderer changes when rooms are added to it
        if reading is None:
            return
        state, volumes, uri = reading
        leader = self.sinks[zone.leader]
        if uri and uri != zone.url:
            self.let_go(zone, zone.key, "the speakers were given another source")
            return
        if state != zone.speaker_state:
            log.debug("speakers of %s report state %s (was %s)", "+".join(sorted(zone.key)), state, zone.speaker_state)
            if state == PAUSED_STATE and zone.speaker_state in PLAYING_STATES and not leader.paused_at:
                log.info("%s: pause pressed on the speakers; asking the sender to pause", leader.id)
                asyncio.ensure_future(self.remote(leader, "Pause"))
            zone.speaker_state = state
        if state in STOPPED_STATES and now - leader.last_audio < 0.15:
            # The speakers dropped the stream (the bridge restarted, a network hiccup) while the
            # audio keeps coming: play it again.
            log.warning("the speakers stopped playing %s's stream", leader.id)
            zone.restart, zone.speaker_state = True, None
        for ident, volume in volumes.items():
            airplay = self.sinks[ident].speakers_report_volume(volume, now, self.settings.max_volume)
            if airplay is not None:
                log.debug("%s: the speakers are at volume %s; telling the sender", ident, volume)
                asyncio.ensure_future(self.remote(self.sinks[ident], "SetAirplayVolume", "d", f"{airplay:.2f}"))

    # ------------------------------------------------------------ the main loop

    def log_statistics(self, now: float) -> None:
        for sink in self.sinks.values():
            if sink.stats["bytes"]:
                log.debug("%s: audio in %d bytes/s (expected %d), peak %d of 32767, listeners %d, backlog %d bytes",
                          sink.id, sink.stats["bytes"] / (now - self.stats_since), streaming.PCM_BYTES_PER_SECOND,
                          sink.stats["peak"], len(sink.listeners), max((l.queued for l in sink.listeners), default=0))
            sink.stats = {"bytes": 0, "peak": 0}
        self.stats_since = now

    async def supervise(self) -> None:
        gap_filler = streaming.silence(0.05)
        while True:
            await asyncio.sleep(0.05)
            now = time.monotonic()
            if now - self.stats_since >= 5:
                self.log_statistics(now)
            await self.reconcile(now)
            for sink in [sink for sink in self.sinks.values() if sink.gone]:
                if not any(sink.id in zone.key for zone in self.zones.values()):
                    self.remove_sink(sink)
            for sink in self.sinks.values():
                if not sink.flushed and now - sink.last_audio > 0.6 and not sink.listeners:
                    # Push what the encoder still holds from before the pause out while nobody listens.
                    self.feed(sink, streaming.silence(0.5))
                    sink.flushed = True
            for zone in self.zones.values():
                leader = self.sinks[zone.leader]
                idle = now - leader.last_audio
                if idle > 0.15 and leader.listeners:
                    self.feed(leader, gap_filler)   # keeps the speakers' stream alive until the audio arrives
                elif idle < 0.15 and not leader.listeners and now - zone.last_listener > 15:
                    # Audio is coming in but the speakers aren't fetching it (any more): start over.
                    log.warning("the speakers aren't fetching %s's stream", leader.id)
                    zone.restart, zone.last_listener = True, now

    async def run(self) -> None:
        os.makedirs(self.settings.run_dir, exist_ok=True)
        server = await asyncio.start_server(self.handle_http, "0.0.0.0", self.settings.port)
        log.info("serving on port %d; looking for Raumfeld rooms", self.settings.port)
        async with server:
            asyncio.ensure_future(self.discover())
            asyncio.ensure_future(self.observe())
            await self.supervise()
