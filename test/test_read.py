"""Delivery: what an agent is told, and what the cursor does about it.

Every test here defends a behaviour with a real failing case behind it. The
cursor tests in particular exist because the bug they catch lost messages
silently while `read --all` kept showing them, so the transcript and what the
agent had actually been told disagreed — the worst possible shape for a tool
whose only job is keeping two agents in sync.
"""
import io
import json
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

    # ── the cursor moves LAST ───────────────────────────────────────────────
    # Reported as `pending: 0` beside `owed: [{"seq": 41}]` — two facts that
    # cannot both be true. Something read the message, advancing the shared
    # cursor, and the wake it should have caused never landed. `owed` is the
    # only reason it was noticed rather than simply lost.

    def cursor_writes(self):
        """Every seen_seq write this read performs, in order."""
        writes = []
        real = cli.update

        def watched(server, table, where, values):
            if table == "memberships" and "seen_seq" in values:
                writes.append(values["seen_seq"])
            return real(server, table, where, values)

        cli.update = watched
        self.addCleanup(lambda: setattr(cli, "update", real))
        return writes

    def test_THE_TEXT_IS_OUT_BEFORE_THE_CURSOR_MOVES(self):
        """The order, asserted as an order rather than as an outcome.

        Both deliverers run this CLI as a subprocess with an 8-second timeout
        and read only its stdout. Advancing first meant a read that reached the
        server and then lost its output — a timeout, a killed child — left the
        cursor advanced and the text delivered to nobody. Both call sites say
        "the message is still queued and arrives on the next poll", which was
        true only if the read never reached the server at all.
        """
        self.fake.channel("room")
        self.fake.membership("room", "me", seen_seq=0)
        self.fake.message("room", 1, "other", "hello")

        events = []
        real = cli.update

        def watched(server, table, where, values):
            if table == "memberships" and "seen_seq" in values:
                events.append("cursor")
            return real(server, table, where, values)

        cli.update = watched
        self.addCleanup(lambda: setattr(cli, "update", real))

        class Watched(io.StringIO):
            def write(self, text):
                if text.strip():
                    events.append("text")
                return io.StringIO.write(self, text)

        with redirect_stdout(Watched()):
            cli.do_read("http://127.0.0.1:1", "room", "me")

        self.assertIn("text", events)
        self.assertIn("cursor", events)
        self.assertLess(events.index("text"), events.index("cursor"),
                        "the cursor advanced before the message was printed — "
                        "a caller that loses stdout loses the message")

    def test_A_READ_THAT_DIES_MID_RENDER_LEAVES_THE_CURSOR_ALONE(self):
        """The failure that loses a message, run as a failure.

        With the cursor advanced first this passes silently and the message is
        gone: `read` reports nothing new, `read --all` still shows it, and
        `owed` says an answer is due to something never seen.
        """
        self.fake.channel("room")
        self.fake.membership("room", "me", seen_seq=0)
        self.fake.message("room", 1, "other", "hello")
        writes = self.cursor_writes()

        real = cli.describe_audience

        def boom(*a, **kw):
            raise RuntimeError("the render died")

        cli.describe_audience = boom
        self.addCleanup(lambda: setattr(cli, "describe_audience", real))

        with self.assertRaises(RuntimeError):
            with redirect_stdout(io.StringIO()):
                cli.do_read("http://127.0.0.1:1", "room", "me")

        self.assertEqual(writes, [],
                         "the cursor moved for a message that was never "
                         "delivered — it is now unreachable forever")
        self.assertEqual(
            self.fake.get_membership("room", "me").get("seen_seq", 0), 0)

    def test_the_message_is_STILL_THERE_for_the_next_reader(self):
        """The point of leaving the cursor alone, stated as the outcome that
        matters: at-least-once instead of at-most-once."""
        self.fake.channel("room")
        self.fake.membership("room", "me", seen_seq=0)
        self.fake.message("room", 1, "other", "hello")

        real = cli.describe_audience
        cli.describe_audience = lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("died"))
        with self.assertRaises(RuntimeError):
            with redirect_stdout(io.StringIO()):
                cli.do_read("http://127.0.0.1:1", "room", "me")
        cli.describe_audience = real

        _, text = self.read()
        self.assertIn("hello", text)

    def test_A_SUCCESSFUL_READ_STILL_ADVANCES(self):
        """Paired, and the one that matters most: leaving the cursor alone on
        failure must not become leaving it alone at all, which would redeliver
        every message forever."""
        self.fake.channel("room")
        self.fake.membership("room", "me", seen_seq=0)
        self.fake.message("room", 1, "other", "hello")
        writes = self.cursor_writes()
        self.read()
        self.assertEqual(writes, [1])
        _, again = self.read()
        self.assertIn("nothing new", again)

    def test_JSON_output_commits_the_cursor_too(self):
        """The other exit from the render. A branch that returns early without
        committing would never advance for the delivery hook, which is the
        only caller that uses --json."""
        self.fake.channel("room")
        self.fake.membership("room", "me", seen_seq=0)
        self.fake.message("room", 1, "other", "hello")
        writes = self.cursor_writes()
        self.read(as_json=True)
        self.assertEqual(writes, [1])

    def test_PEEK_still_commits_nothing(self):
        self.fake.channel("room")
        self.fake.membership("room", "me", seen_seq=0)
        self.fake.message("room", 1, "other", "hello")
        writes = self.cursor_writes()
        self.read(peek=True)
        self.assertEqual(writes, [])

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


class NothingNewTest(unittest.TestCase):
    """"Nothing new" was one sentence for THREE different states.

    Reported by an agent that followed a delivery preview's printed remedy and
    landed on an empty inbox. The delivery hook consumes what it previews, so
    plain `read` returned nothing — and since the preview is truncated, that
    pointer was the only surface those messages had.

    The three states: nothing was ever said, you read it normally, and
    something was consumed on your behalf and shown only in part. Only the
    third leaves the reader missing something, and it looked identical to the
    other two. A non-event assertion has to say what absence it is asserting.
    """

    def setUp(self):
        self.server = FakeServer()
        self.real = cli.call
        cli.call = self.server.call
        self.server.membership("room", "me")

    def tearDown(self):
        cli.call = self.real

    def read(self):
        out = io.StringIO()
        with redirect_stdout(out):
            cli.do_read("srv", "room", "me")
        return out.getvalue()

    def test_a_room_with_history_names_how_to_reach_it(self):
        self.server.channel("room", message_count=3)
        self.server.membership("room", "me")
        member = self.server.get_membership("room", "me")
        member["seen_seq"] = 3
        out = self.read()
        self.assertIn("3 earlier message(s)", out)
        self.assertIn("--all --peek", out)

    def test_a_room_where_nothing_was_ever_said_says_THAT(self):
        """Paired: without this the message would fire everywhere and stop
        meaning anything. An empty room is not a room you are missing."""
        self.server.channel("room", message_count=0)
        out = self.read()
        self.assertIn("nothing has ever been said", out)
        self.assertNotIn("--all --peek", out)

    def test_the_remedy_it_prints_is_one_that_WORKS(self):
        """The whole defect: the old text named plain `read`, which is the one
        command guaranteed not to show a message the delivery already ate."""
        self.server.channel("room", message_count=1)
        out = self.read()
        self.assertNotIn("nothing new in room\n  llm_chat read room\n", out)
        self.assertIn("--peek", out)


class ExitContractTest(unittest.TestCase):
    """THREE OUTCOMES, never two. Asked for by a consumer whose retro trigger
    calls `read` non-interactively.

    "nothing waiting" and "I could not look" must never produce the same bytes
    AND the same exit code, or a caller that folds one into the other is deaf
    and cannot tell. It already behaved this way — by accident, not by
    contract, which is no use to anyone building on it. These pin it:

        exit 0 + "[]"        genuinely nothing waiting
        exit 0 + [ {...} ]   messages
        exit non-zero        could not look; stdout carries NO json

    The same three-outcome split both this project and that consumer arrived at
    independently in their own tools this week.
    """

    def setUp(self):
        self.server = FakeServer()
        self.real = cli.call
        cli.call = self.server.call
        self.server.channel("room")
        self.server.membership("room", "me")

    def tearDown(self):
        cli.call = self.real

    def read_json(self, identity="me", channel="room"):
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                cli.do_read("srv", channel, identity, as_json=True)
        except SystemExit as stop:
            return "refused", out.getvalue(), str(stop)
        return "ok", out.getvalue(), None

    def test_nothing_waiting_is_an_empty_LIST_and_success(self):
        state, out, _ = self.read_json()
        self.assertEqual(state, "ok")
        self.assertEqual(json.loads(out), [])

    def test_messages_waiting_are_a_populated_list_and_success(self):
        self.server.message("room", 1, "someone", "hi")
        state, out, _ = self.read_json()
        self.assertEqual(state, "ok")
        self.assertEqual(len(json.loads(out)), 1)

    def test_could_not_look_REFUSES_and_emits_no_json(self):
        """The case that matters. A caller parsing stdout must not be handed
        something that parses as 'no messages'."""
        state, out, why = self.read_json(identity="stranger")
        self.assertEqual(state, "refused")
        self.assertEqual(out.strip(), "")
        self.assertIn("has not joined", why)

    def test_a_missing_room_also_refuses_rather_than_reading_empty(self):
        state, out, _ = self.read_json(channel="nowhere")
        self.assertEqual(state, "refused")
        self.assertEqual(out.strip(), "")

    def test_the_two_zero_exit_cases_are_distinguishable_from_each_other(self):
        _, empty, _ = self.read_json()
        self.server.message("room", 1, "someone", "hi")
        _, full, _ = self.read_json()
        self.assertNotEqual(empty, full)
