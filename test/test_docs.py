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

    def test_verbs_come_from_ARGPARSE_when_the_tool_can_be_run(self):
        """The regex misses any subcommand registered through a variable. This
        project registers `open` and `join` in a loop, so both were absent from
        the denominator and the reverse walk reported two REAL commands as
        ghosts. Asking the program is the only answer that cannot drift."""
        real = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "bin", "llm_chat")
        verbs, _ = check.declared([real])
        self.assertIn("join", verbs)
        self.assertIn("open", verbs)

    def test_a_tool_that_cannot_be_run_falls_back_to_the_regex(self):
        """A fallback that returned nothing would make an unrunnable tool look
        like a tool with no commands, which is the silent-empty-denominator
        shape this whole file is about."""
        self.assertEqual(check.verbs_from_help("/no/such/tool"), set())
        verbs, _ = check.declared([self.src])
        self.assertEqual(verbs, {"mode", "sync"})

    def test_a_tool_that_EXPLODES_is_an_empty_set_not_a_crash(self):
        """This runs inside somebody else's retro. Dying because a tool has an
        import error is worse than reporting nothing."""
        real = check.subprocess

        class Exploding:
            @staticmethod
            def run(*a, **kw):
                raise OSError("boom")
        check.subprocess = Exploding()
        try:
            self.assertEqual(check.verbs_from_help("whatever"), set())
        finally:
            check.subprocess = real

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


class InventedTest(unittest.TestCase):
    """The REVERSE walk: commands that are named but do not exist.

    The conventional check has a direction — it walks real commands asking "is
    each mentioned?" — and that direction cannot catch a remedy naming a verb
    the parser rejects. Pointed out by a sibling agent who then found a live one
    in their own repo: a refusal ending "(`showrunner campaign`)" with no
    `campaign` verb.

    That is the worst place for it. The only route to a refusal string is being
    blocked already, so the reader is the one person least able to route around
    a wrong instruction — and it can never be found by use, because nobody who
    is working ever sees a refusal.

    This repo had one: its own module docstring said the store is a zonai
    server `llm_chat serve`. There is no such verb; I hit it hours earlier,
    worked around it, and never fixed the thing that told me to run it.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "cli.py")
        self.doc = os.path.join(self.tmp.name, "README.md")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, path, text):
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_a_remedy_naming_a_command_that_does_not_exist_is_caught(self):
        """A remedy sits at the start of its own line, which is how it is told
        from a sentence. Backticks are the docs' rule; source strings are not
        markdown and do not use them."""
        self.write(self.src, 'raise SystemExit("cannot:\\n    llm_chat serve")')
        self.assertEqual(
            check.invented({"read", "say"}, [self.src], [], "llm_chat"),
            ["serve"])

    def test_a_real_command_is_not_reported(self):
        """Paired: a check that flagged every mention would be noise, and
        noise is how a check stops being read."""
        self.write(self.src, 'print("run it:\\n    llm_chat read <room>")')
        self.assertEqual(
            check.invented({"read"}, [self.src], [], "llm_chat"), [])

    def test_PROSE_INSIDE_A_STRING_IS_NOT_A_COMMAND(self):
        """The false positives the first AST version produced. Matching
        anywhere inside a literal cannot tell an instruction from a sentence
        that happens to contain the tool's name — `no llm_chat server at {url}`
        and `this llm_chat checkout is stale` were both reported as ghosts."""
        self.write(self.src, 'print("no llm_chat server at x")\n'
                             'print("this llm_chat checkout is stale")')
        self.assertEqual(
            check.invented({"read"}, [self.src], [], "llm_chat"), [])

    def test_a_remedy_the_OLD_backtick_rule_could_not_see_is_now_seen(self):
        """The denominator question, which came from a sibling agent asking
        what their own rule could not SEE. Fifteen real verbs in this repo sat
        in unbackticked remedies — not failing, ABSENT, and absent reads
        exactly like correct until one of them is renamed."""
        self.write(self.src, 'print("reopen it:\\n  llm_chat ghostverb x")')
        self.assertEqual(
            check.invented({"read"}, [self.src], [], "llm_chat"),
            ["ghostverb"])

    def test_it_catches_one_named_only_in_the_DOCS(self):
        self.write(self.doc, "Use `llm_chat campaign` to track it.")
        self.assertEqual(
            check.invented({"read"}, [], [self.doc], "llm_chat"),
            ["campaign"])

    def test_a_fenced_block_in_docs_counts(self):
        self.write(self.doc, "Run it:\n\n    llm_chat ghost --now\n")
        self.assertEqual(
            check.invented({"read"}, [], [self.doc], "llm_chat"), ["ghost"])

    def test_A_DOCSTRING_IS_NEVER_A_COMMAND(self):
        """The original false positive. Four spaces means 'code block' in
        markdown and 'docstring text' in Python; applying the markdown rule to
        source reported `llm_chat instead` out of the sentence 'a consumer
        vendored llm_chat instead of pointing at a sibling clone'.

        The discriminator was never the file extension — it is what a string is
        FOR, which the AST already knows. Remedy text is a literal; explanatory
        prose is a docstring."""
        self.write(self.src, '"""\n    a consumer vendored llm_chat instead of'
                             ' pointing at a clone\n"""')
        self.assertEqual(
            check.invented({"read"}, [self.src], [], "llm_chat"), [])

    def test_prose_outside_backticks_is_never_a_command(self):
        """Their implementation note, taken whole. Their first version told
        commands from prose with a denylist of English words and grew by eight
        entries on its first run — a denylist tracks the LANGUAGE rather than
        the code, so it grows forever. A positional rule does not."""
        self.write(self.doc, "llm_chat is a chat room and llm_chat does that.")
        self.assertEqual(
            check.invented({"read"}, [], [self.doc], "llm_chat"), [])

    def test_a_trailing_flag_is_not_mistaken_for_a_subcommand(self):
        self.write(self.doc, "`llm_chat --help`")
        self.assertEqual(
            check.invented({"read"}, [], [self.doc], "llm_chat"), [])

    def test_a_path_prefixed_invocation_still_counts(self):
        """Docs name it by absolute path, because agents run it from elsewhere.
        Missing those would blind the check to most real usage."""
        self.write(self.doc, "`./bin/llm_chat ghost`")
        self.assertEqual(
            check.invented({"read"}, [], [self.doc], "llm_chat"), ["ghost"])

    def test_a_missing_file_is_not_a_crash(self):
        self.assertEqual(
            check.invented({"read"}, ["/no/such"], ["/nor/this"], "llm_chat"),
            [])


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

    def test_a_ghost_command_is_reported_by_the_runner_too(self):
        """End to end, not just the helper — the report has to reach a reader,
        and a function nobody calls catches nothing."""
        self.write_doc("mode sync --to-all --yes and also `llm_chat ghost`")
        _, out = self.run_check()
        self.assertIn("NAMED BUT NOT REAL", out)
        self.assertIn("ghost", out)
        self.assertIn("least able to route around it", out)

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
