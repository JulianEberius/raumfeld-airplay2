# SPDX-License-Identifier: GPL-3.0-or-later
"""The parts of UPnP AV the Raumfeld speakers need: SOAP actions on a media renderer, the
DIDL-Lite description of a stream, and the HTTP underneath. All calls block."""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from xml.sax.saxutils import escape

AV_TRANSPORT = "urn:schemas-upnp-org:service:AVTransport:1"
RENDERING_CONTROL = "urn:schemas-upnp-org:service:RenderingControl:1"
PLAYING_STATES = ("PLAYING", "TRANSITIONING")
STOPPED_STATES = ("STOPPED", "NO_MEDIA_PRESENT")
PAUSED_STATE = "PAUSED_PLAYBACK"

Fetch = Callable[[str], bytes]
Post = Callable[[str, bytes, dict[str, str]], bytes]


def http_get(url: str, timeout: float = 6) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as response:   # follows redirects, which the Raumfeld host uses
        return bytes(response.read())


def http_post(url: str, body: bytes, headers: dict[str, str], timeout: float = 6) -> bytes:
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return bytes(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise UPnPError(f"HTTP {error.code} {detail[:300]}") from None


class UPnPError(RuntimeError):
    """The device refused an action."""


def local_name(tag: str) -> str:
    """An XML tag without its namespace."""
    return tag.rsplit("}", 1)[-1]


def soap_envelope(service: str, action: str, arguments: dict[str, object]) -> bytes:
    body = "".join(f"<{name}>{escape(str(value))}</{name}>" for name, value in arguments.items())
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
            f'<u:{action} xmlns:u="{service}">{body}</u:{action}></s:Body></s:Envelope>').encode()


def soap_outputs(data: bytes, action: str) -> dict[str, str]:
    """The output arguments in the response to `action`."""
    for element in ET.fromstring(data).iter():
        if local_name(element.tag) == f"{action}Response":
            return {local_name(child.tag): (child.text or "") for child in element}
    return {}


def control_urls(description: bytes, location: str) -> dict[str, str]:
    """{service type: control URL} from a device description fetched from `location`."""
    urls: dict[str, str] = {}
    for service in ET.fromstring(description).iter():
        if local_name(service.tag) != "service":
            continue
        fields = {local_name(child.tag): (child.text or "") for child in service}
        if "serviceType" in fields and "controlURL" in fields:
            urls[fields["serviceType"]] = urllib.parse.urljoin(location, fields["controlURL"])
    return urls


def stream_didl(url: str, title: str, protocol_info: str) -> str:
    """DIDL-Lite metadata for an endless stream; `protocol_info` says what it is encoded as."""
    return ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
            '<item id="airplay" parentID="0" restricted="1">'
            f'<dc:title>{escape(title)}</dc:title>'
            '<upnp:class>object.item.audioItem.audioBroadcast</upnp:class>'
            f'<res protocolInfo="{escape(protocol_info)}">{escape(url)}</res></item></DIDL-Lite>')


class Renderer:
    """A media renderer. For Raumfeld: a zone's virtual renderer, which plays one stream in all of
    the zone's rooms and knows each room's volume."""

    def __init__(self, location: str, fetch: Fetch = http_get, post: Post = http_post) -> None:
        self.location = location
        self._post = post
        self._control = control_urls(fetch(location), location)
        if AV_TRANSPORT not in self._control:
            raise UPnPError("renderer without AVTransport")

    def call(self, service: str, action: str, **arguments: object) -> dict[str, str]:
        try:
            data = self._post(self._control[service], soap_envelope(service, action, arguments), {
                "Content-Type": 'text/xml; charset="utf-8"', "SOAPACTION": f'"{service}#{action}"'})
        except UPnPError as error:
            raise UPnPError(f"{action} failed: {error}") from None
        return soap_outputs(data, action)

    def play_stream(self, url: str, title: str, protocol_info: str) -> None:
        self.call(AV_TRANSPORT, "SetAVTransportURI", InstanceID=0, CurrentURI=url,
                  CurrentURIMetaData=stream_didl(url, title, protocol_info))
        self.call(AV_TRANSPORT, "Play", InstanceID=0, Speed=1)

    def leave_standby(self, room_udn: str) -> None:
        """Wakes the zone's room, as the Raumfeld app does. Speakers in standby don't always wake
        when told to play."""
        self.call(AV_TRANSPORT, "LeaveStandby", InstanceID=0, Room=room_udn)

    def stop(self) -> None:
        self.call(AV_TRANSPORT, "Stop", InstanceID=0)

    def state(self) -> str:
        return self.call(AV_TRANSPORT, "GetTransportInfo", InstanceID=0).get("CurrentTransportState", "")

    def current_uri(self) -> str:
        return self.call(AV_TRANSPORT, "GetMediaInfo", InstanceID=0).get("CurrentURI", "")

    def room_volume(self, room_udn: str) -> int | None:
        """Raumfeld's per-room volume, 0-100."""
        value = self.call(RENDERING_CONTROL, "GetRoomVolume", InstanceID=0, Room=room_udn).get("CurrentVolume", "")
        return int(value) if value.isdigit() else None

    def set_room_volume(self, room_udn: str, volume: int) -> None:
        if RENDERING_CONTROL in self._control:
            self.call(RENDERING_CONTROL, "SetRoomVolume", InstanceID=0, Room=room_udn, DesiredVolume=int(volume))
