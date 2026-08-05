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

    def help_of(self, build):
        """Real argparse output, never a string I wrote.

        These fixtures were hand-written and did not match reality: argparse
        emits an `options:` section and a `[-h]` in usage, and mine had
        neither. A test pinned to invented help text measures my imagination —
        the same defect as inventing an event payload, one layer down. So the
        parser is built and asked.
        """
        import argparse
        parser = argparse.ArgumentParser(prog="t")
        sub = parser.add_subparsers(dest="cmd")
        target = build(sub)
        text = target.format_help()

        real = check.subprocess

        class Fake:
            @staticmethod
            def run(*a, **kw):
                class R:
                    stdout = text
                    stderr, returncode = "", 0
                return R()
        check.subprocess = Fake()
        self.addCleanup(lambda: setattr(check, "subprocess", real))
        return text

    def test_a_FLAGS_choices_are_not_mistaken_for_subcommands(self):
        """A flag's choices render identically to a subparser group. A sibling
        tool had `close` offering {holds,partial,refuted} as flag values and
        reported the ordinary command `close mytask` as a ghost.

        Their discriminator: flag choices are always attached to their flag; a
        subparser group never is. Fixed here BEFORE it fired, because a false
        positive nobody has triggered is invisible in exactly the way a false
        negative is — they are the same risk until something trips them."""
        def build(sub):
            close = sub.add_parser("close")
            close.add_argument("--why", choices=["holds", "partial"])
            close.add_argument("name")
            inner = close.add_subparsers(dest="sub")
            inner.add_parser("alpha")
            inner.add_parser("beta")
            return close
        text = self.help_of(build)
        self.assertIn("--why {holds,partial}", text)   # the trap is present
        self.assertEqual(check.verbs_from_help("t", "close"),
                         {"alpha", "beta"})

    def test_a_verb_with_ONLY_flag_choices_has_no_subcommands(self):
        """Paired: skipping flag groups must not fall through to returning the
        flag's values anyway, or the fix does nothing.

        This is the assertion that a rule stripping EVERYTHING would fail —
        its partner passes identically whether the rule is right or removes
        the lot, which is the shape a sibling agent nearly shipped."""
        def build(sub):
            say = sub.add_parser("say")
            say.add_argument("--to", choices=["a", "b"])
            say.add_argument("text")
            return say
        self.help_of(build)
        self.assertEqual(check.verbs_from_help("t", "say"), set())

    def test_the_BRACKETED_usage_form_is_covered_on_purpose(self):
        """argparse writes an optional flag two ways — `[--to {a,b}]` in the
        usage line and `--to {a,b}` in the options list — and my guard first
        handled only the second, so the bracket silently defeated it.

        Covered here deliberately rather than by a fixture that happens to
        contain both. A sibling found the same coverage in their suite existing
        only by accident of one command's help text."""
        def build(sub):
            say = sub.add_parser("say")
            say.add_argument("--to", choices=["a", "b"])
            return say
        text = self.help_of(build)
        self.assertIn("[--to {a,b}]", text)
        self.assertEqual(check.verbs_from_help("t", "say"), set())

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


class SecondWordTest(unittest.TestCase):
    """Validating only the first word leaves the rest half-checked.

    A sibling agent found `<tool> lock run` in eight remedies where `lock` was
    validated and `run` — one of five subcommands — was not. Rename `run` and
    all eight go dead while the suite stays green.

    This repo has the same exposure through `choices=`: `mode` accepts
    broadcast|ordinary and its own reversal remedy hard-codes one. Argparse
    renders a choices positional exactly like a subparser group, so the warning
    first read as a false positive and was nearly dismissed. It was a true one
    in an unfamiliar shape.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "cli.py")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, text):
        with open(self.src, "w") as f:
            f.write(text)

    def test_a_remedy_naming_NO_accepted_value_is_caught(self):
        """Asks whether the remedy names any valid choice — not whether every
        word is one. Which position holds the value cannot be known generally:
        `mode <channel> <mode>` puts a room name in between, and demanding
        every word be valid reported the room."""
        self.write('print("Reverse it:\\n  llm_chat mode room ordinary --yes")')
        self.assertEqual(
            check.stale_values(self.src, "llm_chat", {"mode": {"broadcast"}}),
            [("mode", "room ordinary")])

    def test_an_argument_that_is_not_a_value_does_not_trip_it(self):
        self.write('print("  llm_chat mode some-room broadcast --yes")')
        self.assertEqual(
            check.stale_values(self.src, "llm_chat",
                               {"mode": {"broadcast", "ordinary"}}), [])

    def test_a_value_that_is_still_accepted_is_not(self):
        """Paired: a check that flagged every second word would fire on every
        remedy in the file."""
        self.write('print("Reverse it:\\n  llm_chat mode room ordinary --yes")')
        self.assertEqual(
            check.stale_values(self.src, "llm_chat",
                               {"mode": {"broadcast", "ordinary"}}), [])

    def test_placeholders_and_flags_are_not_values(self):
        """`{name}` is filled at runtime and `--yes` is an option; neither can
        go stale the way a hard-coded value can."""
        self.write('print("  llm_chat mode {name} <x> --yes $VAR")')
        self.assertEqual(
            check.stale_values(self.src, "llm_chat", {"mode": {"broadcast"}}),
            [])

    def test_a_verb_with_no_choice_set_is_left_alone(self):
        """`llm_chat reopen deploy-review` names a CHANNEL, not a value.
        Checking it would report every room name in every example."""
        self.write('print("  llm_chat reopen deploy-review")')
        self.assertEqual(
            check.stale_values(self.src, "llm_chat", {"mode": {"broadcast"}}),
            [])

    def test_bare_words_ignores_a_line_that_is_not_a_remedy(self):
        self.assertEqual(check.bare_words("no llm_chat server here", "llm_chat"),
                         (None, []))

    def test_an_unparseable_source_is_not_a_crash(self):
        self.write("def broken(:\n")
        self.assertEqual(
            check.stale_values(self.src, "llm_chat", {"mode": {"x"}}), [])

    def test_A_REMEDY_SPLIT_ACROSS_F_STRINGS_IS_NOT_CHECKED(self):
        """The limit, pinned as an assertion so it cannot rot into a surprise.

        This project's own `mode` reversal is built as
            f"...llm_chat mode {name} " + f"{'ordinary' if want else ...}"
        so the value lives in a different AST node from the verb. It is real,
        hard-coded, and unchecked — which is why the run reports PARTLY CHECKED
        rather than clean. A green immediately after tightening a check is when
        it is most likely to have become silence."""
        self.write('x = f"  llm_chat mode {n} " + f"{\'ordinary\'}"')
        self.assertEqual(
            check.stale_values(self.src, "llm_chat", {"mode": {"broadcast"}}),
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

    def test_second_word_drift_reaches_the_report(self):
        """End to end. A helper nobody calls catches nothing, and this one is
        reached through a --help probe that could silently return no choices."""
        self.write_doc("mode sync --to-all --yes")
        # A VALID module. SOURCE is an indented fragment, so appending to it
        # made the file unparseable — and stale_values returns [] on a parse
        # failure, so the test failed by reporting nothing rather than by
        # erroring. A fixture that cannot parse is a check that cannot fire.
        with open(os.path.join(self.tmp.name, "cli.py"), "w") as f:
            f.write('sub.add_parser("mode")\n'
                    'print("fix it:\\n  llm_chat mode room gone-value")\n')
        real = check.verbs_from_help
        check.verbs_from_help = (
            lambda path, verb=None: {"broadcast"} if verb == "mode" else set())
        try:
            _, out = self.run_check()
        finally:
            check.verbs_from_help = real
        self.assertIn("SECOND-WORD DRIFT", out)
        self.assertIn("gone-value", out)

    def test_a_verb_with_a_second_word_is_reported_as_PARTLY_checked(self):
        """Coverage stated whether or not anything was found. A clean run right
        after tightening a check is when it is most likely to have become
        silence — this repo's own `mode` remedy is assembled from f-string
        pieces and genuinely is not validated."""
        self.write_doc("mode sync --to-all --yes")
        real = check.nested
        check.nested = lambda path, verbs: ["mode"]
        try:
            _, out = self.run_check()
        finally:
            check.nested = real
        self.assertIn("PARTLY CHECKED", out)
        self.assertIn("f-string", out)

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
