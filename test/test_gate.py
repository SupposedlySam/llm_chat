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

        AND IT WENT SHORT AGAIN when `wake.alive` was added, so the exclusion
        is a PREFIX now and this list is every stamp the waker writes. The
        point of asserting all five is that the next one added should make
        somebody come here — not discover it through a refused release.
        """
        for stamp in ("wake.pid", "wake.exit", "wake.alive", "wake.rewake",
                      "wake.landed"):
            with self.subTest(file=stamp):
                before = gate.fingerprint_repo()
                self.write(".llm_chat", stamp, text="37300")
                self.assertFalse(
                    gate.report_repo_damage(before, gate.fingerprint_repo()))

    def test_A_SWEEP_IN_PROGRESS_IS_NOT_A_STRANDING(self):
        """The interaction that emptied this repo's mutation gate.

        The stranded check runs before anything else and returns 1 when it
        finds mutation text in the tree. A sweep's whole method is to put
        mutation text in the tree and then run the suite — so the check fired
        every time, the suite never executed, and the sweep read a non-zero
        exit as "the behaviour is defended". 130 of 133 came back `caught`
        having run no tests at all.

        Asked of the sweep's own lock rather than an environment variable,
        because a variable can be inherited by accident or set to silence the
        check."""
        real = gate.sweep_in_progress
        gate.sweep_in_progress = lambda: True
        try:
            self.assertEqual(gate.stranded_mutations(), [])
        finally:
            gate.sweep_in_progress = real

    def test_with_NO_sweep_running_the_check_actually_SCANS(self):
        """Paired, and the half that matters: this guard exists because four
        mutations were left applied by a sweep killed with -9, and a
        neighbouring agent ran the broken tree by absolute path until its
        waker died. Silencing it during a sweep must not silence it after
        one, so the no-sweep path has to reach the files."""
        import mutate
        # A mutation that WOULD look stranded: its `find` is absent from the
        # file and its `replace` is present, which is exactly the shape of a
        # sweep killed before it restored.
        #
        # Written into gate.ROOT, which setUp has pointed at a temp directory
        # — the first version named a real repo file and found nothing,
        # because stranded_mutations resolves paths against that ROOT.
        with open(os.path.join(gate.ROOT, "planted.txt"), "w") as f:
            f.write("A-STRANDED-MARKER\n")
        planted = ("planted", "planted.txt", "ZZ-not-in-this-file-ZZ",
                   "A-STRANDED-MARKER", "a fixture")
        real_muts, real_sweep = mutate.MUTATIONS, gate.sweep_in_progress
        mutate.MUTATIONS = [planted]
        try:
            gate.sweep_in_progress = lambda: False
            self.assertEqual(gate.stranded_mutations(),
                             [("planted", "planted.txt")],
                             "the no-sweep path did not scan")
            gate.sweep_in_progress = lambda: True
            self.assertEqual(gate.stranded_mutations(), [],
                             "a sweep in progress must silence it")
        finally:
            mutate.MUTATIONS, gate.sweep_in_progress = real_muts, real_sweep

    def test_sweep_in_progress_READS_THE_LOCK_not_an_env_var(self):
        """Against a temp lock, so the answer does not depend on whether this
        suite happens to be running inside a sweep — which it does, whenever
        the sweep runs it. The first version asserted the ambient state and so
        failed for real during the very sweep it was written for."""
        import fcntl
        import mutate
        with tempfile.TemporaryDirectory() as tmp:
            spare = os.path.join(tmp, ".mutate.lock")
            real = mutate.LOCK
            mutate.LOCK = spare
            try:
                self.assertFalse(gate.sweep_in_progress())
                holder = open(spare, "a")
                fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    self.assertTrue(gate.sweep_in_progress())
                finally:
                    holder.close()
                self.assertFalse(gate.sweep_in_progress())
            finally:
                mutate.LOCK = real

    def test_the_sweep_isolates_itself_from_the_LIVE_tree(self):
        """A sweep mutates files five other agents execute by absolute path.
        It was survivable while each mutation lasted about a second because
        nothing was running; making the sweep real made each one last a full
        test run."""
        import mutate
        seen = []
        real = mutate.sweep_in_a_copy
        mutate.sweep_in_a_copy = lambda: seen.append(True) or 0
        env = os.environ.pop(mutate.IN_COPY, None)
        try:
            mutate.main()
        finally:
            mutate.sweep_in_a_copy = real
            if env is not None:
                os.environ[mutate.IN_COPY] = env
        self.assertEqual(seen, [True],
                         "the sweep ran without isolating itself first")

    def test_every_LIVE_WRITTEN_path_is_excluded(self):
        """The anti-drift rule, after getting this wrong three times.

        The exclusion list has now gone short of its own reasoning once per
        file added to a background process: first `wake.pid`/`wake.exit`, then
        `wake.alive`, then `slack-replies.json` — each discovered by a REFUSED
        RELEASE, because the failure only reproduces while something is
        actively using the repo, which is exactly when a release is cut.

        So the expected set is derived from the processes themselves rather
        than remembered. A new state file on a live writer fails this test,
        which names it, instead of failing a publish that does not.
        """
        from support import load
        waker = load("llm-chat-wake")
        bridge = load("bin/llm-chat-slack")
        cli = load("llm_chat")

        live = [waker.PID_PATH, waker.EXIT_PATH, waker.ALIVE_PATH,
                waker.REWAKE_PATH, waker.LANDED_PATH,
                bridge.CURSOR, bridge.THREADS, bridge.REPLIES, bridge.ASKED,
                os.path.join(bridge.STATE, "slack-inbound.txt"),
                cli.maintenance_path(gate.ROOT),
                cli.maintenance_lock(gate.ROOT)]
        for path in live:
            relative = os.path.join(".llm_chat", os.path.basename(path))
            with self.subTest(path=relative):
                self.assertTrue(
                    any(relative.startswith(entry) for entry in gate.UNGUARDED),
                    "%s is written by a live background process and is not "
                    "excluded — it will fail a release while an agent is "
                    "working, which is when releases happen" % relative)

    def test_the_CONFIG_the_bridge_reads_is_still_guarded(self):
        """Paired, and the line the prefix has to fall on. `slack-` covers the
        bridge's live cursors; `slack.json` is configuration a human wrote,
        and a test that rewrote it would be real damage."""
        relative = os.path.join(".llm_chat", "slack.json")
        self.assertFalse(
            any(relative.startswith(entry) for entry in gate.UNGUARDED))

    def test_something_ELSE_in_the_state_dir_is_still_damage(self):
        """Paired with the prefix, and the reason it is `wake.` rather than
        `.llm_chat`. A test escaping into session state has actually happened
        here — the bridge's question-tracking wrote into the real repo
        mid-suite — and the guard caught it. A wider exclusion would have
        traded that catch for a quieter gate."""
        before = gate.fingerprint_repo()
        self.write(".llm_chat", "identity.json", text="{}")
        self.assertTrue(
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
        relpath matches no exclusion.

        The entry is a PREFIX now rather than a filename, so this asks whether
        `wake.pid` is COVERED rather than whether it is listed — the question
        the check actually turns on. Asserting the literal string was what
        broke when the list became a rule, and a test that fails on a
        correctness-preserving change is testing the spelling."""
        self.assertTrue(
            any(os.path.join(".llm_chat", "wake.pid").startswith(entry)
                for entry in gate.UNGUARDED),
            "no UNGUARDED entry covers wake.pid")
        self.write(".llm_chat", "wake.pid", text="x")
        hashed = gate.fingerprint_repo()
        self.assertFalse(any(path.endswith("wake.pid") for path in hashed),
                         "wake.pid is in UNGUARDED and still got fingerprinted "
                         "— the exclusion is being matched against directories")

    def test_AN_ESCAPE_OUTSIDE_THE_NAMED_DIRECTORIES_IS_CAUGHT(self):
        """The gap wcs named in #learnings: "a guard that names directories
        reports all-clear about a set that stopped containing everything."

        This watched `.llm_chat/` and `.claude/` only, so a test writing into
        bin/, triggers/, lib/ or the repo root was invisible — and bin/ is
        where the mutation sweep edits files in place, which has already
        stranded four mutations here for hours. git enumerates the rest now,
        so a directory added next week is covered on the day it is created.
        """
        for tracked in (os.path.join("bin", "llm_chat"),
                        os.path.join("triggers", "piped-verdict"),
                        "README.md"):
            with self.subTest(file=tracked):
                before = gate.fingerprint_repo()
                path = os.path.join(self.tmp.name, tracked)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as f:
                    f.write("a test escaped and wrote here")
                self.assertTrue(
                    gate.report_repo_damage(before, gate.fingerprint_repo()),
                    "%s was modified and the guard did not notice" % tracked)
                os.remove(path)

    def test_the_guarded_set_is_not_a_hand_written_directory_list(self):
        """Asserted directly, because the failure mode is that it silently
        goes back to being one. The named tuple may only contain things git is
        TOLD to ignore — everything else has to come from git, or the list
        starts aging again the moment somebody adds a directory."""
        self.assertEqual(set(gate.GUARDED_IGNORED), {".llm_chat", ".claude"})
        paths = gate.guarded_paths()
        outside = [p for p in paths
                   if not any(os.path.relpath(p, gate.ROOT).startswith(d)
                              for d in gate.GUARDED_IGNORED)]
        self.assertTrue(outside,
                        "guarded_paths returned nothing beyond the named "
                        "directories — the git half is not contributing")

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


class VerdictTest(unittest.TestCase):
    """`killed_by_measurement` — the function that decides every verdict the
    sweep prints, and until now the only thing in this repo with no test.

    IT IS OUTSIDE THE MUTATION DENOMINATOR BY DESIGN. `discover_sources`
    excludes `test/` with a stated reason — "test/ measures; it is not the
    thing measured" — which is honest and stays. Excluded from mutation is not
    the same as excluded from ASSERTION, though, and that gap is what this
    closes: no mutation proves these tests would fail, but at least the
    arithmetic is pinned.

    WRITTEN FROM A LOGGED FAILURE IN A SIBLING PROJECT rather than from a
    hypothetical. gameloop reported that their equivalent had been printing
    222, 222 and 171 kills from runs that never finished — because a suite
    that dies before its summary yields no counts, and a harness that treats
    "no failures reported" as "nothing survived" turns every assertion that
    never ran into a kill. They had shipped a fix, told the room it worked,
    and six producers stayed unmeasured underneath it.

    That cannot happen here, for a structural reason worth stating: kills are
    counted as a DELTA against a control run, not as a total. A summary-less
    run yields (False, 0, 0), which is neither more failures nor more errors
    than the control — so it falls through to "crashed", not to a number.
    These pin that behaviour so the structure cannot be quietly refactored
    into the other one.
    """

    def setUp(self):
        import mutate
        self.mutate = mutate
        self.green = (True, 0, 0)

    def verdict(self, after, before=None):
        return self.mutate.killed_by_measurement(before or self.green, after)[0]

    def test_A_SUMMARY_LESS_RUN_IS_NOT_A_KILL(self):
        """gameloop's tell. The suite went red and reported no counts at all,
        which means it did not finish — and every assertion that never ran
        would otherwise be counted as having killed the mutant."""
        self.assertEqual(self.verdict((False, 0, 0)), "crashed")

    def test_a_new_FAILURE_is_a_measurement(self):
        self.assertEqual(self.verdict((False, 1, 0)), "measured")

    def test_a_new_ERROR_alone_is_not(self):
        """A crash proves the line is load-bearing, not that anything watches
        what it does."""
        self.assertEqual(self.verdict((False, 0, 1)), "crashed")

    def test_a_failure_ALONGSIDE_errors_still_counts_as_measured(self):
        """One assertion that looked and disagreed is enough. Errors beside it
        are noise from the same broken program, not evidence against it."""
        self.assertEqual(self.verdict((False, 1, 9)), "measured")

    def test_a_green_suite_is_a_SURVIVOR(self):
        self.assertEqual(self.verdict((True, 0, 0)), "survived")

    def test_a_HUNG_suite_is_its_own_verdict(self):
        """Waiting for a mutation that hangs measures nothing either."""
        self.assertEqual(self.verdict((None, 0, 0)), "hung")

    def test_counts_are_a_DELTA_not_a_total(self):
        """The structural reason gameloop's failure cannot occur here. A
        control run that is already red does not let its own failures be
        claimed as kills by every mutation after it."""
        already_red = (False, 3, 0)
        self.assertEqual(self.verdict((False, 3, 0), before=already_red),
                         "crashed")
        self.assertEqual(self.verdict((False, 4, 0), before=already_red),
                         "measured")

    def test_the_summary_parser_reads_unittests_own_arithmetic(self):
        """Both halves optional, because unittest omits whichever is zero —
        and a second implementation of the count would be a second thing to be
        wrong."""
        read = dict((kind, int(n)) for kind, n
                    in self.mutate.VERDICT.findall(
                        "FAILED (failures=2, errors=1)"))
        self.assertEqual(read, {"failures": 2, "errors": 1})
        read = dict((kind, int(n)) for kind, n
                    in self.mutate.VERDICT.findall("FAILED (errors=3)"))
        self.assertEqual(read, {"errors": 3})

    def test_an_OK_line_yields_no_counts_at_all(self):
        """Which is what makes a summary-less run indistinguishable from a
        clean one in the numbers alone — and why the exit code is carried
        separately rather than inferred from them."""
        self.assertEqual(self.mutate.VERDICT.findall("OK (skipped=2)"), [])


class AccountingClosesTest(unittest.TestCase):
    """The published sum must equal its own left side, and both must equal the
    candidate count.

    This line has been wrong twice, each time for a different reason and each
    time while looking right. First `swept` was not intersected with the
    denominator: `115 + 222 = 337` printed as `= 293`. That was fixed by
    intersecting — and it still read `125 + 225 + 0 = 303`, whose left side is
    350, because the two sets OVERLAP: 47 candidates are swept AND carry a
    NOT_SWEPT reason.

    Both versions printed a sum computed from the denominator rather than
    from the terms, so the displayed equals sign was decorative. It could
    never disagree with itself, which is the only thing that would have shown
    either bug. This reads the line the tool actually prints and does the
    addition, against the REAL candidate set rather than a fixture — a
    fixture here would only pin whatever grouping I happened to write.
    """

    def test_the_three_groups_PARTITION_the_candidate_set(self):
        import io
        import contextlib
        import re
        import mutate
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            failed = mutate.report_unaccounted()
        said = buffer.getvalue()
        found = re.search(r"candidates (\d+) .*?\((\d+) \+ (\d+) \+ (\d+) = "
                          r"(\d+)\)", said, re.S)
        self.assertIsNotNone(found, "no accounting line in:\n%s" % said)
        total, a, b, c, printed = (int(g) for g in found.groups())
        self.assertEqual(a + b + c, printed,
                         "the printed sum is not the sum of its own terms")
        self.assertEqual(printed, total,
                         "the groups do not add up to the candidate set")
        self.assertFalse(failed, "the real tree has unaccounted candidates")

    # THE CLOSURE CHECK ITSELF IS NOT TESTED HERE, deliberately, and this note
    # is the reason rather than an omission.
    #
    # As written the three terms are a partition of the candidate set by
    # construction — swept, excluded-minus-swept, and neither — so no stubbing
    # of `candidates` or `swept_functions` can make them disagree. Any test
    # that appeared to check the failure branch would be asserting a value it
    # had arranged, which is how the two bugs above survived in the first
    # place: a sum computed from the denominator can never contradict itself.
    #
    # It fires on exactly one thing: someone editing the terms so they overlap
    # again, which is what happened twice. That is measured by the mutation
    # `the accounting line's terms are DISJOINT`, which reverts the middle
    # term to the overlapping form and must turn this suite red. The mutation
    # is the test. Writing a second, stubbed one would add a green light
    # without adding a check.


class CrashCeilingTest(unittest.TestCase):
    """The ceiling must fire IN A SHARD, because a shard is the only thing
    that ever evaluates it.

    It was written `if crashed and not share:`. `sweep_in_a_copy` sets SHARD
    on all eight workers, so in production `share` is always truthy and the
    ceiling never ran. Nothing noticed for as long as it was that way: the
    line had 100% coverage — from these tests, which reached it precisely
    because they did NOT set SHARD — and `verify` printed `all owed checks
    passed ✓` over a report naming a CRASHED mutation.

    So every case here is exercised at BOTH values of `share`. A test that
    only passes the unsharded value re-creates the exact blindness, and would
    look identical in the coverage table.
    """

    def setUp(self):
        import mutate
        self.mutate = mutate
        self.crash = [("some behaviour", "1 test(s) ERRORED and none FAILED")]
        # report_unaccounted reads the real MUTATIONS list against the real
        # tree and is a whole-list property; this test is about the verdict,
        # so it is pinned rather than exercised.
        self.real_unaccounted = mutate.report_unaccounted
        mutate.report_unaccounted = lambda: False
        self.addCleanup(
            lambda: setattr(mutate, "report_unaccounted",
                            self.real_unaccounted))

    def code(self, crashed, survivors, share):
        import io
        import contextlib
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            got = self.mutate.sweep_exit_code(crashed, survivors, share)
        return got, buffer.getvalue()

    SHARES = (None, "0/8", "3/8")

    def test_a_CRASH_OVER_THE_CEILING_FAILS_IN_EVERY_SHARD(self):
        """The regression, stated as the thing that was false: a worker with
        SHARD set returned 0 while naming a mutation nothing measured."""
        self.assertEqual(self.mutate.CRASHED_CEILING, 0,
                         "this test assumes the ceiling is zero; a sharded "
                         "count cannot be compared against a higher one")
        for share in self.SHARES:
            got, said = self.code(self.crash, [], share)
            self.assertEqual(got, 1, "share=%r let a crash pass" % share)
            self.assertIn("ceiling", said)

    def test_it_does_not_ALSO_claim_everything_was_caught(self):
        """The summary that contradicted the report six lines above it, and
        the reason there is no separate guard for it.

        My first fix was a second branch here, so the sentence could not print
        beside a crash. The mutation reverting that branch SURVIVED — and the
        reason is the point: at CRASHED_CEILING = 0 the ceiling returns before
        control ever reaches the summary, so the branch was unreachable and
        this assertion passed without exercising anything. It holds now
        because of the early return above, which the ceiling test covers.

        Deleted rather than kept, because an unreachable guard reads as a
        defended one. The condition under which it would be needed is written
        beside CRASHED_CEILING, where someone would change it.
        """
        for share in self.SHARES:
            got, said = self.code(self.crash, [], share)
            self.assertEqual(got, 1)
            self.assertNotIn("Every reverted fix", said,
                             "share=%r claimed a clean sweep" % share)
            self.assertIn("CRASHED", said,
                          "share=%r did not name what crashed" % share)

    def test_a_CLEAN_share_still_passes(self):
        """The other direction, and the reason the fix is not just `return 1`
        wherever `crashed` is truthy: eight workers each have to be able to
        report success, and only shard 0 owns the accounting."""
        for share in self.SHARES:
            got, said = self.code([], [], share)
            self.assertEqual(got, 0, "share=%r failed a clean sweep" % share)
            self.assertIn("Every reverted fix", said)

    def test_a_SURVIVOR_fails_in_every_shard_too(self):
        """Unlike the ceiling this one was always right, and it is pinned here
        because it sits in the same function and would be easy to lose while
        moving the crash rule around it."""
        for share in self.SHARES:
            got, _ = self.code([], [("a behaviour", "green")], share)
            self.assertEqual(got, 1, "share=%r let a survivor pass" % share)

    def test_only_shard_ZERO_owns_the_whole_list_accounting(self):
        """Eight identical reports of the same set is eight chances to read a
        repeat as a confirmation."""
        seen = []
        self.mutate.report_unaccounted = lambda: seen.append(True) or False
        self.code([], [], "3/8")
        self.assertEqual(seen, [], "a non-zero shard re-ran the accounting")
        self.code([], [], "0/8")
        self.code([], [], None)
        self.assertEqual(len(seen), 2,
                         "shard 0 and the unsharded run must both account")


if __name__ == "__main__":
    unittest.main()
