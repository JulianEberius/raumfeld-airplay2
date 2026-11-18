# SPDX-License-Identifier: GPL-3.0-or-later
"""The bridge's decision rules, free of any I/O.

When a room should play, how rooms group into zones, and whose volume counts. Everything here
works on plain state; times are monotonic seconds handed in by the caller, so the rules can be
tested without a network, a speaker or a clock.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

# The volume a sender announces within this time of starting a session is its opening volume:
# whatever its slider happened to show. The speakers keep theirs instead.
OPENING_SECONDS = 3.0
# A pause this soon after a resume is the sender toggling, as on a skip or seek.
TOGGLE_SECONDS = 1.5
# After the sender moved its slider, the speakers' own volume readings are ignored this long.
SLIDER_SECONDS = 2.0
# A volume the bridge passed to the sender comes back from it within this time, this exactly.
ECHO_SECONDS = 3.0
ECHO_TOLERANCE = 0.06
# Audio this long after a reported pause means a resume that wasn't reported.
UNREPORTED_RESUME_SECONDS = 1.0
# The speakers' play key is acted on at most this often.
PLAY_KEY_SECONDS = 3.0
# A track's title, artist and album arrive one by one; they count once nothing changed this long.
TRACK_SETTLE_SECONDS = 0.5

AIRPLAY_QUIET = -30.0   # AirPlay volume runs from -30 dB (quiet) to 0 dB (full); -144 dB is mute
AIRPLAY_MUTE = -100.0


def airplay_to_volume(airplay: float, max_volume: int) -> int:
    """An AirPlay volume in dB as a Raumfeld volume, where 0 dB is `max_volume`."""
    if airplay <= AIRPLAY_MUTE:
        return 0
    fraction = min(max((airplay - AIRPLAY_QUIET) / -AIRPLAY_QUIET, 0.0), 1.0)
    return round(fraction * max_volume)


def volume_to_airplay(volume: int, max_volume: int) -> float:
    """A Raumfeld volume as an AirPlay volume in dB; anything above `max_volume` is full."""
    return round(AIRPLAY_QUIET - AIRPLAY_QUIET * min(volume, max_volume) / max_volume, 2)


class NowPlaying:
    """The track the sender says it is playing."""

    FIELDS = {"minm": "title", "asar": "artist", "asal": "album"}   # the metadata feed's codes

    def __init__(self) -> None:
        self.title = self.artist = self.album = ""
        self._changed_at = 0.0
        self._settled = ""

    def report(self, code: str, value: str, now: float) -> None:
        """One of the track's details, as the sender reports it."""
        field = self.FIELDS[code]
        if getattr(self, field) != value:
            setattr(self, field, value)
            self._changed_at = now

    def clear(self, now: float) -> None:
        for code in self.FIELDS:
            self.report(code, "", now)

    def text(self, now: float) -> str:
        """The track in one line, "artist - title", as internet radio announces it. While details
        are still arriving this is the previous track, so that the two never get mixed."""
        if now - self._changed_at >= TRACK_SETTLE_SECONDS:
            self._settled = " - ".join(part for part in (self.artist, self.title) if part)
        return self._settled


class Sink:
    """What the bridge knows about one AirPlay speaker and its Raumfeld room.

    The methods are the events that reach a sink: reports from the sender, readings from the
    speakers, the room coming and going. They update the state and, where the bridge has to
    act, return what to do.
    """

    def __init__(self, ident: str, room_udn: str, room_name: str) -> None:
        self.id = ident
        self.room_udn = room_udn
        self.room_name = room_name
        # play
        self.last_audio = 0.0            # when PCM last arrived
        self.session_open = False        # between shairport-sync's "active-begins" and "active-ends"
        self.session_began = 0.0
        self.paused_at = 0.0             # when the sender reported a pause or the end of play; 0 if it hasn't
        self.resumed_at = 0.0            # when the sender last reported that play (re)started
        self.connected_at: float | None = None   # when the sender began its play session; None between sessions
        self.client: str | None = None   # the sender's address, once its metadata feed has told it
        self.yielded = False             # somebody gave the room another source: hands off until the sender plays anew
        self.play_asked_at = 0.0         # when the sender was last asked to play, after the speakers' play key
        self.now_playing = NowPlaying()
        # volume, on Raumfeld's 0-100 scale
        self.volume: int | None = None   # what the sender last asked for, or what the speakers had when it connected
        self.volume_set_at = 0.0         # when the sender last set it
        self.tell_volume = False         # the sender is still to be told the speakers' volume, as its session opens
        self.told_sender: tuple[float, float] | None = None   # (AirPlay volume, when) last passed on to the sender
        self.speaker_volume: int | None = None   # the room's volume as the speakers last reported it
        # the room
        self.missing_since: float | None = None   # since when the host hasn't listed the room
        self.gone = False                # the room left: stop playing, then remove the sink

    # --- what the sender reports

    def play_reported(self, now: float) -> None:
        """Play is starting or resuming: expect audio shortly. A deliberate play also ends a yield."""
        self.paused_at, self.resumed_at = 0.0, now
        self.session_open, self.session_began = True, now
        self.yielded = False

    def pause_reported(self, now: float, final: bool) -> None:
        """Play paused, or (final) ended."""
        if not final and now - self.resumed_at < TOGGLE_SECONDS:
            # The sender is toggling. Keep the speakers' stream open for the audio that is about
            # to come instead of restarting it twice.
            self.session_open, self.session_began = True, now
        else:
            self.paused_at = now

    def sender_report(self, code: str, value: str, now: float) -> None:
        """An item from the metadata feed: clip (sender's address), pbeg, pres, paus, pend, and the
        track's title, artist and album."""
        if code in NowPlaying.FIELDS:
            self.now_playing.report(code, value, now)
        elif code == "clip":
            self.client = value or None
        elif code == "pbeg":
            # A new session: the speakers keep the volume they have, and the sender is told it.
            self.connected_at, self.tell_volume, self.volume = now, True, None
            self.play_reported(now)
        elif code == "pres":
            self.play_reported(now)
        elif code == "paus":
            self.pause_reported(now, final=False)
        elif code == "pend":
            self.connected_at = None
            self.now_playing.clear(now)
            self.pause_reported(now, final=True)

    def session_event(self, name: str, now: float) -> None:
        """A session event from shairport-sync's hooks. Only the "active" pair is acted on: it is
        debounced and fires on pause and resume, unlike play-begins and play-ends."""
        if name == "active-begins":
            self.session_open, self.session_began, self.paused_at = True, now, 0.0
            self.yielded = False
        elif name == "active-ends":
            self.session_open, self.client = False, None

    def audio_arrived(self, now: float) -> None:
        self.last_audio = now
        if self.paused_at and now - self.paused_at > UNREPORTED_RESUME_SECONDS:
            self.paused_at = 0.0

    def sender_set_volume(self, airplay: float, now: float, max_volume: int) -> int | None:
        """The sender set its AirPlay volume. Returns the volume for the room, or None to ignore it."""
        if (self.told_sender and abs(airplay - self.told_sender[0]) < ECHO_TOLERANCE
                and now - self.told_sender[1] < ECHO_SECONDS):
            self.told_sender = None   # the echo of a volume that came from the speakers: they have it already
            return None
        if self.connected_at is None or now - self.connected_at < OPENING_SECONDS:
            return None               # the opening volume: the speakers keep theirs
        self.volume, self.volume_set_at = airplay_to_volume(airplay, max_volume), now
        return self.volume

    # --- what the speakers report

    def speakers_report_volume(self, volume: int | None, now: float, max_volume: int) -> float | None:
        """The room's volume as read from the speakers. Returns the AirPlay volume to pass on to the
        sender, if it is the sender's turn to follow: as its session opens, or after a press of the
        speakers' volume keys."""
        self.speaker_volume = volume
        if volume is None:
            return None
        if self.volume is None:
            self.volume = volume      # not set by a sender yet: what the speakers have counts
        if self.tell_volume:
            self.tell_volume = False
        elif volume == self.volume or now - self.volume_set_at <= SLIDER_SECONDS:
            return None               # as the sender set it, or the sender is still moving its slider
        self.volume = volume
        airplay = volume_to_airplay(volume, max_volume)
        self.told_sender = (airplay, now)
        return airplay

    def play_key_pressed(self, now: float) -> bool:
        """The speakers fetch this sink's stream although the bridge isn't playing it. If the sink is
        paused, that is the speakers' play key: act as if the sender had resumed already, so that
        the speakers get a steady stream, and report True for the sender to be asked to play."""
        if not self.paused_at or now - self.play_asked_at <= PLAY_KEY_SECONDS:
            return False
        self.play_asked_at = now
        self.play_reported(now)
        return True

    def let_go(self) -> None:
        """Somebody chose another source for the room: don't claim it until the sender plays again."""
        self.yielded = True

    # --- the room

    def room_listed(self) -> None:
        self.missing_since = None

    def room_missing(self, now: float, grace: float) -> bool:
        """The host doesn't list the room (or doesn't answer). True once, when the grace period is over."""
        if self.missing_since is None:
            self.missing_since = now
        if self.gone or now - self.missing_since <= grace:
            return False
        self.gone = True
        return True

    # --- the outcome

    def wants_play(self, now: float, pause_after: float, idle_timeout: float) -> bool:
        """Whether the room should be playing: audio is arriving, or play has just (re)started and
        its audio is still to come. Never while the sender says it is paused, the room was given
        another source, or the room is gone."""
        if self.paused_at or self.gone or self.yielded:
            return False
        if now - self.last_audio < pause_after:
            return True
        return self.session_open and self.last_audio < self.session_began and now - self.session_began < idle_timeout


def same_sender(a: Sink, b: Sink) -> bool:
    """Whether two sinks are played to by the same device; unknown counts as the same."""
    return a.client is None or b.client is None or a.client == b.client


def plan_zones(sinks: Iterable[Sink], playing: Mapping[frozenset[str], str], now: float,
               pause_after: float, idle_timeout: float) -> dict[frozenset[str], str]:
    """Which zones should exist: {sink ids: id of the sink whose stream the zone plays}.

    Sinks that should play and share a sender form one zone. `playing` is the same mapping for
    the zones that exist; a zone's leader is kept while it still plays, so that adding or
    removing another room doesn't change the stream the zone is fed from.
    """
    groups: list[list[Sink]] = []
    for sink in sinks:
        if not sink.wants_play(now, pause_after, idle_timeout):
            continue
        for group in groups:
            if all(same_sender(sink, other) for other in group):
                group.append(sink)
                break
        else:
            groups.append([sink])
    plan: dict[frozenset[str], str] = {}
    for group in groups:
        ids = [sink.id for sink in group]
        kept = [leader for leader in playing.values() if leader in ids]
        with_audio = [sink.id for sink in group if now - sink.last_audio < pause_after]
        plan[frozenset(ids)] = (kept or with_audio or ids)[0]
    return plan
