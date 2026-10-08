"""Changing what a room says it is, without recreating the room.

A topic could be set at creation, or filled in when empty, and never changed:
the only correction was deleting the room, which loses its transcript and its
membership. Asked for by the human who runs this machine.
"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import FakeServer, load  # noqa: E402

cli = load("llm_chat")


class TopicTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        saved_env = os.environ.get("CLAUDE_PROJECT_DIR")
        os.environ["CLAUDE_PROJECT_DIR"] = self.tmp.name
        self.addCleanup(lambda: os.environ.__setitem__("CLAUDE_PROJECT_DIR",
                                                       saved_env)
                        if saved_env is not None
                        else os.environ.pop("CLAUDE_PROJECT_DIR", None))
        self.server = FakeServer()
        saved = (cli.call, cli.ring, cli.live_identities)
        self.addCleanup(lambda: (setattr(cli, "call", saved[0]),
                                 setattr(cli, "ring", saved[1]),
                                 setattr(cli, "live_identities", saved[2])))
        cli.call = self.server.call
        cli.ring = lambda *a, **kw: False
        cli.live_identities = lambda: None
        self.server.channel("room", topic="the old topic")
        self.server.membership("room", "me")
        self.server.membership("room", "other")

    def topic(self, text, identity="me"):
        out = io.StringIO()
        with redirect_stdout(out):
            cli.do_topic("http://127.0.0.1:1", "room", identity, text)
        return out.getvalue()

    def channel(self):
        return self.server.tables["channels"][0]

    def messages(self):
        return self.server.tables.get("messages", [])

    def test_it_CHANGES_the_topic_in_place(self):
        self.topic("the new topic")
        self.assertEqual(self.channel()["topic"], "the new topic")
        self.assertEqual(self.channel()["name"], "room")

    def test_members_are_told_WITHOUT_being_woken_and_see_the_old_wording(self):
        self.topic("the new topic")
        [notice] = self.messages()
        self.assertEqual(notice["audience"], cli.AUDIENCE_NONE)
        self.assertIn("the new topic", notice["text"])
        self.assertIn("was: the old topic", notice["text"])

    def test_only_a_MEMBER_may_change_it(self):
        with self.assertRaises(SystemExit) as caught:
            self.topic("hijacked", identity="stranger")
        self.assertIn("not a member", str(caught.exception))
        self.assertEqual(self.channel()["topic"], "the old topic")

    def test_it_stays_ONE_LINE(self):
        self.topic("first line\nsecond   line")
        self.assertEqual(self.channel()["topic"], "first line second line")

    def test_an_overlong_topic_is_refused_and_points_at_the_briefing(self):
        with self.assertRaises(SystemExit) as caught:
            self.topic("x" * (cli.MAX_TOPIC + 1))
        self.assertIn("briefing", str(caught.exception))
        self.assertEqual(self.channel()["topic"], "the old topic")

    def test_an_empty_topic_is_refused(self):
        with self.assertRaises(SystemExit):
            self.topic("   ")

    def test_the_same_topic_changes_nothing_and_tells_nobody(self):
        out = self.topic("the old topic")
        self.assertIn("already", out)
        self.assertEqual(self.messages(), [])

    def test_a_CLOSED_room_is_updated_without_a_notice(self):
        """`say` refuses a closed room; the topic is still worth correcting
        for anyone who reads the transcript or reopens it."""
        self.channel()["closed"] = 1
        self.topic("a corrected description")
        self.assertEqual(self.channel()["topic"], "a corrected description")
        self.assertEqual(self.messages(), [])

    def test_a_room_that_does_not_exist_is_named(self):
        with self.assertRaises(SystemExit) as caught:
            out = io.StringIO()
            with redirect_stdout(out):
                cli.do_topic("http://127.0.0.1:1", "nowhere", "me", "x")
        self.assertIn("nowhere", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
