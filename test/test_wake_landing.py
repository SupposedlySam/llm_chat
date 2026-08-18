"""Did the wake LAND, or did we only ask for one?

Issue #6. `asyncRewake` is the one hook whose success is invisible: the poll
runs, the pid rotates, `doctor` said "listening now: yes", and if the host
quietly ignores exit 2 the messages simply arrive later — when the agent next
runs a tool. Reported from a VSCode extension host, three times out of three,
with every other check green.

The claim under test is evidence, never proof. A false negative says "cannot
confirm", which is honest. A false positive says "listening" to somebody whose
replies never arrive, which is the bug.
"""
import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import FakeServer, load  # noqa: E402

cli = load("llm_chat")
waker = load("llm-chat-wake")


class WakerEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (waker.STATE, waker.REWAKE_PATH, waker.LANDED_PATH)
        waker.STATE = self.tmp.name
        waker.REWAKE_PATH = os.path.join(self.tmp.name, "wake.rewake")
        waker.LANDED_PATH = os.path.join(self.tmp.name, "wake.landed")

    def tearDown(self):
        waker.STATE, waker.REWAKE_PATH, waker.LANDED_PATH = self.saved
        self.tmp.cleanup()

    def pending(self, at):
        with open(waker.REWAKE_PATH, "w") as f:
            json.dump({"at": at, "pid": 1}, f)

    def test_asking_for_a_rewake_leaves_a_note(self):
        waker.note_rewake()
        self.assertTrue(os.path.exists(waker.REWAKE_PATH))

    def test_A_WAKER_STARTING_RIGHT_AFTER_ONE_RECORDS_A_LANDING(self):
        """The only receipt available: a hook that exits 2 cannot observe what
        happens next, so the next waker finding the note fresh is the
        evidence."""
        self.pending(time.time())
        waker.wake_landing("Stop")
        self.assertTrue(os.path.exists(waker.LANDED_PATH))

    def test_A_STALE_REQUEST_IS_NOT_A_LANDING(self):
        """The half that keeps this honest. A turn beginning an hour after the
        rewake was asked for is not attributable to it — claiming otherwise
        would restore exactly the false green being removed."""
        self.pending(time.time() - waker.REWAKE_GRACE - 60)
        waker.wake_landing("Stop")
        self.assertFalse(os.path.exists(waker.LANDED_PATH))

    def test_the_request_is_cleared_either_way(self):
        """Left behind, a single stale note would make every future waker
        record a landing forever."""
        self.pending(time.time() - waker.REWAKE_GRACE - 60)
        waker.wake_landing("Stop")
        self.assertFalse(os.path.exists(waker.REWAKE_PATH))

    def test_no_pending_request_records_nothing(self):
        waker.wake_landing("Stop")
        self.assertFalse(os.path.exists(waker.LANDED_PATH))

    def test_a_corrupt_note_is_not_a_landing(self):
        with open(waker.REWAKE_PATH, "w") as f:
            f.write("{not json")
        waker.wake_landing("Stop")
        self.assertFalse(os.path.exists(waker.LANDED_PATH))

    def test_a_request_that_cannot_be_CLEARED_is_still_not_a_landing(self):
        """The remove is best-effort; a read-only state dir must not turn a
        stale note into a permanent 'yes, waking works'."""
        self.pending(time.time() - waker.REWAKE_GRACE - 60)
        real = waker.os.remove
        waker.os.remove = lambda path: (_ for _ in ()).throw(OSError("ro"))
        try:
            waker.wake_landing("Stop")
        finally:
            waker.os.remove = real
        self.assertFalse(os.path.exists(waker.LANDED_PATH))

    def test_an_unwritable_state_dir_is_survived_not_fatal(self):
        """Every path here is best-effort. A waker that crashed trying to
        record evidence would take out the polling it was evidence about."""
        waker.STATE = "/proc/nope/deeper"
        waker.REWAKE_PATH = "/proc/nope/deeper/wake.rewake"
        waker.note_rewake()          # must not raise
        self.pending_unwritable = True

    def test_an_unwritable_landing_is_survived_too(self):
        self.pending(time.time())
        waker.LANDED_PATH = "/proc/nope/deeper/wake.landed"
        waker.wake_landing("Stop")         # must not raise

    def test_A_SESSION_START_IS_NOT_A_LANDING(self):
        """The false positive this shipped with, and it is the whole of #13.

        A landing means the harness invoked the model, the model took a turn,
        and the turn ENDED — a Stop. This hook is registered on SessionStart
        too and recorded a landing for both, so a window reload inside the
        grace window left a receipt for something that never happened.

        `doctor` then reported the wake path healthy for ninety minutes while
        every wake failed, and the agent reading it told a human twice that
        the mechanism worked."""
        self.pending(time.time())
        waker.wake_landing("SessionStart")
        self.assertFalse(os.path.exists(waker.LANDED_PATH))

    def test_the_request_is_CONSUMED_by_a_session_start_anyway(self):
        """Paired, and not obvious. Leaving the note would let a Stop arriving
        much later claim a landing it did not earn — the stale-request bug
        through a new door."""
        self.pending(time.time())
        waker.wake_landing("SessionStart")
        self.assertFalse(os.path.exists(waker.REWAKE_PATH))

    def test_the_landing_RECORDS_WHAT_WROTE_IT(self):
        """Because a marker written before this distinction existed cannot be
        told from one written after it, and reading an old one as a confirmed
        turn would preserve the bug for everybody who already has a marker."""
        self.pending(time.time())
        waker.wake_landing("Stop")
        with open(waker.LANDED_PATH) as f:
            self.assertEqual(json.load(f)["event"], "Stop")

    def test_an_UNKNOWN_event_is_not_a_landing(self):
        """The default. A caller that does not say what invoked it has not
        provided evidence, and guessing 'probably a Stop' is how the original
        false positive got in."""
        self.pending(time.time())
        waker.wake_landing()
        self.assertFalse(os.path.exists(waker.LANDED_PATH))

    def test_the_landing_records_the_HOST_that_managed_it(self):
        """So a checkout used from two hosts can say which one works."""
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "cli"
        try:
            self.pending(time.time())
            waker.wake_landing("Stop")
            with open(waker.LANDED_PATH) as f:
                self.assertEqual(json.load(f)["host"], "cli")
        finally:
            os.environ.pop("CLAUDE_CODE_ENTRYPOINT", None)


class MissedWakeWatcherTest(unittest.TestCase):
    """Something that outlives the exit, so a wake that never lands is seen.

    THE CIRCULARITY. Exiting 2 asks the harness to wake the model; if it does
    not, no turn happens, so no Stop fires, so no waker starts, so nothing
    looks. Every other detector here needs a turn to run, and a turn is
    exactly what did not occur. A detached child is the only thing that can
    ask afterwards.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mod = load("llm-chat-wake")
        self.mod.STATE = self.tmp.name
        self.mod.REWAKE_PATH = os.path.join(self.tmp.name, "wake.rewake")
        self.mod.MISSED_PATH = os.path.join(self.tmp.name, "wake.missed")
        self.mod.AUTO_RELOAD_PATH = os.path.join(self.tmp.name,
                                                 "wake-by-reload")
        self.mod.REWAKE_GRACE = 0
        self.mod.WATCH_MARGIN = 0
        self.slept = []
        self.mod.time = type("T", (), {
            "sleep": staticmethod(lambda s: self.slept.append(s)),
            "time": staticmethod(lambda: 1000.0)})
        self.ran = []
        self.mod.subprocess = type("S", (), {
            "DEVNULL": -3,
            "Popen": staticmethod(lambda *a, **k: None),
            "run": staticmethod(lambda argv, **k: (
                self.ran.append(argv) or type("R", (), {
                    "returncode": 0, "stdout": "reload requested",
                    "stderr": ""})()))})

    def tearDown(self):
        self.tmp.cleanup()

    def pending(self, at=1000.0):
        with open(self.mod.REWAKE_PATH, "w") as f:
            json.dump({"at": at, "pid": 7}, f)

    def missed(self):
        with open(self.mod.MISSED_PATH) as f:
            return json.load(f)

    def test_A_CONSUMED_REQUEST_MEANS_THE_WAKE_LANDED(self):
        """`wake_landing` removes the note as its first act, so a note that is
        gone is a turn that happened. Nothing is owed and nothing is
        recorded."""
        self.assertEqual(self.mod.watch(1000.0), 0)
        self.assertFalse(os.path.exists(self.mod.MISSED_PATH))

    def test_A_NOTE_STILL_SITTING_THERE_IS_A_MISSED_WAKE(self):
        self.pending()
        self.mod.watch(1000.0)
        self.assertEqual(self.missed()["requested_at"], 1000)

    def tool_mark(self, when):
        d = os.path.join(self.tmp.name, "probe")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "post-tool-use")
        open(path, "w").close()
        os.utime(path, (when, when))

    def test_A_TURN_STILL_RUNNING_IS_NOT_A_MISSED_WAKE(self):
        """`wake_landing` only consumes the note when a turn ENDS, so a turn
        that started on this rewake and is still going leaves the note exactly
        where a wake that never arrived does. This record is now spoken to the
        agent (#20), and telling the session it woke that its wake never
        landed is how a report stops being believed."""
        self.pending()
        self.tool_mark(1500.0)
        self.mod.watch(1000.0)
        self.assertFalse(os.path.exists(self.mod.MISSED_PATH))

    def test_a_tool_call_from_BEFORE_the_request_proves_nothing(self):
        """Paired with the one above, and the reason it is an mtime
        comparison rather than a presence check: every project that has ever
        run a tool has this mark, so presence alone would silence the report
        everywhere."""
        self.pending()
        self.tool_mark(500.0)
        self.mod.watch(1000.0)
        # EXISTENCE FIRST, and not for tidiness. Reading the file straight
        # off made this ERROR rather than FAIL when the comparison was
        # neutered, and the sweep counts an error as "not measured" — the
        # assertion that was going to check the behaviour never ran.
        self.assertTrue(os.path.exists(self.mod.MISSED_PATH),
                        "a stale mark must not read as a running turn")
        self.assertEqual(self.missed()["requested_at"], 1000)

    def test_NO_tool_mark_at_all_still_reports_the_miss(self):
        """A repo wired before probing shipped has no mark. Reading absence as
        "a turn ran" would silence this for exactly the installs least likely
        to be healthy."""
        self.pending()
        self.mod.watch(1000.0)
        self.assertTrue(os.path.exists(self.mod.MISSED_PATH),
                        "absence of a mark is not evidence a turn ran")
        self.assertEqual(self.missed()["requested_at"], 1000)

    def test_a_NEWER_request_is_not_this_watcher_s_business(self):
        """Two wakes in quick succession. The second one's note is not
        evidence about the first, and recording it as such would report a miss
        every time a session is busy."""
        self.pending(at=2000.0)
        self.mod.watch(1000.0)
        self.assertFalse(os.path.exists(self.mod.MISSED_PATH))

    def test_IT_DOES_NOT_RELOAD_UNLESS_OPTED_IN(self):
        """The human's rule. The record is useful to everybody; the action is
        somebody's call."""
        self.pending()
        self.mod.watch(1000.0)
        self.assertEqual(self.ran, [])
        self.assertFalse(self.missed()["acted"])

    def test_OPTED_IN_it_asks_the_CLI_to_reload(self):
        self.pending()
        open(self.mod.AUTO_RELOAD_PATH, "w").close()
        self.mod.watch(1000.0)
        self.assertEqual(len(self.ran), 1)
        self.assertIn("reload", self.ran[0])
        self.assertIn("--force", self.ran[0])
        self.assertTrue(self.missed()["acted"])

    def test_the_CLI_owns_every_refusal(self):
        """It asks rather than decides: not a VSCode host, more than one live
        session, SessionStart never seen firing. A refusal is recorded as not
        having acted, with what it said."""
        self.pending()
        open(self.mod.AUTO_RELOAD_PATH, "w").close()
        self.mod.subprocess = type("S", (), {
            "DEVNULL": -3, "Popen": staticmethod(lambda *a, **k: None),
            "run": staticmethod(lambda argv, **k: type("R", (), {
                "returncode": 1, "stdout": "",
                "stderr": "refusing: 2 live sessions"})())})
        self.mod.watch(1000.0)
        record = self.missed()
        self.assertFalse(record["acted"])
        self.assertIn("2 live sessions", record["said"])

    def test_a_reload_that_EXPLODES_still_records_the_miss(self):
        """The miss is the fact worth keeping. Losing it because the remedy
        failed would leave the session deaf AND undiagnosed."""
        self.pending()
        open(self.mod.AUTO_RELOAD_PATH, "w").close()
        def boom(*a, **kw):
            raise OSError("no such file")
        self.mod.subprocess = type("S", (), {
            "DEVNULL": -3, "Popen": staticmethod(lambda *a, **k: None),
            "run": staticmethod(boom)})
        self.mod.watch(1000.0)
        self.assertIn("no such file", self.missed()["said"])

    def test_it_waits_out_the_GRACE_WINDOW_before_judging(self):
        """Asking immediately would call every wake a miss."""
        self.mod.REWAKE_GRACE = 90
        self.mod.WATCH_MARGIN = 20
        self.mod.watch(1000.0)
        self.assertEqual(self.slept, [110])

    def test_the_spawn_never_breaks_the_wake_it_describes(self):
        """It runs inside `wake`, one line before the exit that delivers the
        message. Bookkeeping must not take the delivery down with it."""
        self.pending()
        self.mod.subprocess = type("S", (), {
            "DEVNULL": -3,
            "Popen": staticmethod(lambda *a, **k: (_ for _ in ()).throw(
                OSError("cannot fork"))),
            "run": staticmethod(lambda *a, **k: None)})
        self.mod.watch_for_a_missed_wake()      # must not raise

    def test_no_pending_note_spawns_nothing(self):
        seen = []
        self.mod.subprocess = type("S", (), {
            "DEVNULL": -3,
            "Popen": staticmethod(lambda *a, **k: seen.append(a)),
            "run": staticmethod(lambda *a, **k: None)})
        self.mod.watch_for_a_missed_wake()
        self.assertEqual(seen, [])

    def test_an_unwritable_state_dir_does_not_break_the_watcher(self):
        """Every bookkeeping write here is best-effort, and this one runs in a
        detached process nobody is watching — raising would be silent."""
        self.pending()
        self.mod.MISSED_PATH = "/proc/nope/wake.missed"
        self.assertEqual(self.mod.watch(1000.0), 0)   # must not raise

    def test_THE_WATCH_MODE_IS_REACHED_FROM_ARGV(self):
        """It is spawned detached with no payload coming, so main() has to
        take this branch BEFORE reading stdin — otherwise it waits forever for
        something nobody will send."""
        seen = []
        real = self.mod.watch
        self.mod.watch = lambda at: seen.append(at) or 0
        argv = sys.argv
        sys.argv = ["llm-chat-wake", "--watch", "1234.5"]
        try:
            self.assertEqual(self.mod.main(), 0)
        finally:
            sys.argv = argv
            self.mod.watch = real
        self.assertEqual(seen, [1234.5])

    def test_a_MALFORMED_watch_argument_does_not_crash(self):
        argv = sys.argv
        sys.argv = ["llm-chat-wake", "--watch", "not-a-number"]
        try:
            self.assertEqual(self.mod.main(), 0)
        finally:
            sys.argv = argv

    def test_MAIN_ACTUALLY_ARMS_THE_WATCHER_before_waking(self):
        """Every other test here calls `watch_for_a_missed_wake` directly, so
        all of them pass with the CALL SITE deleted — which is the whole
        mechanism. Moving the spawn out of `wake` into main() to stop tests
        forking real processes left nothing asserting main still does it, and
        the mutation SURVIVED the next sweep.

        Ordered after note_rewake, because the watcher reads the note it is
        going to watch."""
        order = []
        real_note = self.mod.note_rewake
        real_watch = self.mod.watch_for_a_missed_wake
        real_wake = self.mod.wake
        self.mod.note_rewake = lambda: order.append("note")
        self.mod.watch_for_a_missed_wake = lambda: order.append("watch")
        self.mod.wake = lambda blocks: order.append("wake")
        try:
            self.mod.announce(["#room\n  [other] hi"])
        except SystemExit:
            pass
        finally:
            self.mod.note_rewake = real_note
            self.mod.watch_for_a_missed_wake = real_watch
            self.mod.wake = real_wake
        self.assertEqual(order, ["note", "watch", "wake"])

    def test_a_corrupt_note_spawns_nothing(self):
        with open(self.mod.REWAKE_PATH, "w") as f:
            f.write("{not json")
        seen = []
        self.mod.subprocess = type("S", (), {
            "DEVNULL": -3,
            "Popen": staticmethod(lambda *a, **k: seen.append(a)),
            "run": staticmethod(lambda *a, **k: None)})
        self.mod.watch_for_a_missed_wake()
        self.assertEqual(seen, [])


class StillLandingTest(unittest.TestCase):
    """Is the wake path working NOW — asked of the queue, not of history.

    The whole of #13. A landing marker says a wake worked once; a message that
    wakes this identity, unread, and NEWER than that marker says the path is
    broken at this moment. The second is the question people are actually
    asking when they run `doctor`, and it had no answer.
    """

    cli = load("llm_chat")

    def setUp(self):
        self.fake = FakeServer()
        self.real = self.cli.call
        self.cli.call = self.fake.call
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name
        os.environ["CLAUDE_PROJECT_DIR"] = self.project
        state = os.path.join(self.project, ".llm_chat")
        os.makedirs(state, exist_ok=True)
        with open(os.path.join(state, "joined.json"), "w") as f:
            json.dump({"room": {"identity": "me",
                                "server": "http://127.0.0.1:1"}}, f)
        self.fake.channel("room")
        self.fake.membership("room", "me", seen_seq=0)

    def tearDown(self):
        self.cli.call = self.real
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.tmp.cleanup()

    def waiting(self, landed_at):
        return self.cli.waiting_longer_than_the_last_wake(
            "http://127.0.0.1:1", self.project, landed_at)

    def test_A_MESSAGE_NEWER_THAN_THE_LANDING_IS_THE_LIVE_ANSWER(self):
        """The reported state exactly: a wake landed 94 minutes ago, seq 43
        has been waiting 4 minutes and wakes you, and doctor said healthy."""
        now = self.cli.now_ms()
        self.fake.message("room", 43, "supposedlysam", "<!here> anyone?",
                          created_at=now - 240_000)
        found = self.waiting(now / 1000.0 - 5640)
        self.assertIsNotNone(found)
        seq, seconds = found
        self.assertEqual(seq, 43)
        self.assertAlmostEqual(seconds, 240, delta=5)

    def test_a_message_OLDER_than_the_landing_is_not_evidence(self):
        """That wake may well be the one that delivered it. Counting it would
        make the check cry wolf on a healthy path, which is how a diagnostic
        gets ignored."""
        now = self.cli.now_ms()
        self.fake.message("room", 43, "supposedlysam", "<!here> anyone?",
                          created_at=now - 5_000_000)
        self.assertIsNone(self.waiting(now / 1000.0 - 60))

    def test_a_message_that_does_NOT_wake_me_is_not_evidence(self):
        """A passive message is not supposed to produce a wake, so its
        presence says nothing about whether wakes work. Addressed to somebody
        else here — in an ordinary room an UNADDRESSED message does wake you,
        which is the default this had to be written around."""
        now = self.cli.now_ms()
        self.fake.message("room", 43, "supposedlysam", "for you, other",
                          audience="other", created_at=now - 240_000)
        self.assertIsNone(self.waiting(now / 1000.0 - 5640))

    def test_an_UNADDRESSED_message_in_an_ordinary_room_IS_evidence(self):
        """Paired, because the default is the opposite of the obvious one:
        unaddressed wakes you without being for you."""
        now = self.cli.now_ms()
        self.fake.message("room", 43, "supposedlysam", "no audience at all",
                          created_at=now - 240_000)
        self.assertEqual(self.waiting(now / 1000.0 - 5640)[0], 43)

    def test_MY_OWN_MESSAGE_is_not_evidence(self):
        """Nothing wakes you for your own words, so an unread one of them
        proves nothing about the path."""
        now = self.cli.now_ms()
        self.fake.message("room", 43, "me", "<!here> mine",
                          created_at=now - 240_000)
        self.assertIsNone(self.waiting(now / 1000.0 - 5640))

    def test_an_ALREADY_READ_message_is_not_evidence(self):
        now = self.cli.now_ms()
        self.fake.message("room", 43, "supposedlysam", "<!here> anyone?",
                          created_at=now - 240_000)
        self.fake.get_membership("room", "me")["seen_seq"] = 43
        self.assertIsNone(self.waiting(now / 1000.0 - 5640))

    def test_the_OLDEST_waiting_message_is_the_one_reported(self):
        """It is the one that has been failing longest, so it is the strongest
        statement available about the path."""
        now = self.cli.now_ms()
        self.fake.message("room", 43, "supposedlysam", "<!here> first",
                          created_at=now - 600_000)
        self.fake.message("room", 44, "supposedlysam", "<!here> second",
                          created_at=now - 60_000)
        self.assertEqual(self.waiting(now / 1000.0 - 5640)[0], 43)

    def test_NO_LANDING_AT_ALL_still_answers(self):
        """A checkout that has never recorded one still wants to know that
        something is stuck. None means 'no landing to be newer than', not
        'skip the check'."""
        now = self.cli.now_ms()
        self.fake.message("room", 43, "supposedlysam", "<!here> anyone?",
                          created_at=now - 240_000)
        self.assertEqual(self.waiting(None)[0], 43)

    def test_an_empty_queue_is_not_a_complaint(self):
        self.assertIsNone(self.waiting(self.cli.now_ms() / 1000.0 - 5640))

    def test_a_server_that_cannot_be_REACHED_makes_no_claim(self):
        """Every kind of not-knowing answers None, because this is only ever
        used to make a NEGATIVE claim louder — never a positive one."""
        def dead(*a, **kw):
            raise SystemExit("no llm_chat server")
        self.cli.call = dead
        self.assertIsNone(self.waiting(1))

    def test_a_server_that_dies_MID_LOOKUP_makes_no_claim(self):
        """Separate from the wholly-unreachable case, because it is a
        different code path and the same rule has to hold on it: the channel
        and membership come back, the messages do not. Still None — nothing
        here may turn a lookup failure into an accusation."""
        real = self.cli.rows

        def half_dead(server, table, where=None, **kw):
            if table == "messages":
                raise SystemExit("no llm_chat server at %s" % server)
            return real(server, table, where, **kw)

        self.cli.rows = half_dead
        self.addCleanup(lambda: setattr(self.cli, "rows", real))
        self.assertIsNone(self.waiting(1))

    def test_a_room_with_no_identity_is_skipped(self):
        with open(os.path.join(self.project, ".llm_chat", "joined.json"),
                  "w") as f:
            json.dump({"room": {"server": "http://127.0.0.1:1"}}, f)
        self.assertIsNone(self.waiting(1))

    def test_a_room_the_server_does_not_have_is_skipped(self):
        with open(os.path.join(self.project, ".llm_chat", "joined.json"),
                  "w") as f:
            json.dump({"ghost": {"identity": "me",
                                 "server": "http://127.0.0.1:1"}}, f)
        self.assertIsNone(self.waiting(1))

    def test_DOCTOR_CONTRADICTS_ITS_OWN_OPTIMISTIC_LINE(self):
        """End to end, and the sentence that matters most: the live state wins
        over the historical one, in the same paragraph, out loud.

        This is what the reporter needed and did not get — they read "a wake
        has LANDED here" and told a human the mechanism worked, twice, while
        a message sat queued."""
        now = self.cli.now_ms()
        self.fake.message("room", 43, "supposedlysam", "<!here> anyone?",
                          created_at=now - 240_000)
        state = os.path.join(self.project, ".llm_chat")
        with open(os.path.join(state, "wake.landed"), "w") as f:
            json.dump({"at": now / 1000.0 - 5640, "event": "Stop",
                       "host": "claude-vscode"}, f)
        with open(os.path.join(state, "wake.pid"), "w") as f:
            f.write(str(os.getpid()))
        settings = os.path.join(self.project, ".claude")
        os.makedirs(settings, exist_ok=True)
        hooks = {"hooks": {
            "PostToolUse": [{"matcher": ".*", "hooks": [
                {"type": "command", "command": "/x/bin/llm-chat-deliver"}]}],
            "Stop": [{"hooks": [
                {"type": "command", "command": "/x/bin/llm-chat-wake"}]}],
            "SessionStart": [{"hooks": [
                {"type": "command", "command": "/x/bin/llm-chat-wake"}]}]}}
        with open(os.path.join(settings, "settings.json"), "w") as f:
            json.dump(hooks, f)
        probe = os.path.join(state, "probe")
        os.makedirs(probe, exist_ok=True)
        for mark in ("post-tool-use", "stop"):
            with open(os.path.join(probe, mark), "w") as f:
                f.write("1 1")

        out = io.StringIO()
        with redirect_stdout(out):
            self.cli.do_doctor("http://127.0.0.1:1")
        text = out.getvalue()
        self.assertIn("a wake LANDED 94m ago", text)
        self.assertIn("BUT seq 43 has been waiting", text)
        self.assertIn("the wake is NOT landing", text)
        # The order is the argument: the optimistic line first, then the live
        # state contradicting it. A reader who stops at the first sentence is
        # the reader this failed.
        self.assertLess(text.index("a wake LANDED"), text.index("BUT seq 43"))


class DoctorHonestyTest(unittest.TestCase):
    """POLLING and WAKING are two claims, and doctor used to make the second
    on the strength of the first."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name
        os.makedirs(os.path.join(self.project, ".llm_chat"))

    def tearDown(self):
        self.tmp.cleanup()

    def land(self, host="claude-vscode"):
        with open(os.path.join(self.project, ".llm_chat", "wake.landed"),
                  "w") as f:
            json.dump({"at": time.time(), "host": host}, f)

    def test_no_record_means_no_claim(self):
        at, _ = cli.wake_landing(self.project)
        self.assertIsNone(at)

    def test_a_record_is_reported_with_its_host(self):
        self.land()
        at, host = cli.wake_landing(self.project)
        self.assertIsNotNone(at)
        self.assertEqual(host, "claude-vscode")

    def test_a_corrupt_record_is_not_a_landing(self):
        with open(os.path.join(self.project, ".llm_chat", "wake.landed"),
                  "w") as f:
            f.write("{not json")
        self.assertIsNone(cli.wake_landing(self.project)[0])

    def test_it_names_the_HOST_when_it_cannot_confirm(self):
        """"Which host" is the single most useful fact when the answer is
        "cannot confirm", because the fix is a different one per host."""
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "claude-vscode"
        try:
            self.assertEqual(cli.wake_landing(self.project)[1],
                             "claude-vscode")
        finally:
            os.environ.pop("CLAUDE_CODE_ENTRYPOINT", None)


if __name__ == "__main__":
    unittest.main()
