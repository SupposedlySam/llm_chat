"""Refusing file edits smuggled through an inline interpreter.

The write rail reads `Write`/`Edit` by file_path and parses `Bash` text for
shell redirects. `python3 - <<'PY' ... open(p,"w") ... PY` is neither — the tool
is Bash and the write is inside script text no parser reads. game_loop names the
gap in its own coverage report: "NONE of this sees ... an interpreter one-liner".

Measured: twenty consecutive commits in this repo were authored almost entirely
that way, during a session where the same rail DID refuse several shell
commands. So "the guard did not object" was worthless evidence throughout, while
its author was finding this exact shape in everything else.

The cases below are the real ones from that session, not invented — and the
allow-cases matter more than the refuse-cases, because a guard that blocks
`python3 test/run.py` gets turned off within the hour and then catches nothing.
"""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import load  # noqa: E402

guard = load("triggers/write-through-interpreter")

# Verbatim shapes from the session that prompted this.
SMUGGLED = [
    'python3 - <<\'PY\'\np="bin/llm_chat"\ns=open(p).read()\nopen(p,"w").write(s)\nPY',
    'python3 -c \'open("x","w").write("y")\'',
    'python3 - <<\'PY\'\nimport shutil; shutil.copy(a, b)\nPY',
    'python3 - <<\'PY\'\njson.dump(d, open(p,"w"), indent=2)\nPY',
    'python3 - <<\'PY\'\nos.replace(tmp, path)\nPY',
]

LEGITIMATE = [
    "python3 test/run.py --min 100",
    "python3 test/mutate.py",
    "python3 test/mutate.py --probe bin/llm_chat --old 'a' --new 'b'",
    'python3 -c "print(open(p).read())"',
    "git status --short",
    "./bin/llm_chat say room --file /dev/stdin <<'EOF'\nhello\nEOF",
    "./.game_loop/bin/verify",
    # PROSE THAT MENTIONS A WRITE IS NOT A WRITE. This exact command was
    # refused by the first version, one minute after it shipped: a commit
    # message quoting `open(p,"w")` inside `git commit -m "$(cat <<'MSG')"`.
    # The interpreter and the inline code have to be ADJACENT, not merely both
    # present somewhere in the line.
    'git commit -m "$(cat <<\'MSG\'\nfixed the python3 heredoc that did '
    'open(p,"w").write(s)\nMSG\n)"',
]


class DetectionTest(unittest.TestCase):
    def test_it_refuses_every_shape_actually_used(self):
        for command in SMUGGLED:
            with self.subTest(command=command[:40]):
                self.assertIsNotNone(guard.offending_write(command))

    def test_it_allows_everything_legitimate(self):
        """The half that decides whether this survives. A guard that blocks the
        test runner is one nobody leaves on."""
        for command in LEGITIMATE:
            with self.subTest(command=command[:40]):
                self.assertIsNone(guard.offending_write(command))

    def test_running_a_script_FILE_is_never_refused(self):
        """A reviewed file is not what this refuses. It refuses code that
        exists only inside a command line nobody will read again."""
        self.assertIsNone(guard.offending_write("python3 tools/rewrite.py"))

    def test_a_non_interpreter_heredoc_is_fine(self):
        self.assertIsNone(guard.offending_write("cat <<'EOF'\nplain text\nEOF"))

    def test_it_returns_the_snippet_so_the_refusal_can_quote_it(self):
        found = guard.offending_write('python3 -c \'open("x","w").write(1)\'')
        self.assertIn("open", found)

    def test_KNOWN_FALSE_POSITIVE_a_quoted_example_is_still_refused(self):
        """Recorded rather than fixed, and recorded rather than deleted.

        `echo 'python3 - <<PY ... open(p,"w") ...'` is prose, but the
        adjacency genuinely holds inside the quotes, and telling it from a real
        invocation needs shell parsing this hook has no business doing. The
        commit-message case — the one that actually bit — is handled, because
        there the interpreter and the heredoc are not adjacent.

        Left refused on purpose: the alternative is brittle quote-tracking that
        would fail open somewhere less obvious. The escape hatch covers it, and
        a limit written down as an assertion cannot rot into a surprise.
        """
        command = "echo 'python3 - <<PY / open(p,\"w\") / PY'"
        self.assertIsNotNone(guard.offending_write(command))

    def test_an_empty_command_is_not_a_match(self):
        self.assertIsNone(guard.offending_write(""))
        self.assertIsNone(guard.offending_write(None))


class HookTest(unittest.TestCase):
    def setUp(self):
        self.stdin = sys.stdin
        os.environ.pop("LLM_CHAT_ALLOW_INLINE_WRITE", None)

    def tearDown(self):
        sys.stdin = self.stdin
        os.environ.pop("LLM_CHAT_ALLOW_INLINE_WRITE", None)

    def run_hook(self, payload):
        sys.stdin = io.StringIO(json.dumps(payload))
        err = io.StringIO()
        with redirect_stderr(err):
            code = guard.main([])
        return code, err.getvalue()

    def bash(self, command):
        return {"tool_name": "Bash", "tool_input": {"command": command}}

    def test_a_smuggled_write_is_blocked_with_the_reason(self):
        code, err = self.run_hook(self.bash(SMUGGLED[0]))
        self.assertEqual(code, 2)
        self.assertIn("REFUSED", err)
        self.assertIn("Write or Edit", err)

    def test_a_legitimate_command_passes_silently(self):
        code, err = self.run_hook(self.bash(LEGITIMATE[0]))
        self.assertEqual(code, 0)
        self.assertEqual(err, "")

    def test_other_tools_are_not_its_business(self):
        """Write and Edit are already covered by file_path. Refusing them here
        would double-guard the covered path and leave this one open."""
        code, _ = self.run_hook(
            {"tool_name": "Write", "tool_input": {"file_path": "/x"}})
        self.assertEqual(code, 0)

    def test_an_unparseable_payload_never_blocks(self):
        sys.stdin = io.StringIO("{not json")
        self.assertEqual(guard.main([]), 0)

    def test_a_payload_with_no_command_never_blocks(self):
        code, _ = self.run_hook({"tool_name": "Bash", "tool_input": {}})
        self.assertEqual(code, 0)

    def test_an_explicit_escape_exists_IN_THE_COMMAND(self):
        """There has to be a way through, or the first genuine need turns the
        hook off permanently.

        It must be in the COMMAND. The first version read an environment
        variable, which belongs to the hook process and not to the command
        being inspected — so a caller could never set it and the hatch was
        decorative. Found by trying to use it. A marker in the command is also
        the honest form: visible to anyone reading what ran."""
        code, _ = self.run_hook(
            self.bash("# %s\n%s" % (guard.ALLOW, SMUGGLED[0])))
        self.assertEqual(code, 0)

    def test_the_escape_does_nothing_when_it_is_not_present(self):
        """Paired: a hatch that opens for everyone is not a hatch."""
        code, _ = self.run_hook(self.bash(SMUGGLED[0]))
        self.assertEqual(code, 2)

    def test_an_environment_variable_does_NOT_open_it(self):
        """The bug, pinned. The hook cannot see the caller's environment, so
        anything keyed on it is a door that never opens."""
        os.environ["LLM_CHAT_ALLOW_INLINE_WRITE"] = "1"
        code, _ = self.run_hook(self.bash(SMUGGLED[0]))
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
