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

    def stub_workers(self, missing=()):
        """`missing_workers` reads the REAL filesystem, so stub it too.

        WHY THIS EXISTS AT ALL: `start_server` shells out (stubbed here) and
        then asks whether the compile actually produced `.zonai/executables/*`
        — which are gitignored build artifacts. In a tree where somebody has
        compiled, they are there and the check passes silently. In a COLD
        CLONE they are not, so this raised the schema error before the test
        ever reached the behaviour it was about: one test errored and one
        failed on the wrong exception.

        Found by running the suite from a fresh clone after flutter-device
        posted the practice to #learnings, and it is the whole point of doing
        so — the suite was green on this machine and had only ever run on
        this machine. A stub that covers `subprocess` but not the filesystem
        read two lines later leaves the test depending on the developer's
        working tree.
        """
        real = cli.missing_workers
        cli.missing_workers = lambda: list(missing)
        self.addCleanup(lambda: setattr(cli, "missing_workers", real))

    def stub_subprocess(self, returncode=0):
        self.stub_workers()
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

    def test_A_COMPILE_THAT_BUILT_NOTHING_IS_REFUSED(self):
        """The belt to compile_failed's braces: "did it produce the files?"
        cannot be answered wrong by a change in zonai's wording.

        WRITTEN BECAUSE STUBBING IT WOULD OTHERWISE HAVE REMOVED ITS ONLY
        COVERAGE. Nothing asserted this path — it fired only by accident, in a
        tree where the gitignored build artifacts were absent, which is to say
        in a cold clone and never on the machine that runs the suite. Now that
        `missing_workers` is stubbed so the other tests stop depending on the
        developer's working tree, the behaviour needs a test that means it."""
        self.stub_subprocess()
        self.stub_workers(["db_rules", "db_config"])
        cli.server_up = lambda *a, **kw: True
        with self.assertRaises(SystemExit) as caught:
            with redirect_stdout(io.StringIO()):
                cli.start_server("http://localhost:7717")
        message = str(caught.exception)
        self.assertIn("db_rules", message)
        self.assertIn("did not produce", message)

    def test_the_refusal_says_what_a_server_started_anyway_would_DO(self):
        """"Missing workers" is a fact about a directory. "Every /db request
        500s" is what makes somebody stop and fix it rather than retry."""
        self.stub_subprocess()
        self.stub_workers(["db_rules"])
        cli.server_up = lambda *a, **kw: True
        with self.assertRaises(SystemExit) as caught:
            with redirect_stdout(io.StringIO()):
                cli.start_server("http://localhost:7717")
        self.assertIn("500s", str(caught.exception))


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
        # FOUND rather than assumed to be first. `do_reload` now asks the host
        # how many live sessions are in this project before it touches the UI,
        # so indexing call zero pinned an ordering the test never meant to
        # assert and broke on a guard being added in front of it.
        scripts = [c[0][0] for c in recorder.calls if "osascript" in c[0][0][0]]
        self.assertEqual(len(scripts), 1, "expected exactly one osascript run")
        self.assertIn(os.path.basename(self.project), " ".join(scripts[0]))

    @unittest.skipUnless(sys.platform == "darwin", "the reload path is macOS-only")
    def test_IT_REFUSES_WHEN_A_WINDOW_HOLDS_MORE_THAN_ONE_SESSION(self):
        """The danger the human named. A reload takes the whole WINDOW, and
        the title guard identifies a window without seeing how many
        conversations are inside it. One session per repository is the setup
        here, which is exactly why this would go unnoticed until somebody
        opens a second panel and a reload ends a turn they never asked
        about."""
        self.stub()
        real = cli.live_here
        cli.live_here = lambda project=None: [
            {"sessionId": "aaaaaaaa", "name": "one"},
            {"sessionId": "bbbbbbbb", "name": "two"}]
        self.addCleanup(lambda: setattr(cli, "live_here", real))
        with self.assertRaises(SystemExit) as caught:
            with redirect_stdout(io.StringIO()):
                cli.do_reload(force=True)
        said = str(caught.exception)
        self.assertIn("2 live sessions", said)
        self.assertIn("whole WINDOW", said)
        self.assertIn("aaaaaaaa", said)

    @unittest.skipUnless(sys.platform == "darwin", "the reload path is macOS-only")
    def test_ONE_session_is_the_ordinary_case_and_proceeds(self):
        """Paired. Refusing on ambiguity must not become refusing always."""
        recorder = self.stub()
        real = cli.live_here
        cli.live_here = lambda project=None: [{"sessionId": "aaaaaaaa"}]
        self.addCleanup(lambda: setattr(cli, "live_here", real))
        with redirect_stdout(io.StringIO()):
            cli.do_reload(force=True)
        self.assertTrue([c for c in recorder.calls
                         if "osascript" in c[0][0][0]])

    @unittest.skipUnless(sys.platform == "darwin", "the reload path is macOS-only")
    def test_a_host_that_CANNOT_BE_ASKED_does_not_block_a_reload(self):
        """None means could-not-ask. Refusing on it would make the whole verb
        unusable wherever `claude agents` is unavailable, to prevent a case
        nobody has evidence of."""
        recorder = self.stub()
        real = cli.live_here
        cli.live_here = lambda project=None: None
        self.addCleanup(lambda: setattr(cli, "live_here", real))
        with redirect_stdout(io.StringIO()):
            cli.do_reload(force=True)
        self.assertTrue([c for c in recorder.calls
                         if "osascript" in c[0][0][0]])

    def test_AUTO_RELOAD_IS_OFF_UNTIL_TURNED_ON(self):
        """The human's rule, and the right default: this is UI automation
        that ends whatever turn is in flight."""
        self.assertFalse(cli.auto_reload_allowed(self.project))
        with redirect_stdout(io.StringIO()):
            cli.do_auto_reload("on")
        self.assertTrue(cli.auto_reload_allowed(self.project))
        with redirect_stdout(io.StringIO()):
            cli.do_auto_reload("off")
        self.assertFalse(cli.auto_reload_allowed(self.project))

    def test_the_switch_is_reachable_through_the_real_parser(self):
        """`--auto` must short-circuit before `do_reload` runs, or turning the
        opt-in on would itself reload the window."""
        seen = []
        real = cli.do_reload
        cli.do_reload = lambda *a, **kw: seen.append(a)
        argv = sys.argv
        sys.argv = ["llm_chat", "reload", "--auto", "on"]
        try:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(), 0)
        finally:
            sys.argv = argv
            cli.do_reload = real
        self.assertEqual(seen, [], "turning the switch on reloaded the window")
        self.assertTrue(cli.auto_reload_allowed(self.project))

    def test_turning_it_off_when_it_was_never_on_is_not_an_error(self):
        with redirect_stdout(io.StringIO()) as out:
            cli.do_auto_reload("off")
        self.assertIn("OFF", out.getvalue())

    @unittest.skipUnless(sys.platform == "darwin",
                         "the reload path is macOS-only")
    def test_THE_REFUSAL_OFFERS_THE_FREE_OPTION_FIRST(self):
        """Issue #17. The refusal presented a binary — reload by hand, or
        `--i-know` — and both cost every session in the window, two of which
        had been running for days. The cheap answer was missing from the one
        place somebody is standing when they need it: hooks are read at
        session start, so a NEW conversation in the same window comes up
        rewired while the long-running siblings keep their context.

        `install.sh` already says exactly this on the way out. The gap was
        never knowledge; it was that the sentence lived somewhere else."""
        self.stub()
        real = cli.live_here
        cli.live_here = lambda project=None: [
            {"sessionId": "aaaaaaaa", "name": "one"},
            {"sessionId": "bbbbbbbb", "name": "two"}]
        self.addCleanup(lambda: setattr(cli, "live_here", real))
        with self.assertRaises(SystemExit) as caught:
            with redirect_stdout(io.StringIO()):
                cli.do_reload(force=True)
        said = str(caught.exception)
        self.assertIn("NEW\n  conversation", said)
        self.assertIn("keeps its context", said)
        # And the destructive options must not come first.
        self.assertLess(said.index("NEW\n  conversation"),
                        said.index("--i-know"))

    def test_TURNING_AUTO_ON_SAYS_WHEN_IT_CAN_NEVER_FIRE(self):
        """The sharper half of #17. Auto-reload declines whenever the project
        holds more than one live session, so on a machine running several
        conversations per repo it can only fire in the configuration where a
        manual reload was already cheap — silently, forever.

        Same defect as the two triggers that read CONFIGURED BUT NEVER FIRED
        for four days: a mechanism that cannot operate looks exactly like one
        that is fine. Making it say so is the fix."""
        real = cli.live_here
        cli.live_here = lambda project=None: [
            {"sessionId": "a", "name": "one"}, {"sessionId": "b", "name": "2"}]
        self.addCleanup(lambda: setattr(cli, "live_here", real))
        with redirect_stdout(io.StringIO()) as out:
            cli.do_auto_reload("on")
        said = out.getvalue()
        self.assertIn("never fire", said)
        self.assertTrue(cli.auto_reload_allowed(self.project),
                        "it warns, but still records the choice")

    def test_a_SINGLE_session_gets_no_such_warning(self):
        """Paired. A warning printed always is one nobody reads."""
        real = cli.live_here
        cli.live_here = lambda project=None: [{"sessionId": "a"}]
        self.addCleanup(lambda: setattr(cli, "live_here", real))
        with redirect_stdout(io.StringIO()) as out:
            cli.do_auto_reload("on")
        self.assertNotIn("never fire", out.getvalue())

    def test_the_switch_says_what_it_will_and_will_not_do(self):
        with redirect_stdout(io.StringIO()) as out:
            cli.do_auto_reload("on")
        said = out.getvalue()
        self.assertIn("does not land", said)
        self.assertIn("more than one live session", said)

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


class CompileVerdictTest(unittest.TestCase):
    """`zonai compile` reports failure in its TEXT and success in its CODE.

    The same shape this file opens with, met for real while upgrading to zonai
    0.6.2: compile printed "Failed to compile rules:" and three analyzer
    errors, then exited 0. Every step in start_server is judged by its return
    code, so the bootstrap walked past it and started a server with no rules
    worker — which 500s every /db request. The server accepts connections and
    fails all of them, so it reads as a wire problem rather than a build one.
    """

    def test_a_compile_that_says_it_failed_IS_a_failure(self):
        self.assertTrue(cli.compile_failed(
            ["./zonai", "compile"],
            "Analyzing rules...\nFailed to compile rules:\n  error - x"))

    def test_an_AOT_failure_counts_too(self):
        self.assertTrue(cli.compile_failed(
            ["./zonai", "compile"], "Error: AOT compilation failed"))

    def test_a_clean_compile_is_not_a_failure(self):
        self.assertFalse(cli.compile_failed(
            ["./zonai", "compile"], "Compiled 6 workers"))

    def test_only_the_COMPILE_step_is_read_this_way(self):
        """Scanning every step for the word "failed" would refuse on a log
        line or a test name that merely contains it. `pub get` printing
        somebody else's "failed" is not this project's build breaking."""
        self.assertFalse(cli.compile_failed(
            ["dart", "pub", "get"], "Failed to compile rules:"))


class MissingWorkersTest(unittest.TestCase):
    """The check a change of wording cannot defeat.

    compile_failed reads a message; this asks whether the files exist. Keeping
    both is deliberate — the first names the cause in the error, the second
    still fires if zonai stops printing that sentence.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved_root = cli.ROOT
        cli.ROOT = self.tmp.name
        self.built = os.path.join(self.tmp.name, ".zonai", "executables")
        os.makedirs(self.built)

    def tearDown(self):
        cli.ROOT = self.saved_root
        self.tmp.cleanup()

    def build(self, *names):
        for name in names:
            with open(os.path.join(self.built, name + ".exe"), "w") as f:
                f.write("x")

    def test_every_worker_present_reports_nothing(self):
        self.build(*cli.WORKERS)
        self.assertEqual(cli.missing_workers(), [])

    def test_THE_RULES_WORKER_MISSING_IS_REPORTED(self):
        """The exact file that was absent, and the one whose absence 500s
        every request rather than failing the boot."""
        self.build(*[w for w in cli.WORKERS if w != "db_rules"])
        self.assertEqual(cli.missing_workers(), ["db_rules"])

    def test_a_directory_with_no_workers_reports_all_of_them(self):
        self.assertEqual(cli.missing_workers(), list(cli.WORKERS))


if __name__ == "__main__":
    unittest.main()
