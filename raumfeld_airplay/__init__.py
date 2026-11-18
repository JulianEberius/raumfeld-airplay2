"""AirPlay to Raumfeld bridge: one AirPlay 2 speaker per Raumfeld room.

    model      the decision rules, free of I/O
    bridge     the asyncio program that applies them
    raumfeld   the Raumfeld host: rooms and zones
    upnp       UPnP AV: playing a stream on a renderer
    shairport  shairport-sync: configuration, metadata feed, remote control
    streaming  PCM to FLAC or MP3 over HTTP
    store      what is kept between runs
"""

__version__ = "1.0.0"
