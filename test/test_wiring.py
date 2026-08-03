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
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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
        os.environ["CLAUDE_PROJECT_DIR"] = self.root
        self.assertEqual(cli.joined_path(),
                         os.path.join(self.root, ".llm_chat", "joined.json"))


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


if __name__ == "__main__":
    unittest.main()
