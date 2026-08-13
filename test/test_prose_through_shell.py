"""The guard that refuses prose the shell will run as code.

The REFUSED case at the top is the command that actually shipped: it posted a
public comment with a word missing, and `gh` reported success.

The ALLOWED list is longer than the refused one on purpose, and it is not
invented — it is shapes this repo types constantly, including the remedy the
refusal text recommends. A guard that fires on `echo "EXIT=$?"` would be
disabled the same hour and catch nothing after that.
"""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import load  # noqa: E402

guard = load("triggers/prose-through-shell")

# The one that ran, and two shapes one step away from it.
REFUSED = (
    'gh issue close 10 --comment "only found it because a `kill` visibly '
    'failed"',
    'llm_chat say --to build "run `test/run.py` before you push"',
    # Bare delimiter: the body is expanded exactly like a double-quoted string.
    "git commit -F - <<MSG\nfix: `server_is_current` now probes\nMSG\n",
)

# Shapes that must stay silent. Every one is real traffic from this repo.
ALLOWED = (
    'python3 test/run.py > /tmp/o.log 2>&1; echo "EXIT=$?"',
    "grep -nE 'FAIL|Error|Traceback' /tmp/o.log",
    'gh issue comment 10 --body-file /tmp/body.md',
    "git commit -q -F - <<'MSG'\nfeat: `doctor` asks what is LISTENING\nMSG\n",
    "cat > /tmp/body.md <<'BODY'\nprose with `backticks` in it\nBODY",
    'echo "no backticks here at all"',
    "python3 - <<'EOF'\nprint(\"`hi`\")\nEOF",
    # An escaped backtick is already literal — it is what someone writes when
    # they mean the character.
    'echo "a \\` on purpose"',
    # Single quotes substitute nothing, which is the other correct remedy.
    "gh issue comment 10 --body 'run `test/run.py` first'",
)


class DetectionTest(unittest.TestCase):
    def test_every_known_offence_is_caught(self):
        for command in REFUSED:
            with self.subTest(command=command):
                self.assertIsNotNone(guard.offence(command))

    def test_no_legitimate_shape_fires(self):
        for command in ALLOWED:
            with self.subTest(command=command):
                self.assertIsNone(guard.offence(command))

    def test_THE_COMMAND_THAT_ACTUALLY_SHIPPED_is_refused(self):
        """Named alone because it is the logged failure this exists for. The
        comment posted as "because a  visibly failed" and gh answered ok."""
        where, found = guard.offence(REFUSED[0])
        self.assertEqual(where, "a double-quoted argument")
        self.assertEqual(found, "`kill`")

    def test_a_QUOTED_heredoc_delimiter_is_the_remedy_not_the_offence(self):
        """<<'MSG' performs no substitution at all, and it is how every commit
        message in this repo is written. Refusing it would refuse the advice
        the refusal gives."""
        self.assertIsNone(guard.offence(
            "git commit -F - <<'MSG'\nsee `offence()` for why\nMSG\n"))

    def test_a_BARE_heredoc_delimiter_expands_its_body(self):
        """Paired with it, and the half nobody remembers: the quotes are not
        decoration. <<MSG runs what is in the body."""
        self.assertIsNotNone(guard.offence(
            "git commit -F - <<MSG\nsee `offence()` for why\nMSG\n"))

    def test_SINGLE_QUOTES_ARE_READ_FIRST_so_a_quote_inside_them_is_data(self):
        """The correctness of the scanner in one case. Inside '...' a double
        quote is an ordinary character; pairing it with a later one would
        invent a region and refuse a command that is fine."""
        self.assertIsNone(guard.offence(
            "grep 'he said \"go\"' notes.md && echo done"))

    def test_a_backtick_OUTSIDE_any_quotes_is_left_alone(self):
        """Deliberate legacy substitution is not prose. It is bare on the
        command line, where nobody arrives at it by writing markdown."""
        self.assertIsNone(guard.offence("echo `date`"))

    def test_DOLLAR_EXPANSION_IS_A_DECLARED_MISS_not_an_oversight(self):
        """Stated as a test so the blind spot cannot be quietly closed later
        without someone reading why it is open. "$500" mangles the same way,
        but `$` is deliberate in nearly every double-quoted string typed here —
        the remedy this repo recommends has "EXIT=$?" in it. Refusing that
        would get the guard routed around within the hour."""
        self.assertIsNone(guard.offence('gh issue comment 1 --body "costs $500"'))

    def test_an_unterminated_heredoc_does_not_swallow_or_hang(self):
        """Odd input must answer rather than loop, or it blocks every command
        after it."""
        self.assertIsNone(guard.offence("cat <<'EOF'\nno delimiter\n"))
        self.assertIsNotNone(guard.offence("cat <<EOF\n`whoami`\n"))

    def test_an_unbalanced_quote_does_not_run_off_the_end(self):
        self.assertIsNone(guard.offence('echo "unclosed'))

    def test_A_BACKSLASHED_QUOTE_outside_quotes_does_not_open_a_region(self):
        r"""`echo \"hi\"` has no quoted region at all — the quotes are escaped
        characters. Reading either one as an opener would pair it with the
        next quote anywhere in the command and invent a region spanning
        unrelated text, which is how a scanner starts refusing lines that have
        nothing to do with it."""
        self.assertIsNone(guard.offence(r'echo \"hi\" && date'))
        # And the escape must not become a way through: a real region after
        # one is still read.
        self.assertIsNotNone(guard.offence(r'echo \" ; gh pr comment -b "`x`"'))

    def test_a_LONE_backtick_is_still_named_in_the_refusal(self):
        """An unterminated substitution is the worse spelling — the shell
        either errors or swallows the rest of the line — so the refusal has to
        say something useful when there is no closing pair to quote."""
        found = guard.offence('gh issue comment 1 --body "see `offence for why"')
        self.assertIsNotNone(found)
        self.assertIn("offence", found[1])

    def test_an_empty_command_is_not_an_offence(self):
        self.assertIsNone(guard.offence(""))

    def test_THIS_GUARD_CAN_DESCRIBE_ITSELF_in_a_commit_message(self):
        """The sibling guard shipped unable to do this and refused its own
        commit. Here the same message is safe for a reason worth asserting
        rather than assuming: the delimiter is quoted, so the body is data.

        Two earlier versions of this test were wrong, and both errors are the
        same one. The first scanned the guard's SOURCE line by line; the
        second fed it the whole docstring. Both fired — correctly. The
        docstring quotes a `--comment "..."` with a live backtick in it, so
        read as a command it IS the offence. Prose about a command is not a
        command, and neither is a source line; only tool_input ever reaches
        this guard.

        The false-positive audit those tests were reaching for was run
        properly instead, over all 1776 Bash commands in the transcript where
        the defect happened. Exactly one fired: the one that shipped. Both
        controls are real — 1775 negatives and the known positive — but the
        transcript is not in the repo, so the number is recorded here rather
        than checked here."""
        message = ("git commit -F - <<'MSG'\n"
                   "feat: refuse prose the shell will run as code\n\n"
                   "    gh issue close 10 --comment \"because a `kill` "
                   "visibly failed\"\n\n"
                   "posted the sentence with the word missing.\n"
                   "MSG\n")
        self.assertIsNone(guard.offence(message))


class RefusalTextTest(unittest.TestCase):
    def test_it_names_the_snippet_so_the_sentence_can_be_found(self):
        _, err = run(self, REFUSED[0])
        self.assertIn("`kill`", err)

    def test_it_carries_the_body_file_remedy(self):
        _, err = run(self, REFUSED[0])
        self.assertIn("--body-file", err)

    def test_the_remedy_SAYS_the_delimiter_must_be_quoted(self):
        """The part that is actually load-bearing and the part everyone drops.
        A remedy printed with a bare delimiter would teach the same bug."""
        _, err = run(self, REFUSED[0])
        self.assertIn("<<'BODY'", err)
        self.assertIn("QUOTED delimiter", err)

    def test_it_names_the_escape_hatch(self):
        _, err = run(self, REFUSED[0])
        self.assertIn(guard.ALLOW, err)


def run(case, command):
    return HookTest.invoke({"tool_name": "Bash",
                            "tool_input": {"command": command}})


class HookTest(unittest.TestCase):
    @staticmethod
    def invoke(payload):
        stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps(payload))
        err = io.StringIO()
        try:
            with redirect_stderr(err):
                code = guard.main([])
        finally:
            sys.stdin = stdin
        return code, err.getvalue()

    def test_an_offence_is_blocked_with_the_reason(self):
        code, err = self.invoke({"tool_name": "Bash",
                                 "tool_input": {"command": REFUSED[0]}})
        self.assertEqual(code, 2)
        self.assertIn("REFUSED", err)

    def test_a_legitimate_command_passes_silently(self):
        code, err = self.invoke({"tool_name": "Bash",
                                 "tool_input": {"command": ALLOWED[0]}})
        self.assertEqual(code, 0)
        self.assertEqual(err, "")

    def test_the_escape_hatch_is_IN_THE_COMMAND_where_it_can_be_seen(self):
        code, _ = self.invoke({"tool_name": "Bash", "tool_input": {
            "command": REFUSED[0] + "  # " + guard.ALLOW}})
        self.assertEqual(code, 0)

    def test_other_tools_are_not_its_business(self):
        code, _ = self.invoke({"tool_name": "Write",
                               "tool_input": {"file_path": "/x"}})
        self.assertEqual(code, 0)

    def test_an_unparseable_payload_never_blocks(self):
        stdin = sys.stdin
        sys.stdin = io.StringIO("{not json")
        try:
            self.assertEqual(guard.main([]), 0)
        finally:
            sys.stdin = stdin

    def test_a_payload_with_no_command_never_blocks(self):
        code, _ = self.invoke({"tool_name": "Bash", "tool_input": {}})
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
