"""The doorbell: how an idle agent hears something without asking.

This replaced polling. Five wakers asking the server every five seconds per
room was ~6 requests/second sustained whether or not anyone was talking, and it
eventually rate-limited the server into refusing everything — including the
message announcing the shutdown. Measured after: ZERO requests in 20 seconds
idle, and a wake in milliseconds.

ONE DOORBELL PER MEMBERSHIP, not per identity, and that distinction is most of
why this file exists. Identity is not unique on this machine: four projects
answer to `owner`, and three hold two identities each. Keyed by identity, one
waker bound the socket and the rest silently did not — then heard nothing
addressed to them, which looks exactly like a quiet room.

The failure that matters is never "the doorbell broke". It is a doorbell nobody
can hear, because that is silence, and silence is what the system looks like
when nobody has spoken.
"""
import os
import socket
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import load  # noqa: E402

cli = load("llm_chat")
waker = load("llm-chat-wake")


class ConventionTest(unittest.TestCase):
    """Two programs, one naming rule. The ringer and the listener are separate
    files, so it is duplicated — and a duplicated convention that drifts
    produces a doorbell nobody can find, which reads as nobody having spoken
    rather than as a fault."""

    def test_both_sides_agree_on_where_doorbells_live(self):
        self.assertEqual(cli.doorbell_dir(), waker.doorbell_dir())

    def test_both_sides_agree_on_the_name(self):
        self.assertEqual(cli.doorbell_name("room", "me"),
                         waker.doorbell_name("room", "me"))

    def test_the_SAME_identity_in_DIFFERENT_rooms_gets_different_bells(self):
        """The collision that made this a membership key. Four projects here
        answer to `owner`; keyed by identity, whichever waker started first
        took the socket and the other three went deaf."""
        self.assertNotEqual(cli.doorbell_name("llm_chat_owner", "owner"),
                            cli.doorbell_name("game_loop_owner", "owner"))

    def test_different_identities_in_one_room_get_different_bells(self):
        self.assertNotEqual(cli.doorbell_name("room", "a"),
                            cli.doorbell_name("room", "b"))

    def other_checkout(self):
        """A second copy of the waker at a different path on disk.

        Loaded with SourceFileLoader because these entrypoints have no .py
        suffix — spec_from_file_location returns None for them, which fails as
        an AttributeError three lines later rather than at the cause."""
        import importlib.util
        import shutil
        from importlib.machinery import SourceFileLoader

        other = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        os.makedirs(os.path.join(other, "bin"))
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        target = os.path.join(other, "bin", "llm-chat-wake")
        shutil.copy(os.path.join(here, "bin", "llm-chat-wake"), target)
        loader = SourceFileLoader("other_wake", target)
        spec = importlib.util.spec_from_loader("other_wake", loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module

    def test_TWO_WORKSPACES_DO_NOT_SHARE_DOORBELLS(self):
        """The original concern, kept, with the discriminator corrected.

        Two workspaces on one machine have their own store, rooms and agents,
        and an unqualified directory made them share sockets: `general__owner`
        exists in both, so one waker binds it and the other finds a healthy
        holder and quietly declines. Deaf, with no error anywhere.

        This asserted that two CHECKOUTS differ, which was the wrong
        discriminator — see the paired test below. What separates workspaces is
        the SERVER, so that is what is asserted now, and the property the
        original was protecting still holds."""
        self.assertNotEqual(cli.doorbell_dir("http://localhost:7717"),
                            cli.doorbell_dir("http://localhost:7718"))

    def test_VENDORED_COPIES_ON_ONE_SERVER_DO_SHARE(self):
        """The regression the old test could not see, and it was live.

        lamp and showrunner each carry a copy of llm_chat under .lamp/, and all
        of them point at the same server on 7717 — one store, one set of rooms,
        one conversation. Keyed by checkout, that was three doorbell
        directories: lamp rang sockets under its own hash while showrunner's
        waker listened under another, so messages landed in the shared database
        and woke nobody. It surfaced as a human saying "I had to tell both of
        them to go and look", which is what a ring into an empty directory
        looks like from the outside.

        Asserted across a real second copy on disk, not by calling one function
        twice, because the defect was precisely that two copies disagreed."""
        other = self.other_checkout()
        self.assertEqual(other.doorbell_dir("http://localhost:7717"),
                         waker.doorbell_dir("http://localhost:7717"))

    def test_one_server_spelled_two_ways_is_one_workspace(self):
        """The ringer and the listener reach the server through different
        paths — `--server`, LLM_CHAT_SERVER, the default — so they will not
        always spell it identically. A trailing slash must not partition a
        workspace the way a checkout path just did."""
        self.assertEqual(cli.doorbell_dir("http://localhost:7717/"),
                         cli.doorbell_dir("HTTP://LocalHost:7717"))

    def test_localhost_and_127_0_0_1_stay_APART(self):
        """Not normalised together, deliberately: zonai binds `[::1]` only on
        macOS, so these are genuinely different endpoints and agents on one
        cannot reach the other. Collapsing them would put unreachable agents in
        one namespace — a worse bug than the one being fixed."""
        self.assertNotEqual(cli.doorbell_dir("http://localhost:7717"),
                            cli.doorbell_dir("http://127.0.0.1:7717"))

    def test_it_is_machine_local_and_not_inside_a_repo(self):
        """A doorbell is meaningless after a reboot and belongs to no project.
        Putting it in a checkout makes one project's temp state another's
        tracked file — and every agent here shares this checkout."""
        self.assertTrue(cli.doorbell_dir().startswith(tempfile.gettempdir()))

    def test_a_name_cannot_escape_the_doorbell_directory(self):
        """Safe without escaping only because both parts are validated against
        NAME_OK. Asserted rather than assumed."""
        name = cli.doorbell_name("room", "me")
        self.assertNotIn("/", name)
        self.assertNotIn("..", name)


class RingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.real = cli.doorbell_dir
        cli.doorbell_dir = lambda server=None: self.tmp.name
        self.bells = []

    def tearDown(self):
        cli.doorbell_dir = self.real
        for b in self.bells:
            b.close()
        self.tmp.cleanup()

    def listener(self, channel, identity):
        path = os.path.join(self.tmp.name,
                            cli.doorbell_name(channel, identity))
        bell = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bell.bind(path)
        bell.listen(4)
        self.bells.append(bell)
        return bell

    def test_it_reaches_someone_listening(self):
        bell = self.listener("room", "alice")
        heard = []
        t = threading.Thread(target=lambda: heard.append(bell.accept()))
        t.start()
        self.assertTrue(cli.ring("room", "alice"))
        t.join(5)
        self.assertEqual(len(heard), 1)
        heard[0][0].close()

    def test_it_does_not_reach_the_same_name_in_another_room(self):
        """The whole point of the membership key."""
        self.listener("room", "alice")
        self.assertFalse(cli.ring("elsewhere", "alice"))

    def test_nobody_listening_is_FALSE_not_an_exception(self):
        """The normal case, not an error. An agent that is working has no
        waker; it picks the message up from the delivery hook or from the
        reconcile its next waker does at startup."""
        self.assertFalse(cli.ring("room", "nobody"))

    def test_a_stale_socket_file_is_not_a_listener(self):
        """A waker that died leaves the path behind. Connecting is refused —
        and reporting that as 'rang' would be the worst possible lie here."""
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(os.path.join(self.tmp.name,
                               cli.doorbell_name("room", "ghost")))
        dead.close()
        self.assertFalse(cli.ring("room", "ghost"))

    def test_it_never_raises_whatever_is_on_disk(self):
        os.makedirs(os.path.join(self.tmp.name,
                                 cli.doorbell_name("room", "weird")))
        self.assertFalse(cli.ring("room", "weird"))


class DoorbellTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.real = waker.doorbell_dir
        waker.doorbell_dir = lambda server=None: self.tmp.name
        self.open = []

    def tearDown(self):
        waker.doorbell_dir = self.real
        for b in self.open:
            b.close()
        self.tmp.cleanup()

    def bind(self, channel="room", identity="me"):
        bell = waker.open_doorbell(channel, identity)
        if bell is not None:
            self.open.append(bell)
        return bell

    def path(self, channel="room", identity="me"):
        return os.path.join(self.tmp.name,
                            waker.doorbell_name(channel, identity))

    def test_it_binds_and_can_be_rung(self):
        bell = self.bind()
        self.assertIsNotNone(bell)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self.path())
        sock.sendall(b"1")
        sock.close()
        self.assertTrue(waker.wait_for_ring({bell: "room"}, 5))

    def test_a_second_waker_does_NOT_steal_a_healthy_doorbell(self):
        """Taking the socket from a live listener would make the first agent
        deaf while the second thinks it is covering — and neither would know."""
        self.assertIsNotNone(self.bind())
        self.assertIsNone(self.bind())

    def test_a_STALE_socket_is_reclaimed(self):
        """The other half. Refusing to reclaim a dead waker's socket would make
        the doorbell permanently unusable after any crash — paired with the
        test above, because one rule without the other is broken."""
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(self.path())
        dead.close()
        self.assertIsNotNone(self.bind())

    def test_an_unbindable_path_is_None_rather_than_a_crash(self):
        waker.doorbell_dir = lambda server=None: "/proc/nope/deeper"
        self.assertIsNone(waker.open_doorbell("room", "me"))

    def test_an_unremovable_stale_socket_is_None_rather_than_a_crash(self):
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(self.path())
        dead.close()
        real = os.unlink

        def refuse(path):
            raise OSError("read-only")
        os.unlink = refuse
        try:
            self.assertIsNone(waker.open_doorbell("room", "me"))
        finally:
            os.unlink = real

    def test_a_bind_failure_is_None_rather_than_a_crash(self):
        self.assertIsNone(waker.open_doorbell("room", "x" * 400))


class ManyBellsTest(unittest.TestCase):
    """A project is several memberships, and each needs its own bell.

    Binding one for whichever identity came first left the others unreachable,
    and which one won depended on dict order — so the same agent could be
    reachable or deaf across restarts with nothing having changed.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.real = waker.doorbell_dir
        waker.doorbell_dir = lambda server=None: self.tmp.name

    def tearDown(self):
        waker.doorbell_dir = self.real
        self.tmp.cleanup()

    def rooms(self, **pairs):
        return {c: {"identity": i, "server": "s"} for c, i in pairs.items()}

    def test_every_joined_room_gets_a_bell(self):
        bells = waker.open_doorbells(self.rooms(alpha="owner", beta="other"))
        try:
            self.assertEqual(sorted(bells.values()), ["alpha", "beta"])
        finally:
            for b in bells:
                b.close()

    def test_two_identities_in_one_project_are_BOTH_reachable(self):
        """The reported hole, as a test. Three of five projects here hold two
        identities, and one of them was always unreachable."""
        bells = waker.open_doorbells(
            self.rooms(own_room="owner", shared="lamp-owner"))
        try:
            self.assertEqual(len(bells), 2)
            self.assertTrue(os.path.exists(os.path.join(
                self.tmp.name, waker.doorbell_name("shared", "lamp-owner"))))
        finally:
            for b in bells:
                b.close()

    def test_a_room_with_no_identity_is_skipped(self):
        self.assertEqual(waker.open_doorbells({"a": {"server": "s"}}), {})

    def test_no_rooms_is_no_bells_rather_than_a_crash(self):
        self.assertEqual(waker.open_doorbells({}), {})

    def test_waiting_on_several_wakes_on_ANY_of_them(self):
        bells = waker.open_doorbells(self.rooms(alpha="owner", beta="other"))
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(os.path.join(self.tmp.name,
                                      waker.doorbell_name("beta", "other")))
            sock.sendall(b"1")
            sock.close()
            self.assertTrue(waker.wait_for_ring(bells, 5))
        finally:
            for b in bells:
                b.close()

    def test_with_no_bells_it_waits_rather_than_spinning(self):
        """If binding failed for everything, the loop must not become a
        busy-wait against the supersession checks — that is the poll again, at
        full speed."""
        start = time.time()
        self.assertFalse(waker.wait_for_ring({}, 0.2))
        self.assertGreaterEqual(time.time() - start, 0.15)

    def test_waiting_times_out_and_says_nothing_arrived(self):
        bells = waker.open_doorbells(self.rooms(alpha="owner"))
        try:
            start = time.time()
            self.assertFalse(waker.wait_for_ring(bells, 0.2))
            self.assertLess(time.time() - start, 3)
        finally:
            for b in bells:
                b.close()

    def test_an_accept_that_fails_still_reports_a_ring(self):
        """select() said something is there. If accept then fails, something
        DID arrive — reporting False would drop a real wake, and never missing
        a ring is the whole point of this design."""
        bells = waker.open_doorbells(self.rooms(alpha="owner"))
        real = list(bells)[0]

        class Broken:
            def fileno(self):
                return real.fileno()

            def accept(self):
                raise OSError("gone")
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(os.path.join(self.tmp.name,
                                      waker.doorbell_name("alpha", "owner")))
            sock.close()
            self.assertTrue(waker.wait_for_ring({Broken(): "alpha"}, 5))
        finally:
            for b in bells:
                b.close()


class ImpersonationTest(unittest.TestCase):
    """You may speak only as yourself.

    "Do not put test traffic in a shared room" was written down, agreed with,
    and broken in the same session it was written — a probe into a nine-member
    room, sent as ANOTHER AGENT'S identity. If you can break it, it was never a
    rule; it was prose.
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
