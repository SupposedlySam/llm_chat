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
        self.assertEqual(cli.call("http://127.0.0.1:1", "GET", "/p"), {"data": {"ok": True}})

    def test_an_empty_body_is_not_a_parse_error(self):
        class Response:
            def read(self):
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        cli.urllib.request.urlopen = lambda *a, **kw: Response()
        self.assertEqual(cli.call("http://127.0.0.1:1", "GET", "/p"), {})

    def test_an_http_error_is_reported_with_its_body(self):
        # Closed explicitly: HTTPError holds the stream open and warns on gc,
        # and a suite that prints warnings trains you past its own output.
        body = io.BytesIO(b"details")
        error = urllib.error.HTTPError("u", 500, "boom", {}, body)

        def raise_http(*a, **kw):
            raise error
        cli.urllib.request.urlopen = raise_http
        try:
            result = cli.call("http://127.0.0.1:1", "GET", "/p")
        finally:
            error.close()
            body.close()
        self.assertEqual(result["error"], "HTTP 500")
        self.assertIn("details", result["body"])

    def test_an_unreachable_server_explains_the_localhost_trap(self):
        """zonai binds [::1] only on macOS, so 127.0.0.1 refuses against a
        server that is plainly running (zonai#16). That half hour is worth one
        sentence in the error."""
        def raise_url(*a, **kw):
            raise urllib.error.URLError("refused")
        cli.urllib.request.urlopen = raise_url
        result = cli.call("http://127.0.0.1:1", "GET", "/p")
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
        cli.call("http://127.0.0.1:1", "GET", "/db/list", query={"table": "channels"})
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
            cli.rows("http://127.0.0.1:1", "channels")

    def test_create_exits(self):
        with self.assertRaises(SystemExit):
            cli.create("http://127.0.0.1:1", "channels", {})

    def test_update_exits(self):
        with self.assertRaises(SystemExit):
            cli.update("http://127.0.0.1:1", "channels", cli.eq("id", "1"), {})


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
        cli.remember("room", "me", "http://127.0.0.1:1")
        self.assertEqual(cli.read_joined()["room"]["identity"], "me")
        self.assertFalse(os.path.exists(cli.joined_path() + ".tmp"))

    def test_identity_falls_back_to_what_was_remembered(self):
        cli.remember("room", "me", "http://127.0.0.1:1")
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
            cli.do_channels("http://127.0.0.1:1")
        return out.getvalue()

    def as_json(self, show_closed=False):
        out = io.StringIO()
        with redirect_stdout(out):
            cli.do_channels("http://127.0.0.1:1", show_closed, as_json=True)
        return json.loads(out.getvalue())

    def test_an_empty_server_says_so(self):
        self.assertIn("no channels yet", self.show())

    # ── the machine-readable discovery surface ──────────────────────────────
    # Asked for by an agent whose room-discovery trigger was parsing the
    # rendering above. It is a rendering — the same argument that had just cost
    # this repo a silently-corrupted digest — and this verb had no machine form
    # at all, so "use read --json" would have been the wrong answer to a real gap.

    def test_an_empty_server_is_an_empty_list_not_prose(self):
        """'no channels yet' is not JSON, and a consumer special-casing it is
        back to parsing prose."""
        self.assertEqual(self.as_json(), [])

    def test_it_carries_what_a_tool_decides_with(self):
        self.fake.channel("room", topic="a topic", message_count=3,
                          broadcast=1, briefing="rules", briefing_by="bob")
        self.fake.membership("room", "me", done=0)
        self.fake.membership("room", "other", done=1)
        record = self.as_json()[0]
        self.assertEqual(record["name"], "room")
        self.assertEqual(record["topic"], "a topic")
        self.assertTrue(record["broadcast"])
        self.assertEqual(record["briefing_by"], "bob")
        self.assertEqual(record["members"], ["me", "other"])
        self.assertEqual(record["done"], ["other"])
        self.assertEqual(record["message_count"], 3)

    def test_booleans_come_back_as_booleans_not_the_wire_ints(self):
        """The store speaks 0/1. A consumer writing `if room["broadcast"]`
        against a 0 would be right by accident and wrong the day it is "0"."""
        self.fake.channel("room", broadcast=1, closed=0)
        record = self.as_json()[0]
        self.assertIs(record["broadcast"], True)
        self.assertIs(record["closed"], False)

    def test_closed_rooms_are_INCLUDED_with_a_flag(self):
        """The opposite of the rendering, deliberately. Hiding them is right
        for a reader being offered something to join; a program filtering for
        itself is not the same as one that cannot see them."""
        self.fake.channel("open-one")
        self.fake.channel("dead", closed=1, closed_reason="everyone left")
        names = {c["name"]: c for c in self.as_json()}
        self.assertIn("dead", names)
        self.assertTrue(names["dead"]["closed"])
        self.assertEqual(names["dead"]["closed_reason"], "everyone left")

    def test_a_room_nobody_is_in_has_an_empty_member_list(self):
        self.fake.channel("empty")
        self.assertEqual(self.as_json()[0]["members"], [])

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

    def test_closed_rooms_are_hidden_from_the_discovery_surface(self):
        """`join` refuses a closed room, so listing one offers an agent
        something it cannot act on — and nothing deletes a channel, so they
        accumulate forever. Nineteen rooms, fifteen dead, burying the four
        real ones."""
        self.fake.channel("live", topic="open for business")
        self.fake.channel("dead", closed=1)
        text = self.show()
        self.assertIn("live", text)
        self.assertNotIn("dead", text)

    def test_the_hidden_count_is_reported_rather_than_silently_dropped(self):
        self.fake.channel("live")
        self.fake.channel("dead", closed=1)
        text = self.show()
        self.assertIn("1 closed", text)
        self.assertIn("--all", text)

    def test_all_includes_them_and_marks_them(self):
        self.fake.channel("dead", closed=1)
        out = io.StringIO()
        with redirect_stdout(out):
            cli.do_channels("http://127.0.0.1:1", show_closed=True)
        self.assertIn("[closed]", out.getvalue())

    def test_a_server_with_only_closed_rooms_says_so(self):
        """Distinct from 'no channels yet' — there ARE rooms, none joinable."""
        self.fake.channel("dead", closed=1)
        text = self.show()
        self.assertIn("no open channels", text)
        self.assertIn("--all", text)

    def test_the_invite_is_written_as_instructions_to_an_agent(self):
        """Because that is literally what happens to it: a human pastes it into
        another session and says 'do that'."""
        text = cli.invite("room", "the topic", "http://127.0.0.1:1")
        self.assertIn("invited", text)
        self.assertIn("the topic", text)
        self.assertIn("join room", text)
        self.assertIn("--server http://127.0.0.1:1", text)
        self.assertIn(os.path.join("bin", "llm_chat"), text)

    def test_an_invite_without_a_topic_omits_the_line(self):
        self.assertNotIn("Topic:", cli.invite("room", None, "http://127.0.0.1:1"))


class ServerHelpersTest(unittest.TestCase):
    def setUp(self):
        self.real = cli.call

    def tearDown(self):
        cli.call = self.real

    def test_an_http_error_still_means_something_is_listening(self):
        cli.call = lambda *a, **kw: {"error": "HTTP 404"}
        self.assertTrue(cli.server_up("http://127.0.0.1:1"))

    def test_a_refused_connection_means_it_is_not(self):
        cli.call = lambda *a, **kw: {"error": "no llm_chat server at http://127.0.0.1:1"}
        self.assertFalse(cli.server_up("http://127.0.0.1:1"))

    def test_a_clean_answer_means_it_is_up(self):
        cli.call = lambda *a, **kw: {"data": {"items": []}}
        self.assertTrue(cli.server_up("http://127.0.0.1:1"))

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
            cli.do_doctor("http://127.0.0.1:1")
        return out.getvalue()

    def joined(self):
        """Doctor returns early on an unwired repo, so the waker section is
        only reachable once the hooks are registered AND a room is joined."""
        write_settings(self.project,
                       PostToolUse=["/x/bin/llm-chat-deliver"],
                       Stop=["/x/bin/llm-chat-wake"],
                       SessionStart=["/x/bin/llm-chat-wake"])
        d = os.path.join(self.project, ".llm_chat")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "joined.json"), "w") as f:
            json.dump({"room": {"identity": "me", "server": "http://127.0.0.1:1"}}, f)

    def exited(self, reason, at=None, pid=999):
        d = os.path.join(self.project, ".llm_chat")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "wake.exit"), "w") as f:
            json.dump({"reason": reason, "pid": pid,
                       "at": at if at is not None else cli.now_ms() // 1000}, f)

    # ── why the waker stopped ───────────────────────────────────────────────
    # "pid is gone" was a dead end at exactly the question that matters, and
    # the reasons have different remedies — one of them is not a problem.

    def test_no_record_reads_as_unknown_not_as_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(cli.waker_exit(tmp))

    def test_a_corrupt_record_reads_as_unknown(self):
        d = os.path.join(self.project, ".llm_chat")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "wake.exit"), "w") as f:
            f.write("{not json")
        self.assertIsNone(cli.waker_exit(self.project))

    def test_a_record_with_no_reason_is_not_an_answer(self):
        d = os.path.join(self.project, ".llm_chat")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "wake.exit"), "w") as f:
            json.dump({"pid": 1}, f)
        self.assertIsNone(cli.waker_exit(self.project))

    def test_doctor_reports_the_reason(self):
        self.joined()
        self.exited("every joined room is closed — nothing can arrive")
        self.assertIn("stopped because", self.report())
        self.assertIn("nothing can arrive", self.report())

    def test_a_still_running_record_means_it_was_KILLED(self):
        """The discriminating value. A waker that is gone while its record
        still says 'running' never chose to stop — something outside ended it,
        which is a different diagnosis from every other reason here."""
        self.joined()
        self.exited("running")
        text = self.report()
        self.assertIn("killed from outside", text)

    def test_a_reason_it_chose_does_NOT_claim_it_was_killed(self):
        """Paired with the test above: a message that always fires teaches
        nothing, and 'superseded' is the healthy case."""
        self.joined()
        self.exited("superseded by a newer waker (healthy)")
        self.assertNotIn("killed from outside", self.report())

    def test_it_says_how_long_ago(self):
        self.joined()
        self.exited("running", at=cli.now_ms() // 1000 - 1800)
        self.assertIn("30m ago", self.report())

    def test_a_record_with_no_timestamp_still_reports_the_reason(self):
        self.joined()
        d = os.path.join(self.project, ".llm_chat")
        with open(os.path.join(d, "wake.exit"), "w") as f:
            json.dump({"reason": "orphaned", "pid": 7}, f)
        text = self.report()
        self.assertIn("orphaned", text)
        self.assertNotIn("ago", text.split("orphaned")[1][:20])

    def test_no_record_at_all_says_so_rather_than_staying_silent(self):
        self.joined()
        self.assertIn("no exit record", self.report())

    def wired(self, checkout, fingerprint="old"):
        write_settings(self.project,
                       PostToolUse=["/x/bin/llm-chat-deliver"],
                       Stop=["/x/bin/llm-chat-wake"],
                       SessionStart=["/x/bin/llm-chat-wake"])
        d = os.path.join(self.project, ".llm_chat")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "installed.json"), "w") as f:
            json.dump({"fingerprint": fingerprint, "checkout": checkout}, f)

    def test_a_vendored_consumer_is_told_not_to_reinstall_from_elsewhere(self):
        """Re-installing from any other tree would repoint their hooks and
        silently undo the vendoring, so the remedy names THEIR tree and says
        why anywhere else is wrong."""
        with tempfile.TemporaryDirectory() as tree:
            os.makedirs(os.path.join(tree, "bin"))
            self.wired(tree)
            text = self.report()
            self.assertIn("STALE", text)
            self.assertIn(os.path.join(tree, "install.sh"), text)
            self.assertIn("undo that", text)

    def test_a_repo_wired_from_THIS_checkout_gets_no_such_warning(self):
        """Paired: a note that always fires teaches nothing, and the ordinary
        case is a consumer pointed at this clone."""
        self.wired(os.path.dirname(os.path.dirname(
            os.path.abspath(cli.__file__))))
        text = self.report()
        self.assertNotIn("undo that", text)

    def test_a_tree_that_is_GONE_is_reported_as_that_rather_than_as_stale(self):
        """Their hooks point into it, so they cannot be running at all. Calling
        that 'stale' would send them to re-install from a path that does not
        exist."""
        self.wired("/no/such/vendored/tree")
        text = self.report()
        self.assertIn("WIRED FROM A TREE THAT IS GONE", text)
        self.assertNotIn("STALE:", text)

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

    def joined_with_waker(self, pid):
        d = os.path.join(self.project, ".llm_chat")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "joined.json"), "w") as f:
            json.dump({"room": {"identity": "me", "server": "http://127.0.0.1:1"}}, f)
        with open(os.path.join(d, "wake.pid"), "w") as f:
            f.write(str(pid))
        write_settings(self.project,
                       PostToolUse=["/x/bin/llm-chat-deliver"],
                       Stop=["/x/bin/llm-chat-wake"],
                       SessionStart=["/x/bin/llm-chat-wake"])
        self.mark("post-tool-use")
        self.mark("stop")

    def test_a_live_waker_is_reported_as_listening(self):
        self.joined_with_waker(os.getpid())
        self.assertIn("listening now: yes", self.report())

    def test_a_dead_waker_is_reported_however_green_everything_else_is(self):
        """The failure this exists for: every other check said 'wiring looks
        right' while the session was unreachable, because registered and
        has-fired are both facts about the past."""
        self.joined_with_waker(999999)
        text = self.report()
        self.assertIn("LISTENING NOW: NO", text)
        self.assertIn("999999 is gone", text)
        self.assertIn("read", text, "must say the messages are not lost")

    def test_a_wired_repo_with_no_stamp_is_told_to_record_one(self):
        write_settings(self.project, PostToolUse=["/x/bin/llm-chat-deliver"],
                       Stop=["/x/bin/llm-chat-wake"],
                       SessionStart=["/x/bin/llm-chat-wake"])
        self.mark("post-tool-use")
        self.mark("stop")
        self.assertIn("no install stamp", self.report())


if __name__ == "__main__":
    unittest.main()


class MessageSourceTest(unittest.TestCase):
    """A payload on a command line is handed to a SHELL first, and that has a
    failure mode which reports success: backticks are substituted away before
    this program exists, so the CLI delivers a string that is already wrong."""

    class Args:
        def __init__(self, text=None, file=None):
            self.text = text
            self.file = file

    def test_a_positional_message_is_used(self):
        self.assertEqual(cli.message_text(self.Args(text="hello")), "hello")

    def test_a_file_is_read_verbatim(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("text with `backticks` and $(substitution)\n")
            path = f.name
        try:
            self.assertEqual(cli.message_text(self.Args(file=path)),
                             "text with `backticks` and $(substitution)")
        finally:
            os.unlink(path)

    def test_stdin_is_accepted_as_a_dash(self):
        saved = sys.stdin
        sys.stdin = io.StringIO("from a pipe\n")
        try:
            self.assertEqual(cli.message_text(self.Args(file="-")), "from a pipe")
        finally:
            sys.stdin = saved

    def test_supplying_both_is_refused_rather_than_silently_preferred(self):
        """A caller who supplies both has a wrong belief about which was sent,
        and picking one quietly leaves them holding it."""
        with self.assertRaises(SystemExit) as caught:
            cli.message_text(self.Args(text="a", file="b"))
        self.assertIn("not both", str(caught.exception))

    def test_supplying_neither_names_all_three_forms(self):
        with self.assertRaises(SystemExit) as caught:
            cli.message_text(self.Args())
        message = str(caught.exception)
        self.assertIn("--file", message)
        self.assertIn("stdin", message)

    def test_an_unreadable_file_is_reported_not_swallowed(self):
        with self.assertRaises(SystemExit) as caught:
            cli.message_text(self.Args(file="/dev/null/nope"))
        self.assertIn("cannot read", str(caught.exception))


class WakerLivenessTest(unittest.TestCase):
    """Registered and has-fired are facts about the PAST. This agent sat
    unreachable for two and a half idle hours while every other check was
    green, because the waker it had armed had died."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name
        os.makedirs(os.path.join(self.project, ".llm_chat"))

    def tearDown(self):
        self.tmp.cleanup()

    def write_pid(self, pid):
        with open(os.path.join(self.project, ".llm_chat", "wake.pid"), "w") as f:
            f.write(str(pid))

    def test_no_pidfile_means_nothing_has_been_armed(self):
        self.assertEqual(cli.waker_alive(self.project), (None, False))

    def test_a_live_pid_reports_listening(self):
        self.write_pid(os.getpid())
        pid, alive = cli.waker_alive(self.project)
        self.assertEqual(pid, os.getpid())
        self.assertTrue(alive)

    def test_a_dead_pid_reports_not_listening(self):
        self.write_pid(999999)
        pid, alive = cli.waker_alive(self.project)
        self.assertEqual(pid, 999999)
        self.assertFalse(alive)

    def test_an_unreadable_pidfile_is_not_mistaken_for_a_live_waker(self):
        self.write_pid("not-a-pid")
        self.assertEqual(cli.waker_alive(self.project), (None, False))
