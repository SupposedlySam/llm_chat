"""The two hooks: what reaches an agent, and what proves the hook ran at all.

Both compute their project at import time, so each test loads a fresh copy with
the environment already arranged. Both also shell out to the CLI, which is
stubbed here — the CLI's own behaviour is covered directly elsewhere, and what
matters at this layer is what the hook does with the answer.
"""
import io
import json
import os
import re
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import load, write_settings  # noqa: E402


class FakeSubprocess:
    """Stands in for the whole `subprocess` module.

    Never assign to `mod.subprocess.run` — `mod.subprocess` IS the real,
    shared module, so that swaps subprocess.run process-wide for every test
    that follows and nothing restores it. It happened: the shell tests then
    "ran" install.sh, got a canned exit 0, and asserted against files nothing
    had written. Replacing the ATTRIBUTE on the module under test leaves the
    real one alone.
    """

    def __init__(self, *outputs):
        self.run = Stub(*outputs)


class Stub:
    """Stands in for subprocess.run, returning canned CLI output."""

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        out = self.outputs.pop(0) if self.outputs else ""

        class Result:
            stdout = out
            stderr = ""
            returncode = 0
        return Result()


class HookTestCase(unittest.TestCase):
    script = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name
        os.environ["CLAUDE_PROJECT_DIR"] = self.project
        # Fully wired by default: otherwise every delivery assertion also picks
        # up the "older wiring" notice, and a test that asserts two things at
        # once fails without telling you which.
        write_settings(self.project,
                       PostToolUse=["/x/bin/llm-chat-deliver"],
                       Stop=["/x/bin/llm-chat-wake"],
                       SessionStart=["/x/bin/llm-chat-wake"])
        self.mod = load(self.script)

    def tearDown(self):
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.tmp.cleanup()

    def joined(self, **rooms):
        d = os.path.join(self.project, ".llm_chat")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "joined.json"), "w") as f:
            json.dump({name: {"identity": who, "server": "http://127.0.0.1:1"}
                       for name, who in rooms.items()}, f)

    def probes(self):
        d = os.path.join(self.project, ".llm_chat", "probe")
        return sorted(os.listdir(d)) if os.path.isdir(d) else []


class DeliverTest(HookTestCase):
    script = "llm-chat-deliver"

    def run_hook(self, payload="{}"):
        out, err = io.StringIO(), io.StringIO()
        stdin = sys.stdin
        sys.stdin = io.StringIO(payload)
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = self.mod.main()
        finally:
            sys.stdin = stdin
        return code, out.getvalue()

    def test_in_no_rooms_it_says_nothing(self):
        """Silence is the default: this runs after EVERY tool call, so any
        output that is not a message is noise on a loop."""
        code, out = self.run_hook()
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_AN_UNREADABLE_ROOM_LIST_IS_REPORTED_not_rendered_as_silence(self):
        """The agent is the only party left who can act on it.

        This hook's silence means "nothing waiting", and it runs after every
        tool call — so a resolver that folds "could not read" into "no rooms"
        makes the session DEAF while every sender sees delivery succeed, every
        room still lists them, and `doctor` agrees with the silence because it
        shares the resolver. Nothing downstream is positioned to notice, which
        is why this one refuses out loud instead.

        Reported by showrunner, confirmed from source, sharpened by wcs — and
        it reached me only because I rejoined the room I had left."""
        path = os.path.join(self.project, ".llm_chat", "joined.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("{not json")
        code, out = self.run_hook()
        self.assertEqual(code, 0, "it must not take the turn down with it")
        self.assertIn("could not read", out)
        self.assertIn("NOT 'nothing waiting'", out)

    def test_it_marks_that_it_fired_even_with_nothing_to_deliver(self):
        """The mark's ABSENCE is the only readable evidence that a registered
        hook has never run, so it has to be written before any early return."""
        self.run_hook()
        self.assertIn("post-tool-use", self.probes())

    def test_waiting_messages_are_returned_as_additional_context(self):
        self.joined(room="me")
        self.mod.subprocess = FakeSubprocess(json.dumps(
            [{"seq": 1, "from": "other", "text": "hello there",
              "audience": "me", "mine": False}]))
        _, out = self.run_hook()
        payload = json.loads(out)
        context = payload["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"],
                         "PostToolUse")
        self.assertIn("hello there", context)
        self.assertIn("#room", context)
        self.assertIn("ANOTHER AGENT", context,
                      "the header has to say these are not the agent's own words")

    def test_the_hook_uses_the_DELIVERY_path_not_the_TRANSCRIPT_one(self):
        """The self-filter lives in `read`, and `--all` disables it.

        Reported by an agent who saw their own message delivered with the
        `(you)` marker, which belongs to the transcript format. Not reproduced
        here or by a third agent, and git shows the hook has never passed
        `--all` — but the property is worth pinning regardless, because adding
        that flag later would silently recreate the self-answering loop this
        project's invariants call the expensive one, and nothing else would
        notice.
        """
        self.joined(room="me")
        stub = Stub("[other] hello")
        self.mod.subprocess = FakeSubprocess()
        self.mod.subprocess.run = stub
        self.run_hook()
        argv, = stub.calls
        self.assertIn("read", argv)
        self.assertNotIn("--all", argv,
                         "--all disables the self-filter; the hook must never "
                         "ask for the transcript")
        self.assertNotIn("--peek", argv,
                         "--peek would deliver the same message on every tool "
                         "call, forever")

    def test_the_hook_reads_as_the_identity_that_joined_that_room(self):
        """The filter can only exclude your own words if it is told who you
        are. One project holds a different identity per channel, so passing
        the wrong one filters nothing."""
        self.joined(alpha="me", beta="someone-else")
        stub = Stub("nothing new", "nothing new")
        self.mod.subprocess = FakeSubprocess()
        self.mod.subprocess.run = stub
        self.run_hook()
        pairs = {argv[argv.index("read") + 1]: argv[argv.index("--as") + 1]
                 for argv in stub.calls}
        self.assertEqual(pairs, {"alpha": "me", "beta": "someone-else"})

    def test_nothing_new_is_not_a_delivery(self):
        self.joined(room="me")
        self.mod.subprocess = FakeSubprocess("nothing new in room")
        code, out = self.run_hook()
        self.assertEqual(out, "")

    def test_a_chat_outage_never_breaks_the_session(self):
        def explode(*a, **kw):
            raise OSError("server gone")
        self.joined(room="me")
        self.mod.subprocess = type('M', (), {'run': staticmethod(explode)})
        code, out = self.run_hook()
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_one_delivery_is_capped_so_it_cannot_derail_a_turn(self):
        self.joined(room="me")
        many = json.dumps([{"seq": i, "from": "other",
                            "text": "line %d" % i, "audience": "me",
                            "mine": False} for i in range(50)])
        self.mod.subprocess = FakeSubprocess(many)
        _, out = self.run_hook()
        context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        # MATCH THE MESSAGE BODIES, NOT ANY LINE MENTIONING THEM. This read
        # `"line " in l` over the WHOLE context, header included, so the count
        # was of "lines containing a substring" rather than of messages
        # delivered. Wording the header as "a one-line reply" pushed it to 16
        # — the header is not a message, and the assertion could not tell.
        delivered = [l for l in context.splitlines()
                     if re.search(r"\bline \d+$", l)]
        self.assertEqual(len(delivered), self.mod.MAX_PER_DELIVERY)

    def test_a_missing_hook_is_reported_once_per_session(self):
        """The old hook reporting the new one that is missing — the only channel
        available when the missing hook is the silent one."""
        write_settings(self.project, PostToolUse=["/x/bin/llm-chat-deliver"])
        _, first = self.run_hook('{"session_id": "s1"}')
        self.assertIn("llm-chat-wake", first)
        self.assertIn("OLDER wiring", first)
        _, second = self.run_hook('{"session_id": "s1"}')
        self.assertEqual(second, "", "a standing gap must not become standing noise")

    def test_a_new_session_hears_it_again(self):
        write_settings(self.project, PostToolUse=["/x/bin/llm-chat-deliver"])
        self.run_hook('{"session_id": "s1"}')
        _, other = self.run_hook('{"session_id": "s2"}')
        self.assertIn("llm-chat-wake", other)

    def test_fully_wired_repos_get_no_notice(self):
        write_settings(self.project,
                       PostToolUse=["/x/bin/llm-chat-deliver"],
                       Stop=["/x/bin/llm-chat-wake"])
        _, out = self.run_hook('{"session_id": "s1"}')
        self.assertEqual(out, "")

    def test_drifted_hook_scripts_are_reported_though_registration_matches(self):
        """The case hook-comparison is blind to: same command line, different
        code behind it."""
        write_settings(self.project,
                       PostToolUse=["/x/bin/llm-chat-deliver"],
                       Stop=["/x/bin/llm-chat-wake"])
        d = os.path.join(self.project, ".llm_chat")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "installed.json"), "w") as f:
            json.dump({"fingerprint": "0000000000000000"}, f)
        self.mod.subprocess = FakeSubprocess("ffffffffffffffff")
        _, out = self.run_hook('{"session_id": "s1"}')
        self.assertIn("hook scripts changed", out)
        self.assertIn("0000000000000000", out)

    def test_a_matching_stamp_is_not_reported_as_drift(self):
        write_settings(self.project,
                       PostToolUse=["/x/bin/llm-chat-deliver"],
                       Stop=["/x/bin/llm-chat-wake"])
        d = os.path.join(self.project, ".llm_chat")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "installed.json"), "w") as f:
            json.dump({"fingerprint": "abcdef0123456789"}, f)
        self.mod.subprocess = FakeSubprocess("abcdef0123456789")
        _, out = self.run_hook('{"session_id": "s1"}')
        self.assertEqual(out, "")


class MissedWakeNoticeTest(HookTestCase):
    """The record that existed for months and that nothing ever opened.

    The waker spawns a detached watcher purely so a rewake that went nowhere
    can be noticed after the exit — and it wrote `wake.missed` to a path no
    code in this repo read. #20 is what that costs: a message addressed to an
    agent sat 32 minutes, `doctor` could state the live state precisely, and
    two agents in one room each concluded the other had gone quiet.
    """

    script = "llm-chat-deliver"

    def run_hook(self, payload="{}"):
        out = io.StringIO()
        stdin = sys.stdin
        sys.stdin = io.StringIO(payload)
        try:
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = self.mod.main()
        finally:
            sys.stdin = stdin
        return code, out.getvalue()

    def missed(self, at=None, **extra):
        record = {"at": at if at is not None else int(time.time()),
                  "requested_at": 1, "acted": False}
        record.update(extra)
        os.makedirs(os.path.dirname(self.mod.MISSED_PATH), exist_ok=True)
        with open(self.mod.MISSED_PATH, "w") as f:
            json.dump(record, f)

    def test_a_missed_wake_is_SAID_not_merely_filed(self):
        self.missed()
        _, out = self.run_hook()
        self.assertIn("NEVER LANDED", out)

    def test_it_says_the_messages_arrived_by_TOOL_CALL_not_by_wake(self):
        """The reporter's own account of how the reply reached them. Without
        it the line reads as a past-tense complaint rather than a statement
        about what will happen next time they go idle."""
        self.missed()
        _, out = self.run_hook()
        self.assertIn("tool call", out)

    def test_it_says_silence_is_not_evidence_of_a_quiet_room(self):
        """The conclusion the agent would otherwise draw, and the one that
        made two agents both stop waiting at the same time."""
        self.missed()
        _, out = self.run_hook()
        self.assertIn("not evidence", out)

    def test_it_is_said_ONCE(self):
        """A line on every tool call is read for a day and filtered out for
        good — and this is a warning it would be expensive to stop believing."""
        self.missed()
        _, first = self.run_hook()
        self.assertIn("NEVER LANDED", first)
        _, second = self.run_hook()
        self.assertEqual(second, "")

    def test_a_LATER_miss_is_said_again(self):
        """Paired with the one above. Remembering "told" rather than "told
        about this one" would report the first failure and then go silent
        through every one after it."""
        self.missed(at=1000)
        self.run_hook()
        self.missed(at=2000)
        _, out = self.run_hook()
        self.assertIn("NEVER LANDED", out)

    def landed(self, at):
        with open(self.mod.LANDED_PATH, "w") as f:
            json.dump({"at": at, "event": "Stop"}, f)

    def test_a_LATER_landing_retires_the_miss(self):
        """The path recovered. Saying it anyway would be the defect `doctor`
        already carries a scar for — a fact about a moment printed as a fact
        about now."""
        self.missed(at=1000)
        self.landed(2000)
        _, out = self.run_hook()
        self.assertEqual(out, "")

    def test_an_EARLIER_landing_does_not_retire_it(self):
        """Paired, and it is why age is the wrong test. This was caught
        against real state: a miss from the previous evening was still the
        live state the next afternoon, because the last landing came four
        hours BEFORE it."""
        self.missed(at=2000)
        self.landed(1000)
        _, out = self.run_hook()
        self.assertIn("NEVER LANDED", out)

    def test_NO_landing_record_at_all_does_not_retire_it(self):
        """No wake has ever been seen arriving in this checkout, which is not
        evidence that one just did."""
        self.missed(at=2000)
        _, out = self.run_hook()
        self.assertIn("NEVER LANDED", out)

    def test_it_says_the_miss_is_the_LIVE_state_not_history(self):
        """The line carries an age, and an age invites the reader to discount
        it. What licenses the claim is the absence of a later landing, so the
        line has to say that rather than leave it to be inferred."""
        self.missed(at=2000)
        self.landed(1000)
        _, out = self.run_hook()
        self.assertIn("none has landed since", out)

    def test_a_record_with_no_timestamp_is_not_a_miss(self):
        """A record from a watcher that predates the field, or a truncated
        write. There is nothing to compare a landing against, so there is
        nothing that can honestly be said."""
        os.makedirs(os.path.dirname(self.mod.MISSED_PATH), exist_ok=True)
        with open(self.mod.MISSED_PATH, "w") as f:
            json.dump({"requested_at": 1}, f)
        _, out = self.run_hook()
        self.assertEqual(out, "")

    def test_it_stays_SILENT_when_it_cannot_remember_having_spoken(self):
        """Better to say nothing once than to say it on every tool call
        forever — an unwritable marker turns "once per miss" into a loop, and
        the loop is what makes the warning stop being read."""
        self.missed()
        blocker = os.path.join(self.project, "not-a-directory")
        open(blocker, "w").close()
        self.mod.TOLD_PATH = os.path.join(blocker, "told")
        _, out = self.run_hook()
        self.assertEqual(out, "")

    def test_no_record_means_silence(self):
        _, out = self.run_hook()
        self.assertEqual(out, "")

    def test_an_unreadable_record_does_not_break_the_turn(self):
        os.makedirs(os.path.dirname(self.mod.MISSED_PATH), exist_ok=True)
        with open(self.mod.MISSED_PATH, "w") as f:
            f.write("{not json")
        code, out = self.run_hook()
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_the_record_is_NOT_consumed(self):
        """`doctor` and any later reader are entitled to it. Spending the
        evidence to deliver it once is how the next question gets no answer."""
        self.missed()
        self.run_hook()
        self.assertTrue(os.path.exists(self.mod.MISSED_PATH))

    def test_an_automatic_reload_attempt_is_reported_with_its_outcome(self):
        self.missed(acted=True, said="reload requested")
        _, out = self.run_hook()
        self.assertIn("reload requested", out)


class DivergentCheckoutTest(HookTestCase):
    """A detector on the side of the gap with the NEWER build on it.

    `doctor` warns about this too, and cannot help the case it was built for:
    it lives in the CLI you TYPE, and divergence matters precisely when the
    CLI you type is OLD — which is when it does not contain the warning. So
    that check fires when your copy is newer and is silent when it is older,
    and older is the common direction, because a vendored payload goes stale
    by sitting still while the source moves.

    A hook runs the current code by construction. It can say what no old CLI
    can be made to say. Found by gameloop, who grepped both binaries rather
    than taking my word that the check worked.
    """

    script = "llm-chat-deliver"

    def vendor(self, where, fingerprint):
        """A second checkout inside the project, with a stated build."""
        path = os.path.join(self.project, where, "bin")
        os.makedirs(path, exist_ok=True)
        open(os.path.join(path, "llm_chat"), "w").close()
        self.prints[os.path.abspath(os.path.join(self.project, where))] = \
            fingerprint
        return os.path.abspath(os.path.join(self.project, where))

    def setUp(self):
        super().setUp()
        self.prints = {os.path.abspath(self.mod.ROOT): "aaaaaaaaaaaaaaaa"}
        # Kept before stubbing, so the two tests that exercise the REAL
        # reader still can. Everything else wants the table.
        self.real_fingerprint = self.mod.fingerprint_of
        self.mod.fingerprint_of = lambda tree: self.prints.get(
            os.path.abspath(tree))

    def test_a_vendored_copy_of_a_DIFFERENT_build_is_reported(self):
        self.vendor(".lamp/llm_chat", "bbbbbbbbbbbbbbbb")
        self.assertIn("DIFFERENT BUILD", self.mod.upgrade_notice("s1"))

    def test_a_vendored_copy_of_the_SAME_build_is_SILENT(self):
        """The guard against crying wolf, and the reason this compares
        fingerprints rather than paths. A repo whose hooks were installed
        FROM its vendored copy is correctly configured and would otherwise be
        nagged forever about itself."""
        self.vendor(".lamp/llm_chat", "aaaaaaaaaaaaaaaa")
        self.assertEqual(self.mod.divergent_checkouts(), [])

    def test_the_notice_names_BOTH_trees(self):
        """One path is not actionable — the reader has to be able to tell
        which of the two they have been typing."""
        tree = self.vendor("vendor/llm_chat", "bbbbbbbbbbbbbbbb")
        text = self.mod.upgrade_notice("s1")
        self.assertIn(tree, text)
        self.assertIn(self.mod.ROOT, text)

    def test_it_says_DELIVERY_is_still_current(self):
        """Otherwise it reads as "your messages are broken", which is the
        opposite of true: the hooks are absolute paths into the newer tree."""
        self.vendor(".lamp/llm_chat", "bbbbbbbbbbbbbbbb")
        self.assertIn("DELIVERY is current", self.mod.upgrade_notice("s1"))

    def test_it_says_an_OLD_copy_may_be_missing_this_very_warning(self):
        """The circularity is the point of the finding, so it is said out
        loud rather than left for the reader to deduce."""
        self.vendor(".lamp/llm_chat", "bbbbbbbbbbbbbbbb")
        self.assertIn("would have told you", self.mod.upgrade_notice("s1"))

    def test_the_hooks_OWN_tree_is_never_reported_as_a_second_copy(self):
        """ROOT is moved INSIDE the project for this, because otherwise the
        walk could never reach it and the test passes for the wrong reason —
        a repo installed from a checkout vendored within itself is exactly
        the configuration that must stay silent."""
        inside = self.vendor("tools/llm_chat", "aaaaaaaaaaaaaaaa")
        self.mod.ROOT = inside
        self.assertEqual(self.mod.other_checkouts(), [])

    def test_it_does_not_descend_INTO_a_checkout_it_found(self):
        """A checkout contains its own subdirectories, and a nested walk
        there is cost with nothing at the end of it."""
        self.vendor(".lamp/llm_chat", "bbbbbbbbbbbbbbbb")
        os.makedirs(os.path.join(self.project, ".lamp", "llm_chat",
                                 "inner", "bin"), exist_ok=True)
        open(os.path.join(self.project, ".lamp", "llm_chat", "inner", "bin",
                          "llm_chat"), "w").close()
        self.assertEqual(len(self.mod.other_checkouts()), 1)

    def test_a_copy_beyond_the_depth_limit_is_not_hunted_for(self):
        """Stated rather than implied: this runs after every tool call, and
        an unbounded walk is not worth a rarer catch. Vendored payloads sit
        shallow."""
        deep = os.path.join(*(["deep"] * (self.mod.VENDOR_DEPTH + 2)))
        self.vendor(os.path.join(deep, "llm_chat"), "bbbbbbbbbbbbbbbb")
        self.assertEqual(self.mod.other_checkouts(), [])

    def test_junk_directories_are_skipped(self):
        self.vendor(os.path.join("node_modules", "llm_chat"), "bbbb")
        self.assertEqual(self.mod.other_checkouts(), [])

    def test_an_UNKNOWABLE_fingerprint_says_nothing(self):
        """Cannot compare, so cannot claim. Reporting a divergence on the
        strength of a failed hash would fire for everybody with a second copy
        of any build.

        OURS is unknowable and THEIRS is not, deliberately. Stubbing both to
        None makes the mutation and the fix agree — no candidate has a hash,
        so nothing is appended either way — and the test proves nothing. That
        exact fixture shape has now got past me three times in one day."""
        self.vendor(".lamp/llm_chat", "bbbbbbbbbbbbbbbb")
        del self.prints[os.path.abspath(self.mod.ROOT)]
        self.assertEqual(self.mod.divergent_checkouts(), [])

    def test_it_is_said_once_per_session_like_the_rest(self):
        self.vendor(".lamp/llm_chat", "bbbbbbbbbbbbbbbb")
        self.assertIn("DIFFERENT BUILD", self.mod.upgrade_notice("s1"))
        self.assertEqual(self.mod.upgrade_notice("s1"), "")

    def test_the_fingerprint_is_ASKED_OF_THE_CLI_not_recomputed(self):
        """Two copies of a hash algorithm drift, and a drift detector that
        has drifted is worse than none — the same reason `stale_install`
        shells out rather than hashing here."""
        asked = []

        class Fake:
            @staticmethod
            def run(argv, **kw):
                asked.append(argv)
                return type("R", (), {"returncode": 0, "stdout": "abc123\n",
                                      "stderr": ""})()
        self.mod.subprocess = Fake
        self.assertEqual(self.real_fingerprint("/some/tree"), "abc123")
        self.assertIn("fingerprint", asked[0])
        self.assertIn("/some/tree", asked[0])

    def test_a_fingerprint_that_cannot_be_taken_is_None(self):
        """A CLI that will not start. Never let bookkeeping break the turn,
        and never let a failure read as a hash — `divergent_checkouts` turns
        None into silence, and would turn a bogus string into a warning."""
        class Boom:
            @staticmethod
            def run(*a, **kw):
                raise OSError("no interpreter")
        self.mod.subprocess = Boom
        self.assertIsNone(self.real_fingerprint("/x"))

    def test_an_EMPTY_answer_is_None_rather_than_a_hash(self):
        class Blank:
            @staticmethod
            def run(*a, **kw):
                return type("R", (), {"returncode": 1, "stdout": "",
                                      "stderr": "no such tree"})()
        self.mod.subprocess = Blank
        self.assertIsNone(self.real_fingerprint("/x"))

    def test_it_does_not_claim_the_WIRING_is_old(self):
        """A separate block on purpose. The wiring is fine and the hooks are
        current — saying "this repo is running an OLDER wiring" would be
        false, and `install.sh` is not the remedy for typing the wrong
        binary."""
        self.vendor(".lamp/llm_chat", "bbbbbbbbbbbbbbbb")
        self.assertNotIn("OLDER wiring", self.mod.upgrade_notice("s1"))


class HookEventNameTest(HookTestCase):
    """The reply has to name the event that actually fired.

    This hook is registered on PostToolUse AND SessionStart now, and a
    hardcoded `hookEventName` is the kind of wrong that costs nothing in
    testing and silently discards the output in the one place it was added
    for — a session starting up deaf.
    """

    script = "llm-chat-deliver"

    def run_hook(self, payload):
        out = io.StringIO()
        stdin = sys.stdin
        sys.stdin = io.StringIO(payload)
        try:
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                self.mod.main()
        finally:
            sys.stdin = stdin
        return out.getvalue()

    def test_it_answers_a_SessionStart_as_a_SessionStart(self):
        os.makedirs(os.path.dirname(self.mod.MISSED_PATH), exist_ok=True)
        with open(self.mod.MISSED_PATH, "w") as f:
            json.dump({"at": int(time.time())}, f)
        out = self.run_hook('{"hook_event_name": "SessionStart"}')
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["hookEventName"],
                         "SessionStart")

    def test_a_payload_that_names_no_event_is_assumed_PostToolUse(self):
        """Every caller before this change was PostToolUse, and a payload that
        cannot be parsed must not produce output the host will drop."""
        os.makedirs(os.path.dirname(self.mod.MISSED_PATH), exist_ok=True)
        with open(self.mod.MISSED_PATH, "w") as f:
            json.dump({"at": int(time.time())}, f)
        out = self.run_hook("{not json")
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["hookEventName"],
                         "PostToolUse")

    def test_the_per_event_mark_does_not_replace_the_one_doctor_reads(self):
        """`doctor` and `last_activity` both read the bare name. Writing only
        a per-event mark would make every older reader see a hook that had
        stopped firing."""
        self.run_hook('{"hook_event_name": "SessionStart"}')
        self.assertIn("post-tool-use", self.probes())
        self.assertIn("deliver-SessionStart", self.probes())


class DriftNoticeTest(HookTestCase):
    """What the notice TELLS you to do, given what the source actually is.

    The detector was right and the remedy was not. Reported by an agent that
    got this twice in minutes: the source HEAD had not moved, the fingerprint
    was being shifted by uncommitted files — one of them the wake hook — and
    'fix it with install.sh' would have wired a live session to a half-finished
    hook. The wake hook is what delivers the message telling you it broke.
    """
    script = "llm-chat-deliver"

    def arrange(self, dirty):
        d = os.path.join(self.project, ".llm_chat")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "installed.json"), "w") as f:
            json.dump({"fingerprint": "old"}, f)

        class Routed:
            """Answers by ARGV, because this path makes two different calls —
            `fingerprint` and `git status` — and a positional stub would hand
            the git answer to the fingerprint call."""

            def __init__(self, dirty):
                self.dirty = dirty

            def __call__(self, argv, **kwargs):
                status = "status" in argv
                text = ((" M bin/llm-chat-wake\n" if self.dirty else "")
                        if status else "new")

                class Result:
                    stdout = text
                    stderr = ""
                    returncode = 0
                return Result()

        # The ATTRIBUTE on the module under test, never mod.subprocess.run —
        # that IS the real shared module, and assigning to it swaps
        # subprocess.run process-wide for every test that follows. This repo
        # has paid for that once; I just re-paid for it writing this test, and
        # the eight shell-test errors had one cause again.
        fake = FakeSubprocess()
        fake.run = Routed(dirty)
        self.mod.subprocess = fake
        return self.mod.upgrade_notice("session-1")

    def test_a_dirty_source_is_named_in_the_notice(self):
        notice = self.arrange(dirty=True)
        self.assertIn("UNCOMMITTED", notice)
        self.assertIn("blessed", notice)

    def test_a_clean_source_says_nothing_extra(self):
        """Paired with the test above: a note that always fires teaches
        nothing, and the usual case is a source somebody committed."""
        notice = self.arrange(dirty=False)
        self.assertIn("OLDER wiring", notice)
        self.assertNotIn("UNCOMMITTED", notice)

    def test_it_compares_the_tree_the_repo_was_WIRED_FROM(self):
        """A vendored consumer runs its hooks out of its own copy. Comparing
        against ROOT reported a permanent STALE for a repo matching its own
        source exactly — and this hook fires automatically, so it says it on
        every session rather than only when somebody runs doctor.

        The stub answers BY ARGV: the vendored tree hashes to what the repo
        recorded, this checkout hashes to something else. If the hook asks
        about the wrong one it gets a mismatch and the notice fires."""
        d = os.path.join(self.project, ".llm_chat")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "installed.json"), "w") as f:
            json.dump({"fingerprint": "vendored-hash",
                       "checkout": "/vendored/tree"}, f)

        class ByTree:
            def __call__(self, argv, **kwargs):
                if "status" in argv:
                    text = ""
                elif "--of" in argv and argv[argv.index("--of") + 1] == "/vendored/tree":
                    text = "vendored-hash"
                else:
                    text = "this-checkout-hash"

                class Result:
                    stdout = text
                    stderr = ""
                    returncode = 0
                return Result()

        fake = FakeSubprocess()
        fake.run = ByTree()
        self.mod.subprocess = fake
        self.assertEqual(self.mod.upgrade_notice("s1"), "",
                         "a repo matching its own source is not stale")

    def test_a_vendored_repo_that_HAS_drifted_still_gets_the_notice(self):
        """Paired with the test above: a check that stopped firing entirely
        would pass it and be worthless."""
        d = os.path.join(self.project, ".llm_chat")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "installed.json"), "w") as f:
            json.dump({"fingerprint": "old-vendored",
                       "checkout": "/vendored/tree"}, f)

        class Moved:
            def __call__(self, argv, **kwargs):
                text = "" if "status" in argv else "new-vendored"

                class Result:
                    stdout = text
                    stderr = ""
                    returncode = 0
                return Result()

        fake = FakeSubprocess()
        fake.run = Moved()
        self.mod.subprocess = fake
        self.assertIn("OLDER wiring", self.mod.upgrade_notice("s2"))

    def test_git_being_unavailable_does_not_break_the_notice(self):
        """The notice is the important part; knowing the source's state is a
        bonus. An exception here would swallow a real drift warning."""
        d = os.path.join(self.project, ".llm_chat")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "installed.json"), "w") as f:
            json.dump({"fingerprint": "old"}, f)

        class Exploding:
            def __call__(self, argv, **kwargs):
                if "status" in argv:
                    raise OSError("no git")

                class Result:
                    stdout, stderr, returncode = "new", "", 0
                return Result()

        fake = FakeSubprocess()
        fake.run = Exploding()
        self.mod.subprocess = fake
        notice = self.mod.upgrade_notice("session-1")
        self.assertIn("hook scripts changed", notice)
        self.assertNotIn("UNCOMMITTED", notice)

    def test_the_drift_itself_is_still_reported_either_way(self):
        for dirty in (True, False):
            self.assertIn("hook scripts changed", self.arrange(dirty))
            os.remove(os.path.join(self.project, ".llm_chat",
                                   "wiring.session-1"))


class WakeTest(HookTestCase):
    script = "llm-chat-wake"

    def test_it_records_which_event_invoked_it(self):
        """Registered on both Stop and SessionStart, it wrote the same mark
        either way — so 'did SessionStart fire?' was unanswerable from its own
        instrumentation, which is the question that mattered after a reload."""
        stdin = sys.stdin
        sys.stdin = io.StringIO('{"hook_event_name": "SessionStart"}')
        try:
            self.mod.main()
        finally:
            sys.stdin = stdin
        self.assertIn("wake-SessionStart", self.probes())
        self.assertIn("stop", self.probes())

    def test_the_mark_carries_pid_and_time_so_a_lifecycle_is_readable(self):
        stdin = sys.stdin
        sys.stdin = io.StringIO('{"hook_event_name": "Stop"}')
        try:
            self.mod.main()
        finally:
            sys.stdin = stdin
        path = os.path.join(self.project, ".llm_chat", "probe", "wake-Stop")
        with open(path) as f:
            stamp = f.read().split()
        self.assertEqual(len(stamp), 2)
        self.assertEqual(int(stamp[1]), os.getpid())

    def test_in_no_rooms_it_exits_before_touching_the_network(self):
        stdin = sys.stdin
        sys.stdin = io.StringIO("{}")
        try:
            self.assertEqual(self.mod.main(), 0)
        finally:
            sys.stdin = stdin

    def test_the_newest_waker_wins_the_pidfile(self):
        self.assertTrue(self.mod.claim_pidfile())
        with open(self.mod.PID_PATH) as f:
            self.assertEqual(int(f.read()), os.getpid())

    def test_a_waker_that_lost_the_pidfile_stands_down(self):
        """Several arming at the same instant all read the file before any
        writes, so each believes it won — one message, N wake-ups."""
        os.makedirs(os.path.dirname(self.mod.PID_PATH), exist_ok=True)
        with open(self.mod.PID_PATH, "w") as f:
            f.write("999999")
        self.assertTrue(self.mod.superseded())

    def test_holding_the_pidfile_is_not_superseded(self):
        self.mod.claim_pidfile()
        self.assertFalse(self.mod.superseded())

    def test_an_unreadable_pidfile_is_not_treated_as_supersession(self):
        os.makedirs(os.path.dirname(self.mod.PID_PATH), exist_ok=True)
        with open(self.mod.PID_PATH, "w") as f:
            f.write("not a pid")
        self.assertFalse(self.mod.superseded())

    def test_orphan_detection_uses_the_parent_that_armed_us(self):
        """Replaces an arbitrary listen budget with the condition it was
        approximating: has the session gone away."""
        self.assertFalse(self.mod.orphaned())
        self.mod.PARENT = os.getppid() + 12345
        self.assertTrue(self.mod.orphaned())

    def test_it_declines_to_guess_when_already_reparented(self):
        self.mod.PARENT = 1
        self.assertFalse(self.mod.orphaned(),
                         "cannot tell orphaned from normal, so do not claim to")

    def test_a_closed_room_is_not_worth_listening_to(self):
        self.mod.subprocess = FakeSubprocess(
            '[{"name": "room", "closed": true}]')
        self.assertFalse(self.mod.still_worth_listening(
            {"room": {"identity": "me", "server": "http://127.0.0.1:1"}}))

    def test_an_open_room_is(self):
        self.mod.subprocess = FakeSubprocess(
            '[{"name": "room", "closed": false}]')
        self.assertTrue(self.mod.still_worth_listening(
            {"room": {"identity": "me", "server": "http://127.0.0.1:1"}}))

    def test_an_unreachable_server_keeps_us_listening_rather_than_deaf(self):
        def explode(*a, **kw):
            raise OSError("down")
        self.mod.subprocess = type('M', (), {'run': staticmethod(explode)})
        self.assertTrue(self.mod.still_worth_listening(
            {"room": {"identity": "me", "server": "http://127.0.0.1:1"}}))

    def test_polling_returns_none_when_nothing_is_waiting(self):
        self.mod.subprocess = FakeSubprocess("nothing new in room")
        self.assertIsNone(self.mod.poll("room", {"identity": "me",
                                                 "server": "http://127.0.0.1:1"}))

    def test_polling_returns_the_waiting_text(self):
        self.mod.subprocess = FakeSubprocess("[other] wake up")
        self.assertEqual(self.mod.poll("room", {"identity": "me",
                                                "server": "http://127.0.0.1:1"}),
                         "[other] wake up")

    def test_an_incomplete_room_record_is_skipped(self):
        self.assertIsNone(self.mod.poll("room", {"identity": None,
                                                 "server": "http://127.0.0.1:1"}))

    def test_WAKING_DOES_NOT_FORK_A_REAL_PROCESS(self):
        """`wake` prints and exits. Nothing else.

        The missed-wake watcher used to be spawned from inside it, so this
        very test — which calls `wake` directly to assert the exit-2 contract
        — forked a detached process that outlived the suite. During a mutation
        sweep, fourteen were alive at once, each sleeping out its grace window
        inside a temp copy of the repo.

        Asserted rather than remembered, because "no test forks a real
        process" is exactly the kind of property that comes back."""
        seen = []
        real = self.mod.subprocess.Popen
        self.mod.subprocess.Popen = lambda *a, **kw: seen.append(a)
        try:
            with redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    self.mod.wake(["#room (you are 'me')\n  [other] hello"])
        finally:
            self.mod.subprocess.Popen = real
        self.assertEqual(seen, [], "wake() spawned a process")

    def test_waking_exits_two_with_the_message_on_stderr(self):
        """exit 2 + stderr is what asyncRewake converts into a wake-up; the
        stderr text IS the message the model receives."""
        err = io.StringIO()
        with redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                self.mod.wake(["#room (you are 'me')\n  [other] hello"])
        self.assertEqual(caught.exception.code, 2)
        text = err.getvalue()
        self.assertIn("while you were idle", text)
        self.assertIn("hello", text)
        self.assertIn("ANOTHER AGENT", text)


if __name__ == "__main__":
    unittest.main()
