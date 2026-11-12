"""`llm_chat version`: what is installed, machine-readably.

Asked for by showrunner-code-owner (2026-11-12): SRCode shows, per agent,
whether its tools are current and sends a stock "upgrade" message. Nothing
answered "which llm_chat is this repo on": a clone knows its commit through
git, a lamp copy through `.lamp-wish.json`, and neither was reported.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import load  # noqa: E402

cli = load("llm_chat")


class CommitOfTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tree = self.tmp.name

    def git(self, *args):
        subprocess.run(["git", "-C", self.tree] + list(args), check=True,
                       capture_output=True,
                       env=dict(os.environ, GIT_AUTHOR_NAME="t",
                                GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                                GIT_COMMITTER_EMAIL="t@t"))

    def test_a_CLONE_reports_its_HEAD(self):
        self.git("init", "-q")
        with open(os.path.join(self.tree, "f"), "w") as f:
            f.write("x")
        self.git("add", "f")
        self.git("commit", "-q", "-m", "one")
        head = subprocess.run(["git", "-C", self.tree, "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        found = cli.commit_of(self.tree)
        self.assertEqual((found["commit"], found["source"], found["release"]),
                         (head, "git", None))
        self.assertIs(found["dirty"], False)

    def test_a_LAMP_copy_reports_the_commit_and_release_it_was_vendored_at(self):
        with open(os.path.join(self.tree, ".lamp-wish.json"), "w") as f:
            json.dump({"geanie": "llm_chat", "wish": 66, "sha": "c94ab49"}, f)
        found = cli.commit_of(self.tree)
        self.assertEqual((found["commit"], found["source"], found["release"]),
                         ("c94ab49", "lamp", 66))

    def test_NEITHER_is_said_rather_than_guessed(self):
        found = cli.commit_of(self.tree)
        self.assertEqual((found["commit"], found["source"]), (None, "unknown"))


class VersionReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = os.path.join(self.tmp.name, "repo")
        os.makedirs(os.path.join(self.project, ".llm_chat"))
        os.environ["CLAUDE_PROJECT_DIR"] = self.project
        self.addCleanup(os.environ.pop, "CLAUDE_PROJECT_DIR", None)
        real = cli.reads_are_limited
        cli.reads_are_limited = lambda server: None
        self.addCleanup(setattr, cli, "reads_are_limited", real)

    def stamp(self, fingerprint, checkout):
        with open(os.path.join(self.project, ".llm_chat", "installed.json"),
                  "w") as f:
            json.dump({"fingerprint": fingerprint, "checkout": checkout}, f)

    def report(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.do_version("http://localhost:7717", as_json=True)
        self.assertEqual(code, 0)
        return json.loads(out.getvalue())

    def test_an_UNWIRED_repo_is_told_how_to_wire_it(self):
        found = self.report()
        self.assertIs(found["wired"], False)
        self.assertIn("install.sh", found["rewire_command"])

    def test_a_repo_wired_from_THIS_copy_needs_no_rewire_even_if_stamped_old(self):
        """doctor's rule: its hooks are absolute paths into this checkout, so
        it already runs the current scripts."""
        self.stamp("old", cli.ROOT)
        found = self.report()
        self.assertIs(found["wired"], True)
        self.assertIs(found["rewire_needed"], False)
        self.assertEqual(found["copy"], os.path.abspath(cli.ROOT))

    def test_a_repo_wired_from_ANOTHER_copy_with_an_old_stamp_needs_one(self):
        other = os.path.join(self.tmp.name, "vendored")
        os.makedirs(os.path.join(other, "bin"))
        self.stamp("old", other)
        found = self.report()
        self.assertIs(found["rewire_needed"], True)
        self.assertEqual(found["rewire_command"],
                         "%s %s" % (os.path.join(other, "install.sh"),
                                    self.project))
        self.assertEqual(found["copy"], other)

    def test_a_current_stamp_from_another_copy_needs_none(self):
        other = os.path.join(self.tmp.name, "vendored")
        os.makedirs(os.path.join(other, "bin"))
        self.stamp(cli.wiring_fingerprint(other), other)
        self.assertIs(self.report()["rewire_needed"], False)

    def test_CANNOT_TELL_about_the_server_is_null_not_false(self):
        self.assertIsNone(self.report()["server_restart_needed"])

    def test_an_older_server_is_said(self):
        cli.reads_are_limited = lambda server: True
        self.assertIs(self.report()["server_restart_needed"], True)


if __name__ == "__main__":
    unittest.main()
