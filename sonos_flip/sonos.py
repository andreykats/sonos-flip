"""Talking to Sonos players over their local UPnP/SOAP API (port 1400): requests, topology, discovery."""
import logging
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

log = logging.getLogger(__name__)

PORT = 1400
TIMEOUT = 10   # s per request

# Control paths from /xml/device_description.xml; media services sit under the MediaRenderer sub-device.
CONTROL_PATH = {
    "DeviceProperties": "/DeviceProperties/Control",
    "ZoneGroupTopology": "/ZoneGroupTopology/Control",
    "AVTransport": "/MediaRenderer/AVTransport/Control",
    "RenderingControl": "/MediaRenderer/RenderingControl/Control",
}

# UPnP error codes seen from Sonos and what they mean in practice.
UPNP_MEANING = {
    401: "invalid action: this speaker doesn't support it",
    402: "invalid arguments: often a speaker that hasn't settled after a change",
    800: "not settled yet after a grouping change",
}


class SonosError(Exception):
    """A request to a speaker failed. The message is written for the log and the HTTP response."""


class UPnPError(SonosError):
    def __init__(self, ip, action, code, body=""):
        meaning = UPNP_MEANING.get(code)
        super().__init__(f"{action} on {ip} failed with UPnP error {code}" + (f" ({meaning})" if meaning else ""))
        self.code = code
        self.body = body


def soap(ip, service, action, args="", instance=False):
    """POST a SOAP action and return the response body. service must be a key of CONTROL_PATH."""
    url = f"http://{ip}:{PORT}{CONTROL_PATH[service]}"
    ns = f"urn:schemas-upnp-org:service:{service}:1"
    inst = "<InstanceID>0</InstanceID>" if instance else ""
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        f'<u:{action} xmlns:u="{ns}">{inst}{args}</u:{action}>'
        "</s:Body></s:Envelope>"
    )
    req = urllib.request.Request(
        url,
        data=body.encode(),
        headers={"Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{ns}#{action}"'},
        method="POST",
    )
    log.debug("%s %s %s", ip, action, args)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="replace")
        m = re.search(r"<errorCode>(\d+)</errorCode>", text)
        raise UPnPError(ip, action, int(m.group(1)) if m else e.code, text) from None
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", None) or e
        raise SonosError(f"{action}: no answer from the speaker at {ip} ({reason})") from None


def tag_text(xml, tag):
    """Text of the first <tag> in a SOAP response, or None."""
    m = re.search(rf"<{tag}>(.*?)</{tag}>", xml, re.S)
    return m.group(1) if m else None


def uid_of(ip):
    """The player ID (RINCON_...) of the speaker at ip."""
    try:
        with urllib.request.urlopen(f"http://{ip}:{PORT}/status/zp", timeout=TIMEOUT) as r:
            text = r.read().decode(errors="replace")
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", None) or e
        raise SonosError(f"no answer from a Sonos speaker at {ip} ({reason})") from None
    m = re.search(r"<LocalUID>(RINCON_[A-Za-z0-9]+)</LocalUID>", text)
    if not m:
        raise SonosError(f"the device at {ip} doesn't look like a Sonos speaker")
    return m.group(1)


# --------------------------------------------------------------- topology ----

class Topology:
    """One snapshot of the household from GetZoneGroupState.

    members: ZoneGroupMember elements by UID (rooms and the hidden halves of pairs).
    satellites: Satellite elements by UID (surrounds and subs bonded to a soundbar).
    Each entry is a dict of the element's attributes (UUID, ZoneName, Location, Invisible,
    ChannelMapSet, HTSatChanMapSet, ...)."""

    def __init__(self, xml):
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as e:
            raise SonosError(f"unreadable ZoneGroupState from the soundbar ({e})") from None
        self.members = {}
        self.satellites = {}
        for el in root.iter():
            if el.tag == "ZoneGroupMember" and el.get("UUID"):
                self.members[el.get("UUID")] = dict(el.attrib)
            elif el.tag == "Satellite" and el.get("UUID"):
                self.satellites[el.get("UUID")] = dict(el.attrib)

    def attrs(self, uid):
        return self.members.get(uid) or self.satellites.get(uid) or {}

    def ip(self, uid):
        loc = self.attrs(uid).get("Location")
        return urllib.parse.urlparse(loc).hostname if loc else None

    def ips(self):
        """UID -> IP for every player that reports one."""
        out = {}
        for uid in (*self.satellites, *self.members):
            ip = self.ip(uid)
            if ip:
                out[uid] = ip
        return out

    def uid_by_ip(self, ip):
        return next((uid for uid, a in self.ips().items() if a == ip), None)

    def visible_rooms(self):
        """UIDs of members shown as rooms in the Sonos app (not hidden pair halves, not satellites)."""
        return [uid for uid, a in self.members.items() if a.get("Invisible", "0") == "0"]

    def room_names(self):
        return sorted({self.members[uid].get("ZoneName", "") for uid in self.visible_rooms()})

    def standalone(self, uid):
        """A visible room on its own: not a satellite, not in a stereo pair, not a soundbar with satellites.
        Only the ZoneGroupMember element counts; Satellite elements lag behind changes."""
        a = self.members.get(uid)
        return bool(a) and a.get("Invisible", "0") == "0" and not a.get("ChannelMapSet") \
            and not a.get("HTSatChanMapSet")

    def ht_map(self, soundbar_uid):
        """The soundbar's HTSatChanMapSet, e.g. 'SB:LF,RF;L:LR;R:RR' ('' when it has no satellites)."""
        return self.members.get(soundbar_uid, {}).get("HTSatChanMapSet", "")

    def pair_map(self, uid):
        """ChannelMapSet of the stereo pair uid belongs to ('' when not paired)."""
        return self.attrs(uid).get("ChannelMapSet", "")


def channel_map(value):
    """'UID:LF,LF;UID2:RF,RF' -> {'UID': ['LF', 'LF'], 'UID2': ['RF', 'RF']}"""
    out = {}
    for part in filter(None, value.split(";")):
        uid, _, chans = part.partition(":")
        out[uid] = chans.split(",") if chans else []
    return out


def zone_group_state(ip):
    """Fetch the household topology from the player at ip."""
    r = soap(ip, "ZoneGroupTopology", "GetZoneGroupState")
    try:
        inner = ET.fromstring(r).find(".//ZoneGroupState")
    except ET.ParseError as e:
        raise SonosError(f"unreadable GetZoneGroupState response from {ip} ({e})") from None
    if inner is None or not inner.text:
        raise SonosError(f"GetZoneGroupState from {ip} returned no topology")
    return Topology(inner.text)


# -------------------------------------------------------------- discovery ----

SSDP_ADDR = ("239.255.255.250", 1900)
SSDP_SEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    "HOST: 239.255.255.250:1900\r\n"
    'MAN: "ssdp:discover"\r\n'
    "MX: 1\r\n"
    "ST: urn:schemas-upnp-org:device:ZonePlayer:1\r\n\r\n"
).encode()


def discover(timeout=3.0):
    """IPs of Sonos players that answer an SSDP search on the local network (needs host networking)."""
    ips = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            for _ in range(2):          # UDP: send twice in case one is dropped
                sock.sendto(SSDP_SEARCH, SSDP_ADDR)
            deadline = time.monotonic() + timeout
            while (left := deadline - time.monotonic()) > 0:
                sock.settimeout(left)
                try:
                    data, (ip, _) = sock.recvfrom(4096)
                except socket.timeout:
                    break
                if b"ZonePlayer" in data and ip not in ips:
                    ips.append(ip)
    except OSError as e:
        raise SonosError(f"speaker discovery failed: {e.strerror or e}") from None
    log.debug("discovery found %s", ips)
    return ips
