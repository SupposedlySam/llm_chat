"""The gate that catches a turn ending by handing a decision back.

Written because being told did not work. "This was a T3 you should have
implemented on your own, not escalated to me" was said once, agreed with, and
then contradicted twice more in the same session — which is the definition of
something that needs a rail rather than a resolution.

The three CAUGHT cases below are verbatim from this repo's own history, not
invented. That is the point: a guard tuned on imagined failures catches
imagined failures.
"""
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import load  # noqa: E402

gate = load("triggers/authority-gate")

# Verbatim, from turns that ended the loop and made a human type "yes".
REAL_ESCALATIONS = [
    "I'd like to fix the drift check to fingerprint the recorded `checkout` "
    "rather than assume `ROOT`, and tell lamp-owner I gave them an incomplete "
    "answer. Want me to do that now?",
    "That's gameloop's to add, and I'll raise it with them rather than keep "
    "working around it — unless you'd rather I leave that alone.",
    "Want me to post that, and separately look at whether the upgrade notice "
    "should distinguish the two?",
]

# Ordinary reporting. A gate that fires on these is one nobody leaves on.
REAL_REPORTS = [
    "Shipped f898019. 577 tests, 1387/1387 lines, 32 mutations, 0 unaccounted.",
    "What I could not establish is why the process ends, because nothing "
    "records it.",
    "Two of my own from the same hour, since you have been candid about yours.",
    "The refusal names which harm you are about to cause and how many agents "
    "it affects.",
]


class DetectionTest(unittest.TestCase):
    def test_it_catches_every_real_escalation(self):
        for text in REAL_ESCALATIONS:
            with self.subTest(text=text[:40]):
                self.assertIsNotNone(gate.asks_permission(text))

    def test_it_stays_quiet_on_ordinary_reporting(self):
        """Paired with the test above, and the more important half: a guard
        that fires on everything gets turned off, and then catches nothing."""
        for text in REAL_REPORTS:
            with self.subTest(text=text[:40]):
                self.assertIsNone(gate.asks_permission(text))

    def test_it_returns_the_phrase_so_the_objection_can_quote_it(self):
        """Naming the exact words is what makes it arguable rather than a
        scolding — you can look at them and say 'no, that one is theirs'."""
        self.assertEqual(
            gate.asks_permission("...so, shall I go ahead?").lower(), "shall i")

    def test_a_question_early_in_a_long_message_is_not_the_turn_ending(self):
        """Quoting somebody else's question, or asking and then answering it,
        is not handing a decision back."""
        text = "Should I have done that? No — here is why, and here is the fix."
        self.assertIsNone(gate.asks_permission(text + " " + "x" * 900))

    def test_empty_input_is_not_a_match(self):
        self.assertIsNone(gate.asks_permission(""))
        self.assertIsNone(gate.asks_permission(None))

    def test_it_is_case_insensitive(self):
        self.assertIsNotNone(gate.asks_permission("WANT ME TO push it?"))

    def test_it_does_not_fire_on_a_word_that_merely_contains_a_trigger(self):
        self.assertIsNone(gate.asks_permission("The marshall id is 4."))


class TranscriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, *entries):
        with open(self.path, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def test_it_reads_the_LAST_assistant_message(self):
        self.write({"type": "assistant", "message": {"content": "first"}},
                   {"type": "user", "message": {"content": "hm"}},
                   {"type": "assistant", "message": {"content": "last"}})
        self.assertEqual(gate.last_assistant_text(self.path), "last")

    def test_it_handles_the_content_block_form(self):
        self.write({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "hello"},
            {"type": "text", "text": "world"}]}})
        self.assertEqual(gate.last_assistant_text(self.path), "hello\nworld")

    def test_a_tool_only_turn_is_skipped_for_the_last_TEXT(self):
        """The final entry is often a tool call with no prose. Reading that as
        the message would make the gate silent on exactly the long working
        turns it exists for."""
        self.write({"type": "assistant", "message": {"content": "want me to?"}},
                   {"type": "assistant", "message": {"content": [
                       {"type": "tool_use", "name": "Bash"}]}})
        self.assertEqual(gate.last_assistant_text(self.path), "want me to?")

    def test_an_unreadable_transcript_is_NO_OPINION_not_a_pass(self):
        """A guard that reads a missing file as 'nothing to object to' is the
        silent-green shape this project keeps finding in its own checks."""
        self.assertEqual(gate.last_assistant_text("/no/such/file"), "")

    def test_a_corrupt_line_does_not_stop_the_search(self):
        """The corrupt line goes LAST, because the search runs backwards. My
        first version put it first, so the valid entry was found before the
        corrupt one was ever reached — a test that passed while exercising
        nothing, which coverage caught and reading did not."""
        with open(self.path, "w") as f:
            f.write(json.dumps(
                {"type": "assistant", "message": {"content": "found"}}) + "\n")
            f.write("{not json\n")
        self.assertEqual(gate.last_assistant_text(self.path), "found")

    def test_a_transcript_with_no_assistant_turns(self):
        self.write({"type": "user", "message": {"content": "hi"}})
        self.assertEqual(gate.last_assistant_text(self.path), "")


class OnceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_same_message_is_judged_once(self):
        """An objection that fires again on a message already judged turns the
        turn against itself, and there is no way out by rewording something you
        have decided is right."""
        self.assertFalse(gate.already_judged("x", self.tmp.name))
        self.assertTrue(gate.already_judged("x", self.tmp.name))

    def test_a_different_message_is_judged_on_its_own(self):
        gate.already_judged("x", self.tmp.name)
        self.assertFalse(gate.already_judged("y", self.tmp.name))

    def test_an_unwritable_state_dir_suppresses_rather_than_loops(self):
        self.assertTrue(gate.already_judged("x", "/proc/nope/deeper"))


class MainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.transcript = os.path.join(self.tmp.name, "t.jsonl")
        self.stdin = sys.stdin

    def tearDown(self):
        sys.stdin = self.stdin
        self.tmp.cleanup()

    def run_gate(self, text=None, payload=None):
        if text is not None:
            with open(self.transcript, "w") as f:
                f.write(json.dumps({"type": "assistant",
                                    "message": {"content": text}}) + "\n")
        body = json.dumps(payload if payload is not None
                          else {"transcript_path": self.transcript})
        sys.stdin = io.StringIO(body)
        err = io.StringIO()
        with redirect_stderr(err):
            code = gate.main(["--state", os.path.join(self.tmp.name, "state")])
        return code, err.getvalue()

    def test_an_escalation_blocks_the_turn_with_the_reason(self):
        """Exit 2 puts the objection back into the SAME turn, so the work
        continues instead of parking. That is the whole point — a human who
        already delegated should not have to type 'yes'."""
        code, err = self.run_gate(REAL_ESCALATIONS[0])
        self.assertEqual(code, 2)
        self.assertIn("AUTHORITY GATE", err)

    def test_the_objection_quotes_the_words_it_matched(self):
        _, err = self.run_gate("...so, want me to push it?")
        self.assertIn("want me to", err.lower())

    def test_it_asks_the_one_question_that_decides_it(self):
        """Theirs or yours. Naming which was the step being skipped."""
        _, err = self.run_gate(REAL_ESCALATIONS[1])
        self.assertIn("THEIRS", err)
        self.assertIn("credentials", err)

    def test_ordinary_reporting_passes_silently(self):
        code, err = self.run_gate(REAL_REPORTS[0])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")

    def test_it_objects_once_and_then_lets_the_turn_end(self):
        self.assertEqual(self.run_gate(REAL_ESCALATIONS[2])[0], 2)
        self.assertEqual(self.run_gate(REAL_ESCALATIONS[2])[0], 0)

    def test_an_unparseable_payload_never_blocks(self):
        sys.stdin = io.StringIO("{not json")
        self.assertEqual(gate.main([]), 0)

    def test_a_payload_with_no_transcript_never_blocks(self):
        self.assertEqual(self.run_gate(payload={})[0], 0)

    def test_it_defaults_its_state_to_the_project(self):
        os.environ["CLAUDE_PROJECT_DIR"] = self.tmp.name
        try:
            with open(self.transcript, "w") as f:
                f.write(json.dumps({"type": "assistant", "message": {
                    "content": "want me to?"}}) + "\n")
            sys.stdin = io.StringIO(json.dumps(
                {"transcript_path": self.transcript}))
            with redirect_stderr(io.StringIO()):
                self.assertEqual(gate.main([]), 2)
            self.assertTrue(os.path.isdir(
                os.path.join(self.tmp.name, ".llm_chat")))
        finally:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)


class EntryPointTest(unittest.TestCase):
    def test_it_is_executable(self):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "triggers", "authority-gate")
        self.assertTrue(os.access(path, os.X_OK))


if __name__ == "__main__":
    unittest.main()
