"""Who a message wakes.

Every message used to wake every idle member of an ordinary room, so a third
agent in a two-agent conversation was pulled off its own work at every turn.
This is the machinery that lets a sender narrow that, and lets a broadcast room
widen it for the one note that genuinely needs an answer.

The load-bearing property is NOT "the right people wake". It is that deciding
who wakes must not CONSUME anything: the waker and the delivery hook share one
server-side cursor, so reading in order to decide would take a passive message
off the cursor and drop it — the wallflower would never see it at all.
"""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import FakeServer, load  # noqa: E402

cli = load("llm_chat")


class SentinelTest(unittest.TestCase):
    """The two sentinels share a column with identities and must not collide."""

    def test_neither_sentinel_can_ever_be_a_real_identity(self):
        """Asserted, not trusted. A sentinel that IS a legal value is in-band
        signalling, and an agent called '*' would silently become 'everyone'."""
        self.assertFalse(cli.valid(cli.AUDIENCE_ALL))
        self.assertFalse(cli.valid(cli.AUDIENCE_NONE))

    def test_they_are_distinct(self):
        self.assertNotEqual(cli.AUDIENCE_ALL, cli.AUDIENCE_NONE)


class AudienceForTest(unittest.TestCase):
    def test_no_flags_lets_the_room_decide(self):
        self.assertIsNone(cli.audience_for(None, False, False))

    def test_to_all_and_to_none(self):
        self.assertEqual(cli.audience_for(None, True, False), cli.AUDIENCE_ALL)
        self.assertEqual(cli.audience_for(None, False, True), cli.AUDIENCE_NONE)

    def test_a_single_name(self):
        self.assertEqual(cli.audience_for("bob", False, False), "bob")

    def test_several_names_and_stray_whitespace(self):
        self.assertEqual(cli.audience_for(" bob , carol ", False, False),
                         "bob,carol")

    def test_refuses_two_flags_rather_than_picking_one(self):
        """Silently preferring one leaves the sender holding a wrong belief
        about who it just woke, which is the thing this feature exists to fix."""
        with self.assertRaises(SystemExit) as caught:
            cli.audience_for("bob", True, False)
        self.assertIn("pick one", str(caught.exception))

    def test_refuses_all_three(self):
        with self.assertRaises(SystemExit):
            cli.audience_for("bob", True, True)

    def test_refuses_to_with_no_names(self):
        with self.assertRaises(SystemExit) as caught:
            cli.audience_for(" , ", False, False)
        self.assertIn("no names", str(caught.exception))

    def test_refuses_an_invalid_identity(self):
        """Otherwise a name containing a comma or a space becomes two
        recipients, or one that can never match."""
        with self.assertRaises(SystemExit) as caught:
            cli.audience_for("not a name", False, False)
        self.assertIn("not a valid identity", str(caught.exception))

    def test_a_sentinel_passed_as_a_name_is_refused(self):
        """The collision guard, from the other direction: --to '*' must not be
        a back door to waking everyone."""
        with self.assertRaises(SystemExit):
            cli.audience_for(cli.AUDIENCE_ALL, False, False)


class WakesTest(unittest.TestCase):
    ORDINARY = {"broadcast": False}
    BROADCAST = {"broadcast": True}

    def test_an_unaddressed_message_wakes_everyone_in_an_ordinary_room(self):
        """The default is unchanged on purpose: if you are in a channel you
        should never NOT be notified of a reply. Narrowing is opt-in."""
        self.assertTrue(cli.wakes({"audience": None}, "bob", self.ORDINARY))

    def test_an_unaddressed_message_wakes_nobody_in_a_broadcast_room(self):
        self.assertFalse(cli.wakes({"audience": None}, "bob", self.BROADCAST))

    def test_to_all_wakes_even_in_a_broadcast_room(self):
        """The escape hatch. Without it the one note that genuinely needs an
        answer is the one thing a broadcast room cannot deliver."""
        self.assertTrue(cli.wakes({"audience": cli.AUDIENCE_ALL}, "bob",
                                  self.BROADCAST))

    def test_to_none_wakes_nobody_even_in_an_ordinary_room(self):
        self.assertFalse(cli.wakes({"audience": cli.AUDIENCE_NONE}, "bob",
                                   self.ORDINARY))

    def test_a_named_recipient_wakes(self):
        self.assertTrue(cli.wakes({"audience": "bob,carol"}, "bob",
                                  self.ORDINARY))

    def test_everyone_else_does_not(self):
        self.assertFalse(cli.wakes({"audience": "bob,carol"}, "dave",
                                   self.ORDINARY))

    def test_a_named_recipient_wakes_inside_a_broadcast_room(self):
        """The whole reason the waker no longer skips broadcast rooms."""
        self.assertTrue(cli.wakes({"audience": "bob"}, "bob", self.BROADCAST))

    def test_a_name_is_matched_whole_not_by_prefix(self):
        """'bob' must not be woken by a message addressed to 'bobby'."""
        self.assertFalse(cli.wakes({"audience": "bobby"}, "bob",
                                   self.ORDINARY))


class DescribeTest(unittest.TestCase):
    def test_nothing_to_say_when_the_room_decides(self):
        self.assertEqual(cli.describe_audience(None), "")

    def test_the_sentinels_read_as_words(self):
        self.assertEqual(cli.describe_audience(cli.AUDIENCE_ALL), "everyone")
        self.assertEqual(cli.describe_audience(cli.AUDIENCE_NONE), "nobody")

    def test_it_says_you_when_you_are_the_recipient(self):
        """An agent scanning a transcript needs 'answer this' to be visually
        different from 'this went past you'."""
        self.assertEqual(cli.describe_audience("bob", "bob"), "you")

    def test_it_names_the_others_alongside_you(self):
        self.assertEqual(cli.describe_audience("bob,carol", "bob"),
                         "you and carol")

    def test_it_names_them_plainly_when_you_are_not_included(self):
        self.assertEqual(cli.describe_audience("bob,carol", "dave"),
                         "bob, carol")


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.real = cli.call
        cli.call = self.server.call
        self.chan = self.server.channel("room")
        for who in ("alice", "bob", "carol"):
            self.server.membership("room", who)

    def tearDown(self):
        cli.call = self.real

    def say(self, text, identity="alice", audience=None):
        out = io.StringIO()
        with redirect_stdout(out):
            cli.do_say("srv", "room", identity, text, audience)
        return out.getvalue()


class RecipientTest(ServerTest):
    def test_a_member_is_accepted(self):
        cli.check_recipients("srv", "room", "bob")

    def test_the_sentinels_are_not_looked_up_as_names(self):
        cli.check_recipients("srv", "room", cli.AUDIENCE_ALL)
        cli.check_recipients("srv", "room", cli.AUDIENCE_NONE)
        cli.check_recipients("srv", "room", None)

    def test_a_non_member_is_refused(self):
        """A mention that silently no-ops is the worst failure available here:
        the sender believes it delivered and waits, the recipient is never
        woken, and nothing reports a problem."""
        with self.assertRaises(SystemExit) as caught:
            cli.check_recipients("srv", "room", "dave")
        self.assertIn("dave", str(caught.exception))

    def test_the_refusal_lists_who_IS_in_the_room(self):
        """So the fix is one read rather than a second guess."""
        with self.assertRaises(SystemExit) as caught:
            cli.check_recipients("srv", "room", "dave")
        self.assertIn("alice", str(caught.exception))

    def test_the_refusal_says_nothing_was_sent(self):
        with self.assertRaises(SystemExit) as caught:
            cli.check_recipients("srv", "room", "dave")
        self.assertIn("Nothing sent", str(caught.exception))

    def test_one_bad_name_among_good_ones_refuses_the_whole_send(self):
        with self.assertRaises(SystemExit):
            cli.check_recipients("srv", "room", "bob,dave")

    def test_nothing_is_written_when_a_recipient_is_unknown(self):
        with self.assertRaises(SystemExit):
            self.say("hello", audience="dave")

    def test_a_departed_member_is_refused(self):
        """`leave` sets done; it does not delete the membership row. A
        name-only check would call carol a valid recipient forever."""
        self.server.get_membership("room", "carol")["done"] = 1
        with self.assertRaises(SystemExit) as caught:
            cli.check_recipients("srv", "room", "carol")
        message = str(caught.exception)
        self.assertIn("carol", message)
        self.assertIn("left", message)
        self.assertIn("Nothing sent", message)

    def test_a_departed_member_is_distinguished_from_a_non_member(self):
        """Two different failures need two different remedies: rejoin vs.
        address someone who was never here at all."""
        self.server.get_membership("room", "carol")["done"] = 1
        with self.assertRaises(SystemExit) as gone:
            cli.check_recipients("srv", "room", "carol")
        with self.assertRaises(SystemExit) as missing:
            cli.check_recipients("srv", "room", "dave")
        self.assertNotEqual(str(gone.exception), str(missing.exception))

    def test_a_departed_member_the_refusal_names_who_is_still_here(self):
        self.server.get_membership("room", "carol")["done"] = 1
        with self.assertRaises(SystemExit) as caught:
            cli.check_recipients("srv", "room", "carol")
        message = str(caught.exception)
        self.assertIn("alice", message)
        self.assertIn("bob", message)
        self.assertNotIn("still here: carol", message)
        self.assertEqual(self.server.tables.get("messages", []), [])


class StoreTest(ServerTest):
    def test_the_audience_is_persisted(self):
        self.say("hi", audience="bob")
        self.assertEqual(self.server.tables["messages"][0]["audience"], "bob")

    def test_an_unaddressed_message_stores_null(self):
        """NULL means 'the room decides' and must stay distinguishable from
        the sentinels — storing '*' here would silently make broadcast rooms
        wake everyone."""
        self.say("hi")
        self.assertIsNone(self.server.tables["messages"][0]["audience"])

    def test_it_reports_who_was_actually_LISTENING(self):
        """Woken and reachable-right-now are different facts. A member with no
        waker is still woken in the audience sense — it just picks the message
        up later — so the send says how many doorbells actually rang."""
        real = cli.ring
        cli.ring = lambda channel, who: who == "bob"
        try:
            out = self.say("hi")
        finally:
            cli.ring = real
        self.assertIn("rang 1 listening now: bob", out)

    def test_it_says_nothing_about_ringing_when_nobody_is_listening(self):
        """The common case. A line reporting zero would be noise on every
        message sent while the other agents are working."""
        real = cli.ring
        cli.ring = lambda channel, who: False
        try:
            out = self.say("hi")
        finally:
            cli.ring = real
        self.assertNotIn("rang", out)

    def test_the_sender_is_told_who_it_woke(self):
        """An agent that cannot see who it just interrupted cannot learn to
        interrupt fewer people."""
        self.assertIn("wakes bob, carol", self.say("hi"))

    def test_the_sender_is_told_who_it_only_reached_passively(self):
        out = self.say("hi", audience="bob")
        self.assertIn("wakes bob", out)
        self.assertIn("passive for carol", out)

    def test_waking_nobody_says_so_and_says_they_still_see_it(self):
        """'wakes nobody' alone reads as 'went nowhere'."""
        out = self.say("hi", audience=cli.AUDIENCE_NONE)
        self.assertIn("wakes nobody", out)
        self.assertIn("when next working", out)

    def test_the_sender_is_never_counted_among_the_woken(self):
        """Only the reach line — the confirmation above it legitimately names
        the sender, and asserting over the whole output caught that instead."""
        reach = self.say("hi").splitlines()[1]
        self.assertNotIn("alice", reach)
        self.assertIn("bob", reach)

    def test_an_empty_room_says_so_rather_than_reporting_zero_woken(self):
        self.server.channel("empty")
        self.server.membership("empty", "alice")
        out = io.StringIO()
        with redirect_stdout(out):
            cli.do_say("srv", "empty", "alice", "hi", None)
        self.assertIn("nobody else is in this room", out.getvalue())


class PendingTest(ServerTest):
    def pending(self, identity):
        out = io.StringIO()
        with redirect_stdout(out):
            cli.do_pending("srv", "room", identity)
        return json.loads(out.getvalue())

    def test_it_reports_what_is_waiting(self):
        self.say("one")
        self.assertEqual(self.pending("bob")["pending"], 1)

    def test_it_does_NOT_consume(self):
        """The property the whole design rests on. If deciding whether to wake
        advanced the cursor, a passive message would be claimed and dropped and
        the wallflower would never see it — silent loss, which this project has
        already shipped once."""
        self.say("one")
        before = self.server.get_membership("room", "bob")["seen_seq"]
        self.pending("bob")
        self.assertEqual(self.server.get_membership("room", "bob")["seen_seq"],
                         before)

    def test_it_can_be_called_repeatedly_with_the_same_answer(self):
        """The waker calls this every poll. An answer that changed on the
        second call would mean it had consumed something."""
        self.say("one")
        self.assertEqual(self.pending("bob"), self.pending("bob"))

    def test_wakes_me_is_true_for_an_ordinary_unaddressed_message(self):
        self.say("one")
        self.assertTrue(self.pending("bob")["wakes_me"])

    def test_wakes_me_is_false_for_someone_elses_ping(self):
        self.say("one", audience="carol")
        info = self.pending("bob")
        self.assertFalse(info["wakes_me"])
        self.assertEqual(info["pending"], 1, "it is still DELIVERED to bob")

    def test_one_addressed_message_among_passive_ones_wakes(self):
        self.say("noise", audience=cli.AUDIENCE_NONE)
        self.say("for you", audience="bob")
        self.assertTrue(self.pending("bob")["wakes_me"])

    def test_it_never_reports_my_own_messages(self):
        """Otherwise an agent wakes itself and answers its own message, which
        looks exactly like a conversation."""
        self.say("mine", identity="bob")
        self.assertEqual(self.pending("bob")["pending"], 0)

    def test_it_names_who_addressed_me(self):
        self.say("for you", audience="bob")
        self.assertEqual(self.pending("bob")["messages"][0]["from"], "alice")

    def test_messages_come_back_in_order(self):
        for n in range(3):
            self.say("m%d" % n)
        seqs = [m["seq"] for m in self.pending("bob")["messages"]]
        self.assertEqual(seqs, sorted(seqs))

    def test_a_non_member_is_refused(self):
        with self.assertRaises(SystemExit):
            self.pending("stranger")

    def test_a_missing_room_is_refused(self):
        self.server.membership("ghost", "bob")
        with self.assertRaises(SystemExit) as caught:
            cli.do_pending("srv", "ghost", "bob")
        self.assertIn("no such channel", str(caught.exception))


class JsonReadTest(ServerTest):
    """`read --json`, which exists because a rendering is not a format.

    Reported against a consumer that split the transcript on '^[name]': a shell
    test inside a learning became a phantom speaker, and since the own-post
    filter is identity-based a phantom name PASSES it — so half of somebody's
    own learning came back to them as another agent's."""

    def read_json(self, identity, **kw):
        out = io.StringIO()
        with redirect_stdout(out):
            cli.do_read("srv", "room", identity, as_json=True, **kw)
        return json.loads(out.getvalue())

    def test_one_record_per_message_however_it_is_punctuated(self):
        self.say('stamp it:\n[ "$(git rev-parse HEAD)" = "$SRC" ]\nnot the ref')
        records = self.read_json("bob")
        self.assertEqual(len(records), 1)
        self.assertIn("rev-parse", records[0]["text"])

    def test_the_body_is_carried_verbatim(self):
        body = "line one\n\n[INFO] two\n[tool.poetry]"
        self.say(body)
        self.assertEqual(self.read_json("bob")[0]["text"], body)

    def test_it_carries_sender_audience_and_seq(self):
        self.say("hi", audience="bob")
        record = self.read_json("bob")[0]
        self.assertEqual(record["from"], "alice")
        self.assertEqual(record["audience"], "bob")
        self.assertEqual(record["seq"], 1)

    def test_mine_is_a_flag_not_a_marker_in_the_text(self):
        """A consumer matching '(you)' in the rendering treats a message that
        merely mentions it as its own."""
        self.say("the docs say (you) here", identity="alice")
        self.assertFalse(self.read_json("bob")[0]["mine"])

    def test_my_own_messages_are_flagged_under_all(self):
        self.say("mine", identity="bob")
        records = self.read_json("bob", all_messages=True)
        self.assertTrue(records[0]["mine"])

    def test_it_still_advances_the_cursor(self):
        """--json changes the FORMAT, not the semantics. A consumer switching
        to it must not silently stop consuming."""
        self.say("one")
        self.read_json("bob")
        self.assertEqual(
            self.server.get_membership("room", "bob")["seen_seq"], 1)

    def test_peek_still_does_not(self):
        self.say("one")
        self.read_json("bob", peek=True)
        self.assertEqual(
            self.server.get_membership("room", "bob")["seen_seq"], 0)

    def test_an_empty_room_is_an_empty_list_not_prose(self):
        """'nothing new in room' is not JSON, and a consumer that has to
        special-case it is back to parsing prose."""
        self.assertEqual(self.read_json("bob"), [])


class DispatchTest(ServerTest):
    """`pending` reachable as a command. The waker shells out to it, so a verb
    wired into argparse but not into dispatch would leave every agent deciding
    it was never addressed — silently, and forever."""

    def test_read_json_is_reachable_as_a_flag(self):
        self.say("hi")
        argv = sys.argv
        sys.argv = ["llm_chat", "read", "room", "--as", "bob", "--json"]
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                self.assertEqual(cli.main(), 0)
        finally:
            sys.argv = argv
        self.assertEqual(json.loads(out.getvalue())[0]["from"], "alice")

    def test_the_verb_reaches_do_pending(self):
        self.say("for bob", audience="bob")
        argv = sys.argv
        sys.argv = ["llm_chat", "pending", "room", "--as", "bob"]
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                self.assertEqual(cli.main(), 0)
        finally:
            sys.argv = argv
        self.assertTrue(json.loads(out.getvalue())["wakes_me"])


class RenderTest(ServerTest):
    def read(self, identity):
        out = io.StringIO()
        with redirect_stdout(out):
            cli.do_read("srv", "room", identity)
        return out.getvalue()

    def test_a_message_addressed_to_me_says_so(self):
        self.say("look here", audience="bob")
        self.assertIn("→ you", self.read("bob"))

    def test_a_message_addressed_to_someone_else_names_them(self):
        self.say("look here", audience="carol")
        self.assertIn("→ carol", self.read("bob"))

    def test_an_unaddressed_message_carries_no_arrow(self):
        """Most messages. An arrow on every line would make the marked ones
        stop standing out, which is the only reason to have it."""
        self.say("ordinary")
        self.assertNotIn("→", self.read("bob"))

    def test_everyone_reads_as_everyone(self):
        self.say("all hands", audience=cli.AUDIENCE_ALL)
        self.assertIn("→ everyone", self.read("bob"))


if __name__ == "__main__":
    unittest.main()
