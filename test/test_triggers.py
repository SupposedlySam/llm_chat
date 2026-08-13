"""The game_loop triggers: what gets broadcast, and what a retro is handed.

Two scripts that run at moments in another tool's loop, so the thing worth
defending is not "does it work" but "does it stay quiet when it should, and
loud when it fails". A broadcast trigger that silently posts nothing is
indistinguishable from a room where nobody has learned anything, and a digest
that silently returns nothing is the same lie from the other side.

Every subprocess is faked. These scripts shell out to the CLI, and a test that
let them reach the real one would post into a channel other agents are sitting
in — the blast-radius rule applies to the test suite too.
"""
import io
import json
import os
import shlex
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import ROOT, load  # noqa: E402

broadcast = load("triggers/learnings-broadcast")
digest = load("triggers/learnings-digest")

# Captured from a real `game_loop harden`, not invented. rung is a STRING and
# artifact is a list of ABSOLUTE paths; both disagreed with what the source
# reads like. See .game_loop/triggers.json for why that matters.
REAL_HARDEN = {
    "event": "harden",
    "learning": "the incident form",
    "general": "the transferable form",
    "artifact": ["/Users/someone/dev/llm_chat/test/test_wiring.py"],
    "mechanism": "a paired test",
    "rung": "2",
    "project": "llm_chat",
    "session": "73ce3b55",
}


class FakeRun:
    """Stands in for subprocess.run inside ONE module.

    Assigning to the module's `subprocess` attribute would patch the real
    subprocess module for the whole interpreter — done once in this repo
    already, and it cost nineteen failures with a single cause. So only the
    `run` name on the module is replaced, and it is restored in tearDown.
    """

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return self

    @property
    def argv(self):
        return self.calls[-1][0]


class ComposeTest(unittest.TestCase):
    """What actually goes into the room."""

    def test_no_general_form_means_nothing_to_say(self):
        payload = dict(REAL_HARDEN, general=None)
        self.assertIsNone(broadcast.compose(payload))

    def test_a_blank_general_form_is_also_nothing(self):
        self.assertIsNone(broadcast.compose(dict(REAL_HARDEN, general="   ")))

    def test_missing_general_key_entirely(self):
        payload = {k: v for k, v in REAL_HARDEN.items() if k != "general"}
        self.assertIsNone(broadcast.compose(payload))

    def test_carries_the_general_form_and_its_provenance(self):
        text = broadcast.compose(REAL_HARDEN)
        self.assertIn("the transferable form", text)
        self.assertIn("llm_chat", text)
        self.assertIn("rung 2", text)
        self.assertIn("a paired test", text)

    def test_never_carries_the_incident_form(self):
        """The whole point of --general. The incident does not travel, and a
        channel full of other people's incidents is one nobody reads."""
        self.assertNotIn("the incident form", broadcast.compose(REAL_HARDEN))

    def test_never_publishes_this_machines_paths(self):
        """artifact arrives as ABSOLUTE paths. Relaying them tells everyone in
        the room where this repo sits on disk, and is useless to all of them —
        a reader cannot open a path inside somebody else's checkout. Observed
        in the wild: another project's posts carry theirs."""
        text = broadcast.compose(REAL_HARDEN)
        self.assertNotIn("/Users/someone", text)
        self.assertNotIn("test_wiring.py", text)

    def test_survives_a_payload_with_neither_rung_nor_mechanism(self):
        text = broadcast.compose({"general": "g", "project": "p"})
        self.assertIn("g", text)
        self.assertIn("p", text)
        self.assertNotIn("rung", text)

    def test_names_the_project_even_when_the_payload_does_not(self):
        text = broadcast.compose({"general": "g"})
        self.assertIn("unnamed project", text)


class BroadcastMainTest(unittest.TestCase):
    def setUp(self):
        self.real = broadcast.subprocess.run
        self.run = FakeRun(stdout="sent #13 to learnings as owner")
        broadcast.subprocess.run = self.run
        self.stdin = sys.stdin

    def tearDown(self):
        broadcast.subprocess.run = self.real
        sys.stdin = self.stdin

    def go(self, payload, argv):
        sys.stdin = io.StringIO(payload if isinstance(payload, str)
                                else json.dumps(payload))
        out = io.StringIO()
        with redirect_stdout(out):
            code = broadcast.main(argv)
        return code, out.getvalue()

    def test_posts_the_composed_message(self):
        code, out = self.go(REAL_HARDEN, ["--as", "owner"])
        self.assertEqual(code, 0)
        self.assertIn("broadcast to #learnings", out)
        self.assertEqual(len(self.run.calls), 1)

    def test_sends_via_file_so_quoting_cannot_eat_it(self):
        """A learning about shell quoting that cannot survive a shell is a joke
        this project has already earned once."""
        self.go(REAL_HARDEN, ["--as", "owner"])
        self.assertIn("--file", self.run.argv)
        self.assertNotIn("the transferable form", self.run.argv)

    def test_the_file_actually_holds_the_message_while_the_cli_runs(self):
        """--file is worthless if the file is empty or already deleted. The
        content is captured mid-call, which is the only moment it must exist."""
        seen = {}

        def peek(argv, **kwargs):
            path = argv[argv.index("--file") + 1]
            with open(path) as f:
                seen["text"] = f.read()
            return FakeRun(stdout="ok")(argv, **kwargs)

        broadcast.subprocess.run = peek
        self.go(REAL_HARDEN, ["--as", "owner"])
        self.assertIn("the transferable form", seen["text"])

    def test_the_temp_file_does_not_outlive_the_call(self):
        paths = []
        real_run = self.run

        def note(argv, **kwargs):
            paths.append(argv[argv.index("--file") + 1])
            return real_run(argv, **kwargs)

        broadcast.subprocess.run = note
        self.go(REAL_HARDEN, ["--as", "owner"])
        self.assertFalse(os.path.exists(paths[0]))

    def test_passes_the_room_and_identity_through(self):
        self.go(REAL_HARDEN, ["--as", "someone", "--room", "elsewhere"])
        self.assertIn("elsewhere", self.run.argv)
        self.assertIn("someone", self.run.argv)

    def test_nothing_to_share_is_a_success_that_says_so(self):
        """Not a failure — most hardens are local. But saying it keeps 'nothing
        to share' distinguishable from 'the trigger is broken', which is the
        distinction a silent success destroys."""
        code, out = self.go(dict(REAL_HARDEN, general=None), ["--as", "owner"])
        self.assertEqual(code, 0)
        self.assertIn("nothing was broadcast", out)
        self.assertEqual(self.run.calls, [])

    def test_a_failed_post_is_loud_and_non_zero(self):
        broadcast.subprocess.run = FakeRun(returncode=1, stderr="room closed")
        code, out = self.go(REAL_HARDEN, ["--as", "owner"])
        self.assertEqual(code, 1)
        self.assertIn("could NOT broadcast", out)
        self.assertIn("room closed", out)

    def test_a_failure_with_nothing_on_stderr_still_reports_something(self):
        broadcast.subprocess.run = FakeRun(returncode=3)
        code, out = self.go(REAL_HARDEN, ["--as", "owner"])
        self.assertEqual(code, 1)
        self.assertIn("could NOT broadcast", out)

    def test_unreadable_payload_is_reported_not_crashed(self):
        """game_loop prints a trigger's failure and carries on. An uncaught
        traceback here would land in the middle of a harden's output."""
        code, out = self.go("{not json", ["--as", "owner"])
        self.assertEqual(code, 1)
        self.assertIn("unreadable payload", out)
        self.assertEqual(self.run.calls, [])

    def test_empty_stdin_is_treated_as_an_empty_payload(self):
        code, out = self.go("", ["--as", "owner"])
        self.assertEqual(code, 0)
        self.assertIn("nothing was broadcast", out)

    def test_dry_run_posts_nothing(self):
        code, out = self.go(REAL_HARDEN, ["--as", "owner", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("would post", out)
        self.assertIn("the transferable form", out)
        self.assertEqual(self.run.calls, [])


def records(*items):
    """What `read --json` emits."""
    return json.dumps([{"seq": i + 1, "from": who, "text": text,
                        "audience": None, "mine": mine}
                       for i, (who, text, mine) in enumerate(items)])


class SplitTest(unittest.TestCase):
    """Message boundaries come from the STORE, never from the rendering."""

    def test_keeps_a_multi_line_message_whole(self):
        payload = records(("a", "one\ncontinued\n\nstill a", False),
                          ("b", "two", False))
        self.assertEqual(digest.split_messages(payload),
                         [("a", "one\ncontinued\n\nstill a"), ("b", "two")])

    def test_a_bracketed_line_inside_a_body_is_NOT_a_new_speaker(self):
        """THE REPORTED DEFECT, reproduced. The old parser split on any line
        starting with '[', so a shell test inside a learning became a phantom
        speaker — and because the own-post filter is identity-based, a phantom
        name PASSES it and half your own learning comes back to you as somebody
        else's. Reported with this exact line."""
        shell = '[ "$(git -C "$SRC" rev-parse --show-toplevel)" = "$SRC" ]'
        body = "stamp the content:\n%s\nnot the ref" % shell
        parsed = digest.split_messages(records(("me", body, True)))
        self.assertEqual(len(parsed), 1, "one message, not two")
        self.assertEqual(parsed[0], ("me", body))

    def test_the_other_bracket_shapes_that_would_have_split(self):
        """A learnings room is exactly where these live."""
        for line in ("[INFO] starting up", "[owner] said otherwise",
                     "[1] first item", "[tool.poetry]", '["a", "b"]'):
            with self.subTest(line=line):
                body = "before\n%s\nafter" % line
                self.assertEqual(len(digest.split_messages(
                    records(("me", body, False)))), 1)

    def test_empty_transcript(self):
        self.assertEqual(digest.split_messages(""), [])

    def test_unparseable_payload_is_no_messages_rather_than_a_crash(self):
        self.assertEqual(digest.split_messages("{not json"), [])

    def test_drops_my_own_messages(self):
        """A retro that hands you back your own learnings is a mirror."""
        payload = records(("me", "mine", True), ("them", "theirs", False))
        parsed = digest.split_messages(payload)
        self.assertEqual(digest.from_others(parsed, json.loads(payload)),
                         [("them", "theirs")])

    def test_it_uses_the_stores_flag_not_a_marker_in_the_text(self):
        """An agent whose message merely CONTAINS '(you)' was treated as
        itself by the old marker-matching filter."""
        payload = records(("them", "the docs say (you) here", False))
        parsed = digest.split_messages(payload)
        self.assertEqual(len(digest.from_others(parsed, json.loads(payload))), 1)

    def test_drops_a_speaker_with_an_empty_body(self):
        payload = records(("a", "", False))
        self.assertEqual(
            digest.from_others(digest.split_messages(payload),
                               json.loads(payload)), [])


class RenderTest(unittest.TestCase):
    def test_says_so_when_nobody_has_posted(self):
        self.assertIn("no learnings", digest.render([], "learnings", 8))

    def test_shows_the_most_recent_and_says_how_many_there_were(self):
        messages = [("a", str(n)) for n in range(10)]
        text = digest.render(messages, "learnings", 3)
        self.assertIn("most recent of 10", text)
        self.assertIn("9", text)
        self.assertNotIn("[a] 5", text)

    def test_does_not_claim_truncation_when_it_showed_everything(self):
        text = digest.render([("a", "one")], "learnings", 8)
        self.assertNotIn("most recent", text)


class DigestMainTest(unittest.TestCase):
    def setUp(self):
        self.real = digest.subprocess.run
        self.stdin = sys.stdin

    def tearDown(self):
        digest.subprocess.run = self.real
        sys.stdin = self.stdin

    def go(self, argv):
        sys.stdin = io.StringIO("{}")
        out = io.StringIO()
        with redirect_stdout(out):
            code = digest.main(argv)
        return code, out.getvalue()

    def test_reports_other_agents_learnings(self):
        digest.subprocess.run = FakeRun(
            stdout=records(("wcs", "capture a real payload", False)))
        code, out = self.go(["--as", "owner"])
        self.assertEqual(code, 0)
        self.assertIn("capture a real payload", out)
        self.assertIn("wcs", out)

    def test_never_advances_the_cursor(self):
        """The PostToolUse hook already consumes this room and moves the cursor.
        Two readers, one cursor, and the quiet one loses: an unread-only read
        would report 'nothing new' when it means 'somebody else took it'."""
        run = FakeRun(stdout=records(("a", "x", False)))
        digest.subprocess.run = run
        self.go(["--as", "owner"])
        self.assertIn("--peek", run.argv)
        self.assertIn("--all", run.argv)
        self.assertIn("--json", run.argv)

    def test_a_read_failure_is_loud_and_non_zero(self):
        digest.subprocess.run = FakeRun(returncode=1, stderr="no such channel")
        code, out = self.go(["--as", "owner"])
        self.assertEqual(code, 1)
        self.assertIn("could NOT read", out)
        self.assertIn("no such channel", out)

    def test_unparseable_output_is_reported_rather_than_read_as_empty(self):
        """'no learnings' and 'I could not read the answer' are different
        facts, and only one of them means nobody has learned anything."""
        digest.subprocess.run = FakeRun(stdout="{not json")
        code, out = self.go(["--as", "owner"])
        self.assertEqual(code, 1)
        self.assertIn("could NOT parse", out)

    def test_a_failure_falls_back_to_stdout_when_stderr_is_empty(self):
        digest.subprocess.run = FakeRun(returncode=1, stdout="closed")
        code, out = self.go(["--as", "owner"])
        self.assertEqual(code, 1)
        self.assertIn("closed", out)

    def test_an_empty_room_is_not_an_error(self):
        digest.subprocess.run = FakeRun(stdout="[]")
        code, out = self.go(["--as", "owner"])
        self.assertEqual(code, 0)
        self.assertIn("no learnings", out)

    def test_honours_the_limit_and_the_room(self):
        run = FakeRun(stdout=records(*[("a", str(n), False) for n in range(6)]))
        digest.subprocess.run = run
        code, out = self.go(["--as", "owner", "--room", "elsewhere",
                             "--limit", "2"])
        self.assertIn("elsewhere", run.argv)
        self.assertIn("most recent of 6", out)

    def test_drains_stdin_so_the_writer_never_blocks(self):
        """game_loop writes the payload to this process's stdin. Exiting without
        reading it can hand the writer an EPIPE on a large payload, and the
        trigger would be reported as failing for a reason nothing here explains."""
        digest.subprocess.run = FakeRun(stdout=records(("a", "x", False)))
        sys.stdin = io.StringIO("x" * 100000)
        with redirect_stdout(io.StringIO()):
            digest.main(["--as", "owner"])
        self.assertEqual(sys.stdin.read(), "")


class CallingRepoTest(unittest.TestCase):
    """WHOSE project is this, decided explicitly rather than inherited.

    The CLI resolves a project by walking up from its cwd, so an inherited cwd
    silently decides which identity a post is filed under. game_loop happens to
    run triggers from the repo root, which made this correct by luck — captured,
    not assumed: a trigger that printed os.getcwd() during a real harden.

    All three links are tested because of a neighbouring project's report: their
    equivalent read the same env var, it is unset when a human runs the script
    by hand, and so the code was correct in production and wrong in exactly the
    context they tested it in. The verification run published the bad message.
    """

    def test_the_harness_answer_wins(self):
        for module in (broadcast, digest):
            self.assertEqual(module.calling_repo({"GAME_LOOP_REPO": "/repo"}),
                             "/repo")

    def test_falls_back_to_the_cwd_when_run_by_hand(self):
        for module in (broadcast, digest):
            self.assertEqual(module.calling_repo({}), os.getcwd())

    def test_an_empty_env_var_is_not_an_answer(self):
        """Set-but-empty is how this fails in a shell script, and treating it as
        an answer would hand subprocess an empty cwd."""
        for module in (broadcast, digest):
            self.assertEqual(module.calling_repo({"GAME_LOOP_REPO": ""}),
                             os.getcwd())

    def test_reads_the_real_environment_by_default(self):
        for module in (broadcast, digest):
            self.assertTrue(module.calling_repo())


class ExplicitCwdTest(unittest.TestCase):
    """The cwd is PASSED to the CLI, not left to whatever this process has."""

    def setUp(self):
        self.real = (broadcast.subprocess.run, digest.subprocess.run)
        self.stdin = sys.stdin

    def tearDown(self):
        broadcast.subprocess.run, digest.subprocess.run = self.real
        sys.stdin = self.stdin

    def test_broadcast_passes_an_explicit_cwd(self):
        run = FakeRun(stdout="sent")
        broadcast.subprocess.run = run
        sys.stdin = io.StringIO(json.dumps(REAL_HARDEN))
        with redirect_stdout(io.StringIO()):
            broadcast.main(["--as", "owner", "--repo", "/somewhere"])
        self.assertEqual(run.calls[-1][1]["cwd"], "/somewhere")

    def test_digest_passes_an_explicit_cwd(self):
        run = FakeRun(stdout="[a] x")
        digest.subprocess.run = run
        sys.stdin = io.StringIO("{}")
        with redirect_stdout(io.StringIO()):
            digest.main(["--as", "owner", "--repo", "/somewhere"])
        self.assertEqual(run.calls[-1][1]["cwd"], "/somewhere")

    def test_neither_ever_leaves_the_cwd_to_chance(self):
        """The point is not which value — it is that one was chosen. A missing
        cwd kwarg means the CLI inherits whatever this process happened to have."""
        for module, payload in ((broadcast, json.dumps(REAL_HARDEN)),
                                (digest, "{}")):
            run = FakeRun(stdout="[a] x")
            module.subprocess.run = run
            sys.stdin = io.StringIO(payload)
            with redirect_stdout(io.StringIO()):
                module.main(["--as", "owner"])
            self.assertIn("cwd", run.calls[-1][1])
            self.assertTrue(run.calls[-1][1]["cwd"])


class EntryPointTest(unittest.TestCase):
    """Both are run as commands by another tool, not imported."""

    def test_both_are_executable(self):
        for script in ("learnings-broadcast", "learnings-digest"):
            path = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "triggers", script)
            self.assertTrue(os.access(path, os.X_OK), script)

    def test_both_declare_a_main_returning_an_exit_code(self):
        for module in (broadcast, digest):
            self.assertTrue(callable(module.main))


class WiredTriggersResolveTest(unittest.TestCase):
    """Every command in THIS machine's triggers.json points at something that
    exists and can be run.

    The failure this exists for is silence. game_loop fires a trigger whose
    command is gone, that firing fails, and the visible result is a moment
    where nothing happened — indistinguishable from a moment where nothing
    needed to happen. The release reminder is the worst case: "nothing to
    publish" is its most common CORRECT output, so a broken one looks exactly
    like a working one until a release quietly does not go out.

    That stopped being hypothetical when `lamp-publish` moved to the lamp repo
    and this file started naming a path in somebody else's tree. They guard
    their end — their suite asserts the file exists, resolved from their repo
    root rather than from `__file__`, so renaming the directory fails their
    gate rather than relocating with it. What neither of us could guard from
    one side is the whole repo MOVING: their check follows the move and stays
    green, while the absolute path here goes stale.

    So this asserts the property from the consuming end, where the absolute
    path actually lives. It names no particular tool — it reads whatever is
    configured, which is the only version that does not re-introduce the
    problem the move solved.

    Skipped rather than failed when triggers.json is absent: it is gitignored,
    holds absolute paths belonging to one machine, and a clone legitimately
    has none.
    """

    def setUp(self):
        self.path = os.path.join(ROOT, ".game_loop", "triggers.json")
        if not os.path.isfile(self.path):
            self.skipTest("no .game_loop/triggers.json (gitignored, per-machine)")
        with open(self.path) as f:
            self.config = json.load(f)

    # Interpreters that take the real target as an argument. Without this the
    # check reads `sh` as the thing to look for and asserts a file named "sh"
    # exists — the same command-position mistake this repo fixed in
    # piped-verdict, made again in the check written to catch missing files.
    RUNNERS = ("sh", "bash", "zsh", "python", "python3", "node", "ruby", "env")

    def wired(self):
        """(moment, name, target) for every configured trigger.

        The TARGET, not the first token: a command may name an interpreter and
        pass the script, and `$GAME_LOOP_ROOT` is expanded by the harness at
        fire time rather than stored expanded.
        """
        root = os.path.join(ROOT, ".game_loop")
        for moment, entries in self.config.items():
            if moment.startswith("/"):      # the "//" comment block
                continue
            for entry in entries:
                parts = shlex.split(entry.get("command", ""))
                if not parts:
                    continue
                index = 0
                if os.path.basename(parts[0]) in self.RUNNERS:
                    index = 1
                    while index < len(parts) and parts[index].startswith("-"):
                        index += 1
                if index >= len(parts):
                    continue
                target = parts[index].replace("$GAME_LOOP_ROOT", root)
                target = os.path.expandvars(target)
                yield moment, entry.get("name", "?"), target

    def test_every_wired_trigger_exists_and_is_executable(self):
        for moment, name, executable in self.wired():
            with self.subTest(moment=moment, trigger=name):
                self.assertTrue(
                    os.path.isfile(executable),
                    "%s trigger %r points at a file that does not exist: %s\n"
                    "  game_loop would fire this and fail SILENTLY — the moment "
                    "looks identical\n  to one where nothing needed doing."
                    % (moment, name, executable))
                self.assertTrue(
                    os.access(executable, os.X_OK),
                    "%s trigger %r is not executable: %s"
                    % (moment, name, executable))

    def test_the_check_would_notice_a_path_that_moved(self):
        """Paired, because the test above passes trivially on a machine with
        no triggers wired, and 'it passed' must not mean 'it looked at
        nothing'."""
        self.assertTrue(any(True for _ in self.wired()),
                        "triggers.json exists but wires nothing — the check "
                        "above would pass without examining anything")
        moved = os.path.join(ROOT, ".game_loop", "no-such-trigger")
        self.assertFalse(os.path.isfile(moved))


if __name__ == "__main__":
    unittest.main()
