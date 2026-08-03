"""The waker's polling loop, and both hooks' project resolution.

The loop is the part that actually decides whether an idle agent ever hears
anything, and every exit from it is a decision about a message that may be in
flight — so each is asserted separately rather than inferred from the loop
merely terminating.
"""
import io
import json
import os
import signal
import sys
import tempfile
import time as _real_time
import unittest
from contextlib import redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import load  # noqa: E402


class NoSleep:
    """Stands in for the whole `time` module: runs the loop at full speed and
    counts the passes. `time()` is real, because the probe marks stamp it."""

    def __init__(self, stop_after=None):
        self.slept = 0
        self.stop_after = stop_after

    @staticmethod
    def time():
        return _real_time.time()

    def sleep(self, _):
        self.slept += 1
        if self.stop_after is not None and self.slept >= self.stop_after:
            raise KeyboardInterrupt("enough")


class WakeLoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name
        os.environ["CLAUDE_PROJECT_DIR"] = self.project
        self.mod = load("llm-chat-wake")
        d = os.path.join(self.project, ".llm_chat")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "joined.json"), "w") as f:
            json.dump({"room": {"identity": "me", "server": "http://x"}}, f)

    def tearDown(self):
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.tmp.cleanup()

    def run_main(self, payload='{"hook_event_name": "Stop"}'):
        stdin = sys.stdin
        sys.stdin = io.StringIO(payload)
        try:
            return self.mod.main()
        finally:
            sys.stdin = stdin

    def test_a_waiting_message_wakes_the_session(self):
        """The whole point: exit 2 with the text on stderr, which asyncRewake
        converts into a wake-up in the same session."""
        self.mod.poll = lambda channel, entry: "[other] wake up"
        self.mod.time = NoSleep()
        err = io.StringIO()
        with redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                self.run_main()
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("wake up", err.getvalue())

    def test_it_keeps_listening_while_nothing_arrives(self):
        self.mod.poll = lambda channel, entry: None
        self.mod.still_worth_listening = lambda rooms: True
        clock = NoSleep(stop_after=3)
        self.mod.time = clock
        with self.assertRaises(KeyboardInterrupt):
            self.run_main()
        self.assertEqual(clock.slept, 3, "no listen budget: it does not give up")

    def test_it_stands_down_once_every_room_has_closed(self):
        """Nothing can arrive any more, so polling forever would be waste."""
        self.mod.poll = lambda channel, entry: None
        self.mod.still_worth_listening = lambda rooms: False
        self.mod.time = NoSleep()
        self.assertEqual(self.run_main(), 0)

    def test_a_superseded_waker_stops_before_polling(self):
        """Before, never after: polling advances the cursor, so a waker that
        claimed a message and then stood down would lose it."""
        polled = []
        self.mod.poll = lambda channel, entry: polled.append(channel)
        self.mod.superseded = lambda: True
        self.mod.time = NoSleep()
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(polled, [], "it must not have claimed anything")

    def test_an_orphaned_waker_stops_before_polling(self):
        polled = []
        self.mod.poll = lambda channel, entry: polled.append(channel)
        self.mod.orphaned = lambda: True
        self.mod.time = NoSleep()
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(polled, [])

    def test_it_exits_when_it_cannot_claim_the_pidfile(self):
        self.mod.claim_pidfile = lambda: False
        self.assertEqual(self.run_main(), 0)

    def test_a_corrupt_joined_record_reads_as_no_rooms(self):
        with open(os.path.join(self.project, ".llm_chat", "joined.json"), "w") as f:
            f.write("{not json")
        self.assertEqual(self.run_main(), 0)

    def test_a_stale_pidfile_holder_is_signalled(self):
        """Newest waker wins, or N pollers means N wake-ups for one message."""
        signalled = []
        real_kill = self.mod.os.kill
        self.mod.os.kill = lambda pid, sig: signalled.append((pid, sig))
        try:
            with open(self.mod.PID_PATH, "w") as f:
                f.write("424242")
            self.mod.claim_pidfile()
        finally:
            self.mod.os.kill = real_kill
        self.assertEqual(signalled, [(424242, signal.SIGTERM)])

    def test_a_dead_previous_holder_is_not_an_error(self):
        real_kill = self.mod.os.kill

        def gone(pid, sig):
            raise ProcessLookupError("already gone")
        self.mod.os.kill = gone
        try:
            with open(self.mod.PID_PATH, "w") as f:
                f.write("424242")
            self.assertTrue(self.mod.claim_pidfile())
        finally:
            self.mod.os.kill = real_kill

    def test_an_unwritable_state_directory_reports_failure_rather_than_raising(self):
        self.mod.PID_PATH = "/dev/null/nope/wake.pid"
        self.assertFalse(self.mod.claim_pidfile())

    def test_a_room_missing_from_the_listing_is_not_assumed_open(self):
        """`channels` not naming the room means we cannot confirm it is open."""
        class Fake:
            @staticmethod
            def run(argv, **kw):
                class Result:
                    stdout = "someotherroom  —  topic"
                    stderr = ""
                    returncode = 0
                return Result
        self.mod.subprocess = Fake
        self.assertFalse(self.mod.still_worth_listening(
            {"room": {"identity": "me", "server": "http://x"}}))

    def test_a_room_without_a_server_is_skipped(self):
        self.assertFalse(self.mod.still_worth_listening({"room": {}}))


class HookProjectResolutionTest(unittest.TestCase):
    """Both hooks resolve their project the same way, and both must walk UP:
    `.llm_chat/` lives at a root, so a subdirectory would be a second identity
    and the hook would read the wrong one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        self.cwd = os.getcwd()
        os.environ.pop("CLAUDE_PROJECT_DIR", None)

    def tearDown(self):
        os.chdir(self.cwd)
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.tmp.cleanup()

    def resolve(self, script):
        return os.path.realpath(load(script)._project_dir())

    def test_a_subdirectory_resolves_to_the_repo_root(self):
        os.makedirs(os.path.join(self.root, ".git"))
        deep = os.path.join(self.root, "a", "b")
        os.makedirs(deep)
        os.chdir(deep)
        for script in ("llm-chat-deliver", "llm-chat-wake"):
            self.assertEqual(self.resolve(script), self.root, script)

    def test_an_llm_chat_directory_also_marks_the_root(self):
        os.makedirs(os.path.join(self.root, ".llm_chat"))
        deep = os.path.join(self.root, "x")
        os.makedirs(deep)
        os.chdir(deep)
        for script in ("llm-chat-deliver", "llm-chat-wake"):
            self.assertEqual(self.resolve(script), self.root, script)

    def test_with_no_marker_it_falls_back_to_cwd(self):
        deep = os.path.join(self.root, "loose")
        os.makedirs(deep)
        os.chdir(deep)
        for script in ("llm-chat-deliver", "llm-chat-wake"):
            self.assertEqual(self.resolve(script), os.path.realpath(deep), script)

    def test_the_harness_variable_still_wins(self):
        os.environ["CLAUDE_PROJECT_DIR"] = self.root
        for script in ("llm-chat-deliver", "llm-chat-wake"):
            self.assertEqual(self.resolve(script), self.root, script)


if __name__ == "__main__":
    unittest.main()
