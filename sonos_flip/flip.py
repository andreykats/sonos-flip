"""Finding the speakers, and switching them between the two modes:

  desk     left and right form a standalone stereo pair (AirPlay, line-in and Bluetooth work)
  theater  left and right are the soundbar's rear surrounds (Sonos turns those inputs off)

Recipes and the settle/retry strategy follow willblaschko/chorus docs/SPIKE_FINDINGS.md
(hardware-validated on an Arc and satellites), and were re-checked on an Arc Ultra with two Era 100 SL.
"""
import html
import ipaddress
import logging
import re
import threading
import time

from . import sonos, state
from .sonos import SonosError, UPnPError, channel_map

log = logging.getLogger(__name__)

SETTLE_POLL_SEC = 1.5     # between topology checks while waiting for a speaker to settle
RETRY_FIRST_SEC = 2       # first wait after "not settled yet"; grows x1.6 up to RETRY_MAX_SEC
RETRY_MAX_SEC = 12
SURROUND_CHANNEL = {"left": "LR", "right": "RR"}
PAIR_CHANNEL = {"left": "LF", "right": "RF"}


def is_ip(value):
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


class Flip:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()          # one switch at a time
        self.ready = False                    # set once connect() has found the speakers
        self.startup_error = None             # why connect() failed, while not ready
        self.soundbar_ip = cfg.soundbar if is_ip(cfg.soundbar) else None
        self.ips = {}                         # UID -> last known IP
        self.soundbar_uid = self.left_uid = self.right_uid = None
        self.pair_channel_map = ""
        self.last_request = float("-inf")     # monotonic time of the latest /desk or /theater request

    # ------------------------------------------------------- finding speakers ----

    def topology(self):
        """The household topology, asked of the soundbar. A soundbar given by name is found again
        if it stops answering (its IP may have changed)."""
        t = None
        if self.soundbar_ip:
            try:
                t = sonos.zone_group_state(self.soundbar_ip)
            except SonosError as e:
                if is_ip(self.cfg.soundbar):
                    raise
                log.warning("Soundbar at %s isn't answering (%s); searching the network for it", self.soundbar_ip, e)
        if t is None:
            t = self.discover_soundbar()
        self.ips.update(t.ips())
        if self.soundbar_uid and not is_ip(self.cfg.soundbar) and self.ips.get(self.soundbar_uid):
            self.soundbar_ip = self.ips[self.soundbar_uid]
        return t

    def discover_soundbar(self):
        """Find the soundbar by room name through any speaker that answers discovery."""
        ips = sonos.discover()
        if not ips:
            raise SonosError(
                "no Sonos speakers answered on the network. Run the container with host networking "
                "(--network host) on the speakers' network, or set speakers.soundbar to the soundbar's IP")
        errors = []
        for ip in ips:
            try:
                t = sonos.zone_group_state(ip)
            except SonosError as e:
                errors.append(str(e))
                continue
            uid = self.soundbar_by_name(t)
            self.soundbar_ip = t.ip(uid)
            log.info('Found soundbar "%s" at %s', self.cfg.soundbar, self.soundbar_ip)
            return t
        raise SonosError(f"speakers answered discovery but not the follow-up request: {'; '.join(errors)}")

    def soundbar_by_name(self, t):
        name = self.cfg.soundbar.lower()
        found = [u for u in t.visible_rooms() if t.members[u].get("ZoneName", "").lower() == name]
        if len(found) == 1 and t.ip(found[0]):
            return found[0]
        if len(found) > 1:
            raise SonosError(f'speakers.soundbar: {len(found)} rooms are named "{self.cfg.soundbar}"; '
                             "rename one in the Sonos app or use the soundbar's IP")
        raise SonosError(f'speakers.soundbar: no room named "{self.cfg.soundbar}". '
                         f"Rooms found: {', '.join(t.room_names())}")

    def connect(self):
        """Find the soundbar and both speakers. Raises SonosError saying what to fix."""
        t = self.topology()
        if is_ip(self.cfg.soundbar):
            sb = t.uid_by_ip(self.cfg.soundbar) or sonos.uid_of(self.cfg.soundbar)
        else:
            sb = self.soundbar_by_name(t)
        remembered = state.load(self.cfg.state_file).get("speakers", {})
        left, how_left = self.find_speaker(t, "left", sb, remembered)
        right, how_right = self.find_speaker(t, "right", sb, remembered)
        if left == right:
            raise SonosError("speakers.left and speakers.right are the same speaker")
        if sb in (left, right):
            raise SonosError("speakers.left / speakers.right must not be the soundbar")
        self.soundbar_uid, self.left_uid, self.right_uid = sb, left, right
        self.pair_channel_map = f"{left}:LF,LF;{right}:RF,RF"
        log.info("Soundbar:      %s, %s", self.describe(t, sb), sb)
        log.info("Left speaker:  %s, %s (%s)", self.describe(t, left), left, how_left)
        log.info("Right speaker: %s, %s (%s)", self.describe(t, right), right, how_right)
        new = {role: {"config": getattr(self.cfg, role), "uid": uid}
               for role, uid in (("soundbar", sb), ("left", left), ("right", right))}
        if new != remembered:
            try:
                state.update(self.cfg.state_file, speakers=new)
            except OSError as e:
                log.warning("Couldn't save the speakers to %s (%s); they'll be looked up again next start",
                            self.cfg.state_file, e)
        self.ready = True

    def describe(self, t, uid):
        name = t.attrs(uid).get("ZoneName")
        ip = self.ips.get(uid, "IP not known yet")
        return f'"{name}" at {ip}' if name else ip

    def find_speaker(self, t, role, soundbar_uid, remembered):
        """UID of the left/right speaker and how it was found. Tried in order: IP; the speaker found for
        the same setting before; a standalone room with that name; its role in a current bond (bonded
        speakers show the soundbar's or the pair's name, not their own)."""
        value = getattr(self.cfg, role)
        if is_ip(value):
            uid = t.uid_by_ip(value) or sonos.uid_of(value)
            self.ips[uid] = value
            return uid, "by IP"
        rem = remembered.get(role) or {}
        if rem.get("config") == value and rem.get("uid") in (*t.members, *t.satellites):
            return rem["uid"], "remembered from an earlier start"
        named = [u for u in t.visible_rooms()
                 if u != soundbar_uid and t.standalone(u) and t.members[u].get("ZoneName", "").lower() == value.lower()]
        if len(named) == 1:
            return named[0], "by room name"
        if len(named) > 1:
            raise SonosError(f'speakers.{role}: {len(named)} rooms are named "{value}"; '
                             "rename one in the Sonos app or use the speaker's IP")
        for uid, chans in channel_map(t.ht_map(soundbar_uid)).items():
            if SURROUND_CHANNEL[role] in chans:
                return uid, f"the soundbar's {role} surround"
        for room in t.visible_rooms():
            if t.members[room].get("ZoneName", "").lower() == self.cfg.pair_name.lower():
                for uid, chans in channel_map(t.pair_map(room)).items():
                    if PAIR_CHANNEL[role] in chans:
                        return uid, f'the {role} half of the "{self.cfg.pair_name}" pair'
        raise SonosError(
            f'speakers.{role}: no speaker named "{value}" on its own, no {role} surround on the soundbar, '
            f'and no stereo pair named "{self.cfg.pair_name}". Rooms found: {", ".join(t.room_names())}. '
            "Use the speaker's room name from the Sonos app (while it's not paired or bonded), or its IP")

    def ip(self, uid):
        ip = self.ips.get(uid)
        if not ip:
            raise SonosError(f"the IP of {uid} isn't known yet (the soundbar didn't report it)")
        return ip

    # --------------------------------------------------------------- state ----

    def state(self):
        t = self.topology()
        hm = t.ht_map(self.soundbar_uid)
        pm = t.pair_map(self.left_uid)
        surrounds, pair = channel_map(hm), channel_map(pm)
        if self.left_uid in surrounds and self.right_uid in surrounds:
            mode = "theater"
        elif self.right_uid in pair:
            mode = "desk"
        else:
            mode = "unknown"
        return {
            "mode": mode,
            "surround_map": hm,
            "pair_map": pm,
            "left_standalone": t.standalone(self.left_uid),
            "right_standalone": t.standalone(self.right_uid),
        }

    # ------------------------------------------------------------- helpers ----

    def wait_standalone(self, uid, role):
        t0 = time.monotonic()
        while time.monotonic() - t0 < self.cfg.settle_timeout_sec:
            if self.topology().standalone(uid):
                log.info("  %s speaker is on its own (%.1f s)", role.capitalize(), time.monotonic() - t0)
                return
            time.sleep(SETTLE_POLL_SEC)
        raise SonosError(f"the {role} speaker didn't come back on its own within "
                         f"{self.cfg.settle_timeout_sec:g} s (timing.settle_timeout_sec)")

    def retry_not_ready(self, fn, label):
        """Run fn, retrying with backoff while a speaker answers UPnP 800 (not settled yet)."""
        delay = RETRY_FIRST_SEC
        for i in range(self.cfg.retries_not_ready):
            try:
                fn()
                log.info("  %s: done", label)
                return
            except UPnPError as e:
                if e.code != 800:
                    raise
                log.info("  %s: speaker not settled yet, retry %d/%d in %.0f s",
                         label, i + 1, self.cfg.retries_not_ready, delay)
                time.sleep(delay)
                delay = min(delay * 1.6, RETRY_MAX_SEC)
        raise SonosError(f"{label}: the speakers still weren't settled after "
                         f"{self.cfg.retries_not_ready} tries (timing.retries_not_ready)")

    # ---------------------------------------- primitives (one satellite per call) ----

    def remove_sat(self, uid):
        sonos.soap(self.soundbar_ip, "DeviceProperties", "RemoveHTSatellite", f"<SatRoomUUID>{uid}</SatRoomUUID>")

    def add_sat(self, uid, channel):
        sonos.soap(self.soundbar_ip, "DeviceProperties", "AddHTSatellite",
                   f"<HTSatChanMapSet>{self.soundbar_uid}:CC;{uid}:{channel}</HTSatChanMapSet>")

    def create_pair(self):
        sonos.soap(self.ip(self.left_uid), "DeviceProperties", "CreateStereoPair",
                   f"<ChannelMapSet>{self.pair_channel_map}</ChannelMapSet>")

    def separate_pair(self):
        sonos.soap(self.ip(self.left_uid), "DeviceProperties", "SeparateStereoPair",
                   f"<ChannelMapSet>{self.pair_channel_map}</ChannelMapSet>")

    def zone_attrs(self):
        r = sonos.soap(self.ip(self.left_uid), "DeviceProperties", "GetZoneAttributes")
        return dict(re.findall(r"<Current(\w+)>([^<]*)</Current\1>", r))

    def rename_pair(self, attrs):
        """Set the pair's room name (= its AirPlay name), keeping icon and configuration as they are."""
        sonos.soap(self.ip(self.left_uid), "DeviceProperties", "SetZoneAttributes",
                   f"<DesiredZoneName>{html.escape(self.cfg.pair_name)}</DesiredZoneName>"
                   f"<DesiredIcon>{attrs.get('Icon', '')}</DesiredIcon>"
                   f"<DesiredConfiguration>{attrs.get('Configuration', '1')}</DesiredConfiguration>"
                   f"<DesiredTargetRoomName>{attrs.get('TargetRoomName', '')}</DesiredTargetRoomName>")

    def ensure_pair_name(self):
        """Sonos names a new pair after its left speaker (e.g. "Era Left"); put the configured name back."""
        attrs = self.zone_attrs()
        name = html.unescape(attrs.get("ZoneName", ""))
        if name == self.cfg.pair_name:
            return False
        log.info('  Renaming the pair "%s" -> "%s"', name, self.cfg.pair_name)
        self.retry_not_ready(lambda: self.rename_pair(attrs), "rename pair")
        return True

    # ------------------------------------------------------------- line-in ----

    def play_line_in(self):
        """Point the pair (via its coordinator, the left speaker) at the adapter speaker's line-in and play."""
        src = self.left_uid if self.cfg.line_in == "left" else self.right_uid
        left_ip = self.ip(self.left_uid)
        sonos.soap(left_ip, "AVTransport", "SetAVTransportURI",
                   f"<CurrentURI>x-rincon-stream:{src}</CurrentURI><CurrentURIMetaData></CurrentURIMetaData>",
                   instance=True)
        sonos.soap(left_ip, "AVTransport", "Play", "<Speed>1</Speed>", instance=True)

    def pair_volume(self):
        r = sonos.soap(self.ip(self.left_uid), "RenderingControl", "GetVolume", "<Channel>Master</Channel>",
                       instance=True)
        v = sonos.tag_text(r, "CurrentVolume")
        if v is None or not v.isdigit() or not 0 <= int(v) <= 100:
            raise SonosError(f"the pair reported no valid volume ({v!r})")
        return int(v)

    def set_pair_volume(self, v):
        sonos.soap(self.ip(self.left_uid), "RenderingControl", "SetVolume",
                   f"<Channel>Master</Channel><DesiredVolume>{v}</DesiredVolume>", instance=True)

    def raise_volume(self):
        """Save the pair's volume (unless one is already saved and not yet set back), then set
        line_in.volume. Saving comes first, so a volume that couldn't be saved is never overwritten."""
        path = self.cfg.state_file
        kept = state.saved_volume(path)
        if kept is None:
            v = self.pair_volume()
            state.update(path, saved_volume=v)
            log.info("  Saved the pair's volume (%d) to set back later", v)
        else:
            log.info("  Keeping the earlier saved volume (%d), which hasn't been set back yet", kept)
        self.retry_not_ready(lambda: self.set_pair_volume(self.cfg.line_in_volume),
                             f"volume {self.cfg.line_in_volume}")

    def start_line_in(self):
        """After a switch to desk: play line-in and set line_in.volume, if configured.
        The volume is only raised once line-in plays. Never fails the switch itself."""
        if self.cfg.line_in == "off":
            return {}
        try:
            self.retry_not_ready(self.play_line_in, f"line-in from the {self.cfg.line_in} speaker")
        except SonosError as e:
            log.warning("  Line-in didn't start: %s. Is the line-in adapter on the %s speaker? "
                        "The volume was left as it was.", e, self.cfg.line_in)
            return {"line_in": f"failed: {e}"}
        try:
            self.raise_volume()
            return {"line_in": "playing", "volume": self.cfg.line_in_volume}
        except (SonosError, OSError) as e:
            log.warning("  Line-in is playing, but the volume wasn't changed: %s", e)
            return {"line_in": "playing", "volume": f"failed: {e}"}

    def restore_volume(self):
        """Before the pair is split for theater: set back the volume saved by raise_volume.
        A failure keeps the saved value for next time and never fails the switch itself."""
        path = self.cfg.state_file
        v = state.saved_volume(path)
        if v is None:
            return {}
        try:
            self.retry_not_ready(lambda: self.set_pair_volume(v), f"set the volume back to {v}")
            state.update(path, saved_volume=None)
            return {"volume": f"restored {v}"}
        except (SonosError, OSError) as e:
            log.warning("  Couldn't set the volume back to %d (%s); will try again on the next switch to theater",
                        v, e)
            return {"volume": f"restore failed: {e}"}

    # --------------------------------------------------------------- modes ----

    def switch(self, mode):
        """Switch to "desk" or "theater". Logs the outcome; re-raises failures for the caller."""
        with self.lock:
            t0 = time.monotonic()
            try:
                out = self._to_desk() if mode == "desk" else self._to_theater()
            except Exception as e:
                level = logging.ERROR if isinstance(e, SonosError) else logging.CRITICAL
                log.log(level, "Switch to %s failed after %.0f s: %s. The speakers may be part-way; "
                        "asking for %s again (or the other mode) finishes the job.",
                        mode, time.monotonic() - t0, e, mode, exc_info=not isinstance(e, SonosError))
                raise
            if out.get("changed"):
                log.info("%s mode ready (%.0f s)", mode.capitalize(), time.monotonic() - t0)
            return out

    def to_desk(self):
        return self.switch("desk")

    def to_theater(self):
        return self.switch("theater")

    def _to_desk(self):
        st = self.state()
        if st["mode"] == "desk":
            log.info("Already in desk mode")
            return {"changed": self.ensure_pair_name(), **st}
        log.info("Switching to desk: taking the speakers off the soundbar and pairing them")
        surrounds = channel_map(st["surround_map"])
        for uid in (self.left_uid, self.right_uid):
            if uid in surrounds:
                self.remove_sat(uid)
        self.wait_standalone(self.left_uid, "left")
        self.wait_standalone(self.right_uid, "right")
        time.sleep(self.cfg.pause_before_bond_sec)
        self.retry_not_ready(self.create_pair, "create stereo pair")
        self.ensure_pair_name()
        return {"changed": True, **self.start_line_in(), **self.state()}

    def _to_theater(self):
        st = self.state()
        if st["mode"] == "theater":
            log.info("Already in theater mode")
            return {"changed": False, **st}
        log.info("Switching to theater: splitting the pair and adding the speakers to the soundbar")
        restored = {}
        if self.right_uid in channel_map(st["pair_map"]):
            restored = self.restore_volume()
            self.separate_pair()
        # Skip a speaker that is already bonded (an earlier switch may have stopped part-way).
        surrounds = channel_map(st["surround_map"])
        todo = [(uid, SURROUND_CHANNEL[role], role) for uid, role in
                ((self.left_uid, "left"), (self.right_uid, "right")) if uid not in surrounds]
        for uid, _, role in todo:
            self.wait_standalone(uid, role)
        for uid, ch, role in todo:
            time.sleep(self.cfg.pause_before_bond_sec)
            self.retry_not_ready(lambda: self.add_sat(uid, ch), f"add {role} surround")
        return {"changed": True, **restored, **self.state()}

    # ------------------------------------------------- soundbar playback ----

    def soundbar_transport(self):
        """What the soundbar is playing: used to detect TV audio, and shown by GET /soundbar.
        A soundbar given by name is looked up again if it stops answering."""
        try:
            return self._soundbar_transport()
        except SonosError:
            if is_ip(self.cfg.soundbar):
                raise
            self.topology()
            return self._soundbar_transport()

    def _soundbar_transport(self):
        ti = sonos.soap(self.soundbar_ip, "AVTransport", "GetTransportInfo", instance=True)
        mi = sonos.soap(self.soundbar_ip, "AVTransport", "GetMediaInfo", instance=True)
        uri = sonos.tag_text(mi, "CurrentURI")
        return {
            "transport_state": sonos.tag_text(ti, "CurrentTransportState"),
            "current_uri": html.unescape(uri) if uri is not None else None,
        }
