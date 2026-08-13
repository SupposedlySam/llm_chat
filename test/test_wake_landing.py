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
