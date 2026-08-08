"""The guard that refuses to read a verdict through `tail` or `head`.

Every REFUSED case below is a command that actually ran in this repo and cost
something: a failing suite reported to a human as a clean gate, and a publish
failure whose reason was discarded by the same command that asked for it.

Every ALLOWED case is a shape that must stay silent, because a check that fires
on `git log | tail -3` gets routed around within the hour and then catches
nothing at all.
"""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import load  # noqa: E402

guard = load("triggers/piped-verdict")

# Real commands from this session, each of which reported the wrong thing.
REFUSED = (
    "python3 test/run.py --min 100 2>&1 | tail -20",
    "lamp publish 2>&1 | tail -25",
    "./.game_loop/bin/verify | tail -3",
    "python3 test/mutate.py | head -30",
    "cd test && python3 -m unittest test_mcp 2>&1 | tail -15",
)

# Shapes that must not fire. The remedy itself is in here on purpose: a guard
# that refuses its own advice is worse than none.
ALLOWED = (
    "git log --oneline | tail -3",
    "grep -n foo bar.py | head -20",
    "tail -50 /tmp/out.log",
    "ls -la | head",
    "python3 test/run.py > /tmp/o.log 2>&1; echo EXIT=$?",
    "grep -nE 'FAIL|Traceback' /tmp/o.log | head -20",
    # THE COMPOUND LINE, which is what actually gets typed. The two idioms
    # above were listed separately and both passed; welded together by a `;`
    # the first version refused them, because it read `2>&1` from command one
    # and `| head` from command three as one offence. It fired on the remedy
    # it recommends, within a minute of being registered.
    "./.game_loop/bin/verify > /tmp/v.log 2>&1; echo \"EXIT=$?\"; "
    "grep -iE 'unaccounted|SURVIVED' /tmp/v.log | head",
    "python3 test/run.py > /tmp/o.log 2>&1 && grep -n FAIL /tmp/o.log | head -5",
    # NAMING a verdict command is not RUNNING one. Both of these were refused
    # by the first version, which matched the filename in argument position.
    "grep -n 'write-through-interpreter:' -A 3 test/mutate.py | head -14",
    "cat test/run.py | head -40",
    "wc -l test/run.py test/mutate.py | tail -1",
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

    def test_MERGED_STDERR_is_the_offence_even_for_an_unlisted_command(self):
        """The rule that does not depend on a list staying complete. Routing
        error output into something whose job is to discard most of its input
        is wrong whatever produced it."""
        kind, _ = guard.offence("some-new-tool 2>&1 | tail -5")
        self.assertEqual(kind, "stderr")

    def test_a_verdict_command_is_caught_WITHOUT_merged_stderr(self):
        """The exit-status half. `cmd | tail` reports tail's status, and tail
        almost never fails, so the verdict is lost even with stderr left
        alone."""
        kind, _ = guard.offence("pytest | tail -5")
        self.assertEqual(kind, "verdict")

    def test_bash_pipe_ampersand_is_the_same_offence_spelled_shorter(self):
        """`|&` IS merge-stderr-and-pipe. Matching only the literal 2>&1 would
        leave the shorter spelling open."""
        self.assertIsNotNone(guard.offence("pytest |& head"))

    def test_a_pager_that_is_not_LAST_is_not_truncating_the_verdict(self):
        """`tail -5 out.log | grep x` reads a file and filters it — nothing is
        being run for a verdict, and the pipeline does not end in a pager."""
        self.assertIsNone(guard.offence("tail -5 out.log | grep FAIL"))

    def test_a_verdict_named_AFTER_the_pager_is_not_being_truncated_by_it(self):
        """Only what is upstream counts. Here the pager feeds the test runner
        rather than swallowing it."""
        self.assertIsNone(guard.offence("head -1 list.txt | xargs pytest"))

    def test_A_SEPARATOR_ENDS_THE_PIPELINE_so_stderr_does_not_leak_forward(self):
        """Named on its own because it is the failure this guard shipped with.

        `2>&1` on command one and `| head` on command three are not the same
        pipeline. Reading across the `;` made the recommended remedy an
        offence — a guard that refuses its own advice is worse than no guard,
        because the first thing anyone does is disable it."""
        remedy = ("verify > /tmp/v.log 2>&1; echo \"EXIT=$?\"; "
                  "grep -n FAIL /tmp/v.log | head")
        self.assertIsNone(guard.offence(remedy))

    def test_stderr_merged_IN_THE_SAME_pipeline_still_fires(self):
        """Paired with it. Scoping to the segment must not become a hole: the
        offence is still an offence when it is genuinely one command."""
        self.assertIsNotNone(guard.offence("echo hi; lamp publish 2>&1 | tail -5"))

    def test_a_verdict_NAMED_in_argument_position_is_not_being_run(self):
        """The second false positive this shipped with, and the same mistake
        this repo already corrected once in the remedy counter: five of twelve
        "remedies" there were prose that merely mentioned a command. Asking
        WHERE the name appears is the fix in both places."""
        self.assertIsNone(
            guard.offence("grep -n foo test/mutate.py | head -14"))

    def test_the_same_name_in_COMMAND_position_still_fires(self):
        """Paired. The command-position rule must not become a way through."""
        self.assertIsNotNone(guard.offence("python3 test/mutate.py | head -14"))

    def test_a_runner_puts_the_verdict_in_SECOND_position(self):
        """`python3 test/run.py` and `python3 -m unittest x` both run a
        verdict; the first token is only the interpreter."""
        self.assertIsNotNone(guard.offence("python3 test/run.py | tail -5"))
        self.assertIsNotNone(
            guard.offence("python3 -m unittest test_mcp | tail -5"))

    def test_PROSE_IN_A_HEREDOC_IS_NOT_A_COMMAND(self):
        """The third false positive, and the one whose lesson was already
        written down one file over: a commit message DESCRIBING the offence is
        not the offence. This guard refused its own commit message, which
        quoted the two pipelines it exists to stop.

        A guard that cannot describe itself in a commit message is a guard
        whose reasons never get written down."""
        commit = ("git commit -F - <<'MSG'\n"
                  "feat: refuse to read a verdict through tail\n\n"
                  "    python3 test/run.py --min 100 2>&1 | tail -20\n"
                  "    lamp publish 2>&1 | tail -25\n"
                  "MSG\n")
        self.assertIsNone(guard.offence(commit))

    def test_a_real_offence_AFTER_a_heredoc_still_fires(self):
        """Paired, and the reason stripping bodies is not just deleting text:
        the command continues after the delimiter, and what follows is live."""
        self.assertIsNotNone(guard.offence(
            "git commit -F - <<'MSG'\nsome message\nMSG\n"
            "python3 test/run.py 2>&1 | tail -5"))

    def test_an_unterminated_heredoc_does_not_swallow_the_rest(self):
        """It returns what it has rather than looping or raising. Odd input
        must not block every command after it."""
        self.assertIsNone(guard.offence("cat <<'EOF'\nno delimiter here\n"))

    def test_a_pager_with_NOTHING_upstream_is_not_an_offence(self):
        """A malformed or partial line still has to answer. Nothing is being
        run, so there is no verdict to lose — and a guard that raises on odd
        input blocks every command after it."""
        self.assertIsNone(guard.offence("| head -5"))
        self.assertEqual(guard.command_head(""), "")

    def test_an_empty_command_is_not_an_offence(self):
        self.assertIsNone(guard.offence(""))

    def test_tailing_a_FILE_is_the_remedy_not_the_offence(self):
        """Stated as its own case because the refusal text recommends it, and
        a guard that refuses its own advice teaches people to ignore it."""
        self.assertIsNone(guard.offence("tail -100 /tmp/publish.log"))


class RefusalTextTest(unittest.TestCase):
    def test_the_stderr_refusal_names_what_was_actually_lost(self):
        _, err = run(self, "lamp publish 2>&1 | tail -25")
        self.assertIn("REFUSED", err)
        self.assertIn("unreproducible", err)

    def test_the_verdict_refusal_explains_the_exit_status(self):
        _, err = run(self, "pytest | tail -5")
        self.assertIn("exit status is the LAST", err)

    def test_both_carry_the_two_line_remedy(self):
        """A refusal without an available alternative is just an obstacle, and
        the alternative here is short enough to paste."""
        for command in ("lamp publish 2>&1 | tail -5", "pytest | tail -5"):
            with self.subTest(command=command):
                _, err = run(self, command)
                self.assertIn("EXIT=$?", err)
                self.assertIn("grep", err)

    def test_it_names_the_escape_hatch(self):
        _, err = run(self, "pytest | tail -5")
        self.assertIn(guard.ALLOW, err)


def run(case, command):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    return HookTest.invoke(payload)


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

    def bash(self, command):
        return {"tool_name": "Bash", "tool_input": {"command": command}}

    def test_an_offence_is_blocked_with_the_reason(self):
        code, err = self.invoke(self.bash(REFUSED[0]))
        self.assertEqual(code, 2)
        self.assertIn("REFUSED", err)

    def test_a_legitimate_command_passes_silently(self):
        code, err = self.invoke(self.bash(ALLOWED[0]))
        self.assertEqual(code, 0)
        self.assertEqual(err, "")

    def test_the_escape_hatch_is_IN_THE_COMMAND_where_it_can_be_seen(self):
        """An environment variable would belong to the hook process, not to
        the command being inspected — a hatch the caller could never open.
        The sibling guard shipped exactly that and it was decorative."""
        code, _ = self.invoke(self.bash(
            "lamp publish 2>&1 | tail -25  # " + guard.ALLOW))
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
