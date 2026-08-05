"""Blessing a commit as a release, from a retro.

A consumer sat blocked for half a day because a fix existed, was pushed, and
was never published — the release step was the one part of the loop that lived
only in somebody remembering to do it. This automates the ASKING; lamp still
decides whether to grant.

Almost every test here is about REFUSING. A retro usually happens with nothing
worth blessing, so the common path is "not publishing: <reason>", and each
reason has to be true — a publish that fires when it should not names a commit
nobody tested and hands it to every consumer.
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import load  # noqa: E402


class FakeSubprocess:
    """Stands in for the whole `subprocess` module.

    NEVER assign to `mod.subprocess.run` — `mod.subprocess` IS the real, shared
    module, so that swaps subprocess.run process-wide for every test that
    follows. This repo has paid for it twice: once costing nineteen failures
    with a single cause, and once more today, in the file I wrote immediately
    after committing a message about having paid for it. Replacing the
    ATTRIBUTE on the module under test leaves the real one alone.
    """

    def __init__(self, run):
        self.run = run

pub = load("triggers/lamp-publish")

REGISTRY = {"geanies": {
    "thing": {"repo": "/repo/thing",
              "wishes": [{"number": 1, "sha": "a" * 40},
                         {"number": 2, "sha": "b" * 40}]},
    "other": {"repo": "/repo/other", "wishes": []},
}}


class GeanieLookupTest(unittest.TestCase):
    """Matched by PATH, not by a name in config — a name would have to be kept
    in step by hand, and a step kept by hand is why this trigger exists."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "registry.json")
        with open(self.path, "w") as f:
            json.dump(REGISTRY, f)

    def tearDown(self):
        self.tmp.cleanup()

    def test_it_finds_the_package_for_this_repo(self):
        self.assertEqual(pub.geanie_for("/repo/thing", self.path),
                         ("thing", "b" * 40))

    def test_it_returns_the_NEWEST_wish(self):
        """Comparing HEAD against an older wish would republish forever."""
        _, newest = pub.geanie_for("/repo/thing", self.path)
        self.assertEqual(newest, "b" * 40)

    def test_a_package_with_no_wishes_yet_has_no_newest(self):
        self.assertEqual(pub.geanie_for("/repo/other", self.path),
                         ("other", None))

    def test_an_unregistered_repo_is_not_a_guess(self):
        self.assertEqual(pub.geanie_for("/repo/nope", self.path), (None, None))

    def test_paths_are_compared_normalised(self):
        self.assertEqual(pub.geanie_for("/repo/thing/", self.path)[0], "thing")

    def test_a_missing_registry_is_not_an_error(self):
        self.assertEqual(pub.geanie_for("/repo/thing", "/no/such/file"),
                         (None, None))

    def test_a_corrupt_registry_is_not_an_error(self):
        with open(self.path, "w") as f:
            f.write("{not json")
        self.assertEqual(pub.geanie_for("/repo/thing", self.path), (None, None))


class FakeGit:
    """Answers by subcommand. A positional stub would hand the status answer to
    rev-parse and pass whatever it was asked."""

    def __init__(self, status="", head="c" * 40, unpushed="", code=0):
        self.status, self.head, self.unpushed, self.code = (
            status, head, unpushed, code)

    def __call__(self, repo, *args):
        if args[0] == "status":
            return self.code, self.status
        if args[0] == "rev-parse":
            return self.code, self.head
        if args[0] == "log":
            return 0, self.unpushed
        raise AssertionError("unexpected git call: %r" % (args,))


class WhyNotTest(unittest.TestCase):
    """Every refusal, because publishing when it should not is the expensive
    direction: it names a commit nobody tested and hands it to consumers."""

    def setUp(self):
        self.real = pub.git

    def tearDown(self):
        pub.git = self.real

    def test_an_unregistered_repo(self):
        self.assertIn("not registered", pub.why_not("/r", None, None))

    def test_a_dirty_tree_refuses(self):
        pub.git = FakeGit(status=" M bin/thing")
        self.assertIn("uncommitted", pub.why_not("/r", "thing", "a" * 40))

    def test_head_already_blessed_refuses(self):
        """Otherwise every retro grants another wish for the same commit."""
        pub.git = FakeGit(head="b" * 40)
        self.assertIn("already the newest", pub.why_not("/r", "thing", "b" * 40))

    def test_an_unpushed_head_refuses(self):
        """A blessed commit a consumer cannot fetch is worse than an unblessed
        one — the registry says it is available and the fetch fails."""
        pub.git = FakeGit(unpushed="c0ffee do a thing")
        self.assertIn("not pushed", pub.why_not("/r", "thing", "a" * 40))

    def test_git_being_unreadable_refuses_rather_than_guessing(self):
        pub.git = FakeGit(code=128)
        self.assertIn("cannot read", pub.why_not("/r", "thing", "a" * 40))

    def test_an_unresolvable_head_refuses(self):
        class OnlyStatusWorks(FakeGit):
            def __call__(self, repo, *args):
                if args[0] == "rev-parse":
                    return 1, ""
                return FakeGit.__call__(self, repo, *args)
        pub.git = OnlyStatusWorks()
        self.assertIn("resolve HEAD", pub.why_not("/r", "thing", "a" * 40))

    def test_a_clean_pushed_new_commit_is_publishable(self):
        """Paired with all of the above: a check that refused everything would
        pass every refusal test and never publish anything."""
        pub.git = FakeGit(head="c" * 40)
        self.assertIsNone(pub.why_not("/r", "thing", "a" * 40))

    def test_a_first_release_with_no_previous_wish_is_publishable(self):
        pub.git = FakeGit(head="c" * 40)
        self.assertIsNone(pub.why_not("/r", "thing", None))


class MainTest(unittest.TestCase):
    def setUp(self):
        self.real_git, self.real_sub = pub.git, pub.subprocess
        self.stdin = sys.stdin
        pub.git = FakeGit(head="c" * 40)
        pub.geanie_for = lambda repo, path=None: ("thing", "a" * 40)

    def tearDown(self):
        pub.git, pub.subprocess = self.real_git, self.real_sub
        sys.stdin = self.stdin

    def go(self, argv=()):
        sys.stdin = io.StringIO("{}")
        out = io.StringIO()
        with redirect_stdout(out):
            code = pub.main(["--repo", "/r", *argv])
        return code, out.getvalue()

    def stub_lamp(self, returncode=0, stdout="granted #5", stderr=""):
        calls = []

        class Result:
            pass

        def run(argv, **kwargs):
            calls.append(argv)
            r = Result()
            r.returncode, r.stdout, r.stderr = returncode, stdout, stderr
            return r
        pub.subprocess = FakeSubprocess(run)
        return calls

    def test_it_publishes_when_everything_is_ready(self):
        calls = self.stub_lamp()
        code, out = self.go()
        self.assertEqual(code, 0)
        self.assertIn("granted #5", out)
        self.assertEqual(calls[0][:2], ["lamp", "publish"])

    def test_dry_run_grants_nothing(self):
        calls = self.stub_lamp()
        code, out = self.go(["--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("would publish", out)
        self.assertEqual(calls, [])

    def test_a_refusal_publishes_nothing_and_is_not_an_error(self):
        """A retro with nothing to bless is the COMMON case. Exiting non-zero
        would make game_loop report a failed trigger on most retros, which is
        how a real signal becomes noise nobody reads."""
        pub.git = FakeGit(status=" M x")
        calls = self.stub_lamp()
        code, out = self.go()
        self.assertEqual(code, 0)
        self.assertIn("not publishing", out)
        self.assertEqual(calls, [])

    def test_a_gate_refusal_is_LOUD_but_does_not_pretend_to_succeed(self):
        """lamp refusing to bless a commit is the gate working. The retro
        still stands — a trigger never blocks the verb."""
        self.stub_lamp(returncode=1, stdout="polish failed: 3 tests")
        code, out = self.go()
        self.assertEqual(code, 1)
        self.assertIn("NOT published", out)
        self.assertIn("polish failed", out)

    def test_lamp_not_installed_is_quiet_and_clean(self):
        """Most installs have no lamp. A trigger that errors for everyone who
        does not use one particular tool is worse than no trigger."""
        def missing(argv, **kwargs):
            raise FileNotFoundError("lamp")
        pub.subprocess = FakeSubprocess(missing)
        code, out = self.go()
        self.assertEqual(code, 0)
        self.assertIn("not installed", out)

    def test_any_other_failure_is_reported_rather_than_raised(self):
        def explode(argv, **kwargs):
            raise OSError("boom")
        pub.subprocess = FakeSubprocess(explode)
        code, out = self.go()
        self.assertEqual(code, 1)
        self.assertIn("could NOT publish", out)

    def test_it_falls_back_to_stderr_when_lamp_says_nothing_on_stdout(self):
        self.stub_lamp(stdout="", stderr="something happened")
        _, out = self.go()
        self.assertIn("something happened", out)

    def test_it_publishes_from_the_calling_repo(self):
        calls = self.stub_lamp()
        self.go()
        self.assertEqual(calls[0][:2], ["lamp", "publish"])

    def test_it_drains_stdin_so_the_writer_never_blocks(self):
        self.stub_lamp()
        sys.stdin = io.StringIO("x" * 100000)
        with redirect_stdout(io.StringIO()):
            pub.main(["--repo", "/r"])
        self.assertEqual(sys.stdin.read(), "")


class GitSeamTest(unittest.TestCase):
    """The one place this shells out to git. Every decision above rests on it,
    so what it asks and what it returns are asserted rather than assumed."""

    def setUp(self):
        self.real = pub.subprocess

    def tearDown(self):
        pub.subprocess = self.real

    def test_it_runs_git_in_the_repo_it_was_given(self):
        seen = {}

        class Result:
            returncode, stdout, stderr = 0, " M x\n", ""

        def run(argv, **kwargs):
            seen["argv"] = argv
            return Result()
        pub.subprocess = FakeSubprocess(run)
        code, out = pub.git("/some/repo", "status", "--porcelain")
        self.assertEqual(seen["argv"][:3], ["git", "-C", "/some/repo"])
        self.assertEqual((code, out), (0, "M x"))

    def test_a_failing_git_returns_its_code_rather_than_raising(self):
        class Result:
            returncode, stdout, stderr = 128, "", "not a repository"
        pub.subprocess = FakeSubprocess(lambda argv, **kw: Result())
        self.assertEqual(pub.git("/nope", "status")[0], 128)


class CallingRepoTest(unittest.TestCase):
    def test_the_harness_answer_wins(self):
        self.assertEqual(pub.calling_repo({"GAME_LOOP_REPO": "/x"}), "/x")

    def test_it_falls_back_to_the_cwd_when_run_by_hand(self):
        self.assertEqual(pub.calling_repo({}), os.getcwd())

    def test_reads_the_real_environment_by_default(self):
        self.assertTrue(pub.calling_repo())


class EntryPointTest(unittest.TestCase):
    def test_it_is_executable(self):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "triggers", "lamp-publish")
        self.assertTrue(os.access(path, os.X_OK))


if __name__ == "__main__":
    unittest.main()
