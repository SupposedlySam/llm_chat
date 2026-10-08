"""Holding a loop-shaped send for one confirmation, instead of rate limiting.

Two agents left alone thank each other, agree, and thank each other again,
and every one of those messages wakes somebody. The human who runs this
machine proposed the brake: hold a message shaped like a loop, hand the
sender a code, and let them resend it at once if it really carries something.

These pin both directions: the shapes that are held, and the ordinary work
that must never be — a gate that fires on real work is confirmed by reflex.
"""
import io
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import FakeServer, load  # noqa: E402

cli = load("llm_chat")


class AckOnlyTest(unittest.TestCase):
    def test_a_bare_acknowledgement_is_one(self):
        for text in ("thanks!", "Sounds good.", "got it, thanks",
                     "Thank you so much", "👍", "ok", "+1", "lgtm"):
            with self.subTest(text=text):
                self.assertTrue(cli.ack_only(text))

    def test_an_acknowledgement_that_SAYS_SOMETHING_is_not(self):
        for text in ("thanks, the fix is in 4a1b2c and tests pass",
                     "sounds good — but the migration still fails on eq",
                     "got it?", "Can you check the DELETE path",
                     "ok " + "word " * 20):
            with self.subTest(text=text):
                self.assertFalse(cli.ack_only(text))


def msg(seq, sender, text="a statement", audience=None, minutes_ago=0):
    return {"seq": seq, "from_identity": sender, "text": text,
            "audience": audience,
            "created_at": int((time.time() - minutes_ago * 60) * 1000)}


class LoopReasonsTest(unittest.TestCase):
    def reasons(self, text, recent, woken=("other",)):
        return cli.loop_reasons(text, "me", list(woken), recent)

    def test_an_ordinary_message_has_no_reason(self):
        self.assertEqual(self.reasons("the build is green on 4a1b2c",
                                      [msg(1, "other", "is it green?")]), [])

    def test_ACKNOWLEDGING_is_held(self):
        [why] = self.reasons("thanks!", [msg(1, "other", "deployed to prod")])
        self.assertIn("only acknowledges", why)

    def test_answering_a_CLOSING_message_is_held(self):
        """The 'last word' case: the other agent's message closed the topic."""
        for last in (msg(1, "other", "done: shipped", None),
                     msg(1, "other", "for the record", cli.AUDIENCE_NONE),
                     msg(1, "other", "thanks!")):
            with self.subTest(last=last["text"]):
                reasons = self.reasons("you're welcome, glad it helped",
                                       [last])
                self.assertTrue(any("closed its topic" in r for r in reasons))

    def test_answering_a_closing_message_WITH_CONTENT_is_still_held(self):
        """The shape is the reply, not the wording: it wakes someone who
        closed the topic. Content is what the confirmation is for."""
        reasons = self.reasons("one more thing: the DELETE path also needs it",
                               [msg(1, "other", "done: shipped")])
        self.assertTrue(any("closed its topic" in r for r in reasons))

    def test_a_new_QUESTION_to_someone_who_closed_a_topic_is_work(self):
        self.assertEqual(self.reasons("one more: did you pin the schema too?",
                                      [msg(1, "other", "done: shipped")]), [])

    def test_a_closing_message_from_someone_this_does_NOT_wake_is_fine(self):
        self.assertEqual(self.reasons("the build is green",
                                      [msg(1, "third", "done: mine")],
                                      woken=("other",)), [])

    def test_a_long_BACK_AND_FORTH_with_no_questions_is_held(self):
        recent = [msg(i, "me" if i % 2 else "other", "statement")
                  for i in range(1, 7)]
        reasons = self.reasons("another statement", recent)
        self.assertTrue(any("back and forth" in r for r in reasons))

    def test_a_back_and_forth_with_an_OPEN_QUESTION_is_work(self):
        recent = [msg(i, "me" if i % 2 else "other", "statement")
                  for i in range(1, 7)]
        recent[-1]["text"] = "which version did you pin?"
        self.assertEqual(self.reasons("3.13.2", recent), [])

    def test_a_SLOW_back_and_forth_is_a_conversation(self):
        recent = [msg(i, "me" if i % 2 else "other", "statement",
                      minutes_ago=60 - i * 10) for i in range(1, 7)]
        self.assertEqual(self.reasons("another statement", recent), [])


class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        saved_env = {k: os.environ.get(k) for k in
                     ("CLAUDE_PROJECT_DIR", "CLAUDE_CODE_SESSION_ID")}

        def restore():
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)
        os.environ["CLAUDE_PROJECT_DIR"] = self.tmp.name
        os.environ["CLAUDE_CODE_SESSION_ID"] = "gate-test-session"
        self.server = FakeServer()
        saved = (cli.call, cli.ring, cli.live_identities,
                 cli.refuse_impersonation)
        self.addCleanup(lambda: (setattr(cli, "call", saved[0]),
                                 setattr(cli, "ring", saved[1]),
                                 setattr(cli, "live_identities", saved[2]),
                                 setattr(cli, "refuse_impersonation",
                                         saved[3])))
        cli.call = self.server.call
        cli.ring = lambda *a, **kw: False
        cli.live_identities = lambda: None
        cli.refuse_impersonation = lambda *a: None
        self.server.channel("room")
        self.server.membership("room", "me")
        self.server.membership("room", "other")

    def say(self, text, audience="other", gate=True, confirm=None):
        out = io.StringIO()
        with redirect_stdout(out):
            cli.do_say("http://127.0.0.1:1", "room", "me", text, audience,
                       gate=gate, confirm=confirm)
        return out.getvalue()

    def sent(self):
        return [m["text"] for m in self.server.tables.get("messages", [])]

    def code_from(self, refusal):
        line = next(l for l in str(refusal).splitlines()
                    if l.strip().startswith("--confirm"))
        return line.split()[-1]

    def test_a_held_message_is_NOT_SENT_and_carries_a_code(self):
        with self.assertRaises(cli.NeedsConfirmation) as caught:
            self.say("thanks!")
        self.assertEqual(self.sent(), [])
        text = str(caught.exception)
        self.assertIn("NOT SENT", text)
        self.assertIn("Don't send it if", text)
        self.assertIn("Send it anyway if", text)
        self.assertTrue(self.code_from(caught.exception))

    def test_the_code_SENDS_it_at_once(self):
        with self.assertRaises(cli.NeedsConfirmation) as caught:
            self.say("thanks!")
        self.say("thanks!", confirm=self.code_from(caught.exception))
        self.assertEqual(self.sent(), ["thanks!"])

    def test_a_code_works_ONCE(self):
        with self.assertRaises(cli.NeedsConfirmation) as caught:
            self.say("thanks!")
        code = self.code_from(caught.exception)
        self.say("thanks!", confirm=code)
        with self.assertRaises(cli.NeedsConfirmation):
            self.say("thanks!", confirm=code)

    def test_a_code_is_for_THAT_TEXT(self):
        """Editing the message means reading it again."""
        with self.assertRaises(cli.NeedsConfirmation) as caught:
            self.say("thanks!")
        with self.assertRaises(cli.NeedsConfirmation) as again:
            self.say("thanks a lot!", confirm=self.code_from(caught.exception))
        self.assertIn("different message", str(again.exception))
        self.assertEqual(self.sent(), [])

    def test_an_EXPIRED_code_does_not_send(self):
        with self.assertRaises(cli.NeedsConfirmation) as caught:
            self.say("thanks!")
        code = self.code_from(caught.exception)
        real = cli.time.time
        cli.time.time = lambda: real() + cli.CONFIRM_TTL_SEC + 1
        try:
            with self.assertRaises(cli.NeedsConfirmation):
                self.say("thanks!", confirm=code)
        finally:
            cli.time.time = real

    def test_a_message_that_WAKES_NOBODY_is_never_held(self):
        self.say("thanks!", audience=cli.AUDIENCE_NONE)
        self.assertEqual(self.sent(), ["thanks!"])

    def test_a_HUMAN_relayed_by_the_bridge_is_never_held(self):
        self.say("thanks!", gate=False)
        self.assertEqual(self.sent(), ["thanks!"])

    def test_ordinary_work_is_never_held(self):
        self.say("the build is green on 4a1b2c; DELETE returns 200")
        self.assertEqual(len(self.sent()), 1)

    def test_the_CLI_holds_an_agent_and_never_a_person_at_a_terminal(self):
        """An agent's session always has an id; a person at a terminal has
        none. The bridge marks a relayed human explicitly."""
        seen = []
        real = cli.do_say
        cli.do_say = lambda *a, **kw: seen.append(kw.get("gate"))
        self.addCleanup(lambda: setattr(cli, "do_say", real))
        saved_argv = sys.argv
        self.addCleanup(lambda: setattr(sys, "argv", saved_argv))
        sys.argv = ["llm_chat", "say", "room", "thanks!", "--as", "me"]
        cli.main()                                   # in a session
        os.environ.pop("CLAUDE_CODE_SESSION_ID")
        cli.main()                                   # a person at a terminal
        os.environ["CLAUDE_CODE_SESSION_ID"] = "gate-test-session"
        os.environ["LLM_CHAT_SENDER"] = "human"
        self.addCleanup(lambda: os.environ.pop("LLM_CHAT_SENDER", None))
        cli.main()                                   # relayed by the bridge
        self.assertEqual(seen, [True, False, False])

    def test_it_maps_to_EXIT_6(self):
        real = cli.main

        def held():
            raise cli.NeedsConfirmation("held")
        cli.main = held
        try:
            with redirect_stdout(io.StringIO()):
                saved, sys.stderr = sys.stderr, io.StringIO()
                try:
                    self.assertEqual(cli.run(), cli.EXIT_NEEDS_CONFIRM)
                finally:
                    sys.stderr = saved
        finally:
            cli.main = real
        self.assertEqual(cli.EXIT_NEEDS_CONFIRM, 6)


if __name__ == "__main__":
    unittest.main()
