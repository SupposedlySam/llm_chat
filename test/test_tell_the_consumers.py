"""The trigger that turns "4 consumers are behind" into telling them.

The REFUSED cases matter more than the firing one here. This is a PostToolUse
hook, so it runs after every single Bash call — a version that fires on prose,
or fires twice for the same news, becomes noise faster than it becomes useful,
and the learning it was built from says exactly that.

The publish output below is VERBATIM from a real `lamp publish` in this
repo, not a shape invented to match the regex.
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import load, parsed  # noqa: E402

guard = load("triggers/tell-the-consumers")

REAL = """🧞 llm_chat #36 granted — 7126b91
4 lamp consumer(s) are now behind:
  /Users/supposedlysam/dev/game_loop  #33 → #36
  /Users/supposedlysam/dev/lamp  #33 → #36
  /Users/supposedlysam/dev/showrunner  #35 → #36
  /Users/supposedlysam/dev/wholesale-command-station  #35 → #36
   Only projects that ran `lamp add` are counted.
"""


class DetectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.real_state = guard.STATE
        guard.STATE = os.path.join(self.tmp.name, "consumers-told")

    def tearDown(self):
        guard.STATE = self.real_state
        self.tmp.cleanup()

    def test_a_real_publish_report_is_recognised(self):
        behind = guard.left_behind(REAL)
        self.assertEqual(len(behind), 4)
        self.assertIn("/Users/supposedlysam/dev/showrunner",
                      [p for p, _, _ in behind])

    def test_a_command_that_printed_NOTHING_is_not_a_report(self):
        """Most Bash calls produce no output at all, and this runs after every
        one of them."""
        self.assertEqual(guard.left_behind(""), [])
        self.assertEqual(guard.left_behind("\n"), [])

    def test_ZERO_BEHIND_SAYS_NOTHING(self):
        """The good outcome. A trigger that congratulates you on it is noise."""
        self.assertEqual(guard.left_behind(
            "0 lamp consumer(s) are now behind:\n"), [])

    def test_PROSE_QUOTING_THE_PHRASE_DOES_NOT_FIRE(self):
        """Reading a publish log with grep prints this sentence, and so does
        this project's own summary of it. The count line ALONE is not the
        report — the checkout lines with version arrows are what make it one,
        and requiring both is the difference between a trigger and a nag."""
        for prose in (
            "Four lamp consumer(s) are now behind, worth telling them.",
            "4 lamp consumer(s) are now behind:",
            "I noted that 4 lamp consumer(s) are now behind and moved on.",
        ):
            with self.subTest(prose=prose):
                self.assertEqual(guard.left_behind(prose), [])

    def test_this_triggers_OWN_SOURCE_does_not_set_it_off(self):
        """It quotes the report twice, in a docstring. The sibling guard
        refused its own commit message for the same reason and the lesson was
        already written down one file over."""
        with open(os.path.join(os.path.dirname(__file__), "..", "triggers",
                               "tell-the-consumers")) as f:
            self.assertEqual(guard.left_behind(f.read()), [])

    def test_it_reads_STDERR_as_well_as_stdout(self):
        """Which stream a publisher reports on is not worth a claim."""
        text = guard.output_of({"tool_response": {"stdout": "",
                                                  "stderr": REAL}})
        self.assertEqual(len(guard.left_behind(text)), 4)

    def test_a_payload_with_no_tool_response_is_not_a_crash(self):
        self.assertEqual(guard.output_of({}), "")
        self.assertEqual(guard.output_of({"tool_response": "not a dict"}), "")

    def test_THE_SAME_NEWS_IS_NOT_ANNOUNCED_TWICE(self):
        """Publishing again with nobody having upgraded is not new
        information. A nudge on every run is the one you stop seeing — and the
        more disciplined you are about reading output, the faster."""
        behind = guard.left_behind(REAL)
        self.assertFalse(guard.already_told(behind))
        self.assertTrue(guard.already_told(behind))

    def test_a_DIFFERENT_set_is_news_again(self):
        """Paired, and the half that makes it a memory rather than a latch:
        one consumer upgrading changes the set, and the rest still need
        telling."""
        guard.already_told(guard.left_behind(REAL))
        fewer = guard.left_behind(REAL.replace(
            "  /Users/supposedlysam/dev/lamp  #33 → #36\n", ""))
        self.assertFalse(guard.already_told(fewer))

    def test_an_unwritable_state_file_still_announces(self):
        """Bookkeeping must never swallow the message it exists to schedule."""
        guard.STATE = "/proc/nope/deeper/consumers-told"
        self.assertFalse(guard.already_told(guard.left_behind(REAL)))

    def test_an_ASCII_arrow_is_the_same_report(self):
        self.assertEqual(len(guard.left_behind(REAL.replace("→", "->"))), 4)


class NoticeTest(unittest.TestCase):
    def test_it_names_the_checkouts_by_repo(self):
        text = guard.notice(guard.left_behind(REAL))
        self.assertIn("showrunner", text)
        self.assertIn("#35 → #36", text)

    def test_IT_SAYS_A_SUMMARY_TO_THE_HUMAN_IS_NOT_TELLING_THEM(self):
        """The actual failure. I read the report, wrote 'four consumers are
        behind' in my summary, and moved on — three times, across releases
        that fixed message loss."""
        text = guard.notice(guard.left_behind(REAL))
        self.assertIn("summary", text)
        self.assertIn("is not telling them", text)

    def test_it_points_at_the_lookup_rather_than_guessing_a_room(self):
        """A confidently wrong room name sends the message somewhere nobody is
        listening, which is this project's oldest failure mode."""
        text = guard.notice(guard.left_behind(REAL))
        self.assertIn("llm_chat channels", text)


class HookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.real_state = guard.STATE
        guard.STATE = os.path.join(self.tmp.name, "consumers-told")

    def tearDown(self):
        guard.STATE = self.real_state
        self.tmp.cleanup()

    def invoke(self, payload):
        stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps(payload))
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                code = guard.main([])
        finally:
            sys.stdin = stdin
        return code, out.getvalue()

    def bash(self, stdout):
        return {"tool_name": "Bash", "tool_input": {"command": "publish"},
                "tool_response": {"stdout": stdout, "stderr": ""}}

    def test_a_real_report_produces_additional_context(self):
        code, out = self.invoke(self.bash(REAL))
        self.assertEqual(code, 0)
        # `parsed`, not `json.loads`: a trigger neutered into printing nothing
        # makes loads() RAISE, so this errors instead of failing and the sweep
        # can only report that something exploded. That is the crash-is-not-a-
        # measurement problem, and it showed up in the very mutation written
        # for this file.
        payload = parsed(out) or {}
        self.assertIn("older copy",
                      payload.get("hookSpecificOutput", {})
                             .get("additionalContext", ""))

    def test_it_is_SILENT_when_nothing_is_behind(self):
        """It runs after every Bash call. Silence is the default or it is
        noise."""
        code, out = self.invoke(self.bash("all consumers current"))
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_other_tools_are_not_its_business(self):
        code, out = self.invoke({"tool_name": "Write",
                                 "tool_input": {"file_path": "/x"}})
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_an_unparseable_payload_never_breaks_the_turn(self):
        stdin = sys.stdin
        sys.stdin = io.StringIO("{not json")
        try:
            self.assertEqual(guard.main([]), 0)
        finally:
            sys.stdin = stdin

    def test_it_NEVER_blocks(self):
        """A PostToolUse hook that exits non-zero interrupts the turn. This
        one is a reminder, not a gate — the work it asks for is a message, and
        stopping the agent to demand it would cost more than the delay."""
        code, _ = self.invoke(self.bash(REAL))
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
