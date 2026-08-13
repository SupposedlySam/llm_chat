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


class OwedTest(RoomTest):
    """Issue #3: replying when addressed was a convention, and conventions
    hold only some of the time.

    An agent did the work, answered in its own terminal, and never posted
    back. Nothing was broken and every rule had been read — which is the
    problem. From the human's side, away from that desk, it was
    indistinguishable from an agent that had died.
    """

    SERVER = "http://127.0.0.1:1"

    def arrange(self, mine=(), theirs=(), done=0, closed=0, audience="me"):
        """A room where `theirs` are messages addressed to me at those seqs."""
        self.fake.channel("room", closed=closed)
        self.fake.tables.setdefault("memberships", []).append(
            {"id": "m1", "channel": "room", "identity": "me", "done": done,
             "seen_seq": 0})
        rows = []
        for seq in mine:
            rows.append({"id": "s%d" % seq, "channel": "room", "seq": seq,
                         "from_identity": "me", "body": "mine",
                         "audience": None, "created_at": seq})
        for seq in theirs:
            rows.append({"id": "t%d" % seq, "channel": "room", "seq": seq,
                         "from_identity": "asker", "body": "a question",
                         "audience": audience, "created_at": seq})
        self.fake.tables.setdefault("messages", []).extend(rows)
        cli.remember("room", "me", self.SERVER)

    def owed(self, as_json=False):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.do_owed(self.SERVER, "me", as_json)
        return code, out.getvalue()

    def test_A_QUESTION_AFTER_I_LAST_SPOKE_IS_OWED(self):
        self.arrange(mine=[1], theirs=[2])
        code, text = self.owed()
        self.assertEqual(code, 1)
        self.assertIn("asker asked at seq 2", text)

    def test_answering_clears_it(self):
        """The debt is the RELATIONSHIP between two seqs, so speaking after
        the question settles it without anything being marked."""
        self.arrange(mine=[1, 3], theirs=[2])
        self.assertEqual(self.owed()[0], 0)

    def test_HAVING_READ_IS_NOT_HAVING_ANSWERED(self):
        """The gap `pending` cannot express. It reports what is UNREAD, so an
        agent that read the question and went quiet shows wakes_me:false and
        looks clean — the debt becomes invisible exactly once it is seen."""
        self.arrange(mine=[1], theirs=[2])
        self.fake.tables["memberships"][0]["seen_seq"] = 99
        self.assertEqual(self.owed()[0], 1)

    def test_a_message_that_does_NOT_address_me_is_not_a_debt(self):
        """Being in the room is not being asked. Otherwise every passive line
        in a busy room would block the turn."""
        self.arrange(mine=[1], theirs=[2], audience="someone-else")
        self.assertEqual(self.owed()[0], 0)

    def test_LEAVING_CLEARS_THE_DEBT(self):
        """`leave` is the documented way to say "I have nothing left to add".
        A debt that survived it would make the one honest exit permanently
        unavailable — blocked forever by a conversation correctly finished."""
        self.arrange(mine=[1], theirs=[2], done=1)
        self.assertEqual(self.owed()[0], 0)

    def test_a_closed_room_owes_nothing(self):
        self.arrange(mine=[1], theirs=[2], closed=1)
        self.assertEqual(self.owed()[0], 0)

    def test_a_question_before_I_ever_spoke_still_counts(self):
        """An agent that has never spoken in a room owes the same answer as
        one that spoke and then stopped."""
        self.arrange(mine=[], theirs=[1])
        self.assertEqual(self.owed()[0], 1)

    def test_COULD_NOT_LOOK_IS_ITS_OWN_EXIT_CODE(self):
        """The whole reason this is safe to gate on. A check that folds its
        own failure into 'nothing owed' fails open in silence, which is issue
        #1 in this repo."""
        self.arrange(mine=[1], theirs=[2])
        real = cli.get_channel

        def unreachable(server, name):
            raise SystemExit("no llm_chat server at %s" % server)
        cli.get_channel = unreachable
        try:
            code, text = self.owed()
        finally:
            cli.get_channel = real
        self.assertEqual(code, 2)
        self.assertIn("COULD NOT CHECK", text)

    def test_unreachable_OUTRANKS_owed(self):
        """A gate seeing one debt and one failure would act on the debt and
        never learn a second room could not be read at all."""
        self.arrange(mine=[1], theirs=[2])
        self.fake.channel("other")
        self.fake.tables["memberships"].append(
            {"id": "m2", "channel": "other", "identity": "me", "done": 0,
             "seen_seq": 0})
        cli.remember("other", "me", self.SERVER)
        real = cli.get_channel
        cli.get_channel = lambda server, name: (
            real(server, name) if name == "room" else
            (_ for _ in ()).throw(SystemExit("unreachable")))
        try:
            code, _ = self.owed()
        finally:
            cli.get_channel = real
        self.assertEqual(code, 2)

    def test_a_room_this_identity_never_joined_owes_nothing(self):
        """joined.json can name a room the server no longer has us in — a
        membership removed from elsewhere, or a room deleted and rebuilt."""
        self.arrange(mine=[1], theirs=[2])
        self.fake.tables["memberships"] = []
        self.assertEqual(self.owed()[0], 0)

    def test_an_entry_with_NO_identity_is_skipped_not_guessed(self):
        """Guessing which identity a room belongs to would report a debt
        against an agent that was never in it. Reached only when --as is also
        absent, since an explicit identity legitimately fills the gap."""
        self.arrange(mine=[1], theirs=[2])
        joined = cli.read_joined()
        joined["orphan"] = {"server": self.SERVER}
        with open(cli.joined_path(), "w") as f:
            json.dump(joined, f)
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.do_owed(self.SERVER, None, False)
        self.assertEqual(code, 1)          # the real room still counts
        self.assertNotIn("orphan", out.getvalue())

    def test_UNREADABLE_LOCAL_STATE_IS_LOUD(self):
        """joined.json is the list of rooms to check. If it cannot be read,
        the honest answer is not "no rooms, nothing owed" — that is the
        fail-open-in-silence shape this verb exists to avoid."""
        real = cli.read_joined

        def unreadable():
            raise OSError("permission denied")
        cli.read_joined = unreadable
        try:
            with self.assertRaises(SystemExit) as caught:
                cli.do_owed(self.SERVER, "me", False)
        finally:
            cli.read_joined = real
        self.assertIn("could not read joined rooms", str(caught.exception))

    def test_json_carries_what_a_gate_needs_to_say_WHAT_is_owed(self):
        self.arrange(mine=[1], theirs=[2])
        code, text = self.owed(as_json=True)
        payload = json.loads(text)
        self.assertEqual(code, 1)
        debt = payload["owed"][0]
        self.assertEqual(debt["room"], "room")
        self.assertEqual(debt["from"], "asker")
        self.assertEqual(debt["seq"], 2)
        self.assertIn("a question", debt["text_preview"])

    def test_nothing_owed_says_so_rather_than_printing_nothing(self):
        """Silence from a check is indistinguishable from a check that did not
        run, which is the failure this whole verb is about."""
        self.arrange(mine=[1, 3], theirs=[2])
        self.assertIn("nothing owed", self.owed()[1])

    def test_owed_is_reachable_from_the_command_line(self):
        self.arrange(mine=[1], theirs=[2])
        argv = sys.argv
        sys.argv = ["llm_chat", "--server", self.SERVER, "owed", "--as", "me"]
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(cli.main(), 1)
        finally:
            sys.argv = argv


class DeleteTest(RoomTest):
    """The only irreversible verb, so the refusals matter more than the act."""

    SERVER = "http://127.0.0.1:1"

    def room(self, name="doomed", **overrides):
        self.fake.channel(name, **overrides)
        self.fake.tables.setdefault("memberships", []).append(
            {"id": "m1", "channel": name, "identity": "me", "done": 0})
        self.fake.tables.setdefault("messages", []).extend(
            [{"id": "x%d" % i, "channel": name, "from_identity": "me",
              "body": "hi", "seq": i} for i in range(3)])
        return name

    def test_WITHOUT_yes_it_destroys_NOTHING(self):
        """The whole point of the flag. A dry run that deleted anything would
        be the worst possible reading of 'preview'."""
        name = self.room()
        _, text = self.quiet(cli.do_delete, self.SERVER, name, "me")
        self.assertIn("NOT DELETED", text)
        self.assertIn("3 message(s)", text)
        self.assertEqual(len(self.fake.tables["messages"]), 3)
        self.assertEqual(len(self.fake.tables["channels"]), 1)

    def test_with_yes_the_room_and_everything_in_it_is_gone(self):
        name = self.room()
        _, text = self.quiet(cli.do_delete, self.SERVER, name, "me", yes=True)
        self.assertIn("deleted #doomed", text)
        self.assertEqual(self.fake.tables["messages"], [])
        self.assertEqual(self.fake.tables["memberships"], [])
        self.assertEqual(self.fake.tables["channels"], [])

    def test_ANOTHER_ROOMS_MESSAGES_SURVIVE(self):
        """The where-clause is the whole safety property. A delete that
        matched on the wrong column would empty the database and report
        success, and every other assertion here would still pass."""
        self.room("doomed")
        self.fake.channel("keeper")
        self.fake.tables["messages"].append(
            {"id": "k1", "channel": "keeper", "from_identity": "you",
             "body": "still here", "seq": 1})
        self.quiet(cli.do_delete, self.SERVER, "doomed", "me", yes=True)
        left = self.fake.tables["messages"]
        self.assertEqual([m["id"] for m in left], ["k1"])
        self.assertEqual([c["name"] for c in self.fake.tables["channels"]],
                         ["keeper"])

    def test_a_NON_MEMBER_cannot_delete(self):
        """There is no owner, so membership is the only thing standing between
        a room and somebody who was never in it."""
        name = self.room()
        with self.assertRaises(SystemExit) as caught:
            cli.do_delete(self.SERVER, name, "stranger", yes=True)
        self.assertIn("has not joined", str(caught.exception))
        self.assertEqual(len(self.fake.tables["messages"]), 3)

    def test_a_room_that_does_not_exist_is_refused(self):
        with self.assertRaises(SystemExit):
            cli.do_delete(self.SERVER, "ghost", "me", yes=True)

    def test_the_preview_WARNS_about_a_broadcast_room(self):
        """Every identified project is auto-joined to one, so nobody chose to
        be there and nobody expects it to vanish."""
        name = self.room("notices", broadcast=1)
        _, text = self.quiet(cli.do_delete, self.SERVER, name, "me")
        self.assertIn("BROADCAST ROOM", text)

    def test_the_preview_NAMES_members_who_have_not_left(self):
        name = self.room()
        self.fake.tables["memberships"].append(
            {"id": "m2", "channel": name, "identity": "busy", "done": 0})
        _, text = self.quiet(cli.do_delete, self.SERVER, name, "me")
        self.assertIn("STILL TALKING", text)
        self.assertIn("busy", text)

    def test_deleting_reports_the_doorbells_it_removed(self):
        """Said out loud because it is the part that touches another agent's
        machine state: their waker was listening on that socket, and the count
        is how the deleter learns somebody was."""
        name = self.room()
        bells = tempfile.TemporaryDirectory()
        self.addCleanup(bells.cleanup)
        real = cli.doorbell_dir
        cli.doorbell_dir = lambda server=None: bells.name
        try:
            open(os.path.join(bells.name, "%s__me.sock" % name), "w").close()
            _, text = self.quiet(cli.do_delete, self.SERVER, name, "me",
                                 yes=True)
        finally:
            cli.doorbell_dir = real
        self.assertIn("removed 1 doorbell socket(s)", text)

    def test_it_hangs_up_the_rooms_doorbells(self):
        """A socket file outlives the room. It is not a listener — binding onto
        it fails while connecting is refused — so a rebuilt room of the same
        name would have senders ringing a doorbell nobody can hear."""
        bells = tempfile.TemporaryDirectory()
        self.addCleanup(bells.cleanup)
        real = cli.doorbell_dir
        cli.doorbell_dir = lambda server=None: bells.name
        try:
            for name in ("doomed__me.sock", "doomed__you.sock",
                         "keeper__me.sock", "notes.txt"):
                open(os.path.join(bells.name, name), "w").close()
            self.assertEqual(cli.hang_up("doomed"), 2)
            self.assertEqual(sorted(os.listdir(bells.name)),
                             ["keeper__me.sock", "notes.txt"])
            # A directory that is not there at all is the ordinary case on a
            # machine where nothing has ever listened.
            cli.doorbell_dir = lambda server=None: "/no/such/doorbells"
            self.assertEqual(cli.hang_up("doomed"), 0)
        finally:
            cli.doorbell_dir = real

    def test_a_socket_that_vanishes_mid_sweep_is_not_an_error(self):
        """Another agent deleting the same room at the same moment is a race
        this cannot win and does not need to."""
        bells = tempfile.TemporaryDirectory()
        self.addCleanup(bells.cleanup)
        real, real_unlink = cli.doorbell_dir, cli.os.unlink
        cli.doorbell_dir = lambda server=None: bells.name
        open(os.path.join(bells.name, "doomed__me.sock"), "w").close()

        def vanished(path):
            raise OSError("gone")
        cli.os.unlink = vanished
        try:
            self.assertEqual(cli.hang_up("doomed"), 0)
        finally:
            cli.doorbell_dir, cli.os.unlink = real, real_unlink

    def test_a_wire_error_on_delete_is_loud(self):
        """A partial delete that reports success would leave rows belonging to
        a channel that is gone, which nothing here knows how to find."""
        real = cli.call
        cli.call = lambda *a, **kw: {"error": "HTTP 500", "body": "nope"}
        try:
            with self.assertRaises(SystemExit) as caught:
                cli.remove("http://127.0.0.1:1", "messages", cli.eq("x", "y"))
        finally:
            cli.call = real
        self.assertIn("HTTP 500", str(caught.exception))

    def test_delete_is_reachable_from_the_command_line(self):
        name = self.room()
        cli.remember(name, "me", self.SERVER)
        argv = sys.argv
        sys.argv = ["llm_chat", "--server", self.SERVER, "delete", name,
                    "--as", "me", "--yes"]
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(cli.main(), 0)
        finally:
            sys.argv = argv
        self.assertIn("deleted #doomed", out.getvalue())
        self.assertEqual(self.fake.tables["channels"], [])

    def test_it_is_forgotten_locally_so_the_hooks_stop_polling_it(self):
        """Both hooks read joined.json to decide what to poll. A deleted room
        left in it is polled forever against a room that 404s."""
        name = self.room()
        cli.remember(name, "me", self.SERVER)
        self.assertIn(name, cli.read_joined())
        self.quiet(cli.do_delete, self.SERVER, name, "me", yes=True)
        self.assertNotIn(name, cli.read_joined())
