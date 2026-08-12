"""The gate's own guards — the checks that watch the suite rather than the code.

`fingerprint_repo` exists to catch a test that escapes its temp directory and
writes into the real repo. It watches `.llm_chat/` and `.claude/`, which is
also where the LIVE hooks write while anyone is working here — so it could
report the session's own activity as suite damage, and did.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run as gate  # noqa: E402


class RepoDamageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.real_root = gate.ROOT
        gate.ROOT = self.tmp.name
        os.makedirs(os.path.join(self.tmp.name, ".llm_chat", "probe"))
        os.makedirs(os.path.join(self.tmp.name, ".claude"))

    def tearDown(self):
        gate.ROOT = self.real_root
        self.tmp.cleanup()

    def write(self, *parts, text="x"):
        path = os.path.join(self.tmp.name, *parts)
        with open(path, "w") as f:
            f.write(text)
        return path

    def test_a_PROBE_MARKER_APPEARING_MID_RUN_IS_NOT_SUITE_DAMAGE(self):
        """The regression. `llm-chat-deliver` stamps .llm_chat/probe/ on every
        tool call in this repo, so a marker appearing during the run means an
        agent ran a tool — the normal state of working here, and nothing this
        check can tell apart from a test escaping.

        It failed the gate intermittently on its own author: a `lamp publish`
        was refused, the reason was then discarded by a pager, and the failure
        was written up as unreproducible. It was not. It reproduces whenever
        something is actively using the repo."""
        before = gate.fingerprint_repo()
        self.write(".llm_chat", "probe", "post-tool-use")
        after = gate.fingerprint_repo()
        self.assertFalse(gate.report_repo_damage(before, after))

    def test_a_CHANGED_probe_marker_is_also_not_damage(self):
        """They are rewritten, not just created — every tool call restamps."""
        self.write(".llm_chat", "probe", "post-tool-use", text="first")
        before = gate.fingerprint_repo()
        self.write(".llm_chat", "probe", "post-tool-use", text="second")
        self.assertFalse(gate.report_repo_damage(before, gate.fingerprint_repo()))

    def test_A_WAKER_STAMP_MID_RUN_IS_NOT_SUITE_DAMAGE(self):
        """The same regression as the probe marker, through the two files the
        comment describing it already named and the exclusion list omitted.

        The waker writes wake.pid and wake.exit every time it starts or is
        superseded, which during a 20-second suite run is ordinary. It refused
        a second `lamp publish` — 891 tests OK, 100% coverage, exit 1 — and
        like the first one it reproduces only while an agent is working in the
        repo, which is exactly when a release is cut.
        """
        for stamp in ("wake.pid", "wake.exit"):
            with self.subTest(file=stamp):
                before = gate.fingerprint_repo()
                self.write(".llm_chat", stamp, text="37300")
                self.assertFalse(
                    gate.report_repo_damage(before, gate.fingerprint_repo()))

    def test_a_CHANGED_waker_stamp_is_also_not_damage(self):
        """Restamped on every restart, not only created."""
        self.write(".llm_chat", "wake.pid", text="1")
        before = gate.fingerprint_repo()
        self.write(".llm_chat", "wake.pid", text="2")
        self.assertFalse(gate.report_repo_damage(before, gate.fingerprint_repo()))

    def test_the_exclusion_is_matched_per_FILE_not_per_directory(self):
        """The fix would have looked applied and changed nothing.

        The exclusion was tested against `dirpath`, which worked only because
        the single entry was a DIRECTORY (`.llm_chat/probe/`). Adding a plain
        file to the tuple would have left the walk hashing it anyway — the list
        naming it, the gate still failing, and the difference invisible without
        this assertion. `wake.pid` sits directly in `.llm_chat/`, whose own
        relpath matches no exclusion."""
        self.assertIn(os.path.join(".llm_chat", "wake.pid"), gate.UNGUARDED)
        self.write(".llm_chat", "wake.pid", text="x")
        hashed = gate.fingerprint_repo()
        self.assertFalse(any(path.endswith("wake.pid") for path in hashed),
                         "wake.pid is in UNGUARDED and still got fingerprinted "
                         "— the exclusion is being matched against directories")

    def test_a_real_escape_ELSEWHERE_in_llm_chat_is_still_caught(self):
        """Paired, and the reason this is an exclusion rather than dropping
        the guard: a test writing identity or membership into the real repo is
        exactly what it is for."""
        before = gate.fingerprint_repo()
        self.write(".llm_chat", "identity.json")
        self.assertTrue(gate.report_repo_damage(before, gate.fingerprint_repo()))

    def test_a_real_escape_into_claude_is_still_caught(self):
        before = gate.fingerprint_repo()
        self.write(".claude", "settings.local.json")
        self.assertTrue(gate.report_repo_damage(before, gate.fingerprint_repo()))

    def test_an_unchanged_repo_reports_nothing(self):
        self.write(".claude", "settings.local.json")
        before = gate.fingerprint_repo()
        self.assertFalse(gate.report_repo_damage(before, gate.fingerprint_repo()))


if __name__ == "__main__":
    unittest.main()
