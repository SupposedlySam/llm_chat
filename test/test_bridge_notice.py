"""A room named for a person is not a room that reaches one.

wcs reported this after their human lost a message and only noticed because he
happened to mention it. An agent said something into `#wcs_human`, `say`
reported success, and nothing was carrying it:

    sent #5 to wcs_human as wcs  (2287 chars)
      stored: *Detroit buyer leads — verify before I build further*
      LEFT FOR supposedlysam — no live session, so nobody was woken

That last line is printed whether or not a bridge exists. For a BRIDGED room
it is true and harmless — the human is asleep and will read it in Slack. For
an unbridged one it is the sentence that makes a reader believe a message is
queued for a person. wcs read it that way, told their human it was in Slack,
and it was not.

`llms.txt` documents the convention — "Some rooms are bridged to a human's
Slack ... and are named for the person" — so the naming was load-bearing in
the documentation and carried no weight at all in the code. Before this,
`bin/llm_chat` contained exactly one mention of Slack, in `--thread` help
text: the CLI could not have warned, because it knew nothing about the bridge.
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import load  # noqa: E402

cli = load("llm_chat")
bridge = load("bin/llm-chat-slack")


class BridgeVerdictTest(unittest.TestCase):
    """Five states, and the ones that must not be merged."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.real_config, self.real_alive = cli.BRIDGE_CONFIG, cli.BRIDGE_ALIVE
        cli.BRIDGE_CONFIG = os.path.join(self.tmp.name, "slack.json")
        cli.BRIDGE_ALIVE = os.path.join(self.tmp.name, "slack-alive.json")

    def tearDown(self):
        cli.BRIDGE_CONFIG, cli.BRIDGE_ALIVE = self.real_config, self.real_alive
        self.tmp.cleanup()

    def configure(self, room):
        with open(cli.BRIDGE_CONFIG, "w") as f:
            json.dump({"room": room, "identity": "someone"}, f)

    def beating(self, room, ago_ms=0):
        with open(cli.BRIDGE_ALIVE, "w") as f:
            json.dump({"room": room, "at": cli.now_ms() - ago_ms}, f)

    def test_an_ORDINARY_room_is_not_asked_about(self):
        """The question does not apply. A bridge note on every room would be
        noise on the rooms nobody expects to leave the machine."""
        self.assertIsNone(cli.bridge_for("learnings"))
        self.assertIsNone(cli.bridge_for("deploy-review"))

    def test_NO_CONFIG_AT_ALL(self):
        self.assertEqual(cli.bridge_for("wcs_human"), ("none", None))

    def test_a_config_for_ANOTHER_ROOM_does_not_serve_this_one(self):
        """The live state on the reporter's machine, and the sharpest form of
        the bug: a bridge exists, so everything looks configured, and it is
        pointed somewhere else."""
        self.configure("wcs_human")
        self.assertEqual(cli.bridge_for("supposedlysam_human"),
                         ("other", "wcs_human"))

    def test_CONFIGURED_AND_BEATING(self):
        self.configure("wcs_human")
        self.beating("wcs_human")
        state, age = cli.bridge_for("wcs_human")
        self.assertEqual(state, "live")
        self.assertLess(age, cli.BRIDGE_FRESH_MS)

    def test_a_heartbeat_that_STOPPED_is_stale_not_live(self):
        self.configure("wcs_human")
        self.beating("wcs_human", ago_ms=cli.BRIDGE_FRESH_MS + 60000)
        self.assertEqual(cli.bridge_for("wcs_human")[0], "stale")

    def test_NO_RECORD_is_not_the_same_as_STOPPED(self):
        """The distinction this repo already draws for hook probes: the mark
        exists only from its own start, so a bridge running from a build older
        than the heartbeat writes nothing and is working perfectly. Calling
        that 'stopped' would tell a working setup it is broken."""
        self.configure("wcs_human")
        self.assertEqual(cli.bridge_for("wcs_human"), ("norecord", None))

    def test_a_heartbeat_for_a_DIFFERENT_room_does_not_vouch(self):
        """One bridge process serves one room. Its heartbeat is evidence
        about that room and about nothing else."""
        self.configure("wcs_human")
        self.beating("someone_else_human")
        self.assertEqual(cli.bridge_for("wcs_human"), ("norecord", None))

    def test_a_config_with_NO_ROOM_KEY_is_not_configured(self):
        """Credentials without a room is a half-filled config — `--check`
        reports it as missing fields, and it bridges nothing. Reading it as
        'configured' would produce the reassuring answer from the emptiest
        possible evidence."""
        with open(cli.BRIDGE_CONFIG, "w") as f:
            json.dump({"identity": "someone"}, f)
        self.assertEqual(cli.bridge_for("wcs_human"), ("none", None))

    def test_a_CORRUPT_config_is_not_read_as_configured(self):
        with open(cli.BRIDGE_CONFIG, "w") as f:
            f.write("{not json")
        self.assertEqual(cli.bridge_for("wcs_human"), ("none", None))

    def test_a_heartbeat_with_NO_TIMESTAMP_is_no_record(self):
        """A file that exists proves a file exists. The verdict turns on the
        timestamp, so a malformed one must not read as a fresh beat."""
        self.configure("wcs_human")
        with open(cli.BRIDGE_ALIVE, "w") as f:
            json.dump({"room": "wcs_human"}, f)
        self.assertEqual(cli.bridge_for("wcs_human"), ("norecord", None))


class BridgeNoteTest(BridgeVerdictTest):
    """What `say` actually prints. The verdict is useless if the sentence
    beside it still reads as delivery."""

    def test_an_ordinary_room_gets_NO_LINE(self):
        self.assertEqual(cli.bridge_note("learnings"), "")

    def test_the_unconfigured_case_SAYS_NOTHING_LEAVES(self):
        note = cli.bridge_note("wcs_human")
        self.assertIn("NO SLACK BRIDGE IS CONFIGURED", note)
        self.assertIn("leaves llm_chat", note)

    def test_the_wrong_room_case_NAMES_THE_ROOM_IT_DOES_SERVE(self):
        """So the reader can tell a missing bridge from a misaimed one — the
        remedies are different and only one of them is 'start it'."""
        self.configure("wcs_human")
        self.assertIn("#wcs_human", cli.bridge_note("supposedlysam_human"))

    def test_a_LIVE_bridge_is_not_warned_about(self):
        """Paired, and the whole point. A line that fires for every human
        room teaches the reader to skip the line that matters."""
        self.configure("wcs_human")
        self.beating("wcs_human")
        self.assertEqual(cli.bridge_note("wcs_human"), "")

    def test_the_STALE_line_says_a_config_is_what_makes_it_look_delivered(self):
        self.configure("wcs_human")
        self.beating("wcs_human", ago_ms=3 * 60 * 60 * 1000)
        note = cli.bridge_note("wcs_human")
        self.assertIn("STOPPED CHECKING IN", note)
        self.assertIn("3h", note)

    def test_the_NO_RECORD_line_refuses_to_claim_absence(self):
        """It must not tell a working older bridge that it is down."""
        self.configure("wcs_human")
        note = cli.bridge_note("wcs_human")
        self.assertIn("NO RECORD", note)
        self.assertIn("not proof of absence", note)


class AgeWordingTest(unittest.TestCase):
    """Minutes, hours, days. A duration a person reads on a phone.

    Each unit is asserted because the wrong one is not a rounding error: "3"
    means something very different if a reader takes minutes for days while
    deciding whether their escalation reached anybody.
    """

    def test_minutes(self):
        self.assertEqual(cli.human_age(7 * 60 * 1000), "7m")

    def test_hours(self):
        self.assertEqual(cli.human_age(5 * 60 * 60 * 1000), "5h")

    def test_days_once_hours_stop_being_readable(self):
        self.assertEqual(cli.human_age(6 * 24 * 60 * 60 * 1000), "6d")

    def test_nothing_at_all_does_not_crash(self):
        """A malformed heartbeat reaches here as None, and a diagnostic that
        raises while reporting is worse than the state it reports."""
        self.assertEqual(cli.human_age(None), "0m")


class SayPrintsItTest(unittest.TestCase):
    """The verdict has to reach the sender, under the line it corrects."""

    def setUp(self):
        from support import FakeServer
        self.server = FakeServer()
        self.real_call, cli.call = cli.call, self.server.call
        self.real_live = cli.live_identities
        cli.live_identities = lambda: {"wcs": [{"sessionId": "s"}]}
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CLAUDE_PROJECT_DIR"] = self.tmp.name
        self.server.channel("wcs_human")
        for who in ("wcs", "supposedlysam"):
            self.server.membership("wcs_human", who)
        self.real_config = cli.BRIDGE_CONFIG
        cli.BRIDGE_CONFIG = os.path.join(self.tmp.name, "slack.json")

    def tearDown(self):
        cli.call = self.real_call
        cli.live_identities = self.real_live
        cli.BRIDGE_CONFIG = self.real_config
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.tmp.cleanup()

    def say(self):
        out = io.StringIO()
        with redirect_stdout(out):
            cli.do_say("srv", "wcs_human", "wcs", "the leads", None)
        return out.getvalue()

    def test_the_send_still_SUCCEEDS(self):
        """wcs asked for this explicitly and they are right: the message was
        stored correctly and readable, and a room used as a queue for a bridge
        somebody starts later is legitimate. What was missing is not
        permission, it is the fact."""
        self.assertIn("sent", self.say())

    def test_and_it_SAYS_nothing_leaves_llm_chat(self):
        text = self.say()
        self.assertIn("NO SLACK BRIDGE IS CONFIGURED", text)

    def test_the_warning_sits_with_the_line_it_corrects(self):
        """'LEFT FOR <name>' is what read as queued-for-a-human. The
        correction is worth little three screens away from it."""
        text = self.say().splitlines()
        reach = [i for i, line in enumerate(text) if "wcs_human" in line
                 or "LEFT FOR" in line or "wakes" in line]
        warn = [i for i, line in enumerate(text) if "NO SLACK BRIDGE" in line]
        self.assertTrue(warn, "no warning printed at all")
        self.assertLess(warn[0] - max(reach), 3,
                        "the correction drifted away from the claim")


class HeartbeatTest(unittest.TestCase):
    """The bridge's end of it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.real = bridge.ALIVE
        bridge.ALIVE = os.path.join(self.tmp.name, "state", "slack-alive.json")

    def tearDown(self):
        bridge.ALIVE = self.real
        self.tmp.cleanup()

    def test_it_records_the_room_and_a_time(self):
        bridge.beat("wcs_human")
        with open(bridge.ALIVE) as f:
            written = json.load(f)
        self.assertEqual(written["room"], "wcs_human")
        self.assertIsInstance(written["at"], int)

    def test_it_creates_the_directory(self):
        """The bridge can be the first thing to run in a fresh checkout."""
        bridge.beat("wcs_human")
        self.assertTrue(os.path.isfile(bridge.ALIVE))

    def test_a_beat_it_CANNOT_WRITE_does_not_stop_the_bridge(self):
        """The heartbeat is a diagnostic. Letting it take down the thing it
        describes would be a guard breaking its own subject — and the failure
        would be a bridge that stops relaying because it could not say it was
        relaying."""
        bridge.ALIVE = os.path.join(self.tmp.name, "nope", "\0", "x.json")
        bridge.beat("wcs_human")          # must not raise

    def test_the_CLI_and_the_BRIDGE_agree_on_the_path(self):
        """Two files, one filename, and nothing else checks they match. If
        they drift, the bridge beats where nobody reads and every room reports
        NO RECORD forever — green, silent, and wrong in the safe-looking
        direction."""
        self.assertEqual(os.path.basename(self.real),
                         os.path.basename(cli.BRIDGE_ALIVE))
        self.assertEqual(os.path.basename(bridge.CONFIG),
                         os.path.basename(cli.BRIDGE_CONFIG))


if __name__ == "__main__":
    unittest.main()
