"""`identify` with no name answers "who am I?" instead of erroring.

Issue #33, reported from a shared monorepo checkout with reproducible numbers.
`identify` was SET-ONLY — its positional was required — and `doctor` printed
160 lines without naming the identity anywhere, so the only way to learn it
was `cat .llm_chat/identity.json`: reaching past the CLI into a state
directory whose format nothing promises.

The sharpest evidence in that report is a briefing already in the wild:

    llm_chat identity resolves from CWD and we share one, so run
    'llm_chat identify' and confirm it names you before posting.

Followed literally that SETS the identity to whatever the agent guessed. The
house rule assumed a verb that did not exist, so the only thing the CLI could
do with that instruction was the wrong one.
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

cli = load("llm_chat")


class WhoamiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name
        os.environ["CLAUDE_PROJECT_DIR"] = self.project
        os.makedirs(os.path.join(self.project, ".llm_chat"), exist_ok=True)

    def tearDown(self):
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.tmp.cleanup()

    def joined(self, mapping):
        # SESSION-SCOPED, not `.llm_chat/joined.json` — the path carries the
        # session id, which is the whole reason two sessions in one checkout
        # can hold different names and the reason this verb exists.
        path = cli.joined_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({room: {"identity": who}
                       for room, who in mapping.items()}, f)

    def default(self, name, shared=False):
        path = cli.identity_path(shared=shared)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"identity": name}, f)

    def whoami(self, as_json=False):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.do_whoami(as_json=as_json)
        return code, out.getvalue()

    def test_it_reports_the_name_the_ROOMS_use(self):
        """THE BUG THE FIRST VERSION OF THIS VERB HAD. It read
        `identity.json` and reported "no identity recorded" on a checkout that
        posts as `owner` in three rooms — because `identity_for` resolves PER
        CHANNEL out of joined.json, and identity.json is only the default a
        future `join` takes.

        Naming the default and calling it the answer is the same
        narrower-question defect the verb exists to fix."""
        self.joined({"learnings": "owner", "llm_chat_owner": "owner"})
        code, said = self.whoami()
        self.assertEqual(code, 0)
        self.assertIn("posting as 'owner'", said)
        self.assertIn("#learnings", said)

    def test_MORE_THAN_ONE_NAME_is_named_as_such(self):
        """Issue #33's second ask: three names attach to one session and
        nothing said which one posts. Per-room names are legal and are also
        how a shared checkout ends up split, so the report says both."""
        self.joined({"a": "backcompat", "b": "drops-ed"})
        code, said = self.whoami()
        self.assertEqual(code, 0)
        self.assertIn("backcompat", said)
        self.assertIn("drops-ed", said)
        self.assertIn("MORE THAN ONE NAME", said)

    def test_a_DEFAULT_no_room_uses_is_called_a_default(self):
        """The reported checkout's exact shape: identity.json said
        `backcompat` while the session was known elsewhere as `drops-ed`. A
        report that printed only one of them would recreate the confusion."""
        self.joined({"a": "drops-ed"})
        self.default("backcompat")
        code, said = self.whoami()
        self.assertIn("posting as 'drops-ed'", said)
        self.assertIn("'backcompat'", said)
        self.assertIn("default a future `join`", said)

    def test_NO_ROOMS_but_a_default_says_nothing_has_been_posted(self):
        """A name with no rooms has never posted anything, and saying
        "posting as X" flatly would be a claim about traffic that does not
        exist."""
        self.default("newcomer")
        code, said = self.whoami()
        self.assertEqual(code, 0)
        self.assertIn("NO ROOMS", said)

    def test_NOTHING_AT_ALL_exits_nonzero(self):
        """So a gate can branch on it. Exit 0 with no name would make "I could
        not tell" indistinguishable from "I am nobody"."""
        code, said = self.whoami()
        self.assertEqual(code, 1)
        self.assertIn("no identity anywhere", said)

    def test_json_carries_the_per_room_mapping(self):
        """`--json` is for the gates the issue asks about, so it must carry
        the thing that varies — which name in which room — not just a
        flattened answer."""
        self.joined({"a": "one", "b": "two"})
        code, said = self.whoami(as_json=True)
        payload = json.loads(said)
        self.assertEqual(payload["by_room"], {"a": "one", "b": "two"})
        self.assertEqual(sorted(payload["posts_as"]), ["one", "two"])

    def test_reading_NEVER_WRITES(self):
        """The whole point. The briefing in the wild told agents to run this
        to CHECK their name; the old verb would have overwritten it."""
        self.joined({"a": "existing"})
        before = sorted(os.listdir(os.path.join(self.project, ".llm_chat")))
        self.whoami()
        self.whoami(as_json=True)
        self.assertEqual(
            sorted(os.listdir(os.path.join(self.project, ".llm_chat"))),
            before, "the read mode created or removed a file")

    def test_a_CORRUPT_joined_file_does_not_crash_the_report(self):
        """A diagnostic that dies while diagnosing leaves the reader where
        they started — reading state files by hand.

        Handled by `read_joined`, which already returns {} for an unreadable
        file. A try/except was written here as well and the coverage report
        showed its body was never reached; it is gone rather than tested,
        because a guard that cannot fire reads exactly like one that works.
        """
        path = cli.joined_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("{not json")
        self.default("fallback")
        code, said = self.whoami()
        self.assertEqual(code, 0)
        self.assertIn("fallback", said)

    def test_the_VERB_dispatches_with_no_name(self):
        """The wiring, not just the function. `identify` with no positional
        has to reach the report rather than argparse's usage error — that
        argument was REQUIRED, which is the whole defect."""
        self.joined({"a": "someone"})
        out = io.StringIO()
        real = sys.argv
        sys.argv = ["llm_chat", "identify"]
        try:
            with redirect_stdout(out):
                code = cli.main()
        finally:
            sys.argv = real
        self.assertEqual(code, 0)
        self.assertIn("posting as 'someone'", out.getvalue())

    def test_dispatch_still_SETS_when_given_a_name(self):
        """Paired, and the one that must not regress: making the positional
        optional must not turn the setter into a no-op."""
        calls = []
        real = cli.do_identify
        cli.do_identify = lambda server, identity, shared=False: calls.append(
            (identity, shared))
        self.addCleanup(lambda: setattr(cli, "do_identify", real))
        argv = sys.argv
        sys.argv = ["llm_chat", "identify", "newname"]
        try:
            with redirect_stdout(io.StringIO()):
                cli.main()
        finally:
            sys.argv = argv
        self.assertEqual(calls, [("newname", False)])


if __name__ == "__main__":
    unittest.main()
