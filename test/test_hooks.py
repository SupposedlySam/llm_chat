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
            json.dump({name: {"identity": who, "server": "http://127.0.0.1:1"}
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
        self.mod.subprocess = FakeSubprocess(json.dumps(
            [{"seq": 1, "from": "other", "text": "hello there",
              "audience": "me", "mine": False}]))
        _, out = self.run_hook()
        payload = json.loads(out)
        context = payload["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"],
                         "PostToolUse")
        self.assertIn("hello there", context)
        self.assertIn("#room", context)
        self.assertIn("ANOTHER AGENT", context,
                      "the header has to say these are not the agent's own words")

    def test_the_hook_uses_the_DELIVERY_path_not_the_TRANSCRIPT_one(self):
        """The self-filter lives in `read`, and `--all` disables it.

        Reported by an agent who saw their own message delivered with the
        `(you)` marker, which belongs to the transcript format. Not reproduced
        here or by a third agent, and git shows the hook has never passed
        `--all` — but the property is worth pinning regardless, because adding
        that flag later would silently recreate the self-answering loop this
        project's invariants call the expensive one, and nothing else would
        notice.
        """
        self.joined(room="me")
        stub = Stub("[other] hello")
        self.mod.subprocess = FakeSubprocess()
        self.mod.subprocess.run = stub
        self.run_hook()
        argv, = stub.calls
        self.assertIn("read", argv)
        self.assertNotIn("--all", argv,
                         "--all disables the self-filter; the hook must never "
                         "ask for the transcript")
        self.assertNotIn("--peek", argv,
                         "--peek would deliver the same message on every tool "
                         "call, forever")

    def test_the_hook_reads_as_the_identity_that_joined_that_room(self):
        """The filter can only exclude your own words if it is told who you
        are. One project holds a different identity per channel, so passing
        the wrong one filters nothing."""
        self.joined(alpha="me", beta="someone-else")
        stub = Stub("nothing new", "nothing new")
        self.mod.subprocess = FakeSubprocess()
        self.mod.subprocess.run = stub
        self.run_hook()
        pairs = {argv[argv.index("read") + 1]: argv[argv.index("--as") + 1]
                 for argv in stub.calls}
        self.assertEqual(pairs, {"alpha": "me", "beta": "someone-else"})

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
        many = json.dumps([{"seq": i, "from": "other",
                            "text": "line %d" % i, "audience": "me",
                            "mine": False} for i in range(50)])
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


class DriftNoticeTest(HookTestCase):
    """What the notice TELLS you to do, given what the source actually is.

    The detector was right and the remedy was not. Reported by an agent that
    got this twice in minutes: the source HEAD had not moved, the fingerprint
    was being shifted by uncommitted files — one of them the wake hook — and
    'fix it with install.sh' would have wired a live session to a half-finished
    hook. The wake hook is what delivers the message telling you it broke.
    """
    script = "llm-chat-deliver"

    def arrange(self, dirty):
        d = os.path.join(self.project, ".llm_chat")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "installed.json"), "w") as f:
            json.dump({"fingerprint": "old"}, f)

        class Routed:
            """Answers by ARGV, because this path makes two different calls —
            `fingerprint` and `git status` — and a positional stub would hand
            the git answer to the fingerprint call."""

            def __init__(self, dirty):
                self.dirty = dirty

            def __call__(self, argv, **kwargs):
                status = "status" in argv
                text = ((" M bin/llm-chat-wake\n" if self.dirty else "")
                        if status else "new")

                class Result:
                    stdout = text
                    stderr = ""
                    returncode = 0
                return Result()

        # The ATTRIBUTE on the module under test, never mod.subprocess.run —
        # that IS the real shared module, and assigning to it swaps
        # subprocess.run process-wide for every test that follows. This repo
        # has paid for that once; I just re-paid for it writing this test, and
        # the eight shell-test errors had one cause again.
        fake = FakeSubprocess()
        fake.run = Routed(dirty)
        self.mod.subprocess = fake
        return self.mod.upgrade_notice("session-1")

    def test_a_dirty_source_is_named_in_the_notice(self):
        notice = self.arrange(dirty=True)
        self.assertIn("UNCOMMITTED", notice)
        self.assertIn("blessed", notice)

    def test_a_clean_source_says_nothing_extra(self):
        """Paired with the test above: a note that always fires teaches
        nothing, and the usual case is a source somebody committed."""
        notice = self.arrange(dirty=False)
        self.assertIn("OLDER wiring", notice)
        self.assertNotIn("UNCOMMITTED", notice)

    def test_it_compares_the_tree_the_repo_was_WIRED_FROM(self):
        """A vendored consumer runs its hooks out of its own copy. Comparing
        against ROOT reported a permanent STALE for a repo matching its own
        source exactly — and this hook fires automatically, so it says it on
        every session rather than only when somebody runs doctor.

        The stub answers BY ARGV: the vendored tree hashes to what the repo
        recorded, this checkout hashes to something else. If the hook asks
        about the wrong one it gets a mismatch and the notice fires."""
        d = os.path.join(self.project, ".llm_chat")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "installed.json"), "w") as f:
            json.dump({"fingerprint": "vendored-hash",
                       "checkout": "/vendored/tree"}, f)

        class ByTree:
            def __call__(self, argv, **kwargs):
                if "status" in argv:
                    text = ""
                elif "--of" in argv and argv[argv.index("--of") + 1] == "/vendored/tree":
                    text = "vendored-hash"
                else:
                    text = "this-checkout-hash"

                class Result:
                    stdout = text
                    stderr = ""
                    returncode = 0
                return Result()

        fake = FakeSubprocess()
        fake.run = ByTree()
        self.mod.subprocess = fake
        self.assertEqual(self.mod.upgrade_notice("s1"), "",
                         "a repo matching its own source is not stale")

    def test_a_vendored_repo_that_HAS_drifted_still_gets_the_notice(self):
        """Paired with the test above: a check that stopped firing entirely
        would pass it and be worthless."""
        d = os.path.join(self.project, ".llm_chat")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "installed.json"), "w") as f:
            json.dump({"fingerprint": "old-vendored",
                       "checkout": "/vendored/tree"}, f)

        class Moved:
            def __call__(self, argv, **kwargs):
                text = "" if "status" in argv else "new-vendored"

                class Result:
                    stdout = text
                    stderr = ""
                    returncode = 0
                return Result()

        fake = FakeSubprocess()
        fake.run = Moved()
        self.mod.subprocess = fake
        self.assertIn("OLDER wiring", self.mod.upgrade_notice("s2"))

    def test_git_being_unavailable_does_not_break_the_notice(self):
        """The notice is the important part; knowing the source's state is a
        bonus. An exception here would swallow a real drift warning."""
        d = os.path.join(self.project, ".llm_chat")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "installed.json"), "w") as f:
            json.dump({"fingerprint": "old"}, f)

        class Exploding:
            def __call__(self, argv, **kwargs):
                if "status" in argv:
                    raise OSError("no git")

                class Result:
                    stdout, stderr, returncode = "new", "", 0
                return Result()

        fake = FakeSubprocess()
        fake.run = Exploding()
        self.mod.subprocess = fake
        notice = self.mod.upgrade_notice("session-1")
        self.assertIn("hook scripts changed", notice)
        self.assertNotIn("UNCOMMITTED", notice)

    def test_the_drift_itself_is_still_reported_either_way(self):
        for dirty in (True, False):
            self.assertIn("hook scripts changed", self.arrange(dirty))
            os.remove(os.path.join(self.project, ".llm_chat",
                                   "wiring.session-1"))


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
        self.mod.subprocess = FakeSubprocess(
            '[{"name": "room", "closed": true}]')
        self.assertFalse(self.mod.still_worth_listening(
            {"room": {"identity": "me", "server": "http://127.0.0.1:1"}}))

    def test_an_open_room_is(self):
        self.mod.subprocess = FakeSubprocess(
            '[{"name": "room", "closed": false}]')
        self.assertTrue(self.mod.still_worth_listening(
            {"room": {"identity": "me", "server": "http://127.0.0.1:1"}}))

    def test_an_unreachable_server_keeps_us_listening_rather_than_deaf(self):
        def explode(*a, **kw):
            raise OSError("down")
        self.mod.subprocess = type('M', (), {'run': staticmethod(explode)})
        self.assertTrue(self.mod.still_worth_listening(
            {"room": {"identity": "me", "server": "http://127.0.0.1:1"}}))

    def test_polling_returns_none_when_nothing_is_waiting(self):
        self.mod.subprocess = FakeSubprocess("nothing new in room")
        self.assertIsNone(self.mod.poll("room", {"identity": "me",
                                                 "server": "http://127.0.0.1:1"}))

    def test_polling_returns_the_waiting_text(self):
        self.mod.subprocess = FakeSubprocess("[other] wake up")
        self.assertEqual(self.mod.poll("room", {"identity": "me",
                                                "server": "http://127.0.0.1:1"}),
                         "[other] wake up")

    def test_an_incomplete_room_record_is_skipped(self):
        self.assertIsNone(self.mod.poll("room", {"identity": None,
                                                 "server": "http://127.0.0.1:1"}))

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
