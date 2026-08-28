"""The Dart SDK pin, and the resolution order that makes PATH the wrong lever.

`zonai compile` shells out to a `dart` to build the workers and the .aot
snapshots beside them; the host then loads those snapshots in-process through
`Isolate.spawnUri`. Build them with a newer SDK than the host embeds and the
spawn does not throw — it aborts the VM:

    ../../runtime/bin/snapshot_utils.cc: 269:
      error: Failed to resolve symbol 'kDartIsolateSnapshotData'

A native FATAL, so zonai's catch and its fallback to the worker process never
run, and the server dies on the first /db request with the client seeing a
closed connection and no status code.

WHY DART_SDK AND NOT PATH, which is the whole reason this file exists.
raindrop's DartExecutable.resolve tries a configured path, then DART_SDK, then
DART_HOME, then FVM AND FLUTTER INSTALL PATHS, then the running executable, and
only then `dart` on PATH. On a machine with ~/fvm/default the FVM candidate
wins, so exporting the right SDK on PATH changes nothing — a session spent
hours concluding "the SDK is irrelevant, both versions crash" on exactly that,
because every one of its compiles had silently used Flutter's bundled Dart.

WHAT THIS FILE DOES NOT CHECK: that Isolate.spawnUri still aborts on a
mismatch. That is zonai's behaviour and needs a real server and a real request.
What it defends is our half — that the pin is honest and that the value we
resolve is the one the subprocesses get.
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


class SdkVersionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def root(self, version):
        where = os.path.join(self.tmp.name, "sdk-" + version)
        os.makedirs(where)
        with open(os.path.join(where, "version"), "w") as handle:
            handle.write(version + "\n")
        return where

    def test_it_reads_the_version_file(self):
        self.assertEqual(cli.sdk_version(self.root("3.12.0")), "3.12.0")

    def test_a_directory_that_is_not_an_sdk_is_None_not_a_crash(self):
        """Every candidate is probed, and most of them will not exist on any
        given machine — so absence has to be an answer rather than an error."""
        self.assertIsNone(cli.sdk_version(os.path.join(self.tmp.name, "nope")))


class MatchingSdkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._env = os.environ.get("DART_SDK")

    def tearDown(self):
        if self._env is None:
            os.environ.pop("DART_SDK", None)
        else:
            os.environ["DART_SDK"] = self._env
        self.tmp.cleanup()

    def sdk(self, version):
        where = os.path.join(self.tmp.name, "sdk-" + version)
        os.makedirs(where, exist_ok=True)
        with open(os.path.join(where, "version"), "w") as handle:
            handle.write(version + "\n")
        return where

    def test_DART_SDK_is_honoured_when_it_matches(self):
        """A human who has already answered this must not be overruled by a
        version manager that happens to have a copy."""
        os.environ["DART_SDK"] = self.sdk(cli.HOST_DART)
        self.assertEqual(cli.matching_dart_sdk(), os.environ["DART_SDK"])

    def test_a_DART_SDK_of_the_WRONG_version_is_not_used(self):
        """The dangerous case, and the one a truthy check would wave through:
        the variable is set, so it looks configured, and it points at exactly
        the SDK that produces an unloadable snapshot."""
        os.environ["DART_SDK"] = self.sdk("3.13.2")
        self.assertNotEqual(cli.matching_dart_sdk(), os.environ["DART_SDK"])

    def test_the_match_is_exact_rather_than_a_prefix(self):
        """Patch releases are free to move the snapshot format, so 3.12.1 is
        not 3.12.0 and must not satisfy this."""
        os.environ["DART_SDK"] = self.sdk("3.12.10")
        self.assertNotEqual(cli.matching_dart_sdk(), os.environ["DART_SDK"])


class PinIsHonestTest(unittest.TestCase):
    def test_the_pin_matches_the_binary_when_it_can_be_read(self):
        """Paired with test_pins.py's copy on purpose: this file is where a
        reader looks for the SDK rule, and that one is where the three other
        version pins already live."""
        found = cli.host_dart_version()
        if found is None:
            self.skipTest("fat binary not extracted yet; run any zonai command")
        self.assertEqual(cli.HOST_DART, found)

    def test_an_unreadable_cache_is_None_rather_than_a_raise(self):
        """A fresh clone has never run ./zonai, so the slice does not exist.
        That has to read as 'cannot check' — a raise here would break setup on
        the one machine that has never built anything."""
        home = os.environ.get("HOME")
        with tempfile.TemporaryDirectory() as empty:
            os.environ["HOME"] = empty
            try:
                self.assertIsNone(cli.host_dart_version())
            finally:
                if home is not None:
                    os.environ["HOME"] = home


class WiredIntoTheBootstrapTest(unittest.TestCase):
    """Resolving the SDK and not PASSING it is the whole bug, restated.

    Every test above can pass while the compile still runs under Flutter's
    Dart, because raindrop reads the environment of the subprocess rather than
    anything this program holds in a variable.
    """

    def setUp(self):
        with open(os.path.join(cli.ROOT, "bin", "llm_chat")) as handle:
            source = handle.read()
        start = source.index("def start_server(")
        self.body = source[start:source.index("\ndef ", start + 1)]

    def test_start_server_resolves_a_matching_sdk(self):
        self.assertIn("matching_dart_sdk()", self.body)

    def test_it_refuses_rather_than_compiling_with_the_wrong_one(self):
        """Falling back to "whatever dart we find" would rebuild the exact
        failure this pin exists to prevent, and would do it silently."""
        self.assertIn("SystemExit", self.body)

    def test_DART_SDK_reaches_the_subprocesses(self):
        self.assertIn("DART_SDK=sdk", self.body)

    def test_the_SERVER_gets_it_too_not_just_the_compile(self):
        """zonai re-resolves dart at runtime as well; a server started without
        it can still reach for the wrong SDK."""
        popen = self.body[self.body.index("subprocess.Popen("):]
        self.assertIn("env=env", popen)


if __name__ == "__main__":
    unittest.main()
