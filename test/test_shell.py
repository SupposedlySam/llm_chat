"""install.sh and legacy_teardown.sh, run for real against throwaway repos.

These are not unit tests: they invoke the actual scripts, because what they do
is edit somebody else's settings file, and the only honest way to check that is
to let them edit one. Everything they touch is a temporary directory.

The behaviours defended here are the ones that damage a repo when wrong —
clobbering a foreign hook, stacking duplicates that deliver every message
twice, or leaving a machine-specific absolute path in a tracked file.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import ROOT  # noqa: E402

INSTALL = os.path.join(ROOT, "install.sh")
TEARDOWN = os.path.join(ROOT, "legacy_teardown.sh")

CLAUDE_PATH = shutil.which("claude")
HAS_CLAUDE = CLAUDE_PATH is not None


def _path_without_claude():
    """PATH with EVERY directory holding a `claude` removed, so `command -v
    claude` fails inside install.sh/legacy_teardown.sh exactly like it would
    on a machine without the CLI. Without this, every install/teardown test
    on a dev machine that HAS `claude` also spawns it for real — turning a
    ~40ms suite into one that pays Node startup twice per test for a feature
    almost none of these tests are about. The few that ARE about it opt back
    in explicitly.

    EVERY directory, not just `shutil.which`'s answer: this machine has one
    under nvm and another in /opt/homebrew, so dropping the first left the
    second reachable and `allow_claude=False` quietly meant nothing. The
    install still registered an MCP server and
    test_a_missing_claude_binary_does_not_fail_the_install failed — asserting
    the absence of something the helper had not actually made absent.

    A helper that removes ONE of a thing is indistinguishable from one that
    removes all of it until a machine has two, and then it fails on that
    machine only.
    """
    parts = os.environ.get("PATH", "").split(os.pathsep)
    return os.pathsep.join(
        p for p in parts
        if not os.access(os.path.join(p or ".", "claude"), os.X_OK))


def hooks_in(path):
    """{event: [command, ...]} from a settings file, or {} if absent."""
    try:
        with open(path) as f:
            settings = json.load(f)
    except (OSError, ValueError):
        return {}
    out = {}
    for event, groups in (settings.get("hooks") or {}).items():
        for group in groups:
            for hook in group.get("hooks", []):
                out.setdefault(event, []).append(hook.get("command", ""))
    return out


class ShellTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "project")
        os.makedirs(self.repo)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        # install.sh also registers an MCP server via `claude mcp add`, keyed
        # by $HOME/.claude.json under this test's repo PATH. Without an
        # isolated HOME that writes into the developer's REAL global config —
        # and the entry outlives the test, since self.repo is deleted at
        # tearDown but nothing ever un-registers a project that was never
        # torn down through legacy_teardown.sh. Found exactly that way: a
        # dozen dead temp-dir entries in ~/.claude.json after one test run.
        self.fake_home = os.path.join(self.tmp.name, "home")
        os.makedirs(self.fake_home)

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, script, *args, expect_success=True, allow_claude=False):
        """Run it, and by default REQUIRE it to have worked.

        Swallowing the exit code turns a script failure into a confusing
        FileNotFoundError three lines later, on the file it never wrote. The
        failure should name itself.

        `claude` is off PATH by default so ordinary hook/gitignore assertions
        never pay to spawn it; `allow_claude=True` opts back in for the tests
        that are actually about the MCP registration step.
        """
        env = dict(os.environ, HOME=self.fake_home)
        if not allow_claude:
            env["PATH"] = _path_without_claude()
        done = subprocess.run([script] + list(args), capture_output=True,
                              text=True, cwd=ROOT, env=env)
        if expect_success and done.returncode != 0:
            self.fail("%s failed (exit %d)\n  stdout: %s\n  stderr: %s"
                      % (os.path.basename(script), done.returncode,
                         done.stdout.strip()[-400:], done.stderr.strip()[-400:]))
        return done

    def mcp_registered(self):
        """Whether this test's isolated home has our server registered for
        ANY project — not matched by exact path, because macOS resolves
        $TMPDIR through a symlink (/tmp -> /private/tmp) that `claude`
        follows and this test's own `self.repo` string does not."""
        try:
            with open(os.path.join(self.fake_home, ".claude.json")) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return False
        return any("llm_chat" in (proj.get("mcpServers") or {})
                   for proj in data.get("projects", {}).values())

    @property
    def local(self):
        return os.path.join(self.repo, ".claude", "settings.local.json")

    @property
    def shared(self):
        return os.path.join(self.repo, ".claude", "settings.json")

    def write_shared(self, payload):
        os.makedirs(os.path.dirname(self.shared), exist_ok=True)
        with open(self.shared, "w") as f:
            json.dump(payload, f)


class InstallTest(ShellTestCase):
    def test_it_registers_all_three_hooks(self):
        done = self.run_script(INSTALL, self.repo)
        self.assertEqual(done.returncode, 0, done.stderr)
        hooks = hooks_in(self.local)
        self.assertEqual(len(hooks.get("PostToolUse", [])), 1)
        self.assertEqual(len(hooks.get("Stop", [])), 1)
        self.assertEqual(len(hooks.get("SessionStart", [])), 1,
                         "without SessionStart a reloaded session has nothing "
                         "listening and cannot be woken at all")

    def test_the_absolute_path_stays_out_of_the_tracked_file(self):
        """settings.json is tracked in most repos, and the command is an
        absolute path to THIS machine's checkout."""
        self.run_script(INSTALL, self.repo)
        self.assertEqual(hooks_in(self.shared), {})
        self.assertTrue(os.path.exists(self.local))

    def test_re_running_updates_in_place_rather_than_stacking(self):
        """Two copies deliver every message twice and advance the cursor once,
        which reads as the other agent repeating itself."""
        self.run_script(INSTALL, self.repo)
        self.run_script(INSTALL, self.repo)
        hooks = hooks_in(self.local)
        for event in ("PostToolUse", "Stop", "SessionStart"):
            self.assertEqual(len(hooks[event]), 1, event)

    def test_a_previous_install_is_migrated_out_of_the_tracked_file(self):
        """Re-installing alone cannot fix this: the installer no longer writes
        to the file the old hook lives in."""
        self.write_shared({"hooks": {"PostToolUse": [
            {"matcher": ".*", "hooks": [
                {"type": "command", "command": "/old/bin/llm-chat-deliver"}]}]}})
        done = self.run_script(INSTALL, self.repo)
        self.assertIn("migrated", done.stdout)
        self.assertEqual(hooks_in(self.shared), {})

    def test_foreign_hooks_survive_untouched(self):
        """The file carries somebody else's guards; a bad merge here is not a
        small inconvenience."""
        self.write_shared({"hooks": {
            "PostToolUse": [{"matcher": "Edit", "hooks": [
                {"type": "command", "command": "$CLAUDE_PROJECT_DIR/fmt.sh"}]}],
            "PreToolUse": [{"matcher": ".*", "hooks": [
                {"type": "command", "command": "$CLAUDE_PROJECT_DIR/guard.sh"}]}],
        }})
        self.run_script(INSTALL, self.repo)
        surviving = hooks_in(self.shared)
        self.assertIn("$CLAUDE_PROJECT_DIR/fmt.sh", surviving["PostToolUse"])
        self.assertIn("$CLAUDE_PROJECT_DIR/guard.sh", surviving["PreToolUse"])

    def test_backups_are_kept_outside_the_repo(self):
        """A .bak beside the file is untracked AND unignored, so the next
        `git add -A` commits it."""
        self.run_script(INSTALL, self.repo)
        leaked = [n for n in os.listdir(os.path.join(self.repo, ".claude"))
                  if ".bak." in n]
        self.assertEqual(leaked, [])

    def test_it_gitignores_what_must_not_be_shared(self):
        self.run_script(INSTALL, self.repo)
        with open(os.path.join(self.repo, ".gitignore")) as f:
            text = f.read()
        self.assertIn(".llm_chat/", text)
        self.assertIn(".claude/settings.local.json", text)

    def test_gitignore_entries_are_not_duplicated_on_re_run(self):
        self.run_script(INSTALL, self.repo)
        self.run_script(INSTALL, self.repo)
        with open(os.path.join(self.repo, ".gitignore")) as f:
            lines = [l.strip() for l in f]
        self.assertEqual(lines.count(".llm_chat/"), 1)

    def test_an_existing_gitignore_is_appended_to_not_replaced(self):
        with open(os.path.join(self.repo, ".gitignore"), "w") as f:
            f.write("node_modules/\n")
        self.run_script(INSTALL, self.repo)
        with open(os.path.join(self.repo, ".gitignore")) as f:
            text = f.read()
        self.assertIn("node_modules/", text)
        self.assertIn(".llm_chat/", text)

    def test_it_records_which_hook_scripts_the_repo_was_wired_from(self):
        """The stamp catches a script rewritten behind an unchanged command
        line, which no reading of the registration can see."""
        self.run_script(INSTALL, self.repo)
        with open(os.path.join(self.repo, ".llm_chat", "installed.json")) as f:
            stamp = json.load(f)
        self.assertTrue(stamp["fingerprint"])
        self.assertEqual(stamp["checkout"], ROOT)

    def test_a_missing_target_is_refused(self):
        done = self.run_script(INSTALL, os.path.join(self.tmp.name, "nope"),
                               expect_success=False)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("no such directory", done.stderr)

    def test_no_target_is_refused_with_usage(self):
        done = self.run_script(INSTALL, expect_success=False)
        self.assertEqual(done.returncode, 2)
        self.assertIn("usage:", done.stderr)

    def test_malformed_local_settings_are_refused_rather_than_overwritten(self):
        os.makedirs(os.path.dirname(self.local), exist_ok=True)
        with open(self.local, "w") as f:
            f.write("{not json")
        done = self.run_script(INSTALL, self.repo, expect_success=False)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("refusing", done.stderr)
        with open(self.local) as f:
            self.assertEqual(f.read(), "{not json", "must not have been touched")


class TeardownTest(ShellTestCase):
    def install(self):
        self.run_script(INSTALL, self.repo)

    def test_a_dry_run_changes_nothing(self):
        self.install()
        before = hooks_in(self.local)
        done = self.run_script(TEARDOWN, "--dry-run", self.repo)
        self.assertIn("would", done.stdout)
        self.assertEqual(hooks_in(self.local), before)
        self.assertTrue(os.path.isdir(os.path.join(self.repo, ".llm_chat")))

    def test_it_removes_every_hook_it_installed(self):
        self.install()
        self.run_script(TEARDOWN, self.repo)
        self.assertEqual(hooks_in(self.local), {})
        self.assertFalse(os.path.isdir(os.path.join(self.repo, ".llm_chat")))

    def test_it_cleans_up_a_legacy_install_in_the_tracked_file(self):
        """Re-installing cannot do this, which is the reason this script
        exists at all."""
        self.write_shared({"hooks": {"PostToolUse": [
            {"matcher": ".*", "hooks": [
                {"type": "command", "command": "/old/bin/llm-chat-deliver"}]}]}})
        with open(os.path.join(self.repo, ".claude",
                               "settings.json.bak.123"), "w") as f:
            f.write("{}")
        self.run_script(TEARDOWN, self.repo)
        self.assertEqual(hooks_in(self.shared), {})
        self.assertFalse(os.path.exists(os.path.join(
            self.repo, ".claude", "settings.json.bak.123")))

    def test_foreign_hooks_survive_teardown(self):
        self.write_shared({"hooks": {
            "PostToolUse": [{"matcher": "Edit", "hooks": [
                {"type": "command", "command": "$CLAUDE_PROJECT_DIR/fmt.sh"}]}],
        }})
        self.install()
        self.run_script(TEARDOWN, self.repo)
        self.assertIn("$CLAUDE_PROJECT_DIR/fmt.sh",
                      hooks_in(self.shared)["PostToolUse"])

    def test_only_our_gitignore_entry_is_removed(self):
        with open(os.path.join(self.repo, ".gitignore"), "w") as f:
            f.write("node_modules/\n")
        self.install()
        self.run_script(TEARDOWN, self.repo)
        with open(os.path.join(self.repo, ".gitignore")) as f:
            text = f.read()
        self.assertIn("node_modules/", text)
        self.assertNotIn(".llm_chat/", text)

    def test_the_settings_local_ignore_is_kept_by_default(self):
        """Other tools put machine-specific absolute paths in that file too;
        un-ignoring it could get someone's local config committed."""
        self.install()
        self.run_script(TEARDOWN, self.repo)
        with open(os.path.join(self.repo, ".gitignore")) as f:
            self.assertIn(".claude/settings.local.json", f.read())

    def test_purge_opts_into_removing_that_one_too(self):
        self.install()
        self.run_script(TEARDOWN, "--purge-gitignore", self.repo)
        with open(os.path.join(self.repo, ".gitignore")) as f:
            self.assertNotIn(".claude/settings.local.json", f.read())

    def test_a_second_run_is_a_no_op(self):
        self.install()
        self.run_script(TEARDOWN, self.repo)
        done = self.run_script(TEARDOWN, self.repo)
        self.assertEqual(done.returncode, 0)
        self.assertNotIn("removed", done.stdout)

    def test_setup_works_again_afterwards(self):
        self.install()
        self.run_script(TEARDOWN, self.repo)
        done = self.run_script(INSTALL, self.repo)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(len(hooks_in(self.local)["SessionStart"]), 1)

    def test_an_unknown_option_is_refused(self):
        done = self.run_script(TEARDOWN, "--nonsense", self.repo,
                               expect_success=False)
        self.assertEqual(done.returncode, 2)

    def test_help_explains_itself_without_a_target(self):
        done = self.run_script(TEARDOWN, "--help")
        self.assertEqual(done.returncode, 0)
        self.assertIn("Remove llm_chat from a repo", done.stdout)


class GitignoreTest(ShellTestCase):
    """Issue #8: the ignore guard asked ONE FILE whether a path was ignored.

    A repo that ignores these anywhere else — `.git/info/exclude` is the
    git-provided place for per-developer tooling — looked un-ignored, so every
    re-install appended the lines again. And re-installing is not optional: a
    changed hook makes the wiring stale and llm_chat itself demands it. Twice
    in one day, in a shared monorepo, with a human reverting the file each
    time.
    """

    def install(self, *extra):
        self.run_script(INSTALL, self.repo, *extra)

    def gitignore(self):
        path = os.path.join(self.repo, ".gitignore")
        if not os.path.isfile(path):
            return ""
        with open(path) as f:
            return f.read()

    def exclude(self, *lines):
        path = os.path.join(self.repo, ".git", "info", "exclude")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            f.write("\n".join(lines) + "\n")

    def test_a_fresh_repo_still_gets_its_entries(self):
        self.install()
        self.assertIn(".llm_chat/", self.gitignore())

    def test_A_PATH_IGNORED_VIA_GIT_INFO_EXCLUDE_IS_NOT_RE_ADDED(self):
        """The report, exactly. git check-ignore consults .gitignore,
        .git/info/exclude and core.excludesFile together — the same
        resolution git itself uses, so the guard matches the question."""
        self.exclude(".llm_chat/", ".claude/settings.local.json")
        self.install()
        self.assertEqual(self.gitignore().strip(), "")

    def test_re_installing_does_not_append_a_second_time(self):
        """Idempotence, which the old grep did give for .gitignore itself —
        kept, because the new check must not lose it."""
        self.install()
        first = self.gitignore()
        self.install()
        self.assertEqual(self.gitignore(), first)

    def test_no_gitignore_writes_NOTHING_to_the_repo(self):
        """Writing to a shared repo's .gitignore is that repo's decision."""
        self.run_script(INSTALL, self.repo, "--no-gitignore")
        self.assertEqual(self.gitignore().strip(), "")

    def test_DECLINING_IS_NOT_SILENT(self):
        """The protection being declined is real, so the operator has to be
        choosing an unignored state rather than stumbling into one."""
        done = self.run_script(INSTALL, self.repo, "--no-gitignore")
        self.assertIn("NOT IGNORED", done.stdout)
        self.assertIn(".llm_chat/", done.stdout)
        self.assertIn(".git/info/exclude", done.stdout,
                      "must name the place that prevents a commit without "
                      "telling the whole repo about one developer's tools")

    def test_no_gitignore_says_nothing_when_they_are_ALREADY_ignored(self):
        """A warning that fires when there is nothing to warn about is the
        kind that gets ignored when there is."""
        self.exclude(".llm_chat/", ".claude/settings.local.json")
        done = self.run_script(INSTALL, self.repo, "--no-gitignore")
        self.assertNotIn("NOT IGNORED", done.stdout)

    def test_COULD_NOT_TELL_IS_NOT_NOT_IGNORED(self):
        """#34 from #learnings: a check that fails safe collides its safe
        answer with a real one. `git check-ignore` exits 128 when it cannot
        answer — no git, or not a repository — and folding that into "not
        ignored" silently re-appends on EVERY run, which is the bug this
        replaced, recreated for whoever has no git."""
        plain = os.path.join(self.tmp.name, "not-a-repo")
        os.makedirs(plain)
        done = self.run_script(INSTALL, plain)
        self.assertIn("could not ask git", done.stdout)
        self.assertIn("safe direction", done.stdout)

    def test_a_real_repo_says_nothing_of_the_sort(self):
        """Paired. The note must not fire where git answered fine, or it is
        noise on every install and stops being read."""
        done = self.run_script(INSTALL, self.repo)
        self.assertNotIn("could not ask git", done.stdout)

    def test_an_unknown_option_is_refused_rather_than_taken_as_the_target(self):
        """Otherwise a typo'd flag installs into a directory named after it."""
        done = self.run_script(INSTALL, self.repo, "--nonsense",
                               expect_success=False)
        self.assertEqual(done.returncode, 2)


@unittest.skipUnless(HAS_CLAUDE, "requires the `claude` CLI on PATH")
class McpRegistrationTest(ShellTestCase):
    """The one thing worth spawning the real CLI for: does install.sh
    actually register a working server, does re-running converge rather than
    duplicate, and does teardown undo it. Everything else in InstallTest and
    TeardownTest runs with `claude` off PATH on purpose — see
    `_path_without_claude`."""

    def test_install_registers_the_mcp_server(self):
        self.run_script(INSTALL, self.repo, allow_claude=True)
        self.assertTrue(self.mcp_registered())

    def test_re_running_install_leaves_exactly_one_registration(self):
        self.run_script(INSTALL, self.repo, allow_claude=True)
        self.run_script(INSTALL, self.repo, allow_claude=True)
        with open(os.path.join(self.fake_home, ".claude.json")) as f:
            data = json.load(f)
        count = sum(1 for proj in data.get("projects", {}).values()
                    if "llm_chat" in (proj.get("mcpServers") or {}))
        self.assertEqual(count, 1)

    def test_a_missing_claude_binary_does_not_fail_the_install(self):
        """The MCP server is a convenience on top of the hooks, not a
        requirement — an install must still succeed without it."""
        done = self.run_script(INSTALL, self.repo, allow_claude=False)
        self.assertEqual(done.returncode, 0)
        self.assertFalse(self.mcp_registered())

    def test_teardown_unregisters_the_mcp_server(self):
        self.run_script(INSTALL, self.repo, allow_claude=True)
        self.assertTrue(self.mcp_registered())
        self.run_script(TEARDOWN, self.repo, allow_claude=True)
        self.assertFalse(self.mcp_registered())


if __name__ == "__main__":
    unittest.main()
