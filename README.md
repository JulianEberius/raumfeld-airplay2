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

Download the package for your system from the
[releases](https://github.com/JulianEberius/raumfeld-airplay2/releases) and install it:

```bash
sudo apt install ./raumfeld-airplay_*.deb
```

That is all: the bridge starts, finds the Raumfeld system on its own and offers one AirPlay
speaker per room. There are packages for Debian 13 and 12 (which Raspberry Pi OS is based on),
for arm64 (Raspberry Pi 3 and later with a 64-bit system) and amd64. The package's name says
which: `raumfeld-airplay_1.0.0+debian13_arm64.deb`.

The package brings its own copies of [shairport-sync](https://github.com/mikebrady/shairport-sync)
and [nqptp](https://github.com/mikebrady/nqptp) in `/opt/raumfeld-airplay/bin`, and runs both
itself. It doesn't touch a shairport-sync you may already have; if the machine already runs
nqptp, the bridge uses that one.

To update, install a newer package the same way. The options and what the bridge remembers
about the rooms are kept.

### From a checkout

For another system, or when working on the bridge:

```bash
sudo ./install.sh
```

```bash
./install.sh --on pi
```

This builds the package on the machine it is for (the second form: on another machine, over
ssh) and installs it. The first run builds shairport-sync and nqptp, which takes a few minutes
on a Raspberry Pi; later runs reuse them. `packaging/build-deb.sh` only builds the package,
into `dist/`.

## Options

Options are in `/etc/default/raumfeld-airplay`:

```
OPTIONS="--max-volume 70 --format flac"
```

| Option | Default | Meaning |
|---|---|---|
| `--max-volume N` | 50 | Raumfeld volume (0 to 100) at full AirPlay volume |
| `--format flac\|mp3` | flac, as installed | What the speakers are sent |
| `--host ADDRESS` | found over SSDP | The Raumfeld host's IP address |
| `--grace SECONDS` | 60 | How long a room may be missing before its AirPlay speaker is removed |
| `--verbose` | off | Also log routine events |

`cd /opt/raumfeld-airplay && python3 -m raumfeld_airplay --help` lists all of them. After a
change:

```bash
sudo systemctl restart raumfeld-airplay
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
sudo apt remove raumfeld-airplay
```

`sudo apt purge raumfeld-airplay` also removes the options and what it remembered.

## What it needs

- Debian 12 or later, or a system based on it, with systemd. The package pulls in what it
  needs: Python, ffmpeg, Avahi and the libraries shairport-sync uses.
- The Raumfeld system on the same network.
- TCP port 8090 and, per room, one port from 7000 upward; UDP ports 319 and 320 for the clock
  that AirPlay 2 needs.

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

A tag like `v1.0.0` (matching `__version__`) makes GitHub build the packages and publish them as a
release; every push to `main` builds them as downloadable artifacts of the run.
