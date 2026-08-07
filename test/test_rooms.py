"""Joining, leaving, reopening — and refusing to lie about any of them.

The join tests exist because all three verbs used to report success into a
closed room and only fail later at the first `say`, so an agent believed it was
connected, spoke into an error, and had nothing connecting the two events.
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


class RoomTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeServer()
        self._real_call = cli.call
        cli.call = self.fake.call
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name
        os.environ["CLAUDE_PROJECT_DIR"] = self.project

    def tearDown(self):
        cli.call = self._real_call
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.tmp.cleanup()

    def quiet(self, fn, *a, **kw):
        out = io.StringIO()
        with redirect_stdout(out):
            result = fn(*a, **kw)
        return result, out.getvalue()

    # ── joining ─────────────────────────────────────────────────────────────
    def test_join_refuses_a_closed_room_instead_of_reporting_success(self):
        self.fake.channel("dead", closed=1, closed_reason="every member is done")
        with self.assertRaises(SystemExit) as caught:
            cli.do_join("http://127.0.0.1:1", "dead", "me", None, 200, announce=False)
        message = str(caught.exception)
        self.assertIn("closed", message)
        self.assertIn("reopen dead", message,
                      "a refusal has to name the way out, or it just blocks")

    def test_join_creates_the_room_when_it_does_not_exist(self):
        self.quiet(cli.do_join, "http://127.0.0.1:1", "fresh", "me", "a topic", 200, False)
        chan = self.fake.get_channel("fresh")
        self.assertIsNotNone(chan)
        self.assertEqual(chan["topic"], "a topic")
        self.assertEqual(chan["created_by"], "me")

    def test_joining_starts_you_at_the_current_end_not_at_message_zero(self):
        """Entering a room must not dump its backlog into your context."""
        self.fake.channel("busy", message_count=7)
        self.quiet(cli.do_join, "http://127.0.0.1:1", "busy", "me", None, 200, False)
        self.assertEqual(self.fake.get_membership("busy", "me")["seen_seq"], 7)

    def test_rejoining_preserves_your_cursor_and_clears_done(self):
        """Come back as the same identity and the gap is still waiting for you.
        A new identity would start at the end and silently miss it."""
        self.fake.channel("room", message_count=9)
        self.fake.membership("room", "me", seen_seq=3, done=1)
        self.quiet(cli.do_join, "http://127.0.0.1:1", "room", "me", None, 200, False)
        member = self.fake.get_membership("room", "me")
        self.assertEqual(member["seen_seq"], 3, "cursor must survive rejoining")
        self.assertEqual(member["done"], 0)

    def test_names_and_identities_are_validated(self):
        for channel, identity in (("has space", "me"), ("room", "has space"),
                                  ("", "me"), ("room", "")):
            with self.assertRaises(SystemExit):
                cli.do_join("http://127.0.0.1:1", channel, identity, None, 200, False)

    # ── leaving ─────────────────────────────────────────────────────────────
    def test_leave_forgets_the_room_locally(self):
        """joined.json only ever grew, and both hooks poll every listed room on
        every cycle — so a finished conversation cost a request per poll for
        the life of the project."""
        self.fake.channel("room")
        self.fake.membership("room", "me", done=0)
        self.fake.membership("room", "other", done=0)
        cli.remember("room", "me", "http://127.0.0.1:1")
        self.assertIn("room", cli.read_joined())
        self.quiet(cli.do_leave, "http://127.0.0.1:1", "room", "me")
        self.assertNotIn("room", cli.read_joined())

    def test_room_closes_only_once_every_member_is_done(self):
        self.fake.channel("room")
        self.fake.membership("room", "me", done=0)
        self.fake.membership("room", "other", done=0)
        self.quiet(cli.do_leave, "http://127.0.0.1:1", "room", "me")
        self.assertEqual(self.fake.get_channel("room")["closed"], 0,
                         "one member leaving must not close it on the other")
        self.quiet(cli.do_leave, "http://127.0.0.1:1", "room", "other")
        self.assertEqual(self.fake.get_channel("room")["closed"], 1)

    def test_leave_announces_the_departure(self):
        """Nobody else learns you left otherwise — the print above only ever
        reached the leaver's own stdout."""
        self.fake.channel("room")
        self.fake.membership("room", "me", done=0)
        self.fake.membership("room", "other", done=0)
        self.quiet(cli.do_leave, "http://127.0.0.1:1", "room", "me")
        messages = self.fake.tables["messages"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["from_identity"], "me")
        self.assertEqual(messages[0]["audience"], cli.AUDIENCE_NONE,
                         "an FYI must not wake anyone just to say goodbye")

    def test_ask_announces_but_does_not_finalize(self):
        """The negotiation step: still a member, still polled, still
        reachable — a `say` with an agreed meaning, not a departure."""
        self.fake.channel("room")
        self.fake.membership("room", "me", done=0)
        self.fake.membership("room", "other", done=0)
        cli.remember("room", "me", "http://127.0.0.1:1")
        _, out = self.quiet(cli.do_leave, "http://127.0.0.1:1", "room", "me",
                            ask=True)
        self.assertIn("asked", out)
        member = self.fake.get_membership("room", "me")
        self.assertEqual(member["done"], 0, "asking must not mark done")
        self.assertIn("room", cli.read_joined(),
                      "asking must not forget the room locally")
        messages = self.fake.tables["messages"]
        self.assertEqual(len(messages), 1)
        self.assertIsNone(messages[0]["audience"],
                          "asking wants an answer, so normal wake rules "
                          "apply rather than AUDIENCE_NONE")

    def test_a_departure_that_cannot_be_announced_still_completes(self):
        """A closed or capped room makes `do_say` raise — and that must
        never block the actual membership update, which is what matters."""
        self.fake.channel("room", closed=1, closed_reason="testing")
        self.fake.membership("room", "me", done=0)
        self.quiet(cli.do_leave, "http://127.0.0.1:1", "room", "me")
        self.assertEqual(self.fake.get_membership("room", "me")["done"], 1)

    # ── reopening ───────────────────────────────────────────────────────────
    def test_reopen_clears_the_closure(self):
        self.fake.channel("room", closed=1, closed_reason="every member is done")
        self.quiet(cli.do_reopen, "http://127.0.0.1:1", "room", None)
        chan = self.fake.get_channel("room")
        self.assertEqual(chan["closed"], 0)
        self.assertIsNone(chan["closed_reason"])

    def test_reopen_is_idempotent(self):
        self.fake.channel("room", closed=0)
        _, text = self.quiet(cli.do_reopen, "http://127.0.0.1:1", "room", None)
        self.assertIn("already open", text)

    def test_reopen_refuses_a_capped_room_without_more_room_to_talk(self):
        """Reopening at the cap hands back a room that closes again on the very
        next message, which is worse than refusing."""
        self.fake.channel("room", closed=1, closed_reason="hit the 2-message cap",
                          max_messages=2, message_count=2)
        with self.assertRaises(SystemExit) as caught:
            cli.do_reopen("http://127.0.0.1:1", "room", None)
        self.assertIn("--max-messages", str(caught.exception))

    def test_reopen_with_a_bigger_cap_succeeds(self):
        self.fake.channel("room", closed=1, max_messages=2, message_count=2)
        self.quiet(cli.do_reopen, "http://127.0.0.1:1", "room", 50)
        chan = self.fake.get_channel("room")
        self.assertEqual(chan["closed"], 0)
        self.assertEqual(chan["max_messages"], 50)

    def test_reopen_of_a_missing_room_is_refused(self):
        with self.assertRaises(SystemExit):
            cli.do_reopen("http://127.0.0.1:1", "nope", None)

    # ── saying ──────────────────────────────────────────────────────────────
    def test_say_refuses_a_closed_room(self):
        self.fake.channel("room", closed=1, closed_reason="every member is done")
        self.fake.membership("room", "me")
        with self.assertRaises(SystemExit):
            cli.do_say("http://127.0.0.1:1", "room", "me", "hello?")

    def test_say_closes_the_room_when_it_hits_the_cap(self):
        self.fake.channel("room", max_messages=1, message_count=1)
        self.fake.membership("room", "me")
        with self.assertRaises(SystemExit):
            cli.do_say("http://127.0.0.1:1", "room", "me", "one too many")
        self.assertEqual(self.fake.get_channel("room")["closed"], 1)

    def test_say_warns_before_the_wall_not_at_it(self):
        """An agent that hits the cap mid-thought loses the thought."""
        self.fake.channel("room", max_messages=10, message_count=8)
        self.fake.membership("room", "me")
        _, text = self.quiet(cli.do_say, "http://127.0.0.1:1", "room", "me", "nearly there")
        self.assertIn("before", text)

    def test_say_from_a_non_member_is_refused(self):
        self.fake.channel("room")
        with self.assertRaises(SystemExit):
            cli.do_say("http://127.0.0.1:1", "room", "stranger", "let me in")

    def test_seq_is_gap_free_and_per_channel(self):
        """Cursors compare against seq, not created_at: two agents replying in
        the same millisecond are indistinguishable by time."""
        for name in ("a", "b"):
            self.fake.channel(name)
            self.fake.membership(name, "me")
        for _ in range(3):
            self.quiet(cli.do_say, "http://127.0.0.1:1", "a", "me", "x")
        self.quiet(cli.do_say, "http://127.0.0.1:1", "b", "me", "y")
        seqs = {}
        for row in self.fake.tables["messages"]:
            seqs.setdefault(row["channel"], []).append(row["seq"])
        self.assertEqual(sorted(seqs["a"]), [1, 2, 3])
        self.assertEqual(sorted(seqs["b"]), [1])


if __name__ == "__main__":
    unittest.main()


class BroadcastTest(RoomTest):
    """A room everyone is in, which is exactly why it must never wake anyone.

    Auto-join plus the idle waker would mean one note pulls every agent on the
    machine off its work, and the cost is paid by people who did not choose to
    be in the room. Delivered while already working; skipped when idle.
    """

    def test_opening_with_broadcast_marks_the_room(self):
        self.quiet(cli.do_join, "http://127.0.0.1:1", "notices", "me", None, 200, False,
                   True)
        self.assertEqual(self.fake.get_channel("notices")["broadcast"], 1)

    def test_ordinary_rooms_are_not_broadcast(self):
        self.quiet(cli.do_join, "http://127.0.0.1:1", "room", "me", None, 200, False)
        self.assertEqual(self.fake.get_channel("room")["broadcast"], 0)

    def test_identifying_pulls_in_every_broadcast_room(self):
        """Server-side membership alone would do nothing — both hooks read the
        LOCAL record to decide what to poll."""
        self.fake.channel("notices", broadcast=1, message_count=4)
        added = cli.reconcile_broadcasts("http://127.0.0.1:1", "me")
        self.assertEqual(added, ["notices"])
        self.assertTrue(cli.read_joined()["notices"]["broadcast"])
        self.assertIsNotNone(self.fake.get_membership("notices", "me"))

    def test_joining_a_broadcast_room_starts_you_at_the_end(self):
        """Arriving must not replay every learning ever posted."""
        self.fake.channel("notices", broadcast=1, message_count=9)
        cli.reconcile_broadcasts("http://127.0.0.1:1", "me")
        self.assertEqual(
            self.fake.get_membership("notices", "me")["seen_seq"], 9)

    def test_a_closed_broadcast_room_is_not_auto_joined(self):
        self.fake.channel("notices", broadcast=1, closed=1)
        self.assertEqual(cli.reconcile_broadcasts("http://127.0.0.1:1", "me"), [])

    def test_reconciling_twice_does_not_rejoin(self):
        self.fake.channel("notices", broadcast=1)
        cli.reconcile_broadcasts("http://127.0.0.1:1", "me")
        self.assertEqual(cli.reconcile_broadcasts("http://127.0.0.1:1", "me"), [])

    def test_ordinary_rooms_are_left_alone_by_reconciliation(self):
        self.fake.channel("private")
        self.assertEqual(cli.reconcile_broadcasts("http://127.0.0.1:1", "me"), [])
        self.assertNotIn("private", cli.read_joined())


class ProjectIdentityTest(RoomTest):
    """Identity was already remembered per channel — say/read/leave never
    needed --as. What repeated was --as on every JOIN."""

    def test_an_identified_project_joins_without_naming_itself(self):
        self.quiet(cli.do_identify, "http://127.0.0.1:1", "me")
        self.assertEqual(cli.resolve_identity(None, "brand-new"), "me")

    def test_an_explicit_as_still_wins(self):
        self.quiet(cli.do_identify, "http://127.0.0.1:1", "me")
        self.assertEqual(cli.resolve_identity("someone-else", "room"),
                         "someone-else")

    def test_a_room_you_already_joined_keeps_its_own_identity(self):
        """One project holds a different identity per room, which is what the
        owner-channel convention encourages."""
        self.quiet(cli.do_identify, "http://127.0.0.1:1", "me")
        cli.remember("room", "other-name", "http://127.0.0.1:1")
        self.assertEqual(cli.resolve_identity(None, "room"), "other-name")

    def test_with_no_identity_at_all_it_names_both_ways_out(self):
        with self.assertRaises(SystemExit) as caught:
            cli.resolve_identity(None, "room")
        message = str(caught.exception)
        self.assertIn("--as", message)
        self.assertIn("identify", message)

    def test_identifying_reports_the_rooms_it_pulled_in(self):
        """Auto-join is invisible otherwise: an agent would be in a room it
        was never told about."""
        self.fake.channel("notices", broadcast=1)
        _, text = self.quiet(cli.do_identify, "http://127.0.0.1:1", "me")
        self.assertIn("auto-joined: notices", text)

    def test_identify_is_reachable_from_the_command_line(self):
        argv = sys.argv
        sys.argv = ["llm_chat", "identify", "me"]
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(cli.main(), 0)
        finally:
            sys.argv = argv
        self.assertEqual(cli.project_identity(), "me")

    def test_a_bad_identity_is_refused(self):
        with self.assertRaises(SystemExit):
            cli.do_identify("http://127.0.0.1:1", "has space")
