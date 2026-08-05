"""What a message costs everyone who was NOT talking to you.

Measured on this machine before the change: 231,800 characters written once
were delivered as 1,989,954 — 8.6x amplification, roughly half a million
tokens, almost all of it messages arriving in full at agents they were not
addressed to. A nine-member room means every sentence is paid for nine times,
permanently, in nine context windows.

So content goes to whoever the message was FOR, and everyone else is told it
exists. That is the split the audience rules already make about WAKING, applied
to context: being addressed means you need the words, being in the room means
you need to know they were said.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import load  # noqa: E402

deliver = load("llm-chat-deliver")

LONG = "word " * 500          # ~2500 chars, about this project's average


def msg(seq, sender, text, audience=None):
    return {"seq": seq, "from": sender, "text": text,
            "audience": audience, "mine": False}


class AddressedTest(unittest.TestCase):
    def test_a_message_naming_me_is_mine(self):
        self.assertTrue(deliver.addressed_to_me(msg(1, "a", "x", "me"), "me"))

    def test_everyone_is_mine(self):
        """`--to-all` is somebody deciding this genuinely concerns the room."""
        self.assertTrue(deliver.addressed_to_me(msg(1, "a", "x", "*"), "me"))

    def test_nobody_is_not(self):
        self.assertFalse(deliver.addressed_to_me(msg(1, "a", "x", "-"), "me"))

    def test_unaddressed_is_NOT_mine_even_though_it_wakes_me(self):
        """The distinction the whole change rests on. An unaddressed message in
        an ordinary room still WAKES every member — that is deliberate and
        unchanged. It does not follow that every member needs the text."""
        self.assertFalse(deliver.addressed_to_me(msg(1, "a", "x", None), "me"))

    def test_a_message_naming_somebody_else_is_not_mine(self):
        self.assertFalse(
            deliver.addressed_to_me(msg(1, "a", "x", "you"), "me"))

    def test_a_name_is_matched_whole(self):
        self.assertFalse(
            deliver.addressed_to_me(msg(1, "a", "x", "member"), "me"))


class RenderTest(unittest.TestCase):
    def render(self, waiting, identity="me"):
        return deliver.render_channel("ops", identity, waiting)

    def test_what_was_addressed_to_me_arrives_IN_FULL(self):
        """The half that must not regress. Saving context by hiding the thing
        somebody asked you for would be worse than the cost it saves."""
        out = self.render([msg(1, "a", LONG, "me")])
        self.assertIn(LONG.strip(), out)

    def test_what_was_not_addressed_to_me_is_a_POINTER(self):
        out = self.render([msg(1, "a", LONG, "someone-else")])
        self.assertNotIn(LONG.strip(), out)
        self.assertIn("not addressed to you", out)
        self.assertIn("llm_chat read ops", out)

    def test_the_pointer_names_who_spoke(self):
        """Enough to decide whether to go and read it. Who said it is most of
        that decision and costs a word."""
        out = self.render([msg(1, "alice", LONG), msg(2, "bob", LONG)])
        self.assertIn("alice", out)
        self.assertIn("bob", out)

    def test_a_preview_is_bounded(self):
        """The PREVIEW lines specifically. This measured every line including
        the pointer, so growing the pointer — to name a command that actually
        works — failed a test about message previews. The assertion has to
        measure the thing it is named after."""
        out = self.render([msg(1, "a", LONG)])
        previews = [l for l in out.splitlines() if l.startswith("    [")]
        self.assertTrue(previews)
        self.assertLess(max(len(l) for l in previews),
                        deliver.MAX_PASSIVE_PREVIEW + 60)

    def test_previews_are_capped_in_number_too(self):
        """Twenty previews is the wall of text again, in smaller print."""
        out = self.render([msg(i, "a", LONG) for i in range(20)])
        self.assertLessEqual(len([l for l in out.splitlines()
                                  if l.startswith("    [")]), 3)

    def test_the_saving_is_real_and_large(self):
        """The number that justifies the change. Nine members, one message
        addressed to one of them."""
        waiting = [msg(1, "a", LONG, "me")] + [msg(i, "b", LONG)
                                               for i in range(2, 6)]
        inlined = sum(len(m["text"]) for m in waiting)
        self.assertLess(len(self.render(waiting)), inlined * 0.45)

    def test_a_room_of_only_mine_says_nothing_about_pointers(self):
        """No footer when there is nothing to point at — an unconditional line
        is noise on every delivery."""
        out = self.render([msg(1, "a", "short", "me")])
        self.assertNotIn("not addressed to you", out)

    def test_it_always_names_the_room_and_who_you_are(self):
        out = self.render([msg(1, "a", "x")])
        self.assertIn("#ops", out)
        self.assertIn("'me'", out)

    def test_a_multi_line_message_addressed_to_me_keeps_its_shape(self):
        out = self.render([msg(1, "a", "one\ntwo\nthree", "me")])
        for word in ("one", "two", "three"):
            self.assertIn("  " + word, out)

    def test_an_empty_body_does_not_crash_the_delivery(self):
        self.assertIn("#ops", self.render([msg(1, "a", "", "me")]))

    def test_a_preview_collapses_whitespace(self):
        """A message that is mostly newlines would otherwise spend its whole
        budget on blank lines."""
        out = self.render([msg(1, "a", "a\n\n\n\n\nb")])
        self.assertIn("[a] a b", out)


if __name__ == "__main__":
    unittest.main()
