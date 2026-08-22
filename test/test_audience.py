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
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

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


class AbbreviationTest(unittest.TestCase):
    """`--to-a` must not silently mean `--to-all` (#23).

    argparse allows abbreviations by default, and this surface is the worst
    place for that: `--to`, `--to-all` and `--to-none` decide who gets
    interrupted and share a prefix, so `--to-a` and `--to-n` were opposite
    outcomes one keystroke apart, resolved silently. `audience_for` already
    refuses two audience flags TOGETHER because "silently preferring one
    leaves the sender holding a wrong belief about who they just woke" —
    abbreviation reintroduced exactly that one level lower, where that refusal
    cannot see it.

    Found through the mutation harness rather than by reading: the rename of
    `--peek` to `--peek-at` was never caught, because `--peek` parses as a
    prefix of `--peek-at`.
    """

    def parse(self, argv):
        parser = cli.build_parser()
        err = io.StringIO()
        with redirect_stderr(err):
            try:
                return None, parser.parse_args(argv)
            except SystemExit:
                return err.getvalue(), None

    def test_TO_A_DOES_NOT_MEAN_TO_ALL(self):
        message, args = self.parse(["say", "room", "hi", "--to-a"])
        self.assertIsNone(args, "--to-a was accepted")
        self.assertIn("--to-a", message)

    def test_TO_N_DOES_NOT_MEAN_TO_NONE(self):
        message, args = self.parse(["say", "room", "hi", "--to-n"])
        self.assertIsNone(args, "--to-n was accepted")

    def test_the_refusal_names_what_you_probably_meant(self):
        """Turning abbreviations off is only an improvement if the error says
        what you meant, or it has just moved the confusion. The bare argparse
        message is `unrecognized arguments: --to-a` and stops there."""
        message, _ = self.parse(["say", "room", "hi", "--to-a"])
        self.assertIn("--to-all", message)
        self.assertIn("abbreviations are OFF", message)

    def test_the_hint_reaches_flags_defined_on_SUBCOMMANDS(self):
        """`unrecognized arguments` is raised by the TOP-LEVEL parser — the
        subparser consumed what it recognised and handed the rest back — so a
        lookup over the top parser's own actions sees only `--server` and
        matches nothing. Measured: the refusal fired and the hint did not."""
        message, _ = self.parse(["read", "room", "--pe"])
        self.assertIn("--peek", message)

    def test_the_full_flags_still_work(self):
        """Paired, and the point of the whole change: refusing everything
        would pass every test above and break the tool."""
        _, args = self.parse(["say", "room", "hi", "--to-all"])
        self.assertTrue(args.to_all)
        _, args = self.parse(["say", "room", "hi", "--to-none"])
        self.assertTrue(args.to_none)

    def test_SUBPARSERS_get_it_too_not_just_the_top_level(self):
        """Every flag that matters lives on a subparser, so setting
        allow_abbrev on the root alone left `--to-a` still meaning
        `--to-all`. Measured that way before `Parser.__init__` carried the
        default — which is why it is on the CLASS rather than at each
        `add_parser` call, a list whose next entry is the one that forgets."""
        import argparse
        parser = cli.build_parser()
        subs = [a for a in parser._actions
                if isinstance(a, argparse._SubParsersAction)]
        self.assertTrue(subs, "the CLI has no subcommands")
        for name, sub in subs[0].choices.items():
            with self.subTest(verb=name):
                self.assertFalse(sub.allow_abbrev,
                                 "%s still abbreviates" % name)


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
        # A THROWAWAY PROJECT. `do_say` writes a room hint and `do_read` takes
        # the read lock, both as FILES — so without this they land in whatever
        # repo the suite is run from, under the live session id inherited from
        # the environment. It never showed here because `.llm_chat/hint.room`
        # and that lock already exist on any machine that has used llm_chat;
        # the first cold-clone run reported the suite modifying the repo it
        # tests.
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CLAUDE_PROJECT_DIR"] = self.tmp.name
        self.chan = self.server.channel("room")
        for who in ("alice", "bob", "carol"):
            self.server.membership("room", who)

        # THE HOST IS NOT ASKED IN A UNIT TEST. `say` now consults
        # live_identities() so it can tell a wake from a message left for
        # somebody who is gone (#19), and the unstubbed version shells out to
        # `claude agents --json` — which would make every assertion in this
        # file depend on which sessions happen to be running on the machine,
        # and bob and carol are never among them. The default stub says
        # everyone in the room is alive because that is the ordinary case; the
        # tests that care about death say so explicitly.
        self.real_live = cli.live_identities
        self.alive({who: [{"sessionId": who}] for who in
                    ("alice", "bob", "carol")})

    def alive(self, live):
        cli.live_identities = lambda: live

    def tearDown(self):
        cli.call = self.real
        cli.live_identities = self.real_live
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.tmp.cleanup()

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


class EchoTest(ServerTest):
    """`say` echoes what it STORED, on the success path.

    It reported routing, cost and a sequence number and not one byte of the
    text — so a shell-mangled message and an intact one printed identically,
    and the send genuinely succeeded, so there was no error to notice.

    llms.txt has carried the incident for weeks: an agent found it only by
    re-reading its own message with `--all` after noticing a sentence had lost
    its subject. The same paragraph says `--file` "cannot remove it for a
    caller who does not use it". The design knew the gap and knew the remedy
    was caller-dependent, and the success path still said nothing.

    gameloop's general form, from the identical shape in a different verb: a
    verb that echoes its input on FAILURE but not on success has its blind
    spot exactly where a corrupted input becomes a believed result.
    """

    def test_THE_STORED_TEXT_IS_ECHOED(self):
        out = self.say("the sentence that was actually stored")
        self.assertIn("the sentence that was actually stored", out)

    def test_the_length_is_reported_too(self):
        """The first line would not show a truncation that keeps it intact;
        the count does, and costs four characters."""
        self.assertIn("(37 chars)", self.say("x" * 37))

    def test_two_messages_differing_only_in_the_BODY_print_differently(self):
        """The property, stated as the thing that failed. Routing, cost and
        seq are identical for a mangled message and an intact one — that is
        precisely why the old output could not tell them apart."""
        intact = self.say("the check fires when nothing went wrong")
        mangled = self.say("the check  fires when nothing went wrong")
        self.assertNotEqual(intact, mangled)

    def test_only_the_FIRST_line_is_echoed(self):
        """These rooms cost context and this is the sender's own output. One
        line is what catches the reported incident; the whole body would
        double every send."""
        out = self.say("first line here\nsecond line must not appear")
        self.assertIn("first line here", out)
        self.assertNotIn("second line must not appear", out)

    def test_a_long_first_line_is_truncated_rather_than_dropped(self):
        """Dropped, a long opening sentence would leave exactly the messages
        most worth checking with nothing echoed at all."""
        out = self.say("y" * 200)
        self.assertIn("y" * 40, out)
        self.assertIn("…", out)

    def test_an_empty_message_does_not_print_an_empty_stored_line(self):
        """Nothing was stored worth showing, and a blank `stored:` reads as a
        message that vanished."""
        out = self.say("   ")
        self.assertNotIn("stored:", out)

    def test_the_routing_line_still_says_everything_it_did(self):
        """Added to, not replaced. The blast radius is why this output exists
        and the echo must not have cost it."""
        out = self.say("hi")
        self.assertIn("sent #", out)
        self.assertIn("wakes", out)


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
        cli.ring = lambda channel, who, server=None: who == "bob"
        try:
            out = self.say("hi")
        finally:
            cli.ring = real
        self.assertIn("rang 1 listening now: bob", out)

    def test_it_says_nothing_about_ringing_when_nobody_is_listening(self):
        """The common case. A line reporting zero would be noise on every
        message sent while the other agents are working."""
        real = cli.ring
        cli.ring = lambda channel, who, server=None: False
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

    def test_a_message_to_a_dead_identity_is_not_reported_as_a_wake(self):
        """#19: an orchestrator nudged one agent three times over an hour
        because `say --to` said "wakes lead-ml" for a session that had ended
        four days earlier. The message is still worth leaving — it just must
        not be worded like a delivery that reached somebody."""
        self.alive({"alice": [{}], "carol": [{}]})
        out = self.say("hi", audience="bob")
        self.assertIn("LEFT FOR bob", out)
        self.assertNotIn("wakes bob", out)

    def test_a_dead_addressee_is_worded_differently_from_a_live_one(self):
        """The two states have to be told apart from the line alone. Asserting
        the dead wording in isolation would pass a version that printed it for
        everybody."""
        self.alive({"alice": [{}], "bob": [{}], "carol": [{}]})
        living = self.say("hi", audience="bob")
        self.alive({"alice": [{}], "carol": [{}]})
        dead = self.say("hi", audience="bob")
        self.assertNotEqual(living, dead)
        self.assertIn("wakes bob", living)
        self.assertNotIn("LEFT FOR", living)

    def test_the_live_ones_are_still_reported_as_woken(self):
        """A room with one dead member must not lose the fact that the others
        were genuinely interrupted."""
        self.alive({"alice": [{}], "carol": [{}]})
        out = self.say("hi", audience="bob,carol")
        self.assertIn("wakes carol", out)
        self.assertIn("LEFT FOR bob", out)

    def test_an_unaskable_host_does_not_become_a_liveness_claim(self):
        """None means "could not look", and turning that into "nobody is
        alive" would invent in the same direction the bug ran, just louder."""
        self.alive(None)
        out = self.say("hi", audience="bob")
        self.assertIn("wakes bob", out)
        self.assertNotIn("LEFT FOR", out)

    def test_a_dead_addressee_says_nobody_was_woken(self):
        """The sender's next action turns on this. "LEFT FOR bob" alone still
        reads as delivery to somebody who is merely busy."""
        self.alive({"alice": [{}]})
        out = self.say("hi", audience="bob")
        self.assertIn("no live session", out)

    def test_the_message_is_still_stored_for_a_dead_addressee(self):
        """Not a refusal. Leaving a note for an agent that will resume is most
        of the point of a transcript."""
        self.alive({"alice": [{}]})
        self.say("hi", audience="bob")
        self.assertEqual(self.server.tables["messages"][0]["audience"], "bob")

    def reach_line(self, out):
        """The line about who was woken, found by CONTENT not by index.

        This was `splitlines()[1]`, which is a claim about layout rather than
        about the reach line — and it broke the moment `say` gained a line
        above it. The test's reasoning was always "only the reach line,
        because the confirmation legitimately names the sender"; that is a
        statement about which line, so it should select one.
        """
        for line in out.splitlines():
            if any(w in line for w in ("wakes", "passive", "nobody else")):
                return line
        self.fail("no reach line in:\n%s" % out)

    def test_the_sender_is_never_counted_among_the_woken(self):
        """Only the reach line — the confirmation above it legitimately names
        the sender, and asserting over the whole output caught that instead."""
        reach = self.reach_line(self.say("hi"))
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
