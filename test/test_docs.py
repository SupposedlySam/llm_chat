"""Does the documentation know about what we are about to release?

The failure is quiet and universal: a feature lands, its reasoning goes into a
commit message nobody will read again, and the two files a human or an agent
actually STARTS from never hear about it. This repo shipped a week of
user-facing surface that way while writing careful paragraphs about each piece
in commits.

The check is deliberately narrow and ungameable: every subcommand and option the
CODE defines, against the docs. Prose cannot satisfy it — the name appears or it
does not. What it cannot check is the larger half, and it says so rather than
letting a green run imply otherwise: whether the docs are correct, whether they
EXPLAIN a thing or merely name it, or whether they still describe the code.
"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import load  # noqa: E402

check = load("triggers/undocumented-surface")

SOURCE = '''
    p = sub.add_parser("mode", help="x")
    p.add_argument("--to-all", action="store_true")
    p.add_argument("--yes")
    sub.add_parser("sync")
'''


class DeclaredTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "cli.py")
        with open(self.src, "w") as f:
            f.write(SOURCE)

    def tearDown(self):
        self.tmp.cleanup()

    def test_it_finds_the_verbs_the_code_defines(self):
        verbs, _ = check.declared([self.src])
        self.assertEqual(verbs, {"mode", "sync"})

    def test_it_finds_the_options_the_code_defines(self):
        _, options = check.declared([self.src])
        self.assertEqual(options, {"--to-all", "--yes"})

    def test_a_missing_source_file_is_not_a_crash(self):
        """The trigger runs on somebody else's schedule. Dying inside a retro
        because a path moved is worse than reporting nothing."""
        self.assertEqual(check.declared(["/no/such/file"]), (set(), set()))


class UndocumentedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.doc = os.path.join(self.tmp.name, "README.md")
        with open(self.doc, "w") as f:
            f.write("Use `--to-all` to wake everyone. The `mode` verb converts.")

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_documented_name_is_not_reported(self):
        self.assertEqual(check.undocumented({"--to-all", "mode"}, [self.doc]),
                         [])

    def test_an_undocumented_name_IS_reported(self):
        self.assertEqual(check.undocumented({"--yes"}, [self.doc]), ["--yes"])

    def test_a_prefix_of_a_documented_flag_counts_as_documented(self):
        """`--to` appears inside `--to-all`, and demanding a standalone mention
        would report a gap that is not there. This errs toward silence: a false
        alarm trains people to ignore the check, and an ignored check is worse
        than no check."""
        self.assertEqual(check.undocumented({"--to"}, [self.doc]), [])

    def test_a_missing_doc_file_does_not_hide_the_gap(self):
        """Reading nothing must not read as 'everything is documented'."""
        self.assertEqual(check.undocumented({"--yes"}, ["/no/such/doc"]),
                         ["--yes"])

    def test_several_docs_are_searched_together(self):
        other = os.path.join(self.tmp.name, "llms.txt")
        with open(other, "w") as f:
            f.write("--yes confirms it")
        self.assertEqual(check.undocumented({"--yes"}, [self.doc, other]), [])


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        with open(os.path.join(self.tmp.name, "cli.py"), "w") as f:
            f.write(SOURCE)
        self.doc = os.path.join(self.tmp.name, "README.md")

    def tearDown(self):
        self.tmp.cleanup()

    def write_doc(self, text):
        with open(self.doc, "w") as f:
            f.write(text)

    def run_check(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = check.main(["--repo", self.tmp.name,
                               "--source", "cli.py", "--docs", "README.md"])
        return code, out.getvalue()

    def test_it_names_every_gap(self):
        self.write_doc("nothing here")
        _, out = self.run_check()
        for name in ("mode", "sync", "--to-all", "--yes"):
            self.assertIn(name, out)

    def test_a_complete_doc_says_so(self):
        self.write_doc("mode sync --to-all --yes")
        _, out = self.run_check()
        self.assertIn("every command and option", out)

    def test_it_NEVER_blocks_the_retro(self):
        """A documentation gap must not stop a chapter closing. It is loud and
        it exits 0 — the alternative is a check people disable."""
        self.write_doc("nothing here")
        code, _ = self.run_check()
        self.assertEqual(code, 0)

    def test_even_a_clean_run_disclaims_what_it_cannot_see(self):
        """A green that implies 'the docs are good' is the more expensive lie.
        A mention is the floor, not the goal."""
        self.write_doc("mode sync --to-all --yes")
        _, out = self.run_check()
        self.assertIn("floor, not the goal", out)

    def test_the_failure_says_why_it_matters(self):
        self.write_doc("nothing here")
        _, out = self.run_check()
        self.assertIn("A reader starts from those files", out)


class EntryPointTest(unittest.TestCase):
    def test_it_is_executable(self):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "triggers", "undocumented-surface")
        self.assertTrue(os.access(path, os.X_OK))
