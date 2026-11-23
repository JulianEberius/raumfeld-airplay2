# SPDX-License-Identifier: GPL-3.0-or-later
"""The protocol modules against what the real devices sent and expect."""

import base64
import os
import tempfile
import unittest

from raumfeld_airplay import shairport, streaming, upnp
from raumfeld_airplay.raumfeld import RaumfeldHost, parse_device_location, parse_zone_config
from raumfeld_airplay.store import Store, slug

# The host's answers on 2026-11-14, with the kitchen in a zone and the living room in none.
ZONES = b"""<?xml version='1.0' encoding='UTF-8'?>
<zoneConfig numRooms='2' spotifyMode='singleRoom'>
 <zones>
  <zone udn='uuid:zone-1'>
   <room name='K\xc3\xbcche One S' udn='uuid:kitchen' powerState='MANUAL_STANDBY'>
    <renderer udn='uuid:f00e' name='Speaker K\xc3\xbcche One S'></renderer>
   </room>
  </zone>
 </zones>
 <unassignedRooms>
  <room name='Wohnzimmer ' udn='uuid:living' powerState='AUTOMATIC_STANDBY'>
   <renderer udn='uuid:8a7a' name='Streamer Wohnzimmer ' spotifyConnect='active'></renderer>
  </room>
 </unassignedRooms>
</zoneConfig>"""

DEVICES = b"""<devices>
 <device udn='uuid:zone-1' type='urn:schemas-upnp-org:device:MediaRenderer:1' location='http://192.168.0.65:50690/zone-1.xml'>K\xc3\xbcche One S</device>
 <device udn='uuid:other' type='urn:schemas-raumfeld-com:device:ConfigDevice:1' location='http://192.168.0.65:54616/other.xml'>Raumfeld ConfigDevice</device>
</devices>"""

DESCRIPTION = b"""<root xmlns="urn:schemas-upnp-org:device-1-0"><device><serviceList>
 <service><serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType><controlURL>/RenderingService/Control</controlURL></service>
 <service><serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType><controlURL>/TransportService/Control</controlURL></service>
</serviceList></device></root>"""


class RaumfeldAnswers(unittest.TestCase):
    def test_rooms_and_zones(self) -> None:
        config = parse_zone_config(ZONES)
        rooms = config.rooms()
        self.assertEqual(rooms["uuid:kitchen"].name, "Küche One S")
        self.assertEqual(rooms["uuid:living"].name, "Wohnzimmer", "names are trimmed")
        self.assertEqual(rooms["uuid:living"].power_state, "AUTOMATIC_STANDBY")
        self.assertEqual(rooms["uuid:kitchen"].devices, 1)
        self.assertEqual(config.zone_of("uuid:kitchen"), "uuid:zone-1")
        self.assertIsNone(config.zone_of("uuid:living"))

    def test_unplugged_room_is_not_listed(self) -> None:
        only_living = ZONES.replace(ZONES[ZONES.index(b"<zones>"):ZONES.index(b"</zones>")], b"<zones>")
        self.assertEqual(list(parse_zone_config(only_living).rooms()), ["uuid:living"])

    def test_device_location(self) -> None:
        self.assertEqual(parse_device_location(DEVICES, "uuid:zone-1"), "http://192.168.0.65:50690/zone-1.xml")
        self.assertIsNone(parse_device_location(DEVICES, "uuid:nope"))


class FakeHost:
    """A Raumfeld host that applies zone changes like the real one: a zone's UDN changes with its rooms."""

    def __init__(self, zones: dict[str, set[str]], unassigned: set[str]) -> None:
        self.zones, self.unassigned, self.counter = zones, unassigned, 0
        self.calls: list[tuple[str, dict[str, str]]] = []   # the changes asked for

    def fetch(self, url: str) -> bytes:
        endpoint, _, query = url.split("/", 3)[3].partition("?")
        args = dict(pair.split("=") for pair in query.replace("%3A", ":").split("&")) if query else {}
        if endpoint != "getZones":
            self.calls.append((endpoint, args))
        if endpoint == "connectRoomToZone":
            self._remove(args["roomUDN"])
            members = self.zones.pop(args.get("zoneUDN", ""), set()) | {args["roomUDN"]}
            self.counter += 1
            self.zones[f"uuid:zone-{self.counter}"] = members
        elif endpoint == "dropRoomJob":
            self._remove(args["roomUDN"])
            self.unassigned.add(args["roomUDN"])
        def rooms(udns: set[str]) -> str:
            return "".join(f"<room name='{u}' udn='{u}'><renderer udn='r'/></room>" for u in sorted(udns))

        zones = "".join(f"<zone udn='{zone}'>{rooms(members)}</zone>" for zone, members in self.zones.items())
        return f"<zoneConfig><zones>{zones}</zones><unassignedRooms>{rooms(self.unassigned)}</unassignedRooms></zoneConfig>".encode()

    def _remove(self, room: str) -> None:
        self.unassigned.discard(room)
        for zone in list(self.zones):
            self.zones[zone].discard(room)
            if not self.zones[zone]:
                del self.zones[zone]


class FormingZones(unittest.TestCase):
    def host(self, zones: dict[str, set[str]], unassigned: set[str]) -> tuple[RaumfeldHost, FakeHost]:
        fake = FakeHost(zones, unassigned)
        return RaumfeldHost("host", fetch=fake.fetch, sleep=lambda seconds: None), fake

    def test_room_in_no_zone_gets_one(self) -> None:
        host, fake = self.host({}, {"a", "b"})
        zone = host.ensure_zone(["a"])
        self.assertEqual(fake.zones[zone], {"a"})

    def test_zone_that_fits_is_left_alone(self) -> None:
        host, fake = self.host({"z": {"a", "b"}}, set())
        self.assertEqual(host.ensure_zone(["a", "b"]), "z")
        self.assertEqual(fake.calls, [])

    def test_second_room_is_added(self) -> None:
        host, fake = self.host({"z": {"a"}}, {"b"})
        zone = host.ensure_zone(["a", "b"])
        self.assertEqual(fake.zones[zone], {"a", "b"})

    def test_extra_room_is_dropped(self) -> None:
        host, fake = self.host({"z": {"a", "b"}}, set())
        zone = host.ensure_zone(["a"])
        self.assertEqual(fake.zones[zone], {"a"})
        self.assertIn("b", fake.unassigned)

    def test_rooms_from_two_zones_are_joined(self) -> None:
        host, fake = self.host({"z1": {"a"}, "z2": {"b"}}, set())
        zone = host.ensure_zone(["a", "b"])
        self.assertEqual(fake.zones, {zone: {"a", "b"}})


class UPnP(unittest.TestCase):
    def test_control_urls(self) -> None:
        urls = upnp.control_urls(DESCRIPTION, "http://192.168.0.65:50690/zone-1.xml")
        self.assertEqual(urls[upnp.AV_TRANSPORT], "http://192.168.0.65:50690/TransportService/Control")
        self.assertEqual(urls[upnp.RENDERING_CONTROL], "http://192.168.0.65:50690/RenderingService/Control")

    def test_action_and_response(self) -> None:
        sent: list[tuple[str, bytes, dict[str, str]]] = []

        def post(url: str, body: bytes, headers: dict[str, str]) -> bytes:
            sent.append((url, body, headers))
            return (b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                    b'<u:GetRoomVolumeResponse xmlns:u="urn:schemas-upnp-org:service:RenderingControl:1">'
                    b'<CurrentVolume>28</CurrentVolume></u:GetRoomVolumeResponse></s:Body></s:Envelope>')

        renderer = upnp.Renderer("http://192.168.0.65:50690/zone-1.xml", fetch=lambda url: DESCRIPTION, post=post)
        self.assertEqual(renderer.room_volume("uuid:kitchen"), 28)
        url, body, headers = sent[0]
        self.assertEqual(url, "http://192.168.0.65:50690/RenderingService/Control")
        self.assertEqual(headers["SOAPACTION"], '"urn:schemas-upnp-org:service:RenderingControl:1#GetRoomVolume"')
        self.assertIn(b"<InstanceID>0</InstanceID><Room>uuid:kitchen</Room>", body)

    def test_waking_a_room(self) -> None:
        sent: list[tuple[str, bytes, dict[str, str]]] = []

        def post(url: str, body: bytes, headers: dict[str, str]) -> bytes:
            sent.append((url, body, headers))
            return (b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                    b'<u:LeaveStandbyResponse xmlns:u="urn:schemas-upnp-org:service:AVTransport:1"/>'
                    b'</s:Body></s:Envelope>')

        renderer = upnp.Renderer("http://192.168.0.65:50690/zone-1.xml", fetch=lambda url: DESCRIPTION, post=post)
        renderer.leave_standby("uuid:living")
        url, body, headers = sent[0]
        self.assertEqual(url, "http://192.168.0.65:50690/TransportService/Control")
        self.assertEqual(headers["SOAPACTION"], '"urn:schemas-upnp-org:service:AVTransport:1#LeaveStandby"')
        self.assertIn(b"<InstanceID>0</InstanceID><Room>uuid:living</Room>", body)

    def test_stream_metadata_escapes(self) -> None:
        didl = upnp.stream_didl("http://pi:8090/stream/a.mp3?x=1&y=2", "Rock & Roll", streaming.MP3.protocol_info)
        self.assertIn("Rock &amp; Roll", didl)
        self.assertIn("x=1&amp;y=2", didl)
        self.assertIn("audio/mpeg", didl)


class Shairport(unittest.TestCase):
    def item(self, code: str, value: str = "") -> bytes:
        data = f'\n<data encoding="base64">\n{base64.b64encode(value.encode()).decode()}</data>' if value else ""
        return f"<item><type>73736e63</type><code>{code.encode().hex()}</code><length>{len(value)}</length>{data}</item>\n".encode()

    def test_metadata_items(self) -> None:
        feed = shairport.MetadataFeed()
        data = self.item("clip", "192.168.0.56") + self.item("pbeg") + self.item("mdst") + self.item("paus")
        self.assertEqual(feed.feed(data), [("clip", "192.168.0.56"), ("pbeg", ""), ("paus", "")])

    def test_metadata_arrives_in_pieces(self) -> None:
        feed = shairport.MetadataFeed()
        first, second = self.item("snam", "Julians MacBook Pro"), self.item("pres")
        data, cut = first + second, len(first) + 10
        self.assertEqual(feed.feed(data[:30]), [])
        self.assertEqual(feed.feed(data[30:cut]), [("snam", "Julians MacBook Pro")])
        self.assertEqual(feed.feed(data[cut:]), [("pres", "")])

    def test_configuration(self) -> None:
        conf = shairport.configuration('Küche "One" S', 7000, 0, "kuecheones", "/run/x/kuecheones",
                                       "/run/x/kuecheones.meta", "/opt/raumfeld-airplay")
        for line in ('name = "Küche \'One\' S";', "port = 7000;", "airplay_device_id_offset = 0;",
                     'dbus_nametag = "kuecheones";', 'output_backend = "pipe";', 'ignore_volume_control = "yes";',
                     'name = "/run/x/kuecheones";', "output_rate = 44100;", 'output_format = "S16_LE";',
                     'pipe_name = "/run/x/kuecheones.meta";',
                     'run_this_when_volume_is_set = "/opt/raumfeld-airplay/volume-hook kuecheones";',
                     '"/opt/raumfeld-airplay/event-hook kuecheones active-ends";'):
            self.assertIn(line, conf)

    def test_remote_command(self) -> None:
        self.assertEqual(shairport.remote_command("wohnzimmer", "SetAirplayVolume", "d", "-15.00"),
                         ["busctl", "--system", "call", "--", "org.gnome.ShairportSync.wohnzimmer",
                          "/org/gnome/ShairportSync", "org.gnome.ShairportSync.RemoteControl", "SetAirplayVolume", "d", "-15.00"])


class Nqptp(unittest.TestCase):
    def test_another_nqptp_is_told_from_a_real_failure(self) -> None:
        # What nqptp 1.2.9 printed on the Pi with a second one running, and without permission.
        taken = 'fatal error: nqptp is unable to listen on port 319. The error is: 98, "Address already in use".'
        denied = "fatal error: nqptp does not have permission to access port 319. It must (a) [Linux only] have ..."
        self.assertTrue(shairport.nqptp_ports_taken(taken))
        self.assertFalse(shairport.nqptp_ports_taken(denied))
        self.assertFalse(shairport.nqptp_ports_taken(""))


class Streaming(unittest.TestCase):
    def test_silence_is_whole_frames(self) -> None:
        self.assertEqual(len(streaming.silence(0.05)), 8820)
        self.assertEqual(len(streaming.silence(0.5)) % 4, 0)

    def test_listener_that_falls_behind_is_refused(self) -> None:
        listener = streaming.Listener("192.168.0.65", streaming.MP3)
        chunk = bytes(8192)
        accepted = 0
        while listener.offer(chunk):
            accepted += 1
        self.assertGreater(accepted * len(chunk), 6 * streaming.MP3.bytes_per_second)
        listener.abandon()
        self.assertEqual(listener.queued, 0)
        self.assertIsNone(listener.queue.get_nowait(), "told to hang up")


class HeadStart(unittest.TestCase):
    def test_a_new_speaker_gets_the_last_seconds_of_the_stream(self) -> None:
        recent = streaming.RecentStream(2.0)
        for second in range(5):
            recent.add(bytes([second]), 100.0 + second)
        self.assertEqual(recent.since(104.5), [b"\x03", b"\x04"])

    def test_nothing_when_the_stream_has_been_quiet(self) -> None:
        recent = streaming.RecentStream(2.0)
        recent.add(b"before the pause", 100.0)
        self.assertEqual(recent.since(160.0), [])

    def test_it_starts_where_a_flac_frame_does(self) -> None:
        fmt = streaming.FORMATS["flac"]
        listener = streaming.Listener("speaker", fmt)
        recent = streaming.RecentStream(2.0)
        frame = streaming.FLAC_FRAME_SYNC + b"frame"
        for at, data in ((1.0, b"middle of a frame"), (1.5, frame), (1.9, b"more")):
            recent.add(data, at)
        for data in recent.since(2.0):
            listener.offer(data, b"fLaC header")
        self.assertEqual(listener.queue.get_nowait(), b"fLaC header" + frame)
        self.assertEqual(listener.queue.get_nowait(), b"more")


class Flac(unittest.TestCase):
    # "fLaC", a STREAMINFO block (type 0, 34 bytes) and a last block (type 4 with the last-flag, 8 bytes)
    HEADER = b"fLaC" + b"\x00\x00\x00\x22" + bytes(34) + b"\x84\x00\x00\x08" + bytes(8)
    FRAME = b"\xff\xf8" + bytes(100)

    def test_header_length(self) -> None:
        self.assertEqual(streaming.flac_header_length(self.HEADER + self.FRAME), len(self.HEADER))
        self.assertIsNone(streaming.flac_header_length(self.HEADER[:20]), "not all of it yet")
        self.assertIsNone(streaming.flac_header_length(self.HEADER[:-1]))
        with self.assertRaises(ValueError):
            streaming.flac_header_length(b"ID3\x04" + bytes(40))

    def test_listener_joining_late_gets_the_header_and_starts_at_a_frame(self) -> None:
        listener = streaming.Listener("192.168.0.65", streaming.FLAC)
        self.assertTrue(listener.offer(self.FRAME), "no header known yet: wait")
        self.assertTrue(listener.offer(bytes(50), self.HEADER), "the middle of a frame: wait")
        self.assertTrue(listener.queue.empty())
        listener.offer(self.FRAME, self.HEADER)
        self.assertEqual(listener.queue.get_nowait(), self.HEADER + self.FRAME)
        listener.offer(bytes(50), self.HEADER)
        self.assertEqual(listener.queue.get_nowait(), bytes(50), "once started, everything is passed on")

    def test_mp3_listener_needs_no_header(self) -> None:
        listener = streaming.Listener("192.168.0.65", streaming.MP3)
        listener.offer(b"anything")
        self.assertEqual(listener.queue.get_nowait(), b"anything")


class Titles(unittest.TestCase):
    """Track titles inside the stream, the way internet radio sends them."""

    def test_only_listeners_that_ask_are_sent_titles(self) -> None:
        asking = b"GET /stream/kueche.flac HTTP/1.1\r\nHost: pi\r\nIcy-MetaData: 1\r\n\r\n"
        plain = b"GET /stream/kueche.flac HTTP/1.1\r\nHost: pi\r\n\r\n"
        self.assertTrue(streaming.wants_titles(asking))
        self.assertFalse(streaming.wants_titles(plain))
        self.assertIn(b"icy-metaint: 16000\r\n", streaming.stream_response(streaming.FLAC, True, "AirPlay"))
        self.assertNotIn(b"icy", streaming.stream_response(streaming.FLAC))
        self.assertIsNone(streaming.Listener("peer", streaming.FLAC).titles)

    def test_block_is_padded_to_units_of_sixteen(self) -> None:
        block = streaming.icy_block("Ezra Collective - Sweet Echo")
        self.assertEqual(block[0] * 16, len(block) - 1)
        self.assertEqual(block[1:].rstrip(b"\0"), b"StreamTitle='Ezra Collective - Sweet Echo';")
        self.assertEqual(streaming.icy_block(None), b"\0")

    def test_title_cannot_end_the_block_early_or_overflow_it(self) -> None:
        self.assertEqual(streaming.icy_block("a';b").count(b"';"), 1)
        long = streaming.icy_block("\u00fc" * 5000)
        self.assertLessEqual(len(long), 1 + 255 * 16)
        long[1:].rstrip(b"\0").decode()   # still whole characters

    def test_blocks_fall_at_the_interval_whatever_the_piece_sizes(self) -> None:
        weaver = streaming.TitleWeaver()
        audio = bytes(range(256)) * 200   # 51200 bytes: three blocks
        woven = b"".join(weaver.weave(audio[at:at + 7001], "A - B" if at > 20000 else "")
                         for at in range(0, len(audio), 7001))
        recovered, titles, position = b"", [], 0
        while position < len(woven):
            recovered += woven[position:position + streaming.ICY_INTERVAL]
            position += streaming.ICY_INTERVAL
            if position < len(woven):
                length = woven[position] * 16
                titles.append(woven[position + 1:position + 1 + length].rstrip(b"\0"))
                position += 1 + length
        self.assertEqual(recovered, audio)
        self.assertEqual(titles, [b"", b"StreamTitle='A - B';", b""])   # said once, then no news


class Identities(unittest.TestCase):
    def test_slug(self) -> None:
        self.assertEqual([slug(name) for name in ("Küche One S", "Wohnzimmer", "2. Stock", "Bad & WC", "")],
                         ["kuecheones", "wohnzimmer", "r2stock", "badwc", "room"])

    def test_identity_is_lasting_and_unique(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            kitchen = store.identity("uuid:kitchen", "Küche One S")
            self.assertEqual((kitchen.id, kitchen.offset, kitchen.port), ("kuecheones", 0, 7000))
            living = store.identity("uuid:living", "Wohnzimmer")
            self.assertEqual((living.id, living.offset, living.port), ("wohnzimmer", 1, 7001))
            self.assertEqual(store.identity("uuid:third", "Wohnzimmer").id, "wohnzimmer2", "a second room of that name")
            self.assertEqual(store.identity("uuid:kitchen", "Renamed"), kitchen, "a rename keeps the identity")
            self.assertEqual(Store(directory).identity("uuid:living", "x"), living, "and so does a restart")

    def test_host_is_remembered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(Store(directory).host())
            Store(directory).remember_host("192.168.0.65")
            self.assertEqual(Store(directory).host(), "192.168.0.65")
            self.assertTrue(os.path.exists(os.path.join(directory, "host")))


if __name__ == "__main__":
    unittest.main()
