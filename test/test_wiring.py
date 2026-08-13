"""Where am I, what is registered, and has it ever actually run.

These defend the distinction that cost this project a day: REGISTERED and FIRED
are different facts, and only the second one means anything. A hook can be
perfectly configured on disk and completely inert, and no amount of reading
settings.json will tell you which.
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mutate  # noqa: E402
import run  # noqa: E402
from support import load, write_settings  # noqa: E402

cli = load("llm_chat")


class ProjectDirTest(unittest.TestCase):
    """`.llm_chat/` lives at a project's ROOT. Resolving to wherever the agent
    happens to be standing splits one project into several identities."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self.cwd)
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.tmp.cleanup()

    def test_the_harness_env_var_wins_when_set(self):
        os.environ["CLAUDE_PROJECT_DIR"] = self.root
        self.assertEqual(cli.project_dir(), self.root)

    def test_a_subdirectory_resolves_to_the_repo_root(self):
        """Join at the root, `say` from src/, and you used to be told you had
        never joined — then joining again gave one project two identities."""
        os.makedirs(os.path.join(self.root, ".git"))
        deep = os.path.join(self.root, "src", "inner")
        os.makedirs(deep)
        os.chdir(deep)
        self.assertEqual(os.path.realpath(cli.project_dir()), self.root)

    def test_an_existing_llm_chat_dir_also_marks_the_root(self):
        os.makedirs(os.path.join(self.root, ".llm_chat"))
        deep = os.path.join(self.root, "a", "b")
        os.makedirs(deep)
        os.chdir(deep)
        self.assertEqual(os.path.realpath(cli.project_dir()), self.root)

    def test_with_no_marker_anywhere_it_falls_back_to_cwd(self):
        """Only when there is genuinely nothing to find — and it says so in the
        code rather than silently preferring a wrong answer."""
        deep = os.path.join(self.root, "loose")
        os.makedirs(deep)
        os.chdir(deep)
        self.assertEqual(os.path.realpath(cli.project_dir()),
                         os.path.realpath(deep))

    def test_joined_path_hangs_off_the_resolved_root(self):
        """Both scopes hang off the same resolved root — that is the claim.
        Stated for each explicitly, because the answer now depends on whether
        a session is present and inheriting that from the ambient environment
        would make this test mean different things to different runners."""
        os.environ["CLAUDE_PROJECT_DIR"] = self.root
        saved = os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        try:
            self.assertEqual(
                cli.joined_path(),
                os.path.join(self.root, ".llm_chat", "joined.json"))
            os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-1"
            self.assertEqual(
                cli.joined_path(),
                os.path.join(self.root, ".llm_chat", "sessions", "sid-1",
                             "joined.json"))
        finally:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            if saved is not None:
                os.environ["CLAUDE_CODE_SESSION_ID"] = saved


class HostTest(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.get(k)
                      for k in ("CLAUDE_CODE_ENTRYPOINT", "TERM_PROGRAM")}
        for k in self.saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_the_entrypoint_identifies_the_vscode_extension(self):
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "claude-vscode"
        self.assertEqual(cli.host(), "vscode-extension")

    def test_a_cli_entrypoint_is_not_the_extension_even_inside_vscode(self):
        """TERM_PROGRAM=vscode is equally true of a plain `claude` run in the
        integrated terminal, where the remedy is a RESTART, not a reload."""
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "cli"
        os.environ["TERM_PROGRAM"] = "vscode"
        self.assertEqual(cli.host(), "cli")

    def test_every_host_has_advice(self):
        for where in ("vscode-extension", "vscode-terminal", "cli"):
            self.assertIn(where, cli.RELOAD_ADVICE)


class HookReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def probe(self, name):
        d = os.path.join(self.project, ".llm_chat", "probe")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "w") as f:
            f.write("1 2")

    def test_registered_and_fired_are_tracked_separately(self):
        """The whole point. A hook present in settings.json and never invoked
        is indistinguishable from a working one by configuration alone."""
        write_settings(self.project, PostToolUse=["/x/bin/llm-chat-deliver"])
        registered, fired, _ = cli.hook_report(self.project)
        self.assertIn("llm-chat-deliver", registered)
        self.assertFalse(fired["llm-chat-deliver"])
        self.probe("post-tool-use")
        _, fired, _ = cli.hook_report(self.project)
        self.assertTrue(fired["llm-chat-deliver"])

    def test_the_events_a_hook_is_on_are_reported(self):
        """`registered` alone would hide a waker present only on Stop — the
        half that cannot help a session which never takes a turn."""
        write_settings(self.project, Stop=["/x/bin/llm-chat-wake"])
        _, _, events = cli.hook_report(self.project)
        self.assertEqual(events["llm-chat-wake"], {"Stop"})
        write_settings(self.project, Stop=["/x/bin/llm-chat-wake"],
                       SessionStart=["/x/bin/llm-chat-wake"])
        _, _, events = cli.hook_report(self.project)
        self.assertEqual(events["llm-chat-wake"], {"Stop", "SessionStart"})

    def test_a_repo_with_nothing_registered_reports_nothing(self):
        registered, _, _ = cli.hook_report(self.project)
        self.assertEqual(registered, set())

    def test_malformed_settings_do_not_crash_the_report(self):
        d = os.path.join(self.project, ".claude")
        os.makedirs(d)
        with open(os.path.join(d, "settings.local.json"), "w") as f:
            f.write("{not json")
        registered, _, _ = cli.hook_report(self.project)
        self.assertEqual(registered, set())


class FingerprintTest(unittest.TestCase):
    def test_the_fingerprint_covers_the_hook_scripts(self):
        """Asking only 'is a hook missing' is blind to a script rewritten
        behind an unchanged command line."""
        first = cli.wiring_fingerprint()
        self.assertEqual(first, cli.wiring_fingerprint(), "must be stable")
        self.assertTrue(all(c in "0123456789abcdef" for c in first))

    def test_a_missing_stamp_reads_as_none_rather_than_as_agreement(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(cli.installed_fingerprint(tmp))

    def test_a_recorded_stamp_is_read_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, ".llm_chat")
            os.makedirs(d)
            with open(os.path.join(d, "installed.json"), "w") as f:
                json.dump({"fingerprint": "abc123"}, f)
            self.assertEqual(cli.installed_fingerprint(tmp), "abc123")


class ReloadGuardTest(unittest.TestCase):
    """Every path here must REFUSE. None of these may reach osascript."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name
        os.environ["CLAUDE_PROJECT_DIR"] = self.project
        self.saved = os.environ.get("CLAUDE_CODE_ENTRYPOINT")
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "claude-vscode"

    def tearDown(self):
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        if self.saved is None:
            os.environ.pop("CLAUDE_CODE_ENTRYPOINT", None)
        else:
            os.environ["CLAUDE_CODE_ENTRYPOINT"] = self.saved
        self.tmp.cleanup()

    def test_refuses_outside_the_vscode_extension(self):
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "cli"
        with self.assertRaises(SystemExit) as caught:
            cli.do_reload(force=True)
        self.assertIn("nothing to reload", str(caught.exception))

    def test_refuses_when_the_waker_is_not_on_sessionstart(self):
        write_settings(self.project, Stop=["/x/bin/llm-chat-wake"])
        with self.assertRaises(SystemExit) as caught:
            cli.do_reload(force=True)
        self.assertIn("SessionStart", str(caught.exception))

    def test_refuses_when_sessionstart_has_never_been_seen_firing(self):
        """Registration is not firing. An earlier guard trusted the config and
        stranded the session twice: it came back with nothing listening."""
        write_settings(self.project, SessionStart=["/x/bin/llm-chat-wake"])
        with self.assertRaises(SystemExit) as caught:
            cli.do_reload(force=True)
        self.assertIn("never been", str(caught.exception))

    def test_refuses_without_force_even_when_fully_wired(self):
        write_settings(self.project, SessionStart=["/x/bin/llm-chat-wake"])
        d = os.path.join(self.project, ".llm_chat", "probe")
        os.makedirs(d)
        with open(os.path.join(d, "wake-SessionStart"), "w") as f:
            f.write("1 2")
        with self.assertRaises(SystemExit) as caught:
            cli.do_reload(force=False)
        self.assertIn("--force", str(caught.exception))


class WiredFromTest(unittest.TestCase):
    """Drift is measured against the tree a repo was WIRED FROM.

    It was measured against this checkout, full stop, which was fine while
    every consumer pointed at a sibling clone and wrong the moment one
    vendored its own copy. Measured on a real one: lamp recorded 53473fcc, its
    vendored tree hashed 53473fcc, this checkout hashed 999f615d — permanently
    STALE for a repo that matched its own source exactly, with a remedy that
    would have repointed its hooks here and undone the vendoring.

    installed.json has recorded `checkout` since the beginning and nothing ever
    read it. Asked directly whether anything depended on that field, I checked
    and answered "written by install.sh and read by nothing" — true, and
    exactly why this was broken.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = os.path.join(self.tmp.name, "consumer")
        os.makedirs(os.path.join(self.project, ".llm_chat"))

    def tearDown(self):
        self.tmp.cleanup()

    def vendored(self, deliver=b"one", wake=b"two"):
        tree = os.path.join(self.tmp.name, "vendored")
        os.makedirs(os.path.join(tree, "bin"), exist_ok=True)
        for name, body in (("llm-chat-deliver", deliver),
                           ("llm-chat-wake", wake)):
            with open(os.path.join(tree, "bin", name), "wb") as f:
                f.write(body)
        return tree

    def stamp(self, checkout=None, fingerprint="x"):
        record = {"fingerprint": fingerprint}
        if checkout is not None:
            record["checkout"] = checkout
        with open(os.path.join(self.project, ".llm_chat",
                               "installed.json"), "w") as f:
            json.dump(record, f)

    def test_it_hashes_the_tree_it_is_given(self):
        tree = self.vendored()
        self.assertEqual(cli.wiring_fingerprint(tree),
                         cli.wiring_fingerprint(tree))
        self.assertNotEqual(cli.wiring_fingerprint(tree),
                            cli.wiring_fingerprint())

    def test_it_notices_a_change_in_THAT_tree(self):
        tree = self.vendored()
        before = cli.wiring_fingerprint(tree)
        self.vendored(wake=b"rewritten")
        self.assertNotEqual(cli.wiring_fingerprint(tree), before)

    def test_a_vendored_consumer_matching_its_own_source_is_NOT_stale(self):
        """The reported false positive, as a test."""
        tree = self.vendored()
        self.stamp(checkout=tree, fingerprint=cli.wiring_fingerprint(tree))
        self.assertEqual(cli.installed_fingerprint(self.project),
                         cli.wiring_fingerprint(cli.installed_checkout(
                             self.project)))

    def test_a_vendored_consumer_that_HAS_drifted_still_reads_as_stale(self):
        """Paired with the test above: a check that stopped firing would pass
        the first one and be useless."""
        tree = self.vendored()
        self.stamp(checkout=tree, fingerprint=cli.wiring_fingerprint(tree))
        self.vendored(wake=b"upgraded")
        self.assertNotEqual(cli.installed_fingerprint(self.project),
                            cli.wiring_fingerprint(cli.installed_checkout(
                                self.project)))

    def test_the_recorded_checkout_is_read_back(self):
        self.stamp(checkout="/somewhere/vendored")
        self.assertEqual(cli.installed_checkout(self.project),
                         "/somewhere/vendored")

    def test_a_stamp_without_the_field_is_None_not_a_guess(self):
        """Written before the field existed. The caller must fall back to this
        checkout and SAY so, not silently compare against whatever is handy."""
        self.stamp()
        self.assertIsNone(cli.installed_checkout(self.project))

    def test_no_stamp_at_all_is_None(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(cli.installed_checkout(tmp))

    def test_a_corrupt_stamp_is_None(self):
        with open(os.path.join(self.project, ".llm_chat",
                               "installed.json"), "w") as f:
            f.write("{not json")
        self.assertIsNone(cli.installed_checkout(self.project))

    def test_hashing_a_tree_that_is_gone_does_not_crash(self):
        """It must produce a value rather than raise, so the caller can report
        'wired from a tree that is gone' instead of dying inside doctor."""
        self.assertTrue(cli.wiring_fingerprint("/no/such/tree"))


class DirtyCheckoutTest(unittest.TestCase):
    """A drift notice that prescribes re-installing has to say what state the
    source is in.

    Reported by an agent that got the notice twice in minutes, checked, and
    found the source HEAD had not moved at all — the fingerprint was being
    shifted by uncommitted files, one of them the wake hook itself. They
    declined the fix, correctly: re-installing would have wired a live session
    to a half-finished hook, and the wake hook is what delivers the message
    telling you it broke.
    """

    def setUp(self):
        self.real = cli.subprocess.run

    def tearDown(self):
        cli.subprocess.run = self.real

    def answer(self, stdout, returncode=0):
        class Done:
            pass
        done = Done()
        done.stdout, done.returncode = stdout, returncode
        cli.subprocess.run = lambda *a, **kw: done
        return cli.checkout_dirty("/somewhere")

    def test_uncommitted_changes_are_dirty(self):
        self.assertTrue(self.answer(" M bin/llm-chat-wake\n"))

    def test_a_clean_tree_is_clean(self):
        self.assertFalse(self.answer(""))

    def test_whitespace_only_output_is_clean(self):
        self.assertFalse(self.answer("  \n "))

    def test_not_a_checkout_is_UNKNOWN_not_clean(self):
        """Reporting 'clean' when git could not answer would put the confident
        wording back, which is the whole defect."""
        self.assertIsNone(self.answer("", returncode=128))

    def test_no_git_at_all_is_unknown(self):
        def explode(*a, **kw):
            raise OSError("no git")
        cli.subprocess.run = explode
        self.assertIsNone(cli.checkout_dirty("/somewhere"))


class LeakDetectorTest(unittest.TestCase):
    """The detector that catches a test patching a shared module — itself
    defended, which it was not.

    Found by probing it: mutating its "nothing leaked" branch to always-true
    left the suite GREEN. So the rail everybody relies on to catch
    `mod.subprocess.run = stub` could be broken and nothing would say so. It
    is wired at two call sites and it does fire; it was simply nobody's job to
    notice if it stopped.
    """

    def test_it_reports_a_shared_callable_left_patched(self):
        """report_global_leaks prints to STDERR, not stdout — wrapping only
        redirect_stdout let this deliberate simulation leak its own "THE SUITE
        LEFT SHARED CALLABLES PATCHED" line straight to the real terminal on
        every run, indistinguishable from an actual failure. Its sibling test
        below already redirects the right stream."""
        import subprocess as real
        before = run.shared_callables()
        keep = real.run
        real.run = lambda *a, **k: None
        try:
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                leaked = run.report_global_leaks(before)
        finally:
            real.run = keep
        self.assertTrue(leaked)

    def test_an_unpatched_suite_reports_nothing(self):
        """Paired: a detector that always fires is one nobody leaves on."""
        before = run.shared_callables()
        self.assertFalse(run.report_global_leaks(before))

    def test_it_names_what_leaked(self):
        import subprocess as real
        before = run.shared_callables()
        keep = real.run
        real.run = lambda *a, **k: None
        try:
            err = io.StringIO()
            with redirect_stderr(err):
                run.report_global_leaks(before)
        finally:
            real.run = keep
        self.assertIn("subprocess.run", err.getvalue())


class ExecutableBitTest(unittest.TestCase):
    """Every entrypoint in bin/ has to be runnable.

    The Slack bridge shipped without its execute bit. Nothing noticed: the test
    suite imports these files rather than running them, the coverage runner
    imports them, the mutation sweep imports them — every instrument agreed it
    was 100% covered while the only thing a person actually does with it, type
    its name, failed with permission denied. A file can be perfect and
    unrunnable, and testing it by import cannot tell the difference.

    Discovery is shared with the sweep, so a file added tomorrow is checked
    without anyone remembering to add it here."""

    def test_every_bin_script_is_executable(self):
        scripts = mutate.discover_sources()
        self.assertTrue(scripts, "discovery found no scripts to check")
        for relative in scripts:
            with self.subTest(script=relative):
                # Discovery returns paths relative to the repo root, and this
                # runs from test/. Resolving them against the cwd made all four
                # fail identically, which reads as a real defect rather than as
                # the guard pointing at nothing.
                path = os.path.join(mutate.ROOT, relative)
                self.assertTrue(os.access(path, os.X_OK),
                                "%s is not executable — `chmod +x` it"
                                % relative)


if __name__ == "__main__":
    unittest.main()
