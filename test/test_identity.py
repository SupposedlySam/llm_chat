"""Identity is keyed by SESSION, because a project is not an actor.

Issue #5, measured on one machine in one hour: two sessions shared a checkout.
One ran `identify`, which renamed the other. A human's question was delivered
to the wrong session, which answered under the wrong name about unrelated
work; the session actually asked never woke, because the message had already
been consumed from the shared cursor.

Every test here names two sessions explicitly, because a single-session test
cannot fail for the reason this exists.
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import FakeServer, load  # noqa: E402

cli = load("llm_chat")
deliver = load("llm-chat-deliver")

A = "aaaaaaaa-1111-2222-3333-444444444444"
B = "bbbbbbbb-5555-6666-7777-888888888888"


class SessionScopeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name
        os.environ["CLAUDE_PROJECT_DIR"] = self.project
        self.saved_sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
        self.fake = FakeServer()
        self.real_call = cli.call
        cli.call = self.fake.call

    def tearDown(self):
        cli.call = self.real_call
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        if self.saved_sid is None:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        else:
            os.environ["CLAUDE_CODE_SESSION_ID"] = self.saved_sid
        self.tmp.cleanup()

    def be(self, sid):
        if sid is None:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        else:
            os.environ["CLAUDE_CODE_SESSION_ID"] = sid

    def quiet(self, fn, *a, **kw):
        out = io.StringIO()
        with redirect_stdout(out):
            result = fn(*a, **kw)
        return result, out.getvalue()

    def project_file(self, name, payload):
        path = os.path.join(self.project, ".llm_chat", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(payload, f)

    # ── the collision itself ────────────────────────────────────────────────
    def test_TWO_SESSIONS_DO_NOT_SHARE_STATE(self):
        self.be(A)
        first = cli.state_dir()
        self.be(B)
        self.assertNotEqual(first, cli.state_dir())

    def test_IDENTIFY_IN_ONE_SESSION_DOES_NOT_RENAME_THE_OTHER(self):
        """The incident. Session B ran identify and session A started posting
        under B's name, because implicit resolution read the shared file."""
        self.be(A)
        self.quiet(cli.do_identify, "http://127.0.0.1:1", "backcompat")
        self.be(B)
        self.quiet(cli.do_identify, "http://127.0.0.1:1", "roon-deeplink")
        self.be(A)
        self.assertEqual(cli.project_identity(), "backcompat")

    def test_joining_in_one_session_is_invisible_to_the_other(self):
        """Rooms are the other half of the same file. A shared joined.json is
        why one session's delivery hook consumed the other's messages."""
        self.be(A)
        cli.remember("ops", "alice", "http://127.0.0.1:1")
        self.be(B)
        self.assertEqual(cli.read_joined(), {})

    def test_a_session_keeps_its_own_name_when_the_PROJECT_is_named(self):
        self.be(A)
        self.quiet(cli.do_identify, "http://127.0.0.1:1", "mine")
        self.be(B)
        self.quiet(cli.do_identify, "http://127.0.0.1:1", "ours", shared=True)
        self.be(A)
        self.assertEqual(cli.project_identity(), "mine")

    def test_project_identify_reaches_a_session_that_has_no_name(self):
        """`--project` has to actually mean something, or it is a flag that
        writes a file nobody reads."""
        self.be(A)
        self.quiet(cli.do_identify, "http://127.0.0.1:1", "ours", shared=True)
        self.be(B)
        self.assertEqual(cli.project_identity(), "ours")

    # ── migration: nobody re-joins ──────────────────────────────────────────
    def test_AN_EXISTING_PROJECT_FILE_STILL_WORKS(self):
        """Agents already in rooms must not have to re-join. Being told to
        would be a worse failure than the one being fixed."""
        self.project_file("joined.json",
                          {"ops": {"identity": "alice", "server": "s"}})
        self.be(A)
        self.assertIn("ops", cli.read_joined())

    def test_the_first_write_gives_the_session_its_own_copy(self):
        """Read the project's, write the session's — after which they
        diverge, which is the whole point."""
        self.project_file("joined.json",
                          {"ops": {"identity": "alice", "server": "s"}})
        self.be(A)
        cli.remember("deploy", "alice", "s")
        self.assertEqual(sorted(cli.read_joined()), ["deploy", "ops"])
        self.be(B)
        self.assertEqual(sorted(cli.read_joined()), ["ops"])

    def test_AN_EMPTY_SESSION_FILE_DOES_NOT_RE_INHERIT(self):
        """The fallback is by EXISTENCE, not emptiness. A session that has
        deliberately left every room would otherwise silently rejoin them
        all on the next read."""
        self.project_file("joined.json",
                          {"ops": {"identity": "alice", "server": "s"}})
        self.be(A)
        cli.remember("ops", "alice", "s")
        cli.forget("ops")
        self.assertEqual(cli.read_joined(), {})

    # ── naming ──────────────────────────────────────────────────────────────
    def test_A_SESSION_ALWAYS_HAS_A_NAME(self):
        """If every session must invent one they collide by CONVENTION, which
        is worse than sharing a file because it looks deliberate."""
        self.be(A)
        self.assertTrue(cli.resolve_identity(None))

    def test_the_default_name_is_UNIQUE_PER_SESSION(self):
        self.be(A)
        first = cli.default_identity()
        self.be(B)
        self.assertNotEqual(first, cli.default_identity())

    def test_the_default_name_is_STABLE_within_a_session(self):
        """It survives compaction, which is exactly when an agent forgets who
        it is — the id does not change, so the name must not either."""
        self.be(A)
        self.assertEqual(cli.default_identity(), cli.default_identity())

    def test_the_default_name_is_a_VALID_identity(self):
        """It goes straight into a membership row; NAME_OK is [a-z0-9._-]."""
        self.be(A)
        self.assertTrue(cli.valid(cli.default_identity()))

    def test_an_explicit_name_always_wins(self):
        self.be(A)
        self.quiet(cli.do_identify, "http://127.0.0.1:1", "chosen")
        self.assertEqual(cli.resolve_identity(None), "chosen")

    # ── a human at a terminal ───────────────────────────────────────────────
    def test_NO_SESSION_MEANS_THE_PROJECT_IS_THE_ACTOR(self):
        """A human at a terminal IS the project, and gets the old behaviour
        exactly — including the refusal, since there is no session to name
        them after."""
        self.be(None)
        self.assertEqual(cli.state_dir(),
                         os.path.join(self.project, ".llm_chat"))
        self.assertIsNone(cli.default_identity())
        with self.assertRaises(SystemExit):
            cli.resolve_identity(None)


class DeliverScopeTest(unittest.TestCase):
    """The hook half. It is handed the session id in its PAYLOAD, which is the
    one place it is guaranteed — a hook that guessed would deliver one
    session's messages to another, which is the incident itself."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = deliver.PROJECT
        deliver.PROJECT = self.tmp.name
        deliver.JOINED = os.path.join(self.tmp.name, ".llm_chat",
                                      "joined.json")

    def tearDown(self):
        deliver.PROJECT = self.saved
        deliver.JOINED = os.path.join(self.saved, ".llm_chat", "joined.json")
        self.tmp.cleanup()

    def write(self, payload, sid=None):
        parts = [self.tmp.name, ".llm_chat"]
        if sid:
            parts += ["sessions", sid]
        path = os.path.join(*parts, "joined.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(payload, f)

    def test_it_reads_THIS_sessions_rooms(self):
        self.write({"a": {"identity": "alice"}}, sid=A)
        self.write({"b": {"identity": "bob"}}, sid=B)
        self.assertEqual(list(deliver.joined_for(A)), ["a"])
        self.assertEqual(list(deliver.joined_for(B)), ["b"])

    def test_it_falls_back_to_the_project_for_an_unmigrated_agent(self):
        self.write({"shared": {"identity": "alice"}})
        self.assertEqual(list(deliver.joined_for(A)), ["shared"])

    def test_no_rooms_at_all_is_an_empty_dict_not_a_crash(self):
        """A wiring notice still has to get out when nothing is joined."""
        self.assertEqual(deliver.joined_for(A), {})

    def test_CORRUPT_STATE_IS_NOT_AN_EMPTY_ROOM_LIST(self):
        """This test previously asserted the defect, under the name
        `test_corrupt_state_is_empty_rather_than_fatal`. Not fatal was the
        right instinct; EMPTY was the wrong value, and writing it down as the
        expected answer is what kept it alive.

        Reported by showrunner and confirmed from source. It outranks
        everything else found this week for one reason: a resolver that reads
        empty makes an agent DEAF while every sender sees delivery succeed,
        every room still lists them, and `doctor` agrees with the silence
        because it shares the resolver. No party in the system is positioned
        to notice."""
        path = os.path.join(self.tmp.name, ".llm_chat", "sessions", A,
                            "joined.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("{not json")
        with redirect_stderr(io.StringIO()):
            self.assertIsNone(deliver.joined_for(A),
                              "unreadable state reported as 'no rooms'")

    def test_a_corrupt_SESSION_file_falls_back_to_a_healthy_project_file(self):
        """The `continue` half. An agent with a project store is rescued."""
        path = os.path.join(self.tmp.name, ".llm_chat", "sessions", A,
                            "joined.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("{not json")
        with open(os.path.join(self.tmp.name, ".llm_chat",
                               "joined.json"), "w") as f:
            json.dump({"shared": {"identity": "me", "server": "s"}}, f)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(list(deliver.joined_for(A) or {}), ["shared"])

    def test_the_TERMINAL_case_is_not_empty_either(self):
        """wcs's refinement, and the half a bare `continue` misses.

        With only the fallthrough, a corrupt session file plus NO project file
        runs off the end of the loop and returns {} — deaf again by the same
        mechanism. That is the agent that has only ever joined in-session,
        which is the one least likely to have anyone checking on it."""
        path = os.path.join(self.tmp.name, ".llm_chat", "sessions", A,
                            "joined.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("{not json")
        self.assertFalse(os.path.exists(os.path.join(
            self.tmp.name, ".llm_chat", "joined.json")),
            "this test is only meaningful with no project file to fall back to")
        with redirect_stderr(io.StringIO()):
            self.assertIsNone(deliver.joined_for(A))


if __name__ == "__main__":
    unittest.main()
