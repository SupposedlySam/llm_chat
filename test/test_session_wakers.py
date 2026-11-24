"""Issue #48: a waker's files belong to its SESSION, not to the checkout.

They lived in the project's `.llm_chat/`, so "newest waker wins" SIGTERMed
every other session's waker in a shared checkout: four live sessions, one
minute, three wakers killed by their neighbours, and an idle session with an
addressed message waiting that nothing would wake. `doctor` and the delivery
hook then read whichever session had written last.

Each test arms real processes or real files rather than asserting on a path
constant, because the bug was a behaviour — one session's waker killing
another's — and a constant can be right while the behaviour is not.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import load  # noqa: E402


def sleeper():
    """A process that stands in for another waker: alive until killed."""
    proc = subprocess.Popen([sys.executable, "-c",
                             "import time; time.sleep(60)"])
    return proc


class SessionWakerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = self.tmp.name
        os.makedirs(os.path.join(self.project, ".llm_chat"))
        os.environ["CLAUDE_PROJECT_DIR"] = self.project
        self.addCleanup(os.environ.pop, "CLAUDE_CODE_SESSION_ID", None)
        self.addCleanup(os.environ.pop, "CLAUDE_PROJECT_DIR", None)

    def waker(self, sid):
        if sid:
            os.environ["CLAUDE_CODE_SESSION_ID"] = sid
        else:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        mod = load("llm-chat-wake")
        mod.BELL_HANDOVER_SEC = 2
        return mod

    def pidfile(self, sid):
        base = os.path.join(self.project, ".llm_chat")
        return os.path.join(base, "sessions", sid, "wake.pid") if sid \
            else os.path.join(base, "wake.pid")

    def plant(self, sid, proc):
        """Record `proc` as session `sid`'s waker, at the path THAT session's
        waker resolves — never a path this test hard-codes. A hard-coded path
        is one the old, project-wide code would never have read, so the
        survival test would have passed against the bug it is here to stop."""
        path = self.waker(sid).PID_PATH
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(str(proc.pid))

    def running(self, proc):
        return proc.poll() is None

    def gone_within(self, proc, seconds):
        """Polled rather than `proc.wait(timeout=)`, whose TimeoutExpired
        would make a surviving process an ERROR instead of a failed
        assertion — and an error is not a measurement."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            if proc.poll() is not None:
                return True
            time.sleep(0.05)
        return False

    def spawn(self):
        proc = sleeper()
        self.addCleanup(proc.wait)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        return proc

    # ── the regression the issue asked for ──────────────────────────────────

    def test_ANOTHER_SESSIONS_waker_SURVIVES_a_new_one_arming(self):
        other = self.spawn()
        self.plant("aaaa0001", other)
        mod = self.waker("bbbb0002")
        self.assertTrue(mod.claim_pidfile())
        self.assertFalse(self.gone_within(other, 0.5),
                         "session bbbb0002's waker killed session aaaa0001's")

    def test_a_second_waker_for_the_SAME_session_still_replaces_the_first(self):
        """Paired: the thing claim_pidfile exists for must still happen, or
        every turn-end stacks another poller and one message wakes N times."""
        older = self.spawn()
        self.plant("bbbb0002", older)
        mod = self.waker("bbbb0002")
        self.assertTrue(mod.claim_pidfile())
        self.assertTrue(self.gone_within(older, 5),
                        "the same session's older waker was left running")

    def test_a_waker_is_not_SUPERSEDED_by_another_sessions_pidfile(self):
        """superseded() is the second half of #48: a waker that was not
        killed stood down as soon as the shared file named another pid."""
        mod = self.waker("bbbb0002")
        self.assertTrue(mod.claim_pidfile())
        self.plant("aaaa0001", self.spawn())
        self.assertFalse(mod.superseded())

    def test_with_NO_SESSION_the_project_path_is_kept(self):
        """A human at a terminal IS the project. SRCode also pins the id to
        the empty string for a tool with no session, which must read as none."""
        for sid in (None, ""):
            mod = self.waker(sid)
            self.assertEqual(mod.PID_PATH, self.pidfile(None))
            self.assertEqual(os.path.dirname(mod.EXIT_PATH),
                             os.path.join(self.project, ".llm_chat"))

    def test_EVERY_waker_record_follows_the_session(self):
        mod = self.waker("bbbb0002")
        session = os.path.join(self.project, ".llm_chat", "sessions",
                               "bbbb0002")
        for path in (mod.PID_PATH, mod.EXIT_PATH, mod.ALIVE_PATH,
                     mod.REWAKE_PATH, mod.LANDED_PATH, mod.MISSED_PATH,
                     mod.UNDELIVERED_PATH):
            self.assertEqual(os.path.dirname(path), session, path)
        mod.record_exit("checked")
        mod.record_alive()
        mod.record_landing("turn-started")
        for name in ("wake.exit", "wake.alive", "wake.landed"):
            self.assertTrue(os.path.isfile(os.path.join(session, name)), name)


class SessionReadersTest(unittest.TestCase):
    """The readers must look where the waker now writes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = self.tmp.name
        os.environ["CLAUDE_PROJECT_DIR"] = self.project
        self.addCleanup(os.environ.pop, "CLAUDE_CODE_SESSION_ID", None)
        self.addCleanup(os.environ.pop, "CLAUDE_PROJECT_DIR", None)

    def write(self, sid, name, record):
        d = os.path.join(self.project, ".llm_chat", *(("sessions", sid)
                                                       if sid else ()))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "w") as f:
            json.dump(record, f)

    def test_the_hook_announces_a_miss_only_to_the_session_that_missed(self):
        deliver = load("llm-chat-deliver")
        self.write("aaaa0001", "wake.missed", {"at": int(time.time()) - 60})
        self.assertEqual(deliver.missed_wake_note("bbbb0002"), "",
                         "session bbbb0002 was told about aaaa0001's miss")
        self.assertIn("NEVER LANDED", deliver.missed_wake_note("aaaa0001"))

    def test_the_hook_reads_the_sessions_own_LANDING(self):
        deliver = load("llm-chat-deliver")
        now = int(time.time())
        self.write("aaaa0001", "wake.missed", {"at": now - 60})
        self.write("aaaa0001", "wake.landed", {"at": now - 10})
        self.assertEqual(deliver.missed_wake_note("aaaa0001"), "",
                         "a later landing in the session did not retire it")

    def test_doctor_reads_THIS_SESSIONS_waker_not_the_last_writer(self):
        cli = load("llm_chat")
        self.write("aaaa0001", "wake.alive", {"pid": 1, "at": 100})
        self.write(None, "wake.alive", {"pid": 2, "at": 200})
        os.environ["CLAUDE_CODE_SESSION_ID"] = "aaaa0001"
        self.assertEqual(cli.wake_file(self.project, "wake.alive"),
                         os.path.join(self.project, ".llm_chat", "sessions",
                                      "aaaa0001", "wake.alive"))
        os.environ.pop("CLAUDE_CODE_SESSION_ID")
        self.assertEqual(cli.wake_file(self.project, "wake.alive"),
                         os.path.join(self.project, ".llm_chat",
                                      "wake.alive"))


if __name__ == "__main__":
    unittest.main()
