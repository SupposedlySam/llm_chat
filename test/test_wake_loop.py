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
            json.dump({"room": {"identity": "me", "server": "http://127.0.0.1:1"}}, f)
        # NOTHING here may reach a real subprocess. `http://x` is not a refused
        # connection — it is a DNS lookup that HANGS, so a test that slips
        # through does not fail, it stops, and the child outlives the run.
        # Found with a 9-second suite taking over ten minutes and an
        # eleven-hour-old test/run.py still resolving a hostname. Tests that
        # want the real function override these.
        self.mod.still_worth_listening = lambda rooms: True
        self.mod.sync_broadcasts = lambda: None
        # The waker no longer sleeps, it BLOCKS on a socket for 300s. NoSleep
        # patches `time` and cannot help with select(), so an unstubbed test
        # does not fail — it stops for five minutes. Same shape as the DNS
        # hang: the WAIT is what has to be faked, not the clock.
        self.mod.open_doorbell = lambda identity: None
        self.mod.wait_for_ring = lambda bell, seconds: False
        # The waker no longer sleeps, it BLOCKS on a socket for 300s. NoSleep
        # patches `time` and cannot help with select(), so an unstubbed test
        # does not fail — it stops for five minutes. Same shape as the DNS
        # hang: the wait is the thing that has to be faked, not the clock.
        self.mod.open_doorbell = lambda identity: None
        self.mod.wait_for_ring = lambda bell, seconds: False
        # NOTHING here may reach a real subprocess. `http://x` is not a
        # refused connection — it is a DNS lookup that hangs, so a test that
        # slips through does not fail, it STOPS, and the child outlives the
        # run. Found with a 9-second suite taking over ten minutes and an
        # eleven-hour-old `test/run.py` still resolving a hostname. Tests that
        # want the real function override these.
        self.mod.still_worth_listening = lambda rooms: True
        self.mod.sync_broadcasts = lambda: None

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
        self.mod.addressed = lambda channel, entry: {"wakes_me": True,
                                                     "messages": []}
        self.mod.poll = lambda channel, entry: "[other] wake up"
        self.mod.time = NoSleep()
        err = io.StringIO()
        with redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                self.run_main()
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("wake up", err.getvalue())

    def test_it_keeps_listening_while_nothing_arrives(self):
        """No listen budget: it does not give up.

        Counted WAITS, not sleeps. This test measured `time.sleep` calls, and
        the loop no longer sleeps — it blocks on a doorbell — so the old
        version spun forever instead of failing. The thing being counted has to
        be the thing the loop actually does."""
        waits = []

        def wait(bell, seconds):
            waits.append(seconds)
            if len(waits) >= 3:
                raise KeyboardInterrupt("enough")
            return False

        self.mod.poll = lambda channel, entry: None
        self.mod.still_worth_listening = lambda rooms: True
        self.mod.wait_for_ring = wait
        self.mod.time = NoSleep()
        with self.assertRaises(KeyboardInterrupt):
            self.run_main()
        self.assertEqual(len(waits), 3)
        self.assertTrue(all(s >= 60 for s in waits),
                        "the heartbeat must be long — it is not a poll")

    def test_a_broadcast_room_never_wakes_the_session(self):
        """Paired with the test below, which proves the skip is targeted rather
        than the waker being broken. Measured live too: a broadcast room with an
        unread message left the waker polling, a normal one exited 2.

        The room is now PEEKED rather than skipped outright, because a message
        addressed to you specifically has to be able to reach you even here.
        What must not happen is the CONSUMING read, which is what would both
        wake the session and take the message off the cursor."""
        with open(os.path.join(self.project, ".llm_chat", "joined.json"), "w") as f:
            json.dump({"notices": {"identity": "me", "server": "http://127.0.0.1:1",
                                   "broadcast": True}}, f)
        polled = []
        self.mod.addressed = lambda channel, entry: None   # nothing addresses me
        self.mod.poll = lambda channel, entry: polled.append(channel)
        self.mod.still_worth_listening = lambda rooms: False
        self.mod.time = NoSleep()
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(polled, [], "an unaddressed message must not be read")

    def test_an_ordinary_room_alongside_it_still_wakes(self):
        with open(os.path.join(self.project, ".llm_chat", "joined.json"), "w") as f:
            json.dump({"notices": {"identity": "me", "server": "http://127.0.0.1:1",
                                   "broadcast": True},
                       "room": {"identity": "me", "server": "http://127.0.0.1:1"}}, f)
        self.mod.addressed = lambda channel, entry: (
            {"wakes_me": True, "messages": []} if channel == "room" else None)
        self.mod.poll = lambda channel, entry: "[other] wake up"
        self.mod.time = NoSleep()
        err = io.StringIO()
        with redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                self.run_main()
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("#room", err.getvalue())
        self.assertNotIn("#notices", err.getvalue())

    def exit_record(self):
        import json as _json
        with open(os.path.join(self.project, ".llm_chat", "wake.exit")) as f:
            return _json.load(f)

    def test_it_records_WHY_it_stopped(self):
        """`doctor` could say "pid is gone" and never why, across five silent
        exits plus SIGTERM. The reasons have completely different remedies and
        one of them is not a problem at all, so the bare absence made a healthy
        handover and a dead session look identical."""
        self.mod.poll = lambda channel, entry: None
        self.mod.still_worth_listening = lambda rooms: False
        self.mod.time = NoSleep()
        self.assertEqual(self.run_main(), 0)
        self.assertIn("closed", self.exit_record()["reason"])

    def test_a_superseded_waker_says_it_was_superseded(self):
        """The healthy case, and the one most likely to be misread as a crash."""
        self.mod.addressed = lambda channel, entry: None
        self.mod.superseded = lambda: True
        self.mod.time = NoSleep()
        self.run_main()
        self.assertIn("superseded", self.exit_record()["reason"])

    def test_an_orphaned_waker_says_so(self):
        self.mod.addressed = lambda channel, entry: None
        self.mod.orphaned = lambda: True
        self.mod.time = NoSleep()
        self.run_main()
        self.assertIn("orphaned", self.exit_record()["reason"])

    def test_a_waker_with_no_rooms_says_so(self):
        with open(os.path.join(self.project, ".llm_chat", "joined.json"), "w") as f:
            json.dump({}, f)
        self.assertEqual(self.run_main(), 0)
        self.assertIn("no rooms", self.exit_record()["reason"])

    def test_failing_to_claim_the_pidfile_is_recorded(self):
        self.mod.claim_pidfile = lambda: False
        self.run_main()
        self.assertIn("pidfile", self.exit_record()["reason"])

    def test_a_running_waker_records_running_and_not_a_reason(self):
        """The discriminating value. If a waker is gone but the record still
        says 'running', it never chose to stop — something outside killed it,
        which is a different diagnosis from every other value here."""
        self.mod.addressed = lambda channel, entry: {"wakes_me": True,
                                                     "messages": []}
        self.mod.poll = lambda channel, entry: "[other] hi"
        self.mod.time = NoSleep()
        recorded = []
        real = self.mod.record_exit
        self.mod.record_exit = lambda reason: recorded.append(reason)
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.run_main()
        self.mod.record_exit = real
        self.assertEqual(recorded[0], "running")
        self.assertIn("woke", recorded[-1])

    def test_sigterm_is_recorded_rather_than_looking_like_a_crash(self):
        handled = []
        self.mod.record_exit = lambda reason: handled.append(reason)
        with self.assertRaises(SystemExit):
            self.mod.on_term(15, None)
        self.assertIn("terminated", handled[0])

    def test_an_unwritable_state_dir_does_not_break_the_exit(self):
        """Bookkeeping must never break the thing it describes."""
        self.mod.EXIT_PATH = "/proc/nope/wake.exit"
        self.mod.record_exit("whatever")     # must not raise

    def test_it_stands_down_once_every_room_has_closed(self):
        """Nothing can arrive any more, so polling forever would be waste."""
        self.mod.poll = lambda channel, entry: None
        self.mod.still_worth_listening = lambda rooms: False
        self.mod.time = NoSleep()
        self.assertEqual(self.run_main(), 0)

    def test_a_superseded_waker_stops_before_polling(self):
        """Before, never after: polling advances the cursor, so a waker that
        claimed a message and then stood down would lose it.

        `addressed` is stubbed to say YES on purpose. Without that, poll is
        unreachable anyway — nothing is addressing us — and this test would
        pass with the supersession check deleted, which is the vacuous-green
        shape it exists to prevent."""
        polled = []
        self.mod.addressed = lambda channel, entry: {"wakes_me": True,
                                                     "messages": []}
        self.mod.poll = lambda channel, entry: polled.append(channel)
        self.mod.superseded = lambda: True
        self.mod.time = NoSleep()
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(polled, [], "it must not have claimed anything")

    def test_an_orphaned_waker_stops_before_polling(self):
        polled = []
        self.mod.addressed = lambda channel, entry: {"wakes_me": True,
                                                     "messages": []}
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

    def test_a_room_missing_from_the_listing_KEEPS_US_LISTENING(self):
        """THIS TEST ASSERTED THE DEFECT. It read "cannot confirm it is open"
        as grounds to stand down, and standing down is permanent — nothing
        re-arms an idle waker. So the one situation we could not confirm was
        resolved in the direction that goes deaf forever.

        Absence conflated four things: room genuinely closed, CLI failed,
        server down, and not listed for any other reason. Only the first is a
        reason to retire. Reported by the agent it happened to, whose five open
        rooms were reported as "every joined room is closed"."""
        class Fake:
            @staticmethod
            def run(argv, **kw):
                class Result:
                    stdout = json.dumps([{"name": "someotherroom",
                                          "closed": False}])
                    stderr = ""
                    returncode = 0
                return Result
        self.mod.subprocess = Fake
        self.assertTrue(self.mod.still_worth_listening(
            {"room": {"identity": "me", "server": "http://127.0.0.1:1"}}))

    def test_a_room_without_a_server_is_skipped(self):
        """Uses the REAL function, so it has to undo setUp's stub. That stub
        exists to stop any other test reaching the network by accident; a test
        that genuinely wants the real thing says so here rather than leaving
        the door open for all of them."""
        self.mod.still_worth_listening = load("llm-chat-wake").still_worth_listening
        self.assertFalse(self.mod.still_worth_listening({"room": {}}))


class StillWorthListeningTest(unittest.TestCase):
    """When to stand down — and every way of NOT KNOWING keeps listening.

    This retired wakers on a premise that was false. doctor reported "every
    joined room is closed" for an agent whose five rooms were all open; the
    exit record is what made it findable. subprocess.run does not raise on a
    non-zero exit, and the except clause caught only timeout and OSError, so a
    FAILING CLI returned empty stdout, nothing matched, and False meant "all
    closed". Live for about an hour on this machine while the CLI was broken by
    this repo's own mutation sweep.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CLAUDE_PROJECT_DIR"] = self.tmp.name
        os.makedirs(os.path.join(self.tmp.name, ".llm_chat"))
        self.mod = load("llm-chat-wake")
        self.real = self.mod.subprocess
        self.rooms = {"a": {"identity": "me", "server": "http://127.0.0.1:1"}}

    def tearDown(self):
        self.mod.subprocess = self.real
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.tmp.cleanup()

    def answer(self, returncode=0, stdout="[]"):
        class Result:
            pass

        class Fake:
            @staticmethod
            def run(argv, **kwargs):
                r = Result()
                r.returncode, r.stdout, r.stderr = returncode, stdout, ""
                return r
        self.mod.subprocess = Fake()
        return self.mod.still_worth_listening(self.rooms)

    def listing(self, closed):
        return json.dumps([{"name": "a", "closed": closed}])

    def test_an_open_room_keeps_it_listening(self):
        self.assertTrue(self.answer(stdout=self.listing(False)))

    def test_a_genuinely_closed_room_stands_it_down(self):
        """The one case that may return False. Without this the function could
        never retire and the fix would be 'always listen', which is not a fix."""
        self.assertFalse(self.answer(stdout=self.listing(True)))

    def test_A_FAILING_CLI_KEEPS_IT_LISTENING(self):
        """The defect, as a test. Non-zero exit, empty stdout — previously
        indistinguishable from 'every room closed'."""
        self.assertTrue(self.answer(returncode=1, stdout=""))

    def test_a_crashing_cli_that_prints_a_traceback_keeps_it_listening(self):
        self.assertTrue(self.answer(returncode=1, stdout="Traceback..."))

    def test_unparseable_output_keeps_it_listening(self):
        self.assertTrue(self.answer(stdout="not json at all"))

    def test_a_room_MISSING_from_the_listing_keeps_it_listening(self):
        """Absence is only evidence if something would have been present.
        Nothing guarantees that here, and four situations wore one face."""
        self.assertTrue(self.answer(stdout="[]"))

    def test_an_exception_keeps_it_listening(self):
        class Exploding:
            @staticmethod
            def run(argv, **kwargs):
                raise OSError("down")
        self.mod.subprocess = Exploding()
        self.assertTrue(self.mod.still_worth_listening(self.rooms))

    def test_it_asks_for_json_rather_than_the_rendering(self):
        """The rendering OMITS closed rooms rather than marking them, so the
        old '[closed]' branch was unreachable and absence was the only route to
        False. Third instance of rendering-as-format in a week."""
        seen = {}

        class Spy:
            @staticmethod
            def run(argv, **kwargs):
                seen["argv"] = argv

                class Result:
                    returncode, stdout, stderr = 0, "[]", ""
                return Result()
        self.mod.subprocess = Spy()
        self.mod.still_worth_listening(self.rooms)
        self.assertIn("--json", seen["argv"])

    def test_a_room_with_no_server_is_skipped_without_a_call(self):
        called = []

        class Counting:
            @staticmethod
            def run(argv, **kwargs):
                called.append(argv)

                class Result:
                    returncode, stdout, stderr = 0, "[]", ""
                return Result()
        self.mod.subprocess = Counting()
        self.mod.still_worth_listening({"a": {"identity": "me"}})
        self.assertEqual(called, [])


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
