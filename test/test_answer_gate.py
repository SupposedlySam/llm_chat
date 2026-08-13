"""The Stop hook that refuses to end a turn owing somebody an answer.

Every case below is stated as what the HUMAN experiences, because that is the
only place the failure is visible: an agent that read a question and went quiet
looks, from a phone, exactly like one that died.
"""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import load  # noqa: E402

gate = load("triggers/answer-when-asked")

DEBT = {"owed": [{"room": "ops", "from": "alice", "seq": 7, "unanswered": 1,
                  "text_preview": "which directory?"}],
        "unreachable": []}


class GateTest(unittest.TestCase):
    def setUp(self):
        self.stdin = sys.stdin
        sys.stdin = io.StringIO("")
        # RESTORED, because these stub a module-level callable. Left patched,
        # `owed` stayed a lambda for every test that ran afterwards — the
        # OwedSeamTest cases below then asserted against this file's stub
        # instead of the real function and failed for a reason that had
        # nothing to do with them. Exactly the leak run.py's detector exists
        # to catch, written into the file testing a guard about silent
        # failures.
        self.real_owed = gate.owed

    def tearDown(self):
        sys.stdin = self.stdin
        gate.owed = self.real_owed

    def answer(self, code, payload):
        gate.owed = lambda argv=None: (code, payload)

    def run_gate(self):
        err = io.StringIO()
        with redirect_stderr(err):
            result = gate.main([])
        return result, err.getvalue()

    def test_nothing_owed_lets_the_turn_end(self):
        self.answer(0, {"owed": [], "unreachable": []})
        code, text = self.run_gate()
        self.assertEqual(code, 0)
        self.assertEqual(text, "")

    def test_AN_UNANSWERED_QUESTION_BLOCKS_THE_TURN(self):
        self.answer(1, DEBT)
        code, text = self.run_gate()
        self.assertEqual(code, 2)
        self.assertIn("ANSWER FIRST", text)

    def test_it_names_the_room_the_asker_and_the_question(self):
        """A gate that says only "you owe something" makes the agent go
        looking, and the looking is where the turn gets abandoned."""
        self.answer(1, DEBT)
        _, text = self.run_gate()
        self.assertIn("#ops", text)
        self.assertIn("alice", text)
        self.assertIn("which directory?", text)

    def test_it_hands_over_the_exact_command(self):
        self.answer(1, DEBT)
        _, text = self.run_gate()
        self.assertIn('llm_chat say ops "..." --to alice', text)

    def test_it_offers_LEAVE_as_a_real_answer(self):
        """Otherwise the only way past is to say something, and an agent with
        genuinely nothing to add would be forced to generate filler."""
        self.answer(1, DEBT)
        _, text = self.run_gate()
        self.assertIn("leave", text)

    def test_COULD_NOT_LOOK_ALSO_BLOCKS(self):
        """The one that decides whether this is safe to rely on. Folding a
        failed check into "nothing owed" is the exact defect this repo just
        fixed in the Slack bridge, one layer down and on the same path."""
        self.answer(2, {"owed": [],
                        "unreachable": [{"room": "ops", "why": "server down"}]})
        code, text = self.run_gate()
        self.assertEqual(code, 2)
        self.assertIn("COULD NOT CHECK", text)
        self.assertIn("Not the same as nobody waiting", text)

    def test_the_escape_hatch_is_IN_THE_FINAL_MESSAGE(self):
        """Visible to whoever reads the transcript. An env var would be set
        upstream by something the reader never sees."""
        sys.stdin = io.StringIO("done for now " + gate.ALLOW)
        self.answer(1, DEBT)
        code, _ = self.run_gate()
        self.assertEqual(code, 0)

    def test_several_debts_are_all_named(self):
        self.answer(1, {"owed": [
            dict(DEBT["owed"][0]),
            {"room": "deploy", "from": "bob", "seq": 3, "unanswered": 4,
             "text_preview": "is it safe to ship?"}], "unreachable": []})
        _, text = self.run_gate()
        self.assertIn("#ops", text)
        self.assertIn("#deploy", text)
        self.assertIn("+3 more", text)


class OwedSeamTest(unittest.TestCase):
    """`owed` is reached by subprocess, and this hook must survive it failing
    in every way it can. A Stop hook that raises is a turn that cannot end,
    which is worse than the silence it prevents."""

    def setUp(self):
        self.real_subprocess = gate.subprocess

    def tearDown(self):
        gate.subprocess = self.real_subprocess

    def stub(self, **kw):
        gate.subprocess = type("S", (), {"run": staticmethod(
            lambda *a, **k: type("R", (), kw)())})

    def test_a_crash_reports_COULD_NOT_LOOK_rather_than_raising(self):
        def explode(*a, **k):
            raise OSError("cli missing")
        gate.subprocess = type("S", (), {"run": staticmethod(explode)})
        code, payload = gate.owed()
        self.assertEqual(code, 2)
        self.assertTrue(payload["unreachable"])

    def test_unparseable_json_does_not_raise(self):
        self.stub(returncode=1, stdout="not json", stderr="")
        code, payload = gate.owed()
        self.assertEqual(code, 1)
        self.assertEqual(payload, {})

    def test_extra_arguments_are_passed_to_the_cli(self):
        """So a project holding two identities can gate on the right one."""
        seen = {}

        def capture(command, **kw):
            seen["command"] = command

            class R:
                returncode, stdout, stderr = 0, "{}", ""
            return R()
        gate.subprocess = type("S", (), {"run": staticmethod(capture)})
        gate.owed(["--as", "builder"])
        self.assertIn("--as", seen["command"])
        self.assertIn("builder", seen["command"])

    def test_it_passes_the_exit_code_through_unchanged(self):
        """The exit code IS the answer; re-deriving it from the payload would
        give two sources for one fact."""
        self.stub(returncode=0, stdout=json.dumps({"owed": []}), stderr="")
        self.assertEqual(gate.owed()[0], 0)


if __name__ == "__main__":
    unittest.main()
