"""Tests. Topology fixtures follow real ZoneGroupState from an Arc Ultra (firmware 97.2) with two
Era 100 SL, trimmed to the attributes the code reads; the player IDs are made up.
Run: python3 -m unittest -v"""
import dataclasses
import html
import json
import logging
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from sonos_flip import __main__ as main_mod
from sonos_flip import config, flip as flip_mod, server, sonos, state, watcher

logging.disable(logging.CRITICAL)   # keep test output readable; set to NOTSET to debug

HERE = os.path.dirname(os.path.abspath(__file__))
SB, L, R = "RINCON_000000000A0101400", "RINCON_000000000B0201400", "RINCON_000000000C0301400"
IP = {SB: "10.0.0.1", L: "10.0.0.2", R: "10.0.0.3"}
PAIR = f"{L}:LF,LF;{R}:RF,RF"


def loc(uid):
    return f'Location="http://{IP[uid]}:1400/xml/device_description.xml"'


DESK = (
    f'<ZoneGroupState><ZoneGroups><ZoneGroup Coordinator="{SB}" ID="{SB}:1">'
    f'<ZoneGroupMember UUID="{SB}" ZoneName="Living Room" {loc(SB)}/></ZoneGroup>'
    f'<ZoneGroup Coordinator="{L}" ID="{L}:2">'
    f'<ZoneGroupMember UUID="{L}" ZoneName="Desk" {loc(L)} ChannelMapSet="{PAIR}"/>'
    f'<ZoneGroupMember UUID="{R}" ZoneName="Desk" {loc(R)} Invisible="1" ChannelMapSet="{PAIR}"/>'
    f'</ZoneGroup></ZoneGroups></ZoneGroupState>'
)


def theater(*sats, names=None):
    """Soundbar with the given (uid, channel) satellites; the other speakers standalone.
    Satellites carry no Location here, to check the code copes without their IPs."""
    names = names or {L: "Desk", R: "Desk"}
    hm = ";".join([f"{SB}:LF,RF"] + [f"{u}:{c}" for u, c in sats])
    inner = "".join(f'<Satellite UUID="{u}" ZoneName="Living Room" HTSatChanMapSet="{hm}" Invisible="1"/>'
                    for u, _ in sats)
    standalone = "".join(f'<ZoneGroup Coordinator="{u}" ID="{u}:9"><ZoneGroupMember UUID="{u}" '
                         f'ZoneName="{names[u]}" {loc(u)}/></ZoneGroup>'
                         for u in (L, R) if u not in dict(sats))
    return (f'<ZoneGroupState><ZoneGroups><ZoneGroup Coordinator="{SB}" ID="{SB}:1">'
            f'<ZoneGroupMember UUID="{SB}" ZoneName="Living Room" {loc(SB)} HTSatChanMapSet="{hm}">{inner}'
            f'</ZoneGroupMember></ZoneGroup>{standalone}</ZoneGroups></ZoneGroupState>')


BONDED = theater((L, "LR,LTR"), (R, "RR,RTR"))

# What a speaker answers to the EQ reads, and the EQ that reads as (LF and RF both answer 23 here, so the
# tests that write EQ use their own values)
EQ_REPLY = ("<CurrentBass>-3</CurrentBass><CurrentTreble>2</CurrentTreble><CurrentLoudness>1</CurrentLoudness>"
            "<CurrentVolume>23</CurrentVolume>")
EQ_READ = {"bass": -3, "treble": 2, "loudness": True, "left_volume": 23, "right_volume": 23}
EQ = {**EQ_READ, "left_volume": 100, "right_volume": 90}
APART = theater(names={L: "Desk L", R: "Desk R"})


def topo(xml):
    return sonos.Topology(xml)


class TempDir(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.state_file = os.path.join(tmp.name, "state.json")

    def cfg(self, **kw):
        base = dict(soundbar=IP[SB], left="Desk L", right="Desk R", pair_name="Desk", state_file=self.state_file)
        return config.Config(**{**base, **kw})

    def make_flip(self, xml, **kw):
        """A connected Flip, found in topology xml."""
        f = flip_mod.Flip(self.cfg(**kw))
        with mock.patch.object(sonos, "zone_group_state", return_value=topo(xml)), \
             mock.patch.object(sonos, "uid_of", side_effect=lambda ip: {v: k for k, v in IP.items()}[ip]):
            f.connect()
        return f

    def saved(self):
        return state.load(self.state_file)


# ----------------------------------------------------------------- parsing ----

class Parsing(unittest.TestCase):
    def test_desk(self):
        t = topo(DESK)
        self.assertEqual(t.pair_map(L), PAIR)
        self.assertEqual(t.ht_map(SB), "")
        self.assertFalse(t.standalone(L) or t.standalone(R))
        self.assertEqual(t.room_names(), ["Desk", "Living Room"])   # the hidden right half isn't a room
        self.assertEqual(t.ips(), IP)

    def test_theater(self):
        t = topo(BONDED)
        self.assertEqual(sonos.channel_map(t.ht_map(SB)), {SB: ["LF", "RF"], L: ["LR", "LTR"], R: ["RR", "RTR"]})
        self.assertEqual(t.ips(), {SB: IP[SB]})                      # satellites report no Location here
        self.assertEqual(t.room_names(), ["Living Room"])

    def test_standalone(self):
        t = topo(theater((L, "LR")))
        self.assertFalse(t.standalone(L))
        self.assertTrue(t.standalone(R))
        self.assertFalse(t.standalone("RINCON_UNKNOWN"))

    def test_zone_group_state_unwraps_soap(self):
        """The topology is XML escaped inside the SOAP response; names with & and quotes survive."""
        xml = DESK.replace('ZoneName="Desk"', 'ZoneName="Tom &amp; Jo&quot;s"')
        body = ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                '<u:GetZoneGroupStateResponse xmlns:u="urn:schemas-upnp-org:service:ZoneGroupTopology:1">'
                f'<ZoneGroupState>{html.escape(xml)}</ZoneGroupState>'
                '</u:GetZoneGroupStateResponse></s:Body></s:Envelope>')
        with mock.patch.object(sonos, "soap", return_value=body):
            t = sonos.zone_group_state("10.0.0.1")
        self.assertEqual(t.members[L]["ZoneName"], 'Tom & Jo"s')

    def test_garbage_topology_is_a_sonos_error(self):
        with self.assertRaises(sonos.SonosError):
            sonos.Topology("<ZoneGroupState><oops")

    def test_upnp_error_message(self):
        e = sonos.UPnPError("10.0.0.2", "CreateStereoPair", 800)
        self.assertIn("UPnP error 800 (not settled yet", str(e))

    def test_unreachable_speaker_is_a_sonos_error(self):
        with mock.patch.object(urllib.request, "urlopen", side_effect=urllib.error.URLError("timed out")):
            with self.assertRaisesRegex(sonos.SonosError, r"no answer from the speaker at 10.0.0.9 \(timed out\)"):
                sonos.soap("10.0.0.9", "AVTransport", "Play")


# --------------------------------------------------------- finding speakers ----

class Finding(TempDir):
    def test_by_room_name(self):
        f = self.make_flip(APART)
        self.assertEqual((f.soundbar_uid, f.left_uid, f.right_uid), (SB, L, R))
        self.assertTrue(f.ready)

    def test_by_place_in_desk_pair(self):
        """Paired, both halves are called "Desk": left/right come from LF/RF."""
        f = self.make_flip(DESK)
        self.assertEqual((f.left_uid, f.right_uid), (L, R))

    def test_by_place_on_soundbar(self):
        f = self.make_flip(BONDED)
        self.assertEqual((f.left_uid, f.right_uid), (L, R))

    def test_by_ip(self):
        f = self.make_flip(BONDED, left=IP[L], right=IP[R])
        self.assertEqual((f.left_uid, f.right_uid), (L, R))
        self.assertEqual(f.ips[L], IP[L])

    def test_remembered(self):
        """Found once, then renamed in the app: still recognised from state.json."""
        self.make_flip(APART)
        renamed = theater(names={L: "Other 1", R: "Other 2"})
        f = self.make_flip(renamed)
        self.assertEqual((f.left_uid, f.right_uid), (L, R))
        self.assertEqual(self.saved()["speakers"]["left"], {"config": "Desk L", "uid": L})

    def test_remembered_only_for_same_setting(self):
        self.make_flip(APART)
        with self.assertRaisesRegex(sonos.SonosError, 'no speaker named "Kitchen"'):
            self.make_flip(theater(names={L: "Other 1", R: "Other 2"}), left="Kitchen")

    def test_not_found_lists_rooms(self):
        with self.assertRaises(sonos.SonosError) as e:
            self.make_flip(APART, left="Kitchen")
        self.assertIn("Rooms found: Desk L, Desk R, Living Room", str(e.exception))

    def test_same_speaker_twice(self):
        with self.assertRaisesRegex(sonos.SonosError, "same speaker"):
            self.make_flip(APART, right=IP[L])

    def test_soundbar_by_name_via_discovery(self):
        f = flip_mod.Flip(self.cfg(soundbar="living room"))
        with mock.patch.object(sonos, "discover", return_value=["10.0.0.3"]) as disc, \
             mock.patch.object(sonos, "zone_group_state", return_value=topo(APART)):
            f.connect()
        disc.assert_called_once()
        self.assertEqual((f.soundbar_uid, f.soundbar_ip), (SB, IP[SB]))

    def test_soundbar_found_again_after_ip_change(self):
        f = flip_mod.Flip(self.cfg(soundbar="Living Room"))
        f.soundbar_ip = "10.0.0.99"
        calls = []

        def zgs(ip):
            calls.append(ip)
            if ip == "10.0.0.99":
                raise sonos.SonosError("no answer")
            return topo(APART)
        with mock.patch.object(sonos, "discover", return_value=["10.0.0.2"]), \
             mock.patch.object(sonos, "zone_group_state", side_effect=zgs):
            f.connect()
        self.assertEqual(calls, ["10.0.0.99", "10.0.0.2"])
        self.assertEqual(f.soundbar_ip, IP[SB])

    def test_no_speakers_answer_discovery(self):
        f = flip_mod.Flip(self.cfg(soundbar="Living Room"))
        with mock.patch.object(sonos, "discover", return_value=[]):
            with self.assertRaisesRegex(sonos.SonosError, "--network host"):
                f.connect()

    def test_unwritable_state_does_not_stop_connect(self):
        f = self.make_flip(APART, state_file="/nonexistent-dir/state.json")
        self.assertTrue(f.ready)


# ---------------------------------------------------------------- switching ----

class Switching(TempDir):
    def run_switch(self, f, mode, zgs, soap_side_effect=None,
                   soap_return="<CurrentZoneName>Desk</CurrentZoneName><CurrentVolume>23</CurrentVolume>"):
        soap = mock.Mock(return_value=soap_return, side_effect=soap_side_effect)
        with mock.patch.object(sonos, "zone_group_state", side_effect=[topo(x) for x in zgs]), \
             mock.patch.object(sonos, "soap", soap), mock.patch.object(flip_mod.time, "sleep"):
            return getattr(f, f"to_{mode}")(), soap

    def desk_switch(self, line_in="off", soap_side_effect=None, **kw):
        """A full theater -> desk switch; returns (result, soap mock)."""
        f = self.make_flip(BONDED, line_in=line_in, **kw)
        return self.run_switch(f, "desk", [BONDED, theater(), theater(), DESK], soap_side_effect)

    def theater_switch(self, soap_side_effect=None):
        """A full desk -> theater switch; returns (result, soap mock). Speakers report EQ_REPLY."""
        f = self.make_flip(DESK)
        return self.run_switch(f, "theater", [DESK, theater(), theater(), BONDED], soap_side_effect,
                               soap_return=EQ_REPLY)

    def test_desk_recipe(self):
        out, soap = self.desk_switch()
        self.assertEqual([c.args[:3] for c in soap.call_args_list], [
            (IP[SB], "DeviceProperties", "RemoveHTSatellite"),
            (IP[SB], "DeviceProperties", "RemoveHTSatellite"),
            (IP[L], "DeviceProperties", "CreateStereoPair"),
            (IP[L], "DeviceProperties", "GetZoneAttributes"),
        ])
        self.assertEqual(soap.call_args_list[2].args[3], f"<ChannelMapSet>{PAIR}</ChannelMapSet>")
        self.assertEqual((out["changed"], out["mode"]), (True, "desk"))

    def test_theater_recipe(self):
        out, soap = self.theater_switch()
        self.assertEqual([c.args[2:4] for c in soap.call_args_list if c.args[1] == "DeviceProperties"], [
            ("SeparateStereoPair", f"<ChannelMapSet>{PAIR}</ChannelMapSet>"),
            ("AddHTSatellite", f"<HTSatChanMapSet>{SB}:CC;{L}:LR</HTSatChanMapSet>"),
            ("AddHTSatellite", f"<HTSatChanMapSet>{SB}:CC;{R}:RR</HTSatChanMapSet>"),
        ])
        self.assertEqual(out["mode"], "theater")

    def test_theater_resumes_half_finished_switch(self):
        """Left already bonded, right missing: only the right speaker is waited on and added."""
        half = theater((L, "LR"), names={R: "Desk R"})
        f = self.make_flip(half)
        out, soap = self.run_switch(f, "theater", [half, half, BONDED], soap_return="")
        self.assertEqual([c.args[2:4] for c in soap.call_args_list],
                         [("AddHTSatellite", f"<HTSatChanMapSet>{SB}:CC;{R}:RR</HTSatChanMapSet>")])

    def test_retries_while_not_settled(self):
        n = []

        def flaky(ip, service, action, *a, **k):
            if action == "CreateStereoPair" and len(n) < 2:
                n.append(1)
                raise sonos.UPnPError(ip, action, 800)
            return "<CurrentZoneName>Desk</CurrentZoneName>"
        out, soap = self.desk_switch(soap_side_effect=flaky)
        self.assertEqual([c.args[2] for c in soap.call_args_list].count("CreateStereoPair"), 3)
        self.assertEqual(out["mode"], "desk")

    def test_gives_up_with_clear_error(self):
        def never(ip, service, action, *a, **k):
            if action == "CreateStereoPair":
                raise sonos.UPnPError(ip, action, 800)
            return ""
        with self.assertRaisesRegex(sonos.SonosError, r"still weren't settled after 8 tries \(timing.retries_not_ready\)"):
            self.desk_switch(soap_side_effect=never)

    def test_settle_timeout_names_the_setting(self):
        f = self.make_flip(BONDED, settle_timeout_sec=0.01)
        with mock.patch.object(sonos, "zone_group_state", return_value=topo(BONDED)), \
             mock.patch.object(sonos, "soap", return_value=""), \
             mock.patch.object(flip_mod, "SETTLE_POLL_SEC", 0.002):
            with self.assertRaisesRegex(sonos.SonosError, r"left speaker didn't come back.*timing.settle_timeout_sec"):
                f.to_desk()

    def test_desk_restores_pair_name(self):
        """Already paired but Sonos named it "Desk L": /desk only renames, keeping icon and config."""
        attrs = ("<CurrentZoneName>Desk L</CurrentZoneName><CurrentIcon>x-rincon-roomicon:office</CurrentIcon>"
                 "<CurrentConfiguration>1</CurrentConfiguration><CurrentTargetRoomName></CurrentTargetRoomName>")
        f = self.make_flip(DESK)
        out, soap = self.run_switch(f, "desk", [DESK], soap_side_effect=[attrs, ""])
        self.assertTrue(out["changed"])
        self.assertEqual(soap.call_args_list[1].args[2:], (
            "SetZoneAttributes",
            "<DesiredZoneName>Desk</DesiredZoneName><DesiredIcon>x-rincon-roomicon:office</DesiredIcon>"
            "<DesiredConfiguration>1</DesiredConfiguration><DesiredTargetRoomName></DesiredTargetRoomName>"))

    def test_desk_noop_when_named(self):
        f = self.make_flip(DESK)
        out, soap = self.run_switch(f, "desk", [DESK])
        self.assertFalse(out["changed"])
        self.assertEqual(soap.call_count, 1)

    # -- line-in and volume --

    def test_desk_starts_line_in_at_configured_volume(self):
        """Line-in plays first, then the pair's volume (23) is saved and the pair set to line_in.volume."""
        out, soap = self.desk_switch("right", line_in_volume=90)
        calls = [c.args[:4] for c in soap.call_args_list if c.args[1] in ("AVTransport", "RenderingControl")]
        self.assertEqual(calls, [
            (IP[L], "AVTransport", "SetAVTransportURI",
             f"<CurrentURI>x-rincon-stream:{R}</CurrentURI><CurrentURIMetaData></CurrentURIMetaData>"),
            (IP[L], "AVTransport", "Play", "<Speed>1</Speed>"),
            (IP[L], "RenderingControl", "GetVolume", "<Channel>Master</Channel>"),
            (IP[L], "RenderingControl", "SetVolume", "<Channel>Master</Channel><DesiredVolume>90</DesiredVolume>"),
        ])
        self.assertEqual((out["line_in"], out["volume"], out["mode"]), ("playing", 90, "desk"))
        self.assertEqual(self.saved()["saved_volume"], 23)

    def test_desk_keeps_unrestored_saved_volume(self):
        """A saved volume not set back yet (e.g. that failed) isn't overwritten by the current one."""
        state.update(self.state_file, saved_volume=40)
        out, soap = self.desk_switch("right")
        self.assertFalse([c for c in soap.call_args_list if c.args[2] == "GetVolume"])
        self.assertEqual(out["volume"], 100)
        self.assertEqual(self.saved()["saved_volume"], 40)

    def test_desk_volume_not_raised_if_it_cannot_be_saved(self):
        out, soap = self.desk_switch("right", state_file="/nonexistent-dir/state.json")
        self.assertFalse([c for c in soap.call_args_list if c.args[2] == "SetVolume"])
        self.assertEqual(out["line_in"], "playing")
        self.assertTrue(out["volume"].startswith("failed"))

    def test_desk_line_in_off_leaves_source(self):
        out, soap = self.desk_switch("off")
        self.assertFalse([c for c in soap.call_args_list if c.args[1] in ("AVTransport", "RenderingControl")])
        self.assertNotIn("line_in", out)

    def test_desk_line_in_failure_does_not_fail_switch(self):
        def fail_av(ip, service, action, *a, **k):
            if service == "AVTransport":
                raise sonos.UPnPError(ip, action, 714)
            return "<CurrentZoneName>Desk</CurrentZoneName>"
        out, soap = self.desk_switch("left", soap_side_effect=fail_av)
        self.assertTrue(out["changed"])
        self.assertTrue(out["line_in"].startswith("failed"))
        self.assertFalse([c for c in soap.call_args_list if c.args[1] == "RenderingControl"])

    def test_theater_restores_volume_before_split(self):
        state.update(self.state_file, saved_volume=23)
        out, soap = self.theater_switch()
        self.assertEqual([c.args[2] for c in soap.call_args_list if not c.args[2].startswith("Get")],
                         ["SetVolume", "SeparateStereoPair", "AddHTSatellite", "AddHTSatellite"])
        self.assertEqual(soap.call_args_list[0].args[:4], (IP[L], "RenderingControl", "SetVolume",
                                                           "<Channel>Master</Channel><DesiredVolume>23</DesiredVolume>"))
        self.assertEqual(out["volume"], "restored 23")
        self.assertNotIn("saved_volume", self.saved())

    def test_theater_without_saved_volume_leaves_it(self):
        out, soap = self.theater_switch()
        self.assertFalse([c for c in soap.call_args_list if c.args[2] == "SetVolume"])
        self.assertNotIn("volume", out)

    def test_theater_restore_failure_keeps_value_and_switches(self):
        state.update(self.state_file, saved_volume=23)

        def fail_rc(ip, service, action, *a, **k):
            if service == "RenderingControl":
                raise sonos.UPnPError(ip, action, 402)
            return ""
        out, _ = self.theater_switch(soap_side_effect=fail_rc)
        self.assertEqual(out["mode"], "theater")
        self.assertTrue(out["volume"].startswith("restore failed"))
        self.assertEqual(self.saved()["saved_volume"], 23)

    # -- EQ --

    def test_theater_saves_eq_before_split(self):
        out, soap = self.theater_switch()
        actions = [c.args[2] for c in soap.call_args_list]
        self.assertLess(actions.index("GetBass"), actions.index("SeparateStereoPair"))
        self.assertEqual({c.args[0] for c in soap.call_args_list if c.args[2] == "GetBass"}, {IP[L], IP[R]})
        self.assertEqual(self.saved()["saved_eq"], {L: EQ_READ, R: EQ_READ})
        self.assertEqual(out["eq"], "saved")

    def test_desk_restores_eq_after_pairing(self):
        state.update(self.state_file, saved_eq={L: EQ, R: {**EQ, "bass": 4, "loudness": False}})
        out, soap = self.desk_switch()
        actions = [c.args[2] for c in soap.call_args_list]
        self.assertLess(actions.index("CreateStereoPair"), actions.index("SetBass"))
        sets = [c.args[:4] for c in soap.call_args_list if c.args[1] == "RenderingControl"]
        self.assertEqual(sets, [
            (IP[L], "RenderingControl", "SetBass", "<DesiredBass>-3</DesiredBass>"),
            (IP[L], "RenderingControl", "SetTreble", "<DesiredTreble>2</DesiredTreble>"),
            (IP[L], "RenderingControl", "SetLoudness", "<Channel>Master</Channel><DesiredLoudness>1</DesiredLoudness>"),
            (IP[L], "RenderingControl", "SetVolume", "<Channel>LF</Channel><DesiredVolume>100</DesiredVolume>"),
            (IP[L], "RenderingControl", "SetVolume", "<Channel>RF</Channel><DesiredVolume>90</DesiredVolume>"),
            (IP[R], "RenderingControl", "SetBass", "<DesiredBass>4</DesiredBass>"),
            (IP[R], "RenderingControl", "SetTreble", "<DesiredTreble>2</DesiredTreble>"),
            (IP[R], "RenderingControl", "SetLoudness", "<Channel>Master</Channel><DesiredLoudness>0</DesiredLoudness>"),
            (IP[R], "RenderingControl", "SetVolume", "<Channel>LF</Channel><DesiredVolume>100</DesiredVolume>"),
            (IP[R], "RenderingControl", "SetVolume", "<Channel>RF</Channel><DesiredVolume>90</DesiredVolume>"),
        ])
        self.assertEqual((out["eq"], out["mode"]), ("restored", "desk"))

    def test_desk_eq_restore_retries_while_not_settled(self):
        state.update(self.state_file, saved_eq={L: EQ, R: EQ})
        n = []

        def flaky(ip, service, action, *a, **k):
            if action == "SetBass" and len(n) < 2:
                n.append(1)
                raise sonos.UPnPError(ip, action, 800)
            return "<CurrentZoneName>Desk</CurrentZoneName>"
        out, soap = self.desk_switch(soap_side_effect=flaky)
        self.assertEqual([c.args[2] for c in soap.call_args_list].count("SetBass"), 4)
        self.assertEqual(out["eq"], "restored")

    def test_desk_eq_restore_failure_does_not_fail_switch(self):
        state.update(self.state_file, saved_eq={L: EQ, R: EQ})

        def fail_rc(ip, service, action, *a, **k):
            if service == "RenderingControl":
                raise sonos.UPnPError(ip, action, 402)
            return "<CurrentZoneName>Desk</CurrentZoneName>"
        out, _ = self.desk_switch(soap_side_effect=fail_rc)
        self.assertEqual(out["mode"], "desk")
        self.assertTrue(out["eq"].startswith("restore failed"))

    def test_theater_eq_save_failure_keeps_earlier_and_switches(self):
        earlier = {L: {**EQ, "bass": 5}, R: EQ}
        state.update(self.state_file, saved_eq=earlier)
        out, _ = self.theater_switch(soap_side_effect=lambda ip, svc, action, *a, **k:
                                     "<CurrentBass>99</CurrentBass>" if action == "GetBass" else EQ_REPLY)
        self.assertEqual(out["mode"], "theater")
        self.assertTrue(out["eq"].startswith("save failed"))
        self.assertEqual(self.saved()["saved_eq"], earlier)

    def test_already_desk_leaves_eq(self):
        state.update(self.state_file, saved_eq={L: EQ, R: EQ})
        f = self.make_flip(DESK)
        out, soap = self.run_switch(f, "desk", [DESK])
        self.assertFalse([c for c in soap.call_args_list if c.args[1] == "RenderingControl"])
        self.assertNotIn("eq", out)

    def test_invalid_saved_eq_is_ignored(self):
        state.update(self.state_file, saved_eq={L: EQ, R: {**EQ, "bass": 11}, "x": "nope"})
        self.assertEqual(state.saved_eq(self.state_file), {L: EQ})
        state.update(self.state_file, saved_eq=[1, 2])
        self.assertEqual(state.saved_eq(self.state_file), {})

    def test_corrupt_state_file_is_ignored(self):
        with open(self.state_file, "w") as f:
            f.write("{not json")
        self.assertIsNone(state.saved_volume(self.state_file))
        state.update(self.state_file, saved_volume=300)
        self.assertIsNone(state.saved_volume(self.state_file))


# ------------------------------------------------------------------ watcher ----

TV = {"transport_state": "PLAYING", "current_uri": "x-sonos-htastream:RINCON_X:spdif"}
OFF = {"transport_state": "STOPPED", "current_uri": "x-rincon-queue:RINCON_X#0"}


class Watcher(unittest.TestCase):
    def run_watcher(self, polls, requests_at=(), **cfg):
        """Drive watcher.watch with one fake minute per poll. polls: list of 'tv'/'off'/'err'.
        requests_at: poll indexes at which a /desk or /theater request arrives."""
        clock = [1000.0]
        c = config.Config(soundbar="x", left="l", right="r", pair_name="p", **cfg)
        flip = mock.Mock(last_request=float("-inf"), cfg=c)
        flip.soundbar_transport.side_effect = [
            sonos.SonosError("no answer") if p == "err" else TV if p == "tv" else OFF for p in polls]

        def wait(_):
            n = flip.soundbar_transport.call_count
            if n in requests_at:
                flip.last_request = clock[0]
            clock[0] += 60
            return n >= len(polls)

        with mock.patch.object(watcher.time, "monotonic", side_effect=lambda: clock[0]):
            watcher.watch(flip, wait=wait)
        return flip

    def test_edges(self):
        """TV already playing at startup is a baseline, not an edge; only off->on switches, once."""
        flip = self.run_watcher(["tv", "tv", "off", "tv", "tv", "off"], desk_after_tv_min=None)
        self.assertEqual(flip.to_theater.call_count, 1)

    def test_theater_on_tv_off(self):
        flip = self.run_watcher(["off", "tv", "off"], theater_on_tv=False, desk_after_tv_min=None)
        flip.to_theater.assert_not_called()

    def test_desk_after_10_min(self):
        flip = self.run_watcher(["tv", "tv"] + ["off"] * 12, desk_after_tv_min=10.0)
        self.assertEqual(flip.to_desk.call_count, 1)

    def test_desk_cancelled_when_tv_resumes(self):
        flip = self.run_watcher(["tv"] + ["off"] * 5 + ["tv"] * 10, desk_after_tv_min=10.0)
        flip.to_desk.assert_not_called()
        self.assertEqual(flip.to_theater.call_count, 1)

    def test_desk_skipped_after_manual_request(self):
        flip = self.run_watcher(["tv"] + ["off"] * 12, requests_at={4}, desk_after_tv_min=10.0)
        flip.to_desk.assert_not_called()

    def test_no_countdown_when_off_at_startup(self):
        flip = self.run_watcher(["off"] * 15, desk_after_tv_min=10.0)
        flip.to_desk.assert_not_called()

    def test_desk_immediately_when_zero(self):
        flip = self.run_watcher(["tv", "tv", "off", "off"], desk_after_tv_min=0.0)
        self.assertEqual(flip.to_desk.call_count, 1)

    def test_desk_never_when_off(self):
        flip = self.run_watcher(["tv"] + ["off"] * 20, desk_after_tv_min=None)
        flip.to_desk.assert_not_called()

    def test_poll_errors_dont_stop_or_trigger(self):
        """An outage in the middle is neither a TV stop nor a TV start."""
        flip = self.run_watcher(["tv", "err", "err", "tv", "off"], desk_after_tv_min=0.0)
        flip.to_theater.assert_not_called()
        self.assertEqual(flip.to_desk.call_count, 1)

    def test_failed_switch_doesnt_stop_watching(self):
        c = dict(desk_after_tv_min=None)
        clock = [0.0]
        cfg = config.Config(soundbar="x", left="l", right="r", pair_name="p", **c)
        flip = mock.Mock(last_request=float("-inf"), cfg=cfg)
        flip.soundbar_transport.side_effect = [OFF, TV, OFF, TV]
        flip.to_theater.side_effect = sonos.SonosError("boom")
        with mock.patch.object(watcher.time, "monotonic", side_effect=lambda: clock[0]):
            watcher.watch(flip, wait=lambda _: flip.soundbar_transport.call_count >= 4)
        self.assertEqual(flip.to_theater.call_count, 2)


# --------------------------------------------------------------------- config ----

def read_example():
    with open(os.path.join(HERE, "config.example.toml")) as f:
        return f.read()


def example_with_speakers(text=None):
    text = text or read_example()
    for key, value in (("soundbar", "Living Room"), ("left", "Desk L"), ("right", "Desk R"), ("pair_name", "Desk")):
        text = text.replace(f'{key} = ""', f'{key} = "{value}"', 1)
    return text


class ConfigFile(TempDir):
    def load(self, text):
        path = os.path.join(self.dir, "config.toml")
        with open(path, "w") as f:
            f.write(text)
        return config.load(path)

    def test_example_with_speakers_filled_in_equals_defaults(self):
        cfg = self.load(example_with_speakers())
        defaults = config.Config(soundbar="Living Room", left="Desk L", right="Desk R", pair_name="Desk",
                                 state_file=os.path.join(self.dir, "state.json"))
        self.assertEqual(cfg, defaults)

    def test_example_as_shipped_asks_for_speakers(self):
        with self.assertRaisesRegex(config.ConfigError, "speakers.soundbar must not be empty"):
            self.load(read_example())

    def test_values(self):
        cfg = self.load(example_with_speakers(
            '[speakers]\nsoundbar = ""\nleft = ""\nright = ""\npair_name = ""\n'
            '[automation]\ndesk_after_tv_min = "off"\npoll_sec = 2\n[line_in]\nspeaker = "Right"\nvolume = 90\n'))
        self.assertIsNone(cfg.desk_after_tv_min)
        self.assertEqual((cfg.poll_sec, cfg.line_in, cfg.line_in_volume), (2.0, "right", 90))

    def test_errors(self):
        sp = '[speakers]\nsoundbar = "s"\nleft = "l"\nright = "r"\npair_name = "p"\n'
        for text, msg in [
            ('[speakers]\nleft = "l"\nright = "r"\npair_name = "p"\n', "speakers.soundbar is required"),
            (sp + '[line_in]\nvolum = 90\n', "unknown setting(s) in [line_in]: volum"),
            (sp + '[lin_in]\n', "unknown setting 'lin_in'"),
            ('port = 8080\n' + sp, "unknown setting 'port' (settings belong under a [table] heading)"),
            (sp + '[http]\nport = "8099"\n', "http.port must be a whole number"),
            (sp + '[http]\nport = 70000\n', "http.port must be between 1 and 65535"),
            (sp + '[automation]\ntheater_on_tv = 1\n', "automation.theater_on_tv must be true or false"),
            (sp + '[automation]\npoll_sec = 0\n', "automation.poll_sec must be > 0"),
            (sp + '[automation]\ndesk_after_tv_min = -1\n', 'desk_after_tv_min must be a number of minutes >= 0 or "off"'),
            (sp + '[line_in]\nspeaker = "middle"\n', "line_in.speaker must be one of off, left, right"),
            (sp + '[line_in]\nvolume = 101\n', "line_in.volume must be between 0 and 100"),
            (sp.replace('right = "r"', 'right = "L"'), "must be different speakers"),
            ('log_level = "loud"\n' + sp, "log_level must be one of"),
            (sp + 'port = \n', "not valid TOML"),
        ]:
            with self.subTest(msg=msg), self.assertRaises(config.ConfigError) as e:
                self.load(text)
            self.assertIn(msg, str(e.exception))

    def test_missing_file(self):
        with self.assertRaisesRegex(config.ConfigError, "not found"):
            config.load(os.path.join(self.dir, "nope.toml"))


# ----------------------------------------------------------------- HTTP API ----

class Api(unittest.TestCase):
    def setUp(self):
        self.flip = mock.Mock(ready=True, startup_error=None, last_request=float("-inf"))
        self.flip.state.return_value = {"mode": "desk"}
        srv = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(self.flip))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.base = f"http://127.0.0.1:{srv.server_port}"

    def call(self, method, path):
        req = urllib.request.Request(self.base + path, method=method, data=b"" if method == "POST" else None)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_status(self):
        self.assertEqual(self.call("GET", "/status"), (200, {"mode": "desk"}))

    def test_health(self):
        self.assertEqual(self.call("GET", "/health")[0], 200)
        self.flip.ready, self.flip.startup_error = False, "no room named X"
        code, body = self.call("GET", "/health")
        self.assertEqual((code, body["error"]), (503, "no room named X"))

    def test_not_ready(self):
        self.flip.ready, self.flip.startup_error = False, "no room named X"
        self.assertEqual(self.call("POST", "/desk"), (503, {"error": "not ready: no room named X"}))
        self.flip.to_desk.assert_not_called()

    def test_post_switches_and_marks_request(self):
        self.flip.to_theater.return_value = {"changed": True}
        self.assertEqual(self.call("POST", "/theater/"), (200, {"changed": True}))
        self.assertGreater(self.flip.last_request, 0)

    def test_errors(self):
        self.flip.to_desk.side_effect = sonos.SonosError("speaker at 10.0.0.2 didn't answer")
        self.assertEqual(self.call("POST", "/desk"), (502, {"error": "speaker at 10.0.0.2 didn't answer"}))
        self.flip.state.side_effect = KeyError("bug")
        self.assertEqual(self.call("GET", "/status")[0], 500)
        self.assertEqual(self.call("GET", "/desk"), (405, {"error": "use POST /desk"}))
        self.assertEqual(self.call("GET", "/nope")[0], 404)


# --------------------------------------------------------------------- startup ----

class Startup(TempDir):
    def run_main(self, path):
        with mock.patch.dict(os.environ, {"CONFIG_FILE": path}):
            return main_mod.main([])

    def test_first_run_writes_template(self):
        path = os.path.join(self.dir, "sub", "config.toml")
        self.assertEqual(self.run_main(path), main_mod.EX_CONFIG)
        with open(path) as f:
            self.assertEqual(f.read(), read_example())

    def test_bad_config_exits(self):
        path = os.path.join(self.dir, "config.toml")
        with open(path, "w") as f:
            f.write("[speakers]\n")
        self.assertEqual(self.run_main(path), main_mod.EX_CONFIG)

    def test_port_in_use_exits(self):
        busy = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(mock.Mock()))
        self.addCleanup(busy.server_close)
        path = os.path.join(self.dir, "config.toml")
        with open(path, "w") as f:
            f.write(example_with_speakers().replace("port = 8080", f"port = {busy.server_port}")
                    .replace('bind = "0.0.0.0"', 'bind = "127.0.0.1"'))
        self.assertEqual(self.run_main(path), 1)


if __name__ == "__main__":
    unittest.main()
