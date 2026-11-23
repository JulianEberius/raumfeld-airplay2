# SPDX-License-Identifier: GPL-3.0-or-later
"""Cutting the audio into tracks, as scenarios on a simulated clock."""

import json
import unittest

from raumfeld_airplay import archive
from raumfeld_airplay.archive import Recorder, Track
from raumfeld_airplay.streaming import PCM_BYTES_PER_SECOND

T = 1_800_000_000.0   # an arbitrary "now", Unix time
CHUNK = 0.25          # seconds of audio per read from the FIFO


class FakeWriter:
    def __init__(self, stem: str) -> None:
        self.stem, self.data, self.finished = stem, bytearray(), False
        self.kept: Track | None = None

    def write(self, pcm: bytes) -> None:
        assert not self.finished
        self.data += pcm

    def finish(self, track: Track | None) -> None:
        self.finished, self.kept = True, track

    @property
    def seconds(self) -> float:
        return len(self.data) / PCM_BYTES_PER_SECOND


def track(title: str, start: float, duration: float, number: int = 1, elapsed: float = 0.0,
          playing: bool = True) -> Track:
    return Track(key=title, title=title, artist="Ezra Collective", album="Dance, No One's Watching",
                 number=number, duration=duration, elapsed=elapsed, stamp=start + elapsed, playing=playing)


class Scenario:
    """A recorder, the writers it opened, and a clock that audio arrives by."""

    def __init__(self, existing: tuple[str, ...] = ()) -> None:
        self.writers: list[FakeWriter] = []
        self.recorder = Recorder("/archive", self.open, exists=lambda path: path in existing, lead=0.0)
        self.now = T

    def open(self, stem: str) -> FakeWriter:
        writer = FakeWriter(stem)
        self.writers.append(writer)
        return writer

    def play(self, seconds: float) -> None:
        """Audio for `seconds`, arriving as it plays."""
        for _ in range(round(seconds / CHUNK)):
            self.now += CHUNK
            self.recorder.audio(bytes(int(PCM_BYTES_PER_SECOND * CHUNK)), self.now)

    def kept(self) -> dict[str, float]:
        return {w.kept.title: round(w.seconds, 1) for w in self.writers if w.kept is not None}


class CuttingTracks(unittest.TestCase):
    def test_tracks_heard_whole_are_kept_and_cut_where_the_next_starts(self) -> None:
        s = Scenario()
        s.recorder.record(track("Intro", T, 30))
        s.play(30)
        s.recorder.record(track("Ting", T + 30, 40, number=2))   # arrives a little after its start, as measured
        s.play(40.5)
        self.assertEqual(s.kept(), {"Intro": 30.0, "Ting": 40.0})
        self.assertEqual(s.writers[1].stem, "/archive/Ezra Collective/Dance, No One's Watching/02 Ting")

    def test_record_arriving_late_loses_no_audio(self) -> None:
        s = Scenario()
        s.recorder.record(track("Intro", T, 10))
        s.play(10.75)                                    # 0.75 s of the next track before its record
        s.recorder.record(track("Ting", T + 10, 10, number=2))
        s.play(9.75)
        self.assertEqual(s.kept(), {"Intro": 10.0, "Ting": 10.0})

    def test_joining_in_the_middle_skips_that_track(self) -> None:
        s = Scenario()
        s.recorder.record(track("Intro", T - 20, 30))    # began 20 s before this speaker's audio
        s.play(10)
        s.recorder.record(track("Ting", T + 10, 10, number=2))
        s.play(10.25)
        self.assertEqual(s.kept(), {"Ting": 10.0})
        self.assertEqual(len(s.writers), 1)

    def test_tracks_in_the_archive_are_skipped(self) -> None:
        s = Scenario(existing=("/archive/Ezra Collective/Dance, No One's Watching/01 Intro.m4a",))
        s.recorder.record(track("Intro", T, 10))
        s.play(10.25)
        self.assertEqual(s.writers, [])

    def test_two_speakers_playing_the_same_track_record_it_once(self) -> None:
        a, b = Scenario(), Scenario()
        b.recorder.busy = a.recorder.busy
        a.recorder.record(track("Intro", T, 10))
        b.recorder.record(track("Intro", T, 10))
        a.play(1)
        b.play(1)
        self.assertEqual((len(a.writers), len(b.writers)), (1, 0))

    def test_skipped_track_is_dropped(self) -> None:
        s = Scenario()
        s.recorder.record(track("Intro", T, 60))
        s.play(20)
        s.recorder.record(track("Ting", T + 20, 10, number=2))   # next pressed after 20 s
        s.play(10.25)
        self.assertEqual(s.kept(), {"Ting": 10.0})
        self.assertTrue(s.writers[0].finished)
        self.assertIsNone(s.writers[0].kept)

    def test_pause_in_a_track_keeps_it(self) -> None:
        s = Scenario()
        s.recorder.record(track("Intro", T, 30))
        s.play(10)
        s.recorder.record(track("Intro", T, 30, elapsed=10, playing=False))
        s.now += 120                                               # paused for two minutes
        s.recorder.record(Track(**{**track("Intro", T, 30).__dict__, "elapsed": 10.0, "stamp": s.now}))
        s.play(20.25)
        self.assertEqual(s.kept(), {"Intro": 30.0})

    def test_pause_with_the_new_record_after_the_audio_keeps_the_track(self) -> None:
        s = Scenario()
        s.recorder.record(track("Intro", T, 30))
        s.play(25)
        s.now += 50                                                # paused past where the track would have ended
        s.play(0.5)
        s.recorder.record(Track(**{**track("Intro", T, 30).__dict__, "elapsed": 25.0, "stamp": s.now - 0.5}))
        s.play(5)
        self.assertEqual(s.kept(), {"Intro": 30.0})

    def test_seek_drops_the_track_and_going_back_to_its_start_tries_again(self) -> None:
        s = Scenario()
        s.recorder.record(track("Intro", T, 30))
        s.play(10)
        s.recorder.record(Track(**{**track("Intro", T, 30).__dict__, "elapsed": 25.0, "stamp": s.now}))   # forward
        self.assertIsNone(s.writers[0].kept)
        self.assertTrue(s.writers[0].finished)
        s.recorder.record(Track(**{**track("Intro", T, 30).__dict__, "elapsed": 0.0, "stamp": s.now}))    # back to 0
        s.play(30.25)
        self.assertEqual(s.kept(), {"Intro": 30.0})

    def test_end_of_track_report_changes_nothing(self) -> None:
        s = Scenario()
        s.recorder.record(track("Intro", T, 10))
        s.play(10)
        s.recorder.record(track("Intro", T, 10, elapsed=10, playing=False))
        s.recorder.record(track("Ting", T + 10, 10, number=2))
        s.play(10.25)
        self.assertEqual(s.kept(), {"Intro": 10.0, "Ting": 10.0})

    def test_last_track_is_finished_once_audio_stops(self) -> None:
        s = Scenario()
        s.recorder.record(track("Outro", T, 10))
        s.play(10)
        s.recorder.idle()
        self.assertEqual(s.kept(), {"Outro": 10.0})

    def test_sender_leaving_mid_track_drops_it(self) -> None:
        s = Scenario()
        s.recorder.record(track("Intro", T, 30))
        s.play(10)
        s.recorder.stop()
        self.assertEqual(s.kept(), {})
        self.assertTrue(s.writers[0].finished)


class AudioAheadOfItsTime(unittest.TestCase):
    def test_audio_arriving_early_is_cut_where_it_plays(self) -> None:
        # shairport-sync's pipe delivers audio before the sender plays it: here 0.9 s, as on the Pi.
        s = Scenario()
        s.recorder.lead = 0.9
        s.now -= 0.9
        s.recorder.record(track("Intro", T, 10))
        s.play(10)                                      # arrives from T - 0.9, plays from T
        s.recorder.record(track("Ting", T + 10, 10, number=2))
        s.play(10.25)
        self.assertEqual(s.kept(), {"Intro": 10.0, "Ting": 10.0})


class Records(unittest.TestCase):
    # As busctl printed it on the Pi on 2026-11-22, shortened.
    RECORD = json.dumps({"type": "a{sv}", "data": {
        "kMRMediaRemoteNowPlayingInfoTitle": {"type": "s", "data": "Activate (Live)"},
        "kMRMediaRemoteNowPlayingInfoArtist": {"type": "s", "data": "Julian Lage"},
        "kMRMediaRemoteNowPlayingInfoAlbum": {"type": "s", "data": "Live in Los Angeles"},
        "kMRMediaRemoteNowPlayingInfoGenre": {"type": "s", "data": "Jazz"},
        "kMRMediaRemoteNowPlayingInfoTrackNumber": {"type": "t", "data": 5},
        "kMRMediaRemoteNowPlayingInfoDuration": {"type": "d", "data": 255.81725},
        "kMRMediaRemoteNowPlayingInfoElapsedTime": {"type": "d", "data": 0.044655977},
        "kMRMediaRemoteNowPlayingInfoTimestamp": {"type": "s", "data": "2026-11-22T08:35:46.132182Z"},
        "kMRMediaRemoteNowPlayingInfoPlaybackRate": {"type": "d", "data": 1.0},
        "kMRMediaRemoteNowPlayingInfoUniqueIdentifier": {"type": "t", "data": 1850038115},
        "kShairportSyncNowPlayingInfoArtworkFilePath": {
            "type": "s", "data": "file:///tmp/shairport-sync/.cache/coverart/cover-5bb8.jpg"}}})

    def test_record_is_read(self) -> None:
        t = archive.parse_now_playing(self.RECORD)
        assert t is not None
        self.assertEqual((t.title, t.artist, t.album, t.number, t.key), ("Activate (Live)", "Julian Lage",
                                                                         "Live in Los Angeles", 5, "1850038115"))
        self.assertAlmostEqual(t.start, 1795336546.087526, places=3)
        self.assertEqual(t.cover, "/tmp/shairport-sync/.cache/coverart/cover-5bb8.jpg")
        self.assertTrue(t.playing)

    def test_record_without_timing_is_useless(self) -> None:
        data = json.loads(self.RECORD)
        del data["data"]["kMRMediaRemoteNowPlayingInfoTimestamp"]
        self.assertIsNone(archive.parse_now_playing(json.dumps(data)))
        self.assertIsNone(archive.parse_now_playing("Failed to get property"))


class Files(unittest.TestCase):
    def test_new_folders_take_the_share_s_group_and_stay_group_writable(self) -> None:
        import os, stat, tempfile
        with tempfile.TemporaryDirectory() as base:
            os.chmod(base, 0o2775)
            archive.make_directories(os.path.join(base, "Artist", "Album"))
            mode = os.stat(os.path.join(base, "Artist", "Album")).st_mode
            self.assertTrue(mode & stat.S_IWGRP)
            if os.uname().sysname == "Linux":   # BSD folders always take their parent's group
                self.assertTrue(mode & stat.S_ISGID)

    def test_a_new_recording_keeps_its_folder_from_a_dropped_one_s_cleanup(self) -> None:
        # A take of the album was dropped just as the next one started: its cleanup removes the
        # album's folder if empty, which must not pull the folder from under the new recording.
        import os, tempfile
        with tempfile.TemporaryDirectory() as base:
            album = os.path.join(base, "Ezra Collective", "Here Because of Hope")
            recording = os.path.join(album, ".01 Part 1.m4a.part")
            archive.claim_file(recording)
            self.assertTrue(os.path.isfile(recording))
            with self.assertRaises(OSError):
                os.rmdir(album)

    def test_cut_off_recordings_are_cleaned_up(self) -> None:
        import os, tempfile
        with tempfile.TemporaryDirectory() as base:
            album = os.path.join(base, "Asaf Avidan", "Unfurl")
            os.makedirs(album)
            for name in (".02 Unfurling Dream.m4a.part", ".03 X.m4a.tagged", ".04 Old.flac.part", "01 Intro.m4a", "cover.jpg"):
                open(os.path.join(album, name), "w").close()
            self.assertEqual(archive.remove_leftovers(base), 3)
            self.assertEqual(sorted(os.listdir(album)), ["01 Intro.m4a", "cover.jpg"])

    def test_names_are_safe_everywhere(self) -> None:
        self.assertEqual(archive.clean("AC/DC", "x"), "AC-DC")
        self.assertEqual(archive.clean('What? "Live": 1/2', "x"), "What_ _Live__ 1-2")
        self.assertEqual(archive.clean("...", "Unknown"), "Unknown")
        self.assertEqual(archive.clean(".hidden. ", "x"), "hidden")
        self.assertLessEqual(len(archive.clean("ü" * 200, "x").encode()), 150)

    def test_path_is_artist_album_number_title(self) -> None:
        t = Track(key="k", title="Sweet Echo", artist="Ezra Collective", album="Here Because of Hope", number=3)
        self.assertEqual(archive.track_stem("/a", t), "/a/Ezra Collective/Here Because of Hope/03 Sweet Echo")
        self.assertEqual(archive.track_stem("/a", Track(key="k", title="Radio")), "/a/Unknown Artist/Unknown Album/Radio")

    def test_tags_and_cover_go_into_the_file(self) -> None:
        t = Track(key="k", title="Sweet Echo", artist="Ezra Collective", album="Here Because of Hope", number=3)
        command = archive.tagging_command("in.part", t, "cover.jpg", "out.m4a")
        self.assertEqual(command[command.index("-c:a") + 1], "alac")
        self.assertEqual(command[-3:], ["-f", "ipod", "out.m4a"])
        self.assertIn("title=Sweet Echo", command)
        self.assertIn("track=3", command)
        self.assertNotIn("genre=", " ".join(command))
        self.assertEqual(command[command.index("-disposition:v") + 1], "attached_pic")
        self.assertNotIn("-disposition:v", archive.tagging_command("in.part", t, None, "out.m4a"))


if __name__ == "__main__":
    unittest.main()
