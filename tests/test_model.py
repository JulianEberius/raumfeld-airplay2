# SPDX-License-Identifier: GPL-3.0-or-later
"""The decision rules, as scenarios. Each one is something that happened on the real speakers."""

import unittest

from raumfeld_airplay.model import Sink, airplay_to_volume, plan_zones, volume_to_airplay

PAUSE_AFTER, IDLE_TIMEOUT, MAX_VOLUME = 0.4, 8.0, 70
T = 100.0   # an arbitrary "now"


def sink(ident: str = "kueche") -> Sink:
    return Sink(ident, f"uuid:{ident}", ident.capitalize())


def wants(s: Sink, now: float) -> bool:
    return s.wants_play(now, PAUSE_AFTER, IDLE_TIMEOUT)


class WhenARoomShouldPlay(unittest.TestCase):
    def test_idle_room_stays_silent(self) -> None:
        self.assertFalse(wants(sink(), T))

    def test_audio_arriving_means_play(self) -> None:
        s = sink()
        s.audio_arrived(T)
        self.assertTrue(wants(s, T + 0.1))

    def test_audio_that_stopped_means_stop(self) -> None:
        s = sink()
        s.audio_arrived(T)
        self.assertFalse(wants(s, T + 0.5))

    def test_speakers_start_before_the_audio_arrives(self) -> None:
        s = sink()
        s.sender_report("pbeg", "", T)
        self.assertTrue(wants(s, T + 0.1))

    def test_session_that_stays_silent_is_given_up(self) -> None:
        s = sink()
        s.session_event("active-begins", T)
        self.assertTrue(wants(s, T + 1))
        self.assertFalse(wants(s, T + 9))

    def test_reported_pause_stops_at_once_despite_trailing_audio(self) -> None:
        s = sink()
        s.sender_report("pbeg", "", T)
        s.audio_arrived(T + 5)
        s.sender_report("paus", "", T + 5.1)
        s.audio_arrived(T + 5.15)
        self.assertFalse(wants(s, T + 5.2))

    def test_reported_resume_starts_before_the_audio(self) -> None:
        s = sink()
        s.sender_report("pbeg", "", T)
        s.audio_arrived(T + 5)
        s.sender_report("paus", "", T + 5.1)
        s.sender_report("pres", "", T + 20)
        self.assertTrue(wants(s, T + 20.05))

    def test_pause_right_after_resume_keeps_the_stream_open(self) -> None:
        # What a skip or seek looks like: resume, pause, resume within a second.
        s = sink()
        s.sender_report("pres", "", T)
        s.audio_arrived(T + 0.3)
        s.sender_report("paus", "", T + 0.5)
        self.assertTrue(wants(s, T + 1.5))
        self.assertFalse(wants(s, T + 9), "but not for ever")

    def test_end_of_play_right_after_resume_still_stops(self) -> None:
        s = sink()
        s.sender_report("pres", "", T)
        s.audio_arrived(T + 1)
        s.sender_report("pend", "", T + 1.2)
        self.assertFalse(wants(s, T + 1.25))

    def test_audio_long_after_a_pause_is_an_unreported_resume(self) -> None:
        s = sink()
        s.sender_report("pres", "", T - 10)
        s.sender_report("paus", "", T)
        s.audio_arrived(T + 2)
        self.assertTrue(wants(s, T + 2.1))

    def test_room_that_is_gone_never_plays(self) -> None:
        s = sink()
        s.audio_arrived(T)
        s.gone = True
        self.assertFalse(wants(s, T + 0.1))


class HowRoomsGroup(unittest.TestCase):
    def setUp(self) -> None:
        self.k, self.w = sink("kueche"), sink("wohnzimmer")
        self.K, self.W = frozenset({"kueche"}), frozenset({"wohnzimmer"})
        self.KW = self.K | self.W

    def plan(self, playing: dict[frozenset[str], str], now: float = T) -> dict[frozenset[str], str]:
        return plan_zones([self.k, self.w], playing, now, PAUSE_AFTER, IDLE_TIMEOUT)

    def test_nothing_active(self) -> None:
        self.assertEqual(self.plan({}), {})

    def test_one_room(self) -> None:
        self.k.audio_arrived(T - 0.1)
        self.assertEqual(self.plan({}), {self.K: "kueche"})

    def test_both_start_at_once(self) -> None:
        self.k.audio_arrived(T - 0.05)
        self.w.audio_arrived(T - 0.05)
        self.assertEqual(self.plan({}), {self.KW: "kueche"})

    def test_second_room_joins_and_the_leader_is_kept(self) -> None:
        self.k.audio_arrived(T - 0.1)
        self.k.client = "192.168.0.50"
        self.w.session_event("active-begins", T - 0.2)   # its sender isn't known yet
        self.assertEqual(self.plan({self.K: "kueche"}), {self.KW: "kueche"})
        self.w.client = "192.168.0.50"
        self.w.audio_arrived(T - 0.05)
        self.assertEqual(self.plan({self.KW: "kueche"}), {self.KW: "kueche"})

    def test_different_senders_get_separate_zones(self) -> None:
        self.k.audio_arrived(T - 0.1)
        self.w.audio_arrived(T - 0.1)
        self.k.client, self.w.client = "192.168.0.50", "192.168.0.77"
        self.assertEqual(self.plan({self.KW: "kueche"}), {self.K: "kueche", self.W: "wohnzimmer"})

    def test_leader_deselected_other_room_carries_on_with_its_own_stream(self) -> None:
        self.k.audio_arrived(T - 1)
        self.w.audio_arrived(T - 0.05)
        self.assertEqual(self.plan({self.KW: "kueche"}), {self.W: "wohnzimmer"})

    def test_other_room_deselected_zone_shrinks_and_keeps_its_stream(self) -> None:
        self.k.audio_arrived(T - 0.1)
        self.w.audio_arrived(T - 1)
        self.assertEqual(self.plan({self.KW: "kueche"}), {self.K: "kueche"})


class Yielding(unittest.TestCase):
    def setUp(self) -> None:
        self.k, self.w = sink("kueche"), sink("wohnzimmer")
        self.k.audio_arrived(T - 0.05)
        self.w.audio_arrived(T - 0.05)
        self.playing = {frozenset({"kueche", "wohnzimmer"}): "kueche"}

    def plan(self, playing: dict[frozenset[str], str], now: float = T) -> dict[frozenset[str], str]:
        return plan_zones([self.k, self.w], playing, now, PAUSE_AFTER, IDLE_TIMEOUT)

    def test_one_room_taken_the_other_keeps_playing(self) -> None:
        self.w.let_go()
        self.assertEqual(self.plan(self.playing), {frozenset({"kueche"}): "kueche"})

    def test_leaders_room_taken_the_other_carries_on(self) -> None:
        self.k.let_go()
        self.assertEqual(self.plan(self.playing), {frozenset({"wohnzimmer"}): "wohnzimmer"})

    def test_all_taken_nothing_is_claimed_although_audio_still_arrives(self) -> None:
        self.k.let_go()
        self.w.let_go()
        self.assertEqual(self.plan(self.playing), {})

    def test_sender_plays_again_and_takes_its_room_back(self) -> None:
        self.k.let_go()
        self.w.let_go()
        self.k.sender_report("pres", "", T + 1)
        self.k.audio_arrived(T + 1)
        self.assertEqual(self.plan({}, T + 1.05), {frozenset({"kueche"}): "kueche"})
        self.assertTrue(self.w.yielded, "only the room that was played to")


class WhoseVolumeCounts(unittest.TestCase):
    def test_scale(self) -> None:
        self.assertEqual(airplay_to_volume(0.0, 70), 70)
        self.assertEqual(airplay_to_volume(-30.0, 70), 0)
        self.assertEqual(airplay_to_volume(-15.0, 70), 35)
        self.assertEqual(airplay_to_volume(-144.0, 70), 0)
        self.assertEqual(volume_to_airplay(35, 70), -15.0)
        self.assertEqual(volume_to_airplay(90, 70), 0.0, "above the ceiling is full")

    def test_volume_before_a_session_is_ignored(self) -> None:
        s = sink()
        s.volume = 40
        self.assertIsNone(s.sender_set_volume(-24.0, T, MAX_VOLUME))
        self.assertEqual(s.volume, 40)

    def test_opening_volume_is_ignored_and_the_sender_is_told_the_speakers_volume(self) -> None:
        s = sink()
        s.sender_report("pbeg", "", T)
        self.assertIsNone(s.sender_set_volume(-24.0, T + 0.1, MAX_VOLUME))
        self.assertEqual(s.speakers_report_volume(47, T + 1, MAX_VOLUME), volume_to_airplay(47, MAX_VOLUME))
        self.assertEqual(s.volume, 47)
        self.assertIsNone(s.speakers_report_volume(47, T + 1.5, MAX_VOLUME), "told once")

    def test_slider_move_during_the_session_is_applied(self) -> None:
        s = sink()
        s.sender_report("pbeg", "", T)
        self.assertEqual(s.sender_set_volume(-15.0, T + 5, MAX_VOLUME), 35)

    def session(self) -> Sink:
        """A session that has opened: the sender was told the speakers' volume, then set its own."""
        s = sink()
        s.sender_report("pbeg", "", T)
        s.speakers_report_volume(30, T + 1, MAX_VOLUME)
        s.sender_set_volume(-15.0, T + 5, MAX_VOLUME)
        return s

    def test_volume_key_on_the_speakers_is_passed_to_the_sender(self) -> None:
        s = self.session()
        self.assertIsNone(s.speakers_report_volume(35, T + 6, MAX_VOLUME), "as the sender set it")
        self.assertEqual(s.speakers_report_volume(38, T + 8, MAX_VOLUME), volume_to_airplay(38, MAX_VOLUME))
        self.assertEqual(s.volume, 38)

    def test_echo_of_a_passed_on_volume_is_not_applied_again(self) -> None:
        s = self.session()
        airplay = s.speakers_report_volume(38, T + 8, MAX_VOLUME)
        assert airplay is not None
        self.assertIsNone(s.sender_set_volume(airplay, T + 8.2, MAX_VOLUME))
        self.assertEqual(s.volume, 38)
        # and repeated presses go through without waiting
        self.assertIsNotNone(s.speakers_report_volume(41, T + 8.5, MAX_VOLUME))

    def test_speaker_readings_are_ignored_while_the_sender_moves_its_slider(self) -> None:
        s = self.session()
        self.assertIsNone(s.speakers_report_volume(12, T + 5.5, MAX_VOLUME), "an in-between reading")
        self.assertEqual(s.volume, 35)

    def test_volume_the_speakers_cannot_report_is_no_volume(self) -> None:
        s = sink()
        self.assertIsNone(s.speakers_report_volume(None, T, MAX_VOLUME))
        self.assertIsNone(s.volume)


class TheSpeakersPlayKey(unittest.TestCase):
    def test_fetching_the_stream_of_a_paused_sink_is_the_play_key(self) -> None:
        s = sink()
        s.sender_report("pres", "", T - 10)
        s.sender_report("paus", "", T)
        self.assertTrue(s.play_key_pressed(T + 5))
        self.assertTrue(wants(s, T + 5.1), "the speakers get a stream at once")
        self.assertFalse(s.play_key_pressed(T + 6), "once")

    def test_fetching_the_stream_of_a_playing_sink_is_not(self) -> None:
        s = sink()
        s.audio_arrived(T)
        self.assertFalse(s.play_key_pressed(T + 0.1))


class TheTrackThatPlays(unittest.TestCase):
    def test_details_arrive_one_by_one_and_count_once_settled(self) -> None:
        s = sink()
        s.sender_report("asal", "Here Because of Hope", T)
        s.sender_report("minm", "Sweet Echo", T)
        s.sender_report("asar", "Ezra Collective", T + 0.01)
        self.assertEqual(s.now_playing.text(T + 0.02), "")
        self.assertEqual(s.now_playing.text(T + 1), "Ezra Collective - Sweet Echo")

    def test_next_track_never_mixes_with_the_previous_one(self) -> None:
        s = sink()
        s.sender_report("minm", "Sweet Echo", T)
        s.sender_report("asar", "Ezra Collective", T)
        s.now_playing.text(T + 1)
        s.sender_report("minm", "So What", T + 60)
        self.assertEqual(s.now_playing.text(T + 60.1), "Ezra Collective - Sweet Echo")
        s.sender_report("asar", "Miles Davis", T + 60.2)
        self.assertEqual(s.now_playing.text(T + 61), "Miles Davis - So What")

    def test_title_alone_is_enough(self) -> None:
        s = sink()
        s.sender_report("minm", "Tagesschau", T)
        self.assertEqual(s.now_playing.text(T + 1), "Tagesschau")

    def test_end_of_play_clears_it(self) -> None:
        s = sink()
        s.sender_report("minm", "Sweet Echo", T)
        s.sender_report("pend", "", T + 5)
        self.assertEqual(s.now_playing.text(T + 6), "")


class RoomsComeAndGo(unittest.TestCase):
    def test_room_is_given_a_grace_period(self) -> None:
        s = sink()
        self.assertFalse(s.room_missing(T, 60))
        self.assertFalse(s.room_missing(T + 30, 60))
        self.assertTrue(s.room_missing(T + 61, 60))
        self.assertTrue(s.gone)
        self.assertFalse(s.room_missing(T + 70, 60), "reported once")

    def test_room_that_returns_in_time_stays(self) -> None:
        s = sink()
        s.room_missing(T, 60)
        s.room_listed()
        self.assertFalse(s.room_missing(T + 61, 60))
        self.assertFalse(s.gone)


if __name__ == "__main__":
    unittest.main()
