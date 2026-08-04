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
import unittest
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import load  # noqa: E402


class FakeSlack:
    """Stands in for the Slack API, recording what was posted."""

    def __init__(self, messages=None, ok=True, error=None, explode=False):
        self.posted = []
        self.messages = messages or []
        self.ok = ok
        self.error = error
        self.explode = explode

    def post(self, text):
        if self.explode:
            raise OSError("slack is down")
        self.posted.append(text)
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
        self.mod.CURSOR = os.path.join(self.tmp.name, "slack-cursor.json")
        self.config = {"room": "human", "identity": "me",
                       "slack": {"bot_token": "x", "channel": "C1"}}
        self.said = []
        self.mod.say = lambda room, identity, text: (
            self.said.append((room, identity, text)) or True)

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
        self.assertEqual([t for _, _, t in self.said], ["an actual human answer"])

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
        self.assertEqual(self.said, [("human", "me", "yes, ship it")])

    def test_joins_and_other_events_without_text_are_ignored(self):
        slack = FakeSlack([{"ts": "1", "user": "U1", "subtype": "channel_join"}])
        self.quiet(self.mod.pump_in, self.config, slack)
        self.assertEqual(self.said, [])

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
        self.assertEqual([t for _, _, t in self.said], ["first", "second"])

    # ── outbound ────────────────────────────────────────────────────────────
    def test_agent_messages_are_relayed_to_slack(self):
        self.mod.waiting_for_human = lambda room, identity: [
            "[builder] should I force-push?"]
        slack = FakeSlack()
        count, _ = self.quiet(self.mod.pump_out, self.config, slack)
        self.assertEqual(slack.posted, ["[builder] should I force-push?"])
        self.assertEqual(count, 1)

    def test_a_slack_outage_reports_the_lost_message_rather_than_dropping_it(self):
        """The message is already off llm_chat's cursor by then, so a silent
        failure loses it with nobody able to tell."""
        self.mod.waiting_for_human = lambda room, identity: ["[builder] hello"]
        _, text = self.quiet(self.mod.pump_out, self.config,
                             FakeSlack(explode=True))
        self.assertIn("LOST", text)
        self.assertIn("[builder] hello", text)

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
        self.assertEqual(slack.posted, [], "--check must not post")

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

    def stub(self, stdout="", explode=False):
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
                Result.returncode = 0
                return Result
        self.mod.subprocess = Fake

    def test_it_reads_through_the_cli_as_the_human(self):
        self.stub("[builder] a question\n[reviewer] another")
        lines = self.mod.waiting_for_human("human", "me")
        self.assertEqual(len(lines), 2)
        argv = self.calls[0]
        self.assertIn("read", argv)
        self.assertIn("--as", argv)
        self.assertNotIn("--all", argv,
                         "--all disables the self-filter and would relay the "
                         "human's own answers straight back to Slack")

    def test_nothing_new_is_not_a_message(self):
        self.stub("nothing new in human")
        self.assertEqual(self.mod.waiting_for_human("human", "me"), [])

    def test_a_broken_cli_does_not_take_the_bridge_down(self):
        self.stub(explode=True)
        self.assertEqual(self.mod.waiting_for_human("human", "me"), [])

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
        self.mod.time = self.Clock(stop_after=3)
        out = io.StringIO()
        with redirect_stdout(out):
            with self.assertRaises(KeyboardInterrupt):
                self.mod.main()
        self.assertNotIn("-> Slack", out.getvalue())
