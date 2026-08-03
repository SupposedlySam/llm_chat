"""The CLI's remaining surface: transport, locking, setup, doctor, dispatch.

The transport tests matter more than they look. `call` is the only place HTTP
happens, so its error handling decides whether a chat outage is a clear message
or a stack trace in the middle of somebody's refactor — and the zonai wire
conventions it encodes are each a 500 or a silent wrong answer if got wrong.
"""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import FakeServer, load, write_settings  # noqa: E402

cli = load("llm_chat")


class WireTest(unittest.TestCase):
    """The conventions in the comments, asserted."""

    def test_booleans_go_over_the_wire_as_one_and_zero(self):
        self.assertEqual(cli.b(True), 1)
        self.assertEqual(cli.b(False), 0)

    def test_datetimes_are_epoch_millis_not_iso_8601(self):
        """An ISO-8601 string 500s (zonai#19)."""
        now = cli.now_ms()
        self.assertIsInstance(now, int)
        self.assertGreater(now, 1_600_000_000_000)

    def test_conditions_build_the_shapes_the_server_expects(self):
        self.assertEqual(cli.eq("a", 1), {"type": "eq", "column": "a", "value": 1})
        self.assertEqual(cli.gt("a", 1), {"type": "gt", "column": "a", "value": 1})
        self.assertEqual(cli.and_(cli.eq("a", 1))["type"], "and")

    def test_names_are_validated_against_the_documented_shape(self):
        for good in ("room", "deploy-review", "a.b_c", "A1"):
            self.assertTrue(cli.valid(good), good)
        for bad in ("", "has space", "-leading", "x" * 65, "wh@t"):
            self.assertFalse(cli.valid(bad), bad)


class CallTest(unittest.TestCase):
    def setUp(self):
        self.real = cli.urllib.request.urlopen

    def tearDown(self):
        cli.urllib.request.urlopen = self.real

    def test_a_json_body_comes_back_parsed(self):
        class Response:
            def read(self):
                return b'{"data": {"ok": true}}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        cli.urllib.request.urlopen = lambda *a, **kw: Response()
        self.assertEqual(cli.call("http://x", "GET", "/p"), {"data": {"ok": True}})

    def test_an_empty_body_is_not_a_parse_error(self):
        class Response:
            def read(self):
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        cli.urllib.request.urlopen = lambda *a, **kw: Response()
        self.assertEqual(cli.call("http://x", "GET", "/p"), {})

    def test_an_http_error_is_reported_with_its_body(self):
        def raise_http(*a, **kw):
            raise urllib.error.HTTPError("u", 500, "boom", {}, io.BytesIO(b"details"))
        cli.urllib.request.urlopen = raise_http
        result = cli.call("http://x", "GET", "/p")
        self.assertEqual(result["error"], "HTTP 500")
        self.assertIn("details", result["body"])

    def test_an_unreachable_server_explains_the_localhost_trap(self):
        """zonai binds [::1] only on macOS, so 127.0.0.1 refuses against a
        server that is plainly running (zonai#16). That half hour is worth one
        sentence in the error."""
        def raise_url(*a, **kw):
            raise urllib.error.URLError("refused")
        cli.urllib.request.urlopen = raise_url
        result = cli.call("http://x", "GET", "/p")
        self.assertIn("no llm_chat server", result["error"])
        self.assertIn("localhost", result["error"])

    def test_a_query_is_sent_as_an_encoded_body_parameter(self):
        seen = {}

        class Response:
            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def capture(req, **kw):
            seen["url"] = req.full_url
            return Response()
        cli.urllib.request.urlopen = capture
        cli.call("http://x", "GET", "/db/list", query={"table": "channels"})
        self.assertIn("body=", seen["url"])
        self.assertIn("channels", seen["url"])


class HelperErrorTest(unittest.TestCase):
    """rows/create/update must turn a server error into an exit, not a
    traceback: the caller is an agent mid-task, not a developer."""

    def setUp(self):
        self.real = cli.call
        cli.call = lambda *a, **kw: {"error": "HTTP 500", "body": "why"}

    def tearDown(self):
        cli.call = self.real

    def test_rows_exits(self):
        with self.assertRaises(SystemExit):
            cli.rows("http://x", "channels")

    def test_create_exits(self):
        with self.assertRaises(SystemExit):
            cli.create("http://x", "channels", {})

    def test_update_exits(self):
        with self.assertRaises(SystemExit):
            cli.update("http://x", "channels", cli.eq("id", "1"), {})


class IdentityMemoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CLAUDE_PROJECT_DIR"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.tmp.cleanup()

    def test_a_missing_record_reads_as_empty_not_as_an_error(self):
        self.assertEqual(cli.read_joined(), {})

    def test_a_corrupt_record_reads_as_empty_rather_than_crashing_a_hook(self):
        d = os.path.join(self.tmp.name, ".llm_chat")
        os.makedirs(d)
        with open(os.path.join(d, "joined.json"), "w") as f:
            f.write("{not json")
        self.assertEqual(cli.read_joined(), {})

    def test_remembering_is_atomic(self):
        """Written to a temp file and renamed: a hook reading it concurrently
        must never see half a record."""
        cli.remember("room", "me", "http://x")
        self.assertEqual(cli.read_joined()["room"]["identity"], "me")
        self.assertFalse(os.path.exists(cli.joined_path() + ".tmp"))

    def test_identity_falls_back_to_what_was_remembered(self):
        cli.remember("room", "me", "http://x")
        self.assertEqual(cli.identity_for("room", None), "me")
        self.assertEqual(cli.identity_for("room", "explicit"), "explicit")

    def test_an_unjoined_room_names_the_way_in(self):
        with self.assertRaises(SystemExit) as caught:
            cli.identity_for("nowhere", None)
        self.assertIn("--as", str(caught.exception))


class ReadLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CLAUDE_PROJECT_DIR"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.tmp.cleanup()

    def test_the_lock_is_held_inside_the_block(self):
        with cli.read_lock() as held:
            self.assertTrue(held)

    def test_it_fails_open_rather_than_wedging_delivery(self):
        """A lock that cannot be taken must not stop a message arriving: a rare
        duplicate beats a session that never hears anything again."""
        with cli.read_lock():
            with cli.read_lock(timeout=0.05) as held_again:
                self.assertFalse(held_again)

    def test_an_unusable_lock_directory_does_not_raise(self):
        os.environ["CLAUDE_PROJECT_DIR"] = "/dev/null/nope"
        with cli.read_lock() as held:
            self.assertFalse(held)


class ChannelsAndInviteTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeServer()
        self.real = cli.call
        cli.call = self.fake.call

    def tearDown(self):
        cli.call = self.real

    def show(self):
        out = io.StringIO()
        with redirect_stdout(out):
            cli.do_channels("http://x")
        return out.getvalue()

    def test_an_empty_server_says_so(self):
        self.assertIn("no channels yet", self.show())

    def test_rooms_list_members_and_who_is_done(self):
        self.fake.channel("room", topic="a topic", message_count=3)
        self.fake.membership("room", "me", done=0)
        self.fake.membership("room", "other", done=1)
        text = self.show()
        self.assertIn("a topic", text)
        self.assertIn("me, other", text)
        self.assertIn("done: other", text)
        self.assertIn("messages: 3", text)

    def test_a_room_with_no_topic_says_so_rather_than_printing_none(self):
        self.fake.channel("bare")
        self.assertIn("no topic", self.show())

    def test_closed_rooms_are_marked(self):
        self.fake.channel("dead", closed=1)
        self.assertIn("[closed]", self.show())

    def test_the_invite_is_written_as_instructions_to_an_agent(self):
        """Because that is literally what happens to it: a human pastes it into
        another session and says 'do that'."""
        text = cli.invite("room", "the topic", "http://x")
        self.assertIn("invited", text)
        self.assertIn("the topic", text)
        self.assertIn("join room", text)
        self.assertIn("--server http://x", text)
        self.assertIn(os.path.join("bin", "llm_chat"), text)

    def test_an_invite_without_a_topic_omits_the_line(self):
        self.assertNotIn("Topic:", cli.invite("room", None, "http://x"))


class ServerHelpersTest(unittest.TestCase):
    def setUp(self):
        self.real = cli.call

    def tearDown(self):
        cli.call = self.real

    def test_an_http_error_still_means_something_is_listening(self):
        cli.call = lambda *a, **kw: {"error": "HTTP 404"}
        self.assertTrue(cli.server_up("http://x"))

    def test_a_refused_connection_means_it_is_not(self):
        cli.call = lambda *a, **kw: {"error": "no llm_chat server at http://x"}
        self.assertFalse(cli.server_up("http://x"))

    def test_a_clean_answer_means_it_is_up(self):
        cli.call = lambda *a, **kw: {"data": {"items": []}}
        self.assertTrue(cli.server_up("http://x"))

    def test_the_port_is_taken_from_the_url(self):
        self.assertEqual(cli.port_of("http://localhost:7717"), 7717)
        self.assertEqual(cli.port_of("http://localhost"), 80)
        self.assertEqual(cli.port_of("https://example.com"), 443)


class DoctorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name
        os.environ["CLAUDE_PROJECT_DIR"] = self.project
        self.saved = os.environ.get("CLAUDE_CODE_ENTRYPOINT")
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "claude-vscode"

    def tearDown(self):
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        if self.saved is None:
            os.environ.pop("CLAUDE_CODE_ENTRYPOINT", None)
        else:
            os.environ["CLAUDE_CODE_ENTRYPOINT"] = self.saved
        self.tmp.cleanup()

    def report(self):
        out = io.StringIO()
        with redirect_stdout(out):
            cli.do_doctor("http://x")
        return out.getvalue()

    def mark(self, name):
        d = os.path.join(self.project, ".llm_chat", "probe")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "w") as f:
            f.write("1 2")

    def test_an_unwired_repo_is_told_how_to_start(self):
        text = self.report()
        self.assertIn("not set up", text)
        self.assertIn("setup <channel>", text)

    def test_older_wiring_is_named_specifically(self):
        write_settings(self.project, PostToolUse=["/x/bin/llm-chat-deliver"])
        text = self.report()
        self.assertIn("Older wiring", text)
        self.assertIn("llm-chat-wake missing", text)

    def test_no_record_of_firing_is_not_a_claim_that_it_never_fired(self):
        """The mark only exists from the probe's own start, so a hook working
        before probing shipped reads the same way until it next runs. Saying
        'never fired' there would contradict what the operator watched work."""
        write_settings(self.project,
                       PostToolUse=["/x/bin/llm-chat-deliver"],
                       Stop=["/x/bin/llm-chat-wake"],
                       SessionStart=["/x/bin/llm-chat-wake"])
        text = self.report()
        self.assertIn("NO RECORD OF FIRING", text)
        self.assertNotIn("NEVER FIRED", text)
        self.assertIn("NOT proof", text)

    def test_a_waker_only_on_stop_is_called_out(self):
        write_settings(self.project,
                       PostToolUse=["/x/bin/llm-chat-deliver"],
                       Stop=["/x/bin/llm-chat-wake"])
        self.mark("post-tool-use")
        self.mark("stop")
        text = self.report()
        self.assertIn("not on SessionStart", text)
        self.assertNotIn("Wiring looks right", text,
                         "an all-clear two lines after a warning is a contradiction")

    def test_a_fully_wired_and_fired_repo_gets_the_all_clear(self):
        write_settings(self.project,
                       PostToolUse=["/x/bin/llm-chat-deliver"],
                       Stop=["/x/bin/llm-chat-wake"],
                       SessionStart=["/x/bin/llm-chat-wake"])
        self.mark("post-tool-use")
        self.mark("stop")
        text = self.report()
        self.assertIn("Wiring looks right", text)

    def test_drift_against_the_install_stamp_is_reported(self):
        write_settings(self.project,
                       PostToolUse=["/x/bin/llm-chat-deliver"],
                       Stop=["/x/bin/llm-chat-wake"],
                       SessionStart=["/x/bin/llm-chat-wake"])
        self.mark("post-tool-use")
        self.mark("stop")
        d = os.path.join(self.project, ".llm_chat")
        with open(os.path.join(d, "installed.json"), "w") as f:
            json.dump({"fingerprint": "0000000000000000"}, f)
        self.assertIn("STALE", self.report())

    def test_a_wired_repo_with_no_stamp_is_told_to_record_one(self):
        write_settings(self.project, PostToolUse=["/x/bin/llm-chat-deliver"],
                       Stop=["/x/bin/llm-chat-wake"],
                       SessionStart=["/x/bin/llm-chat-wake"])
        self.mark("post-tool-use")
        self.mark("stop")
        self.assertIn("no install stamp", self.report())


if __name__ == "__main__":
    unittest.main()
