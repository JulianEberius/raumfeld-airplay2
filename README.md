# AirPlay to Raumfeld bridge

Makes every room of a Teufel Raumfeld system show up as an AirPlay 2 speaker. Runs on a small
Linux machine on the same network, such as a Raspberry Pi.

- One AirPlay speaker per Raumfeld room, named after the room. Rooms that are unplugged
  disappear, and come back when they are plugged in again.
- Rooms selected together on one sender are joined into a Raumfeld zone and play in sync.
- Lossless: the speakers are sent FLAC.
- Pause, play and volume work from the sender and from the keys on the speakers.
- The Raumfeld app shows the playing track's artist and title.
- Steps aside when another source, such as Spotify, takes a room.

How it works is described in `protocol-flow.html`.

## Install

On the machine that is to run it (Debian 12 or later, Raspberry Pi OS, Ubuntu):

```bash
sudo ./install.sh
```

Or from another machine, over ssh:

```bash
./install.sh --on pi
```

The script installs the packages it needs, builds [shairport-sync](https://github.com/mikebrady/shairport-sync)
and [nqptp](https://github.com/mikebrady/nqptp) from source if they aren't there (a few minutes on
a Raspberry Pi 4), installs the bridge as the service `raumfeld-airplay`, and lists the AirPlay
speakers it now offers. The Raumfeld system is found on its own.

It is safe to run again. Running it again is also how to update: it installs the checkout it
is run from, and keeps the options and what the bridge remembers about the rooms.

## Options

Options are in `/etc/default/raumfeld-airplay`:

```
OPTIONS="--max-volume 70 --format flac"
```

| Option | Default | Meaning |
|---|---|---|
| `--max-volume N` | 50 | Raumfeld volume (0 to 100) at full AirPlay volume |
| `--format flac\|mp3` | flac when installed by the script | What the speakers are sent |
| `--host ADDRESS` | found over SSDP | The Raumfeld host's IP address |
| `--grace SECONDS` | 60 | How long a room may be missing before its AirPlay speaker is removed |
| `--verbose` | off | Also log routine events |

`cd /opt/raumfeld-airplay && python3 -m raumfeld_airplay --help` lists all of them. Restart the
service after a change, or set them while installing:

```bash
sudo ./install.sh -- --max-volume 70 --format flac
```

## Looking at it

```bash
curl http://127.0.0.1:8090/status
```

```bash
journalctl -u raumfeld-airplay -f
```

## Remove

```bash
sudo ./install.sh uninstall
```

This removes the bridge. It leaves shairport-sync, nqptp and the installed packages, and says
where they are.

## What it needs

- Linux with systemd, and `apt` for the script. Elsewhere, install by hand what the script
  installs: Python 3.11 or later, ffmpeg, curl, Avahi, nqptp, and shairport-sync 5 or later
  built with `--with-airplay-2 --with-pipe --with-metadata --with-dbus-interface --with-avahi`.
- The Raumfeld system on the same network.
- TCP port 8090 and, per room, one port from 7000 upward.

## Working on it

The bridge is the Python package `raumfeld_airplay`, standard library only.

```bash
python3 -m unittest discover -s tests -t .
```

```bash
mypy raumfeld_airplay tests
```

`tests/fake_raumfeld.py` stands in for a Raumfeld system, so the bridge can be tried without
real speakers; its first lines say how.
