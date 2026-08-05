"""Setup, reload and dispatch — asserting which branch ran, not just that it did.

That distinction is borrowed from a bug in the other project found the same
day: a piped installer silently resolved its source to the current directory,
found a payload lying there, skipped the fetch and installed the WRONG BYTES
with a clean exit. Every test of it had asked whether the command worked. None
had asked which branch it took, so the arm that succeeds and lies stayed hidden
while the arm that refuses loudly got fixed.

So the setup tests below assert that `start_server` was NOT called when a server
was already up, and WAS when it was not — the observable that separates working
from working-for-the-wrong-reason.
"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import FakeServer, load  # noqa: E402

cli = load("llm_chat")


class Recorder:
    """Records that it was called, and what with."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def __call__(self, *a, **kw):
        self.calls.append((a, kw))
        return self.result

    @property
    def called(self):
        return bool(self.calls)


class SetupTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeServer()
        self.saved = {"call": cli.call, "start_server": cli.start_server,
                      "install_hook": cli.install_hook,
                      "server_up": cli.server_up}
        cli.call = self.fake.call
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name
        os.environ["CLAUDE_PROJECT_DIR"] = self.project
        cli.install_hook = Recorder("added llm_chat hooks")
        cli.start_server = Recorder()

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(cli, name, value)
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.tmp.cleanup()

    def setup(self, channel="room", identity="me", **kw):
        out = io.StringIO()
        with redirect_stdout(out):
            cli.do_setup("http://127.0.0.1:1", channel, identity, kw.pop("topic", None),
                         kw.pop("max_messages", 200), **kw)
        return out.getvalue()

    def test_an_already_running_server_is_not_restarted(self):
        """The branch that matters: 'it worked' is true either way, and
        starting a second server would be a silent, wrong success."""
        cli.server_up = Recorder(True)
        self.setup()
        self.assertFalse(cli.start_server.called,
                         "must reuse a live server, not start another")

    def test_a_missing_server_is_started(self):
        cli.server_up = Recorder(False)
        self.setup()
        self.assertTrue(cli.start_server.called)

    def test_the_hook_is_registered_in_the_calling_repo(self):
        cli.server_up = Recorder(True)
        self.setup()
        (args, _), = cli.install_hook.calls
        self.assertEqual(os.path.realpath(args[0]),
                         os.path.realpath(self.project),
                         "the hook belongs to the caller's repo, not llm_chat's")

    def test_it_joins_and_remembers(self):
        cli.server_up = Recorder(True)
        self.setup(channel="room", identity="me")
        self.assertIsNotNone(self.fake.get_membership("room", "me"))
        self.assertEqual(cli.read_joined()["room"]["identity"], "me")

    def test_it_prints_what_to_do_next(self):
        cli.server_up = Recorder(True)
        text = self.setup()
        for verb in ("say", "read", "leave"):
            self.assertIn(verb + " room", text)
        self.assertIn("NEW session", text,
                      "must not promise the current session picks the hook up")

    def test_running_it_inside_the_checkout_is_refused(self):
        """Identity is per calling project. Two agents set up here would share
        one identity and receive each other's messages.

        ROOT is redirected at a throwaway directory rather than pointed at the
        real checkout: an earlier version of this test set CLAUDE_PROJECT_DIR to
        the actual repo, and its sibling below then wrote a junk room into this
        project's own .llm_chat/joined.json — which the live hooks would have
        polled on every tool call. A suite must not touch what it tests.
        """
        cli.server_up = Recorder(True)
        real_root = cli.ROOT
        cli.ROOT = self.project
        try:
            with self.assertRaises(SystemExit) as caught:
                self.setup()
        finally:
            cli.ROOT = real_root
        self.assertIn("--in-checkout", str(caught.exception))

    def test_the_maintainer_may_opt_in_explicitly(self):
        cli.server_up = Recorder(True)
        real_root = cli.ROOT
        cli.ROOT = self.project
        try:
            self.setup(in_checkout=True)
        finally:
            cli.ROOT = real_root
        self.assertIsNotNone(self.fake.get_membership("room", "me"))

    def test_bad_names_are_refused_before_anything_is_touched(self):
        cli.server_up = Recorder(True)
        with self.assertRaises(SystemExit):
            self.setup(channel="has space")
        self.assertFalse(cli.install_hook.called,
                         "nothing should be written for a request that cannot work")


class StartServerTest(unittest.TestCase):
    def setUp(self):
        self.saved = {"subprocess": cli.subprocess, "server_up": cli.server_up,
                      "time": cli.time}
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(cli, name, value)
        self.tmp.cleanup()

    def stub_subprocess(self, returncode=0):
        recorder = Recorder()

        class Fake:
            Popen = Recorder()
            DEVNULL = -3

            @staticmethod
            def run(argv, **kw):
                recorder((argv, kw))

                class Result:
                    pass
                Result.returncode = returncode
                Result.stdout = "out"
                Result.stderr = "err"
                return Result
        cli.subprocess = Fake
        return recorder, Fake

    def test_a_failing_build_step_stops_with_its_output(self):
        recorder, _ = self.stub_subprocess(returncode=1)
        with self.assertRaises(SystemExit) as caught:
            with redirect_stdout(io.StringIO()):
                cli.start_server("http://localhost:7717")
        self.assertIn("setup failed", str(caught.exception))

    def test_it_gives_up_if_the_server_never_answers(self):
        self.stub_subprocess()
        cli.server_up = lambda *a, **kw: False

        class NoSleep:
            @staticmethod
            def sleep(_):
                return None
        cli.time = NoSleep
        with self.assertRaises(SystemExit) as caught:
            with redirect_stdout(io.StringIO()):
                cli.start_server("http://localhost:7717")
        self.assertIn("never came up", str(caught.exception))

    def test_it_returns_once_the_server_answers(self):
        self.stub_subprocess()
        cli.server_up = lambda *a, **kw: True
        with redirect_stdout(io.StringIO()):
            cli.start_server("http://localhost:7717")


class InstallHookTest(unittest.TestCase):
    def setUp(self):
        self.saved = cli.subprocess

    def tearDown(self):
        cli.subprocess = self.saved

    def test_a_failing_installer_is_reported_not_swallowed(self):
        class Fake:
            @staticmethod
            def run(argv, **kw):
                class Result:
                    returncode = 1
                    stdout = ""
                    stderr = "no such directory"
                return Result
        cli.subprocess = Fake
        with self.assertRaises(SystemExit) as caught:
            cli.install_hook("/tmp/x")
        self.assertIn("could not register", str(caught.exception))

    def test_the_first_line_of_output_is_the_summary(self):
        class Fake:
            @staticmethod
            def run(argv, **kw):
                class Result:
                    returncode = 0
                    stdout = "added llm_chat hooks\nstamped install (abc)\n"
                    stderr = ""
                return Result
        cli.subprocess = Fake
        self.assertEqual(cli.install_hook("/tmp/x"), "added llm_chat hooks")

    def test_silent_success_still_reports_something(self):
        class Fake:
            @staticmethod
            def run(argv, **kw):
                class Result:
                    returncode = 0
                    stdout = "\n\n"
                    stderr = ""
                return Result
        cli.subprocess = Fake
        self.assertEqual(cli.install_hook("/tmp/x"), "registered")


class ReloadTest(unittest.TestCase):
    """The one path that reaches osascript. Stubbed — a test suite must never
    reload the window it is running in."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name
        os.environ["CLAUDE_PROJECT_DIR"] = self.project
        self.saved_entry = os.environ.get("CLAUDE_CODE_ENTRYPOINT")
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "claude-vscode"
        self.saved = {"subprocess": cli.subprocess, "platform": sys.platform}
        d = os.path.join(self.project, ".claude")
        os.makedirs(d)
        with open(os.path.join(d, "settings.local.json"), "w") as f:
            f.write('{"hooks": {"SessionStart": [{"hooks": [{"type": "command",'
                    ' "command": "/x/bin/llm-chat-wake"}]}]}}')
        probe = os.path.join(self.project, ".llm_chat", "probe")
        os.makedirs(probe)
        with open(os.path.join(probe, "wake-SessionStart"), "w") as f:
            f.write("1 2")

    def tearDown(self):
        cli.subprocess = self.saved["subprocess"]
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        if self.saved_entry is None:
            os.environ.pop("CLAUDE_CODE_ENTRYPOINT", None)
        else:
            os.environ["CLAUDE_CODE_ENTRYPOINT"] = self.saved_entry
        self.tmp.cleanup()

    def stub(self, returncode=0, stderr=""):
        recorder = Recorder()

        class Fake:
            @staticmethod
            def run(argv, **kw):
                recorder(argv)

                class Result:
                    pass
                Result.returncode = returncode
                Result.stdout = ""
                Result.stderr = stderr
                return Result
        cli.subprocess = Fake
        return recorder

    @unittest.skipUnless(sys.platform == "darwin", "the reload path is macOS-only")
    def test_it_targets_this_window_by_title_not_whatever_is_frontmost(self):
        """On a machine running one window per agent, driving the frontmost is
        a coin flip that can reload a colleague's session mid-task."""
        recorder = self.stub()
        with redirect_stdout(io.StringIO()):
            cli.do_reload(force=True)
        script = recorder.calls[0][0][0]
        self.assertIn("osascript", script[0])
        self.assertIn(os.path.basename(self.project), " ".join(script))

    @unittest.skipUnless(sys.platform == "darwin", "the reload path is macOS-only")
    def test_a_failed_reload_says_what_to_do_instead(self):
        self.stub(returncode=1, stderr="not authorised")
        with self.assertRaises(SystemExit) as caught:
            with redirect_stdout(io.StringIO()):
                cli.do_reload(force=True)
        self.assertIn("Accessibility", str(caught.exception))

    def test_the_override_skips_the_never_fired_guard(self):
        os.remove(os.path.join(self.project, ".llm_chat", "probe",
                               "wake-SessionStart"))
        recorder = self.stub()
        if sys.platform != "darwin":
            with self.assertRaises(SystemExit):
                cli.do_reload(force=True, i_know=True)
        else:
            with redirect_stdout(io.StringIO()):
                cli.do_reload(force=True, i_know=True)
            self.assertTrue(recorder.called)


class DispatchTest(unittest.TestCase):
    """Every subcommand reaches its handler with the arguments it was given."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CLAUDE_PROJECT_DIR"] = self.tmp.name
        self.saved = {n: getattr(cli, n) for n in
                      ("do_setup", "do_join", "do_say", "do_read", "do_leave",
                       "do_channels", "do_reopen", "do_doctor", "do_reload",
                       "get_channel", "remember", "identity_for")}
        for name in self.saved:
            setattr(cli, name, Recorder())
        # do_join returns the channel row; a stub returning None would not
        # match that contract, and the dispatch reads `broadcast` off it.
        cli.do_join = Recorder({"name": "room", "broadcast": 0})
        cli.identity_for = lambda channel, explicit: explicit or "remembered"
        cli.get_channel = lambda server, name: {"topic": "t"}
        self.argv = sys.argv

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(cli, name, value)
        sys.argv = self.argv
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.tmp.cleanup()

    def run_cli(self, *args):
        sys.argv = ["llm_chat"] + list(args)
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main()
        return code, out.getvalue()

    def test_setup_passes_the_in_checkout_flag_through(self):
        self.run_cli("setup", "room", "--as", "me", "--in-checkout")
        (args, _), = cli.do_setup.calls
        self.assertEqual(args[1], "room")
        self.assertEqual(args[2], "me")
        self.assertTrue(args[5])

    def test_open_announces_the_invite_and_join_does_not(self):
        """`open` prints the block a human pastes into the other agent; `join`
        is the same operation without the ceremony."""
        self.run_cli("open", "room", "--as", "me")
        self.assertTrue(cli.do_join.calls[0][1]["announce"])
        cli.do_join.calls.clear()
        self.run_cli("join", "room", "--as", "me")
        self.assertFalse(cli.do_join.calls[0][1]["announce"])

    def test_say_read_leave_fall_back_to_the_remembered_identity(self):
        for command, handler in (("say", cli.do_say), ("read", cli.do_read),
                                 ("leave", cli.do_leave)):
            handler.calls.clear()
            extra = ["hello"] if command == "say" else []
            self.run_cli(command, "room", *extra)
            self.assertIn("remembered", handler.calls[0][0])

    def test_read_forwards_peek_and_all(self):
        self.run_cli("read", "room", "--peek", "--all")
        args, _ = cli.do_read.calls[0]
        self.assertEqual(args[3:5], (True, True), "peek and --all, in that order")

    def test_read_defaults_to_neither(self):
        self.run_cli("read", "room")
        args, _ = cli.do_read.calls[0]
        self.assertEqual(args[3:5], (False, False))

    def test_reopen_forwards_a_new_cap(self):
        self.run_cli("reopen", "room", "--max-messages", "50")
        self.assertIn(50, cli.do_reopen.calls[0][0])

    def test_channels_doctor_and_fingerprint_need_no_arguments(self):
        for command in ("channels", "doctor", "fingerprint"):
            code, _ = self.run_cli(command)
            self.assertEqual(code, 0)

    def test_invite_reprints_the_block_for_an_existing_room(self):
        _, text = self.run_cli("invite", "room")
        self.assertIn("invited", text)

    def test_invite_refuses_a_room_that_does_not_exist(self):
        cli.get_channel = lambda server, name: None
        with self.assertRaises(SystemExit):
            self.run_cli("invite", "ghost")

    def test_the_server_can_be_overridden(self):
        self.run_cli("--server", "http://elsewhere:9", "channels")
        self.assertEqual(cli.do_channels.calls[0][0][0], "http://elsewhere:9")


if __name__ == "__main__":
    unittest.main()
