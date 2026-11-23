# SPDX-License-Identifier: GPL-3.0-or-later
"""A private archive of what plays: every track heard from start to end is written to its own
ALAC file, tagged and with its cover, as BASE/artist/album/NN title.m4a. Tracks that are in
the archive already, in any common format, are skipped.

AirPlay doesn't mark in the audio where a track begins. The sender's now-playing record says
when the current track started on its clock (the record's timestamp minus the elapsed time)
and how long it is. Measured on the real speakers, that start always fell into the silence
between two tracks, within half a second. So the audio is cut by time: each piece of audio is
stamped with when it played, and a track gets the audio between its start and its end, or the
next track's start.

A track is kept only if it was heard whole: from its first two seconds to its last two, with
no seek in between. Pausing is fine. Skipped and seeked tracks are thrown away.

ALAC (Apple Lossless) because it adds no loss of its own, so the file holds exactly the samples
that arrived, and because the Music app on a Mac or an iPhone takes it, which it doesn't FLAC.
The samples are the sender's stream decoded, which for Apple Music is AAC, so the file is not
lossless relative to the original recording. While a track plays it is recorded as FLAC, which
stays readable when cut off; it becomes ALAC when it is kept.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Protocol

from .shairport import PIPE_AHEAD
from .streaming import CHANNELS, PCM_BYTES_PER_SECOND, RATE

log = logging.getLogger("raumfeld-airplay")

EXTENSION = ".m4a"
RECORDING_EXTENSIONS = (".m4a", ".flac")   # of hidden recordings; .flac: from before ALAC
# A track in any of these formats counts as archived already.
ARCHIVED_EXTENSIONS = (".flac", ".m4a", ".m4p", ".alac", ".mp3", ".opus", ".ogg", ".aiff", ".aif", ".wav")
START_TOLERANCE = 2.0    # seconds of a track's beginning that may be missing from a recording
END_TOLERANCE = 2.0      # seconds of its end
SEEK_TOLERANCE = 2.0     # a record whose elapsed time is this far off from what was heard means a seek
WAIT_FOR_RECORD = 10.0   # seconds of audio past a track's end kept while the next track's record is awaited
CLOCK_SLACK = 0.3        # how far the audio's clock may lag behind when audio arrived
# How long before the sender plays it audio arrives from shairport-sync's pipe. shairport-sync
# keeps the pipe PIPE_AHEAD (1 s) ahead; measured on the Pi from an archived album, each file
# ended with 0.85-0.9 s of the next track, which began right after the previous one's silence.
AUDIO_LEAD = PIPE_AHEAD - 0.1
FRAME = 4                # bytes per stereo sample
NOW_PLAYING = "kMRMediaRemoteNowPlayingInfo"


@dataclass(frozen=True)
class Track:
    """A sender's now-playing record."""

    key: str                  # tells tracks apart
    title: str
    artist: str = ""
    album: str = ""
    genre: str = ""
    composer: str = ""
    number: int | None = None
    duration: float = 0.0
    elapsed: float = 0.0      # how far into the track the sender was ...
    stamp: float = 0.0        # ... at this time (Unix time, the sender's clock)
    playing: bool = True
    cover: str = ""           # the cover image shairport-sync saved, if any

    @property
    def start(self) -> float:
        """When the track started, or would have, had it played without pause."""
        return self.stamp - self.elapsed

    @property
    def end(self) -> float:
        return self.start + self.duration


def parse_now_playing(data: bytes | str) -> Track | None:
    """The record in busctl's JSON form of shairport-sync's NowPlayingInformation property.
    None if it lacks what cutting needs: a title, a duration and the timing."""
    try:
        fields = json.loads(data)["data"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(fields, dict):
        return None

    def get(name: str, full: bool = False) -> object:
        item = fields.get(name if full else NOW_PLAYING + name)
        return item.get("data") if isinstance(item, dict) else None

    def text(name: str) -> str:
        value = get(name)
        return value.strip() if isinstance(value, str) else ""

    title, stamp_text = text("Title"), get("Timestamp")
    duration, elapsed, rate = get("Duration"), get("ElapsedTime"), get("PlaybackRate")
    if not title or not isinstance(stamp_text, str) or not isinstance(duration, (int, float)) \
            or not isinstance(elapsed, (int, float)) or duration <= 0:
        return None
    try:
        stamp = datetime.fromisoformat(stamp_text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    artist, album = text("Artist"), text("Album")
    identifier = get("UniqueIdentifier") or get("ContentItemIdentifier")
    number = get("TrackNumber")
    cover = get("kShairportSyncNowPlayingInfoArtworkFilePath", full=True)
    cover_path = urllib.parse.unquote(cover[len("file://"):]) if isinstance(cover, str) and cover.startswith("file://") else ""
    return Track(key=str(identifier) if identifier else f"{artist}\n{album}\n{title}",
                 title=title, artist=artist, album=album, genre=text("Genre"), composer=text("Composer"),
                 number=number if isinstance(number, int) and not isinstance(number, bool) and number > 0 else None,
                 duration=float(duration), elapsed=float(elapsed), stamp=stamp,
                 playing=not isinstance(rate, (int, float)) or rate > 0, cover=cover_path)


# ---------------------------------------------------------------- where tracks go

_UNSAFE = re.compile(r'[\x00-\x1f:*?"<>|]')


def clean(name: str, fallback: str) -> str:
    """A name made safe as a file or folder name, here and on the Mac or Windows reading the share."""
    name = _UNSAFE.sub("_", name.replace("/", "-").replace("\\", "-")).strip().lstrip(".").rstrip(". ")
    while len(name.encode()) > 150:
        name = name[:-1].rstrip(". ")
    return name or fallback


def track_stem(base: str, track: Track) -> str:
    """BASE/artist/album/NN title, without the extension."""
    title = clean(track.title, "Unknown Track")
    name = f"{track.number:02d} {title}" if track.number else title
    return os.path.join(base, clean(track.artist, "Unknown Artist"), clean(track.album, "Unknown Album"), name)


def archived(stem: str, exists: Callable[[str], bool] = os.path.exists) -> bool:
    return any(exists(stem + extension) for extension in ARCHIVED_EXTENSIONS)


def encoder_command(output: str) -> list[str]:
    """ffmpeg turning the PCM on its standard input into a FLAC file."""
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "s16le", "-ar", str(RATE), "-ac", str(CHANNELS), "-i", "pipe:0",
            "-c:a", "flac", "-compression_level", "8", "-f", "flac", output]


def tagging_command(source: str, track: Track, cover: str | None, output: str) -> list[str]:
    """ffmpeg turning the recorded audio into the track's ALAC file, with its tags and cover."""
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", source]
    if cover:
        command += ["-i", cover]
    command += ["-map", "0:a", "-c:a", "alac"]
    if cover:
        command += ["-map", "1:v", "-c:v", "copy", "-disposition:v", "attached_pic"]
    tags = {"title": track.title, "artist": track.artist, "album": track.album, "genre": track.genre,
            "composer": track.composer, "track": str(track.number) if track.number else ""}
    for name, value in tags.items():
        if value:
            command += ["-metadata", f"{name}={value}"]
    return command + ["-f", "ipod", output]


# ---------------------------------------------------------------- cutting the audio into tracks

class Writer(Protocol):
    """Where one track's audio goes."""

    def write(self, pcm: bytes) -> None: ...

    def finish(self, track: Track | None) -> None:
        """Ends the recording: keeps it as `track`, as the latest record describes it, or drops it if None."""


OpenWriter = Callable[[str], Writer]   # given the track's stem


@dataclass
class Take:
    """One track as it plays, and its recording."""

    track: Track
    state: str = "waiting"   # waiting for its audio, recording, skipped (archived already), missed, done
    writer: Writer | None = None
    stem: str = ""
    first: float = 0.0       # how far into the track the recording begins, seconds
    recorded: int = 0        # bytes

    @property
    def position(self) -> float:
        """How far into the track the recording has got."""
        return self.first + self.recorded / PCM_BYTES_PER_SECOND


@dataclass
class Recorder:
    """Cuts one AirPlay speaker's audio into tracks and has the complete ones written."""

    base: str
    open_writer: OpenWriter
    busy: set[str] = field(default_factory=set)   # stems being recorded, shared by all speakers' recorders
    exists: Callable[[str], bool] = os.path.exists
    takes: list[Take] = field(default_factory=list)     # by start: the track playing, and any that follow
    waiting: list[tuple[float, bytes]] = field(default_factory=list)   # audio past the last track's end
    clock: float | None = None          # when the next audio plays, as far as is known
    last_record: tuple[str, float, float] | None = None
    last_owner: tuple[str, float] | None = None   # the track the latest audio was for, and its start then
    lead: float = AUDIO_LEAD            # how much earlier than it plays audio arrives

    # --- what the sender says

    def record(self, track: Track) -> None:
        """A now-playing record from the sender."""
        if not track.playing:
            return   # paused, or a track's end reported: the audio says the same
        signature = (track.key, track.stamp, track.elapsed)
        if signature == self.last_record:
            return
        self.last_record = signature
        take = next((take for take in self.takes if take.track.key == track.key), None)
        if take is None:
            self._add(Take(track))
        elif take.state == "recording" and abs(track.elapsed - take.position) <= SEEK_TOLERANCE:
            take.track = track   # the same track, its timing refreshed (as after a pause)
        elif track.elapsed <= START_TOLERANCE and abs(track.start - take.track.start) > SEEK_TOLERANCE:
            self._drop(take)     # started over: try anew
            self.takes.remove(take)
            self._add(Take(track))
        elif take.state == "recording":
            log.info("not archiving %s: it was seeked in", describe(track))
            self._drop(take)
            take.state = "missed"
        else:
            take.track = track
        self.takes.sort(key=lambda take: take.track.start)
        self._place_waiting()

    def _add(self, take: Take) -> None:
        self.takes.append(take)

    def stop(self) -> None:
        """The sender is gone: keeps what is complete, drops the rest."""
        for take in self.takes:
            self._finish(take)
        self.takes, self.waiting, self.clock, self.last_record, self.last_owner = [], [], None, None, None

    def idle(self) -> None:
        """No audio for a while: a track that has been heard to its end is finished now, rather
        than when the next audio arrives."""
        for take in [take for take in self.takes if take.state == "recording"
                     and take.position >= take.track.duration - END_TOLERANCE]:
            self._finish(take)
            self.takes.remove(take)

    # --- the audio

    def audio(self, pcm: bytes, arrived: float) -> None:
        """Audio that just arrived; `arrived` is the time it did (Unix time)."""
        arrived += self.lead   # when it plays, which is what the sender's timing refers to
        seconds = len(pcm) / PCM_BYTES_PER_SECOND
        earliest = arrived - seconds
        if self.clock is None:
            begins = earliest
        elif earliest - self.clock > 1.0:
            # After a pause the tracks' timeline has moved on by its length. The sender's new
            # record says so, but it may come after the audio, as it did on the Pi.
            self._shift_for_pause(earliest - self.clock)
            begins = earliest
        else:
            begins = min(max(self.clock, earliest - CLOCK_SLACK), earliest)
        self.clock = begins + seconds
        self._place(begins, pcm)

    def _shift_for_pause(self, pause: float) -> None:
        playing = next((take for take in self.takes if self.last_owner and take.track.key == self.last_owner[0]), None)
        if not playing or not self.last_owner:
            return
        shift = pause - (playing.track.start - self.last_owner[1])   # less what a new record moved it already
        if shift > SEEK_TOLERANCE / 2:
            for take in self.takes:
                take.track = replace(take.track, stamp=take.track.stamp + shift)

    def _place(self, at: float, pcm: bytes) -> None:
        while pcm:
            self._finish_before(at)
            index = self._owner(at)
            if index is None:
                later = [take.track.start for take in self.takes if take.track.start > at]
                if not later:
                    self.waiting.append((at, pcm))   # the next track's record is on its way
                    while self.waiting and at - self.waiting[0][0] > WAIT_FOR_RECORD:
                        self.waiting.pop(0)
                    return
                # Before the next known track: the rest of one that isn't known, or isn't recorded.
                skipped = pcm[:max(int((later[0] - at) * RATE), 1) * FRAME]
                pcm, at = pcm[len(skipped):], at + len(skipped) / PCM_BYTES_PER_SECOND
                continue
            take = self.takes[index]
            self.last_owner = (take.track.key, take.track.start)
            boundary = self._boundary(index)
            frames = int((boundary - at) * RATE) if boundary < float("inf") else len(pcm) // FRAME
            piece = pcm[:max(frames, 1) * FRAME]
            self._feed(take, at, piece)
            pcm, at = pcm[len(piece):], at + len(piece) / PCM_BYTES_PER_SECOND
        self._finish_before(at)

    def _place_waiting(self) -> None:
        waiting, self.waiting = self.waiting, []
        for at, pcm in waiting:
            self._place(at, pcm)

    def _owner(self, at: float) -> int | None:
        """The take the audio at `at` belongs to."""
        for index in range(len(self.takes) - 1, -1, -1):
            if self.takes[index].track.start <= at:
                return index if at < self._boundary(index) else None
        return None

    def _boundary(self, index: int) -> float:
        """Where the take's audio ends: at its end, or where the next track starts."""
        end = self.takes[index].track.end
        if index + 1 < len(self.takes):
            end = min(end, self.takes[index + 1].track.start)
        return end

    def _finish_before(self, at: float) -> None:
        """Finishes the takes whose audio all lies before `at`."""
        for index in range(len(self.takes) - 1, -1, -1):
            if self._boundary(index) <= at:
                self._finish(self.takes.pop(index))

    def _feed(self, take: Take, at: float, pcm: bytes) -> None:
        if take.state == "waiting":
            into = at - take.track.start
            if into > START_TOLERANCE:
                take.state = "missed"   # its beginning was before this speaker's audio began
                return
            take.stem = track_stem(self.base, take.track)
            if take.stem in self.busy or archived(take.stem, self.exists):
                take.state = "skipped"
                log.debug("%s is archived already", describe(take.track))
                return
            take.writer, take.first, take.state = self.open_writer(take.stem), max(into, 0.0), "recording"
            self.busy.add(take.stem)
        if take.state == "recording" and take.writer:
            take.writer.write(pcm)
            take.recorded += len(pcm)

    def _finish(self, take: Take) -> None:
        if take.state == "recording":
            if take.position >= take.track.duration - END_TOLERANCE:
                assert take.writer
                take.writer.finish(take.track)
                self.busy.discard(take.stem)
                log.info("archiving %s", describe(take.track))
            else:
                log.info("not archiving %s: heard %.0f of %.0f seconds", describe(take.track),
                         take.position - take.first, take.track.duration)
                self._drop(take)
        take.state = "done"

    def _drop(self, take: Take) -> None:
        if take.writer:
            take.writer.finish(None)
            take.writer = None
        self.busy.discard(take.stem)


def describe(track: Track) -> str:
    return " - ".join(part for part in (track.artist, track.album, track.title) if part)


# ---------------------------------------------------------------- the files

def make_directories(path: str) -> None:
    """Creates the folders writable by their group. They take the group of the folder they are
    in, if that is set to pass it on, as on a share; changing their mode afterwards would undo
    that for a user outside the group, so it is set only by the umask."""
    previous = os.umask(0o002)
    try:
        os.makedirs(path, exist_ok=True)
    finally:
        os.umask(previous)


def claim_file(path: str) -> None:
    """Creates the folders for `path` and an empty file there. The file keeps the folders from
    being removed as empty by a recording of the same album that was dropped meanwhile."""
    for attempt in range(3):
        make_directories(os.path.dirname(path))
        try:
            with open(path, "wb"):
                return
        except FileNotFoundError:
            if attempt == 2:
                raise


def save_cover(track: Track, directory: str) -> str | None:
    """The album's cover file, saved next to its tracks from what shairport-sync received."""
    for name in ("cover.jpg", "cover.png"):
        if os.path.exists(os.path.join(directory, name)):
            return os.path.join(directory, name)
    if not track.cover:
        return None
    try:
        with open(track.cover, "rb") as file:
            data = file.read()
    except OSError:
        return None
    if not data:
        return None
    path = os.path.join(directory, "cover.png" if data.startswith(b"\x89PNG") else "cover.jpg")
    with open(path, "wb") as file:
        file.write(data)
    os.chmod(path, 0o664)
    return path


class FileWriter:
    """Records one track with ffmpeg into a hidden file next to where it is to go. A kept
    recording is then copied into the track's file with its tags and cover, which are known
    for certain only by then."""

    def __init__(self, stem: str) -> None:
        self.directory, name = os.path.split(stem)
        self.final = stem + EXTENSION
        self.recording = os.path.join(self.directory, f".{name}{EXTENSION}.part")
        self.tagged = os.path.join(self.directory, f".{name}{EXTENSION}.tagged")
        self.process: asyncio.subprocess.Process | None = None
        self.pending: list[bytes] = []
        self.started = asyncio.ensure_future(self._start())

    async def _start(self) -> None:
        claim_file(self.recording)
        self.process = await asyncio.create_subprocess_exec(
            *encoder_command(self.recording), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        assert self.process.stdin
        for pcm in self.pending:
            self.process.stdin.write(pcm)
        self.pending = []

    def write(self, pcm: bytes) -> None:
        if self.process and self.process.stdin:
            if not self.process.stdin.is_closing():
                self.process.stdin.write(pcm)
        elif not self.started.done():
            self.pending.append(pcm)

    def finish(self, track: Track | None) -> None:
        asyncio.ensure_future(self._finish(track))

    def _remove_empty_folders(self) -> None:
        """Removes the album's and the artist's folder again if nothing is in them."""
        directory = self.directory
        for _ in range(2):
            try:
                os.rmdir(directory)
            except OSError:
                return
            directory = os.path.dirname(directory)

    async def _finish(self, track: Track | None) -> None:
        try:
            await self.started
            assert self.process and self.process.stdin
            self.process.stdin.close()
            _, errors = await self.process.communicate()
            if track is None:
                os.remove(self.recording)
                self._remove_empty_folders()
                return
            if self.process.returncode:
                log.error("archive: recording %s failed: %s", self.final, errors.decode(errors="replace").strip()[-300:])
                return
            cover = save_cover(track, self.directory)
            tagging = await asyncio.create_subprocess_exec(
                *tagging_command(self.recording, track, cover, self.tagged),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, errors = await tagging.communicate()
            if tagging.returncode:
                log.error("archive: tagging %s failed: %s", self.final, errors.decode(errors="replace").strip()[-300:])
                return
            os.chmod(self.tagged, 0o664)
            os.replace(self.tagged, self.final)
        except OSError as error:
            log.error("archive: can't write %s: %s", self.final, error)
        finally:
            for leftover in (self.recording, self.tagged):
                try:
                    os.remove(leftover)
                except OSError:
                    pass


def remove_leftovers(base: str) -> int:
    """Removes the hidden files of recordings that were cut off, as by a restart. Returns how many."""
    removed = 0
    for directory, _, names in os.walk(base):
        for name in names:
            if name.startswith(".") and name.endswith(tuple(extension + leftover for extension in RECORDING_EXTENSIONS
                                                            for leftover in (".part", ".tagged"))):
                try:
                    os.remove(os.path.join(directory, name))
                    removed += 1
                except OSError:
                    pass
    return removed


def now_playing_command(tag: str) -> list[str]:
    """busctl reading the now-playing record of the shairport-sync instance `tag`."""
    return ["busctl", "--system", "--json=short", "get-property", f"org.gnome.ShairportSync.{tag}",
            "/org/gnome/ShairportSync", "org.gnome.ShairportSync.Client", "NowPlayingInformation"]
