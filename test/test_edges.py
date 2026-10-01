"""The branches that only run when something has already gone wrong.

Error paths are worth defending precisely because nobody exercises them by
hand: a hook that raises instead of staying quiet breaks the session it was
supposed to serve, and it does so in the middle of somebody's refactor.
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import FakeServer, load, write_settings  # noqa: E402

cli = load("llm_chat")

UNWRITABLE = "/dev/null/not-a-directory"


class CliEdgeTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeServer()
        self.real_call = cli.call
        cli.call = self.fake.call
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CLAUDE_PROJECT_DIR"] = self.tmp.name

    def tearDown(self):
        cli.call = self.real_call
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.tmp.cleanup()

    def quiet(self, fn, *a, **kw):
        out = io.StringIO()
        with redirect_stdout(out):
            fn(*a, **kw)
        return out.getvalue()

    def test_joining_fills_in_a_topic_the_room_never_had(self):
        """Whoever names it first wins; a later joiner supplying one is not
        overwriting anything."""
        self.fake.channel("room", topic=None)
        self.quiet(cli.do_join, "http://127.0.0.1:1", "room", "me", "now it has one", 200, False)
        self.assertEqual(self.fake.get_channel("room")["topic"], "now it has one")

    def test_a_topic_already_set_is_not_overwritten(self):
        self.fake.channel("room", topic="the original")
        self.quiet(cli.do_join, "http://127.0.0.1:1", "room", "me", "a later idea", 200, False)
        self.assertEqual(self.fake.get_channel("room")["topic"], "the original")

    def test_open_prints_the_invite_block(self):
        text = self.quiet(cli.do_join, "http://127.0.0.1:1", "room", "me", "a topic", 200, True)
        self.assertIn("invited", text)
        self.assertIn("-" * 70, text)

    def test_saying_into_a_room_that_does_not_exist_is_refused(self):
        with self.assertRaises(SystemExit) as caught:
            cli.do_say("http://127.0.0.1:1", "ghost", "me", "anyone there?")
        self.assertIn("no such channel", str(caught.exception))

    def test_leaving_a_room_you_never_joined_is_refused(self):
        self.fake.channel("room")
        with self.assertRaises(SystemExit) as caught:
            cli.do_leave("http://127.0.0.1:1", "room", "stranger")
        self.assertIn("has not joined", str(caught.exception))

    def test_reopen_warns_when_every_member_is_still_done(self):
        """The next `leave` by anyone would satisfy 'all members are done' and
        close it straight back."""
        self.fake.channel("room", closed=1, closed_reason="every member is done")
        self.fake.membership("room", "me", done=1)
        self.fake.membership("room", "other", done=1)
        text = self.quiet(cli.do_reopen, "http://127.0.0.1:1", "room", None)
        self.assertIn("still marked done", text)
        self.assertIn("rejoin", text)

    def test_a_fingerprint_survives_a_missing_hook_script(self):
        """It has to answer even for a broken checkout, or the drift check
        becomes the thing that breaks."""
        real = cli.EXPECTED_HOOKS
        cli.EXPECTED_HOOKS = ("llm-chat-deliver", "not-a-real-script")
        try:
            self.assertTrue(cli.wiring_fingerprint())
        finally:
            cli.EXPECTED_HOOKS = real

    def test_the_integrated_terminal_is_distinguished_from_the_extension(self):
        """Its remedy is a RESTART, not a reload — the wrong advice sends
        someone to an action that cannot help."""
        saved = os.environ.get("CLAUDE_CODE_ENTRYPOINT")
        os.environ.pop("CLAUDE_CODE_ENTRYPOINT", None)
        os.environ["TERM_PROGRAM"] = "vscode"
        try:
            self.assertEqual(cli.host(), "vscode-terminal")
        finally:
            os.environ.pop("TERM_PROGRAM", None)
            if saved is not None:
                os.environ["CLAUDE_CODE_ENTRYPOINT"] = saved

    def test_reload_is_refused_off_macos(self):
        saved_platform = sys.platform
        saved_entry = os.environ.get("CLAUDE_CODE_ENTRYPOINT")
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "claude-vscode"
        project = self.tmp.name
        write_settings(project, SessionStart=["/x/bin/llm-chat-wake"])
        probe = os.path.join(project, ".llm_chat", "probe")
        os.makedirs(probe, exist_ok=True)
        with open(os.path.join(probe, "wake-SessionStart"), "w") as f:
            f.write("1 2")
        sys.platform = "linux"
        try:
            with self.assertRaises(SystemExit) as caught:
                cli.do_reload(force=True)
            self.assertIn("macOS-only", str(caught.exception))
        finally:
            sys.platform = saved_platform
            if saved_entry is None:
                os.environ.pop("CLAUDE_CODE_ENTRYPOINT", None)
            else:
                os.environ["CLAUDE_CODE_ENTRYPOINT"] = saved_entry

    def test_a_lock_that_cannot_be_created_does_not_raise(self):
        os.environ["CLAUDE_PROJECT_DIR"] = UNWRITABLE
        with cli.read_lock() as held:
            self.assertFalse(held)

    def test_a_fresh_checkout_installs_dependencies_before_serving(self):
        """`.dart_tool/` is gitignored, so a clone has none of pub get's output
        and cannot compile anything."""
        # Stubbed for the same reason as its neighbour below: the SDK lookup
        # reads the real filesystem and runs before the steps this asserts on.
        saved = {"subprocess": cli.subprocess, "server_up": cli.server_up,
                 "ROOT": cli.ROOT, "matching_dart_sdk": cli.matching_dart_sdk}
        cli.matching_dart_sdk = lambda: "/fake/dart-sdk"
        steps = []

        root = self.tmp.name

        class Fake:
            Popen = staticmethod(lambda *a, **kw: None)
            DEVNULL = -3

            @staticmethod
            def run(argv, **kw):
                steps.append(argv)
                # A successful compile PRODUCES the workers. Returning 0 and
                # writing nothing is the exact failure start_server now
                # refuses, so a fake that only sets returncode would make this
                # test assert the opposite of what a real compile does.
                if "compile" in argv:
                    built = os.path.join(root, ".zonai", "executables")
                    os.makedirs(built, exist_ok=True)
                    for worker in cli.WORKERS:
                        with open(os.path.join(built, worker + ".exe"), "w") as f:
                            f.write("x")

                class Result:
                    returncode = 0
                    stdout = stderr = ""
                return Result
        cli.subprocess = Fake
        cli.server_up = lambda *a, **kw: True
        cli.ROOT = self.tmp.name          # has no .dart_tool
        try:
            with redirect_stdout(io.StringIO()):
                cli.start_server("http://localhost:7717")
        finally:
            for name, value in saved.items():
                setattr(cli, name, value)
        self.assertIn(["dart", "pub", "get"], steps)

    def test_a_compile_that_PRODUCES_NOTHING_refuses_to_start_a_server(self):
        """The partner, and the real bug: `zonai compile` exits 0 while
        printing that it failed. Without this the bootstrap starts a server
        with no rules worker, which accepts connections and 500s every single
        /db request — so it reads as a wire problem, not a build one."""
        # `matching_dart_sdk` IS SAVED AND STUBBED WITH THE REST, because it
        # reads the real filesystem — `~/.cache/zonai/fat`, `~/fvm` — and
        # `start_server` consults it BEFORE the steps this test is about. On a
        # developer machine that has the SDK it returns a path and this test
        # passes; on a cold CI runner it returns None and `start_server`
        # raises the SDK refusal, so the assertion below saw the wrong
        # exception and main went red. Third real-filesystem read to catch
        # this fixture family out, and the second to do it only on CI.
        saved = {"subprocess": cli.subprocess, "server_up": cli.server_up,
                 "ROOT": cli.ROOT, "matching_dart_sdk": cli.matching_dart_sdk}
        cli.matching_dart_sdk = lambda: "/fake/dart-sdk"

        class Fake:
            Popen = staticmethod(lambda *a, **kw: None)
            DEVNULL = -3

            @staticmethod
            def run(argv, **kw):
                class Result:
                    returncode = 0        # the lie
                    stdout = stderr = ""
                return Result
        cli.subprocess = Fake
        cli.server_up = lambda *a, **kw: True
        cli.ROOT = self.tmp.name          # nothing was built
        try:
            with redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    cli.start_server("http://localhost:7717")
        finally:
            for name, value in saved.items():
                setattr(cli, name, value)
        self.assertIn("db_rules", str(caught.exception))
        self.assertIn("500", str(caught.exception))


class MachineStoreTest(unittest.TestCase):
    """One store per address, whichever copy starts the server. On 09-28 a
    vendored copy started :7717 on its own empty store, and every agent
    already in a room heard nothing for three days."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        saved = os.environ.get(cli.STORE_ENV)
        os.environ[cli.STORE_ENV] = os.path.join(self.tmp.name, "machine")
        self.addCleanup(lambda: os.environ.__setitem__(cli.STORE_ENV, saved)
                        if saved is not None
                        else os.environ.pop(cli.STORE_ENV, None))
        self.store = cli.machine_store(7717)

    def copy(self, name, rows=None):
        root = os.path.join(self.tmp.name, name)
        data = os.path.join(root, ".zonai", "data")
        os.makedirs(data)
        if rows is not None:
            with open(os.path.join(data, "zonai.sqlite"), "w") as f:
                f.write(rows)
        return root

    def data(self, root):
        return os.path.join(root, ".zonai", "data")

    def test_the_FIRST_copy_s_store_BECOMES_the_machine_store(self):
        root = self.copy("checkout", rows="the real rooms")
        cli.adopt_machine_store(root, 7717)
        self.assertTrue(os.path.islink(self.data(root)))
        with open(os.path.join(self.store, "zonai.sqlite")) as f:
            self.assertEqual(f.read(), "the real rooms")

    def test_a_SECOND_copy_serves_the_same_store_not_its_own(self):
        """The 09-28 failure, exactly: a vendored copy with its own (empty)
        store starts the server."""
        first = self.copy("checkout", rows="the real rooms")
        cli.adopt_machine_store(first, 7717)
        vendored = self.copy("vendored", rows="empty bootstrap")
        notes = cli.adopt_machine_store(vendored, 7717)
        with open(os.path.join(self.data(vendored), "zonai.sqlite")) as f:
            self.assertEqual(f.read(), "the real rooms")
        self.assertIn("set aside", notes[0])

    def test_a_copy_s_own_store_is_SET_ASIDE_never_deleted(self):
        cli.adopt_machine_store(self.copy("checkout", rows="real"), 7717)
        vendored = self.copy("vendored", rows="a fork somebody wrote to")
        cli.adopt_machine_store(vendored, 7717)
        aside = [n for n in os.listdir(os.path.join(vendored, ".zonai"))
                 if n.startswith("data.set-aside-")]
        self.assertEqual(len(aside), 1)
        with open(os.path.join(vendored, ".zonai", aside[0],
                               "zonai.sqlite")) as f:
            self.assertEqual(f.read(), "a fork somebody wrote to")

    def test_a_FRESH_clone_gets_a_link_to_a_new_store(self):
        root = os.path.join(self.tmp.name, "fresh")
        os.makedirs(root)
        cli.adopt_machine_store(root, 7717)
        self.assertEqual(os.path.realpath(self.data(root)),
                         os.path.realpath(self.store))

    def test_a_link_to_ANOTHER_store_is_re_pointed_not_followed(self):
        root = os.path.join(self.tmp.name, "moved")
        os.makedirs(os.path.join(root, ".zonai"))
        elsewhere = os.path.join(self.tmp.name, "old-store")
        os.makedirs(elsewhere)
        os.symlink(elsewhere, self.data(root))
        cli.adopt_machine_store(root, 7717)
        self.assertEqual(os.path.realpath(self.data(root)),
                         os.path.realpath(self.store))
        self.assertTrue(os.path.isdir(elsewhere))    # its target is untouched

    def test_already_linked_is_a_no_op(self):
        root = self.copy("checkout", rows="real")
        cli.adopt_machine_store(root, 7717)
        self.assertEqual(cli.adopt_machine_store(root, 7717), [])

    def test_a_store_a_LIVE_server_holds_is_never_moved(self):
        """The first run of this change set the live store aside, because a
        test left ROOT real. zonai's lock names the pid; a live one stops it."""
        root = self.copy("serving", rows="live rooms")
        with open(os.path.join(self.data(root), ".serve.lock"), "w") as f:
            f.write("%d\n" % os.getpid())
        with self.assertRaises(SystemExit) as caught:
            cli.adopt_machine_store(root, 7717)
        self.assertIn("held by a running server", str(caught.exception))
        self.assertFalse(os.path.islink(self.data(root)))
        self.assertFalse(os.path.exists(self.store))

    def test_a_STALE_lock_does_not_block(self):
        root = self.copy("crashed", rows="rooms")
        with open(os.path.join(self.data(root), ".serve.lock"), "w") as f:
            f.write("999999\n")
        cli.adopt_machine_store(root, 7717)
        self.assertTrue(os.path.islink(self.data(root)))

    def test_ANOTHER_PORT_is_another_workspace(self):
        """Clones run as separate workspaces on separate ports keep separate
        stores, as doctor has always described."""
        self.assertNotEqual(cli.machine_store(7717), cli.machine_store(7718))


class DeliverEdgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name
        os.environ["CLAUDE_PROJECT_DIR"] = self.project
        write_settings(self.project,
                       PostToolUse=["/x/bin/llm-chat-deliver"],
                       Stop=["/x/bin/llm-chat-wake"],
                       SessionStart=["/x/bin/llm-chat-wake"])
        self.mod = load("llm-chat-deliver")

    def tearDown(self):
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.tmp.cleanup()

    def run_hook(self, payload="{}"):
        out = io.StringIO()
        stdin = sys.stdin
        sys.stdin = io.StringIO(payload)
        try:
            with redirect_stdout(out):
                self.mod.main()
        finally:
            sys.stdin = stdin
        return out.getvalue()

    def test_an_unwritable_probe_directory_never_breaks_delivery(self):
        """Bookkeeping must never be the reason a message does not arrive."""
        self.mod.PROJECT = UNWRITABLE
        self.mod.mark_fired("post-tool-use")   # must not raise

    def test_unreadable_settings_are_skipped_not_fatal(self):
        d = os.path.join(self.project, ".claude")
        with open(os.path.join(d, "settings.json"), "w") as f:
            f.write("{not json")
        self.assertEqual(self.mod.missing_hooks(), [])

    def test_no_stamp_means_no_drift_claim(self):
        self.assertIsNone(self.mod.stale_install())

    def test_a_stamp_without_a_fingerprint_is_not_a_drift_claim(self):
        d = os.path.join(self.project, ".llm_chat")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "installed.json"), "w") as f:
            json.dump({"checkout": "/somewhere"}, f)
        self.assertIsNone(self.mod.stale_install())

    def test_an_unanswerable_fingerprint_is_not_reported_as_drift(self):
        """Better to say nothing than to claim drift on a failed lookup."""
        d = os.path.join(self.project, ".llm_chat")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "installed.json"), "w") as f:
            json.dump({"fingerprint": "abc"}, f)

        class Boom:
            @staticmethod
            def run(*a, **kw):
                raise OSError("cannot run")
        self.mod.subprocess = Boom
        self.assertIsNone(self.mod.stale_install())

    def test_a_notice_that_cannot_be_recorded_is_not_repeated_forever(self):
        """If the marker cannot be written the notice is skipped, because the
        alternative is emitting it on every single tool call."""
        write_settings(self.project, PostToolUse=["/x/bin/llm-chat-deliver"])
        self.mod.PROJECT = UNWRITABLE
        self.assertEqual(self.mod.upgrade_notice("s1"), "")

    def test_a_room_record_missing_its_identity_is_skipped(self):
        d = os.path.join(self.project, ".llm_chat")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "joined.json"), "w") as f:
            json.dump({"room": {"server": "http://127.0.0.1:1"}}, f)
        self.assertEqual(self.run_hook(), "")

    def test_blank_cli_output_is_not_a_delivery(self):
        d = os.path.join(self.project, ".llm_chat")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "joined.json"), "w") as f:
            json.dump({"room": {"identity": "me", "server": "http://127.0.0.1:1"}}, f)

        class Blank:
            @staticmethod
            def run(*a, **kw):
                class Result:
                    stdout = "\n  \n"
                    stderr = ""
                    returncode = 0
                return Result
        self.mod.subprocess = Blank
        self.assertEqual(self.run_hook(), "")


class WakeEdgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CLAUDE_PROJECT_DIR"] = self.tmp.name
        self.mod = load("llm-chat-wake")

    def tearDown(self):
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.tmp.cleanup()

    def test_a_failed_poll_is_silent_rather_than_fatal(self):
        """A chat outage must never break the session. The message stays queued
        and the next pass picks it up."""
        class Boom:
            @staticmethod
            def run(*a, **kw):
                raise OSError("server gone")
        self.mod.subprocess = Boom
        self.assertIsNone(self.mod.poll("room", {"identity": "me",
                                                 "server": "http://127.0.0.1:1"}))

    def test_an_unwritable_probe_directory_does_not_stop_the_waker(self):
        self.mod.STATE = UNWRITABLE
        stdin = sys.stdin
        sys.stdin = io.StringIO('{"hook_event_name": "Stop"}')
        try:
            self.assertEqual(self.mod.main(), 0)
        finally:
            sys.stdin = stdin

    def test_an_unparseable_payload_still_records_that_it_fired(self):
        """The event name is a nicety; the mark is the diagnosis."""
        stdin = sys.stdin
        sys.stdin = io.StringIO("not json at all")
        try:
            self.mod.main()
        finally:
            sys.stdin = stdin
        probe = os.path.join(self.tmp.name, ".llm_chat", "probe")
        self.assertIn("wake-unknown", os.listdir(probe))


if __name__ == "__main__":
    unittest.main()


class LastMileTest(unittest.TestCase):
    """The remaining branches: malformed shapes, partial state, and the one
    dispatch that reaches the window-reload path."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name
        os.environ["CLAUDE_PROJECT_DIR"] = self.project

    def tearDown(self):
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.tmp.cleanup()

    def write_odd_settings(self):
        """hooks present, but an event maps to something that is not a list.
        Hand-edited settings files are not guaranteed to match the schema, and
        a hook that crashes on one takes the session down with it."""
        d = os.path.join(self.project, ".claude")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "settings.local.json"), "w") as f:
            json.dump({"hooks": {"PostToolUse": "not a list",
                                 "Stop": {"also": "not a list"}}}, f)

    def test_the_cli_survives_a_malformed_hooks_shape(self):
        self.write_odd_settings()
        registered, _, _, _ = cli.hook_report(self.project)
        self.assertEqual(registered, set())

    def test_the_deliver_hook_survives_a_malformed_hooks_shape(self):
        self.write_odd_settings()
        mod = load("llm-chat-deliver")
        self.assertEqual(sorted(mod.missing_hooks()),
                         ["llm-chat-deliver", "llm-chat-wake"])

    def test_the_deliver_hook_survives_an_unparseable_payload(self):
        """stdin must be consumed regardless, or the pipe breaks on the caller."""
        mod = load("llm-chat-deliver")
        stdin = sys.stdin
        sys.stdin = io.StringIO("not json at all")
        try:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(mod.main(), 0)
        finally:
            sys.stdin = stdin

    def test_reopen_lists_only_the_members_still_marked_done(self):
        """Distinct from every member being done: the room will not re-close on
        the next leave, so it is a note rather than a warning."""
        fake = FakeServer()
        real = cli.call
        cli.call = fake.call
        try:
            fake.channel("room", closed=1, closed_reason="hit the cap",
                         max_messages=200, message_count=3)
            fake.membership("room", "active", done=0)
            fake.membership("room", "gone", done=1)
            out = io.StringIO()
            with redirect_stdout(out):
                cli.do_reopen("http://127.0.0.1:1", "room", None)
            text = out.getvalue()
        finally:
            cli.call = real
        self.assertIn("still marked done: gone", text)
        self.assertNotIn("rejoin", text)

    def test_reload_is_reachable_from_the_command_line(self):
        called = []
        real = cli.do_reload
        cli.do_reload = lambda force, i_know: called.append((force, i_know))
        argv = sys.argv
        sys.argv = ["llm_chat", "reload", "--force", "--i-know"]
        try:
            self.assertEqual(cli.main(), 0)
        finally:
            cli.do_reload = real
            sys.argv = argv
        self.assertEqual(called, [(True, True)])

    def test_reload_refuses_when_no_window_carries_this_project_title(self):
        """Refusing beats reloading whichever window is frontmost — on a machine
        running one window per agent that is a coin flip that can reload a
        colleague's session mid-task."""
        self._reload_verdict("NOMATCH", "no VSCode window")

    def test_reload_refuses_when_the_title_is_ambiguous(self):
        self._reload_verdict("AMBIGUOUS", "more than one VSCode window")

    def _reload_verdict(self, verdict, expected):
        if sys.platform != "darwin":
            self.skipTest("the reload path is macOS-only")
        saved_entry = os.environ.get("CLAUDE_CODE_ENTRYPOINT")
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "claude-vscode"
        write_settings(self.project, SessionStart=["/x/bin/llm-chat-wake"])
        probe = os.path.join(self.project, ".llm_chat", "probe")
        os.makedirs(probe, exist_ok=True)
        with open(os.path.join(probe, "wake-SessionStart"), "w") as f:
            f.write("1 2")

        class Fake:
            @staticmethod
            def run(argv, **kw):
                class Result:
                    returncode = 0
                    stdout = verdict
                    stderr = ""
                return Result
        saved_sub = cli.subprocess
        cli.subprocess = Fake
        try:
            with self.assertRaises(SystemExit) as caught:
                cli.do_reload(force=True)
            self.assertIn(expected, str(caught.exception))
        finally:
            cli.subprocess = saved_sub
            if saved_entry is None:
                os.environ.pop("CLAUDE_CODE_ENTRYPOINT", None)
            else:
                os.environ["CLAUDE_CODE_ENTRYPOINT"] = saved_entry


class LockReleaseTest(unittest.TestCase):
    """Releasing the lock is best-effort inside a `finally`. If unlocking could
    raise, a failure there would escape the contextmanager and surface as a
    crash in whatever the caller was doing — losing the delivery it had just
    completed successfully."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CLAUDE_PROJECT_DIR"] = self.tmp.name
        self.real = cli.fcntl

    def tearDown(self):
        cli.fcntl = self.real
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.tmp.cleanup()

    def test_a_failing_unlock_does_not_escape(self):
        real = self.real

        class FailsOnUnlock:
            LOCK_EX = real.LOCK_EX
            LOCK_NB = real.LOCK_NB
            LOCK_UN = real.LOCK_UN

            @staticmethod
            def flock(fd, operation):
                if operation == real.LOCK_UN:
                    raise OSError("cannot release")
                return real.flock(fd, operation)
        cli.fcntl = FailsOnUnlock
        with cli.read_lock() as held:
            self.assertTrue(held)
        # reaching here at all is the assertion: no OSError escaped
