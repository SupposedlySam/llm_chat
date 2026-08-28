"""Three version pins that must agree, checked instead of described.

The vendored `zonai` binary, `version:` in `zonai.yaml`, and the `zonai_schema`
`ref:` in `pubspec.yaml` are one decision written in three files. Disagreement
does not fail loudly in one place — it fails differently in three:

- binary vs zonai.yaml: every command refuses with "Version mismatch", which is
  at least honest.
- binary vs schema ref: `zonai compile` prints "Failed to compile rules:" and
  EXITS 0, so the bootstrap continues and starts a server whose rules worker
  does not exist. Every /db request then returns 500, which reads as a wire
  problem rather than a build one. That is how the 0.6.2 upgrade actually
  presented, and it cost the time it took to find `db_rules.exe` missing.

The README and llms.txt both now say "bump all three or none". That sentence is
exactly the kind this project has learned not to trust: it is true, it is
written down, and nothing stops the next person skipping one. The fat launcher
carries its own `ZONAI_VERSION=` in its shell header, so the check is cheap.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import ROOT  # noqa: E402


def binary_version():
    """`ZONAI_VERSION="0.6.2"` from the fat launcher's /bin/sh header.

    Read as bytes with errors ignored: everything after the payload marker is
    compressed binary, and the header is plain ASCII in the first few hundred
    bytes.
    """
    with open(os.path.join(ROOT, "zonai"), "rb") as f:
        head = f.read(4096).decode("ascii", "ignore")
    found = re.search(r'ZONAI_VERSION="([^"]+)"', head)
    return found.group(1) if found else None


def yaml_version():
    with open(os.path.join(ROOT, "zonai.yaml")) as f:
        found = re.search(r"^version:\s*(\S+)", f.read(), re.M)
    return found.group(1) if found else None


def schema_ref():
    with open(os.path.join(ROOT, "pubspec.yaml")) as f:
        found = re.search(r"^\s*ref:\s*v?(\S+)", f.read(), re.M)
    return found.group(1) if found else None


class VersionPinTest(unittest.TestCase):
    def test_the_vendored_binary_declares_a_version(self):
        """If this fails the binary is not the fat build — the per-arch ones
        are Mach-O/ELF with no shell header, and the two checks below would
        then both pass vacuously by comparing None to None."""
        self.assertIsNotNone(binary_version(),
                             "no ZONAI_VERSION in ./zonai; is it the fat build?")

    def test_zonai_yaml_matches_the_binary(self):
        """A mismatch here refuses every command with 'Version mismatch'."""
        self.assertEqual(yaml_version(), binary_version())

    def test_the_schema_ref_matches_the_binary(self):
        """A mismatch here does NOT refuse. compile prints that it failed,
        exits 0, and the server starts without a rules worker."""
        self.assertEqual(schema_ref(), binary_version())


class HostDartPinTest(unittest.TestCase):
    """The FOURTH thing that must agree, and the one with no loud failure.

    The other three refuse or misbehave visibly. This one does not: build the
    workers with an SDK newer than the host's and everything reports success —
    compile prints its counts, the workers exist, the suite is green — and the
    server then aborts the VM on the first /db request, which reaches the
    client as a closed connection rather than any kind of error.

    So it is checked here, against the binary itself rather than against a note
    somebody kept up to date.
    """

    def setUp(self):
        sys.path.insert(0, os.path.join(ROOT, "test"))
        from support import load
        self.cli = load("llm_chat")

    def test_HOST_DART_matches_the_vendored_binary(self):
        found = self.cli.host_dart_version()
        if found is None:
            self.skipTest(
                "the fat binary has not been extracted yet — only the "
                "unpacked slice in ~/.cache/zonai/fat carries the version in "
                "the clear, so a tree that has never run ./zonai cannot "
                "answer this. Run any zonai command and re-run.")
        self.assertEqual(
            self.cli.HOST_DART, found,
            "bin/llm_chat pins Dart %s and the vendored zonai host embeds %s; "
            "the workers would be built for a runtime that cannot load them"
            % (self.cli.HOST_DART, found))


if __name__ == "__main__":
    unittest.main()
