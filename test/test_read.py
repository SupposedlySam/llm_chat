"""Delivery: what an agent is told, and what the cursor does about it.

Every test here defends a behaviour with a real failing case behind it. The
cursor tests in particular exist because the bug they catch lost messages
silently while `read --all` kept showing them, so the transcript and what the
agent had actually been told disagreed — the worst possible shape for a tool
whose only job is keeping two agents in sync.
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


class ReadTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeServer()
        self._real_call = cli.call
        cli.call = self.fake.call
        # joined.json / read.lock land under a throwaway project, never the repo
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name
        os.environ["CLAUDE_PROJECT_DIR"] = self.project

    def tearDown(self):
        cli.call = self._real_call
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.tmp.cleanup()

    def read(self, channel="room", identity="me", **kwargs):
        out = io.StringIO()
        with redirect_stdout(out):
            got = cli.do_read("http://127.0.0.1:1", channel, identity, **kwargs)
        return got, out.getvalue()

    # ── the cursor ──────────────────────────────────────────────────────────
    def test_cursor_advances_only_to_what_was_actually_read(self):
        """A message arriving mid-read must not be stepped over.

        The cursor used to be set from the channel's live message_count, which
        is fetched in a SEPARATE request after the messages. Anything landing in
        between was skipped: never delivered, and unreachable forever because
        the cursor only moves forward.
        """
        self.fake.channel("room")
        self.fake.membership("room", "me", seen_seq=0)
        self.fake.message("room", 1, "other", "first")

        # message_count says 2 — as if seq 2 landed after the fetch above
        self.fake.get_channel("room")["message_count"] = 2

        got, _ = self.read()
        self.assertEqual([m["seq"] for m in got], [1])
        self.assertEqual(self.fake.get_membership("room", "me")["seen_seq"], 1,
                         "cursor must stop at the highest seq actually fetched")

        # and seq 2, arriving late, is still delivered
        self.fake.message("room", 2, "other", "second")
        got, text = self.read()
        self.assertIn("second", text)

    def test_cursor_does_not_move_backwards_when_nothing_is_waiting(self):
        self.fake.channel("room", message_count=5)
        self.fake.membership("room", "me", seen_seq=5)
        self.read()
        self.assertEqual(self.fake.get_membership("room", "me")["seen_seq"], 5)

    def test_peek_leaves_the_cursor_alone(self):
        self.fake.channel("room")
        self.fake.membership("room", "me", seen_seq=0)
        self.fake.message("room", 1, "other", "hello")
        _, text = self.read(peek=True)
        self.assertIn("hello", text)
        self.assertEqual(self.fake.get_membership("room", "me")["seen_seq"], 0)

    # ── the self-echo guard ─────────────────────────────────────────────────
    def test_your_own_words_are_never_returned_as_new_input(self):
        """Without this an agent answers itself, which looks like a
        conversation from the outside and never terminates."""
        self.fake.channel("room")
        self.fake.membership("room", "me", seen_seq=0)
        self.fake.message("room", 1, "me", "my own words")
        got, text = self.read()
        self.assertEqual(got, [])
        self.assertIn("nothing new", text)

    def test_own_messages_still_advance_the_cursor(self):
        """Filtered from delivery is not the same as unseen. If the cursor did
        not move past them they would be re-fetched on every single read."""
        self.fake.channel("room")
        self.fake.membership("room", "me", seen_seq=0)
        self.fake.message("room", 1, "me", "mine")
        self.read()
        self.assertEqual(self.fake.get_membership("room", "me")["seen_seq"], 1)

    def test_all_includes_your_own_words_because_it_means_transcript(self):
        self.fake.channel("room")
        self.fake.membership("room", "me", seen_seq=0)
        self.fake.message("room", 1, "me", "mine")
        self.fake.message("room", 2, "other", "theirs")
        _, text = self.read(all_messages=True)
        self.assertIn("mine", text)
        self.assertIn("theirs", text)
        self.assertIn("(you)", text)

    def test_reading_a_room_you_never_joined_is_refused(self):
        self.fake.channel("room")
        with self.assertRaises(SystemExit):
            self.read()

    def test_closed_rooms_say_so_and_still_deliver_the_backlog(self):
        self.fake.channel("room", closed=1, closed_reason="every member is done")
        self.fake.membership("room", "me", seen_seq=0)
        self.fake.message("room", 1, "other", "last words")
        _, text = self.read()
        self.assertIn("last words", text)
        self.assertIn("closed", text)
        self.assertIn("every member is done", text)




class ConcurrentDeliveryTest(unittest.TestCase):
    """Two deliverers, one cursor — the race the lock exists for.

    This closes the one exclusion in test/mutate.py that admitted a behaviour
    was undefended rather than merely unswept: deleting the lock could not fail
    a single-threaded suite, because the race needs two readers at once. The
    lock's MECHANICS were asserted; what it is FOR was not.

    flock is per open-file-description, and read_lock opens its own descriptor
    per call, so two threads in one process contend exactly as two processes do.
    """
    def setUp(self):
        self.fake = FakeServer()
        self._real_call = cli.call
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CLAUDE_PROJECT_DIR"] = self.tmp.name

    def tearDown(self):
        cli.call = self._real_call
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.tmp.cleanup()

    def test_one_message_is_delivered_exactly_once_to_two_readers(self):
        import threading

        self.fake.channel("room")
        self.fake.membership("room", "me", seen_seq=0)
        self.fake.message("room", 1, "other", "deliver me once")

        # Widen the read-modify-write window so an unlocked run loses the race
        # reliably rather than occasionally: without this the test would pass
        # for timing reasons and defend nothing.
        real_call = self.fake.call

        def slow(server, method, path, body=None, query=None, timeout=10):
            if method == "GET" and path == "/db/list" and query.get("table") == "messages":
                time.sleep(0.05)
            return real_call(server, method, path, body, query, timeout)
        cli.call = slow

        delivered = []
        lock = threading.Lock()

        def read_once():
            got = cli.do_read("http://127.0.0.1:1", "room", "me")
            with lock:
                delivered.extend(got)

        # Redirected ONCE around both threads, not inside each: redirect_stdout
        # swaps sys.stdout globally, so per-thread use races and leaks a line
        # to the real terminal — which it did, and a suite that prints stray
        # output is one whose output stops being read.
        with redirect_stdout(io.StringIO()):
            threads = [threading.Thread(target=read_once) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(len(delivered), 1,
                         "the lock must serialise claim-and-advance; delivering "
                         "twice is what the other agent reads as you repeating "
                         "yourself")
        self.assertEqual(self.fake.get_membership("room", "me")["seen_seq"], 1)

if __name__ == "__main__":
    unittest.main()
