"""The audio on its way to the speakers: raw PCM from shairport-sync, encoded to an endless
stream that the Raumfeld host fetches over HTTP like an internet radio station."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

RATE, CHANNELS, BITS = 44100, 2, 16
PCM_BYTES_PER_SECOND = RATE * CHANNELS * BITS // 8
BACKLOG_SECONDS = 6   # how far a listener may fall behind before it counts as stalled


@dataclass(frozen=True)
class Format:
    """An encoding the speakers can be sent."""

    name: str                 # also the stream's file extension
    content_type: str
    protocol_info: str        # how UPnP describes it
    encoder: tuple[str, ...]  # ffmpeg's output options
    bytes_per_second: int     # roughly
    has_header: bool          # the stream starts with a header that every listener needs first


MP3 = Format("mp3", "audio/mpeg",
             "http-get:*:audio/mpeg:DLNA.ORG_PN=MP3;DLNA.ORG_OP=00;DLNA.ORG_FLAGS=01700000000000000000000000000000",
             ("-c:a", "libmp3lame", "-b:a", "320k", "-f", "mp3"), 40_000, False)
FLAC = Format("flac", "audio/flac", "http-get:*:audio/flac:*",
              ("-c:a", "flac", "-compression_level", "5", "-f", "flac"), 120_000, True)
FORMATS = {fmt.name: fmt for fmt in (MP3, FLAC)}

# Internet radio's way of naming the track inside the stream ("ICY"), for listeners that ask for it:
# after every ICY_INTERVAL bytes of audio comes a block that is empty or holds the new title.
ICY_REQUEST = "icy-metadata"
ICY_INTERVAL = 16_000
ICY_TITLE_BYTES = 480

FLAC_MARKER = b"fLaC"
FLAC_FRAME_SYNC = b"\xff\xf8"   # how a frame of a fixed-blocksize FLAC stream starts


def stream_path(ident: str, fmt: Format) -> str:
    return f"/stream/{ident}.{fmt.name}"


def stream_response(fmt: Format, titles: bool = False, name: str = "") -> bytes:
    """The start of the answer to a request for a stream; `titles` if track titles will be woven in."""
    icy = f"icy-metaint: {ICY_INTERVAL}\r\nicy-name: {name}\r\n" if titles else ""
    return (f"HTTP/1.1 200 OK\r\nContent-Type: {fmt.content_type}\r\nCache-Control: no-cache\r\n"
            f"{icy}Connection: close\r\nAccept-Ranges: none\r\n\r\n").encode()


def wants_titles(request: bytes) -> bool:
    """Whether an HTTP request asks for track titles inside the stream."""
    for line in request.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == ICY_REQUEST.encode():
            return value.strip() == b"1"
    return False


def icy_block(title: str | None) -> bytes:
    """A title block: a length byte counting units of 16 bytes, then the padded text. None for no news."""
    if title is None:
        return b"\0"
    text = title.replace("';", "'\u200a;").encode()[:ICY_TITLE_BYTES].decode(errors="ignore")   # whole characters only
    body = f"StreamTitle='{text}';".encode()
    units = -(-len(body) // 16)
    return bytes([units]) + body.ljust(units * 16, b"\0")


class TitleWeaver:
    """Weaves title blocks into one listener's stream."""

    def __init__(self) -> None:
        self._until_block = ICY_INTERVAL
        self._told = ""

    def weave(self, data: bytes, title: str) -> bytes:
        """`data` with the blocks that fall into it; `title` goes into the first one after it changed."""
        pieces: list[bytes] = []
        while len(data) >= self._until_block:
            pieces.append(data[:self._until_block])
            data = data[self._until_block:]
            pieces.append(icy_block(title if title != self._told else None))
            self._told, self._until_block = title, ICY_INTERVAL
        self._until_block -= len(data)
        pieces.append(data)
        return b"".join(pieces)


def silence(seconds: float) -> bytes:
    return bytes(int(PCM_BYTES_PER_SECOND * seconds) & ~3)


def encoder_command(fmt: Format) -> list[str]:
    """ffmpeg turning PCM on its standard input into `fmt` on its standard output, without delay."""
    return ["ffmpeg", "-hide_banner", "-loglevel", "error",
            # Without these ffmpeg collects seconds of input before it produces anything.
            "-probesize", "32", "-analyzeduration", "0", "-fflags", "nobuffer",
            "-f", "s16le", "-ar", str(RATE), "-ac", str(CHANNELS), "-i", "pipe:0",
            *fmt.encoder, "-flush_packets", "1", "pipe:1"]


def flac_header_length(data: bytes) -> int | None:
    """The length of the header a FLAC stream starts with: the marker and its metadata blocks.
    None while `data` doesn't hold all of it yet."""
    if len(data) < 4:
        return None
    if data[:4] != FLAC_MARKER:
        raise ValueError("not a FLAC stream")
    position = 4
    while True:
        if len(data) < position + 4:
            return None
        last = bool(data[position] & 0x80)
        position += 4 + int.from_bytes(data[position + 1:position + 4], "big")
        if len(data) < position:
            return None
        if last:
            return position


class Listener:
    """One HTTP connection fetching a stream."""

    def __init__(self, peer: str, fmt: Format, titles: bool = False) -> None:
        self.peer = peer
        self.titles = TitleWeaver() if titles else None   # set if it asked for track titles
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.queued = 0   # bytes waiting to be sent
        self.sent = 0
        self.started = not fmt.has_header   # whether it has been sent the stream's header, if there is one
        self._limit = BACKLOG_SECONDS * fmt.bytes_per_second

    def offer(self, data: bytes, header: bytes = b"") -> bool:
        """Queues data for the listener. A listener that hasn't started yet waits for the stream's
        header to be known and for data that begins a frame, and is given the header first.
        False if the listener has fallen too far behind to take more."""
        if not self.started:
            if not header or not data.startswith(FLAC_FRAME_SYNC):
                return True
            self.started = True
            data = header + data
        if self.queued > self._limit:
            return False
        self.queued += len(data)
        self.queue.put_nowait(data)
        return True

    def abandon(self) -> None:
        """Drops what is still waiting and ends the connection."""
        while not self.queue.empty():
            self.queue.get_nowait()
        self.queued = 0
        self.queue.put_nowait(None)
