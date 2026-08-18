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

    def throttled(self, rooms=("ops",)):
        return {"owed": [], "unreachable": [
            {"room": r, "why": "HTTP 429  Rate limit exceeded",
             "rate_limited": True} for r in rooms]}

    def answers(self, *results):
        """Successive `owed` results, so a retry can be observed."""
        seen = iter(results)
        self.slept = []
        real = gate.time.sleep
        gate.time.sleep = lambda s: self.slept.append(s)
        self.addCleanup(lambda: setattr(gate.time, "sleep", real))
        gate.owed = lambda argv=None: next(seen)

    def test_A_TRANSIENT_429_IS_RETRIED_rather_than_blocking(self):
        """Issue #18. Every room 429'd and the identical check returned
        "nothing owed" twenty seconds later — so the correct answer was
        available the whole time, one retry away.

        The block itself was not the expensive part. The only exit on offer
        was the bypass, which makes typing it the cheapest way to clear a
        transient — and an agent that has typed it for a transient will type
        it for a real outage. A gate that cries wolf teaches its own
        bypass."""
        self.answers((2, self.throttled()), (0, {"owed": []}))
        code, text = self.run_gate()
        self.assertEqual(code, 0, "a transient must not end the turn")
        self.assertEqual(text, "")
        self.assertEqual(self.slept, [gate.RETRY_WAIT])

    def test_a_429_THAT_PERSISTS_still_blocks_and_says_it_retried(self):
        """The half that keeps the gate a gate. If waiting did not help, this
        is not a passing spike, and the reader needs to know the difference —
        the responses are opposite."""
        self.answers(*[(2, self.throttled())] * gate.RETRIES)
        code, text = self.run_gate()
        self.assertEqual(code, 2)
        self.assertIn("Retried", text)
        self.assertIn("did not clear", text)
        self.assertEqual(len(self.slept), gate.RETRIES - 1)

    def test_A_REFUSED_CONNECTION_IS_NOT_RETRIED(self):
        """Waiting does not start a server. Retrying an outage would only
        delay the turn-end that correctly refuses."""
        self.answer(2, {"owed": [], "unreachable": [
            {"room": "ops", "why": "no llm_chat server",
             "rate_limited": False}]})
        code, text = self.run_gate()
        self.assertEqual(code, 2)
        self.assertNotIn("Retried", text)

    def test_a_MIXED_failure_is_not_retried_either(self):
        """One refused connection among the 429s means waiting will not fix
        it, so the retry is reserved for the case where every room said the
        same 'later'."""
        self.assertFalse(gate.all_throttled([
            {"room": "a", "rate_limited": True},
            {"room": "b", "rate_limited": False}]))

    def test_an_EMPTY_unreachable_list_is_not_all_throttled(self):
        """`all()` says True for an empty list, which would retry a failure
        that named no rooms at all — nothing to reason about is not the same
        as everything being transient."""
        self.assertFalse(gate.all_throttled([]))
        self.assertFalse(gate.all_throttled(None))

    def test_IT_OFFERS_A_WAY_OUT_THAT_DOES_NOT_WAKE_ANYBODY(self):
        """Issue #15. This gate used to print `leave` as the remedy for having
        nothing to add, which is the one move that costs a headless agent its
        waker — a session that leaves and then stops with work outstanding
        becomes unreachable, and a Crawler's verdict had to be recovered from
        a transcript because of it.

        The leaf was squeezed three ways: the gate demanded an answer, the
        etiquette forbids trivial acknowledgements because every message wakes
        the room, and the remedy on offer was the trap. `--to-none` is the
        move that satisfies all three, and its discharge is proved in
        test_rooms."""
        self.answer(1, DEBT)
        _, text = self.run_gate()
        self.assertIn("--to-none", text)
        # The ROOM, not a placeholder: an agent reading this is mid-turn and
        # under a gate, and a substitution step is where turns get abandoned.
        self.assertIn("llm_chat say ops \"done:", text)

    def test_the_non_waking_line_survives_an_EMPTY_debt_list(self):
        """`owed` and the payload can disagree — the gate blocks on the exit
        code, and a formatter that indexes [0] of an empty list would crash
        the Stop hook it is trying to be helpful in."""
        self.answer(1, {"owed": [], "unreachable": []})
        code, text = self.run_gate()
        self.assertEqual(code, 2)
        self.assertIn("<room>", text)

    def test_IT_SAYS_WHAT_LEAVING_COSTS(self):
        """It still offers `leave`, because finishing with a room is real —
        but an agent choosing it should know it stands down its own waker."""
        self.answer(1, DEBT)
        _, text = self.run_gate()
        self.assertIn("stands down your waker", text)
        self.assertIn("unreachable", text)

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
