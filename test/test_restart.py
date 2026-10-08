"""Restarting the server onto new code, and knowing when that is needed.

A running zonai server keeps the compiled workers it started with. An update
that changes them — the rate-limit policies in lib/src/rate_limit/ — reaches
nobody until the server restarts, and before this every check still said
"current". These pin the restart's ORDER (the gap must be seconds, so the
build happens while the old server still serves) and the staleness probe,
which reads the server's own counter table rather than guessing.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import load  # noqa: E402

cli = load("llm_chat")


class RestartOrderTest(unittest.TestCase):
    NAMES = ("dart_env", "run_steps", "listener_pid", "stop_listener",
             "adopt_machine_store", "launch_server", "server_up")

    def setUp(self):
        self.saved = {n: getattr(cli, n) for n in self.NAMES}
        self.addCleanup(lambda: [setattr(cli, n, v)
                                 for n, v in self.saved.items()])
        self.order = []
        cli.dart_env = lambda: {"DART_SDK": "/fake"}
        cli.run_steps = lambda steps, env: self.order.append(
            "migrate" if steps == [cli.MIGRATE_STEP] else "build")
        cli.listener_pid = lambda server: 4242
        cli.stop_listener = lambda pid, server: self.order.append("stop") or True
        cli.adopt_machine_store = lambda root, port: self.order.append("adopt") or []
        cli.launch_server = lambda server, env: self.order.append("launch")
        cli.server_up = lambda server: True

    def restart(self):
        with open(os.devnull, "w") as sink:
            saved, sys.stdout = sys.stdout, sink
            try:
                cli.restart_server("http://localhost:7717")
            finally:
                sys.stdout = saved

    def test_it_BUILDS_while_the_old_server_still_serves(self):
        """The gap must be seconds, not a compile."""
        self.restart()
        self.assertEqual(self.order,
                         ["build", "stop", "adopt", "migrate", "launch"])

    def test_a_server_that_will_not_stop_changes_nothing_else(self):
        cli.stop_listener = lambda pid, server: self.order.append("stop") or False
        with self.assertRaises(SystemExit) as caught:
            self.restart()
        self.assertEqual(self.order, ["build", "stop"])
        self.assertIn("did not stop", str(caught.exception))

    def test_an_answering_server_with_no_local_process_is_not_guessed_at(self):
        cli.listener_pid = lambda server: None
        with self.assertRaises(SystemExit) as caught:
            self.restart()
        self.assertEqual(self.order, ["build"])
        self.assertIn("no local process", str(caught.exception))

    def test_with_NOTHING_running_it_simply_starts_one(self):
        cli.listener_pid = lambda server: None
        cli.server_up = lambda server: False
        self.restart()
        self.assertEqual(self.order, ["build", "adopt", "migrate", "launch"])


class ListenerHelpersTest(unittest.TestCase):
    """The two helpers that touch a real process, exercised without one."""

    def test_a_server_on_ANOTHER_HOST_has_no_local_listener(self):
        self.assertIsNone(cli.listener_pid("http://example.com:7717"))

    def test_stopping_a_process_that_is_ALREADY_GONE_succeeds(self):
        import subprocess
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()                      # reaped: the pid no longer exists
        self.assertTrue(cli.stop_listener(child.pid, "http://127.0.0.1:1"))


class ReadsAreLimitedTest(unittest.TestCase):
    """The probe: one read of ours, and the server's own counter row."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "zonai_rate_limit.sqlite")
        conn = sqlite3.connect(self.db)
        conn.execute('CREATE TABLE "_rate_limit" ("id" TEXT, "client_ip" '
                     'TEXT, "operation" TEXT, "table" TEXT, "count" INTEGER, '
                     '"window_start" INTEGER)')
        conn.execute("INSERT INTO _rate_limit VALUES "
                     "('a', '::1', 'list', 'channels', 5, 1000)")
        conn.commit()
        conn.close()
        self.saved = (cli.serving_store, cli.rows)
        self.addCleanup(lambda: setattr(cli, "serving_store", self.saved[0])
                        or setattr(cli, "rows", self.saved[1]))
        cli.serving_store = lambda server: self.tmp.name

    def bump(self, *a, **kw):
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE _rate_limit SET count = count + 1")
        conn.commit()
        conn.close()
        return []

    def test_a_server_that_COUNTS_reads_is_stale(self):
        cli.rows = self.bump
        self.assertTrue(cli.reads_are_limited("http://localhost:7717"))

    def test_a_server_that_does_not_is_current(self):
        cli.rows = lambda *a, **kw: []
        self.assertFalse(cli.reads_are_limited("http://localhost:7717"))

    def test_a_429_on_the_probe_read_is_itself_the_answer(self):
        def throttled(*a, **kw):
            raise cli.Throttled("HTTP 429 ... channels/list limit")
        cli.rows = throttled
        self.assertTrue(cli.reads_are_limited("http://localhost:7717"))

    def test_no_store_to_read_is_CANNOT_TELL_not_current(self):
        cli.serving_store = lambda server: None
        self.assertIsNone(cli.reads_are_limited("http://localhost:7717"))

    def test_an_unreachable_server_is_CANNOT_TELL(self):
        def down(*a, **kw):
            raise cli.Unreachable("nothing there")
        cli.rows = down
        self.assertIsNone(cli.reads_are_limited("http://localhost:7717"))


class ReadRefusalSaysRestartTest(unittest.TestCase):
    def test_a_refused_READ_names_the_restart_not_a_wait(self):
        line = cli.throttled_which({"bucket": "memberships/list",
                                    "limit": "100", "reset_at": None})
        self.assertIn("older than", line)
        self.assertIn("restart-server", line)

    def test_a_refused_WRITE_is_still_an_ordinary_throttle(self):
        line = cli.throttled_which({"bucket": "messages/create",
                                    "limit": "1000", "reset_at": None})
        self.assertNotIn("restart-server", line)
        self.assertIn("messages/create limit", line)


class UndeliveredReportTest(unittest.TestCase):
    """The listener saves messages it read and could not post; doctor is
    where a reader learns the file exists."""

    def report(self, text=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        if text is not None:
            os.makedirs(os.path.join(tmp.name, ".llm_chat"))
            with open(os.path.join(tmp.name, ".llm_chat",
                                   "wake.undelivered"), "w") as f:
                f.write(text)
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            cli.report_undelivered(tmp.name)
        return out.getvalue()

    def test_saved_wakes_are_COUNTED_and_located(self):
        text = ("--- 2026-10-08 13:00:00\nhello\n---\nnot an entry\n"
                "--- 2026-10-08 13:05:00\nsecond\n")
        out = self.report(text)
        self.assertIn("UNDELIVERED WAKES: 2", out)
        self.assertIn("wake.undelivered", out)

    def test_no_file_says_nothing(self):
        self.assertEqual(self.report(), "")


if __name__ == "__main__":
    unittest.main()
