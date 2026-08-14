"""Deferring disruptive work until nobody is using the place.

The job that motivated this rewrites an 850MB database and locks it for the
duration. It is a minute's work and an interruption to every agent holding the
file open, and there is no good moment to pick in advance — so this waits for
one.

The tests that matter here are not the happy path. They are the refusals: this
is the only thing in the repo that runs a job with nobody watching, so every
way it could run at the WRONG time, or run the wrong thing, is asserted
directly.
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import FakeServer, load  # noqa: E402

cli = load("llm_chat")


class QuietTest(unittest.TestCase):
    """How long has it been since anything happened."""

    def setUp(self):
        self.fake = FakeServer()
        self.real = cli.call
        cli.call = self.fake.call
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name
        os.environ["CLAUDE_PROJECT_DIR"] = self.project
        os.makedirs(os.path.join(self.project, ".llm_chat"), exist_ok=True)
        self.fake.channel("room")

    def tearDown(self):
        cli.call = self.real
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.tmp.cleanup()

    def quiet(self):
        return cli.quiet_for("http://127.0.0.1:1", self.project)

    def tool_ran(self, ago):
        probe = os.path.join(self.project, ".llm_chat", "probe")
        os.makedirs(probe, exist_ok=True)
        mark = os.path.join(probe, "post-tool-use")
        with open(mark, "w") as f:
            f.write("x")
        when = cli.now_ms() / 1000.0 - ago
        os.utime(mark, (when, when))

    def test_a_recent_message_means_it_is_NOT_quiet(self):
        self.fake.message("room", 1, "someone", "hi",
                          created_at=cli.now_ms() - 60_000)
        seconds, what = self.quiet()
        self.assertLess(seconds, 120)
        self.assertEqual(what, "a message")

    def test_EVERY_MESSAGE_PUSHES_THE_DEADLINE_OUT(self):
        """The debounce, stated as the behaviour asked for. It is a high-water
        timestamp rather than a countdown some process holds — the processes
        here die constantly by design, and a timer living in one of them
        either vanishes or survives as a stale claim that the coast is
        clear."""
        self.fake.message("room", 1, "someone", "old",
                          created_at=cli.now_ms() - 7200_000)
        self.assertGreater(self.quiet()[0], 7000)
        self.fake.message("room", 2, "someone", "new",
                          created_at=cli.now_ms() - 30_000)
        self.assertLess(self.quiet()[0], 120)

    def test_AN_AGENT_RUNNING_A_TOOL_COUNTS_AS_ACTIVITY(self):
        """"No messages" is not "nobody working". An agent can spend an hour
        deep in a task without saying anything, and starting a database
        rewrite underneath it is the interruption this exists to avoid."""
        self.fake.message("room", 1, "someone", "ages ago",
                          created_at=cli.now_ms() - 7200_000)
        self.tool_ran(ago=45)
        seconds, what = self.quiet()
        self.assertLess(seconds, 120)
        self.assertEqual(what, "an agent running a tool")

    def test_the_NEWEST_of_the_two_signals_wins(self):
        self.fake.message("room", 1, "someone", "recent",
                          created_at=cli.now_ms() - 30_000)
        self.tool_ran(ago=7200)
        self.assertEqual(self.quiet()[1], "a message")

    def test_A_SERVER_THAT_CANNOT_BE_REACHED_IS_NOT_SILENCE(self):
        """THE most important assertion in this file. An unreachable server
        would otherwise look like an hour of perfect quiet — absence read as a
        clean bill of health, which is the exact inversion that has cost this
        project a day at a time. None means CANNOT TELL and nothing may run on
        it."""
        def dead(*a, **kw):
            raise SystemExit("no llm_chat server")
        cli.call = dead
        self.assertIsNone(self.quiet())

    def test_an_empty_server_with_no_agent_is_CANNOT_TELL_too(self):
        """Nothing has ever happened, so there is no last-activity to measure
        from. That is not an hour of quiet either."""
        self.assertIsNone(self.quiet())

    def test_a_message_with_no_timestamp_does_not_count_as_now(self):
        self.fake.message("room", 1, "someone", "hi", created_at=0)
        self.tool_ran(ago=7200)
        self.assertGreater(self.quiet()[0], 7000)


class RegistryTest(unittest.TestCase):
    """What may be queued, and what may not."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name
        os.environ["CLAUDE_PROJECT_DIR"] = self.project
        os.makedirs(os.path.join(self.project, ".llm_chat"), exist_ok=True)
        self.fake = FakeServer()
        self.real = cli.call
        cli.call = self.fake.call

    def tearDown(self):
        cli.call = self.real
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.tmp.cleanup()

    def run_cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.do_maintenance("http://127.0.0.1:1", *argv)
        return code, out.getvalue()

    def test_THE_QUEUE_HOLDS_NAMES_NEVER_COMMANDS(self):
        """A security property, not a stylistic one. This runs unattended on a
        loopback server with no authentication, and any agent in any room can
        write to this project's state. A queue of shell strings on a timer
        would turn "persuade an agent to write a file" into arbitrary code
        execution — and persuading an agent is exactly what a message from
        another machine gets to attempt."""
        with self.assertRaises(SystemExit) as caught:
            self.run_cli("queue", "rm -rf /")
        self.assertIn("not a maintenance task", str(caught.exception))
        self.assertEqual(cli.read_maintenance(self.project), [])

    def test_the_refusal_NAMES_what_may_be_queued(self):
        """A refusal with no available alternative is just an obstacle."""
        with self.assertRaises(SystemExit) as caught:
            self.run_cli("queue", "nonsense")
        self.assertIn("vacuum", str(caught.exception))

    def test_a_known_task_is_queued_with_its_reason(self):
        self.run_cli("queue", "vacuum", "853MB of cleared log pages")
        queued = cli.read_maintenance(self.project)
        self.assertEqual(queued[0]["task"], "vacuum")
        self.assertIn("853MB", queued[0]["why"])

    def test_queueing_twice_does_not_stack_it_up(self):
        self.run_cli("queue", "vacuum")
        self.run_cli("queue", "vacuum")
        self.assertEqual(len(cli.read_maintenance(self.project)), 1)

    def test_it_can_be_cancelled(self):
        self.run_cli("queue", "vacuum")
        self.run_cli("cancel", "vacuum")
        self.assertEqual(cli.read_maintenance(self.project), [])

    def test_cancelling_something_not_queued_is_not_an_error(self):
        code, text = self.run_cli("cancel", "vacuum")
        self.assertEqual(code, 0)
        self.assertIn("was not queued", text)

    def test_list_names_the_tasks_that_EXIST(self):
        """So a reader can find out what is queueable without guessing."""
        _, text = self.run_cli("list")
        self.assertIn("vacuum", text)
        self.assertIn("things that may be queued", text)

    def test_a_corrupt_queue_file_reads_as_empty_rather_than_crashing(self):
        with open(cli.maintenance_path(self.project), "w") as f:
            f.write("{not json")
        self.assertEqual(cli.read_maintenance(self.project), [])

    def test_a_queue_of_the_wrong_SHAPE_reads_as_empty(self):
        for junk in ('"a string"', "[1, 2, 3]", '[{"no": "task key"}]'):
            with self.subTest(junk=junk):
                with open(cli.maintenance_path(self.project), "w") as f:
                    f.write(junk)
                self.assertEqual(cli.read_maintenance(self.project), [])


class ReportingTest(unittest.TestCase):
    """What a person reads. The queue is only useful if you can see why
    nothing has happened yet."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name
        os.environ["CLAUDE_PROJECT_DIR"] = self.project
        os.makedirs(os.path.join(self.project, ".llm_chat"), exist_ok=True)
        self.fake = FakeServer()
        self.real = cli.call
        cli.call = self.fake.call
        self.fake.channel("room")

    def tearDown(self):
        cli.call = self.real
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.tmp.cleanup()

    def spoke(self, ago):
        self.fake.message("room", 1, "someone", "hi",
                          created_at=cli.now_ms() - int(ago * 1000))

    def listed(self):
        out = io.StringIO()
        with redirect_stdout(out):
            cli.do_maintenance("http://127.0.0.1:1", "list")
        return out.getvalue()

    def test_it_says_HOW_MUCH_LONGER(self):
        """"Not yet" with no number is the thing people re-run every minute
        to find out."""
        self.spoke(ago=cli.QUIET_SECONDS - 600)
        text = cli.describe_quiet("http://127.0.0.1:1", self.project)
        self.assertIn("10m to go", text)
        self.assertIn("a message", text)

    def test_it_says_when_the_work_is_DUE(self):
        self.spoke(ago=cli.QUIET_SECONDS + 600)
        self.assertIn("due? yes",
                      cli.describe_quiet("http://127.0.0.1:1", self.project))

    def test_CANNOT_TELL_reads_as_cannot_tell_not_as_silence(self):
        def dead(*a, **kw):
            raise SystemExit("no llm_chat server")
        cli.call = dead
        text = cli.describe_quiet("http://127.0.0.1:1", self.project)
        self.assertIn("CANNOT TELL", text)
        self.assertIn("not silence", text)

    def test_THE_NUMBER_SAYS_WHAT_IT_COULD_NOT_SEE(self):
        """An absent PostToolUse mark means "no agent is wired into this
        project", which is not the fact "no agent ran a tool" — and a bare
        number invites you to read the second.

        wcs's rule, from #learnings while this was being written: when a
        computation's inputs can each be independently missing, the guard
        belongs on the COMPUTATION. The strict form refuses to produce a
        number; here the message signal is genuinely server-wide, so the
        number survives and carries its own gap instead."""
        self.spoke(ago=60)
        text = cli.describe_quiet("http://127.0.0.1:1", self.project)
        self.assertIn("NOT COUNTED", text)
        self.assertIn("only messages were counted", text)

    def test_a_WIRED_project_makes_no_such_disclaimer(self):
        """Paired. A caveat printed always is a caveat nobody reads, and here
        both signals really were available."""
        probe = os.path.join(self.project, ".llm_chat", "probe")
        os.makedirs(probe, exist_ok=True)
        with open(os.path.join(probe, "post-tool-use"), "w") as f:
            f.write("1 1")
        self.spoke(ago=60)
        self.assertNotIn(
            "NOT COUNTED",
            cli.describe_quiet("http://127.0.0.1:1", self.project))

    def test_the_list_shows_a_queued_task_and_why(self):
        self.spoke(ago=60)
        cli.write_maintenance([{"task": "vacuum", "why": "853MB of pages"}],
                              self.project)
        text = self.listed()
        self.assertIn("queued: 1", text)
        self.assertIn("853MB of pages", text)

    def test_the_list_shows_PREVIOUS_ATTEMPTS_and_why_they_failed(self):
        """A task that has been sitting there for a day needs to say what has
        been happening to it, or the queue looks stuck for no reason."""
        self.spoke(ago=60)
        cli.write_maintenance([{
            "task": "vacuum", "why": "pages",
            "attempts": [{"at": cli.now_ms() // 1000 - 3600, "ok": False,
                          "detail": "database is locked"}]}], self.project)
        text = self.listed()
        self.assertIn("tried 60m ago", text)
        self.assertIn("database is locked", text)

    def test_an_attempt_with_no_detail_or_time_still_prints(self):
        self.spoke(ago=60)
        cli.write_maintenance([{"task": "vacuum",
                                "attempts": [{"ok": False}]}], self.project)
        text = self.listed()
        self.assertIn("tried ? ago", text)
        self.assertIn("no detail", text)

    def test_finished_work_is_listed_separately(self):
        self.spoke(ago=60)
        cli.write_maintenance([{"task": "vacuum", "done": True}], self.project)
        text = self.listed()
        self.assertIn("queued: 0", text)
        self.assertIn("done: vacuum", text)

    def test_RUN_WITH_NOTHING_DUE_SAYS_WHY(self):
        """Silence from a command somebody typed is indistinguishable from a
        broken command."""
        self.spoke(ago=60)
        cli.write_maintenance([{"task": "vacuum"}], self.project)
        out = io.StringIO()
        with redirect_stdout(out):
            cli.do_maintenance("http://127.0.0.1:1", "run")
        text = out.getvalue()
        self.assertIn("nothing ran", text)
        self.assertIn("to go", text)

    def test_the_verb_is_reachable_through_the_real_parser(self):
        """Through main(), so the wiring is exercised rather than assumed —
        an argparse dest that does not match the dispatch call is invisible to
        every test that calls do_maintenance directly."""
        self.spoke(ago=60)
        code, text = self.through_main("maintenance", "list")
        self.assertEqual(code, 0)
        self.assertIn("things that may be queued", text)

    def test_queue_through_the_real_parser_carries_why(self):
        self.spoke(ago=60)
        self.through_main("maintenance", "queue", "vacuum", "--why", "853MB")
        self.assertEqual(cli.read_maintenance(self.project)[0]["why"], "853MB")

    def through_main(self, *argv):
        """main() parses sys.argv itself, so the only way to exercise the
        dispatch is to hand it one."""
        saved = sys.argv
        sys.argv = ["llm_chat", "--server", "http://127.0.0.1:1"] + list(argv)
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                code = cli.main()
        finally:
            sys.argv = saved
        return code, out.getvalue()


class RunTest(unittest.TestCase):
    """When it runs, and — mostly — when it refuses to."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name
        os.environ["CLAUDE_PROJECT_DIR"] = self.project
        os.makedirs(os.path.join(self.project, ".llm_chat"), exist_ok=True)
        self.fake = FakeServer()
        self.real = cli.call
        cli.call = self.fake.call
        self.fake.channel("room")
        self.ran = []
        self.real_registry = dict(cli.MAINTENANCE)
        cli.MAINTENANCE["spy"] = ("a test double",
                                  lambda server, project:
                                  (self.ran.append(1), (True, "did it"))[1])

    def tearDown(self):
        cli.MAINTENANCE.clear()
        cli.MAINTENANCE.update(self.real_registry)
        cli.call = self.real
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.tmp.cleanup()

    def queue(self, task="spy"):
        cli.write_maintenance([{"task": task, "why": "test"}], self.project)

    def spoke(self, ago):
        self.fake.message("room", 1, "someone", "hi",
                          created_at=cli.now_ms() - int(ago * 1000))

    def run_due(self, force=False):
        # NOT `run`. Naming it that overrode unittest's own TestCase.run, so
        # the runner called this instead of running the test — every case in
        # the class errored on a missing attribute before its body ever ran.
        return cli.run_maintenance("http://127.0.0.1:1", self.project, force)

    def test_IT_RUNS_ONCE_THE_SILENCE_IS_LONG_ENOUGH(self):
        self.queue()
        self.spoke(ago=cli.QUIET_SECONDS + 60)
        said = self.run_due()
        self.assertEqual(len(self.ran), 1)
        self.assertIn("did it", "\n".join(said))

    def test_IT_DOES_NOT_RUN_WHILE_PEOPLE_ARE_TALKING(self):
        self.queue()
        self.spoke(ago=60)
        self.assertEqual(self.run_due(), [])
        self.assertEqual(self.ran, [])

    def test_A_MESSAGE_JUST_UNDER_THE_LINE_STILL_DEFERS_IT(self):
        """The boundary, asserted because an off-by-one here means running a
        database rewrite a minute after somebody spoke."""
        self.queue()
        self.spoke(ago=cli.QUIET_SECONDS - 30)
        self.assertEqual(self.run_due(), [])
        self.assertEqual(self.ran, [])

    def test_CANNOT_TELL_IS_NOT_PERMISSION(self):
        """The failure mode that would do real damage: a server that cannot be
        reached reads as perfect silence and the job runs during a busy
        afternoon. It says so out loud rather than staying quiet, because this
        one is worth seeing."""
        self.queue()
        def dead(*a, **kw):
            raise SystemExit("no llm_chat server")
        cli.call = dead
        said = self.run_due()
        self.assertEqual(self.ran, [])
        self.assertIn("cannot tell", said[0].lower())

    def test_nothing_queued_does_not_even_ask_how_quiet_it_is(self):
        """The ordinary case is an empty queue, and it must cost nothing —
        this runs on every waker heartbeat."""
        def explode(*a, **kw):
            raise AssertionError("asked the server with an empty queue")
        cli.call = explode
        self.assertEqual(self.run_due(), [])

    def test_FORCE_skips_the_quiet_check_but_nothing_else(self):
        """For doing it deliberately while you watch."""
        self.queue()
        self.spoke(ago=1)
        said = self.run_due(force=True)
        self.assertEqual(len(self.ran), 1)
        self.assertIn("did it", "\n".join(said))

    def test_a_completed_task_does_not_run_again(self):
        self.queue()
        self.spoke(ago=cli.QUIET_SECONDS + 60)
        self.run_due()
        self.run_due()
        self.assertEqual(len(self.ran), 1)

    def test_A_FAILED_TASK_IS_NOT_MARKED_DONE(self):
        """It stays queued for the next quiet window. `vacuum` fails when the
        server holds the database open, which is a REASON rather than a
        defeat — waiting another hour costs nothing."""
        cli.MAINTENANCE["spy"] = ("failing double",
                                  lambda s, p: (False, "database is locked"))
        self.queue()
        self.spoke(ago=cli.QUIET_SECONDS + 60)
        said = self.run_due()
        self.assertIn("locked", "\n".join(said))
        left = cli.read_maintenance(self.project)
        self.assertFalse(left[0].get("done"))

    def test_THE_RECORD_OF_A_FAILURE_SURVIVES_THE_RETRY(self):
        """The lesson from wake.exit one file over: a single slot means the
        record of the retry overwrites the record of the failure, which is the
        only one anybody wanted."""
        cli.MAINTENANCE["spy"] = ("failing double",
                                  lambda s, p: (False, "database is locked"))
        self.queue()
        self.spoke(ago=cli.QUIET_SECONDS + 60)
        self.run_due()
        self.run_due()
        attempts = cli.read_maintenance(self.project)[0]["attempts"]
        self.assertEqual(len(attempts), 2)
        self.assertTrue(all("locked" in a["detail"] for a in attempts))

    def test_the_attempt_history_is_capped(self):
        cli.MAINTENANCE["spy"] = ("failing double", lambda s, p: (False, "no"))
        self.queue()
        self.spoke(ago=cli.QUIET_SECONDS + 60)
        for _ in range(8):
            self.run_due()
        self.assertLessEqual(
            len(cli.read_maintenance(self.project)[0]["attempts"]), 4)

    def test_A_TASK_THAT_RAISES_DOES_NOT_BREAK_THE_TURN(self):
        """This runs from a hook. An exception escaping here would take out
        the waker that was only trying to be helpful."""
        def boom(server, project):
            raise RuntimeError("everything is on fire")
        cli.MAINTENANCE["spy"] = ("exploding double", boom)
        self.queue()
        self.spoke(ago=cli.QUIET_SECONDS + 60)
        said = self.run_due()
        self.assertIn("RuntimeError", "\n".join(said))
        self.assertFalse(cli.read_maintenance(self.project)[0].get("done"))

    def test_AN_UNKNOWN_TASK_IN_THE_FILE_IS_REFUSED_AT_RUN_TIME_TOO(self):
        """Checked again here, not only at queue time. The file is on disk and
        anything that can write to this project can edit it — so the registry
        has to be the gate at the moment of execution, not only at the moment
        of asking."""
        self.queue(task="rm -rf /")
        self.spoke(ago=cli.QUIET_SECONDS + 60)
        said = self.run_due()
        self.assertIn("refused", "\n".join(said))
        self.assertEqual(self.ran, [])

    def test_THE_DECISION_ALSO_SAYS_WHEN_ITS_VIEW_WAS_PARTIAL(self):
        """The caveat had been added to the REPORT and not to the run, which
        is the site where partial sight actually costs something. wcs's
        second rule, an hour after the first: when you add a precondition to a
        derived value, grep for the arithmetic rather than the field."""
        self.queue()
        self.spoke(ago=cli.QUIET_SECONDS + 60)
        said = self.run_due()
        self.assertTrue(any("partial view" in line for line in said), said)
        self.assertEqual(len(self.ran), 1, "it should still have run")

    def test_a_WIRED_project_runs_without_the_caveat(self):
        probe = os.path.join(self.project, ".llm_chat", "probe")
        os.makedirs(probe, exist_ok=True)
        with open(os.path.join(probe, "post-tool-use"), "w") as f:
            f.write("1 1")
        when = cli.now_ms() / 1000.0 - (cli.QUIET_SECONDS + 60)
        os.utime(os.path.join(probe, "post-tool-use"), (when, when))
        self.queue()
        self.spoke(ago=cli.QUIET_SECONDS + 60)
        said = self.run_due()
        self.assertFalse(any("partial view" in line for line in said), said)

    def test_a_task_queued_AFTER_the_quiet_check_waits_for_the_next_pass(self):
        """It was not part of the decision that this moment is safe. Somebody
        queueing work is itself activity, so letting it ride along on a check
        made before it arrived would run a job at the one moment we just
        learned somebody is at the keyboard."""
        cli.write_maintenance([{"task": "spy", "why": "first"}], self.project)
        self.spoke(ago=cli.QUIET_SECONDS + 60)
        queued_then = cli.read_maintenance(self.project)

        # A second task appears between the decision and the doing.
        cli.write_maintenance(queued_then + [{"task": "spy2", "why": "late"}],
                              self.project)
        cli.MAINTENANCE["spy2"] = ("late double",
                                   lambda s, p: (True, "should not run"))
        said = cli._run_queued("http://127.0.0.1:1", self.project, queued_then)
        self.assertEqual(len(said), 1)
        self.assertNotIn("should not run", "\n".join(said))
        left = cli.read_maintenance(self.project)
        self.assertFalse([e for e in left if e["task"] == "spy2"][0]
                         .get("done"))

    def test_an_already_DONE_entry_is_stepped_over(self):
        cli.write_maintenance([{"task": "spy", "done": True},
                               {"task": "spy", "why": "the live one"}],
                              self.project)
        self.spoke(ago=cli.QUIET_SECONDS + 60)
        self.run_due()
        self.assertEqual(len(self.ran), 1)

    def test_a_run_that_DID_something_prints_it(self):
        """Through do_maintenance, which is what a person invokes — the
        reporting path is separate from the running path and had no test."""
        self.queue()
        self.spoke(ago=cli.QUIET_SECONDS + 60)
        out = io.StringIO()
        with redirect_stdout(out):
            cli.do_maintenance("http://127.0.0.1:1", "run")
        self.assertIn("did it", out.getvalue())

    def test_a_second_runner_does_not_start_the_same_job(self):
        """Two wakers reaching a quiet window together must not both begin
        rewriting the same database."""
        import fcntl
        self.queue()
        self.spoke(ago=cli.QUIET_SECONDS + 60)
        path = cli.maintenance_lock(self.project)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        held = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            self.assertEqual(self.run_due(), [])
            self.assertEqual(self.ran, [])
        finally:
            os.close(held)


class VacuumTest(unittest.TestCase):
    """The one real task in the registry."""

    def test_a_missing_database_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = cli.ROOT
            cli.ROOT = tmp
            try:
                ok, detail = cli.vacuum_store("http://127.0.0.1:1", tmp)
            finally:
                cli.ROOT = real
        self.assertFalse(ok)
        self.assertIn("no database", detail)

    def test_IT_RECLAIMS_AND_SAYS_HOW_MUCH(self):
        """Against a real SQLite file with real freed pages, because the whole
        value of this task is a number that changed on disk."""
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            data = os.path.join(tmp, ".zonai", "data")
            os.makedirs(data)
            store = os.path.join(data, "zonai.sqlite")
            conn = sqlite3.connect(store)
            conn.execute("create table junk (id integer, body text)")
            conn.executemany("insert into junk values (?, ?)",
                             [(i, "x" * 2000) for i in range(4000)])
            conn.commit()
            conn.execute("delete from junk")
            conn.commit()
            conn.close()
            before = os.path.getsize(store)
            real = cli.ROOT
            cli.ROOT = tmp
            try:
                ok, detail = cli.vacuum_store("http://127.0.0.1:1", tmp)
            finally:
                cli.ROOT = real
            self.assertTrue(ok, detail)
            self.assertLess(os.path.getsize(store), before)
            self.assertIn("reclaimed", detail)

    def test_A_LOCKED_DATABASE_IS_A_REASON_NOT_A_CRASH(self):
        """VACUUM needs an exclusive lock and the zonai server holds this file
        open. Refusing and staying queued is right: waiting another hour costs
        nothing, and wrestling a lock away from the process that owns the data
        is not a thing to do unattended."""
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            data = os.path.join(tmp, ".zonai", "data")
            os.makedirs(data)
            store = os.path.join(data, "zonai.sqlite")
            holder = sqlite3.connect(store)
            holder.execute("create table t (id integer)")
            holder.commit()
            holder.execute("begin exclusive")
            real = cli.ROOT
            cli.ROOT = tmp
            try:
                ok, detail = cli.vacuum_store("http://127.0.0.1:1", tmp)
            finally:
                cli.ROOT = real
                holder.close()
        self.assertFalse(ok)
        self.assertIn("still queued", detail)


if __name__ == "__main__":
    unittest.main()
