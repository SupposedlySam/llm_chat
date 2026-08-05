"""The doorbell: how an idle agent hears something without asking.

This replaced polling. Five wakers asking the server every five seconds per
room was ~6 requests/second sustained whether or not anyone was talking, and it
eventually rate-limited the server into refusing everything — including the
message announcing the shutdown.

A waker now blocks on a unix socket: no requests, no CPU, and a wake in
milliseconds instead of up to an interval. Measured end to end at 0.317s
against a real server, most of which is process startup.

The failure that matters is NOT "the doorbell broke". It is a doorbell nobody
can hear, because that is silence, and silence is exactly what the system looks
like when nobody has spoken.
"""
import os
import socket
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import FakeServer, load  # noqa: E402

cli = load("llm_chat")
waker = load("llm-chat-wake")


class ConventionTest(unittest.TestCase):
    """Two programs, one path. The ringer and the listener are different
    files, so the convention is duplicated — and a duplicated convention that
    drifts produces a doorbell nobody can find, which reads as nobody having
    spoken rather than as a fault."""

    def test_both_sides_agree_on_where_doorbells_live(self):
        self.assertEqual(cli.doorbell_dir(), waker.doorbell_dir())

    def test_it_is_machine_local_and_not_inside_a_repo(self):
        """A doorbell is meaningless after a reboot and belongs to no project.
        Putting it in a checkout makes one project's temp state another's
        tracked file — and every agent here shares this checkout."""
        self.assertTrue(cli.doorbell_dir().startswith(tempfile.gettempdir()))


class RingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.real = cli.doorbell_dir
        cli.doorbell_dir = lambda: self.tmp.name
        self.bells = []

    def tearDown(self):
        cli.doorbell_dir = self.real
        for b in self.bells:
            b.close()
        self.tmp.cleanup()

    def listener(self, identity):
        path = os.path.join(self.tmp.name, "%s.sock" % identity)
        bell = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bell.bind(path)
        bell.listen(4)
        self.bells.append(bell)
        return bell

    def test_it_reaches_someone_listening(self):
        bell = self.listener("alice")
        heard = []
        t = threading.Thread(target=lambda: heard.append(bell.accept()))
        t.start()
        self.assertTrue(cli.ring("alice"))
        t.join(5)
        self.assertEqual(len(heard), 1)
        heard[0][0].close()

    def test_nobody_listening_is_FALSE_not_an_exception(self):
        """The normal case, not an error. An agent that is working has no
        waker; it picks the message up from the delivery hook or from the
        reconcile its next waker does at startup."""
        self.assertFalse(cli.ring("nobody"))

    def test_a_stale_socket_file_is_not_a_listener(self):
        """A waker that died leaves the path behind. Connecting is refused —
        and reporting that as 'rang' would be the worst possible lie here."""
        path = os.path.join(self.tmp.name, "ghost.sock")
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(path)
        dead.close()
        self.assertFalse(cli.ring("ghost"))

    def test_it_never_raises_whatever_is_on_disk(self):
        os.makedirs(os.path.join(self.tmp.name, "weird.sock"))
        self.assertFalse(cli.ring("weird"))


class DoorbellTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.real = waker.doorbell_dir
        waker.doorbell_dir = lambda: self.tmp.name
        self.open = []

    def tearDown(self):
        waker.doorbell_dir = self.real
        for b in self.open:
            b.close()
        self.tmp.cleanup()

    def bind(self, identity="me"):
        bell = waker.open_doorbell(identity)
        if bell is not None:
            self.open.append(bell)
        return bell

    def test_it_binds_and_can_be_rung(self):
        bell = self.bind()
        self.assertIsNotNone(bell)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(os.path.join(self.tmp.name, "me.sock"))
        sock.sendall(b"1")
        sock.close()
        self.assertTrue(waker.wait_for_ring(bell, 5))

    def test_a_second_waker_does_NOT_steal_a_healthy_doorbell(self):
        """Two projects can hold the same identity. Taking the socket from a
        live listener would make the first agent deaf while the second thinks
        it is covering — and neither would know."""
        self.assertIsNotNone(self.bind())
        self.assertIsNone(self.bind())

    def test_a_STALE_socket_is_reclaimed(self):
        """The other half. Refusing to reclaim a dead waker's socket would
        make the doorbell permanently unusable after any crash — paired with
        the test above, because one rule without the other is broken."""
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(os.path.join(self.tmp.name, "me.sock"))
        dead.close()
        self.assertIsNotNone(self.bind())

    def test_an_unbindable_path_is_None_rather_than_a_crash(self):
        waker.doorbell_dir = lambda: "/proc/nope/deeper"
        self.assertIsNone(waker.open_doorbell("me"))

    def test_an_unremovable_stale_socket_is_None_rather_than_a_crash(self):
        """If the dead waker's socket cannot be deleted, this waker has no
        doorbell — but the session must still run."""
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(os.path.join(self.tmp.name, "me.sock"))
        dead.close()
        real = os.unlink

        def refuse(path):
            raise OSError("read-only")
        os.unlink = refuse
        try:
            self.assertIsNone(waker.open_doorbell("me"))
        finally:
            os.unlink = real

    def test_a_bind_failure_is_None_rather_than_a_crash(self):
        long_name = "x" * 400          # exceeds the sockaddr_un path limit
        self.assertIsNone(waker.open_doorbell(long_name))

    def test_a_ring_that_hangs_up_first_still_counts(self):
        """The sender connects, sends and closes immediately. Treating the
        resulting error as 'nothing arrived' would drop the wake."""
        bell = self.bind()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(os.path.join(self.tmp.name, "me.sock"))
        sock.close()
        self.assertTrue(waker.wait_for_ring(bell, 5))

    def test_an_accept_that_fails_still_reports_a_ring(self):
        """select() said something is there. If accept then fails, something
        DID arrive — reporting False would drop a real wake, and the whole
        point of this design is that a ring is never missed."""
        bell = self.bind()

        class Broken:
            def fileno(self):
                return bell.fileno()

            def accept(self):
                raise OSError("gone")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(os.path.join(self.tmp.name, "me.sock"))
        sock.close()
        self.assertTrue(waker.wait_for_ring(Broken(), 5))

    def test_waiting_times_out_and_says_nothing_arrived(self):
        bell = self.bind()
        start = time.time()
        self.assertFalse(waker.wait_for_ring(bell, 0.2))
        self.assertLess(time.time() - start, 3)

    def test_with_no_doorbell_it_still_waits_rather_than_spinning(self):
        """If binding failed, the loop must not become a busy-wait against the
        supersession checks — that would be the poll back, at full speed."""
        start = time.time()
        self.assertFalse(waker.wait_for_ring(None, 0.2))
        self.assertGreaterEqual(time.time() - start, 0.15)


class ImpersonationTest(unittest.TestCase):
    """You may speak only as yourself.

    "Do not put test traffic in a shared room" was written down, agreed with,
    and broken in the same session it was written — a probe into a nine-member
    room, sent as ANOTHER AGENT'S identity. If you can break it, it was never a
    rule; it was prose. This is the rule.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CLAUDE_PROJECT_DIR"] = self.tmp.name
        os.makedirs(os.path.join(self.tmp.name, ".llm_chat"))

    def tearDown(self):
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.tmp.cleanup()

    def identify(self, name):
        import json
        with open(os.path.join(self.tmp.name, ".llm_chat",
                               "identity.json"), "w") as f:
            json.dump({"identity": name}, f)

    def joined(self, channel, name):
        import json
        with open(os.path.join(self.tmp.name, ".llm_chat",
                               "joined.json"), "w") as f:
            json.dump({channel: {"identity": name, "server": "s"}}, f)

    def test_speaking_as_yourself_is_allowed(self):
        self.identify("owner")
        cli.refuse_impersonation("room", "owner")

    def test_speaking_as_ANOTHER_agent_is_refused(self):
        self.identify("owner")
        with self.assertRaises(SystemExit) as caught:
            cli.refuse_impersonation("llm_chat_owner", "gameloop")
        self.assertIn("refusing to speak as 'gameloop'", str(caught.exception))

    def test_the_refusal_names_who_you_actually_are(self):
        self.identify("owner")
        with self.assertRaises(SystemExit) as caught:
            cli.refuse_impersonation("room", "someone-else")
        self.assertIn("owner", str(caught.exception))

    def test_an_identity_this_project_joined_that_room_as_is_allowed(self):
        """Not a loophole: it is how a project legitimately holding a second
        identity keeps working. It still cannot invent a third."""
        self.identify("owner")
        self.joined("human", "supposedlysam")
        cli.refuse_impersonation("human", "supposedlysam")

    def test_a_project_with_no_identity_at_all_is_not_blocked(self):
        """Refusing here would break first-time setup, which speaks before it
        has ever recorded anything."""
        cli.refuse_impersonation("room", "whoever")


if __name__ == "__main__":
    unittest.main()
