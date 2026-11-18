"""The audio on its way to the speakers: raw PCM from shairport-sync, encoded to an endless MP3
stream that the Raumfeld host fetches over HTTP like an internet radio station."""

from __future__ import annotations

import asyncio

RATE, CHANNELS, BITS = 44100, 2, 16
PCM_BYTES_PER_SECOND = RATE * CHANNELS * BITS // 8
MP3_BYTES_PER_SECOND = 40_000   # 320 kbit/s
# How far a listener may fall behind before it counts as stalled: six seconds of stream.
BACKLOG_LIMIT = 6 * MP3_BYTES_PER_SECOND

STREAM_RESPONSE = (b"HTTP/1.1 200 OK\r\nContent-Type: audio/mpeg\r\nCache-Control: no-cache\r\n"
                   b"Connection: close\r\nAccept-Ranges: none\r\n\r\n")


def stream_path(ident: str) -> str:
    return f"/stream/{ident}.mp3"


def silence(seconds: float) -> bytes:
    return bytes(int(PCM_BYTES_PER_SECOND * seconds) & ~3)


def encoder_command() -> list[str]:
    """ffmpeg turning PCM on its standard input into MP3 on its standard output, without delay."""
    return ["ffmpeg", "-hide_banner", "-loglevel", "error",
            # Without these ffmpeg collects seconds of input before it produces anything.
            "-probesize", "32", "-analyzeduration", "0", "-fflags", "nobuffer",
            "-f", "s16le", "-ar", str(RATE), "-ac", str(CHANNELS), "-i", "pipe:0",
            "-c:a", "libmp3lame", "-b:a", "320k", "-f", "mp3", "-flush_packets", "1", "pipe:1"]


class Listener:
    """One HTTP connection fetching a stream."""

    def __init__(self, peer: str) -> None:
        self.peer = peer
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.queued = 0   # bytes waiting to be sent
        self.sent = 0

    def offer(self, data: bytes) -> bool:
        """Queues data for the listener. False if it has fallen too far behind to take more."""
        if self.queued > BACKLOG_LIMIT:
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
