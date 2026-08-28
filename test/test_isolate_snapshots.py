"""The .aot snapshots that kill the server, and the removal that keeps it up.

zonai's mailman prefers in-process dispatch: when `.zonai/executables/
db_rules.aot` exists it hands that path to `Isolate.spawnUri`. The spawn does
not throw when it cannot load the snapshot — it aborts the VM inside the
runtime with

    ../../runtime/bin/snapshot_utils.cc: 269:
      error: Failed to resolve symbol 'kDartIsolateSnapshotData'

so zonai's own `catch`, and the fallback to the worker process that it guards,
never run. The server binds, logs "Serving at", and dies on the FIRST /db
request with the connection closed and no status code.

MEASURED IN BOTH DIRECTIONS before this file was written: remove the two
snapshots and `llm_chat channels` answers, a channel opens and a message
round-trips with the server still alive; copy them back and the next request
kills it. Reproduced on zonai 0.8.5 and 0.7.1, under Dart 3.13.2 and 3.12.0.

WHY A TEST AND NOT A COMMENT. `zonai compile` RECREATES these files, so the
removal is not a one-time cleanup — it has to run after every compile this
project performs, forever, or the tree silently goes back to being one that
cannot serve. A comment saying so is exactly the shape of thing this repo has
already watched drift: the compile gate carried "exits 0 when it fails" in
prose for months while still shelling out to a bare `./zonai compile`.

WHAT THIS DOES NOT CHECK, stated rather than implied: that `Isolate.spawnUri`
still aborts. That needs a real server and a real request, and it is zonai's
behaviour rather than ours — if zonai fixes the spawn to fail catchably, these
tests keep passing and the removal simply stops being load-bearing. What they
DO defend is that our side keeps doing the thing that currently makes the
difference between a server that answers and one that dies.
"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import load  # noqa: E402

cli = load("llm_chat")


class SnapshotNamesTest(unittest.TestCase):
    def test_both_snapshots_zonai_emits_are_named(self):
        """zonai writes an .aot for rules AND for operations, and either one is
        enough to abort the spawn. Naming only db_rules.aot would leave a tree
        that still dies, which is the failure this whole file exists to stop."""
        self.assertEqual(set(cli.SNAPSHOTS),
                         {"db_rules.aot", "db_operations.aot"})

    def test_every_named_snapshot_is_an_aot(self):
        """A `.exe` in this tuple would delete a WORKER, and the workers are
        what serve once in-process dispatch is gone — so the tree would stop
        serving for the opposite reason."""
        for name in cli.SNAPSHOTS:
            self.assertTrue(name.endswith(".aot"), name)


class DropIsolateSnapshotsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.built = os.path.join(self.tmp.name, ".zonai", "executables")
        os.makedirs(self.built)
        self._root = cli.ROOT
        cli.ROOT = self.tmp.name

    def tearDown(self):
        cli.ROOT = self._root
        self.tmp.cleanup()

    def drop(self):
        """Call it with stdout captured — it reports what it removed, and a
        suite that let that through would print into the test output."""
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            dropped = cli.drop_isolate_snapshots()
        return dropped, buffer.getvalue()

    def write(self, name):
        path = os.path.join(self.built, name)
        with open(path, "w") as handle:
            handle.write("x")
        return path

    def test_it_removes_every_snapshot_that_is_there(self):
        for name in cli.SNAPSHOTS:
            self.write(name)
        dropped, said = self.drop()
        self.assertEqual(sorted(dropped), sorted(cli.SNAPSHOTS))
        for name in cli.SNAPSHOTS:
            self.assertIn(name, said, "the removal was silent")
        for name in cli.SNAPSHOTS:
            self.assertFalse(os.path.exists(os.path.join(self.built, name)),
                             f"{name} survived")

    def test_it_reports_only_what_was_actually_there(self):
        """The distinction the return value exists for: "removed one" and
        "removed nothing" are different states, and a bool would flatten them."""
        self.write("db_rules.aot")
        dropped, said = self.drop()
        self.assertEqual(dropped, ["db_rules.aot"])
        self.assertNotIn("db_operations.aot", said)

    def test_a_clean_tree_is_not_an_error(self):
        """It runs after EVERY compile, including ones that emitted no snapshot,
        so finding nothing must be silent rather than a failure."""
        dropped, said = self.drop()
        self.assertEqual(dropped, [])
        self.assertEqual(said, "", "a clean tree should say nothing")

    def test_it_leaves_the_worker_executables_alone(self):
        """The workers are what serve once the snapshots are gone. Deleting one
        here would swap a server that dies on request one for a server that
        cannot start."""
        for worker in cli.WORKERS:
            self.write(worker + ".exe")
        self.drop()
        for worker in cli.WORKERS:
            self.assertTrue(
                os.path.exists(os.path.join(self.built, worker + ".exe")),
                f"{worker}.exe was deleted")


class StartServerDropsThemTest(unittest.TestCase):
    """The removal has to be WIRED IN, not merely available.

    A correct `drop_isolate_snapshots` that nothing calls leaves the tree
    exactly as broken as no function at all, and every test above it would
    still pass — which is the gap this class closes.
    """

    def test_start_server_calls_it_after_compiling(self):
        with open(os.path.join(cli.ROOT, "bin", "llm_chat")) as handle:
            source = handle.read()
        start = source.index("def start_server(")
        body = source[start:source.index("\ndef ", start + 1)]
        self.assertIn("drop_isolate_snapshots()", body,
                      "start_server compiles and then serves without dropping "
                      "the snapshots, so the server it starts dies on the "
                      "first /db request")


if __name__ == "__main__":
    unittest.main()
