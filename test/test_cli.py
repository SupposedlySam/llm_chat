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
        # THE MACHINE-WIDE SKILL IS NOT THIS PROJECT'S STATE. `doctor` now
        # reads ~/.claude/skills/llm-chat/SKILL.md, which is one file for the
        # whole machine and belongs to whoever ran install.sh last — so
        # without this every assertion in this class depends on that, and one
        # of them started failing the moment the real file pointed at another
        # checkout. Same warm-tree coupling the cold-clone run found twice;
        # the tests that care about the skill say so explicitly.
        # Kept before stubbing, so the one test that exercises the REAL
        # reader still can — the same thing that caught me stubbing
        # `fingerprint_of` in test_hooks an hour earlier.
        self.real_skill_checkout = cli.skill_checkout
        cli.skill_checkout = lambda: None
        self.addCleanup(
            lambda: setattr(cli, "skill_checkout", self.real_skill_checkout))

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

    # ── a live pid is not a live waker ──────────────────────────────────────

    def beat(self, ago):
        path = os.path.join(self.project, ".llm_chat", "wake.alive")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"pid": 1, "at": int(cli.now_ms() / 1000) - ago}, f)

    def test_a_FRESH_heartbeat_says_it_went_round_and_found_nothing(self):
        """gameloop's ask, from the other side of the wire: "a dead waker and
        a quiet room are the same observation from in here", and a timestamp
        the waker touches is worth more than the feature.

        It used to be written ONCE, before the loop — a birth certificate, not
        a heartbeat — so a waker that armed and then died left a mark
        identical to a healthy one."""
        self.joined_with_waker(os.getpid())
        self.beat(ago=30)
        text = self.report()
        # A RANGE, not an exact second: the fixture stamps the file and doctor
        # reads the clock a moment later, so `30` was off by one the first
        # time this ran. A test that pins a number it does not control fails
        # for a reason that has nothing to do with the behaviour.
        self.assertRegex(text, r"last heartbeat 3[0-9]s ago")
        self.assertIn("different from not running", text)

    def test_a_STALE_heartbeat_is_named_even_though_the_pid_is_alive(self):
        """The case the stamp exists for. A process can be stopped, blocked on
        a socket nobody will ring, or wedged after the machine slept — and
        from outside every one of those looks like a quiet room."""
        self.joined_with_waker(os.getpid())
        self.beat(ago=4 * cli.WAKER_HEARTBEAT_SEC)
        text = self.report()
        self.assertIn("LAST HEARTBEAT", text)
        self.assertIn("is NOT going", text)

    def test_NO_heartbeat_is_cannot_say_rather_than_healthy(self):
        """Absence of evidence was the thing being fixed, so it must not be
        reported as evidence of health."""
        self.joined_with_waker(os.getpid())
        text = self.report()
        self.assertIn("no heartbeat recorded", text)
        self.assertIn("cannot say so", text)

    def test_a_corrupt_alive_file_reads_as_no_heartbeat(self):
        path = os.path.join(self.project, ".llm_chat", "wake.alive")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("{not json")
        self.assertIsNone(cli.heartbeat_age(self.project))

    def test_an_alive_file_with_no_timestamp_is_no_heartbeat(self):
        path = os.path.join(self.project, ".llm_chat", "wake.alive")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"pid": 1}, f)
        self.assertIsNone(cli.heartbeat_age(self.project))

    def test_the_two_copies_of_the_interval_agree(self):
        """Duplicated across two standalone scripts the way doorbell_dir is —
        and a duplicated constant that drifts makes the staleness threshold
        wrong in exactly the direction that reports a dead waker as fine."""
        waker = load("llm-chat-wake")
        self.assertEqual(cli.WAKER_HEARTBEAT_SEC, waker.HEARTBEAT_SEC_DEFAULT)

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

    # ── identity → live session, the mapping #19 rebuilt badly outside ─────

    def joined_at(self, where, rooms):
        """Write a joined.json the way the CLI does, at a given base."""
        os.makedirs(where, exist_ok=True)
        with open(os.path.join(where, "joined.json"), "w") as f:
            json.dump(rooms, f)

    def test_a_live_sessions_identity_is_read_from_ITS_OWN_store(self):
        base = os.path.join(self.project, ".llm_chat", "sessions", "sid-1")
        self.joined_at(base, {"room": {"identity": "worker-7"}})
        self.hosts([{"pid": 1, "cwd": self.project, "sessionId": "sid-1"}])
        self.assertIn("worker-7", cli.live_identities())

    def test_a_session_with_NO_store_of_its_own_uses_the_project_one(self):
        """Measured across this machine, one checkout runs entirely on the
        project file. Skipping the fallback reports that agent dead while it
        is answering — and #19's wording would then be confidently wrong
        about the one case it exists to report."""
        self.joined_at(os.path.join(self.project, ".llm_chat"),
                       {"room": {"identity": "project-wide"}})
        self.hosts([{"pid": 1, "cwd": self.project, "sessionId": "no-store"}])
        self.assertIn("project-wide", cli.live_identities())

    def test_the_sessions_OWN_store_wins_over_the_project_one(self):
        """Paired with the fallback: a session that has moved to its own
        identity must not still answer to the project's older one."""
        self.joined_at(os.path.join(self.project, ".llm_chat"),
                       {"room": {"identity": "stale"}})
        self.joined_at(
            os.path.join(self.project, ".llm_chat", "sessions", "sid-1"),
            {"room": {"identity": "current"}})
        self.hosts([{"pid": 1, "cwd": self.project, "sessionId": "sid-1"}])
        live = cli.live_identities()
        self.assertIn("current", live)
        self.assertNotIn("stale", live)

    def test_an_identity_with_no_live_session_is_simply_absent(self):
        self.joined_at(os.path.join(self.project, ".llm_chat", "sessions",
                                    "sid-1"),
                       {"room": {"identity": "worker-7"}})
        self.hosts([{"pid": 1, "cwd": self.project, "sessionId": "sid-1"}])
        self.assertNotIn("lead-ml", cli.live_identities())

    def test_a_host_that_CANNOT_BE_ASKED_is_None_not_an_empty_mapping(self):
        """The whole complaint in #19 is a wake reported identically whether
        anybody was there. An empty dict would let a caller answer "nobody is
        alive" from a question that was never asked."""
        self.hosts([], returncode=1)
        self.assertIsNone(cli.live_identities())
        self.hosts([])
        self.assertEqual(cli.live_identities(), {})

    def test_a_session_the_host_describes_incompletely_is_SKIPPED(self):
        """No cwd or no sessionId means there is nowhere to look. Guessing
        would attribute a live session to whichever project asked."""
        self.hosts([{"pid": 1, "sessionId": "sid-1"},
                    {"pid": 2, "cwd": self.project}])
        self.assertEqual(cli.live_identities(), {})

    def test_a_corrupt_joined_file_does_not_take_the_whole_mapping_down(self):
        """One unreadable checkout among several must not make every other
        agent read as dead."""
        good = os.path.join(self.project, ".llm_chat", "sessions", "good")
        self.joined_at(good, {"room": {"identity": "fine"}})
        bad = os.path.join(self.project, ".llm_chat", "sessions", "bad")
        os.makedirs(bad, exist_ok=True)
        with open(os.path.join(bad, "joined.json"), "w") as f:
            f.write("{not json")
        self.hosts([{"pid": 1, "cwd": self.project, "sessionId": "good"},
                    {"pid": 2, "cwd": self.project, "sessionId": "bad"}])
        self.assertIn("fine", cli.live_identities())

    # ── issue #21: over-attribution, which fails in the dangerous direction ─

    def declared(self, sid, identity):
        where = os.path.join(self.project, ".llm_chat", "sessions", sid)
        os.makedirs(where, exist_ok=True)
        with open(os.path.join(where, "identity.json"), "w") as f:
            json.dump({"identity": identity}, f)

    def how_for(self, live, identity, sid):
        for row in live.get(identity, []):
            if row.get("sessionId") == sid:
                return row.get("llm_chat_how")
        return None

    def test_a_DECLARED_identity_is_marked_as_declared(self):
        """`identity.json` IS written per session — I said otherwise in #19
        and it was false. `identity_path()` has been session-scoped since
        `identify` in one session was found renaming every other session in
        the checkout."""
        self.declared("sid-1", "orchestrator")
        self.hosts([{"pid": 1, "cwd": self.project, "sessionId": "sid-1"}])
        live = cli.live_identities()
        self.assertEqual(self.how_for(live, "orchestrator", "sid-1"),
                         ["declared"])

    def test_a_ROOM_JOIN_says_WHICH_ROOM(self):
        """A session genuinely holds a different identity per room —
        game_loop's c9156a5d is `gameloop` in #llm_chat_owner and `owner` in
        #game_loop_owner, which is the data rather than a defect. Reporting
        the union as "the session's identities" is what made one session
        appear under two names with no way to see why."""
        self.joined_at(
            os.path.join(self.project, ".llm_chat", "sessions", "sid-1"),
            {"alpha": {"identity": "one"}, "beta": {"identity": "two"}})
        self.hosts([{"pid": 1, "cwd": self.project, "sessionId": "sid-1"}])
        live = cli.live_identities()
        self.assertEqual(self.how_for(live, "one", "sid-1"), ["joined #alpha"])
        self.assertEqual(self.how_for(live, "two", "sid-1"), ["joined #beta"])

    def test_the_DECLARATION_is_listed_before_the_room_joins(self):
        """Strongest evidence first, and it is not cosmetic: `who` prints the
        list in order, so a reader skimming the first item must land on the
        thing that settles it rather than on one room among several."""
        self.declared("sid-1", "orchestrator")
        self.joined_at(
            os.path.join(self.project, ".llm_chat", "sessions", "sid-1"),
            {"alpha": {"identity": "orchestrator"}})
        self.hosts([{"pid": 1, "cwd": self.project, "sessionId": "sid-1"}])
        self.assertEqual(self.how_for(cli.live_identities(), "orchestrator",
                                      "sid-1"),
                         ["declared", "joined #alpha"])

    def test_a_session_declaring_AND_joining_elsewhere_shows_both(self):
        """The declaration does not suppress a room joined under a different
        name — they are different claims, and the room one is what answers
        "will a message to that name in that room reach anybody"."""
        self.declared("sid-1", "orchestrator")
        self.joined_at(
            os.path.join(self.project, ".llm_chat", "sessions", "sid-1"),
            {"alpha": {"identity": "backcompat"}})
        self.hosts([{"pid": 1, "cwd": self.project, "sessionId": "sid-1"}])
        live = cli.live_identities()
        self.assertEqual(self.how_for(live, "orchestrator", "sid-1"),
                         ["declared"])
        self.assertEqual(self.how_for(live, "backcompat", "sid-1"),
                         ["joined #alpha"])

    def test_TWO_undeclared_sessions_in_one_checkout_claim_NOBODY(self):
        """#21's core. The project store cannot say which session wrote it,
        and claiming both makes a DEAD identity look alive — so the caller
        nudges a room nobody is reading, which is the exact failure the verb
        exists to prevent."""
        self.joined_at(os.path.join(self.project, ".llm_chat"),
                       {"room": {"identity": "backcompat"}})
        self.hosts([{"pid": 1, "cwd": self.project, "sessionId": "a"},
                    {"pid": 2, "cwd": self.project, "sessionId": "b"}])
        self.assertEqual(cli.live_identities(), {})

    def test_ONE_undeclared_session_still_gets_the_project_file(self):
        """Paired, and the reason this is not simply deleted: measured across
        this machine, one checkout runs entirely on the project store, and
        skipping it reports that agent dead while it is answering."""
        self.joined_at(os.path.join(self.project, ".llm_chat"),
                       {"room": {"identity": "backcompat"}})
        self.hosts([{"pid": 1, "cwd": self.project, "sessionId": "a"}])
        self.assertIn("backcompat", cli.live_identities())

    def test_the_project_file_attribution_is_marked_INFERRED(self):
        """It is a guess from a file that cannot name its author, and #21
        asked for the guessed half to be visible as a guess."""
        self.joined_at(os.path.join(self.project, ".llm_chat"),
                       {"room": {"identity": "backcompat"}})
        self.hosts([{"pid": 1, "cwd": self.project, "sessionId": "a"}])
        self.assertIn("inferred",
                      self.how_for(cli.live_identities(), "backcompat", "a")[0])

    def test_a_DECLARED_session_does_not_make_its_NEIGHBOUR_ambiguous(self):
        """A session with evidence of its own is not competing for the shared
        file, so one declared session plus one bare one still resolves."""
        self.declared("mine", "orchestrator")
        self.joined_at(os.path.join(self.project, ".llm_chat"),
                       {"room": {"identity": "backcompat"}})
        self.hosts([{"pid": 1, "cwd": self.project, "sessionId": "mine"},
                    {"pid": 2, "cwd": self.project, "sessionId": "bare"}])
        live = cli.live_identities()
        self.assertEqual(self.how_for(live, "orchestrator", "mine"),
                         ["declared"])
        self.assertIn("backcompat", live)
        self.assertNotIn("bare", [s.get("sessionId")
                                  for s in live.get("orchestrator", [])])

    def test_a_declared_session_is_NEVER_claimed_by_the_shared_file(self):
        """The reporter's rule, and the one that matters most: a session that
        said who it is must not also be attributed to somebody else."""
        self.declared("mine", "orchestrator")
        self.joined_at(os.path.join(self.project, ".llm_chat"),
                       {"room": {"identity": "backcompat"}})
        self.hosts([{"pid": 1, "cwd": self.project, "sessionId": "mine"}])
        live = cli.live_identities()
        self.assertNotIn("mine", [s.get("sessionId")
                                  for s in live.get("backcompat", [])])

    def test_a_SHARED_FILE_naming_several_identities_claims_nobody(self):
        """The same coin flip one level down: it says this CHECKOUT talks
        under several names, not which of them this session is."""
        self.joined_at(os.path.join(self.project, ".llm_chat"),
                       {"a": {"identity": "one"}, "b": {"identity": "two"}})
        self.hosts([{"pid": 1, "cwd": self.project, "sessionId": "bare"}])
        self.assertEqual(cli.live_identities(), {})

    def test_a_project_identity_json_beats_the_project_joined_json(self):
        """A shared declaration is still a declaration — weaker than a
        session's own, stronger than reading room names off a membership
        file."""
        self.joined_at(os.path.join(self.project, ".llm_chat"),
                       {"room": {"identity": "from-rooms"}})
        with open(os.path.join(self.project, ".llm_chat", "identity.json"),
                  "w") as f:
            json.dump({"identity": "from-declaration"}, f)
        self.hosts([{"pid": 1, "cwd": self.project, "sessionId": "bare"}])
        live = cli.live_identities()
        self.assertIn("from-declaration", live)
        self.assertNotIn("from-rooms", live)

    def test_sessions_in_DIFFERENT_checkouts_do_not_make_each_other_ambiguous(self):
        """The ambiguity is per directory, because the file being guessed at
        is per directory."""
        other = os.path.join(self.tmp.name, "other")
        os.makedirs(os.path.join(other, ".llm_chat"), exist_ok=True)
        self.joined_at(os.path.join(self.project, ".llm_chat"),
                       {"room": {"identity": "here"}})
        self.joined_at(os.path.join(other, ".llm_chat"),
                       {"room": {"identity": "there"}})
        self.hosts([{"pid": 1, "cwd": self.project, "sessionId": "a"},
                    {"pid": 2, "cwd": other, "sessionId": "b"}])
        live = cli.live_identities()
        self.assertIn("here", live)
        self.assertIn("there", live)

    def test_who_PRINTS_how_each_row_was_attributed(self):
        self.declared("sid-1", "orchestrator")
        self.hosts([{"pid": 1, "cwd": self.project, "sessionId": "sid-1"}])
        _, text = self.whoami()
        self.assertIn("how: declared", text)

    def test_who_EXPLAINS_inferred_when_it_shows_one(self):
        """The word alone is not enough — a reader needs to know it is a
        guess from a file that cannot name its author, and that the silent
        case is stricter rather than looser."""
        self.joined_at(os.path.join(self.project, ".llm_chat"),
                       {"room": {"identity": "backcompat"}})
        self.hosts([{"pid": 1, "cwd": self.project, "sessionId": "a"}])
        _, text = self.whoami()
        self.assertIn("GUESS", text)

    def test_who_does_NOT_explain_inferred_when_nothing_is(self):
        """A footnote on every listing is a footnote nobody reads."""
        self.declared("sid-1", "orchestrator")
        self.hosts([{"pid": 1, "cwd": self.project, "sessionId": "sid-1"}])
        _, text = self.whoami()
        self.assertNotIn("GUESS", text)

    def test_who_json_carries_an_INFERRED_flag_a_script_can_branch_on(self):
        """A sentence is not something a caller branches on, and the whole
        request was that the guessed half be visible to a program."""
        self.joined_at(os.path.join(self.project, ".llm_chat"),
                       {"room": {"identity": "backcompat"}})
        self.hosts([{"pid": 1, "cwd": self.project, "sessionId": "a"}])
        _, text = self.whoami(as_json=True)
        row = json.loads(text)["identities"][0]["sessions"][0]
        self.assertIs(row["inferred"], True)

    def test_who_json_does_not_mark_a_DECLARATION_as_inferred(self):
        self.declared("sid-1", "orchestrator")
        self.hosts([{"pid": 1, "cwd": self.project, "sessionId": "sid-1"}])
        _, text = self.whoami(as_json=True)
        row = json.loads(text)["identities"][0]["sessions"][0]
        self.assertIs(row["inferred"], False)

    def test_one_session_in_FOUR_ROOMS_is_listed_once(self):
        """joined.json is keyed by room, so the obvious loop lists the same
        session once per room. `who` printed exactly that on its first real
        run, and any count taken from this mapping would have been a count of
        memberships wearing a session's name."""
        self.joined_at(
            os.path.join(self.project, ".llm_chat", "sessions", "sid-1"),
            {name: {"identity": "busy"} for name in "abcd"})
        self.hosts([{"pid": 1, "cwd": self.project, "sessionId": "sid-1"}])
        self.assertEqual(len(cli.live_identities()["busy"]), 1)

    def whoami(self, as_json=False):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.do_who(as_json)
        return code, out.getvalue()

    def test_who_prints_the_FULL_session_id(self):
        """The first thing #19's hand-rolled version got wrong: `doctor`
        truncates to 8 characters, so comparing against a full uuid matched
        nothing and every session read as dead, including its own."""
        self.joined_at(
            os.path.join(self.project, ".llm_chat", "sessions", "sid-1"),
            {"room": {"identity": "worker-7"}})
        full = "73ce3b55-7a02-469e-a10e-ee86da7e1737"
        self.hosts([{"pid": 1, "cwd": self.project, "sessionId": full}])
        # The host's id is what gets printed; the store is keyed by the id the
        # session actually joined under, which is why this fixture has both.
        self.joined_at(
            os.path.join(self.project, ".llm_chat", "sessions", full),
            {"room": {"identity": "worker-7"}})
        _, text = self.whoami()
        self.assertIn(full, text)

    def test_who_CANNOT_TELL_exits_nonzero(self):
        """The whole point of the verb. A caller scripting against it gets an
        empty list either way — only the status separates "nobody is running"
        from "nothing answered", and conflating those is both open issues."""
        self.hosts([], returncode=1)
        code, text = self.whoami()
        self.assertEqual(code, 1)
        self.assertIn("CANNOT TELL", text)

    def test_who_NOBODY_LIVE_is_a_success_and_says_it_asked(self):
        self.hosts([])
        code, text = self.whoami()
        self.assertEqual(code, 0)
        self.assertIn("was asked and answered", text)

    def test_who_is_REACHABLE_from_the_command_line(self):
        """Through `main`, not by calling `do_who`. Every other test here
        holds the function; a verb wired to the wrong handler, or not wired at
        all, would pass all of them and fail for every user — and the parser
        test only proves the FLAG parses, never that anything runs."""
        self.hosts([], returncode=1)
        argv = sys.argv
        sys.argv = ["llm_chat", "who"]
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                code = cli.main()
        finally:
            sys.argv = argv
        self.assertEqual(code, 1)
        self.assertIn("CANNOT TELL", out.getvalue())

    def test_who_json_marks_whether_the_host_was_ASKED(self):
        """Both cases produce an empty list, so the flag is the only thing
        carrying the difference into a program."""
        self.hosts([], returncode=1)
        _, unasked = self.whoami(as_json=True)
        self.assertIs(json.loads(unasked)["asked"], False)
        self.hosts([])
        _, asked = self.whoami(as_json=True)
        self.assertIs(json.loads(asked)["asked"], True)

    def test_one_identity_in_two_live_sessions_keeps_both(self):
        for sid in ("a", "b"):
            self.joined_at(
                os.path.join(self.project, ".llm_chat", "sessions", sid),
                {"room": {"identity": "twin"}})
        self.hosts([{"pid": 1, "cwd": self.project, "sessionId": "a"},
                    {"pid": 2, "cwd": self.project, "sessionId": "b"}])
        self.assertEqual(len(cli.live_identities()["twin"]), 2)

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
        # `(x or {}).get`, because the failure being guarded against is that
        # the search gives up and returns NOTHING — and indexing into None
        # raised TypeError before this could disagree with it. The sweep read
        # that as "crashed, not measured" (#22).
        self.assertEqual((cli.ide_window(self.project) or {}).get("port"),
                         "222")

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

    def skill_names(self, checkout):
        """Stand in for the MACHINE-WIDE skill naming a checkout."""
        real = cli.skill_checkout
        cli.skill_checkout = lambda: checkout
        self.addCleanup(lambda: setattr(cli, "skill_checkout", real))

    def prints(self, table):
        real = cli.wiring_fingerprint
        cli.wiring_fingerprint = lambda tree: table.get(os.path.abspath(tree))
        self.addCleanup(lambda: setattr(cli, "wiring_fingerprint", real))

    def test_A_MACHINE_WIDE_SKILL_NAMING_ANOTHER_BUILD_IS_REPORTED(self):
        """`~/.claude/skills/` is ONE file for the whole machine, and
        `install.sh` rewrites it with whatever checkout it was run from — so
        it is last-writer-wins with nothing recording who won.

        Found by running gameloop's radius questions on this repo. The
        installed skill was sending every agent on this machine to a vendored
        copy under another project's `.lamp/`, whose CLI has no `who`, no
        `--since`, and still accepted `--to-a` as `--to-all` — the silent
        wrong-audience bug closed in #23.

        `stale_skill_report` does not cover it: that looks for a leftover
        PER-REPO copy, which was the previous scheme's problem. The
        machine-wide file replaced it and nothing watched the replacement."""
        with tempfile.TemporaryDirectory() as other:
            os.makedirs(os.path.join(other, "bin"))
            self.skill_names(other)
            self.prints({os.path.abspath(other): "aaaa",
                         os.path.abspath(cli.ROOT): "bbbb"})
            text = cli.divergent_skill_report()
        # NOT the hook warning's phrase. Two findings with two remedies must
        # be distinguishable from the words alone, or a test for one trips on
        # the other — which is exactly what happened when both said
        # "DIFFERENT BUILD".
        self.assertIn("SENDS AGENTS ELSEWHERE", text)
        self.assertIn("whole machine", text)

    def test_the_SAME_build_at_another_path_is_silent(self):
        """Fingerprints, not paths. A machine with several checkouts of the
        same build is fine, and a warning that fires at the wrong population
        is one people learn to skip."""
        with tempfile.TemporaryDirectory() as other:
            os.makedirs(os.path.join(other, "bin"))
            self.skill_names(other)
            self.prints({os.path.abspath(other): "same",
                         os.path.abspath(cli.ROOT): "same"})
            self.assertEqual(cli.divergent_skill_report(), "")

    def test_the_skill_naming_THIS_checkout_is_silent(self):
        self.skill_names(os.path.abspath(cli.ROOT))
        self.assertEqual(cli.divergent_skill_report(), "")

    def test_no_machine_wide_skill_at_all_is_silent(self):
        """Not every install has one, and absence is not a fault."""
        self.skill_names(None)
        self.assertEqual(cli.divergent_skill_report(), "")

    def test_a_skill_naming_a_tree_that_is_GONE_says_so(self):
        """Different remedy: nothing is being run from there at all, and
        comparing fingerprints against a missing tree would report nothing."""
        self.skill_names("/no/such/checkout")
        self.assertIn("GONE", cli.divergent_skill_report())

    def test_the_prescribed_checkout_is_read_from_the_skill_TEXT(self):
        """The path is the fact that matters and it is in the file; a lookup
        that guessed from anything else would answer for the wrong copy."""
        home = tempfile.mkdtemp()
        where = os.path.join(home, ".claude", "skills", "llm-chat")
        os.makedirs(where)
        with open(os.path.join(where, "SKILL.md"), "w") as f:
            f.write("run /some/tree/bin/llm_chat setup <channel>\n")
        real = os.path.expanduser
        os.path.expanduser = lambda p: (p.replace("~", home, 1)
                                        if p.startswith("~") else real(p))
        self.addCleanup(lambda: setattr(os.path, "expanduser", real))
        self.assertEqual(self.real_skill_checkout(), "/some/tree")

    def test_a_skill_naming_TWO_checkouts_reads_as_none(self):
        """One name is an answer; several are not, and offering one of them
        is a coin flip presented as a fact.

        `found[0]` was safe only because the template is generated by a single
        global `sed`, so every occurrence is the same path BY CONSTRUCTION —
        safety living in the generator rather than in this reader. auditor's
        rule: selecting by POSITION when the claim is about IDENTITY. Same
        resolution as the shared-file ambiguity in `live_identities`."""
        home = tempfile.mkdtemp()
        where = os.path.join(home, ".claude", "skills", "llm-chat")
        os.makedirs(where)
        with open(os.path.join(where, "SKILL.md"), "w") as f:
            f.write("run /one/tree/bin/llm_chat setup\n"
                    "or maybe /another/tree/bin/llm_chat setup\n")
        real = os.path.expanduser
        os.path.expanduser = lambda p: (p.replace("~", home, 1)
                                        if p.startswith("~") else real(p))
        self.addCleanup(lambda: setattr(os.path, "expanduser", real))
        self.assertIsNone(self.real_skill_checkout())

    def test_the_SAME_path_named_twice_is_still_an_answer(self):
        """Paired, and the ordinary case: the generator substitutes globally,
        so a skill mentioning the command three times names one checkout three
        times. Refusing that would silence the check for every real file."""
        home = tempfile.mkdtemp()
        where = os.path.join(home, ".claude", "skills", "llm-chat")
        os.makedirs(where)
        with open(os.path.join(where, "SKILL.md"), "w") as f:
            f.write("run /one/tree/bin/llm_chat setup\n"
                    "then /one/tree/bin/llm_chat say <room>\n")
        real = os.path.expanduser
        os.path.expanduser = lambda p: (p.replace("~", home, 1)
                                        if p.startswith("~") else real(p))
        self.addCleanup(lambda: setattr(os.path, "expanduser", real))
        self.assertEqual(self.real_skill_checkout(), "/one/tree")

    def test_a_skill_file_naming_NO_checkout_reads_as_none(self):
        """A skill rewritten by hand, or one whose command shape changed. A
        regex that found nothing must answer "cannot tell" rather than
        indexing into an empty list."""
        home = tempfile.mkdtemp()
        where = os.path.join(home, ".claude", "skills", "llm-chat")
        os.makedirs(where)
        with open(os.path.join(where, "SKILL.md"), "w") as f:
            f.write("talk to other agents. no command here.\n")
        real = os.path.expanduser
        os.path.expanduser = lambda p: (p.replace("~", home, 1)
                                        if p.startswith("~") else real(p))
        self.addCleanup(lambda: setattr(os.path, "expanduser", real))
        self.assertIsNone(self.real_skill_checkout())

    def test_NO_skill_file_is_not_an_error(self):
        """Claude Code without the skill installed is an ordinary state."""
        home = tempfile.mkdtemp()
        real = os.path.expanduser
        os.path.expanduser = lambda p: (p.replace("~", home, 1)
                                        if p.startswith("~") else real(p))
        self.addCleanup(lambda: setattr(os.path, "expanduser", real))
        self.assertIsNone(self.real_skill_checkout())

    def dirty(self, yes):
        real = cli.checkout_dirty
        cli.checkout_dirty = lambda root=None: yes
        self.addCleanup(lambda: setattr(cli, "checkout_dirty", real))

    def test_being_SERVED_BY_A_TREE_YOU_ARE_EDITING_is_reported(self):
        """showrunner's post to #learnings: if your agent runs ON the tool it
        is editing, the running copy and the edited copy have to be different
        copies, and the distance is a thing to measure rather than assume.

        The line above this one calls the directly-wired state reassuring —
        "already running the current scripts, nothing to do" — and CURRENT is
        not COMMITTED. For whoever maintains this checkout the scripts serving
        the session are uncommitted work, a half-saved hook takes effect on
        the next tool call, and the wake hook is the one that would have
        delivered the message saying it broke. A sweep mutating this tree once
        reached a neighbouring agent and retired its waker."""
        here = os.path.dirname(os.path.dirname(os.path.abspath(cli.__file__)))
        self.wired(here, fingerprint="old-stamp")
        self.dirty(True)
        self.assertIn("UNCOMMITTED CHANGES", self.report())

    def test_a_CLEAN_tree_is_not_warned_about(self):
        """Paired. The hazard is uncommitted work, not direct wiring — and a
        line that fires for every directly-wired repo is one nobody reads."""
        here = os.path.dirname(os.path.dirname(os.path.abspath(cli.__file__)))
        self.wired(here, fingerprint="old-stamp")
        self.dirty(False)
        self.assertNotIn("UNCOMMITTED CHANGES", self.report())

    def test_a_CLEAN_direct_consumer_is_STILL_not_told_to_reinstall(self):
        """The pairing that was missing, and its absence shipped a real bug.

        The dirty-tree warning was first written BETWEEN the `if` above and
        its `elif`, which silently re-parented that `elif` onto the new
        condition. On a dirty tree the STALE branch stopped firing; on a clean
        one it fired alongside "already running the current scripts, nothing
        to do" — the contradictory advice
        `test_a_DIRECT_consumer_is_not_told_to_reinstall` exists to prevent.

        That test could not catch it, because the tree it runs in is the
        maintainer's, and the maintainer's tree is permanently dirty. Every
        assertion about the CLEAN case has to say `dirty(False)` explicitly or
        it is asserting about this laptop rather than about the code. CI on a
        clean checkout failed on its first run."""
        here = os.path.dirname(os.path.dirname(os.path.abspath(cli.__file__)))
        self.wired(here, fingerprint="old-stamp")
        self.dirty(False)
        text = self.report()
        self.assertIn("already running the current scripts", text)
        self.assertNotIn("STALE:", text)

    def test_a_VENDORED_consumer_is_not_warned_about_OUR_working_tree(self):
        """The warning is about the tree the HOOKS run from. A consumer wired
        to its own vendored copy is not being served by this one, so our
        uncommitted state is none of its business — and telling it otherwise
        would be the cry-wolf failure this file keeps removing."""
        with tempfile.TemporaryDirectory() as tree:
            os.makedirs(os.path.join(tree, "bin"))
            self.wired(tree, fingerprint="old-stamp")
            self.dirty(True)
            self.assertNotIn("UNCOMMITTED CHANGES", self.report())

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
                       SessionStart=["/x/bin/llm-chat-wake",
                                     "/x/bin/llm-chat-deliver"])
        self.mark("post-tool-use")
        self.mark("stop")
        text = self.report()
        self.assertIn("Wiring looks right", text)

    def test_a_hook_running_from_ANOTHER_CHECKOUT_is_reported(self):
        """gameloop's repo vendors llm_chat under `.lamp/`, so `llm_chat
        doctor` there ran a months-old copy while the hooks — absolute paths
        into the source tree — ran current code. Their doctor could not
        mention a hook gap it had never heard of, and `who` did not exist.
        They found it through an argparse error, not through the tool whose
        job it is."""
        write_settings(self.project,
                       PostToolUse=["/other/tree/bin/llm-chat-deliver"],
                       Stop=["/other/tree/bin/llm-chat-wake"],
                       SessionStart=["/other/tree/bin/llm-chat-wake",
                                     "/other/tree/bin/llm-chat-deliver"])
        self.mark("post-tool-use")
        self.mark("stop")
        text = self.report()
        self.assertIn("DIFFERENT BUILD", text)
        self.assertIn("/other/tree", text)

    def test_hooks_from_THIS_checkout_say_nothing(self):
        """Paired. The ordinary case is one tree, and a warning that fires
        for everybody is one nobody reads."""
        write_settings(self.project,
                       PostToolUse=[os.path.join(cli.ROOT, "bin",
                                                 "llm-chat-deliver")],
                       Stop=[os.path.join(cli.ROOT, "bin", "llm-chat-wake")],
                       SessionStart=[os.path.join(cli.ROOT, "bin",
                                                  "llm-chat-wake")])
        self.mark("post-tool-use")
        self.mark("stop")
        self.assertNotIn("DIFFERENT BUILD", self.report())

    def test_the_divergence_is_reported_even_when_the_STAMP_matches(self):
        """The stamp compares the wiring to the tree it was wired FROM, and
        can be perfectly current while this is wrong. It is a fact about the
        hooks; this is a fact about the program printing the report."""
        write_settings(self.project,
                       PostToolUse=["/other/tree/bin/llm-chat-deliver"],
                       Stop=["/other/tree/bin/llm-chat-wake"],
                       SessionStart=["/other/tree/bin/llm-chat-wake",
                                     "/other/tree/bin/llm-chat-deliver"])
        self.mark("post-tool-use")
        self.mark("stop")
        d = os.path.join(self.project, ".llm_chat")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "installed.json"), "w") as f:
            json.dump({"fingerprint": cli.wiring_fingerprint(cli.ROOT),
                       "checkout": cli.ROOT}, f)
        text = self.report()
        self.assertIn("DIFFERENT BUILD", text)
        self.assertNotIn("STALE:", text)

    def test_the_divergence_names_the_command_to_run_instead(self):
        """A warning whose remedy is 'find the other copy yourself' is one
        more thing to work out while already confused."""
        write_settings(self.project,
                       PostToolUse=["/other/tree/bin/llm-chat-deliver"],
                       Stop=["/other/tree/bin/llm-chat-wake"],
                       SessionStart=["/other/tree/bin/llm-chat-wake",
                                     "/other/tree/bin/llm-chat-deliver"])
        self.mark("post-tool-use")
        self.mark("stop")
        self.assertIn("/other/tree/bin/llm_chat doctor", self.report())

    def test_a_hook_tree_that_is_GONE_is_said_so(self):
        """Different remedy entirely: nothing is delivering at all, and
        pointing somebody at a copy that does not exist wastes the one line
        they were going to read."""
        write_settings(self.project,
                       PostToolUse=["/no/such/tree/bin/llm-chat-deliver"],
                       Stop=["/no/such/tree/bin/llm-chat-wake"],
                       SessionStart=["/no/such/tree/bin/llm-chat-wake",
                                     "/no/such/tree/bin/llm-chat-deliver"])
        self.mark("post-tool-use")
        self.mark("stop")
        self.assertIn("GONE", self.report())

    def test_an_unparseable_hook_command_names_no_tree(self):
        """A wrapper script, a shell one-liner. A tree named wrongly and
        confidently is worse here than no tree named at all."""
        self.assertIsNone(cli.hook_checkout("run-my-wrapper --deliver",
                                            "llm-chat-deliver"))

    def test_a_QUOTED_hook_command_is_still_read(self):
        """install.sh writes the interpreter and the script as separate argv
        entries, and settings files in the wild carry both quoted forms."""
        self.assertEqual(
            cli.hook_checkout("'/usr/bin/python3' '/a/b/bin/llm-chat-deliver'",
                              "llm-chat-deliver"),
            "/a/b")

    def test_deliver_missing_from_SessionStart_is_reported(self):
        """The waker IS on SessionStart and still cannot say anything there —
        it is asyncRewake with a week-long timeout, so it blocks in the
        background rather than answering. Everything about a wake that stopped
        landing (#20) reaches a starting session through this hook or through
        nobody, and the restart that causes it IS a session start."""
        write_settings(self.project,
                       PostToolUse=["/x/bin/llm-chat-deliver"],
                       Stop=["/x/bin/llm-chat-wake"],
                       SessionStart=["/x/bin/llm-chat-wake"])
        self.mark("post-tool-use")
        self.mark("stop")
        text = self.report()
        self.assertIn("deliver is not on SessionStart", text)
        self.assertNotIn("Wiring looks right", text)

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
        self.assertIn("a wake last LANDED", self.report())

    def test_a_landing_does_not_CONCLUDE_that_replies_arrive(self):
        """It used to end "so replies arrive on their own" — a present-tense
        conclusion drawn from the last success. gameloop read it, believed
        it, and passed "llm_chat is healthy" to a human while a newer wake
        had already failed. The age is the fact; the conclusion belongs to
        the reader, and only after the contradicting checks have spoken."""
        self.joined_with_waker(os.getpid())
        self.landed(event="Stop")
        self.assertNotIn("replies arrive on their own", self.report())

    # ── a wake that failed AFTER the last landing, with nothing queued ──────

    def missed_at(self, at):
        with open(os.path.join(self.project, ".llm_chat", "wake.missed"),
                  "w") as f:
            json.dump({"at": at, "requested_at": at - 110}, f)

    def test_a_miss_NEWER_than_the_landing_contradicts_it(self):
        """gameloop had both lines on screen an hour after installing the
        delivery hook: doctor said a wake landed 1718m ago, the hook said one
        was requested 6h ago and never came. Both true; only one was the live
        state, and the reassuring one had already been passed to a human."""
        self.joined_with_waker(os.getpid())
        now = cli.now_ms() // 1000
        self.landed(at=now - 100_000, event="Stop")
        self.missed_at(now - 20_000)
        text = self.report()
        self.assertIn("NEVER LANDED", text)

    def test_the_contradiction_does_NOT_need_a_message_still_waiting(self):
        """The whole gap. `waiting_longer_than_the_last_wake` can only speak
        while something is unread, and the delivery hook collects anything a
        missed wake stranded on the very next tool call — so the ordinary
        outcome of this failure is an empty queue and an intact failure."""
        self.joined_with_waker(os.getpid())
        now = cli.now_ms() // 1000
        self.landed(at=now - 100_000, event="Stop")
        self.missed_at(now - 20_000)
        text = self.report()
        self.assertIn("NEVER LANDED", text)
        # Nothing is queued here, so the OLDER witness is silent — which is
        # what makes this a test of the new one rather than of both at once.
        self.assertNotIn("has been waiting", text)

    def test_a_miss_OLDER_than_the_landing_is_spent(self):
        """Paired, and it is why this compares against the landing rather
        than the clock. A wake that failed and was then followed by one that
        worked is history."""
        self.joined_with_waker(os.getpid())
        now = cli.now_ms() // 1000
        self.missed_at(now - 100_000)
        self.landed(at=now - 20_000, event="Stop")
        self.assertNotIn("NEVER LANDED", self.report())

    def test_no_miss_recorded_says_nothing(self):
        self.joined_with_waker(os.getpid())
        self.landed(event="Stop")
        self.assertNotIn("NEVER LANDED", self.report())

    def test_a_corrupt_miss_record_does_not_take_doctor_down(self):
        self.joined_with_waker(os.getpid())
        self.landed(event="Stop")
        with open(os.path.join(self.project, ".llm_chat", "wake.missed"),
                  "w") as f:
            f.write("{not json")
        self.assertIn("a wake last LANDED", self.report())

    def test_the_contradiction_says_idle_means_NOTHING_ARRIVES(self):
        """The actionable half. "A wake failed" is a fact about a mechanism;
        "nothing will reach you while you sit still" is the thing that
        changes what the reader does next."""
        self.joined_with_waker(os.getpid())
        now = cli.now_ms() // 1000
        self.landed(at=now - 100_000, event="Stop")
        self.missed_at(now - 20_000)
        self.assertIn("while you are idle", self.report())

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

