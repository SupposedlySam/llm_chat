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

    def test_AN_OWNER_CANNOT_WALK_OUT_AND_LEAVE_THE_ROOM_OPEN(self):
        """A room named after a project is where people bring that project's
        problems. Its creator leaving does not close it — the room stays open,
        accepts messages and wakes nobody, so a question arrives and sits.

        Measured: showrunner filed a reproducible lockout in #llm_chat_owner
        and it sat for three hours, because the owner had left the room they
        created. `owed` could not see it either — a room you are done with
        owes nothing, by design."""
        self.fake.channel("llm_chat_owner", created_by="owner")
        self.fake.membership("llm_chat_owner", "owner", done=0)
        cli.remember("llm_chat_owner", "owner", "http://127.0.0.1:1")
        with self.assertRaises(SystemExit) as caught:
            self.quiet(cli.do_leave, "http://127.0.0.1:1", "llm_chat_owner",
                       "owner")
        said = str(caught.exception)
        self.assertIn("CREATED", said)
        self.assertIn("close", said)
        self.assertIn("delete", said)
        self.assertIn("llm_chat_owner", cli.read_joined(),
                      "the refusal must not half-apply")

    def test_a_NON_owner_may_still_leave(self):
        """Paired. The rule is about abandoning a help desk you opened, not
        about making rooms impossible to exit."""
        self.fake.channel("llm_chat_owner", created_by="owner")
        self.fake.membership("llm_chat_owner", "visitor", done=0)
        cli.remember("llm_chat_owner", "visitor", "http://127.0.0.1:1")
        self.quiet(cli.do_leave, "http://127.0.0.1:1", "llm_chat_owner",
                   "visitor")
        self.assertNotIn("llm_chat_owner", cli.read_joined())

    def test_an_owner_may_leave_a_room_they_already_CLOSED(self):
        """Closing is the honest exit: it ends the room and says so to anyone
        who tries. Once closed there is no open door to abandon."""
        self.fake.channel("llm_chat_owner", created_by="owner", closed=1)
        self.fake.membership("llm_chat_owner", "owner", done=0)
        cli.remember("llm_chat_owner", "owner", "http://127.0.0.1:1")
        self.quiet(cli.do_leave, "http://127.0.0.1:1", "llm_chat_owner",
                   "owner")
        self.assertNotIn("llm_chat_owner", cli.read_joined())

    def test_an_owner_may_still_ASK_the_room(self):
        """`--ask` keeps the membership and puts the question to the room,
        which is the negotiation an owner SHOULD be able to start."""
        self.fake.channel("llm_chat_owner", created_by="owner")
        self.fake.membership("llm_chat_owner", "owner", done=0)
        cli.remember("llm_chat_owner", "owner", "http://127.0.0.1:1")
        self.quiet(cli.do_leave, "http://127.0.0.1:1", "llm_chat_owner",
                   "owner", ask=True)
        self.assertIn("llm_chat_owner", cli.read_joined())

    def test_LEAVING_DOES_NOT_CLOBBER_ANOTHER_IDENTITYS_LOCAL_RECORD(self):
        """showrunner's lockout, as a test.

        A project can hold several identities and joined.json keeps ONE record
        per room. `leave --as owner` deleted a record that said `showrunner` —
        so a live server-side membership existed that the client would no
        longer use. `channels` showed them a member, every call answered "you
        have not joined", and `join` reported success each time.

        Two stores disagreeing with the client trusting the wrong one, and the
        write was made by a departing identity on behalf of one that had not
        departed."""
        self.fake.channel("room")
        self.fake.membership("room", "owner", done=0)
        self.fake.membership("room", "showrunner", done=0)
        cli.remember("room", "showrunner", "http://127.0.0.1:1")
        self.quiet(cli.do_leave, "http://127.0.0.1:1", "room", "owner")
        self.assertEqual(cli.read_joined().get("room", {}).get("identity"),
                         "showrunner",
                         "owner leaving deleted showrunner's local record")

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

    def test_THE_CAP_CAN_BE_RAISED_BEFORE_THE_ROOM_SHUTS(self):
        """The remedy has to be reachable BEFORE the harm, not after it.

        `--max-messages` used to do nothing on an open room — it printed
        "already open" and returned — so the only way to raise a cap was to
        let the room close first. That is the shape this project keeps
        removing: a fix that becomes available only once the thing it
        prevents has happened.

        It matters most where it is worst. #llm_chat_owner is where agents
        land when they CANNOT get connected, so the population arriving at a
        shut door is exactly the one least able to run `reopen`. auditor
        noticed it at 187 of 200 and said so while there was still room."""
        self.fake.channel("room", closed=0, max_messages=200,
                          message_count=187)
        _, text = self.quiet(cli.do_reopen, "http://127.0.0.1:1", "room", 600)
        self.assertEqual(self.fake.get_channel("room")["max_messages"], 600)
        self.assertIn("cap raised", text)

    def test_raising_the_cap_leaves_an_open_room_OPEN(self):
        """It must not disturb anything else about a room that was fine."""
        self.fake.channel("room", closed=0, max_messages=200,
                          message_count=10)
        self.quiet(cli.do_reopen, "http://127.0.0.1:1", "room", 600)
        self.assertEqual(self.fake.get_channel("room")["closed"], 0)

    def test_a_cap_BELOW_what_is_already_used_is_refused(self):
        """Paired with the closed-room version above, and the same reasoning:
        accepting it would hand back a room that shuts on the next message,
        which is worse than refusing."""
        self.fake.channel("room", closed=0, max_messages=200,
                          message_count=187)
        with self.assertRaises(SystemExit) as caught:
            cli.do_reopen("http://127.0.0.1:1", "room", 50)
        message = str(caught.exception)
        self.assertIn("187", message)
        self.assertIn("--max-messages", message)

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
        """A HUMAN at a terminal, stated explicitly rather than inherited from
        whoever happens to run the suite. A session gets a generated name
        instead (see test_identity), so leaving the ambient session id in
        place would make this assert the opposite of what it says."""
        saved = os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        try:
            with self.assertRaises(SystemExit) as caught:
                cli.resolve_identity(None, "room")
        finally:
            if saved is not None:
                os.environ["CLAUDE_CODE_SESSION_ID"] = saved
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

    def test_A_NON_WAKING_REPLY_DISCHARGES_THE_DEBT(self):
        """Issue #15's squeeze, and the way out of it.

        A finished leaf is caught between three rules: the gate demands an
        answer, the etiquette forbids trivial "ack" messages because every
        message WAKES the room, and `leave` — the remedy the gate actually
        prints — stands down its own waker, so a headless session that leaves
        and then stops becomes unreachable. The reporter lost a Crawler's
        verdict to exactly that.

        `--to-none` is the missing move: it says the thing, wakes nobody, and
        the debt clears because the debt is "somebody spoke after I last did",
        which is audience-independent. Asserted here because the refusal text
        is about to recommend it, and a remedy that did not work would be
        worse than the trap."""
        self.arrange(mine=[1], theirs=[2])
        self.seq_counter_at(2)
        self.assertEqual(self.owed()[0], 1)
        with redirect_stdout(io.StringIO()):
            cli.do_say(self.SERVER, "room", "me", "done: refuted, report "
                       "filed", audience=cli.audience_for(None, False, True))
        self.assertEqual(self.owed()[0], 0,
                         "a --to-none reply must clear the debt")

    def seq_counter_at(self, n):
        """`arrange` writes message rows straight into the table, so the
        channel's own counter — which `say` reads to number the next message —
        is still zero. Without this a reply is numbered BELOW the question it
        answers, and the debt survives for a reason that exists only in the
        fixture."""
        for chan in self.fake.tables["channels"]:
            if chan["name"] == "room":
                chan["message_count"] = n

    def test_the_non_waking_reply_really_wakes_NOBODY(self):
        """Paired. If it woke the room it would be the thing the etiquette
        forbids, and recommending it would trade one trap for another."""
        self.arrange(mine=[1], theirs=[2])
        self.seq_counter_at(2)
        with redirect_stdout(io.StringIO()):
            cli.do_say(self.SERVER, "room", "me", "done",
                       audience=cli.audience_for(None, False, True))
        posted = [m for m in self.fake.tables["messages"]
                  if m["from_identity"] == "me"][-1]
        self.assertNotIn("everyone", (posted.get("audience") or ""))

    def test_THE_COST_DOES_NOT_SCALE_WITH_ROOMS_JOINED(self):
        """Issue #14, and the whole point of batching. `owed` cost three
        requests per room — channel, membership, messages — so an orchestrator
        holding eight rooms spent twenty-four on one turn-end check, and the
        agent in the most rooms is by construction the one coordinating
        everybody. The reporter hit the server's rate limit and mitigated it
        by LEAVING rooms, which treats the number of conversations you are in
        as the thing to reduce.

        Asserted as a count rather than a duration, because a timing test
        would pass on a fast machine while the request count crept back."""
        self.arrange(mine=[1], theirs=[2])
        for extra in ("two", "three", "four", "five"):
            self.fake.channel(extra)
            self.fake.tables["memberships"].append(
                {"id": "m-" + extra, "channel": extra, "identity": "me",
                 "done": 0, "seen_seq": 0})
            cli.remember(extra, "me", self.SERVER)

        seen = []
        real = cli.call
        cli.call = lambda *a, **kw: (seen.append(a[2]) or real(*a, **kw))
        try:
            self.owed()
        finally:
            cli.call = real
        self.assertLessEqual(
            len(seen), 4,
            "five rooms cost %d requests: %s" % (len(seen), seen))

    def test_the_batch_gives_THE_SAME_ANSWER_as_the_per_room_path(self):
        """Speed is worthless if it changed the verdict. The debt found across
        five rooms must be the one the unbatched lookup finds in the room it
        is in."""
        self.arrange(mine=[1], theirs=[2])
        self.fake.channel("quiet")
        self.fake.tables["memberships"].append(
            {"id": "mq", "channel": "quiet", "identity": "me", "done": 0,
             "seen_seq": 0})
        cli.remember("quiet", "me", self.SERVER)
        code, text = self.owed()
        self.assertEqual(code, 1)
        self.assertIn("room", text)
        self.assertNotIn("#quiet", text)
        # And the single-room path, with no store, agrees.
        alone = cli.owed_in(self.SERVER, "room", {}, "me")
        self.assertIsNotNone(alone)
        self.assertEqual(alone["seq"], 2)

    def test_ONE_BAD_ROOM_DOES_NOT_ABANDON_THE_WHOLE_CHECK(self):
        """The risk batching introduced. Per-room, a room whose rows were
        malformed failed alone; sharing one fetch means an exception while
        interpreting ONE room would abandon every other room and leave the
        gate with no answer at all.

        The bad room is reported as unchecked — which is exit 2, not a
        silently smaller answer."""
        self.arrange(mine=[1], theirs=[2])
        self.fake.channel("bad")
        self.fake.tables["memberships"].append(
            {"id": "mb", "channel": "bad", "identity": "me", "done": 0,
             "seen_seq": 0})
        cli.remember("bad", "me", self.SERVER)
        real = cli.owed_in
        cli.owed_in = lambda server, name, entry, who, store=None: (
            (_ for _ in ()).throw(KeyError("from_identity")) if name == "bad"
            else real(server, name, entry, who, store=store))
        try:
            code, text = self.owed()
        finally:
            cli.owed_in = real
        self.assertEqual(code, 2, "a bad room must not read as 'nothing owed'")
        self.assertIn("bad", text)
        self.assertIn("KeyError", text)

    def test_COULD_NOT_LOOK_IS_ITS_OWN_EXIT_CODE(self):
        """The whole reason this is safe to gate on. A check that folds its
        own failure into 'nothing owed' fails open in silence, which is issue
        #1 in this repo."""
        self.arrange(mine=[1], theirs=[2])
        real = cli.Store

        def unreachable(server):
            raise SystemExit("no llm_chat server at %s" % server)
        cli.Store = unreachable
        try:
            code, text = self.owed()
        finally:
            cli.Store = real
        self.assertEqual(code, 2)
        self.assertIn("COULD NOT CHECK", text)

    def test_A_429_IS_MARKED_RATE_LIMITED_IN_THE_PAYLOAD(self):
        """Issue #18, at the producing end.

        `call` already works out that a failure was a rate limit; `rows` then
        flattened it into a string and the flag was gone one line later. So a
        transient and an outage reached the turn-end gate as the same thing,
        with only prose to tell them apart — and the gate blocked on a 429
        that cleared twenty seconds later.

        Exercised through the REAL path — call → rows → Throttled → payload —
        because the gate's own tests feed a hand-built payload and would pass
        with the flag never produced at all. That is how this mutation
        survived a sweep."""
        self.arrange(mine=[1], theirs=[2])
        cli.call = lambda *a, **kw: {"error": "HTTP 429",
                                     "body": "Rate limit exceeded",
                                     "rate_limited": True}
        code, text = self.owed(as_json=True)
        self.assertEqual(code, 2)
        payload = json.loads(text)
        self.assertTrue(payload["unreachable"][0]["rate_limited"],
                        "a 429 reached the gate indistinguishable from an "
                        "outage")

    def test_an_OUTAGE_is_not_marked_rate_limited(self):
        """Paired, and the half that keeps the flag meaningful: if everything
        were marked retryable the gate would wait on a server that is not
        there."""
        self.arrange(mine=[1], theirs=[2])
        cli.call = lambda *a, **kw: {"error": "no llm_chat server at x"}
        code, text = self.owed(as_json=True)
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(text)["unreachable"][0]["rate_limited"])

    def test_THE_REASON_SURVIVES_so_a_throttle_is_not_an_outage(self):
        """A 429 and a dead server call for opposite responses — wait versus
        stop — and issue #15 is that they read identically at the point of
        decision. The message the server gave has to reach the operator."""
        self.arrange(mine=[1], theirs=[2])
        real = cli.Store

        def throttled(server):
            raise SystemExit("HTTP 429  Rate limit exceeded")
        cli.Store = throttled
        try:
            _, text = self.owed()
        finally:
            cli.Store = real
        self.assertIn("429", text)

    def test_unreachable_OUTRANKS_owed(self):
        """A gate seeing one debt and one failure would act on the debt and
        never learn a second room could not be read at all.

        TWO SERVERS, because that is now the only way a room can be
        unreachable while another is fine: one fetch covers a whole server, so
        per-room failure within one server no longer exists. The old version
        of this test made one room's `get_channel` raise, which batching stopped
        calling — it passed by construction and then stopped meaning
        anything."""
        self.arrange(mine=[1], theirs=[2])
        cli.remember("other", "me", "http://127.0.0.1:9")
        real = cli.Store
        cli.Store = lambda server: (
            real(server) if server == self.SERVER else
            (_ for _ in ()).throw(SystemExit("no llm_chat server at " + server)))
        try:
            code, text = self.owed()
        finally:
            cli.Store = real
        self.assertEqual(code, 2)
        self.assertIn("other", text)

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
