"""joined.json is read-modify-written; concurrent writers must not lose rooms.

Measured 2026-11-25, chasing #47: eight `join`s in one session at once kept
about one room in eight (140 of 160 lost over 20 rounds), and every one
printed "joined". A Crawler fanning reviewers out in parallel is that shape,
and a session whose entry for its own room is gone is refused under its own
name.

The race is made deterministic here rather than hoped for: every reader
pauses between reading the map and writing it back, which is the window the
real race needs. Without the lock every writer reads the same empty map.
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import load  # noqa: E402

cli = load("llm_chat")

WRITERS = 8


class JoinedLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = {k: os.environ.get(k) for k in
                    ("CLAUDE_PROJECT_DIR", "CLAUDE_CODE_SESSION_ID")}
        os.environ["CLAUDE_PROJECT_DIR"] = self.tmp.name
        os.environ["CLAUDE_CODE_SESSION_ID"] = "lock0001"
        self.addCleanup(self.restore_env)
        os.makedirs(os.path.join(self.tmp.name, ".llm_chat"))
        real = cli.read_joined

        def slow_read():
            found = real()
            time.sleep(0.05)              # the read-then-write window
            return found

        cli.read_joined = slow_read
        self.addCleanup(lambda: setattr(cli, "read_joined", real))
        self.real_read = real

    def restore_env(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def all_at_once(self, fn, args):
        start = threading.Barrier(len(args))

        def run(a):
            start.wait()
            fn(*a)

        threads = [threading.Thread(target=run, args=(a,)) for a in args]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertFalse(any(t.is_alive() for t in threads))

    def test_parallel_joins_in_ONE_session_keep_every_room(self):
        rooms = ["room%d" % i for i in range(WRITERS)]
        self.all_at_once(cli.remember, [(r, "x", "http://s") for r in rooms])
        self.assertEqual(sorted(self.real_read()), rooms)

    def test_parallel_leaves_drop_exactly_the_rooms_left(self):
        rooms = ["room%d" % i for i in range(WRITERS)]
        path = cli.joined_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({r: {"identity": "x", "server": "s"}
                       for r in rooms + ["kept"]}, f)
        self.all_at_once(cli.forget, [(r,) for r in rooms])
        self.assertEqual(sorted(self.real_read()), ["kept"])

    def test_leaving_a_room_never_joined_leaves_NO_lock_file(self):
        """A session directory holding only a lock is no longer a stub that
        `sync --repair` may remove, so a no-op must not create one."""
        self.assertFalse(cli.forget("never"))
        self.assertFalse(os.path.exists(cli.joined_path() + ".lock"))

    def test_the_lock_FAILS_OPEN_rather_than_hanging_a_join(self):
        path = cli.joined_path()
        with cli.joined_lock(path):
            began = time.time()
            done = []
            t = threading.Thread(target=lambda: done.append(
                cli.joined_lock(path, timeout=0.2).__enter__()))
            t.start()
            t.join(5)
            self.assertEqual(len(done), 1)
            self.assertLess(time.time() - began, 3)

    def test_an_unusable_directory_does_not_stop_the_write(self):
        blocker = os.path.join(self.tmp.name, "file")
        open(blocker, "w").close()
        with cli.joined_lock(os.path.join(blocker, "joined.json")):
            pass                          # no raise


if __name__ == "__main__":
    unittest.main()
