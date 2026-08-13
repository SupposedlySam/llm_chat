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
from support import load  # noqa: E402

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
        waker.wake_landing()
        self.assertTrue(os.path.exists(waker.LANDED_PATH))

    def test_A_STALE_REQUEST_IS_NOT_A_LANDING(self):
        """The half that keeps this honest. A turn beginning an hour after the
        rewake was asked for is not attributable to it — claiming otherwise
        would restore exactly the false green being removed."""
        self.pending(time.time() - waker.REWAKE_GRACE - 60)
        waker.wake_landing()
        self.assertFalse(os.path.exists(waker.LANDED_PATH))

    def test_the_request_is_cleared_either_way(self):
        """Left behind, a single stale note would make every future waker
        record a landing forever."""
        self.pending(time.time() - waker.REWAKE_GRACE - 60)
        waker.wake_landing()
        self.assertFalse(os.path.exists(waker.REWAKE_PATH))

    def test_no_pending_request_records_nothing(self):
        waker.wake_landing()
        self.assertFalse(os.path.exists(waker.LANDED_PATH))

    def test_a_corrupt_note_is_not_a_landing(self):
        with open(waker.REWAKE_PATH, "w") as f:
            f.write("{not json")
        waker.wake_landing()
        self.assertFalse(os.path.exists(waker.LANDED_PATH))

    def test_a_request_that_cannot_be_CLEARED_is_still_not_a_landing(self):
        """The remove is best-effort; a read-only state dir must not turn a
        stale note into a permanent 'yes, waking works'."""
        self.pending(time.time() - waker.REWAKE_GRACE - 60)
        real = waker.os.remove
        waker.os.remove = lambda path: (_ for _ in ()).throw(OSError("ro"))
        try:
            waker.wake_landing()
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
        waker.wake_landing()         # must not raise

    def test_the_landing_records_the_HOST_that_managed_it(self):
        """So a checkout used from two hosts can say which one works."""
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "cli"
        try:
            self.pending(time.time())
            waker.wake_landing()
            with open(waker.LANDED_PATH) as f:
                self.assertEqual(json.load(f)["host"], "cli")
        finally:
            os.environ.pop("CLAUDE_CODE_ENTRYPOINT", None)


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
