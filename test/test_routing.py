"""Routing at the two edges: the Slack bridge, and the waker's peek.

These are the two places the audience feature has to survive contact with
something outside its own process — a human typing on a phone, and a hook that
must decide whether to interrupt a session without consuming anything.
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import load  # noqa: E402

bridge = load("bin/llm-chat-slack")
waker = load("llm-chat-wake")


class RouteTest(unittest.TestCase):
    """The one place addressing IS parsed out of text, because a human has no
    flags to pass. Everywhere else it is a flag, so an agent pasting a log line
    containing @here cannot wake every agent on the machine."""

    THREADS = {"170.1": "alice"}

    def route(self, **message):
        return bridge.route(message, self.THREADS)

    def test_a_top_level_message_wakes_nobody(self):
        """A human thinking out loud in the channel should not pull every agent
        off its work. They still see it when next working."""
        self.assertEqual(self.route(text="hm", ts="171.0"), ["--to-none"])

    def test_a_thread_reply_wakes_only_that_thread_s_agent(self):
        """The whole mechanism: a thread is the only structured gesture a
        human has on a phone."""
        self.assertEqual(
            self.route(text="yes", ts="171.0", thread_ts="170.1"),
            ["--to", "alice"])

    def test_slack_s_own_encoding_of_here_is_recognised(self):
        """Slack does NOT send '@here'. It sends <!here>, already parsed —
        matching only the literal would be a feature that never once fires
        while looking implemented."""
        self.assertEqual(self.route(text="<!here> all", ts="1"), ["--to-all"])

    def test_channel_and_everyone_mean_the_same_thing(self):
        """An agent in the room is always 'here', so the human distinction
        between present and merely-a-member does not exist on this side."""
        for form in ("<!channel>", "<!everyone>"):
            self.assertEqual(self.route(text=form + " x", ts="1"),
                             ["--to-all"], form)

    def test_a_typed_at_here_also_works(self):
        """It is what a human sees themselves type, whatever Slack sends."""
        self.assertEqual(self.route(text="@here look", ts="1"), ["--to-all"])
        self.assertEqual(self.route(text="@channel look", ts="1"),
                         ["--to-all"])

    def test_it_is_case_insensitive(self):
        self.assertEqual(self.route(text="@HERE look", ts="1"), ["--to-all"])

    def test_an_email_address_is_not_a_mention(self):
        """'mail bob@here.com' waking every agent on the machine is exactly the
        in-band failure this design avoids everywhere else."""
        self.assertEqual(self.route(text="mail bob@here.com", ts="1"),
                         ["--to-none"])

    def test_here_inside_a_word_is_not_a_mention(self):
        self.assertEqual(self.route(text="see attached@here", ts="1"),
                         ["--to-none"])

    def test_at_here_beats_the_thread_rule(self):
        """Explicit beats inferred: someone who writes @here inside a thread
        means everyone, not 'only the agent I happen to be replying to'."""
        self.assertEqual(
            self.route(text="<!here> stop", ts="171.0", thread_ts="170.1"),
            ["--to-all"])

    def test_a_thread_root_we_no_longer_remember_wakes_nobody(self):
        """Waking everyone would spam the room for a mapping WE lost."""
        self.assertEqual(
            self.route(text="x", ts="171.0", thread_ts="999.9"),
            ["--to-none"])

    def test_a_message_that_is_its_own_thread_root_is_top_level(self):
        """Slack sets thread_ts == ts on the parent of a thread. Treating that
        as a reply would route a top-level message to its own author."""
        self.assertEqual(self.route(text="x", ts="171.0", thread_ts="171.0"),
                         ["--to-none"])

    def test_a_message_with_no_text_does_not_explode(self):
        self.assertEqual(bridge.route({}, {}), ["--to-none"])


class ThreadMapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.real = bridge.THREADS
        bridge.THREADS = os.path.join(self.tmp.name, "threads.json")

    def tearDown(self):
        bridge.THREADS = self.real
        self.tmp.cleanup()

    def test_a_missing_map_reads_as_empty(self):
        self.assertEqual(bridge.read_threads(), {})

    def test_a_corrupt_map_reads_as_empty_rather_than_crashing(self):
        """It is written on every relay; a truncated write must not take the
        bridge down."""
        with open(bridge.THREADS, "w") as f:
            f.write("{not json")
        self.assertEqual(bridge.read_threads(), {})

    def test_it_remembers_which_agent_owns_a_thread(self):
        bridge.remember_thread("170.1", "alice")
        self.assertEqual(bridge.read_threads(), {"170.1": "alice"})

    def test_it_ignores_a_relay_with_no_ts_or_no_sender(self):
        bridge.remember_thread(None, "alice")
        bridge.remember_thread("1.0", None)
        self.assertEqual(bridge.read_threads(), {})

    def test_it_is_bounded(self):
        """Written once per relayed message forever. Unbounded, this is a file
        that only grows on a machine nobody is watching."""
        for n in range(bridge.MAX_THREADS + 25):
            bridge.remember_thread("%d.0" % n, "alice")
        self.assertEqual(len(bridge.read_threads()), bridge.MAX_THREADS)

    def test_the_oldest_entries_are_the_ones_dropped(self):
        for n in range(bridge.MAX_THREADS + 5):
            bridge.remember_thread("%d.0" % n, "alice")
        kept = bridge.read_threads()
        self.assertNotIn("0.0", kept)
        self.assertIn("%d.0" % (bridge.MAX_THREADS + 4), kept)


class SenderParseTest(unittest.TestCase):
    """The bridge has to know whose thread it is creating; a relay whose author
    is unknown is one a threaded reply can never be routed back to."""

    def setUp(self):
        self.real = bridge.subprocess.run

    def tearDown(self):
        bridge.subprocess.run = self.real

    def feed(self, stdout, returncode=0):
        class Done:
            pass
        done = Done()
        done.stdout, done.returncode, done.stderr = stdout, returncode, ""
        bridge.subprocess.run = lambda *a, **kw: done
        return bridge.waiting_for_human("room", "me")

    @staticmethod
    def records(*items):
        return json.dumps([{"seq": i + 1, "from": who, "text": text,
                            "audience": None, "mine": False}
                           for i, (who, text) in enumerate(items)])

    def test_it_asks_for_json(self):
        """The rendering is not a parseable format, and the sender here is the
        KEY the thread map is written under — a phantom name routes the human's
        reply to nobody."""
        seen = {}

        class Done:
            stdout, returncode, stderr = "[]", 0, ""

        bridge.subprocess.run = lambda argv, **kw: (
            seen.setdefault("argv", argv), Done())[1]
        bridge.waiting_for_human("room", "me")
        self.assertIn("--json", seen["argv"])

    def test_it_carries_sender_and_text(self):
        self.assertEqual(self.feed(self.records(("builder", "ship it?"))),
                         [("builder", "ship it?")])

    def test_a_bracketed_line_in_a_body_does_NOT_become_a_second_relay(self):
        """The defect this replaced: an [INFO] log line pasted as evidence
        became its own Slack post, attributed to a sender that does not exist,
        and written into the thread map under that phantom."""
        body = "the log said\n[INFO] starting up\nso it ran"
        self.assertEqual(self.feed(self.records(("builder", body))),
                         [("builder", body)])

    def test_a_multi_line_message_is_ONE_relay(self):
        """Otherwise a human gets one notification per paragraph."""
        body = "one\n\ntwo"
        self.assertEqual(len(self.feed(self.records(("a", body)))), 1)

    def test_nothing_waiting_is_nothing(self):
        self.assertEqual(self.feed("[]"), [])

    def test_blank_output_is_nothing(self):
        self.assertEqual(self.feed(""), [])

    def test_unparseable_output_is_nothing_rather_than_a_crash(self):
        self.assertEqual(self.feed("{not json"), [])

    def test_an_empty_message_is_not_relayed(self):
        self.assertEqual(self.feed(self.records(("a", "   "))), [])

    def test_a_crash_is_nothing_rather_than_an_exception(self):
        def explode(*a, **kw):
            raise OSError("no cli")
        bridge.subprocess.run = explode
        self.assertEqual(bridge.waiting_for_human("room", "me"), [])


class AddressedTest(unittest.TestCase):
    """The waker's peek. Never consumes; never wakes on a non-answer."""

    def setUp(self):
        self.real = waker.subprocess.run

    def tearDown(self):
        waker.subprocess.run = self.real

    def feed(self, stdout):
        class Done:
            pass
        done = Done()
        done.stdout, done.returncode, done.stderr = stdout, 0, ""
        waker.subprocess.run = lambda *a, **kw: done
        return waker.addressed("room", {"identity": "me", "server": "http://x"})

    def test_it_asks_pending_and_never_read(self):
        """`read` claims messages. Asking it whether to wake would consume a
        passive message and drop it — the failure this design exists to avoid."""
        seen = {}

        class Done:
            stdout, returncode, stderr = '{"wakes_me": false}', 0, ""

        def spy(argv, **kw):
            seen["argv"] = argv
            return Done()

        waker.subprocess.run = spy
        waker.addressed("room", {"identity": "me", "server": "http://x"})
        self.assertIn("pending", seen["argv"])
        self.assertNotIn("read", seen["argv"])

    def test_something_addressed_to_me_comes_back(self):
        info = self.feed(json.dumps({"wakes_me": True, "messages": []}))
        self.assertTrue(info["wakes_me"])

    def test_nothing_addressed_to_me_is_None(self):
        self.assertIsNone(self.feed(json.dumps({"wakes_me": False})))

    def test_an_outage_is_a_non_answer_not_a_wake(self):
        """Returning anything truthy here would wake the session every poll
        for as long as the server was down."""
        def explode(*a, **kw):
            raise OSError("down")
        waker.subprocess.run = explode
        self.assertIsNone(
            waker.addressed("room", {"identity": "me", "server": "http://x"}))

    def test_unparseable_output_is_a_non_answer(self):
        self.assertIsNone(self.feed("not json"))

    def test_empty_output_is_a_non_answer(self):
        self.assertIsNone(self.feed(""))

    def test_a_json_non_object_is_a_non_answer(self):
        self.assertIsNone(self.feed("[1, 2]"))

    def test_a_room_with_no_identity_is_skipped_without_a_call(self):
        called = []
        waker.subprocess.run = lambda *a, **kw: called.append(a)
        self.assertIsNone(waker.addressed("room", {"server": "http://x"}))
        self.assertIsNone(waker.addressed("room", {"identity": "me"}))
        self.assertEqual(called, [])


class WakeHeaderTest(unittest.TestCase):
    """The wake text an agent actually receives."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CLAUDE_PROJECT_DIR"] = self.tmp.name
        d = os.path.join(self.tmp.name, ".llm_chat")
        os.makedirs(d)
        with open(os.path.join(d, "joined.json"), "w") as f:
            json.dump({"room": {"identity": "me", "server": "http://x"}}, f)
        self.mod = load("llm-chat-wake")

    def tearDown(self):
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.tmp.cleanup()

    def wake_text(self, info):
        import io as _io
        from contextlib import redirect_stderr
        self.mod.addressed = lambda channel, entry: info
        self.mod.poll = lambda channel, entry: "[alice] your build is green"

        class NoSleep:
            @staticmethod
            def time():
                import time as t
                return t.time()

            def sleep(self, _):
                raise KeyboardInterrupt

        self.mod.time = NoSleep()
        err = _io.StringIO()
        stdin = sys.stdin
        sys.stdin = _io.StringIO('{"hook_event_name": "Stop"}')
        try:
            with redirect_stderr(err):
                with self.assertRaises(SystemExit):
                    self.mod.main()
        finally:
            sys.stdin = stdin
        return err.getvalue()

    def test_it_says_who_addressed_you(self):
        text = self.wake_text({"wakes_me": True,
                               "messages": [{"from": "alice",
                                             "wakes_me": True}]})
        self.assertIn("alice addressed you", text)
        self.assertIn("your build is green", text)

    def test_an_unattributable_wake_still_names_the_room(self):
        """The header must survive a payload with no message detail rather than
        producing '#room —  addressed you'."""
        text = self.wake_text({"wakes_me": True, "messages": []})
        self.assertIn("#room", text)
        self.assertNotIn("addressed you", text)


class SyncBroadcastsTest(unittest.TestCase):
    """The waker asking the CLI to reconcile. Convenience, so it must never be
    able to take a session down."""

    def setUp(self):
        self.real = waker.subprocess.run
        self.real_rooms = waker.joined_rooms

    def tearDown(self):
        waker.subprocess.run = self.real
        waker.joined_rooms = self.real_rooms

    def test_it_shells_out_to_sync(self):
        seen = {}
        waker.joined_rooms = lambda: {"room": {"identity": "me",
                                               "server": "http://x"}}
        waker.subprocess.run = lambda argv, **kw: seen.setdefault("argv", argv)
        waker.sync_broadcasts()
        self.assertIn("sync", seen["argv"])

    def test_no_server_means_no_call(self):
        called = []
        waker.joined_rooms = lambda: {"room": {"identity": "me"}}
        waker.subprocess.run = lambda *a, **kw: called.append(a)
        waker.sync_broadcasts()
        self.assertEqual(called, [])

    def test_no_rooms_means_no_call(self):
        called = []
        waker.joined_rooms = lambda: {}
        waker.subprocess.run = lambda *a, **kw: called.append(a)
        waker.sync_broadcasts()
        self.assertEqual(called, [])

    def test_a_failure_is_swallowed(self):
        """A chat outage must never break the session, and this is the least
        important thing the waker does."""
        waker.joined_rooms = lambda: {"room": {"identity": "me",
                                               "server": "http://x"}}

        def explode(*a, **kw):
            raise OSError("down")

        waker.subprocess.run = explode
        waker.sync_broadcasts()          # must not raise


class WhoAddressedTest(unittest.TestCase):
    """An agent pulled off its work with no idea who wanted it has to read the
    whole room to find out."""

    def test_it_names_the_senders_that_woke_me(self):
        info = {"messages": [{"from": "alice", "wakes_me": True},
                             {"from": "bob", "wakes_me": False}]}
        self.assertEqual(waker.who_addressed(info), "alice")

    def test_it_does_not_repeat_a_sender(self):
        info = {"messages": [{"from": "alice", "wakes_me": True},
                             {"from": "alice", "wakes_me": True}]}
        self.assertEqual(waker.who_addressed(info), "alice")

    def test_several_senders_are_listed(self):
        info = {"messages": [{"from": "bob", "wakes_me": True},
                             {"from": "alice", "wakes_me": True}]}
        self.assertEqual(waker.who_addressed(info), "alice, bob")

    def test_no_messages_is_empty_rather_than_a_crash(self):
        self.assertEqual(waker.who_addressed({}), "")


if __name__ == "__main__":
    unittest.main()
