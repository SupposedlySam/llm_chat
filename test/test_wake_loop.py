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

    # A CEILING EVEN WHEN NOBODY ASKED FOR ONE. These tests drive a loop whose
    # only exit is the very check a mutation removes — so with the
    # supersession check gone, `stop_after=None` meant the suite ran forever.
    # Two shards of a mutation sweep sat at 46 minutes on exactly that, and a
    # sweep that never finishes measures nothing at all.
    #
    # Unbounded now means "many, then stop", so a loop that fails to exit
    # FAILS the assertion that was going to read its exit reason instead of
    # hanging the run.
    FUSE = 500

    def __init__(self, stop_after=None):
        self.slept = 0
        self.stop_after = stop_after if stop_after is not None else self.FUSE

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
        # The waker no longer sleeps, it BLOCKS on sockets for 300s. NoSleep
        # patches `time` and cannot help with select(), so an unstubbed test
        # does not fail — it stops for five minutes. Same shape as the DNS
        # hang: the WAIT is what has to be faked, not the clock.
        # The loop asks the CLI to run deferred work on every pass, which
        # shells out with a 600s timeout. Harmless once; a test that drives
        # the loop hundreds of times pays for a subprocess each pass, and this
        # file went from 1s to 39s the moment one did. Its own behaviour is
        # asserted in test_wake_landing.
        self.real_run_maintenance = self.mod.run_maintenance
        self.mod.run_maintenance = lambda: None
        self.mod.open_doorbells = lambda rooms: {}
        # BOUNDED, not just instant. Returning False immediately keeps the
        # suite fast, but it also means the loop spins with no sleep in it at
        # all — so a mutation that removes the loop's exit condition runs
        # forever and the NoSleep fuse, which counts sleeps, never fires. A
        # mutation sweep sat on exactly this for 46 minutes.
        #
        # The wait is where the loop pauses, so the wait is where the ceiling
        # belongs. A loop that fails to exit now fails the assertion reading
        # its exit reason.
        self.rings = []

        def wait_once(bells, seconds):
            self.rings.append(seconds)
            if len(self.rings) >= NoSleep.FUSE:
                raise KeyboardInterrupt("the loop never exited")
            return False

        self.mod.wait_for_ring = wait_once
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

    def run_main(self, payload='{"hook_event_name": "Stop"}', catch_fuse=True):
        stdin = sys.stdin
        sys.stdin = io.StringIO(payload)
        try:
            return self.mod.main()
        except KeyboardInterrupt:
            if not catch_fuse:
                raise
            # The fuse in NoSleep, not a real interrupt. Swallowed so the
            # assertion that was going to read the exit reason gets to run and
            # DISAGREE — a loop that never terminated should fail a test, not
            # hang the suite that is measuring it.
            return None
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
        # This one raises its OWN interrupt from `wait` after three passes and
        # asserts it arrived, so it opts out of run_main's fuse-swallowing —
        # otherwise the two mechanisms are indistinguishable and the assertion
        # below would pass whether the loop waited three times or never
        # waited at all.
        with self.assertRaises(KeyboardInterrupt):
            self.run_main(catch_fuse=False)
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
        """The NEWEST exit record. The file holds a history now — see #11: one
        slot meant a new waker starting destroyed the evidence about why the
        old one stopped, which is the only question the file exists for."""
        return self.exit_records()[-1]

    def exit_records(self):
        import json as _json
        with open(os.path.join(self.project, ".llm_chat", "wake.exit")) as f:
            return _json.load(f)

    def alive_record(self):
        import json as _json
        with open(os.path.join(self.project, ".llm_chat", "wake.alive")) as f:
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

    def test_THE_LIVENESS_MARK_IS_REFRESHED_EVERY_PASS(self):
        """It used to be written ONCE, before the loop — a birth certificate
        rather than a heartbeat. So a waker that armed and then died (killed,
        crashed, wedged after the machine slept) left a file identical to a
        healthy one, and doctor's `polling now: yes` rested on a live pid.

        gameloop asked for exactly this from the other side: a dead waker and
        a quiet room are the same observation from in there, and a timestamp
        the waker touches is worth more than the feature.

        Asserting the WRITER, not the reader. doctor's three branches are
        covered in test_cli, and every one of them passed while nothing
        refreshed the stamp — which is how this mutation survived a sweep."""
        beats = []
        real = self.mod.record_alive
        self.mod.record_alive = lambda server=None: beats.append(server)
        self.mod.addressed = lambda channel, entry: None
        self.mod.time = NoSleep(stop_after=3)
        try:
            self.run_main()
        finally:
            self.mod.record_alive = real
        self.assertGreater(len(beats), 1,
                           "the mark was written once and never refreshed — "
                           "a birth certificate, not a heartbeat")

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

    def test_a_LIVE_waker_is_recorded_somewhere_that_destroys_nothing(self):
        """This test used to assert that `running` was written into wake.exit,
        and that assertion was the bug — issue #11.

        The value it was protecting is real and is kept: a waker that is gone
        while the record says it was running never CHOSE to stop, so something
        outside killed it, which is a different diagnosis from every other
        reason. But writing it to wake.exit meant a starting waker overwrote
        the record of the one it replaced, and on this host a window reload
        starts one. The file then described the healthy waker and never the
        failed one — in exactly the case somebody was reading it for.

        So `running` moved to wake.alive, where it answers only that question
        and cannot displace an answer to another."""
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
        self.assertNotIn("running", recorded)
        self.assertIn("woke", recorded[-1])
        self.assertEqual(self.alive_record()["pid"], os.getpid())

    def test_A_NEW_WAKER_DOES_NOT_BURY_WHY_THE_LAST_ONE_STOPPED(self):
        """The whole of #11 in one assertion.

        Reported with the file reading {"reason": "running", "pid": 503} — pid
        503 being the waker that started AFTER the failure under investigation.
        The record of the one alive when the message arrived was gone. That is
        structural, not unlucky: a new waker starting is the event that
        destroys the evidence about why the old one stopped."""
        self.mod.claim_pidfile = lambda: False
        self.run_main()                       # the waker that failed
        first = self.exit_records()
        self.assertEqual(len(first), 1)

        self.mod.addressed = lambda channel, entry: None
        self.mod.claim_pidfile = lambda: True
        self.mod.superseded = lambda: True
        self.mod.time = NoSleep()
        self.run_main()                       # its healthy replacement

        reasons = [r["reason"] for r in self.exit_records()]
        self.assertIn("pidfile", reasons[-2])
        self.assertIn("superseded", reasons[-1])

    def test_the_history_is_CAPPED_so_the_file_stays_readable(self):
        """Kept small on purpose. The point is the pair — the waker that
        stopped and the one that replaced it — not a log nobody reads."""
        self.mod.claim_pidfile = lambda: False
        for _ in range(self.mod.KEEP_EXITS + 3):
            self.run_main()
        self.assertEqual(len(self.exit_records()), self.mod.KEEP_EXITS)

    def test_the_ONE_RECORD_format_is_still_readable(self):
        """An installed waker is mid-flight when this ships, and the record it
        already wrote is the one somebody will be interrogating. Discarding it
        would repeat this bug once at upgrade time, which is a poor way to fix
        it."""
        path = os.path.join(self.project, ".llm_chat", "wake.exit")
        with open(path, "w") as f:
            json.dump({"reason": "in no rooms", "pid": 41, "at": 1}, f)
        self.assertEqual(self.mod.read_exits()[0]["pid"], 41)
        self.mod.claim_pidfile = lambda: False
        self.run_main()
        self.assertEqual(len(self.exit_records()), 2)
        self.assertIn("no rooms", self.exit_records()[0]["reason"])

    def test_a_corrupt_history_does_not_break_the_exit_being_recorded(self):
        """Bookkeeping must never break the thing it describes, and a file
        that is a list of the wrong things is as likely as one that is not
        JSON."""
        path = os.path.join(self.project, ".llm_chat", "wake.exit")
        for junk in ("{not json", "[1, 2, 3]", '"a string"', "[]"):
            with self.subTest(junk=junk):
                with open(path, "w") as f:
                    f.write(junk)
                self.assertEqual(self.mod.read_exits(), [])
                self.mod.record_exit("whatever")
                # The junk is dropped and the new record is the only one —
                # asserted by LENGTH, because `[-1]` alone would pass on a
                # history that kept three integers in front of it.
                self.assertEqual(len(self.exit_records()), 1)
                self.assertIn("whatever", self.exit_records()[-1]["reason"])

    def test_an_exit_records_the_SERVER_it_was_polling(self):
        """"The waker died" and "the waker was fine and its backend went away"
        have opposite remedies and were indistinguishable after the fact. The
        reporter's own incident was the second — they restarted the zonai
        server five minutes before the message that never arrived."""
        self.mod.claim_pidfile = lambda: False
        self.run_main()
        self.assertEqual(self.exit_record()["server"], "http://127.0.0.1:1")

    def test_A_STUB_SESSION_SAYS_SO_rather_than_standing_down_silently(self):
        """Issue #12. A waker armed under one id is in no rooms while another
        id in the same project holds them. "Nothing to listen for" is true and
        useless — it reads as an empty project, and the agent goes permanently
        deaf with every other check green.

        This docstring used to name a window reload as the cause. That was
        asserted without checking and is contradicted by this repo's own
        transcript: one sessionId from 2026-08-03, the process serving it
        started 2026-08-12. The split is real; the mechanism is open."""
        base = os.path.join(self.project, ".llm_chat", "sessions", "5930ff25")
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, "joined.json"), "w") as f:
            json.dump({"room": {"identity": "someone"}}, f)
        with open(os.path.join(self.project, ".llm_chat", "joined.json"),
                  "w") as f:
            json.dump({}, f)
        self.assertEqual(self.run_main(), 0)
        reason = self.exit_record()["reason"]
        self.assertIn("identity split", reason)
        self.assertIn("5930ff25", reason)

    def test_THE_WAKER_ASKS_THE_CLI_TO_DO_DEFERRED_WORK(self):
        """It is the only process that exists precisely BECAUSE nothing is
        happening — asleep on a doorbell during exactly the silence the
        maintenance queue is waiting for."""
        seen = []
        real = self.mod.subprocess.run
        self.mod.subprocess.run = lambda argv, **kw: seen.append(argv)
        try:
            # THE REAL ONE. setUp replaces run_maintenance with a no-op so the
            # loop tests do not spawn a subprocess per pass — this is the test
            # that asserts what it actually does, so it reaches past the stub.
            self.real_run_maintenance()
        finally:
            self.mod.subprocess.run = real
        self.assertEqual(len(seen), 1)
        self.assertIn("maintenance", seen[0])
        self.assertIn("run", seen[0])

    def test_it_does_not_ask_when_there_is_no_server_to_ask(self):
        self.mod.joined_rooms = lambda: {}
        real = self.mod.subprocess.run
        self.mod.subprocess.run = lambda *a, **kw: self.fail("asked anyway")
        try:
            self.real_run_maintenance()     # the real one, past setUp's stub
        finally:
            self.mod.subprocess.run = real

    def test_a_failing_maintenance_run_never_breaks_the_waker(self):
        """It is bookkeeping running from a hook. An exception escaping would
        take out the polling it was only trying to be helpful alongside."""
        real = self.mod.subprocess.run

        def boom(*a, **kw):
            raise OSError("no such file")

        self.mod.subprocess.run = boom
        try:
            self.real_run_maintenance()     # the real one, past setUp's stub
        finally:
            self.mod.subprocess.run = real

    def test_A_WAKER_DOES_NOT_REPORT_A_SPLIT_WITH_ITSELF(self):
        """The false alarm this is one line away from. A waker reaches the
        stand-down only when it is in no rooms, and its own session directory
        can hold an EMPTY joined.json — a file, present, holding nothing. Count
        that and every ordinary empty session accuses itself of an identity
        split, which is the kind of noise that gets a diagnostic ignored."""
        self.mod._SID = "eaf6e8d1"
        mine = os.path.join(self.project, ".llm_chat", "sessions", "eaf6e8d1")
        os.makedirs(mine, exist_ok=True)
        with open(os.path.join(mine, "joined.json"), "w") as f:
            json.dump({}, f)
        self.assertEqual(self.mod.sessions_holding_rooms(), [])

    def test_an_EMPTY_PROJECT_is_still_reported_as_simply_empty(self):
        """Paired. Being in no rooms while nobody else holds any is the
        ordinary case and must not be dressed up as a split — a check that
        cries wolf on the healthy path is worse than no check."""
        with open(os.path.join(self.project, ".llm_chat", "joined.json"),
                  "w") as f:
            json.dump({}, f)
        self.assertEqual(self.run_main(), 0)
        reason = self.exit_record()["reason"]
        self.assertIn("nothing to listen for", reason)
        self.assertNotIn("identity split", reason)

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

    def test_an_unwritable_state_dir_does_not_break_the_LIVE_record_either(self):
        """Paired with it, and the newer of the two files — a rule that holds
        for one bookkeeping write and not the other is not a rule."""
        self.mod.ALIVE_PATH = "/proc/nope/wake.alive"
        self.mod.record_alive("http://x")    # must not raise

    def test_A_BROKEN_ROOM_FILE_does_not_break_the_exit_it_describes(self):
        """`_polling_server` reads rooms while an exit is being recorded, and
        an exception there would destroy the record explaining the exit — the
        bookkeeping killing the thing it exists to describe."""
        def boom():
            raise RuntimeError("unreadable")
        self.mod.joined_rooms = boom
        self.assertIsNone(self.mod._polling_server())
        self.mod.record_exit("still recorded")
        self.assertIn("still recorded", self.exit_record()["reason"])

    def test_BOTH_RECORDS_NAME_THE_SESSION_that_wrote_them(self):
        """wake.exit and wake.alive are PROJECT-level while a waker is
        session-scoped, so without this two wakers' records are
        indistinguishable in the one file they share — which is issue #12
        arriving inside the fix for issue #11."""
        self.mod._SID = "5930ff25"
        self.mod.claim_pidfile = lambda: False
        self.run_main()
        self.assertEqual(self.exit_record()["session"], "5930ff25")
        self.mod.record_alive("http://127.0.0.1:1")
        self.assertEqual(self.alive_record()["session"], "5930ff25")

    def test_no_session_id_leaves_the_field_OUT_rather_than_empty(self):
        """Paired. A human at a terminal has no session, and an empty string
        there would read as a session whose id is blank."""
        self.mod._SID = ""
        self.mod.claim_pidfile = lambda: False
        self.run_main()
        self.assertNotIn("session", self.exit_record())

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

    def test_a_failing_CLI_IS_NOT_BELIEVED_EVEN_WHEN_IT_PRINTS_CLOSED(self):
        """The case that makes the exit-code check measurable, and the reason
        the mutation for it SURVIVED the first sweep that ran tests.

        Every other failing-CLI fixture here prints nothing or prints
        rubbish — and each of those still returns True after the exit-code
        check is deleted, by a later branch: an empty listing means the room
        is 'not listed', and unparseable output is caught by the ValueError.
        Four tests, one answer, none of them measuring this check.

        A non-zero exit whose stdout happens to PARSE and say `closed` is the
        only shape where believing it and refusing to believe it differ. The
        cost of getting this wrong is a waker retiring permanently on a false
        premise — which is exactly what this repo's own mutation sweep does to
        the CLI for a few seconds at a time."""
        self.assertTrue(
            self.answer(returncode=1, stdout=self.listing(True)),
            "a CLI that exited non-zero was believed about closure")

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
