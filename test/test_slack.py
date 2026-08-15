"""The Slack bridge, against a fake Slack.

The loop can eat itself in two directions and only one of them is already
guarded, so both are asserted here as a pair. llm_chat's self-filter stops a
relayed answer being relayed back out; nothing in Slack stops the bridge's own
posts returning, and that check is all that separates this from a message loop
that wakes every agent in the room forever.
"""
import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import load  # noqa: E402


class FakeSlack:
    """Stands in for the Slack API, recording what was posted."""

    def __init__(self, messages=None, ok=True, error=None, explode=False):
        self.posted = []
        self.threaded = []
        self.messages = messages or []
        self.ok = ok
        self.error = error
        self.explode = explode

    def post(self, text, thread_ts=None):
        if self.explode:
            raise OSError("slack is down")
        self.posted.append(text)
        self.threaded.append(thread_ts)
        return {"ok": True}

    def history(self, oldest=None):
        if self.explode:
            raise OSError("slack is down")
        if not self.ok:
            return {"ok": False, "error": self.error}
        if oldest is None:
            return {"ok": True, "messages": list(self.messages)}
        return {"ok": True, "messages": [m for m in self.messages
                                         if float(m["ts"]) > float(oldest)]}


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mod = load("llm-chat-slack")
        self.mod.STATE = self.tmp.name
        # EVERY state path, not just the ones a given test reads. `pump_in`
        # now records who was asked in which thread, so a test that only meant
        # to exercise routing began writing into the REAL repo's .llm_chat/ —
        # caught by the suite's own repo-damage guard rather than by review,
        # which is precisely what that guard is for. A module-level path left
        # unpatched is not inert; it is the live one.
        self.mod.CURSOR = os.path.join(self.tmp.name, "slack-cursor.json")
        self.mod.THREADS = os.path.join(self.tmp.name, "slack-threads.json")
        self.mod.REPLIES = os.path.join(self.tmp.name, "slack-replies.json")
        self.mod.ASKED = os.path.join(self.tmp.name, "slack-asked.json")
        self.config = {"room": "human", "identity": "me",
                       "slack": {"bot_token": "x", "channel": "C1"}}
        self.said = []
        self.mod.say = lambda room, identity, text, addressing=None, thread=None: (
            self.said.append((room, identity, text, addressing, thread)) or True)
        # The room-still-exists lookup shells out to the CLI, so without a stub
        # every loop test queries the LIVE server. It answered — with the real
        # machine's rooms, none of them named `human` — so the bridge correctly
        # concluded its room was deleted and stopped, and three unrelated tests
        # failed with "KeyboardInterrupt not raised". A suite that reaches the
        # running system is not testing the code, it is testing the machine.
        self.mod.subprocess = type("S", (), {"run": staticmethod(
            lambda *a, **k: type("R", (), {
                "returncode": 0, "stderr": "",
                "stdout": json.dumps([{"name": self.config["room"]}])})())})

    def tearDown(self):
        self.tmp.cleanup()

    def quiet(self, fn, *a):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = fn(*a)
        return result, out.getvalue() + err.getvalue()

    # ── the loop that would eat itself ──────────────────────────────────────
    def test_the_bridges_own_posts_are_never_relayed_back(self):
        """Without this every relay returns from Slack, is posted into
        llm_chat, wakes the room, and does it again — forever."""
        slack = FakeSlack([
            {"ts": "1", "text": "[builder] a relayed question", "bot_id": "B1"},
            {"ts": "2", "text": "an actual human answer", "user": "U1"},
        ])
        self.quiet(self.mod.pump_in, self.config, slack)
        self.assertEqual([t for _, _, t, _a, _th in self.said], ["an actual human answer"])

    def test_bot_subtype_is_also_treated_as_the_bridge(self):
        slack = FakeSlack([{"ts": "1", "text": "relayed",
                            "subtype": "bot_message"}])
        self.quiet(self.mod.pump_in, self.config, slack)
        self.assertEqual(self.said, [])

    def test_a_human_message_reaches_llm_chat_as_the_human(self):
        """Paired with the test above: a filter that dropped everything would
        pass that one and be useless."""
        slack = FakeSlack([{"ts": "5", "text": "yes, ship it", "user": "U1"}])
        self.quiet(self.mod.pump_in, self.config, slack)
        # --to-none because it is a TOP-LEVEL Slack message: the human thinking
        # out loud in the channel should not pull every agent off its work.
        # The thread stamp is the message's OWN ts for a top-level post, which
        # is what lets an agent open a thread ON it rather than reply beside it.
        self.assertEqual(self.said,
                         [("human", "me", "yes, ship it", ["--to-none"], "5")])

    def test_joins_and_other_events_without_text_are_ignored(self):
        slack = FakeSlack([{"ts": "1", "user": "U1", "subtype": "channel_join"}])
        self.quiet(self.mod.pump_in, self.config, slack)
        self.assertEqual(self.said, [])

    # ── a question for the BRIDGE, not for the room ─────────────────────────
    def test_A_BRIDGE_COMMAND_IS_ANSWERED_AND_NEVER_RELAYED(self):
        """The whole point: a human who has to wake five agents to find out
        which one to wake has not been helped. Nothing reaches llm_chat, so
        nobody spends a turn on it."""
        real = self.mod.members_of
        self.mod.members_of = lambda room: ("build", "baccompat")
        try:
            slack = FakeSlack([{"ts": "5", "text": "@llm_chat list",
                                "user": "U1"}])
            self.quiet(self.mod.pump_in, self.config, slack)
        finally:
            self.mod.members_of = real
        self.assertEqual(self.said, [])
        self.assertTrue(any("build" in text for text in slack.posted))

    def test_the_cursor_still_advances_past_a_bridge_command(self):
        """Otherwise it is answered again on every poll, forever."""
        real = self.mod.members_of
        self.mod.members_of = lambda room: ("build",)
        try:
            slack = FakeSlack([{"ts": "7", "text": "@llm_chat list",
                                "user": "U1"}])
            self.quiet(self.mod.pump_in, self.config, slack)
            self.assertEqual(self.mod.read_cursor(), "7")
            self.quiet(self.mod.pump_in, self.config, slack)
        finally:
            self.mod.members_of = real
        self.assertEqual(len(slack.posted), 1)

    def test_AN_UNKNOWN_BRIDGE_VERB_STILL_REACHES_THE_ROOM(self):
        """A typo must not vanish into a bridge that thought it was for
        itself. `@llm_chat lsit` is relayed like any other message."""
        slack = FakeSlack([{"ts": "5", "text": "@llm_chat lsit", "user": "U1"}])
        self.quiet(self.mod.pump_in, self.config, slack)
        self.assertEqual([t for _, _, t, _a, _th in self.said],
                         ["@llm_chat lsit"])

    def test_MEMBERS_ARE_LOOKED_UP_ONCE_PER_POLL_not_per_message(self):
        """It is a subprocess. A busy channel would otherwise pay for it on
        every line."""
        calls = []
        real = self.mod.members_of
        self.mod.members_of = lambda room: calls.append(room) or ("build",)
        try:
            slack = FakeSlack([{"ts": "1", "text": "one", "user": "U1"},
                               {"ts": "2", "text": "two", "user": "U1"},
                               {"ts": "3", "text": "three", "user": "U1"}])
            self.quiet(self.mod.pump_in, self.config, slack)
        finally:
            self.mod.members_of = real
        self.assertEqual(len(calls), 1)

    # ── the cursor ──────────────────────────────────────────────────────────
    def test_a_message_is_relayed_once_and_not_again(self):
        slack = FakeSlack([{"ts": "5", "text": "answer", "user": "U1"}])
        self.quiet(self.mod.pump_in, self.config, slack)
        self.quiet(self.mod.pump_in, self.config, slack)
        self.assertEqual(len(self.said), 1)

    def test_the_cursor_advances_past_bot_messages_too(self):
        """Otherwise the bridge re-reads its own post every cycle forever,
        paying a Slack call each time to decide to ignore it again."""
        slack = FakeSlack([{"ts": "9", "text": "relayed", "bot_id": "B1"}])
        self.quiet(self.mod.pump_in, self.config, slack)
        self.assertEqual(self.mod.read_cursor(), "9")

    def test_messages_are_relayed_oldest_first(self):
        slack = FakeSlack([
            {"ts": "3", "text": "second", "user": "U1"},
            {"ts": "1", "text": "first", "user": "U1"},
        ])
        self.quiet(self.mod.pump_in, self.config, slack)
        self.assertEqual([t for _, _, t, _a, _th in self.said], ["first", "second"])

    # ── outbound ────────────────────────────────────────────────────────────
    def test_agent_messages_are_relayed_to_slack(self):
        self.mod.waiting_for_human = lambda room, identity: [
            ("builder", "should I force-push?", None)]
        slack = FakeSlack()
        count, _ = self.quiet(self.mod.pump_out, self.config, slack)
        self.assertEqual(slack.posted, ["*builder*: should I force-push?"])
        self.assertEqual(count, 1)

    def test_a_slack_outage_reports_the_lost_message_rather_than_dropping_it(self):
        """The message is already off llm_chat's cursor by then, so a silent
        failure loses it with nobody able to tell."""
        self.mod.waiting_for_human = lambda room, identity: [
            ("builder", "hello", None)]
        _, text = self.quiet(self.mod.pump_out, self.config,
                             FakeSlack(explode=True))
        self.assertIn("LOST", text)
        self.assertIn("hello", text)

    def test_nothing_waiting_posts_nothing(self):
        self.mod.waiting_for_human = lambda room, identity: []
        slack = FakeSlack()
        self.quiet(self.mod.pump_out, self.config, slack)
        self.assertEqual(slack.posted, [])

    def test_an_unreachable_slack_does_not_raise_on_the_way_in(self):
        count, _ = self.quiet(self.mod.pump_in, self.config,
                              FakeSlack(explode=True))
        self.assertEqual(count, 0)

    def test_a_refusal_from_slack_is_reported_not_swallowed(self):
        _, text = self.quiet(self.mod.pump_in, self.config,
                             FakeSlack(ok=False, error="not_in_channel"))
        self.assertIn("not_in_channel", text)


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mod = load("llm-chat-slack")
        self.mod.CONFIG = os.path.join(self.tmp.name, "slack.json")
        self.mod.FALLBACK = os.path.join(self.tmp.name, "notify.json")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, path, payload):
        with open(path, "w") as f:
            json.dump(payload, f)

    def test_no_config_at_all_reads_as_none(self):
        self.assertIsNone(self.mod.load_config())

    def test_a_complete_config_is_accepted(self):
        self.write(self.mod.CONFIG, {"room": "r", "identity": "i",
                                     "slack": {"bot_token": "t",
                                               "channel": "C"}})
        self.assertNotIn("missing", self.mod.load_config())

    def test_credentials_fall_back_to_game_loops_notify_config(self):
        """A machine that configured Slack once should not do it twice."""
        self.write(self.mod.CONFIG, {"room": "r", "identity": "i"})
        self.write(self.mod.FALLBACK, {"slack": {"bot_token": "t",
                                                 "channel": "C"}})
        config = self.mod.load_config()
        self.assertNotIn("missing", config)
        self.assertEqual(config["slack"]["bot_token"], "t")

    def test_the_bridges_own_config_wins_over_the_fallback(self):
        self.write(self.mod.CONFIG, {"room": "r", "identity": "i",
                                     "slack": {"bot_token": "mine",
                                               "channel": "C"}})
        self.write(self.mod.FALLBACK, {"slack": {"bot_token": "theirs",
                                                 "channel": "D"}})
        self.assertEqual(self.mod.load_config()["slack"]["bot_token"], "mine")

    def test_what_is_missing_is_named_individually(self):
        self.write(self.mod.CONFIG, {"room": "r"})
        missing = self.mod.load_config()["missing"]
        self.assertIn("identity", missing)
        self.assertIn("slack.bot_token", missing)

    def test_a_corrupt_config_reads_as_none_rather_than_crashing(self):
        with open(self.mod.CONFIG, "w") as f:
            f.write("{not json")
        self.assertIsNone(self.mod.load_config())


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.mod = load("llm-chat-slack")
        # --check now exercises the llm_chat read too. Readable by default so
        # each test states only the thing it is about; the read-side cases set
        # it themselves.
        self.mod.waiting_for_human = lambda room, identity: []

    def report(self, config, slack=None):
        if slack is not None:
            self.mod.Slack = lambda *a, **kw: slack
        out = io.StringIO()
        with redirect_stdout(out):
            code = self.mod.check(config)
        return code, out.getvalue()

    def test_no_config_explains_the_shape_to_write(self):
        code, text = self.report(None)
        self.assertEqual(code, 1)
        self.assertIn("bot_token", text)

    def test_an_incomplete_config_names_the_gaps(self):
        code, text = self.report({"missing": ["identity", "slack.channel"]})
        self.assertEqual(code, 1)
        self.assertIn("identity", text)

    def test_a_missing_scope_says_to_reinstall(self):
        """A token keeps its old scopes until the app is reinstalled, which is
        the step people skip and then cannot explain."""
        config = {"room": "r", "identity": "i",
                  "slack": {"bot_token": "t", "channel": "C"}}
        code, text = self.report(config, FakeSlack(ok=False,
                                                   error="missing_scope"))
        self.assertEqual(code, 1)
        self.assertIn("REINSTALL", text)

    def test_not_in_channel_says_membership_is_separate_from_scope(self):
        config = {"room": "r", "identity": "i",
                  "slack": {"bot_token": "t", "channel": "C"}}
        _, text = self.report(config, FakeSlack(ok=False,
                                                error="not_in_channel"))
        self.assertIn("invite the bot", text)

    def test_a_working_wiring_says_so_and_sends_nothing(self):
        config = {"room": "r", "identity": "i",
                  "slack": {"bot_token": "t", "channel": "C"}}
        slack = FakeSlack()
        code, text = self.report(config, slack)
        self.assertEqual(code, 0)
        self.assertIn("reachable", text)
        self.assertIn("llm_chat readable", text)
        self.assertEqual(slack.posted, [], "--check must not post")

    def test_A_HEALTHY_SLACK_HALF_IS_NOT_A_PASS(self):
        """Issue #1. --check validated Slack and then announced
        'bridging <room> as <identity>' without ever asking whether that
        identity can read that room — so the one command whose entire purpose
        is 'is the wiring live?' passed with half the wiring dead."""
        self.mod.waiting_for_human = lambda room, identity: None
        config = {"room": "r", "identity": "i",
                  "slack": {"bot_token": "t", "channel": "C"}}
        code, text = self.report(config, FakeSlack())
        self.assertEqual(code, 1)
        self.assertIn("CANNOT READ", text)

    def test_it_names_the_join_that_usually_fixes_it(self):
        """A refusal without the remedy is an obstacle. The measured cause was
        an identity that had never joined."""
        self.mod.waiting_for_human = lambda room, identity: None
        code, text = self.report({"room": "r", "identity": "i",
                                  "slack": {"bot_token": "t", "channel": "C"}},
                                 FakeSlack())
        self.assertIn("llm_chat join r --as i", text)

    def test_an_unreachable_slack_is_reported(self):
        config = {"room": "r", "identity": "i",
                  "slack": {"bot_token": "t", "channel": "C"}}
        code, text = self.report(config, FakeSlack(explode=True))
        self.assertEqual(code, 1)
        self.assertIn("cannot reach", text)


if __name__ == "__main__":
    unittest.main()


class SlackClientTest(unittest.TestCase):
    """The one place network happens. Stubbed at urlopen so the request that
    would have gone out is inspectable."""

    def setUp(self):
        self.mod = load("llm-chat-slack")
        self.real = self.mod.urllib.request.urlopen
        self.seen = {}

    def tearDown(self):
        self.mod.urllib.request.urlopen = self.real

    def respond(self, payload):
        class Response:
            def read(inner):
                return json.dumps(payload).encode()

            def __enter__(inner):
                return inner

            def __exit__(inner, *a):
                return False

        def capture(request, **kw):
            self.seen["url"] = request.full_url
            self.seen["data"] = request.data
            self.seen["headers"] = dict(request.headers)
            return Response()
        self.mod.urllib.request.urlopen = capture

    def test_posting_sends_json_with_the_bearer_token(self):
        self.respond({"ok": True})
        client = self.mod.Slack("xoxb-secret", "C1")
        self.assertEqual(client.post("hello"), {"ok": True})
        self.assertIn("chat.postMessage", self.seen["url"])
        self.assertEqual(json.loads(self.seen["data"])["text"], "hello")
        self.assertEqual(self.seen["headers"]["Authorization"],
                         "Bearer xoxb-secret")

    def test_posting_into_a_thread_sends_thread_ts(self):
        """The wire form of an answer landing where it was asked."""
        self.respond({"ok": True})
        client = self.mod.Slack("t", "C1")
        client.post("the answer", thread_ts="100.5")
        self.assertEqual(json.loads(self.seen["data"])["thread_ts"], "100.5")

    def test_a_root_post_carries_NO_thread_ts(self):
        """Sending a null would make Slack reject it, and sending the
        channel's own ts would bury every new topic in one thread."""
        self.respond({"ok": True})
        self.mod.Slack("t", "C1").post("new topic")
        self.assertNotIn("thread_ts", json.loads(self.seen["data"]))

    def test_replies_asks_the_ONLY_endpoint_that_returns_thread_messages(self):
        """conversations.history does not return thread replies. Not an error
        and not an empty result — a correct answer to a different question,
        which is why the primary reply path was dead and nothing complained."""
        self.respond({"ok": True, "messages": []})
        client = self.mod.Slack("t", "C1")
        client.replies("100.0")
        self.assertIn("conversations.replies", self.seen["url"])
        self.assertIn("ts=100.0", self.seen["url"])
        self.assertIsNone(self.seen["data"], "must be a query, not a body")

    def test_history_is_a_query_not_a_body(self):
        self.respond({"ok": True, "messages": []})
        client = self.mod.Slack("t", "C1")
        client.history()
        self.assertIn("conversations.history", self.seen["url"])
        self.assertIn("channel=C1", self.seen["url"])
        self.assertIsNone(self.seen["data"])

    def test_history_passes_the_cursor_as_oldest(self):
        self.respond({"ok": True, "messages": []})
        self.mod.Slack("t", "C1").history(oldest="123.45")
        self.assertIn("oldest=123.45", self.seen["url"])

    def test_the_api_base_is_overridable(self):
        self.respond({"ok": True})
        self.mod.Slack("t", "C1", api="http://localhost:9/api").post("x")
        self.assertTrue(self.seen["url"].startswith("http://localhost:9/api"))


class CliSeamTest(unittest.TestCase):
    """The bridge shells out to the CLI rather than querying the store, so
    there is ONE reader of the self-filter rather than two."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mod = load("llm-chat-slack")
        self.mod.STATE = self.tmp.name
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def stub(self, stdout="", explode=False, returncode=0):
        outer = self

        class Fake:
            @staticmethod
            def run(argv, **kw):
                outer.calls.append(argv)
                if explode:
                    raise OSError("cli missing")

                class Result:
                    pass
                Result.stdout = stdout
                Result.stderr = ""
                Result.returncode = returncode
                return Result
        self.mod.subprocess = Fake

    def test_it_reads_through_the_cli_as_the_human(self):
        self.stub('[{"seq": 1, "from": "builder", "text": "a question", '
                  '"audience": null, "mine": false}, '
                  '{"seq": 2, "from": "reviewer", "text": "another", '
                  '"audience": null, "mine": false}]')
        lines = self.mod.waiting_for_human("human", "me")
        self.assertEqual(len(lines), 2)
        argv = self.calls[0]
        self.assertIn("read", argv)
        self.assertIn("--as", argv)
        self.assertNotIn("--all", argv,
                         "--all disables the self-filter and would relay the "
                         "human's own answers straight back to Slack")

    def test_nothing_new_is_not_a_message(self):
        """MEASURED against the real CLI rather than imagined: `read --json`
        with nothing waiting prints `[]` and exits 0. This fixture used to
        stub the PROSE line ("nothing new in human") with a zero exit, which
        the CLI never emits on --json — a fixture written from the same belief
        as the code it was guarding, and wrong about both."""
        self.stub("[]\n")
        self.assertEqual(self.mod.waiting_for_human("human", "me"), [])

    def test_A_FAILED_READ_IS_NOT_AN_EMPTY_ROOM(self):
        """Issue #1, and the reason it was silent for a whole session.

        Measured shape of the real failure: the identity had never joined, so
        the CLI printed prose on stdout and exited 1. Folding that into `[]`
        made "I could not look" indistinguishable from "nobody has said
        anything" — on the one path where the cost is a person waiting.

        THE STDOUT HERE IS VALID JSON ON PURPOSE, and the test was worthless
        without that. With prose on stdout, deleting the exit-code check still
        yields None — the JSON parse fails and returns None one line further
        down. Two routes to the same answer, so the assertion could not tell
        them apart, and this mutation SURVIVED the first sweep that actually
        ran tests. Valid JSON with a non-zero exit is the only fixture where
        the check is the thing being measured."""
        self.stub("[]\n", returncode=1)
        self.assertIsNone(
            self.mod.waiting_for_human("human", "me"),
            "a non-zero exit with parseable output was read as an empty room")

    def test_a_failed_read_that_printed_PROSE_is_also_not_empty(self):
        """The realistic shape, kept beside the discriminating one: this is
        what the live bridge actually produced for a whole session."""
        self.stub("me has not joined human\n", returncode=1)
        self.assertIsNone(self.mod.waiting_for_human("human", "me"))

    def test_unparseable_output_is_also_not_an_empty_room(self):
        self.stub("something unexpected", returncode=0)
        self.assertIsNone(self.mod.waiting_for_human("human", "me"))

    def test_a_broken_cli_does_not_take_the_bridge_down(self):
        """Still true, and still the point — but the honest answer to "the CLI
        is missing" is "I could not look", not "the room is empty"."""
        self.stub(explode=True)
        self.assertIsNone(self.mod.waiting_for_human("human", "me"))

    def test_it_sends_via_file_so_the_shell_cannot_eat_the_text(self):
        """A Slack message can contain anything, including backticks — which a
        command line would substitute away before the CLI ever saw them."""
        self.stub()
        self.assertTrue(self.mod.say("human", "me", "run `date` now"))
        argv = self.calls[0]
        self.assertIn("--file", argv)
        path = argv[argv.index("--file") + 1]
        with open(path) as f:
            self.assertEqual(f.read(), "run `date` now")

    def test_a_failed_send_reports_false_rather_than_raising(self):
        self.stub(explode=True)
        self.assertFalse(self.mod.say("human", "me", "hello"))


class EntryPointTest(unittest.TestCase):
    def setUp(self):
        self.mod = load("llm-chat-slack")
        self.argv = sys.argv

    def tearDown(self):
        sys.argv = self.argv

    def run_main(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = self.mod.main()
        return code, out.getvalue()

    def test_check_reports_without_running_the_loop(self):
        sys.argv = ["llm-chat-slack", "--check"]
        self.mod.load_config = lambda: None
        code, text = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("no config", text)

    def test_an_unconfigured_bridge_explains_itself_instead_of_looping(self):
        """Starting a loop that can never work would look like it is running."""
        sys.argv = ["llm-chat-slack"]
        self.mod.load_config = lambda: {"missing": ["slack.bot_token"]}
        code, text = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("slack.bot_token", text)


class CursorFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mod = load("llm-chat-slack")
        self.mod.CURSOR = os.path.join(self.tmp.name, "c.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_cursor_reads_as_none(self):
        self.assertIsNone(self.mod.read_cursor())

    def test_a_corrupt_cursor_reads_as_none_rather_than_crashing(self):
        with open(self.mod.CURSOR, "w") as f:
            f.write("{not json")
        self.assertIsNone(self.mod.read_cursor())

    def test_writing_is_atomic(self):
        self.mod.write_cursor("123.45")
        self.assertEqual(self.mod.read_cursor(), "123.45")
        self.assertFalse(os.path.exists(self.mod.CURSOR + ".tmp"))


class LoopTest(unittest.TestCase):
    """The run loop. It never exits on its own, so the clock is the seam."""

    class Clock:
        def __init__(self, stop_after):
            self.slept = []
            self.stop_after = stop_after

        def sleep(self, seconds):
            self.slept.append(seconds)
            if len(self.slept) >= self.stop_after:
                raise KeyboardInterrupt("enough")

    def setUp(self):
        self.mod = load("llm-chat-slack")
        self.argv = sys.argv
        sys.argv = ["llm-chat-slack"]
        self.mod.load_config = lambda: {
            "room": "human", "identity": "me",
            "slack": {"bot_token": "t", "channel": "C", "poll_sec": 3}}
        self.mod.Slack = lambda *a, **kw: FakeSlack()
        self.pumped = []
        self.mod.pump_out = lambda c, s: self.pumped.append("out") or 1
        self.mod.pump_in = lambda c, s: self.pumped.append("in") or 0
        # These tests are about pump cadence. Left real, the room-exists check
        # shells out to the live CLI on every cycle, which both slows the suite
        # and — since the machine's real rooms are not named `human` — stops
        # the loop before it ever sleeps. Its own behaviour is RoomGoneTest.
        self.mod.room_is_gone = lambda config: False

    def tearDown(self):
        sys.argv = self.argv

    def test_it_pumps_both_directions_every_cycle(self):
        self.mod.time = self.Clock(stop_after=2)
        out = io.StringIO()
        with redirect_stdout(out):
            with self.assertRaises(KeyboardInterrupt):
                self.mod.main()
        self.assertEqual(self.pumped, ["out", "in", "out", "in"])

    def test_it_honours_the_configured_poll_interval(self):
        clock = self.Clock(stop_after=1)
        self.mod.time = clock
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                self.mod.main()
        self.assertEqual(clock.slept, [3])

    def test_it_reports_only_the_cycles_that_moved_something(self):
        """A bridge printing a line every ten seconds forever is a log nobody
        reads, and the one line that matters is in it somewhere."""
        self.mod.pump_out = lambda c, s: 0
        self.mod.pump_in = lambda c, s: 0
        self.mod.time = LoopTest.Clock(stop_after=3)
        out = io.StringIO()
        with redirect_stdout(out):
            with self.assertRaises(KeyboardInterrupt):
                self.mod.main()
        self.assertNotIn("-> Slack", out.getvalue())


class ThreadRepliesTest(BridgeTest):
    """Issue #2: thread replies never arrived, because `conversations.history`
    does not return them.

    The reporter's point about why the old suite could not catch it is the
    thing worth preserving: "the failure is not an error, an exception, or a
    wrong value — it is an API that returns exactly what it promises and
    simply does not contain the thing." So FakeSlack must model that split
    honestly. A fake whose history returned replies would make the bug
    untestable and the fix unnecessary, which is how it survived being written
    down as the primary path.
    """

    class Threaded:
        """history returns parents with reply_count; replies returns the rest.

        Timestamps are REAL epoch seconds, not toy values. Slack ts IS an
        epoch, and the watch window is an age computed from it — toy values
        like "100.0" are 1970 and fall outside any sane window, so a fixture
        using them tests a thread nobody would poll."""

        def __init__(self, parent_ts=None, replies=(), count=None):
            parent_ts = parent_ts or ("%.6f" % time.time())
            self.parent_ts = parent_ts
            self._replies = list(replies)
            self.count = len(self._replies) if count is None else count
            self.asked = []
            self.posted = []

        def history(self, oldest=None):
            return {"ok": True, "messages": [
                {"ts": self.parent_ts, "thread_ts": self.parent_ts,
                 "bot_id": "B1", "text": "*asker*: question",
                 "reply_count": self.count}]}

        def replies(self, ts):
            self.asked.append(ts)
            return {"ok": True, "messages": [
                {"ts": self.parent_ts, "bot_id": "B1", "text": "*asker*: question"}
            ] + self._replies}

        def post(self, text):
            self.posted.append(text)
            return {"ok": True, "ts": "1.0"}

    def human(self, offset, text):
        """A reply `offset` seconds after the parent."""
        return {"ts": "%.6f" % (self.base + offset), "thread_ts": self.parent,
                "user": "U1", "text": text}

    def setUp(self):
        super().setUp()
        self.mod.REPLIES = os.path.join(self.tmp.name, "slack-replies.json")
        self.mod.THREADS = os.path.join(self.tmp.name, "slack-threads.json")
        self.base = time.time()
        self.parent = "%.6f" % self.base
        self.mod.remember_thread(self.parent, "asker")

    def threaded(self, **kw):
        kw.setdefault("parent_ts", self.parent)
        return self.Threaded(**kw)

    def test_A_THREAD_REPLY_REACHES_THE_ROOM(self):
        """The bug, in one assertion. It was in `replies` and not in `history`,
        so nothing arrived, for every threaded reply ever sent."""
        slack = self.threaded(replies=[self.human(1, "in the thread")])
        self.quiet(self.mod.pump_in, self.config, slack)
        self.assertEqual([t for _, _, t, _, _ in self.said], ["in the thread"])

    def test_it_is_routed_to_the_agent_WHOSE_THREAD_IT_IS(self):
        """The whole reason threading is the primary path: it is the only
        structured gesture a phone gives a human."""
        slack = self.threaded(replies=[self.human(1, "answer")])
        self.quiet(self.mod.pump_in, self.config, slack)
        self.assertEqual(self.said[0][3], ["--to", "asker"])

    def test_a_reply_is_relayed_ONCE(self):
        slack = self.threaded(replies=[self.human(1, "answer")])
        self.quiet(self.mod.pump_in, self.config, slack)
        self.quiet(self.mod.pump_in, self.config, slack)
        self.assertEqual(len(self.said), 1)

    def test_a_LATER_reply_in_the_same_thread_still_arrives(self):
        slack = self.threaded(replies=[self.human(1, "first")])
        self.quiet(self.mod.pump_in, self.config, slack)
        slack._replies.append(self.human(2, "second"))
        slack.count = 2
        self.quiet(self.mod.pump_in, self.config, slack)
        self.assertEqual([t for _, _, t, _, _ in self.said], ["first", "second"])

    # ── a relay that FAILED must not move the mark ──────────────────────────
    # Reported by wcs, and the shape named by showrunner: one value answering
    # two questions — "how far have I read" and "what is safe to never re-read"
    # — which disagree in exactly the case that matters. On this channel the
    # sender is a person, so a dropped reply is somebody waiting.

    def test_A_FAILED_RELAY_DOES_NOT_ADVANCE_THE_MARK(self):
        """The advance sat outside the success branch, so a reply whose relay
        failed moved the high-water mark and was never looked at again."""
        self.mod.say = lambda *a, **kw: False
        slack = self.threaded(replies=[self.human(1, "please answer me")])
        self.quiet(self.mod.pump_in, self.config, slack)

        # It is still retryable: the mark must not have passed it.
        seen = self.mod.read_reply_state().get(self.parent, {})
        self.assertEqual(seen.get("seen_ts"), self.parent,
                         "the mark moved past a message that never arrived")

    def test_and_the_NEXT_POLL_ACTUALLY_RETRIES_IT(self):
        """Paired, and the one that matters — the mark not moving is only
        useful if the retry then happens."""
        self.mod.say = lambda *a, **kw: False
        slack = self.threaded(replies=[self.human(1, "please answer me")])
        self.quiet(self.mod.pump_in, self.config, slack)
        self.assertEqual(self.said, [])

        self.mod.say = lambda room, identity, text, addressing=None, thread=None: (
            self.said.append((room, identity, text, addressing, thread)) or True)
        self.quiet(self.mod.pump_in, self.config, slack)
        self.assertEqual([t for _, _, t, _, _ in self.said],
                         ["please answer me"])

    def test_a_failure_does_not_step_over_LATER_replies_either(self):
        """Ordering. If the first of three fails, the two behind it must not
        be relayed and marked while the first is still owed — that would
        deliver a conversation out of order and strand the opening line."""
        self.mod.say = lambda *a, **kw: False
        slack = self.threaded(replies=[self.human(1, "first"),
                                       self.human(2, "second")])
        self.quiet(self.mod.pump_in, self.config, slack)
        seen = self.mod.read_reply_state().get(self.parent, {})
        self.assertEqual(seen.get("seen_ts"), self.parent)

    def test_a_SUCCESSFUL_relay_still_advances_it(self):
        """Paired with all of the above: refusing to advance on failure must
        not become refusing to advance at all, which would relay every reply
        forever."""
        slack = self.threaded(replies=[self.human(1, "answer")])
        self.quiet(self.mod.pump_in, self.config, slack)
        seen = self.mod.read_reply_state().get(self.parent, {})
        self.assertNotEqual(seen.get("seen_ts"), self.parent)

    def test_A_NAME_IN_A_THREAD_REPLY_IS_HONOURED(self):
        """The name-tagging path did not reach here at all — `route` was
        called without members, so `@build fix this` in a thread woke the
        thread's owner instead of build. A gap in a feature shipped hours
        earlier, found by reading the call sites rather than the tests."""
        real = self.mod.members_of
        self.mod.members_of = lambda room: ("build", "asker")
        try:
            slack = self.threaded(replies=[self.human(1, "@build take this")])
            self.quiet(self.mod.pump_in, self.config, slack)
        finally:
            self.mod.members_of = real
        self.assertEqual(self.said[0][3], ["--to", "build"])

    def test_the_PARENT_is_not_relayed_back_into_the_room(self):
        """It came FROM the room. Relaying it would post the agent's own
        question back at it — the loop this bridge exists not to have."""
        slack = self.threaded(replies=[])
        self.quiet(self.mod.pump_in, self.config, slack)
        self.assertEqual(self.said, [])

    def test_a_thread_with_NO_replies_is_never_fetched(self):
        """One request per poll on a quiet channel, not one per thread. The
        count on the parent is what makes that possible."""
        slack = self.threaded(replies=[], count=0)
        self.quiet(self.mod.pump_in, self.config, slack)
        self.assertEqual(slack.asked, [])

    def test_an_UNCHANGED_thread_is_not_re_fetched(self):
        slack = self.threaded(replies=[self.human(1, "answer")])
        self.quiet(self.mod.pump_in, self.config, slack)
        self.quiet(self.mod.pump_in, self.config, slack)
        self.assertEqual(slack.asked, [self.parent])

    def test_a_refused_replies_call_does_not_stop_the_poll(self):
        class Refuses(self.Threaded):
            def replies(self, ts):
                return {"ok": False, "error": "thread_not_found"}
        slack = Refuses(replies=[self.human(1, "x")])
        _, text = self.quiet(self.mod.pump_in, self.config, slack)
        self.assertIn("thread_not_found", text)
        self.assertEqual(self.said, [])

    def test_A_REPLY_ARRIVES_AFTER_THE_CURSOR_HAS_PASSED_THE_PARENT(self):
        """Issue #4: the whole point, and what the first fix got wrong.

        The reporter's reproduction exactly: relay a parent, let a later
        top-level message advance the cursor past it, THEN reply in the older
        thread. Driven off the history window, the parent is no longer in it,
        so its reply_count is never re-read and it goes deaf forever — one
        unrelated post silently killing every existing thread.

        The old fake could not express this: its history always returned the
        parent, so the window never moved. A fake that cannot go out of window
        cannot catch a bug about being out of window."""
        parent, base = self.parent, self.base

        class Moved:
            """history has moved past the parent — it returns only newer
            top-level traffic, which is what a real cursor produces."""

            def __init__(self, replies):
                self._replies = replies
                self.asked = []

            def history(self, oldest=None):
                return {"ok": True, "messages": [
                    {"ts": "%.6f" % (base + 400), "bot_id": "B1",
                     "text": "later chatter"}]}

            def replies(self, ts):
                self.asked.append(ts)
                return {"ok": True, "messages": [
                    {"ts": parent, "bot_id": "B1", "text": "*asker*: q"}
                ] + self._replies}

            def post(self, text):
                return {"ok": True, "ts": "1.0"}

        slack = Moved([self.human(1, "the answer")])
        self.quiet(self.mod.pump_in, self.config, slack)
        self.assertEqual(slack.asked, [self.parent],
                         "the parent fell out of the history window and was "
                         "never asked about")
        self.assertEqual([t for _, _, t, _, _ in self.said], ["the answer"])

    def test_watching_is_bounded_by_age_count_and_recheck(self):
        """An out-of-window parent cannot be skipped for free, so an unbounded
        watch list is an unbounded per-poll cost against a rate limit."""
        old = str(self.mod.now() - self.mod.THREAD_WATCH_SEC - 60)
        recent = {str(self.mod.now() - n): "asker" for n in range(30)}
        recent[old] = "asker"
        due = self.mod.watched_parents(recent, {})
        self.assertLessEqual(len(due), self.mod.MAX_WATCHED_THREADS)
        self.assertNotIn(old, due, "older than the watch window")

    def test_a_thread_map_key_that_is_not_a_TIMESTAMP_is_skipped(self):
        """The map is written by this bridge, but it is a file on disk that
        outlives any one version of it. A key that is not a Slack ts must be
        ignored rather than crash the poll — a corrupt entry taking the bridge
        down would silence the escalation path over a parsing detail."""
        self.assertEqual(
            self.mod.watched_parents({"not-a-ts": "asker"}, {}), [])

    def test_an_IN_WINDOW_parent_is_asked_immediately_when_it_grows(self):
        """The rate limit is for blind parents only. history already said the
        count changed, so waiting RECHECK_SEC would delay a reply the bridge
        can already see."""
        ts = "%.6f" % self.mod.now()
        seen = {ts: {"count": 1, "seen_ts": ts, "checked_at": self.mod.now()}}
        self.assertEqual(
            self.mod.watched_parents({ts: "asker"}, seen, {ts: 2}), [ts])

    def test_an_IN_WINDOW_parent_with_no_replies_is_never_asked(self):
        ts = "%.6f" % self.mod.now()
        self.assertEqual(
            self.mod.watched_parents({ts: "asker"}, {}, {ts: 0}), [])

    def test_a_parent_checked_moments_ago_is_not_re_asked(self):
        ts = str(self.mod.now())
        seen = {ts: {"count": 0, "seen_ts": ts,
                     "checked_at": self.mod.now()}}
        self.assertEqual(self.mod.watched_parents({ts: "asker"}, seen), [])

    def test_a_thread_that_cannot_be_FETCHED_does_not_stop_the_poll(self):
        """One unreachable thread must not stop the other messages arriving."""
        class Explodes(self.Threaded):
            def replies(self, ts):
                raise OSError("network")
        slack = Explodes(replies=[self.human(1, "x")])
        _, text = self.quiet(self.mod.pump_in, self.config, slack)
        self.assertIn("could not read thread", text)

    def test_the_reply_state_is_BOUNDED(self):
        """Written once per live thread forever otherwise. Oldest drop first —
        a thread nobody has touched in five hundred is not one about to move."""
        state = {"%d.0" % n: {"count": 1, "seen_ts": "1.0"}
                 for n in range(self.mod.MAX_THREADS + 25)}
        self.mod.write_reply_state(state)
        self.assertEqual(len(self.mod.read_reply_state()),
                         self.mod.MAX_THREADS)

    def test_a_here_in_a_thread_still_wakes_everyone(self):
        """Explicit beats inferred, and that rule has to survive the path it
        could never previously be exercised on."""
        slack = self.threaded(replies=[self.human(1, "@here look")])
        self.quiet(self.mod.pump_in, self.config, slack)
        self.assertEqual(self.said[0][3], ["--to-all"])


class OutboundThreadingTest(BridgeTest):
    """Issue #7: an answer must land in the thread that asked.

    With inbound threading working, the asymmetry is what the human actually
    experiences — they ask in a thread, and the reply appears somewhere else
    in the channel while the thread they are watching stays silent. From a
    phone that is indistinguishable from not being answered.
    """

    class Recorder:
        def __init__(self):
            self.posted = []

        def post(self, text, thread_ts=None):
            self.posted.append((text, thread_ts))
            return {"ok": True, "ts": "%.6f" % (time.time() + len(self.posted))}

        def history(self, oldest=None):
            return {"ok": True, "messages": []}

        def replies(self, ts):
            return {"ok": True, "messages": []}

    def setUp(self):
        super().setUp()
        for name in ("ASKED", "THREADS", "REPLIES"):
            setattr(self.mod, name,
                    os.path.join(self.tmp.name, "slack-%s.json" % name.lower()))
        self.parent = "%.6f" % time.time()

    def ask(self, sender="alice"):
        """A human asks `sender`, in a thread."""
        self.mod.note_question({"thread_ts": self.parent, "ts": self.parent},
                               ["--to", sender])

    def answer(self, sender="alice", text="the answer", thread=None):
        self.mod.waiting_for_human = lambda room, identity: [
            (sender, text, thread)]
        slack = self.Recorder()
        self.quiet(self.mod.pump_out, self.config, slack)
        return slack

    def test_AN_ANSWER_LANDS_IN_THE_THREAD_THAT_ASKED(self):
        self.ask()
        slack = self.answer()
        self.assertEqual(slack.posted[0][1], self.parent)

    def test_an_UNPROMPTED_message_is_still_a_new_root(self):
        """An agent raising something on its own has no thread to land in,
        and forcing one would bury it under an unrelated conversation."""
        slack = self.answer(sender="bob", text="something new")
        self.assertIsNone(slack.posted[0][1])

    def test_the_debt_is_cleared_so_the_NEXT_message_is_a_root(self):
        """Otherwise every later message from that agent is buried in one
        thread forever, which is the mirror of the bug."""
        self.ask()
        self.answer()
        slack = self.answer(text="a later, unrelated thought")
        self.assertIsNone(slack.posted[0][1])

    def test_a_threaded_answer_is_NOT_recorded_as_a_new_root(self):
        """Recording every relay as a parent grew the map by one per message
        and made the thread the human was replying in stop being the newest,
        so their next reply competed with a pile of roots."""
        self.ask()
        before = dict(self.mod.read_threads())
        self.answer()
        self.assertEqual(self.mod.read_threads(), before)

    def test_a_root_message_IS_recorded_so_replies_to_it_route(self):
        self.answer(sender="bob", text="new topic")
        self.assertIn("bob", self.mod.read_threads().values())

    def test_only_the_ADDRESSED_agent_owes_an_answer_there(self):
        self.ask(sender="alice")
        slack = self.answer(sender="carol", text="unrelated")
        self.assertIsNone(slack.posted[0][1])

    def test_a_message_with_no_timestamp_at_all_creates_no_debt(self):
        """Slack always sends one, but the map is keyed on it — a missing ts
        would write `None` as a parent and post every later answer into a
        thread that does not exist."""
        self.mod.note_question({}, ["--to", "alice"])
        self.assertEqual(self.mod.read_asked(), {})

    def test_A_REFUSED_POST_IS_LOUD_NOT_COUNTED_AS_DELIVERED(self):
        """Issue #10's surviving point. Slack answers a bad thread_ts with
        {"ok": false} rather than an exception, and this used to read `ok`
        only to decide whether to record a root — so a refusal fell through as
        success while the message was already off llm_chat's cursor.

        The artifact is the bad part: a message asserting it was threaded,
        absent from Slack entirely, with the sender told it was sent."""
        class Refuses:
            posted = []

            def post(self, text, thread_ts=None):
                return {"ok": False, "error": "thread_not_found"}

            def history(self, oldest=None):
                return {"ok": True, "messages": []}
        self.mod.waiting_for_human = lambda room, identity: [
            ("alice", "an answer", "999.0")]
        _, text = self.quiet(self.mod.pump_out, self.config, Refuses())
        self.assertIn("REFUSED by Slack", text)
        self.assertIn("thread_not_found", text)
        self.assertIn("999.0", text, "must name the thread it could not reach")

    def test_AN_AGENT_CAN_NAME_THE_THREAD_ITSELF(self):
        """The mechanism that replaces guessing. The agent read the thread off
        the message it is answering and hands it back via `say --thread`, so
        with two questions outstanding it can answer EITHER — which is the case
        no amount of inference could get right."""
        self.ask()                                   # one debt outstanding
        other = "%.6f" % (time.time() + 5)
        slack = self.answer(thread=other)
        self.assertEqual(slack.posted[0][1], other,
                         "an explicitly named thread must beat the inferred "
                         "one, or naming it achieves nothing")

    def test_naming_NO_thread_still_posts_at_root_even_with_a_debt(self):
        """Answering "none of them" has to be expressible too. An agent
        raising something unrelated while a question is open must not have it
        buried in that question's thread."""
        self.ask()
        self.mod.note_question(
            {"thread_ts": "%.6f" % (time.time() + 5)}, ["--to", "alice"])
        self.assertIsNone(self.answer().posted[0][1])

    def test_TWO_PENDING_QUESTIONS_POST_AT_ROOT_RATHER_THAN_GUESSING(self):
        """Issue #9, and the reporter's reasoning is the design.

        With one debt, threading is unambiguous and confirmed working. With
        two, nothing here knows which is being answered — llm_chat carries no
        reply-to — and the old slot attached to whichever was stored, the
        OLDER one. The human then watched their newest question sit unanswered
        in the channel while the answer went into a thread they had finished
        with. From their side that is worse than no threading at all: with
        everything top-level they would at least have found it."""
        self.ask()
        self.mod.note_question(
            {"thread_ts": "%.6f" % (time.time() + 5)}, ["--to", "alice"])
        slack = self.answer()
        self.assertIsNone(slack.posted[0][1])

    def test_ONE_pending_question_still_threads(self):
        """The confirmed-working case must survive the fix for the broken one."""
        self.ask()
        self.assertEqual(self.answer().posted[0][1], self.parent)

    def test_ambiguity_is_cleared_rather_than_held(self):
        """Holding parents after the agent has spoken would thread some later,
        unrelated message into a stale conversation."""
        self.ask()
        self.mod.note_question(
            {"thread_ts": "%.6f" % (time.time() + 5)}, ["--to", "alice"])
        self.answer()
        self.assertEqual(self.mod.read_asked().get("alice", []), [])

    def test_a_NEW_question_appends_to_an_old_format_slot(self):
        """Both halves have to read yesterday's file, not just the reader.
        A bridge upgraded mid-conversation would otherwise drop the parent it
        had already recorded the moment a second question arrived."""
        self.mod.write_asked({"alice": self.parent})
        later = "%.6f" % (time.time() + 5)
        self.mod.note_question({"thread_ts": later}, ["--to", "alice"])
        self.assertEqual(self.mod.read_asked()["alice"], [self.parent, later])

    def test_a_slot_written_by_the_OLD_format_still_works(self):
        """The file outlives the version that wrote it, and a bridge that
        crashed on yesterday's state would take the escalation path down."""
        self.mod.write_asked({"alice": self.parent})
        self.assertEqual(self.answer().posted[0][1], self.parent)

    def test_the_asked_map_is_BOUNDED(self):
        """One entry per agent asked, forever, otherwise."""
        many = {"agent%d" % n: "%d.0" % n
                for n in range(self.mod.MAX_THREADS + 20)}
        self.mod.write_asked(many)
        self.assertEqual(len(self.mod.read_asked()), self.mod.MAX_THREADS)

    def test_an_at_here_creates_no_debt(self):
        """It reaches everyone and belongs to no single answer."""
        self.mod.note_question({"thread_ts": self.parent}, ["--to-all"])
        slack = self.answer()
        self.assertIsNone(slack.posted[0][1])

    def test_a_TOP_LEVEL_question_creates_no_thread_debt(self):
        """route() gives it --to-none, so nobody owes an answer in it."""
        self.mod.note_question({"ts": self.parent}, ["--to-none"])
        slack = self.answer()
        self.assertIsNone(slack.posted[0][1])


class DeadReadIsLoudTest(BridgeTest):
    """Issue #1: the agent->Slack direction was dead for a whole session and
    the bridge printed nothing at all."""

    def setUp(self):
        super().setUp()
        self.mod._READ_STATE["broken"] = False
        self.addCleanup(self.mod._READ_STATE.update, {"broken": False})

    def test_A_FAILED_READ_SAYS_SO(self):
        self.mod.waiting_for_human = lambda room, identity: None
        count, text = self.quiet(self.mod.pump_out, self.config, FakeSlack())
        self.assertEqual(count, 0)
        self.assertIn("CANNOT READ", text)
        self.assertIn("llm_chat join human --as me", text)

    def test_it_says_it_ONCE_not_every_poll(self):
        """Every poll_sec forever would bury the first occurrence — the line
        that says WHEN it started — under hundreds of identical ones."""
        self.mod.waiting_for_human = lambda room, identity: None
        first = self.quiet(self.mod.pump_out, self.config, FakeSlack())[1]
        again = self.quiet(self.mod.pump_out, self.config, FakeSlack())[1]
        self.assertIn("CANNOT READ", first)
        self.assertEqual(again, "")

    def test_RECOVERY_is_reported_too(self):
        """Otherwise the last thing in the log is a failure that has since
        fixed itself, and nobody can tell the bridge came back."""
        self.mod.waiting_for_human = lambda room, identity: None
        self.quiet(self.mod.pump_out, self.config, FakeSlack())
        self.mod.waiting_for_human = lambda room, identity: []
        _, text = self.quiet(self.mod.pump_out, self.config, FakeSlack())
        self.assertIn("reading #human again", text)

    def test_a_working_read_is_silent(self):
        self.mod.waiting_for_human = lambda room, identity: []
        _, text = self.quiet(self.mod.pump_out, self.config, FakeSlack())
        self.assertEqual(text, "")


class RoomGoneTest(BridgeTest):
    """`llm_chat delete` destroys the room from another process entirely, so
    the bridge has to notice on its own. Three states, and only one stops it —
    a bridge torn down by a brief server outage would take the human's
    escalation path with it."""

    class Ran:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode, self.stdout, self.stderr = returncode, stdout, stderr

    def answer(self, **kw):
        self.mod.subprocess = type("S", (), {
            "run": staticmethod(lambda *a, **k: self.Ran(**kw))})

    def test_a_room_that_is_still_listed_keeps_the_bridge_running(self):
        self.answer(stdout=json.dumps([{"name": self.config["room"]},
                                       {"name": "other"}]))
        self.assertFalse(self.mod.room_is_gone(self.config))

    def test_a_room_MISSING_from_the_listing_stops_it(self):
        self.answer(stdout=json.dumps([{"name": "other"}]))
        self.assertTrue(self.mod.room_is_gone(self.config))

    def test_A_FAILED_LOOKUP_DOES_NOT_STOP_IT(self):
        """The one that matters. 'Cannot tell' is not 'deleted': the server
        being briefly unreachable is the most common thing that happens here,
        and tearing the bridge down over a restart would also delete its
        cursor and thread map."""
        _, text = self.quiet(self.mod.room_is_gone, self.config)
        self.answer(returncode=1, stderr="no llm_chat server at ...")
        self.assertFalse(self.quiet(self.mod.room_is_gone, self.config)[0])

    def test_unparseable_output_does_not_stop_it_either(self):
        self.answer(stdout="not json")
        self.assertFalse(self.mod.room_is_gone(self.config))

    def test_stopping_CLEARS_the_bridges_own_state(self):
        """The cursor and thread map are keyed to a room that no longer
        exists. Left behind, a NEW room of the same name inherits another
        conversation's read position and thread parents — so the first thing
        the human says in it lands as a reply to a thread from the old one."""
        self.mod.THREADS = os.path.join(self.tmp.name, "slack-threads.json")
        for path in (self.mod.CURSOR, self.mod.THREADS):
            with open(path, "w") as f:
                f.write("{}")
        self.mod.load_config = lambda: self.config
        self.mod.Slack = lambda *a, **kw: FakeSlack()
        self.mod.room_is_gone = lambda config: True
        # A FUSE ON THE LOOP. The only thing stopping `main` here is the
        # room-gone check, so with that check removed this test ran forever —
        # a mutation sweep sat on it for 46 minutes, and a sweep that never
        # finishes measures nothing. Bounded, a loop that fails to stop fails
        # the assertion below instead of hanging the run.
        self.mod.time = LoopTest.Clock(stop_after=3)
        argv = sys.argv
        sys.argv = ["llm-chat-slack"]
        try:
            _, text = self.quiet(self.mod.main)
        except KeyboardInterrupt:
            text = ""          # the fuse blew: the loop never stopped
        finally:
            sys.argv = argv
        self.assertIn("no longer exists", text)
        self.assertFalse(os.path.exists(self.mod.CURSOR))
        self.assertFalse(os.path.exists(self.mod.THREADS))

    def test_stopping_with_no_state_files_is_not_an_error(self):
        """The common case: a bridge that never saw a message has neither."""
        self.mod.THREADS = os.path.join(self.tmp.name, "absent-threads.json")
        self.mod.CURSOR = os.path.join(self.tmp.name, "absent-cursor.json")
        self.mod.load_config = lambda: self.config
        self.mod.Slack = lambda *a, **kw: FakeSlack()
        self.mod.room_is_gone = lambda config: True
        self.mod.time = LoopTest.Clock(stop_after=3)   # same fuse, same reason
        argv = sys.argv
        sys.argv = ["llm-chat-slack"]
        try:
            code, _ = self.quiet(self.mod.main)
        except KeyboardInterrupt:
            code = "the loop never stopped"
        finally:
            sys.argv = argv
        self.assertEqual(code, 0)
