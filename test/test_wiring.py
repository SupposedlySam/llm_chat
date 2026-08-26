"""Where am I, what is registered, and has it ever actually run.

These defend the distinction that cost this project a day: REGISTERED and FIRED
are different facts, and only the second one means anything. A hook can be
perfectly configured on disk and completely inert, and no amount of reading
settings.json will tell you which.
"""
import io
import json
import os
import re
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
        registered, fired, _, _ = cli.hook_report(self.project)
        self.assertIn("llm-chat-deliver", registered)
        self.assertFalse(fired["llm-chat-deliver"])
        self.probe("post-tool-use")
        _, fired, _, _ = cli.hook_report(self.project)
        self.assertTrue(fired["llm-chat-deliver"])

    def test_the_events_a_hook_is_on_are_reported(self):
        """`registered` alone would hide a waker present only on Stop — the
        half that cannot help a session which never takes a turn."""
        write_settings(self.project, Stop=["/x/bin/llm-chat-wake"])
        _, _, events, _ = cli.hook_report(self.project)
        self.assertEqual(events["llm-chat-wake"], {"Stop"})
        write_settings(self.project, Stop=["/x/bin/llm-chat-wake"],
                       SessionStart=["/x/bin/llm-chat-wake"])
        _, _, events, _ = cli.hook_report(self.project)
        self.assertEqual(events["llm-chat-wake"], {"Stop", "SessionStart"})

    def test_the_CHECKOUT_each_hook_runs_from_is_kept(self):
        """It was read and discarded — the string was in hand and the one
        fact saying WHICH BUILD delivers your messages was thrown away.
        gameloop's repo vendors llm_chat under `.lamp/`, so their doctor
        answered from a months-old copy while the hooks ran current code."""
        write_settings(self.project,
                       PostToolUse=["/a/tree/bin/llm-chat-deliver"],
                       Stop=["/other/tree/bin/llm-chat-wake"])
        _, _, _, trees = cli.hook_report(self.project)
        self.assertEqual(trees, {"/a/tree", "/other/tree"})

    def test_an_unparseable_command_contributes_no_tree(self):
        """A tree named wrongly and confidently is worse than none named.

        The name must be CONTAINED and not be the basename, or the fixture
        answers the same either way — `llm-chat-x` contains no hook name at
        all, so it proved nothing about how the path is read. Caught by the
        sweep, which is the whole reason the sweep exists."""
        write_settings(self.project,
                       PostToolUse=["/w/bin/run-llm-chat-deliver-first"])
        _, _, _, trees = cli.hook_report(self.project)
        self.assertEqual(trees, set())

    def test_a_command_naming_no_hook_at_all_contributes_no_tree(self):
        write_settings(self.project, PostToolUse=["run-wrapper llm-chat-x"])
        _, _, _, trees = cli.hook_report(self.project)
        self.assertEqual(trees, set())

    def test_a_repo_with_nothing_registered_reports_nothing(self):
        registered, _, _, _ = cli.hook_report(self.project)
        self.assertEqual(registered, set())

    def test_malformed_settings_do_not_crash_the_report(self):
        d = os.path.join(self.project, ".claude")
        os.makedirs(d)
        with open(os.path.join(d, "settings.local.json"), "w") as f:
            f.write("{not json")
        registered, _, _, _ = cli.hook_report(self.project)
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


@unittest.skipUnless(sys.platform == "darwin",
                     "the reload path is macOS-only — off it, do_reload "
                     "short-circuits before reaching any of these refusals")
class ReloadGuardTest(unittest.TestCase):
    """Every path here must REFUSE. None of these may reach osascript.

    SKIPPED OFF macOS, which five sibling tests in test_setup.py already were
    and these three were not. `do_reload` returns "automated reload is
    macOS-only" before evaluating any guard, so on Linux each of these
    asserted its refusal text against that sentence and failed — for a reason
    that is nothing to do with the behaviour. Invisible until the suite ran
    somewhere other than the maintainer's laptop; the first CI run found all
    three.
    """

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


class ContinuousIntegrationTest(unittest.TestCase):
    """The workflow is the one place that runs this suite off this machine.

    WHAT THIS CHECKS AND WHAT IT CANNOT. GitHub is the only real consumer of
    that file, so nothing here validates its YAML — an invalid workflow fails
    on GitHub, loudly, which is an honest failure. What is NOT honest is a
    workflow that parses perfectly and runs nothing: it goes green, the badge
    is green, and the suite has silently stopped running off this machine.
    That is this repo's recurring defect, so that is what is asserted.

    Written because verify reported the file UNCHECKED — it matched no rule,
    so nothing looked at it at all. A CI file nothing verifies is the same
    shape as the thing it was added to fix.
    """

    def setUp(self):
        self.path = os.path.join(mutate.ROOT, ".github", "workflows",
                                 "tests.yml")

    def test_the_workflow_exists(self):
        self.assertTrue(os.path.isfile(self.path),
                        "no CI workflow — the suite runs only where somebody "
                        "types a command")

    def test_IT_ACTUALLY_RUNS_THE_SUITE(self):
        """A workflow that runs nothing is green and means nothing."""
        with open(self.path) as f:
            text = f.read()
        self.assertIn("test/run.py", text,
                      "the workflow does not invoke the suite")

    def test_it_runs_on_push_AND_on_pull_request(self):
        """Push alone leaves a contributor's branch unmeasured until it is
        merged, which is the moment the check is worth least."""
        with open(self.path) as f:
            text = f.read()
        self.assertIn("pull_request:", text)
        self.assertIn("push:", text)

    def commands(self):
        """The workflow's executable lines, with comments stripped.

        Reading the whole file failed this the first time it ran — on the
        COMMENT explaining why there are no installs. A check whose fixture
        cannot tell a command from a sentence about commands reports the
        explanation as the violation, and would go on doing so however the
        file was fixed.
        """
        with open(self.path) as f:
            return "\n".join(line for line in f
                             if not line.lstrip().startswith("#"))

    def test_it_installs_NOTHING(self):
        """Every entrypoint here is stdlib-only on purpose — an MCP client
        that spawns bin/llm-chat-mcp should not need a pip install to talk
        to it. A workflow that quietly installed dependencies would make the
        suite pass in a way no consumer can reproduce."""
        self.assertNotIn("pip install", self.commands())

    def test_it_says_the_MUTATION_SWEEP_is_not_run_there(self):
        """The sweep is what says a behaviour is DEFENDED rather than merely
        covered, and it stays on the commit gate because it takes tens of
        minutes. A green tick that is read as "the tests would notice" is
        exactly the false comfort this project keeps removing, so the file has
        to say so where somebody reading it will see it."""
        with open(self.path) as f:
            text = f.read()
        self.assertIn("does NOT run the mutation sweep", text)


# WHERE EACH SHIPPED TRIGGER IS SUPPOSED TO BE WIRED, and it is a decision
# record rather than a list somebody maintains: the test below fails when a
# file in triggers/ has no entry here, so it cannot age quietly. That is the
# only shape of list this repo allows, and it is the difference between
# default-deny and the four complete-by-accident lists already removed from it.
#
# THE THREE VALUES ARE THREE DIFFERENT REGISTRIES, not a taxonomy for its own
# sake. A check that knew about one of them would report the others as unwired.
#
#   claude-hook   .claude/settings.local.json — machine-local, because the
#                 command is an absolute path and the tracked settings.json
#                 would fire it against a directory only this machine has.
#                 NOT VISIBLE ON A COLD CLONE, which is the whole reason this
#                 table exists separately from the registry.
#   game-loop     .game_loop/triggers.json — its own events (harden, stepback,
#                 session_start). Also gitignored, also absolute paths.
#   by-hand       nothing invokes it, deliberately, with the reason.
EXPECTED = {
    "answer-when-asked": "claude-hook",
    "authority-gate": "claude-hook",
    "piped-verdict": "claude-hook",
    "prose-through-shell": "claude-hook",
    "tell-the-consumers": "claude-hook",
    "write-through-interpreter": "claude-hook",
    "learnings-broadcast": "game-loop",
    "learnings-digest": "game-loop",
    "undocumented-surface": "game-loop",
    "issue-watch": "by-hand",
}

BY_HAND_REASON = {
    "issue-watch":
        "a long-running watcher started BY HAND — `sh triggers/issue-watch` — "
        "not a hook. It prints its current open set as a positive control on "
        "the first line precisely because nothing invokes it on a schedule, "
        "so a silent one is visibly broken rather than quietly absent.",
}


class EveryTriggerIsCLASSIFIEDTest(unittest.TestCase):
    """A shipped trigger is registered somewhere, or excused here in writing.

    THE FAILURE THIS ENCODES, and it is not hypothetical. README.md says:

        | `triggers/answer-when-asked` | `Stop` | refuses to end a turn while
          a question is unanswered |

    It was registered in NONE of `.claude/settings.json`,
    `.claude/settings.local.json`, `~/.claude/settings.json`, or
    `.game_loop/triggers.json`. It had tests, 100% coverage and a mutation,
    and it had never fired once — while `owed` sat reporting a question I had
    not answered. The documentation asserted a live guard; the guard was
    furniture.

    Third instance of configured-but-inert here, and the first where a
    document claimed it worked.

    DEFAULT-DENY, over the DIRECTORY. The set of triggers is read from
    `triggers/`, never from a list — a hand-kept list is the
    complete-by-accident defect this repo has now removed four times, most
    recently from the verdict guard I was measuring when I found this one. A
    new trigger that nobody has decided about fails this test on the commit
    that adds it, which is the moment it is cheapest to decide.

    WHAT IT CANNOT DO, said plainly because a rail is silent where it is
    blind: it cannot tell a REGISTERED trigger from a WORKING one. Registration
    is a fact about a config file. Whether the thing fires, and whether it
    fires usefully, is what the tests and the mutation sweep are for. This
    check only refuses the state where nobody has looked.

    showrunner's mechanism, adopted after they described it: enforcement lives
    in the tool, and what the tool enforces is that the unenforceable part was
    written down and is still there.
    """

    def registries(self):
        """Every place a trigger can legitimately be wired, and its contents.

        READ, not assumed. Two registries exist here for different reasons —
        Claude Code's hooks live in settings, and game_loop's event triggers
        live in its own file — and a check that knew about one of them would
        report half the triggers as unwired.
        """
        found = set()
        for relative in (".claude/settings.json",
                         ".claude/settings.local.json",
                         ".game_loop/triggers.json"):
            path = os.path.join(mutate.ROOT, relative)
            try:
                with open(path) as f:
                    text = f.read()
            except OSError:
                continue          # absent is not evidence of anything
            found.add((relative, text))
        return found

    def shipped(self):
        directory = os.path.join(mutate.ROOT, "triggers")
        return sorted(name for name in os.listdir(directory)
                      if os.path.isfile(os.path.join(directory, name))
                      and not name.startswith("."))

    @staticmethod
    def unclassified(shipped, expected):
        """Triggers nobody has decided about. A FUNCTION, so it can be asked
        about a trigger this repo does not have — asked only about the real
        directory, where everything IS classified, it would pass whether or
        not it worked."""
        return [name for name in shipped if name not in expected]

    def test_NOTHING_SHIPS_UNCLASSIFIED(self):
        """The half that works EVERYWHERE, including a cold clone.

        Both registries are gitignored — they hold absolute paths — so on CI
        neither is visible and registration cannot be checked at all. The
        first version of this test did not separate those two questions and
        failed on CI listing six triggers as unwired, which was true of the
        checkout and false of the world. A guard that cannot run where it
        ships is the defect this whole test was written to catch, and it had
        it on the first commit.

        So: WHERE a trigger belongs is tracked and always checkable. WHETHER
        it is really there can only be asked where the registry exists.
        """
        self.assertEqual(
            self.unclassified(self.shipped(), EXPECTED), [],
            "a trigger with no entry in EXPECTED — say which registry it "
            "belongs in, or mark it by-hand with a reason")

    def test_IT_ACTUALLY_REPORTS_ONE(self):
        """The direction the live check cannot exercise while the repo is
        clean, and the direction the whole thing exists for."""
        self.assertEqual(
            self.unclassified(["known", "nobody-decided"],
                              {"known": "claude-hook"}),
            ["nobody-decided"])

    # SPELLED OUT COUNTS TOO, and `one` deliberately absent. In prose "one"
    # is the indefinite article wearing a numeral — "the one test file every
    # change touches" means a single file, not a count that will rot.
    # showrunner measured two false positives from including it.
    WORD_COUNT = ("two|three|four|five|six|seven|eight|nine|ten|eleven|"
                  "twelve|dozen|hundreds?|thousands?")
    GROWS = "tests?|mutations?|entrypoints?|triggers?|assertions?"

    @classmethod
    def counted_in_prose(cls, text):
        """Hard-coded counts of things this repo GROWS.

        DIGITS AND WORDS. The first version was digits-only: it caught "242
        tests" and missed "the four entrypoints" sitting two lines away, which
        I then fixed BY HAND in the same edit. A check that misses the case
        beside the one it caught is the enumeration defect living inside the
        guard against stale claims — showrunner hit the identical thing from
        their side the same morning.

        A function, so it can be asked about text this repo does not contain,
        and so the word case could be proved to FAIL against the old pattern
        before it passed against this one. showrunner's widening looked
        correct because they had written `or "four" in phrase` into the
        assertion — the check agreeing for a reason unrelated to its claim,
        inside the test for a check about exactly that.
        """
        pattern = r"\b(?:\d[\d,]*|%s)\s+(?:%s)\b" % (cls.WORD_COUNT, cls.GROWS)
        return re.findall(pattern, text, re.I)

    def test_THE_DOCS_DO_NOT_COUNT_A_GROWING_SET(self):
        """A count in prose beside a set that grows is the reliable offender.

        README said "242 tests, 100% line coverage on the four entrypoints".
        Wrong twice — the suite passed 1,800 some time ago, and the floor
        covers fourteen files, not four. Nobody re-derived either number
        because nothing had to.

        lamp-owner's remedy from #learnings, and the reason it is deletion
        rather than correction: a corrected number falls behind again on the
        next commit, and there is no version of "242" that survives a test
        being added. What survives is the property the gate enforces —
        `--min 100` — which a reader can run.

        FOUND BY RUNNING MY OWN SUGGESTION AGAINST MY OWN DOCS. I told
        showrunner every rate should name a committed tool that reproduces
        it; they built that, it found three unsourced rates in their front
        door, and the symmetric sweep here found this. I did NOT build their
        net: measured over both front doors it flagged six blocks, of which
        four were the detector's own noise — a threshold read as a rate,
        prose read as a claim. A check that is two-thirds noise is the
        mostly-noise failure this repo has removed twice. This is the narrow
        rule the one real finding actually supports.
        """
        for name in ("README.md", "llms.txt"):
            with open(os.path.join(mutate.ROOT, name)) as f:
                text = f.read()
            # The paragraph explaining the deletion quotes the old sentence,
            # so the quote is excluded by the quoting itself: only counts
            # outside a blockquote are claims the doc is still making.
            live = "\n".join(line for line in text.splitlines()
                             if not line.lstrip().startswith(">"))
            with self.subTest(doc=name):
                self.assertEqual(
                    self.counted_in_prose(live), [],
                    "%s states a count of something that grows — say the "
                    "property instead, and let the gate carry the number"
                    % name)

    def test_IT_CATCHES_A_COUNT_IT_SHOULD(self):
        """The direction the live check cannot exercise now that the docs are
        clean, which is exactly when a broken guard looks healthiest."""
        self.assertEqual(
            self.counted_in_prose("the suite runs 242 tests and is fine"),
            ["242 tests"])
        self.assertEqual(
            self.counted_in_prose("100% line coverage, ratcheted at --min 100"),
            [], "a property with a number in it is not a count of a growing set")

    def test_a_count_SPELLED_OUT_is_caught_too(self):
        """The case the first version missed while catching its neighbour.

        `242 tests` was found by the check; `the four entrypoints`, two lines
        away in the same file, was found by me reading it. Both were wrong,
        both rot the same way, and only one of them had a guard.
        """
        self.assertEqual(
            self.counted_in_prose("coverage on the four entrypoints"),
            ["four entrypoints"])
        self.assertEqual(self.counted_in_prose("THREE TRIGGERS are wired"),
                         ["THREE TRIGGERS"])

    def test_THE_DELETED_SENTENCE_IS_ITS_OWN_FIXTURE(self):
        """README quotes the claim it removed, and that quote carries BOTH
        forms — `242 tests` and `four entrypoints`. So the widening is proved
        against real text in this repo rather than a synthetic string, and the
        blockquote exclusion is proved to be what keeps the live scan quiet
        rather than the pattern simply missing them.

        Two things fail together if either half breaks: a narrowed pattern
        stops finding the word form here, and a dropped blockquote rule makes
        the live check fire on a sentence the doc is explicitly not claiming.
        """
        with open(os.path.join(mutate.ROOT, "README.md")) as f:
            text = f.read()
        quoted = "\n".join(line for line in text.splitlines()
                           if line.lstrip().startswith(">"))
        found = [" ".join(hit.split()).lower()
                 for hit in self.counted_in_prose(quoted)]
        self.assertIn("242 tests", found)
        self.assertIn("four entrypoints", found,
                      "the spelled-out form is the one the first version "
                      "missed — if it is absent here the widening is undone")

    def test_ONE_is_not_a_count(self):
        """In prose it is the indefinite article wearing a numeral, and it
        does not rot: "the one test file every change touches" means a single
        file, and stays true however many tests are added. showrunner
        measured two false positives from including it."""
        self.assertEqual(
            self.counted_in_prose("the one test file every change touches"),
            [])

    def test_a_count_ACROSS_A_LINE_BREAK_is_still_a_count(self):
        """`205 tests` was invisible to the eye AND to a line-by-line scan
        because prose wrapped between the number and the noun. The whole file
        is matched, so a wrap changes nothing."""
        self.assertEqual(
            self.counted_in_prose("leaves all 205\ntests green"),
            ["205\ntests"])

    def test_BOTH_DOCS_SAY_WHICH_BUILD_TO_RUN(self):
        """`doctor` already warns when it is the wrong build — and an OLD copy
        cannot warn you about itself, which is exactly the case that bites.

        showrunner ran a vendored llm_chat for a week and reported their chat
        healthy. The two builds gave opposite readings of the same fact: the
        old one said a wake had landed and replies arrive on their own; the
        current one adds that a LATER wake was requested and never landed, so
        the idle path was dead. A stale build hands you the reassuring half of
        a two-part finding.

        So the rule has to be readable with NO build running, which means the
        docs — and a doc claim rots exactly as quietly as a code one. This
        asserts the sentence is there, the way `test_it_says_the_MUTATION_
        SWEEP_is_not_run_there` does for the CI file. It cannot check that the
        advice is good; only that the decision to give it is still written
        down.
        """
        for name in ("README.md", "llms.txt"):
            with self.subTest(doc=name):
                with open(os.path.join(mutate.ROOT, name)) as f:
                    text = f.read()
                self.assertIn("registered hooks", text,
                              "%s must say the hooks' directory is the build "
                              "that matters" % name)
                self.assertIn("llm-chat-deliver' .claude/settings.local.json",
                              text,
                              "%s must show HOW to read it, not just say to"
                              % name)

    def test_BUILD_LEAVINGS_ARE_NOT_TRIGGERS(self):
        """`triggers/__pycache__` is real and this check survives it by
        ACCIDENT — the listing filters `os.path.isfile`, and a directory
        happens not to be a file. Nothing decided about it.

        showrunner hit the sharp version: their parse check compiled hooks
        with `py_compile`, which left a `__pycache__` in the hooks directory,
        which their wiring net then reported as a hook nobody had registered.
        One check manufacturing the condition another check flags, with
        neither wrong on its own.

        Here the same artifact exists — `support.load` imports these scripts
        to test them — and the only thing standing between it and a spurious
        "unclassified trigger" is that filter. So it is pinned WITH THE
        REASON, on exactly the argument I put to showrunner about their own
        accidental case: the point is not that the behaviour is load-bearing,
        it is that the ACCIDENT is. A later `glob` or a dropped `isfile` would
        start reporting a build artifact as an unwired guard, and the failure
        would read as a wiring problem rather than a listing one.
        """
        self.assertNotIn("__pycache__", self.shipped())
        self.assertIn("piped-verdict", self.shipped(),
                      "the filter must not have thrown out the real ones too")

    def test_a_BY_HAND_trigger_carries_its_reason(self):
        """`by-hand` is the answer that asserts nothing invokes it, which is
        the same claim the defect made by accident. It costs a sentence."""
        for name, where in EXPECTED.items():
            if where == "by-hand":
                self.assertIn(name, BY_HAND_REASON,
                              "by-hand without a reason is a shrug")

    def test_an_ENTRY_names_a_trigger_that_still_exists(self):
        """The direction that rots quietly: an entry for a deleted trigger
        reads as a decision about something real."""
        gone = [name for name in EXPECTED if name not in self.shipped()]
        self.assertEqual(gone, [], "classified triggers that no longer exist")

    def test_where_a_REGISTRY_IS_VISIBLE_the_wiring_is_checked(self):
        """The other half, and it SKIPS OUT LOUD rather than passing quietly.

        On this machine both registries exist and every hook is verified
        present. On a cold clone neither does, and the honest report is that
        registration went unchecked — not a green tick over a question nobody
        asked.
        """
        registries = {name: text for name, text in self.registries()}
        local = registries.get(".claude/settings.local.json")
        loop = registries.get(".game_loop/triggers.json")
        if local is None and loop is None:
            self.skipTest("neither registry is visible — both are gitignored, "
                          "so registration cannot be checked from this "
                          "checkout. WHERE each trigger belongs is still "
                          "enforced above.")
        for name, where in sorted(EXPECTED.items()):
            text = {"claude-hook": local, "game-loop": loop}.get(where)
            if where == "by-hand" or text is None:
                continue
            with self.subTest(trigger=name):
                self.assertIn(name, text,
                              "%s is classified %s and is not in that "
                              "registry" % (name, where))


if __name__ == "__main__":
    unittest.main()
