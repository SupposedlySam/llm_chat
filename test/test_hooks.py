"""The two hooks: what reaches an agent, and what proves the hook ran at all.

Both compute their project at import time, so each test loads a fresh copy with
the environment already arranged. Both also shell out to the CLI, which is
stubbed here — the CLI's own behaviour is covered directly elsewhere, and what
matters at this layer is what the hook does with the answer.
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import load, write_settings  # noqa: E402


class FakeSubprocess:
    """Stands in for the whole `subprocess` module.

    Never assign to `mod.subprocess.run` — `mod.subprocess` IS the real,
    shared module, so that swaps subprocess.run process-wide for every test
    that follows and nothing restores it. It happened: the shell tests then
    "ran" install.sh, got a canned exit 0, and asserted against files nothing
    had written. Replacing the ATTRIBUTE on the module under test leaves the
    real one alone.
    """

    def __init__(self, *outputs):
        self.run = Stub(*outputs)


class Stub:
    """Stands in for subprocess.run, returning canned CLI output."""

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        out = self.outputs.pop(0) if self.outputs else ""

        class Result:
            stdout = out
            stderr = ""
            returncode = 0
        return Result()


class HookTestCase(unittest.TestCase):
    script = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name
        os.environ["CLAUDE_PROJECT_DIR"] = self.project
        # Fully wired by default: otherwise every delivery assertion also picks
        # up the "older wiring" notice, and a test that asserts two things at
        # once fails without telling you which.
        write_settings(self.project,
                       PostToolUse=["/x/bin/llm-chat-deliver"],
                       Stop=["/x/bin/llm-chat-wake"],
                       SessionStart=["/x/bin/llm-chat-wake"])
        self.mod = load(self.script)

    def tearDown(self):
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.tmp.cleanup()

    def joined(self, **rooms):
        d = os.path.join(self.project, ".llm_chat")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "joined.json"), "w") as f:
            json.dump({name: {"identity": who, "server": "http://x"}
                       for name, who in rooms.items()}, f)

    def probes(self):
        d = os.path.join(self.project, ".llm_chat", "probe")
        return sorted(os.listdir(d)) if os.path.isdir(d) else []


class DeliverTest(HookTestCase):
    script = "llm-chat-deliver"

    def run_hook(self, payload="{}"):
        out, err = io.StringIO(), io.StringIO()
        stdin = sys.stdin
        sys.stdin = io.StringIO(payload)
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = self.mod.main()
        finally:
            sys.stdin = stdin
        return code, out.getvalue()

    def test_in_no_rooms_it_says_nothing(self):
        """Silence is the default: this runs after EVERY tool call, so any
        output that is not a message is noise on a loop."""
        code, out = self.run_hook()
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_it_marks_that_it_fired_even_with_nothing_to_deliver(self):
        """The mark's ABSENCE is the only readable evidence that a registered
        hook has never run, so it has to be written before any early return."""
        self.run_hook()
        self.assertIn("post-tool-use", self.probes())

    def test_waiting_messages_are_returned_as_additional_context(self):
        self.joined(room="me")
        self.mod.subprocess = FakeSubprocess("[other] hello there")
        _, out = self.run_hook()
        payload = json.loads(out)
        context = payload["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"],
                         "PostToolUse")
        self.assertIn("hello there", context)
        self.assertIn("#room", context)
        self.assertIn("ANOTHER AGENT", context,
                      "the header has to say these are not the agent's own words")

    def test_nothing_new_is_not_a_delivery(self):
        self.joined(room="me")
        self.mod.subprocess = FakeSubprocess("nothing new in room")
        code, out = self.run_hook()
        self.assertEqual(out, "")

    def test_a_chat_outage_never_breaks_the_session(self):
        def explode(*a, **kw):
            raise OSError("server gone")
        self.joined(room="me")
        self.mod.subprocess = type('M', (), {'run': staticmethod(explode)})
        code, out = self.run_hook()
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_one_delivery_is_capped_so_it_cannot_derail_a_turn(self):
        self.joined(room="me")
        many = "\n".join("[other] line %d" % i for i in range(50))
        self.mod.subprocess = FakeSubprocess(many)
        _, out = self.run_hook()
        context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        delivered = [l for l in context.splitlines() if "line " in l]
        self.assertEqual(len(delivered), self.mod.MAX_PER_DELIVERY)

    def test_a_missing_hook_is_reported_once_per_session(self):
        """The old hook reporting the new one that is missing — the only channel
        available when the missing hook is the silent one."""
        write_settings(self.project, PostToolUse=["/x/bin/llm-chat-deliver"])
        _, first = self.run_hook('{"session_id": "s1"}')
        self.assertIn("llm-chat-wake", first)
        self.assertIn("OLDER wiring", first)
        _, second = self.run_hook('{"session_id": "s1"}')
        self.assertEqual(second, "", "a standing gap must not become standing noise")

    def test_a_new_session_hears_it_again(self):
        write_settings(self.project, PostToolUse=["/x/bin/llm-chat-deliver"])
        self.run_hook('{"session_id": "s1"}')
        _, other = self.run_hook('{"session_id": "s2"}')
        self.assertIn("llm-chat-wake", other)

    def test_fully_wired_repos_get_no_notice(self):
        write_settings(self.project,
                       PostToolUse=["/x/bin/llm-chat-deliver"],
                       Stop=["/x/bin/llm-chat-wake"])
        _, out = self.run_hook('{"session_id": "s1"}')
        self.assertEqual(out, "")

    def test_drifted_hook_scripts_are_reported_though_registration_matches(self):
        """The case hook-comparison is blind to: same command line, different
        code behind it."""
        write_settings(self.project,
                       PostToolUse=["/x/bin/llm-chat-deliver"],
                       Stop=["/x/bin/llm-chat-wake"])
        d = os.path.join(self.project, ".llm_chat")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "installed.json"), "w") as f:
            json.dump({"fingerprint": "0000000000000000"}, f)
        self.mod.subprocess = FakeSubprocess("ffffffffffffffff")
        _, out = self.run_hook('{"session_id": "s1"}')
        self.assertIn("hook scripts changed", out)
        self.assertIn("0000000000000000", out)

    def test_a_matching_stamp_is_not_reported_as_drift(self):
        write_settings(self.project,
                       PostToolUse=["/x/bin/llm-chat-deliver"],
                       Stop=["/x/bin/llm-chat-wake"])
        d = os.path.join(self.project, ".llm_chat")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "installed.json"), "w") as f:
            json.dump({"fingerprint": "abcdef0123456789"}, f)
        self.mod.subprocess = FakeSubprocess("abcdef0123456789")
        _, out = self.run_hook('{"session_id": "s1"}')
        self.assertEqual(out, "")


class WakeTest(HookTestCase):
    script = "llm-chat-wake"

    def test_it_records_which_event_invoked_it(self):
        """Registered on both Stop and SessionStart, it wrote the same mark
        either way — so 'did SessionStart fire?' was unanswerable from its own
        instrumentation, which is the question that mattered after a reload."""
        stdin = sys.stdin
        sys.stdin = io.StringIO('{"hook_event_name": "SessionStart"}')
        try:
            self.mod.main()
        finally:
            sys.stdin = stdin
        self.assertIn("wake-SessionStart", self.probes())
        self.assertIn("stop", self.probes())

    def test_the_mark_carries_pid_and_time_so_a_lifecycle_is_readable(self):
        stdin = sys.stdin
        sys.stdin = io.StringIO('{"hook_event_name": "Stop"}')
        try:
            self.mod.main()
        finally:
            sys.stdin = stdin
        path = os.path.join(self.project, ".llm_chat", "probe", "wake-Stop")
        with open(path) as f:
            stamp = f.read().split()
        self.assertEqual(len(stamp), 2)
        self.assertEqual(int(stamp[1]), os.getpid())

    def test_in_no_rooms_it_exits_before_touching_the_network(self):
        stdin = sys.stdin
        sys.stdin = io.StringIO("{}")
        try:
            self.assertEqual(self.mod.main(), 0)
        finally:
            sys.stdin = stdin

    def test_the_newest_waker_wins_the_pidfile(self):
        self.assertTrue(self.mod.claim_pidfile())
        with open(self.mod.PID_PATH) as f:
            self.assertEqual(int(f.read()), os.getpid())

    def test_a_waker_that_lost_the_pidfile_stands_down(self):
        """Several arming at the same instant all read the file before any
        writes, so each believes it won — one message, N wake-ups."""
        os.makedirs(os.path.dirname(self.mod.PID_PATH), exist_ok=True)
        with open(self.mod.PID_PATH, "w") as f:
            f.write("999999")
        self.assertTrue(self.mod.superseded())

    def test_holding_the_pidfile_is_not_superseded(self):
        self.mod.claim_pidfile()
        self.assertFalse(self.mod.superseded())

    def test_an_unreadable_pidfile_is_not_treated_as_supersession(self):
        os.makedirs(os.path.dirname(self.mod.PID_PATH), exist_ok=True)
        with open(self.mod.PID_PATH, "w") as f:
            f.write("not a pid")
        self.assertFalse(self.mod.superseded())

    def test_orphan_detection_uses_the_parent_that_armed_us(self):
        """Replaces an arbitrary listen budget with the condition it was
        approximating: has the session gone away."""
        self.assertFalse(self.mod.orphaned())
        self.mod.PARENT = os.getppid() + 12345
        self.assertTrue(self.mod.orphaned())

    def test_it_declines_to_guess_when_already_reparented(self):
        self.mod.PARENT = 1
        self.assertFalse(self.mod.orphaned(),
                         "cannot tell orphaned from normal, so do not claim to")

    def test_a_closed_room_is_not_worth_listening_to(self):
        self.mod.subprocess = FakeSubprocess("room  [closed]  —  no topic")
        self.assertFalse(self.mod.still_worth_listening(
            {"room": {"identity": "me", "server": "http://x"}}))

    def test_an_open_room_is(self):
        self.mod.subprocess = FakeSubprocess("room  —  a topic")
        self.assertTrue(self.mod.still_worth_listening(
            {"room": {"identity": "me", "server": "http://x"}}))

    def test_an_unreachable_server_keeps_us_listening_rather_than_deaf(self):
        def explode(*a, **kw):
            raise OSError("down")
        self.mod.subprocess = type('M', (), {'run': staticmethod(explode)})
        self.assertTrue(self.mod.still_worth_listening(
            {"room": {"identity": "me", "server": "http://x"}}))

    def test_polling_returns_none_when_nothing_is_waiting(self):
        self.mod.subprocess = FakeSubprocess("nothing new in room")
        self.assertIsNone(self.mod.poll("room", {"identity": "me",
                                                 "server": "http://x"}))

    def test_polling_returns_the_waiting_text(self):
        self.mod.subprocess = FakeSubprocess("[other] wake up")
        self.assertEqual(self.mod.poll("room", {"identity": "me",
                                                "server": "http://x"}),
                         "[other] wake up")

    def test_an_incomplete_room_record_is_skipped(self):
        self.assertIsNone(self.mod.poll("room", {"identity": None,
                                                 "server": "http://x"}))

    def test_waking_exits_two_with_the_message_on_stderr(self):
        """exit 2 + stderr is what asyncRewake converts into a wake-up; the
        stderr text IS the message the model receives."""
        err = io.StringIO()
        with redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                self.mod.wake(["#room (you are 'me')\n  [other] hello"])
        self.assertEqual(caught.exception.code, 2)
        text = err.getvalue()
        self.assertIn("while you were idle", text)
        self.assertIn("hello", text)
        self.assertIn("ANOTHER AGENT", text)


if __name__ == "__main__":
    unittest.main()
