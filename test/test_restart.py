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
             "adopt_machine_store", "launch_server", "server_up",
             "check_machine_store")

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
        cli.check_machine_store = lambda port: self.order.append("check") or "/s"

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
                         ["build", "check", "stop", "adopt", "migrate",
                          "launch"])

    def test_an_unusable_STORE_is_refused_BEFORE_the_stop(self):
        """#43: a root-owned ~/.local failed the store step after the old
        server had been stopped, and the machine had no chat."""
        def unusable(port):
            raise SystemExit("cannot use ~/.local/share ... LLM_CHAT_STORE")
        cli.check_machine_store = unusable
        with self.assertRaises(SystemExit) as caught:
            self.restart()
        self.assertEqual(self.order, ["build"])
        self.assertIn("LLM_CHAT_STORE", str(caught.exception))

    def test_a_failure_AFTER_the_stop_starts_the_server_again(self):
        """A failed restart must still leave a server running."""
        def fails(root, port):
            self.order.append("adopt")
            raise PermissionError(13, "Permission denied")
        cli.adopt_machine_store = fails
        cli.server_up = lambda server: False
        with self.assertRaises(SystemExit) as caught:
            self.restart()
        self.assertEqual(self.order, ["build", "check", "stop", "adopt",
                                      "launch"])
        self.assertIn("started again", str(caught.exception))

    def test_if_starting_again_ALSO_fails_it_says_nothing_is_serving(self):
        def fails(root, port):
            raise PermissionError(13, "Permission denied")
        cli.adopt_machine_store = fails
        cli.server_up = lambda server: False

        def launch(server, env):
            raise SystemExit("server never came up")
        cli.launch_server = launch
        with self.assertRaises(SystemExit) as caught:
            self.restart()
        self.assertIn("Nothing is serving", str(caught.exception))
        self.assertIn("--host=::1", str(caught.exception))

    def test_a_server_that_will_not_stop_changes_nothing_else(self):
        cli.stop_listener = lambda pid, server: self.order.append("stop") or False
        with self.assertRaises(SystemExit) as caught:
            self.restart()
        self.assertEqual(self.order, ["build", "check", "stop"])
        self.assertIn("did not stop", str(caught.exception))

    def test_an_answering_server_with_no_local_process_is_not_guessed_at(self):
        cli.listener_pid = lambda server: None
        with self.assertRaises(SystemExit) as caught:
            self.restart()
        self.assertEqual(self.order, ["build", "check"])
        self.assertIn("no local process", str(caught.exception))

    def test_with_NOTHING_running_it_simply_starts_one(self):
        cli.listener_pid = lambda server: None
        cli.server_up = lambda server: False
        self.restart()
        self.assertEqual(self.order, ["build", "check", "adopt", "migrate",
                                      "launch"])


class StoreSafetyTest(unittest.TestCase):
    """#43's other half: the store step itself, and the probe during a stop."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        saved = os.environ.get(cli.STORE_ENV)
        self.addCleanup(lambda: os.environ.__setitem__(cli.STORE_ENV, saved)
                        if saved is not None
                        else os.environ.pop(cli.STORE_ENV, None))
        # A directory nobody may write into, like a root-owned ~/.local.
        self.locked = os.path.join(self.tmp.name, "locked")
        os.makedirs(self.locked)
        os.chmod(self.locked, 0o500)
        self.addCleanup(os.chmod, self.locked, 0o700)

    def test_an_unwritable_store_parent_is_refused_and_names_the_way_out(self):
        os.environ[cli.STORE_ENV] = os.path.join(self.locked, "share", "x")
        with self.assertRaises(SystemExit) as caught:
            cli.check_machine_store(7717)
        self.assertIn("LLM_CHAT_STORE", str(caught.exception))
        self.assertIn("Nothing was stopped", str(caught.exception))

    def test_a_writable_store_parent_is_made_and_passes(self):
        os.environ[cli.STORE_ENV] = os.path.join(self.tmp.name, "ok")
        store = cli.check_machine_store(7717)
        self.assertTrue(os.path.isdir(os.path.dirname(store)))

    def test_store_creatable_is_READ_ONLY_and_right_both_ways(self):
        """doctor uses it, and doctor writes nothing."""
        good = os.path.join(self.tmp.name, "not", "yet", "there")
        self.assertTrue(cli.store_creatable(good))
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "not")))
        self.assertFalse(cli.store_creatable(
            os.path.join(self.locked, "share", "llm_chat", "port-7717")))

    def test_a_failed_link_PUTS_THE_DATA_BACK(self):
        """Otherwise a relaunch would serve an empty directory zonai made."""
        os.environ[cli.STORE_ENV] = os.path.join(self.tmp.name, "machine")
        root = os.path.join(self.tmp.name, "copy")
        data = os.path.join(root, ".zonai", "data")
        os.makedirs(data)
        with open(os.path.join(data, "zonai.sqlite"), "w") as f:
            f.write("the rooms")
        real = cli.os.symlink

        def refuse(*a, **kw):
            raise PermissionError(13, "Permission denied")
        cli.os.symlink = refuse
        try:
            with self.assertRaises(PermissionError):
                cli.adopt_machine_store(root, 7717)
        finally:
            cli.os.symlink = real
        with open(os.path.join(data, "zonai.sqlite")) as f:
            self.assertEqual(f.read(), "the rooms")

    def test_a_connection_DROPPED_by_a_server_going_away_reads_as_down(self):
        """#43's second failure: ConnectionResetError during the stop."""
        real = cli.call

        def reset(*a, **kw):
            raise ConnectionResetError(54, "Connection reset by peer")
        cli.call = reset
        try:
            self.assertFalse(cli.server_up("http://localhost:7717"))
        finally:
            cli.call = real


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
