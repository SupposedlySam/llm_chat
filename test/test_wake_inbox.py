"""Waking through the session's own inbox socket.

Claude Code binds a socket per session and exports it, with a token, to the
session's hooks. A line posted there by one of the session's own children is
delivered, and an IDLE session starts a turn with it — measured on 2026-10-07
in a VS Code extension session, which woke five seconds after the post.

These tests use a real Unix socket, so what is asserted is the bytes the inbox
would receive, not a description of them. Nothing here may reach the inbox of
the session running the suite: run.py's hermetic() clears both variables, and
every test that needs them sets its own.
"""
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import load  # noqa: E402


class FakeInbox:
    """A listening Unix socket that records every connection's lines.

    Under /tmp with a short name, because a socket path over ~104 bytes cannot
    be bound on macOS and TemporaryDirectory's names are long.
    """

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="lci-", dir="/tmp")
        self.path = os.path.join(self.dir, "s.sock")
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen(8)
        self.connections = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            data = b""
            conn.settimeout(3)
            try:
                while chunk := conn.recv(4096):
                    data += chunk
            except OSError:
                pass
            conn.close()
            self.connections.append(data.decode())

    def lines(self):
        """JSON lines from every connection that sent any."""
        self.wait_for(1)
        return [[json.loads(ln) for ln in c.splitlines() if ln.strip()]
                for c in self.connections if c]

    def wait_for(self, n):
        for _ in range(100):
            if len([c for c in self.connections if c]) >= n:
                return
            threading.Event().wait(0.02)

    def close(self):
        self.server.close()
        shutil.rmtree(self.dir, ignore_errors=True)


class InboxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.saved_env = {k: os.environ.get(k) for k in (
            "CLAUDE_PROJECT_DIR", "CLAUDE_CODE_MESSAGING_SOCKET",
            "CLAUDE_CODE_MESSAGING_TOKEN", "LLM_CHAT_WAKE_VIA")}
        self.addCleanup(self.restore_env)
        os.environ["CLAUDE_PROJECT_DIR"] = self.tmp.name
        for k in ("CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
                  "LLM_CHAT_WAKE_VIA"):
            os.environ.pop(k, None)
        self.mod = load("llm-chat-wake")
        self.mod.inbound_settings = lambda project=None, home=None: []
        self.box = FakeInbox()
        self.addCleanup(self.box.close)

    def restore_env(self):
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def with_inbox(self, token="tok-123"):
        os.environ["CLAUDE_CODE_MESSAGING_SOCKET"] = self.box.path
        os.environ["CLAUDE_CODE_MESSAGING_TOKEN"] = token

    def exits(self):
        try:
            return self.mod.read_exits()
        except Exception:
            return []

    # ---- the wire

    def test_a_post_sends_the_AUTH_line_first_then_one_user_message(self):
        """The auth line is what lets macOS verify a child that has already
        exited — which a detached listener that posts and leaves always is."""
        self.assertTrue(self.mod.post_to_inbox(self.box.path, "tok-123", "hi"))
        [lines] = self.box.lines()
        self.assertEqual(lines[0], {"type": "auth", "token": "tok-123"})
        self.assertEqual(lines[1], {"type": "user", "message": {
            "role": "user", "content": "hi"}})
        self.assertEqual(len(lines), 2)

    def test_a_post_to_NOTHING_reports_failure_rather_than_success(self):
        missing = os.path.join(self.box.dir, "gone.sock")
        self.assertFalse(self.mod.post_to_inbox(missing, "t", "hi"))

    def test_alive_means_ACCEPTS_not_merely_exists(self):
        """Dead sessions leave socket files behind; this machine had more of
        them than live sessions."""
        self.assertTrue(self.mod.inbox_alive(self.box.path))
        stale = os.path.join(self.box.dir, "stale.sock")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(stale)
        s.close()                         # file remains, nobody listening
        self.assertTrue(os.path.exists(stale))
        self.assertFalse(self.mod.inbox_alive(stale))

    # ---- when the inbox is trusted

    def test_no_inbox_without_BOTH_variables(self):
        self.assertIsNone(self.mod.inbox())
        os.environ["CLAUDE_CODE_MESSAGING_SOCKET"] = self.box.path
        self.assertIsNone(self.mod.inbox())
        self.with_inbox()
        self.assertEqual(self.mod.inbox(), (self.box.path, "tok-123"))

    def test_the_escape_hatch_forces_the_old_path(self):
        self.with_inbox()
        os.environ["LLM_CHAT_WAKE_VIA"] = "rewake"
        self.assertIsNone(self.mod.inbox())

    def test_a_HOLD_or_REFUSE_setting_means_the_old_path(self):
        """Own-child messages are delivered only when no crossSessionInbound
        value applies; hold and refuse swallow them silently."""
        self.with_inbox()
        for value in ("hold", "refuse"):
            with self.subTest(value=value):
                self.mod.inbound_settings = lambda project=None, home=None, v=value: [v]
                self.assertIsNone(self.mod.inbox())
        self.mod.inbound_settings = lambda project=None, home=None: ["accept"]
        self.assertIsNotNone(self.mod.inbox())

    def test_settings_are_READ_from_project_and_home_files(self):
        load_real = load("llm-chat-wake")
        home = os.path.join(self.tmp.name, "home")
        os.makedirs(os.path.join(home, ".claude"))
        project = os.path.join(self.tmp.name, "proj")
        os.makedirs(os.path.join(project, ".claude"))
        self.assertEqual(load_real.inbound_settings(project, home), [])
        with open(os.path.join(project, ".claude", "settings.local.json"), "w") as f:
            json.dump({"crossSessionInbound": "hold"}, f)
        with open(os.path.join(home, ".claude", "settings.json"), "w") as f:
            f.write("{not json")              # unreadable is not a value
        self.assertEqual(load_real.inbound_settings(project, home), ["hold"])

    def test_a_DEAD_socket_is_no_inbox(self):
        os.environ["CLAUDE_CODE_MESSAGING_SOCKET"] = os.path.join(
            self.box.dir, "gone.sock")
        os.environ["CLAUDE_CODE_MESSAGING_TOKEN"] = "t"
        self.assertIsNone(self.mod.inbox())

    # ---- waking

    def quiet_announce(self):
        self.mod.note_rewake = lambda: None
        self.mod.watch_for_a_missed_wake = lambda: None

    def test_announce_POSTS_the_wake_text_and_does_not_exit_2(self):
        self.quiet_announce()
        blocks = ["#room (you are 'me')\n  [other] hello"]
        self.assertEqual(self.mod.announce(blocks, (self.box.path, "t")), 0)
        [lines] = self.box.lines()
        self.assertEqual(lines[1]["message"]["content"],
                         self.mod.wake_text(blocks))
        self.assertIn("[other] hello", lines[1]["message"]["content"])
        self.assertIn("through its inbox", json.dumps(self.exits()))

    def test_the_inbox_path_STILL_arms_the_missed_wake_watcher(self):
        """A held or dropped post looks exactly like an ignored exit 2 from
        here, so the same watcher has to be left behind for it."""
        order = []
        self.mod.note_rewake = lambda: order.append("note")
        self.mod.watch_for_a_missed_wake = lambda: order.append("watch")
        self.mod.announce(["#r\n  x"], (self.box.path, "t"))
        self.assertEqual(order, ["note", "watch"])

    def test_a_FAILED_post_keeps_the_claimed_text_and_says_so(self):
        """The messages were already read. Losing them silently is the one
        outcome this must not have."""
        self.quiet_announce()
        missing = os.path.join(self.box.dir, "gone.sock")
        self.assertEqual(self.mod.announce(["#room\n  [other] keep me"],
                                           (missing, "t")), 0)
        with open(self.mod.UNDELIVERED_PATH) as f:
            self.assertIn("[other] keep me", f.read())
        self.assertIn("COULD NOT POST", json.dumps(self.exits()))

    def test_without_an_inbox_announce_still_EXITS_2(self):
        self.quiet_announce()
        with self.assertRaises(SystemExit) as caught:
            with open(os.devnull, "w") as sink:
                saved, sys.stderr = sys.stderr, sink
                try:
                    self.mod.announce(["#r\n  x"])
                finally:
                    sys.stderr = saved
        self.assertEqual(caught.exception.code, 2)

    # ---- the hand-off

    def run_main(self, event, source=None):
        payload = {"hook_event_name": event}
        if source:
            payload["source"] = source
        saved = sys.stdin, sys.argv
        sys.stdin = __import__("io").StringIO(json.dumps(payload))
        sys.argv = ["llm-chat-wake"]
        try:
            return self.mod.main()
        finally:
            sys.stdin, sys.argv = saved

    def in_a_room(self):
        self.mod.joined_rooms = lambda: {"room": {"identity": "me",
                                                  "server": "http://127.0.0.1:1"}}

    def test_a_FRESH_START_with_an_inbox_hands_off_and_returns_at_once(self):
        """#39 was SessionStart holding a long-lived waker. With an inbox the
        listener is detached, so even a startup can listen without blocking."""
        self.with_inbox()
        self.in_a_room()
        started = []
        self.mod.start_inbox_listener = lambda: started.append(1) or 4242
        self.mod.listen = lambda rooms, box: self.fail("must not block here")
        for source in ("startup", "resume", "compact", None):
            with self.subTest(source=source):
                self.assertEqual(self.run_main("SessionStart", source), 0)
        self.assertEqual(self.run_main("Stop"), 0)
        self.assertEqual(len(started), 5)
        self.assertIn("handed listening to the inbox listener (pid 4242)",
                      json.dumps(self.exits()))

    def test_WITHOUT_an_inbox_a_fresh_start_still_does_not_listen(self):
        """The #39 guard is unchanged for hosts with no inbox."""
        self.in_a_room()
        self.mod.start_inbox_listener = lambda: self.fail("no inbox to use")
        self.mod.listen = lambda rooms, box: self.fail("must not listen")
        self.assertEqual(self.run_main("SessionStart", "startup"), 0)

    def test_if_the_listener_cannot_START_the_hook_listens_the_old_way(self):
        self.with_inbox()
        self.in_a_room()
        self.mod.start_inbox_listener = lambda: None
        seen = []
        self.mod.listen = lambda rooms, box: seen.append(box) or 0
        self.assertEqual(self.run_main("Stop"), 0)
        self.assertEqual(seen, [None])

    def test_the_listener_is_DETACHED_and_holds_none_of_the_host_s_pipes(self):
        calls = []

        class Popen:
            def __init__(self, argv, **kw):
                calls.append((argv, kw))
                self.pid = 777
        self.mod.subprocess = type("S", (), {"Popen": Popen,
                                             "DEVNULL": -3})
        self.assertEqual(self.mod.start_inbox_listener(), 777)
        [(argv, kw)] = calls
        self.assertEqual(argv[-1], "--inbox")
        self.assertTrue(kw["start_new_session"])
        self.assertEqual(kw["stdin"], -3)
        for stream in ("stdout", "stderr"):
            self.assertTrue(hasattr(kw[stream], "write"),
                            "%s must be a file, not the host's pipe" % stream)

    def test_the_INBOX_MODE_entry_listens_with_the_box(self):
        self.with_inbox()
        self.in_a_room()
        seen = []
        self.mod.listen = lambda rooms, box: seen.append(box) or 0
        saved = sys.argv
        sys.argv = ["llm-chat-wake", "--inbox"]
        try:
            self.assertEqual(self.mod.main(), 0)
        finally:
            sys.argv = saved
        self.assertEqual(seen, [(self.box.path, "tok-123")])

    def test_the_INBOX_MODE_entry_stands_down_without_a_usable_inbox(self):
        saved = sys.argv
        sys.argv = ["llm-chat-wake", "--inbox"]
        self.mod.listen = lambda rooms, box: self.fail("nothing to post to")
        try:
            self.assertEqual(self.mod.main(), 0)
        finally:
            sys.argv = saved
        self.assertIn("not usable", json.dumps(self.exits()))

    def test_the_INBOX_MODE_entry_stands_down_in_no_rooms(self):
        self.with_inbox()
        self.mod.joined_rooms = lambda: {}
        saved = sys.argv
        sys.argv = ["llm-chat-wake", "--inbox"]
        try:
            self.assertEqual(self.mod.main(), 0)
        finally:
            sys.argv = saved
        self.assertIn("in no rooms", json.dumps(self.exits()))

    def test_the_listener_is_ORPHANED_when_the_inbox_stops_answering(self):
        """It is reparented on purpose, so its parent proves nothing."""
        box = (self.box.path, "t")
        self.assertFalse(self.mod.inbox_orphaned(box))
        self.box.server.close()
        os.remove(self.box.path)
        self.assertTrue(self.mod.inbox_orphaned(box))


if __name__ == "__main__":
    unittest.main()
