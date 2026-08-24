"""The gate's own guards — the checks that watch the suite rather than the code.

`fingerprint_repo` exists to catch a test that escapes its temp directory and
writes into the real repo. It watches `.llm_chat/` and `.claude/`, which is
also where the LIVE hooks write while anyone is working here — so it could
report the session's own activity as suite damage, and did.
"""
import os
import re
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

    def damage_found(self, before, names=None):
        """(verdict, report) — and the report goes into a buffer, not stderr.

        `report_repo_damage` PRINTS as a side effect of returning True, which
        is right for its real caller and pure noise here. Six of these blocks
        stood above every green suite run:

            THE SUITE MODIFIED THE REPO IT TESTS:
              bin/llm_chat
              A test escaped its temp directory. Fix the test, not this check.

        naming files nothing had touched. They cost me ten minutes mid-verify,
        in a session where a test really had escaped three times, and the
        suite had exited 0 the whole time. Issue #29. Same shape as the two
        false positives removed from the ghost check in 24ab166: a report that
        reads as an alarm, is not one, and has stood long enough to be
        furniture.

        Fixed HERE and not in the function, because for the real caller the
        message is the entire point — the boolean says something happened, the
        text says what. That distinction is lamp-owner's, from #learnings.

        And since the text has to be captured anyway, it gets asserted:
        `names` is the file the report must NAME. Nothing checked that before,
        so the report could have listed the wrong path, or none at all.
        """
        import contextlib
        import io
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            verdict = gate.report_repo_damage(before, gate.fingerprint_repo())
        said = buffer.getvalue()
        if names is not None:
            self.assertIn(names, said,
                          "the report did not name what it found")
        return verdict, said

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
        found, _ = self.damage_found(before, names="identity.json")
        self.assertTrue(found)

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
                found, _ = self.damage_found(before, names=tracked)
                self.assertTrue(
                    found,
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
        found, _ = self.damage_found(before, names="identity.json")
        self.assertTrue(found)

    def test_a_real_escape_into_claude_is_still_caught(self):
        before = gate.fingerprint_repo()
        self.write(".claude", "settings.local.json")
        found, _ = self.damage_found(before, names="settings.local.json")
        self.assertTrue(found)

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


class InertExclusionTest(unittest.TestCase):
    """The check that an exclusion names a function something actually runs.

    Written for `sweep_in_a_copy`, whose NOT_SWEPT reason described the call
    site and was filed as coverage of the body. It found a second one —
    `probe`, excused as "asserted by running it", which was a HAND-RUN
    recorded as coverage — on its first execution.

    THE TRAP IT TURNS ON, and I wrote it the wrong way first: a function's
    `def` line executes at IMPORT. Spanning from `node.lineno` reports every
    function in an imported module as run, including ones nothing ever calls,
    so the check would pass on exactly the case it exists to catch. It spans
    from the first non-docstring statement instead.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.real_root = gate.ROOT
        gate.ROOT = self.tmp.name
        self.addCleanup(self.restore)
        self.path = os.path.join(self.tmp.name, "prog.py")
        with open(self.path, "w") as f:
            f.write('def ran():\n'
                    '    """doc."""\n'
                    '    return 1\n'
                    '\n'
                    'def never():\n'
                    '    """doc."""\n'
                    '    return 2\n')

    def restore(self):
        gate.ROOT = self.real_root
        self.tmp.cleanup()

    def counts_for(self, *lines):
        return {(self.path, n): 1 for n in lines}

    def test_the_span_starts_AFTER_the_def_and_the_docstring(self):
        """The whole check rests on this. `def` runs at import and the
        docstring is folded into __doc__ at compile time, so neither is
        evidence that anything CALLED the function."""
        spans = gate.body_spans("prog.py")
        self.assertEqual(spans["prog.py:ran"][0], 3)
        self.assertEqual(spans["prog.py:never"][0], 7)

    def test_a_function_NOTHING_CALLS_is_reported(self):
        import mutate
        real = mutate.NOT_SWEPT
        mutate.NOT_SWEPT = {"prog.py:never": "excused for some reason"}
        self.addCleanup(lambda: setattr(mutate, "NOT_SWEPT", real))
        found = gate.inert_exclusions(self.counts_for(3))
        self.assertEqual([k for k, _ in found], ["prog.py:never"])

    def test_a_function_the_suite_DOES_call_is_not_reported(self):
        """Paired, so the test above cannot be satisfied by reporting
        everything — which is what a span anchored on `def` would do in
        reverse, reporting nothing."""
        import mutate
        real = mutate.NOT_SWEPT
        mutate.NOT_SWEPT = {"prog.py:ran": "excused for some reason"}
        self.addCleanup(lambda: setattr(mutate, "NOT_SWEPT", real))
        self.assertEqual(gate.inert_exclusions(self.counts_for(3)), [])

    def test_IMPORTING_the_module_is_not_calling_the_function(self):
        """The failure the def-line span would have caused, asserted as its
        own case: line 5 is `def never():`, which runs on import. If that
        counted, an entirely uncalled function would read as executed."""
        import mutate
        real = mutate.NOT_SWEPT
        mutate.NOT_SWEPT = {"prog.py:never": "excused for some reason"}
        self.addCleanup(lambda: setattr(mutate, "NOT_SWEPT", real))
        found = gate.inert_exclusions(self.counts_for(1, 5))
        self.assertEqual([k for k, _ in found], ["prog.py:never"],
                         "the def line was counted as a call")

    def test_a_NON_FUNCTION_exclusion_is_skipped_not_reported(self):
        """NOT_SWEPT also carries file-level and module-level entries. A check
        that reported those would fire on every run and be switched off."""
        import mutate
        real = mutate.NOT_SWEPT
        mutate.NOT_SWEPT = {"prog.py:not_a_function_here": "whatever",
                            "no/such/file.py:thing": "whatever"}
        self.addCleanup(lambda: setattr(mutate, "NOT_SWEPT", real))
        self.assertEqual(gate.inert_exclusions(self.counts_for(3)), [])

    def test_the_report_NAMES_the_excuse_it_is_contradicting(self):
        """The reason is the whole point: a reader has to see the sentence
        that claimed coverage next to the evidence that there is none."""
        import mutate
        import contextlib
        import io
        real = mutate.NOT_SWEPT
        mutate.NOT_SWEPT = {"prog.py:never": "excused as thoroughly asserted"}
        self.addCleanup(lambda: setattr(mutate, "NOT_SWEPT", real))
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            failed = gate.report_inert_exclusions(self.counts_for(3))
        self.assertTrue(failed)
        self.assertIn("prog.py:never", buffer.getvalue())
        self.assertIn("excused as thoroughly asserted", buffer.getvalue())


class ProbeTest(unittest.TestCase):
    """`probe` — the second thing the inert-exclusion check found, one run in.

    Its NOT_SWEPT reason read "all three outcomes asserted by running it —
    caught, survived, no-anchor and ambiguous, exit codes read unpiped". The
    suite never called it. Whoever wrote that had run it by hand, which is
    true and is not a test, and the entry recorded the hand-run as coverage.

    Identical shape to `sweep_in_a_copy` an hour earlier, found by the check
    written for that one on its FIRST execution — which is the argument for
    encoding a finding rather than remembering it.

    `run_suite` is stubbed because the real one runs the whole suite twice per
    probe, and `sole_sweep` because it takes the flock this process's own
    sweep uses. Everything between them is the real function, including the
    restore.
    """

    def setUp(self):
        import mutate
        self.mutate = mutate
        self.tmp = tempfile.TemporaryDirectory()
        self.real_root = mutate.ROOT
        self.real_suite = mutate.run_suite
        self.real_lock = mutate.sole_sweep
        mutate.ROOT = self.tmp.name
        mutate.sole_sweep = lambda: None
        self.runs = []
        self.addCleanup(self.restore)
        self.path = os.path.join(self.tmp.name, "prog.py")
        with open(self.path, "w") as f:
            f.write("def f():\n    return 1\n")

    def restore(self):
        self.mutate.ROOT = self.real_root
        self.mutate.run_suite = self.real_suite
        self.mutate.sole_sweep = self.real_lock
        self.tmp.cleanup()

    def results(self, *outcomes):
        """Feed run_suite one (green, failures, errors) per call."""
        queue = list(outcomes)

        def fake():
            self.runs.append(True)
            return queue.pop(0)

        self.mutate.run_suite = fake

    def probe(self, old="    return 1", new="    return 2"):
        import contextlib
        import io
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = self.mutate.probe("prog.py", old, new)
        return code, buffer.getvalue()

    def test_a_measured_kill_is_CAUGHT(self):
        self.results((True, 0, 0), (False, 1, 0))
        code, said = self.probe()
        self.assertEqual(code, 0)
        self.assertIn("CAUGHT", said)

    def test_a_green_mutant_SURVIVED(self):
        self.results((True, 0, 0), (True, 0, 0))
        code, said = self.probe()
        self.assertEqual(code, 1)
        self.assertIn("SURVIVED", said)

    def test_a_CRASH_is_not_a_kill(self):
        """The distinction the whole tool turns on: red by raising is not red
        by measuring, and the old version printed CAUGHT for both."""
        self.results((True, 0, 0), (False, 0, 1))
        code, said = self.probe()
        self.assertEqual(code, 2)
        self.assertIn("CRASHED", said)

    def test_an_ALREADY_RED_suite_yields_no_verdict(self):
        """Without the control run, a suite that is already failing makes
        every mutation look measured — confidently wrong in the direction of
        `do not build`."""
        self.results((False, 1, 0))
        code, said = self.probe()
        self.assertEqual(code, 2)
        self.assertIn("CANNOT TELL", said)

    def test_a_MISSING_anchor_is_not_a_verdict_about_defence(self):
        self.results()
        code, said = self.probe(old="    return 99")
        self.assertEqual(code, 2)
        self.assertIn("NO ANCHOR", said)
        self.assertEqual(self.runs, [], "the suite ran without a mutation")

    def test_an_AMBIGUOUS_anchor_is_refused_rather_than_guessed(self):
        with open(self.path, "w") as f:
            f.write("def f():\n    return 1\n\ndef g():\n    return 1\n")
        self.results()
        code, said = self.probe()
        self.assertEqual(code, 2)
        self.assertIn("AMBIGUOUS", said)
        self.assertEqual(self.runs, [], "the suite ran on a guessed anchor")

    def test_THE_TREE_IS_RESTORED_including_its_mtime(self):
        """The one nothing was watching, and the most damaging to get wrong.
        This mutates a file other agents execute by absolute path, and the
        commit gate reads mtime to decide whether evidence is stale — so a
        probe that restored the bytes but not the timestamp would silently
        mark every check owed by this file as out of date."""
        stat_before = os.stat(self.path)
        with open(self.path) as f:
            source_before = f.read()
        self.results((True, 0, 0), (False, 1, 0))
        self.probe()
        with open(self.path) as f:
            self.assertEqual(f.read(), source_before, "the tree was left mutated")
        self.assertEqual(os.stat(self.path).st_mtime, stat_before.st_mtime,
                         "the mtime moved, so the commit gate now sees this "
                         "file as newer than its evidence")

    def test_the_tree_is_restored_even_when_the_SUITE_RAISES(self):
        """A probe that dies partway leaves a deliberately broken program in a
        tree five other agents run out of. That has happened here — a
        NameError from a mutation reached a neighbouring agent and retired its
        waker."""
        def explode():
            self.runs.append(True)
            if len(self.runs) == 1:
                return (True, 0, 0)
            raise KeyboardInterrupt("killed mid-probe")

        self.mutate.run_suite = explode
        with self.assertRaises(KeyboardInterrupt):
            self.probe()
        with open(self.path) as f:
            self.assertEqual(f.read(), "def f():\n    return 1\n")


class SweepOrchestratorTest(unittest.TestCase):
    """`sweep_in_a_copy` — measured INERT, then given a body that executes.

    HOW IT WAS FOUND, and the method is the useful part. lamp-owner and
    gameloop split SURVIVED into two findings in #learnings: the line runs and
    nothing asserts it (undefended), versus the line never runs at all
    (INERT). A value mutation cannot separate them — both go green. A `raise`
    on the arm can.

    I first reasoned that this repo could not have the INERT case, because the
    gate enforces 100% line coverage. That is only true of what the coverage
    floor covers, and `discover_sources` excludes test/ — "test/ measures; it
    is not the thing measured". So the gate's own files have no floor. Then
    lamp-owner's warning landed: they had shipped a rule on two probes that
    both came back killed, which proves a throw reddens a suite that runs the
    arm and says nothing about the other direction. They had demonstrated the
    control and called it the experiment.

    So both directions, measured rather than argued:

        raise in a line the suite runs      -> CAUGHT    (suite red)
        raise at the top of THIS function   -> SURVIVED  (suite green) = INERT

    The only test that touched it replaced it with a lambda, which asserts
    that main() CALLS it. Its NOT_SWEPT reason recorded that as "asserted
    directly by stubbing it" — a claim about the call site, filed as coverage
    of the function. The body had never executed under a test.

    WHAT THAT BODY DECIDES. `max(child.wait() for child in running)` is the
    line that carries eight shards' verdicts back to `verify`. Had it read
    `return 0`, every mutation sweep this project has ever run would have
    reported success, and nothing anywhere would have said otherwise. It is
    the single most load-bearing line in the gate and it was inert.

    STILL NOT SWEPT, and now for the honest reason rather than the recorded
    one: a mutation here is applied inside the COPY, whose main() has IN_COPY
    set and therefore never calls this function at all. The mutant cannot run
    itself. That is structural, so these tests are the defence, and the
    exclusion says so.
    """

    class FakeChild:
        def __init__(self, code):
            self.code = code
            self.waited = False

        def wait(self):
            self.waited = True
            return self.code

    def setUp(self):
        import mutate
        self.mutate = mutate
        self.tmp = tempfile.TemporaryDirectory()
        self.real = {name: getattr(mutate, name)
                     for name in ("subprocess", "ROOT")}
        mutate.ROOT = self.tmp.name
        self.spawned = []
        self.children = []
        self.copy_code = 0
        outer = self

        class FakeSubprocess:
            PIPE = None

            @staticmethod
            def run(argv, **kw):
                # The copy step. Make the destination so the caller's later
                # use of the path is not fiction.
                if argv[0] in ("rsync", "cp"):
                    # THE DESTINATION BY NAME, NOT BY POSITION. `argv[-1]`
                    # is the copy today for both rsync and cp, and it is an
                    # unwritten assertion that nobody ever appends a flag to
                    # a command line built 600 lines away. That exact
                    # assumption broke the script assertion below on its
                    # first run yesterday, and auditor found the same shape
                    # in their gate and in this repo's `say` check in one
                    # week: selecting by POSITION when the claim is about
                    # IDENTITY. The copy is the argument named repoN.
                    dest = [a for a in argv
                            if re.search(r"/repo\d+/?$", a)]
                    self.assertEqual(len(dest), 1,
                                     "no single copy path in %r" % (argv,))
                    os.makedirs(dest[0], exist_ok=True)
                return type("Done", (), {"returncode": outer.copy_code,
                                         "stderr": "no rsync here"})()

            @staticmethod
            def Popen(argv, cwd=None, env=None, **kw):
                outer.spawned.append((argv, cwd, env))
                child = outer.FakeChild(
                    outer.child_codes[len(outer.spawned) - 1]
                    if len(outer.spawned) <= len(outer.child_codes) else 0)
                outer.children.append(child)
                return child

        self.child_codes = []
        mutate.subprocess = FakeSubprocess

    def tearDown(self):
        for name, value in self.real.items():
            setattr(self.mutate, name, value)
        self.tmp.cleanup()

    def run_it(self, child_codes):
        import contextlib
        import io
        self.child_codes = child_codes
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = self.mutate.sweep_in_a_copy()
        return code, buffer.getvalue()

    def test_EVERY_SHARD_IS_WAITED_ON_BEFORE_A_VERDICT_IS_RETURNED(self):
        """The comment says this; nothing checked it. Returning early leaves
        copies mutating files in the background and reports a result that had
        not finished being measured."""
        self.run_it([0, 0, 0, 0, 0, 0, 0, 0])
        self.assertTrue(self.children, "no shards were started at all")
        self.assertTrue(all(c.waited for c in self.children),
                        "a shard was not waited on")

    def test_ONE_FAILING_SHARD_FAILS_THE_WHOLE_SWEEP(self):
        """The line that carries eight verdicts back to `verify`. If this
        returned 0 regardless, every sweep this project has run would have
        reported success and nothing would have contradicted it."""
        code, _ = self.run_it([0, 0, 1, 0, 0, 0, 0, 0])
        self.assertEqual(code, 1, "a red shard did not fail the sweep")

    def test_an_ALL_GREEN_sweep_returns_zero(self):
        """Paired, so the test above cannot be satisfied by always failing."""
        code, _ = self.run_it([0] * 8)
        self.assertEqual(code, 0)

    def test_each_shard_is_told_WHICH_shard_it_is(self):
        """The share drives `my_share`, and it is also the variable the crash
        ceiling turns on — a worker that did not know its index would take the
        whole list, and eight workers would each sweep everything."""
        self.run_it([0] * 8)
        shares = [env[self.mutate.SHARD] for _, _, env in self.spawned]
        self.assertEqual(sorted(shares),
                         sorted("%d/%d" % (i, len(shares))
                                for i in range(len(shares))))
        self.assertTrue(all(env[self.mutate.IN_COPY] == "1"
                            for _, _, env in self.spawned),
                        "a shard was not told it is running inside a copy")

    def test_each_shard_runs_the_COPY_not_the_live_tree(self):
        """The whole reason this function exists. Other agents run
        bin/llm_chat out of the live tree by absolute path, and a mutation
        applied there is a deliberately broken program in their hands — that
        is how a NameError once reached a neighbouring agent and retired its
        waker."""
        self.run_it([0] * 8)
        for argv, cwd, _ in self.spawned:
            self.assertNotEqual(cwd, self.mutate.ROOT,
                                "a shard ran in the live tree")
            # NOT argv[-1] — the child inherits this process's own sys.argv
            # tail, so under unittest the last element is a test name. The
            # script is the element that ends in mutate.py, and asserting on
            # position rather than on identity is how I wrote it wrong first.
            script = [a for a in argv if a.endswith("mutate.py")]
            self.assertEqual(len(script), 1, "no single script in %r" % (argv,))
            self.assertTrue(script[0].startswith(cwd),
                            "a shard ran a script from outside its own copy")

    def test_a_FAILED_COPY_stops_rather_than_sweeping_a_partial_tree(self):
        """Both copiers failing means there is no tree to measure. Carrying on
        would sweep whatever happened to land, and report a number about it."""
        self.copy_code = 3
        code, said = self.run_it([0] * 8)
        self.assertEqual(code, 1)
        self.assertIn("could not copy", said)
        self.assertEqual(self.spawned, [], "shards ran on a failed copy")


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
