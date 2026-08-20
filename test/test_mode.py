"""Converting a room between broadcast and ordinary, and telling people.

A mode is not a label — it decides whether a message interrupts you. So a
conversion changes OTHER agents' working conditions without their involvement,
in both directions and with no safe one:

    -> ordinary   a room everyone is in starts interrupting everyone
    -> broadcast  a live conversation stops waking anybody, silently

Both hazards are what the confirmation and the passive notice are for.
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import FakeServer, load  # noqa: E402

cli = load("llm_chat")


class ModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CLAUDE_PROJECT_DIR"] = self.tmp.name
        self.server = FakeServer()
        self.real = cli.call
        cli.call = self.server.call
        self.server.channel("room")
        for who in ("alice", "bob", "carol"):
            self.server.membership("room", who)

    def tearDown(self):
        cli.call = self.real
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.tmp.cleanup()

    def mode(self, mode, yes=True, name="room", identity="alice"):
        out = io.StringIO()
        with redirect_stdout(out):
            cli.do_mode("srv", name, identity, mode, yes)
        return out.getvalue()

    def last_posted(self):
        """The newest message row, or {} when nothing was posted at all.

        NOT `tables["messages"][-1]`, and that is #22 rather than style. The
        thing these tests measure is whether the room is TOLD, so the case
        worth catching is the one where nothing was said — and indexing
        straight into the table raised KeyError there, killing the test before
        its assertion ran. The sweep could then only report "crashed, not
        measured": four tests died and not one of them disagreed with
        anything, so nothing had ever established that this notice is
        defended."""
        return (self.server.tables.get("messages") or [{}])[-1]

    # ── the conversion ──────────────────────────────────────────────────────
    def test_ordinary_becomes_broadcast(self):
        self.mode("broadcast")
        self.assertTrue(self.server.get_channel("room")["broadcast"])

    def test_broadcast_becomes_ordinary_again(self):
        """Both directions. A one-way door would make the mode a decision you
        can only get wrong once."""
        self.mode("broadcast")
        self.mode("ordinary")
        self.assertFalse(self.server.get_channel("room")["broadcast"])

    def test_the_wake_default_actually_follows_the_mode(self):
        """The only property that matters. Flipping a flag that nothing reads
        would pass every other test in this file."""
        chan = self.server.get_channel("room")
        self.assertTrue(cli.wakes({"audience": None}, "bob", chan))
        self.mode("broadcast")
        self.assertFalse(cli.wakes({"audience": None}, "bob",
                                   self.server.get_channel("room")))

    def test_converting_to_the_mode_it_already_has_is_a_no_op(self):
        out = self.mode("ordinary")
        self.assertIn("already ordinary", out)
        self.assertEqual(len(self.server.tables.get("messages", [])), 0)

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(SystemExit) as caught:
            self.mode("loud")
        self.assertIn("must be one of", str(caught.exception))

    def test_a_missing_room_is_refused(self):
        with self.assertRaises(SystemExit) as caught:
            self.mode("broadcast", name="nowhere")
        self.assertIn("no such channel", str(caught.exception))

    # ── the confirmation ────────────────────────────────────────────────────
    def test_it_refuses_without_yes(self):
        with self.assertRaises(SystemExit):
            self.mode("broadcast", yes=False)
        self.assertFalse(self.server.get_channel("room")["broadcast"])

    def test_the_refusal_says_how_many_agents_it_would_affect(self):
        with self.assertRaises(SystemExit) as caught:
            self.mode("broadcast", yes=False)
        self.assertIn("3 agent(s)", str(caught.exception))

    def test_the_refusal_names_the_harm_of_going_quiet(self):
        """Direction-specific, because the two hazards are opposites and a
        generic warning teaches nothing."""
        with self.assertRaises(SystemExit) as caught:
            self.mode("broadcast", yes=False)
        self.assertIn("stall", str(caught.exception))

    def test_the_refusal_names_the_harm_of_going_loud(self):
        self.mode("broadcast")
        with self.assertRaises(SystemExit) as caught:
            self.mode("ordinary", yes=False)
        self.assertIn("waking all", str(caught.exception))

    # ── telling the room ────────────────────────────────────────────────────
    def test_the_members_are_told(self):
        """A room whose wake behaviour changed under them and did not say so is
        a trap."""
        self.mode("broadcast")
        posted = self.last_posted()
        self.assertIn("BROADCAST", posted.get("text", ""))

    def test_the_notice_wakes_nobody(self):
        """It costs every member a turn otherwise — for an announcement about
        reducing how often they are interrupted."""
        self.mode("broadcast")
        self.assertEqual(self.last_posted().get("audience"),
                         cli.AUDIENCE_NONE)

    def test_the_notice_says_how_to_reverse_it(self):
        """So an agent that disagrees can act without reading the docs, which
        is the point of telling them at all."""
        self.mode("broadcast")
        self.assertIn("mode room ordinary --yes",
                      self.last_posted().get("text", ""))

    def test_the_notice_for_the_other_direction_names_the_other_reversal(self):
        self.mode("broadcast")
        self.mode("ordinary")
        self.assertIn("mode room broadcast --yes",
                      self.last_posted().get("text", ""))

    def test_going_broadcast_says_auto_join_is_not_retroactive(self):
        """Both hooks poll from LOCAL state, so a project that already
        identified is not reachable until it syncs. Claiming 'everyone is now
        in it' would be the half-truth that makes this feature untrustworthy."""
        self.assertIn("next time they do", self.mode("broadcast"))


class HintTest(unittest.TestCase):
    """The nudge that lets an agent decide for itself, told to the one agent
    positioned to act on it, once."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CLAUDE_PROJECT_DIR"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.tmp.cleanup()

    def test_three_or_more_members_gets_the_hint(self):
        hint = cli.crowded_room_hint("room", ["a", "b", "c"], None)
        self.assertIn("3 agents", hint)

    def test_two_members_do_not(self):
        """In a two-agent room, waking the other one IS the feature."""
        self.assertEqual(cli.crowded_room_hint("room", ["a", "b"], None), "")

    def test_an_addressed_message_does_not(self):
        """They already did the thing the hint would suggest."""
        self.assertEqual(
            cli.crowded_room_hint("room", ["a", "b", "c"], "b"), "")
        self.assertEqual(
            cli.crowded_room_hint("room", ["a", "b", "c"], cli.AUDIENCE_NONE),
            "")

    def test_it_fires_once_per_room(self):
        """A hint on every message is standing noise, which this project has
        already had to harden against once."""
        self.assertNotEqual(cli.crowded_room_hint("room", ["a", "b", "c"], None), "")
        self.assertEqual(cli.crowded_room_hint("room", ["a", "b", "c"], None), "")

    def test_a_different_room_gets_its_own_hint(self):
        cli.crowded_room_hint("room", ["a", "b", "c"], None)
        self.assertNotEqual(
            cli.crowded_room_hint("other", ["a", "b", "c"], None), "")

    def test_it_offers_both_the_narrow_and_the_convert(self):
        hint = cli.crowded_room_hint("room", ["a", "b", "c"], None)
        self.assertIn("--to", hint)
        self.assertIn("mode room broadcast", hint)

    def test_an_unwritable_marker_suppresses_rather_than_repeats(self):
        """If it cannot record that it fired, repeating forever is the worse
        failure — it would nag on every single message."""
        os.environ["CLAUDE_PROJECT_DIR"] = os.path.join(self.tmp.name, "gone")
        open(os.path.join(self.tmp.name, "gone"), "w").close()
        self.assertEqual(
            cli.crowded_room_hint("room", ["a", "b", "c"], None), "")


class SayHintTest(unittest.TestCase):
    """The hint reaching the sender through `say`, which is where it matters."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CLAUDE_PROJECT_DIR"] = self.tmp.name
        self.server = FakeServer()
        self.real = cli.call
        cli.call = self.server.call
        self.server.channel("room")
        for who in ("alice", "bob", "carol"):
            self.server.membership("room", who)

    def tearDown(self):
        cli.call = self.real
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.tmp.cleanup()

    def say(self, audience=None, name="room"):
        out = io.StringIO()
        with redirect_stdout(out):
            cli.do_say("srv", name, "alice", "hi", audience)
        return out.getvalue()

    def test_a_crowded_room_hints_on_the_first_message(self):
        self.assertIn("3 agents", self.say())

    def test_and_not_on_the_second(self):
        self.say()
        self.assertNotIn("3 agents", self.say())

    def test_an_addressed_message_never_hints(self):
        self.assertNotIn("3 agents", self.say(audience="bob"))

    def test_a_broadcast_room_never_hints(self):
        """It already wakes nobody — suggesting they quieten it is noise, and
        suggesting `mode broadcast` on a broadcast room is nonsense."""
        self.server.channel("notices", broadcast=1)
        for who in ("alice", "bob", "carol"):
            self.server.membership("notices", who)
        self.assertNotIn("agents are in", self.say(name="notices"))


class DispatchTest(unittest.TestCase):
    """Both verbs reachable as commands. Wired into argparse but not into
    dispatch, `mode` would exit 0 having changed nothing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CLAUDE_PROJECT_DIR"] = self.tmp.name
        os.makedirs(os.path.join(self.tmp.name, ".llm_chat"))
        self.server = FakeServer()
        self.real, self.argv = cli.call, sys.argv
        cli.call = self.server.call
        self.server.channel("room")
        self.server.membership("room", "alice")

    def tearDown(self):
        cli.call, sys.argv = self.real, self.argv
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.tmp.cleanup()

    def run_argv(self, *argv):
        sys.argv = ["llm_chat"] + list(argv)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(), 0)
        return out.getvalue()

    def test_mode_reaches_do_mode(self):
        self.run_argv("mode", "room", "broadcast", "--as", "alice", "--yes")
        self.assertTrue(self.server.get_channel("room")["broadcast"])

    def test_sync_reaches_do_sync(self):
        with open(os.path.join(self.tmp.name, ".llm_chat", "identity.json"),
                  "w") as f:
            json.dump({"identity": "me"}, f)
        self.server.channel("notices", broadcast=1)
        self.assertIn("notices", self.run_argv("sync"))


class SyncTest(unittest.TestCase):
    """Picking up a room that BECAME broadcast after you identified."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CLAUDE_PROJECT_DIR"] = self.tmp.name
        os.makedirs(os.path.join(self.tmp.name, ".llm_chat"))
        self.server = FakeServer()
        self.real = cli.call
        cli.call = self.server.call

    def tearDown(self):
        cli.call = self.real
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.tmp.cleanup()

    def identify_as(self, name):
        with open(os.path.join(self.tmp.name, ".llm_chat", "identity.json"),
                  "w") as f:
            json.dump({"identity": name}, f)

    def sync(self):
        out = io.StringIO()
        with redirect_stdout(out):
            cli.do_sync("srv")
        return out.getvalue()

    def test_it_joins_a_room_that_became_broadcast(self):
        """The whole reason this exists: `identify` reconciles once, and
        broadcast is no longer a property fixed at creation."""
        self.identify_as("me")
        self.server.channel("notices", broadcast=1)
        self.assertIn("notices", self.sync())
        self.assertIsNotNone(self.server.get_membership("notices", "me"))

    def test_it_writes_local_state_not_just_server_membership(self):
        """Both hooks poll from joined.json, so a server-side membership the
        project has never heard of is invisible to delivery."""
        self.identify_as("me")
        self.server.channel("notices", broadcast=1)
        self.sync()
        # Asked through the API rather than at a hardcoded path: state is
        # session-scoped now, so where it lands is an implementation detail
        # and the claim being made is "the hooks can see it".
        self.assertIn("notices", cli.read_joined())

    def test_it_is_quiet_when_there_is_nothing_to_do(self):
        """The waker runs this every turn boundary."""
        self.identify_as("me")
        self.assertEqual(self.sync(), "")

    def test_a_project_that_never_identified_is_an_opt_out_not_an_error(self):
        self.server.channel("notices", broadcast=1)
        self.assertEqual(self.sync(), "")

    def test_it_ignores_ordinary_rooms(self):
        """Auto-join is the broadcast bargain. Doing it for a conversation
        would put every agent in every room."""
        self.identify_as("me")
        self.server.channel("chat")
        self.sync()
        self.assertIsNone(self.server.get_membership("chat", "me"))

    def test_it_ignores_a_closed_broadcast_room(self):
        self.identify_as("me")
        self.server.channel("old", broadcast=1, closed=1)
        self.assertEqual(self.sync(), "")


if __name__ == "__main__":
    unittest.main()
