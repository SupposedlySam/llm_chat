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
from support import FakeServer, load, parsed, write_settings  # noqa: E402

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

    def throttle(self, times, retry_after=None, then=b'{"data": {"ok": true}}'):
        """A server that 429s `times` times and then answers."""
        state = {"n": 0}
        self.slept = []
        real_sleep = cli.time.sleep
        cli.time.sleep = lambda s: self.slept.append(s)
        self.addCleanup(lambda: setattr(cli.time, "sleep", real_sleep))

        class Response:
            def read(self):
                return then

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        # CLOSED ON THE WAY OUT. HTTPError holds its stream open and warns on
        # gc, and run.py turns ResourceWarning into an error — so a fixture
        # that leaks them fails the suite somewhere else entirely. The test
        # below this one already documented that and I did not follow it.
        made = []
        self.addCleanup(lambda: [(e.close(), b.close()) for e, b in made])

        def maybe(*a, **kw):
            state["n"] += 1
            if state["n"] <= times:
                body = io.BytesIO(b"Rate limit exceeded")
                headers = {"Retry-After": retry_after} if retry_after else {}
                error = urllib.error.HTTPError("u", 429, "slow down", headers,
                                               body)
                made.append((error, body))
                raise error
            return Response()
        cli.urllib.request.urlopen = maybe
        self.attempts = state

    def test_A_429_IS_RETRIED_rather_than_handed_to_the_caller(self):
        """Issue #15: the exit from a rate-limited state was itself rate
        limited, which makes the state absorbing — the only way out was the
        override, and an override typed routinely stops being read. `leave` is
        the caller that most needs this."""
        self.throttle(times=1)
        self.assertEqual(cli.call("http://127.0.0.1:1", "GET", "/p"),
                         {"data": {"ok": True}})
        self.assertEqual(self.attempts["n"], 2)

    def test_it_gives_up_rather_than_retrying_forever(self):
        """A retry policy generous enough to outlast a long window turns a
        throttle into a hang, which is worse than the error it replaces —
        `call` runs inside hooks with their own deadlines."""
        self.throttle(times=99)
        found = cli.call("http://127.0.0.1:1", "GET", "/p")
        self.assertEqual(found["error"], "HTTP 429")
        self.assertEqual(self.attempts["n"], len(cli.RETRY_WAITS) + 1)

    def test_a_429_that_survives_is_NAMED_as_a_rate_limit(self):
        """The operator's response to a throttle and to a dead server are
        opposite — wait versus stop — and issue #15 is that they read
        identically."""
        self.throttle(times=99)
        found = cli.call("http://127.0.0.1:1", "GET", "/p")
        self.assertTrue(found.get("rate_limited"))

    def test_RETRY_AFTER_is_honoured_but_capped(self):
        """The server's own number beats a guess, but a 60s window honoured
        literally inside a Stop hook is a hang."""
        self.throttle(times=1, retry_after="60")
        cli.call("http://127.0.0.1:1", "GET", "/p")
        self.assertEqual(self.slept, [cli.RETRY_WAITS[0]])

    def test_a_SHORTER_retry_after_is_taken_at_its_word(self):
        self.throttle(times=1, retry_after="0.1")
        cli.call("http://127.0.0.1:1", "GET", "/p")
        self.assertEqual(self.slept, [0.1])

    def test_a_nonsense_retry_after_falls_back_to_the_default(self):
        self.throttle(times=1, retry_after="soon")
        cli.call("http://127.0.0.1:1", "GET", "/p")
        self.assertEqual(self.slept, [cli.RETRY_WAITS[0]])

    def test_a_NON_429_error_is_not_retried(self):
        """Retrying a 500 or a 404 buys nothing and doubles the wait before
        the caller learns."""
        state = {"n": 0}

        made = []
        self.addCleanup(lambda: [(e.close(), b.close()) for e, b in made])

        def always_500(*a, **kw):
            state["n"] += 1
            body = io.BytesIO(b"details")
            error = urllib.error.HTTPError("u", 500, "boom", {}, body)
            made.append((error, body))
            raise error
        cli.urllib.request.urlopen = always_500
        found = cli.call("http://127.0.0.1:1", "GET", "/p")
        self.assertEqual(found["error"], "HTTP 500")
        self.assertEqual(state["n"], 1)

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
        # `parsed`, not `json.loads`: a --json verb broken into printing prose
        # makes loads() RAISE, so every assertion below it errors instead of
        # failing and the sweep can only report that something exploded.
        # None flows into the comparisons and they disagree, which is a
        # measurement.
        return parsed(out.getvalue())

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

    def test_it_names_the_WORKSPACE_and_the_SERVER(self):
        """A machine can hold several llm_chat clones, each with its own store
        and its own rooms. Without this, "no such channel" and "you are talking
        to the other workspace" look identical.

        Also: llms.txt promises doctor reports the server. It did not, and the
        documentation would have shipped a pointer to something that does not
        exist — the same dead-remedy class this project spent a day on, inside
        the sentence written to document the fix for it."""
        self.joined()
        report = self.report()
        self.assertIn("workspace", report)
        self.assertIn("server", report)

    def test_a_non_default_server_is_the_one_reported(self):
        """Paired: printing a constant would satisfy the test above while
        telling every workspace it is the default one."""
        self.joined()
        out = io.StringIO()
        with redirect_stdout(out):
            cli.do_doctor("http://localhost:7718")
        self.assertIn("http://localhost:7718", out.getvalue())

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

    # ── the record a handover would have buried (issue #11) ─────────────────

    def exit_history(self, *records):
        d = os.path.join(self.project, ".llm_chat")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "wake.exit"), "w") as f:
            json.dump(list(records), f)

    def test_THE_BURIED_RECORD_IS_SHOWN_when_a_supersede_sits_on_top(self):
        """The whole of #11 from the reading side. A supersede on top says a
        newer waker took over — healthy, and silent about the failure being
        investigated. The reporter found exactly this and the answer was
        already gone."""
        self.joined()
        self.exit_history(
            {"reason": "orphaned — the session that armed it is gone",
             "pid": 41, "at": cli.now_ms() // 1000},
            {"reason": "superseded by a newer waker (healthy)",
             "pid": 503, "at": cli.now_ms() // 1000})
        text = self.report()
        self.assertIn("the one BEFORE it", text)
        self.assertIn("orphaned", text)
        self.assertIn("41", text)

    def test_a_REAL_stop_on_top_is_the_answer_and_nothing_is_dug_up(self):
        """Paired. The buried record only matters when the newest one is a
        handover; showing it always would bury the actual answer in noise."""
        self.joined()
        self.exit_history(
            {"reason": "superseded by a newer waker (healthy)", "pid": 41},
            {"reason": "every joined room is closed — nothing can arrive",
             "pid": 503})
        self.assertNotIn("the one BEFORE it", self.report())

    def test_one_record_alone_has_nothing_underneath_it(self):
        self.joined()
        self.exit_history({"reason": "superseded by a newer waker", "pid": 1})
        self.assertIsNone(cli.masked_exit(self.project))

    def test_THE_SERVER_IT_WAS_POLLING_is_reported(self):
        """"The waker died" and "its backend went away" have opposite remedies
        and were indistinguishable after the fact. The reporter's incident was
        the second: they restarted zonai five minutes before the message."""
        self.joined()
        self.exit_history({"reason": "orphaned", "pid": 41,
                           "server": "http://localhost:7717"})
        text = self.report()
        self.assertIn("http://localhost:7717", text)
        self.assertIn("likelier explanation", text)

    def test_an_UNRECORDED_server_is_not_invented(self):
        """Paired. A waker that predates this field must not be reported as
        polling the default — that is a definite claim about an unknown."""
        self.joined()
        self.exit_history({"reason": "orphaned", "pid": 41})
        self.assertIsNone(cli.last_server(self.project))
        self.assertNotIn("it was polling", self.report())

    def test_the_ONE_RECORD_format_still_reads(self):
        """An installed waker is mid-flight when this ships, and the file it
        already wrote is the one somebody will be interrogating."""
        self.joined()
        self.exited("orphaned — the session that armed it is gone")
        self.assertIn("orphaned", self.report())

    def test_a_history_of_the_WRONG_SHAPE_reads_as_no_record(self):
        self.joined()
        d = os.path.join(self.project, ".llm_chat")
        for junk in ([1, 2, "three"], "a string", 7):
            with self.subTest(junk=junk):
                with open(os.path.join(d, "wake.exit"), "w") as f:
                    json.dump(junk, f)
                self.assertEqual(cli.waker_exits(self.project), [])
                self.assertIn("no exit record", self.report())

    # ── a session that holds no rooms (issue #12) ───────────────────────────

    def session(self, sid, rooms=None):
        d = os.path.join(self.project, ".llm_chat", "sessions", sid)
        os.makedirs(d, exist_ok=True)
        if rooms is not None:
            with open(os.path.join(d, "joined.json"), "w") as f:
                json.dump(rooms, f)
        else:                      # a stub: an id that never joined anything
            open(os.path.join(d, "read.lock"), "w").close()

    def test_A_STUB_SESSION_IS_NAMED_rather_than_reported_healthy(self):
        """Everything else in doctor reports at PROJECT level, which reads as
        fine while this session's waker is looking at nothing. The rooms stay
        with the id that joined them; what created the other id is not known,
        and this asserts the report rather than a cause."""
        self.joined()
        self.session("5930ff25", {"room": {"identity": "me"}})
        self.session("eaf6e8d1")
        os.environ["CLAUDE_CODE_SESSION_ID"] = "eaf6e8d1"
        try:
            text = self.report()
        finally:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        self.assertIn("THIS SESSION IS THE STUB", text)
        self.assertIn("5930ff25", text)
        self.assertIn("NO IDENTITY", text)

    def test_the_session_HOLDING_the_rooms_is_not_accused(self):
        """Paired. Being one of two sessions is not itself a problem — the
        one with an identity is working correctly."""
        self.joined()
        self.session("5930ff25", {"room": {"identity": "me"}})
        self.session("eaf6e8d1")
        os.environ["CLAUDE_CODE_SESSION_ID"] = "5930ff25"
        try:
            text = self.report()
        finally:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        self.assertIn("sessions: 2", text)
        self.assertNotIn("THIS SESSION IS THE STUB", text)

    def test_ONE_SESSION_SAYS_NOTHING_AT_ALL(self):
        """A section that prints every time is a section people learn to skip,
        and one session is the ordinary case."""
        self.joined()
        self.session("5930ff25", {"room": {"identity": "me"}})
        self.assertNotIn("sessions:", self.report())

    def test_a_human_at_a_terminal_is_told_which_they_are(self):
        """No session id in the environment is not a stub — it is a human, and
        a human at a terminal IS the project."""
        self.joined()
        self.session("5930ff25", {"room": {"identity": "me"}})
        self.session("eaf6e8d1")
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        self.assertIn("human at a terminal", self.report())

    # ── is anybody actually here (the host's own answer) ────────────────────

    def hosts(self, sessions, returncode=0, stdout=None):
        """Stub `claude agents --json`."""
        real = cli.subprocess.run

        def run(argv, **kw):
            out = stdout if stdout is not None else json.dumps(sessions)
            return type("R", (), {"returncode": returncode, "stdout": out,
                                  "stderr": ""})()

        cli.subprocess.run = run
        self.addCleanup(lambda: setattr(cli.subprocess, "run", real))

    def test_a_live_session_IN_THIS_PROJECT_is_found(self):
        self.hosts([{"pid": 1, "cwd": "/elsewhere", "sessionId": "aaa"},
                    {"pid": 2, "cwd": self.project, "sessionId": "bbb",
                     "name": "here"}])
        here = cli.live_here(self.project)
        self.assertEqual([s["sessionId"] for s in here], ["bbb"])

    def test_a_session_in_a_SUBDIRECTORY_is_still_this_project(self):
        """A session started in a subdirectory belongs to the project, which
        is the same walk-up `project_dir` already does."""
        self.hosts([{"pid": 2, "cwd": os.path.join(self.project, "lib", "x"),
                     "sessionId": "bbb"}])
        self.assertEqual(len(cli.live_here(self.project)), 1)

    def test_a_session_with_NO_cwd_is_skipped_not_matched(self):
        """The host reports what it has. A record missing `cwd` cannot be
        placed in any project, and treating it as one here would put a live
        agent in whichever project happened to ask."""
        self.hosts([{"pid": 2, "sessionId": "bbb"},
                    {"pid": 3, "cwd": self.project, "sessionId": "ccc"}])
        self.assertEqual([s["sessionId"] for s in cli.live_here(self.project)],
                         ["ccc"])

    def test_a_SIBLING_directory_is_not_this_project(self):
        """Paired, and the reason the match is a prefix on a path separator
        rather than a bare startswith: `/x/llm_chat_old` must not match
        `/x/llm_chat`."""
        self.hosts([{"pid": 2, "cwd": self.project + "_old",
                     "sessionId": "bbb"}])
        self.assertEqual(cli.live_here(self.project), [])

    def test_NOBODY_HOME_is_a_different_answer_from_CANNOT_ASK(self):
        """The distinction the whole check exists for. An empty list means
        the host looked and found nobody; None means the host could not be
        asked, and reporting that as nobody-home would be the inversion this
        file keeps removing."""
        self.hosts([])
        self.assertEqual(cli.live_here(self.project), [])
        self.hosts([], returncode=1)
        self.assertIsNone(cli.live_here(self.project))

    def test_unparseable_or_wrong_shaped_output_is_CANNOT_ASK(self):
        for junk in ("{not json", '"a string"', "7"):
            with self.subTest(junk=junk):
                self.hosts(None, stdout=junk)
                self.assertIsNone(cli.host_sessions())

    def test_a_host_that_is_not_installed_is_CANNOT_ASK(self):
        real = cli.subprocess.run

        def boom(*a, **kw):
            raise FileNotFoundError("no claude on PATH")

        cli.subprocess.run = boom
        self.addCleanup(lambda: setattr(cli.subprocess, "run", real))
        self.assertIsNone(cli.host_sessions())

    def test_doctor_says_NOTHING_CAN_BE_WOKEN_when_nobody_is_home(self):
        """It changes what the waker diagnosis means. "No wake has landed"
        reads as a broken mechanism when somebody is sitting there deaf, and
        as nothing at all when the session ended hours ago."""
        self.joined()
        self.hosts([])
        text = self.report()
        self.assertIn("NO live session", text)
        self.assertIn("nothing is broken", text)

    def test_doctor_NAMES_the_live_agent(self):
        self.joined()
        self.hosts([{"pid": 42, "cwd": self.project, "sessionId": "bbbbbbbb",
                     "name": "worker-7"}])
        text = self.report()
        self.assertIn("worker-7", text)
        self.assertIn("pid 42", text)

    def test_doctor_says_CANNOT_TELL_rather_than_staying_silent(self):
        self.joined()
        self.hosts([], returncode=1)
        text = self.report()
        self.assertIn("CANNOT TELL", text)
        self.assertIn("Not the same as nobody", text)

    def test_THE_HOST_DISAGREEING_WITH_THE_ENVIRONMENT_IS_NAMED(self):
        """Issue #12's ambiguity, answered by a third source. When a reload
        mints a new id the environment and the hook payload disagree and
        neither is authoritative; the host's list belongs to the host."""
        self.joined()
        self.hosts([{"pid": 42, "cwd": self.project, "sessionId": "otherid",
                     "name": "worker-7"}])
        os.environ["CLAUDE_CODE_SESSION_ID"] = "myid"
        try:
            text = self.report()
        finally:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        self.assertIn("HOST DOES NOT LIST THE SESSION", text)

    def test_matching_ids_make_no_such_complaint(self):
        self.joined()
        self.hosts([{"pid": 42, "cwd": self.project, "sessionId": "myid",
                     "name": "worker-7"}])
        os.environ["CLAUDE_CODE_SESSION_ID"] = "myid"
        try:
            text = self.report()
        finally:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        self.assertNotIn("HOST DOES NOT LIST THE SESSION", text)
        self.assertIn("(this session)", text)

    # ── which WINDOW an agent is in ─────────────────────────────────────────

    def ide(self, entries):
        """Stub ~/.claude/ide/<port>.lock."""
        home = tempfile.mkdtemp()
        ide = os.path.join(home, ".claude", "ide")
        os.makedirs(ide)
        for port, folders in entries:
            with open(os.path.join(ide, "%s.lock" % port), "w") as f:
                json.dump({"pid": 1375, "workspaceFolders": folders}, f)
        real = os.path.expanduser
        os.path.expanduser = lambda p: (
            p.replace("~", home, 1) if p.startswith("~") else real(p))
        self.addCleanup(lambda: setattr(os.path, "expanduser", real))

    def test_THE_PORT_IS_WHAT_TELLS_TWO_WINDOWS_APART(self):
        """Every VSCode window on this machine shares one extension-host pid,
        so the pid cannot identify a window and the port can."""
        self.ide([("111", ["/other/project"]),
                  ("222", [self.project])])
        found = cli.ide_window(self.project)
        self.assertEqual(found["port"], "222")

    def test_a_project_in_NO_window_is_None(self):
        self.ide([("111", ["/other/project"])])
        self.assertIsNone(cli.ide_window(self.project))

    def test_a_window_with_SEVERAL_folders_still_matches(self):
        self.ide([("333", ["/somewhere/else", self.project])])
        self.assertEqual(cli.ide_window(self.project)["port"], "333")

    def test_an_unreadable_lock_does_not_stop_the_search(self):
        """One corrupt file must not hide every other window.

        Named to sort BEFORE the real one, because that is the only ordering
        where it could do harm — the first version of this test wrote `999`
        and the match happened before the corrupt file was ever opened, so it
        proved nothing while passing."""
        self.ide([("222", [self.project])])
        ide = os.path.expanduser("~/.claude/ide")
        with open(os.path.join(ide, "000.lock"), "w") as f:
            f.write("{not json")
        # And something that is not a lock at all, which the directory
        # collects: a stray file must be stepped over, not parsed. The NAME
        # matters for the same reason as above — it has to sort before the
        # real lock, or the match returns first and this asserts nothing.
        with open(os.path.join(ide, "000.txt"), "w") as f:
            f.write("ignore me")
        self.assertEqual(cli.ide_window(self.project)["port"], "222")

    def test_no_ide_directory_at_all_is_not_an_error(self):
        """Claude Code outside VSCode writes none of this, and that is a
        normal state rather than a fault."""
        home = tempfile.mkdtemp()
        real = os.path.expanduser
        os.path.expanduser = lambda p: (
            p.replace("~", home, 1) if p.startswith("~") else real(p))
        self.addCleanup(lambda: setattr(os.path, "expanduser", real))
        self.assertIsNone(cli.ide_window(self.project))

    def test_doctor_names_the_window_when_there_is_one(self):
        self.joined()
        self.hosts([{"pid": 42, "cwd": self.project, "sessionId": "bbb",
                     "name": "w"}])
        self.ide([("61888", [self.project])])
        text = self.report()
        self.assertIn("port 61888", text)
        self.assertIn("not a lever", text)

    def test_live_sessions_reports_identity_per_session(self):
        self.session("aaa", {"room": {"identity": "me"}})
        self.session("bbb")
        self.assertEqual(cli.live_sessions(self.project),
                         [("aaa", True), ("bbb", False)])

    def test_a_project_that_never_had_a_session_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(cli.live_sessions(tmp), [])

    def wired(self, checkout, fingerprint="old"):
        write_settings(self.project,
                       PostToolUse=["/x/bin/llm-chat-deliver"],
                       Stop=["/x/bin/llm-chat-wake"],
                       SessionStart=["/x/bin/llm-chat-wake"])
        d = os.path.join(self.project, ".llm_chat")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "installed.json"), "w") as f:
            json.dump({"fingerprint": fingerprint, "checkout": checkout}, f)

    def test_a_DIRECT_consumer_is_not_told_to_reinstall(self):
        """Its hooks are absolute paths into this checkout, so it is already
        running the current scripts and only the STAMP is behind.

        Reported by an agent standing in two places at once: a broadcast told
        the directly-wired population "you need do nothing" while doctor told
        the same population to re-install. Both true about different facts, and
        a consumer cannot follow both. The expensive half is not a wasted
        re-install — it is that a line permanently on, and permanently wrong
        for this reader, teaches them to skip it, and it will be right one day.
        """
        here = os.path.dirname(os.path.dirname(os.path.abspath(cli.__file__)))
        self.wired(here, fingerprint="old-stamp")
        text = self.report()
        self.assertIn("stamp is behind", text)
        self.assertNotIn("STALE:", text)
        self.assertIn("already running the current scripts", text)

    def test_a_VENDORED_consumer_that_drifted_still_gets_the_loud_one(self):
        """Paired, and the reason the check exists at all: a vendored copy that
        has drifted really is running old code, and softening that for everyone
        would remove the only warning that matters."""
        with tempfile.TemporaryDirectory() as tree:
            os.makedirs(os.path.join(tree, "bin"))
            self.wired(tree, fingerprint="old-stamp")
            text = self.report()
            self.assertIn("STALE", text)
            self.assertIn("install.sh", text)

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

    def test_a_DIRTY_source_tree_is_named_as_dirty(self):
        """Covered by ACCIDENT until now. Nothing stubbed `checkout_dirty`, so
        it ran against the real repo and this branch executed only while THIS
        working tree happened to have uncommitted changes — which it does for
        the whole time anyone is working, and stops having the moment they
        commit. The line went uncovered on the first clean-tree run, which is
        precisely when the gate runs before a push.

        Coverage that depends on git state is not coverage, and the failure
        arrives at the least convenient moment by construction.
        """
        with tempfile.TemporaryDirectory() as tree:
            os.makedirs(os.path.join(tree, "bin"))
            self.wired(tree, fingerprint="old-stamp")
            real = cli.checkout_dirty
            cli.checkout_dirty = lambda path=None: True
            try:
                dirty = self.report()
            finally:
                cli.checkout_dirty = real
            self.assertIn("UNCOMMITTED", dirty)
            self.assertIn("safer side", dirty)

    def test_a_CLEAN_source_tree_is_not(self):
        """Paired. A notice that always fires is not a notice."""
        with tempfile.TemporaryDirectory() as tree:
            os.makedirs(os.path.join(tree, "bin"))
            self.wired(tree, fingerprint="old-stamp")
            real = cli.checkout_dirty
            cli.checkout_dirty = lambda path=None: False
            try:
                clean = self.report()
            finally:
                cli.checkout_dirty = real
            self.assertIn("STALE", clean)
            self.assertNotIn("UNCOMMITTED", clean)

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

    def test_a_live_waker_is_reported_as_POLLING(self):
        """Renamed with the claim it makes. Polling and waking are two facts,
        and this line used to assert the second on the strength of the first —
        which is how a host that ignores asyncRewake read as healthy."""
        self.joined_with_waker(os.getpid())
        self.assertIn("polling now: yes", self.report())

    def test_doctor_PRINTS_the_stale_diagnosis_and_the_remedy(self):
        """A helper nobody calls catches nothing, and the remedy is the part
        that matters: restarting a bridge or re-running install.sh does NOT
        restart the server, which is exactly how both agents got here."""
        self.joined_with_waker(os.getpid())
        real = cli.server_is_current
        cli.server_is_current = lambda server: "stale"
        try:
            text = self.report()
        finally:
            cli.server_is_current = real
        self.assertIn("SERVER IS STALE", text)
        self.assertIn("silently", text)
        self.assertIn("zonai serve", text)

    def test_doctor_reports_a_current_server_without_alarm(self):
        """Paired: a line that always warns is one nobody reads."""
        self.joined_with_waker(os.getpid())
        real = cli.server_is_current
        cli.server_is_current = lambda server: "current"
        try:
            text = self.report()
        finally:
            cli.server_is_current = real
        self.assertIn("server build        current", text)
        self.assertNotIn("SERVER IS STALE", text)

    def test_doctor_says_CANNOT_TELL_when_nothing_answered(self):
        self.joined_with_waker(os.getpid())
        real = cli.server_is_current
        cli.server_is_current = lambda server: None
        try:
            text = self.report()
        finally:
            cli.server_is_current = real
        self.assertIn("CANNOT TELL", text)

    def test_A_STALE_SERVER_IS_NAMED_rather_than_reported_as_the_url(self):
        """The gap that cost two agents hours in one day: doctor reported the
        URL it was CONFIGURED with, which is not a claim about the process
        listening there. A server predating the migration accepts a write
        carrying an unknown column and silently drops it."""
        real = cli.call
        cli.call = lambda *a, **kw: {"error": "HTTP 500", "body": "no column"}
        try:
            self.assertEqual(cli.server_is_current("http://x"), "stale")
        finally:
            cli.call = real

    def test_a_server_that_is_NOT_THERE_is_not_reported_as_stale(self):
        """Two different diagnoses with two different remedies. Collapsing
        them would send somebody restarting a server that is not running."""
        real = cli.call
        cli.call = lambda *a, **kw: {"error": "no llm_chat server at http://x"}
        try:
            self.assertIsNone(cli.server_is_current("http://x"))
        finally:
            cli.call = real

    def test_a_current_server_says_so(self):
        real = cli.call
        cli.call = lambda *a, **kw: {"data": {"items": []}}
        try:
            self.assertEqual(cli.server_is_current("http://x"), "current")
        finally:
            cli.call = real

    def test_the_probe_WRITES_NOTHING(self):
        """It runs on every doctor. A probe with a side effect would put a row
        in somebody's transcript each time they asked why nothing arrived."""
        seen = []
        real = cli.call
        cli.call = lambda server, method, path, body=None, query=None, **kw: (
            seen.append((method, path)) or {"data": {"items": []}})
        try:
            cli.server_is_current("http://x")
        finally:
            cli.call = real
        self.assertEqual([m for m, _ in seen], ["GET"])

    def test_doctor_says_CANNOT_TELL_rather_than_nothing_armed(self):
        """A pidfile holding rubbish means a waker MAY be running. Saying "no
        waker has been armed" is a definite claim about an indefinite state,
        in the command whose whole job is removing uncertainty."""
        self.joined_with_waker(os.getpid())
        with open(os.path.join(self.project, ".llm_chat", "wake.pid"),
                  "w") as f:
            f.write("\x00\x01binary")
        text = self.report()
        self.assertIn("cannot be read as a pid", text)
        self.assertNotIn("no waker has been armed", text)

    def test_POLLING_IS_NOT_WAKING_until_one_has_landed(self):
        """Issue #6. The poll runs, the pid rotates, and if the host drops
        exit 2 the messages arrive only when the agent next runs a tool."""
        self.joined_with_waker(os.getpid())
        text = self.report()
        self.assertIn("NO WAKE HAS EVER BEEN OBSERVED LANDING", text)

    def landed(self, **fields):
        record = {"at": cli.now_ms() / 1000.0, "host": "cli"}
        record.update(fields)
        with open(os.path.join(self.project, ".llm_chat", "wake.landed"),
                  "w") as f:
            json.dump(record, f)

    def test_a_landing_is_reported_when_there_is_one(self):
        self.joined_with_waker(os.getpid())
        self.landed(event="Stop")
        self.assertIn("a wake LANDED", self.report())

    # ── issue #13: worked ONCE is not works NOW ─────────────────────────────

    def test_THE_AGE_IS_REPORTED_not_just_the_existence(self):
        """It stayed on the screen for ninety minutes while every wake failed,
        and the agent reading it told a human twice that the mechanism worked.
        "94m ago" is immediately actionable; "a wake has landed here" is
        not."""
        self.joined_with_waker(os.getpid())
        self.landed(at=cli.now_ms() / 1000.0 - 5640, event="Stop")
        self.assertIn("94m ago", self.report())

    def test_a_SESSION_START_landing_is_not_offered_as_evidence(self):
        """It proves the hook runs, not that `asyncRewake` reaches anybody —
        and on this host a window reload is exactly what tends to happen near
        an unanswered wake."""
        self.joined_with_waker(os.getpid())
        self.landed(event="SessionStart")
        text = self.report()
        self.assertIn("SESSION START", text)
        self.assertIn("not evidence", text)
        self.assertNotIn("so replies arrive on their own", text)

    def test_a_marker_with_no_provenance_says_it_CANNOT_TELL(self):
        """Every existing marker is one of these. Reading it as a confirmed
        turn would preserve the bug for everybody who already has one, which
        is everybody, the first time they upgrade."""
        self.joined_with_waker(os.getpid())
        self.landed()
        text = self.report()
        self.assertIn("before this check knew to record WHAT", text)
        self.assertNotIn("so replies arrive on their own", text)

    def test_landing_provenance_reads_three_ways(self):
        self.joined_with_waker(os.getpid())
        self.landed(event="Stop")
        self.assertIs(cli.landing_is_confirmed(self.project), True)
        self.landed(event="SessionStart")
        self.assertIs(cli.landing_is_confirmed(self.project), False)
        self.landed()
        self.assertIsNone(cli.landing_is_confirmed(self.project))

    def test_no_marker_at_all_is_not_a_provenance_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(cli.landing_is_confirmed(tmp))

    def test_a_corrupt_marker_is_not_a_provenance_answer(self):
        self.joined_with_waker(os.getpid())
        with open(os.path.join(self.project, ".llm_chat", "wake.landed"),
                  "w") as f:
            f.write("{not json")
        self.assertIsNone(cli.landing_is_confirmed(self.project))

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
        self.assertFalse(cli.waker_alive(self.project)[1])

    def test_UNREADABLE_IS_NOT_THE_SAME_AS_ABSENT(self):
        """This fixture used to assert they were identical — a test written
        from the same belief as the code, confirming a conflation rather than
        catching it. A pidfile holding rubbish means a waker MAY be running;
        no pidfile means none was armed. doctor states one of those as fact."""
        self.write_pid("not-a-pid")
        unreadable = cli.waker_alive(self.project)
        os.remove(os.path.join(self.project, ".llm_chat", "wake.pid"))
        self.assertNotEqual(unreadable, cli.waker_alive(self.project))

