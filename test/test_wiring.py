"""Where am I, what is registered, and has it ever actually run.

These defend the distinction that cost this project a day: REGISTERED and FIRED
are different facts, and only the second one means anything. A hook can be
perfectly configured on disk and completely inert, and no amount of reading
settings.json will tell you which.
"""
import io
import json
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mutate  # noqa: E402
import run  # noqa: E402
from support import load, write_settings  # noqa: E402

cli = load("llm_chat")


class ProjectDirTest(unittest.TestCase):
    """`.llm_chat/` lives at a project's ROOT. Resolving to wherever the agent
    happens to be standing splits one project into several identities."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self.cwd)
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.tmp.cleanup()

    def test_the_harness_env_var_wins_when_set(self):
        os.environ["CLAUDE_PROJECT_DIR"] = self.root
        self.assertEqual(cli.project_dir(), self.root)

    def test_a_subdirectory_resolves_to_the_repo_root(self):
        """Join at the root, `say` from src/, and you used to be told you had
        never joined — then joining again gave one project two identities."""
        os.makedirs(os.path.join(self.root, ".git"))
        deep = os.path.join(self.root, "src", "inner")
        os.makedirs(deep)
        os.chdir(deep)
        self.assertEqual(os.path.realpath(cli.project_dir()), self.root)

    def test_an_existing_llm_chat_dir_also_marks_the_root(self):
        os.makedirs(os.path.join(self.root, ".llm_chat"))
        deep = os.path.join(self.root, "a", "b")
        os.makedirs(deep)
        os.chdir(deep)
        self.assertEqual(os.path.realpath(cli.project_dir()), self.root)

    def test_with_no_marker_anywhere_it_falls_back_to_cwd(self):
        """Only when there is genuinely nothing to find — and it says so in the
        code rather than silently preferring a wrong answer."""
        deep = os.path.join(self.root, "loose")
        os.makedirs(deep)
        os.chdir(deep)
        self.assertEqual(os.path.realpath(cli.project_dir()),
                         os.path.realpath(deep))

    def test_joined_path_hangs_off_the_resolved_root(self):
        """Both scopes hang off the same resolved root — that is the claim.
        Stated for each explicitly, because the answer now depends on whether
        a session is present and inheriting that from the ambient environment
        would make this test mean different things to different runners."""
        os.environ["CLAUDE_PROJECT_DIR"] = self.root
        saved = os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        try:
            self.assertEqual(
                cli.joined_path(),
                os.path.join(self.root, ".llm_chat", "joined.json"))
            os.environ["CLAUDE_CODE_SESSION_ID"] = "sid-1"
            self.assertEqual(
                cli.joined_path(),
                os.path.join(self.root, ".llm_chat", "sessions", "sid-1",
                             "joined.json"))
        finally:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            if saved is not None:
                os.environ["CLAUDE_CODE_SESSION_ID"] = saved


class HostTest(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.get(k)
                      for k in ("CLAUDE_CODE_ENTRYPOINT", "TERM_PROGRAM")}
        for k in self.saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_the_entrypoint_identifies_the_vscode_extension(self):
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "claude-vscode"
        self.assertEqual(cli.host(), "vscode-extension")

    def test_a_cli_entrypoint_is_not_the_extension_even_inside_vscode(self):
        """TERM_PROGRAM=vscode is equally true of a plain `claude` run in the
        integrated terminal, where the remedy is a RESTART, not a reload."""
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "cli"
        os.environ["TERM_PROGRAM"] = "vscode"
        self.assertEqual(cli.host(), "cli")

    def test_every_host_has_advice(self):
        for where in ("vscode-extension", "vscode-terminal", "cli"):
            self.assertIn(where, cli.RELOAD_ADVICE)


class HookReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def probe(self, name):
        d = os.path.join(self.project, ".llm_chat", "probe")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "w") as f:
            f.write("1 2")

    def test_registered_and_fired_are_tracked_separately(self):
        """The whole point. A hook present in settings.json and never invoked
        is indistinguishable from a working one by configuration alone."""
        write_settings(self.project, PostToolUse=["/x/bin/llm-chat-deliver"])
        registered, fired, _, _ = cli.hook_report(self.project)
        self.assertIn("llm-chat-deliver", registered)
        self.assertFalse(fired["llm-chat-deliver"])
        self.probe("post-tool-use")
        _, fired, _, _ = cli.hook_report(self.project)
        self.assertTrue(fired["llm-chat-deliver"])

    def test_the_events_a_hook_is_on_are_reported(self):
        """`registered` alone would hide a waker present only on Stop — the
        half that cannot help a session which never takes a turn."""
        write_settings(self.project, Stop=["/x/bin/llm-chat-wake"])
        _, _, events, _ = cli.hook_report(self.project)
        self.assertEqual(events["llm-chat-wake"], {"Stop"})
        write_settings(self.project, Stop=["/x/bin/llm-chat-wake"],
                       SessionStart=["/x/bin/llm-chat-wake"])
        _, _, events, _ = cli.hook_report(self.project)
        self.assertEqual(events["llm-chat-wake"], {"Stop", "SessionStart"})

    def test_the_CHECKOUT_each_hook_runs_from_is_kept(self):
        """It was read and discarded — the string was in hand and the one
        fact saying WHICH BUILD delivers your messages was thrown away.
        gameloop's repo vendors llm_chat under `.lamp/`, so their doctor
        answered from a months-old copy while the hooks ran current code."""
        write_settings(self.project,
                       PostToolUse=["/a/tree/bin/llm-chat-deliver"],
                       Stop=["/other/tree/bin/llm-chat-wake"])
        _, _, _, trees = cli.hook_report(self.project)
        self.assertEqual(trees, {"/a/tree", "/other/tree"})

    def test_an_unparseable_command_contributes_no_tree(self):
        """A tree named wrongly and confidently is worse than none named.

        The name must be CONTAINED and not be the basename, or the fixture
        answers the same either way — `llm-chat-x` contains no hook name at
        all, so it proved nothing about how the path is read. Caught by the
        sweep, which is the whole reason the sweep exists."""
        write_settings(self.project,
                       PostToolUse=["/w/bin/run-llm-chat-deliver-first"])
        _, _, _, trees = cli.hook_report(self.project)
        self.assertEqual(trees, set())

    def test_a_command_naming_no_hook_at_all_contributes_no_tree(self):
        write_settings(self.project, PostToolUse=["run-wrapper llm-chat-x"])
        _, _, _, trees = cli.hook_report(self.project)
        self.assertEqual(trees, set())

    def test_a_repo_with_nothing_registered_reports_nothing(self):
        registered, _, _, _ = cli.hook_report(self.project)
        self.assertEqual(registered, set())

    def test_malformed_settings_do_not_crash_the_report(self):
        d = os.path.join(self.project, ".claude")
        os.makedirs(d)
        with open(os.path.join(d, "settings.local.json"), "w") as f:
            f.write("{not json")
        registered, _, _, _ = cli.hook_report(self.project)
        self.assertEqual(registered, set())


class FingerprintTest(unittest.TestCase):
    def test_the_fingerprint_covers_the_hook_scripts(self):
        """Asking only 'is a hook missing' is blind to a script rewritten
        behind an unchanged command line."""
        first = cli.wiring_fingerprint()
        self.assertEqual(first, cli.wiring_fingerprint(), "must be stable")
        self.assertTrue(all(c in "0123456789abcdef" for c in first))

    def test_a_missing_stamp_reads_as_none_rather_than_as_agreement(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(cli.installed_fingerprint(tmp))

    def test_a_recorded_stamp_is_read_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, ".llm_chat")
            os.makedirs(d)
            with open(os.path.join(d, "installed.json"), "w") as f:
                json.dump({"fingerprint": "abc123"}, f)
            self.assertEqual(cli.installed_fingerprint(tmp), "abc123")


@unittest.skipUnless(sys.platform == "darwin",
                     "the reload path is macOS-only — off it, do_reload "
                     "short-circuits before reaching any of these refusals")
class ReloadGuardTest(unittest.TestCase):
    """Every path here must REFUSE. None of these may reach osascript.

    SKIPPED OFF macOS, which five sibling tests in test_setup.py already were
    and these three were not. `do_reload` returns "automated reload is
    macOS-only" before evaluating any guard, so on Linux each of these
    asserted its refusal text against that sentence and failed — for a reason
    that is nothing to do with the behaviour. Invisible until the suite ran
    somewhere other than the maintainer's laptop; the first CI run found all
    three.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = self.tmp.name
        os.environ["CLAUDE_PROJECT_DIR"] = self.project
        self.saved = os.environ.get("CLAUDE_CODE_ENTRYPOINT")
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "claude-vscode"

    def tearDown(self):
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        if self.saved is None:
            os.environ.pop("CLAUDE_CODE_ENTRYPOINT", None)
        else:
            os.environ["CLAUDE_CODE_ENTRYPOINT"] = self.saved
        self.tmp.cleanup()

    def test_refuses_outside_the_vscode_extension(self):
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "cli"
        with self.assertRaises(SystemExit) as caught:
            cli.do_reload(force=True)
        self.assertIn("nothing to reload", str(caught.exception))

    def test_refuses_when_the_waker_is_not_on_sessionstart(self):
        write_settings(self.project, Stop=["/x/bin/llm-chat-wake"])
        with self.assertRaises(SystemExit) as caught:
            cli.do_reload(force=True)
        self.assertIn("SessionStart", str(caught.exception))

    def test_refuses_when_sessionstart_has_never_been_seen_firing(self):
        """Registration is not firing. An earlier guard trusted the config and
        stranded the session twice: it came back with nothing listening."""
        write_settings(self.project, SessionStart=["/x/bin/llm-chat-wake"])
        with self.assertRaises(SystemExit) as caught:
            cli.do_reload(force=True)
        self.assertIn("never been", str(caught.exception))

    def test_refuses_without_force_even_when_fully_wired(self):
        write_settings(self.project, SessionStart=["/x/bin/llm-chat-wake"])
        d = os.path.join(self.project, ".llm_chat", "probe")
        os.makedirs(d)
        with open(os.path.join(d, "wake-SessionStart"), "w") as f:
            f.write("1 2")
        with self.assertRaises(SystemExit) as caught:
            cli.do_reload(force=False)
        self.assertIn("--force", str(caught.exception))


class WiredFromTest(unittest.TestCase):
    """Drift is measured against the tree a repo was WIRED FROM.

    It was measured against this checkout, full stop, which was fine while
    every consumer pointed at a sibling clone and wrong the moment one
    vendored its own copy. Measured on a real one: lamp recorded 53473fcc, its
    vendored tree hashed 53473fcc, this checkout hashed 999f615d — permanently
    STALE for a repo that matched its own source exactly, with a remedy that
    would have repointed its hooks here and undone the vendoring.

    installed.json has recorded `checkout` since the beginning and nothing ever
    read it. Asked directly whether anything depended on that field, I checked
    and answered "written by install.sh and read by nothing" — true, and
    exactly why this was broken.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = os.path.join(self.tmp.name, "consumer")
        os.makedirs(os.path.join(self.project, ".llm_chat"))

    def tearDown(self):
        self.tmp.cleanup()

    def vendored(self, deliver=b"one", wake=b"two"):
        tree = os.path.join(self.tmp.name, "vendored")
        os.makedirs(os.path.join(tree, "bin"), exist_ok=True)
        for name, body in (("llm-chat-deliver", deliver),
                           ("llm-chat-wake", wake)):
            with open(os.path.join(tree, "bin", name), "wb") as f:
                f.write(body)
        return tree

    def stamp(self, checkout=None, fingerprint="x"):
        record = {"fingerprint": fingerprint}
        if checkout is not None:
            record["checkout"] = checkout
        with open(os.path.join(self.project, ".llm_chat",
                               "installed.json"), "w") as f:
            json.dump(record, f)

    def test_it_hashes_the_tree_it_is_given(self):
        tree = self.vendored()
        self.assertEqual(cli.wiring_fingerprint(tree),
                         cli.wiring_fingerprint(tree))
        self.assertNotEqual(cli.wiring_fingerprint(tree),
                            cli.wiring_fingerprint())

    def test_it_notices_a_change_in_THAT_tree(self):
        tree = self.vendored()
        before = cli.wiring_fingerprint(tree)
        self.vendored(wake=b"rewritten")
        self.assertNotEqual(cli.wiring_fingerprint(tree), before)

    def test_a_vendored_consumer_matching_its_own_source_is_NOT_stale(self):
        """The reported false positive, as a test."""
        tree = self.vendored()
        self.stamp(checkout=tree, fingerprint=cli.wiring_fingerprint(tree))
        self.assertEqual(cli.installed_fingerprint(self.project),
                         cli.wiring_fingerprint(cli.installed_checkout(
                             self.project)))

    def test_a_vendored_consumer_that_HAS_drifted_still_reads_as_stale(self):
        """Paired with the test above: a check that stopped firing would pass
        the first one and be useless."""
        tree = self.vendored()
        self.stamp(checkout=tree, fingerprint=cli.wiring_fingerprint(tree))
        self.vendored(wake=b"upgraded")
        self.assertNotEqual(cli.installed_fingerprint(self.project),
                            cli.wiring_fingerprint(cli.installed_checkout(
                                self.project)))

    def test_the_recorded_checkout_is_read_back(self):
        self.stamp(checkout="/somewhere/vendored")
        self.assertEqual(cli.installed_checkout(self.project),
                         "/somewhere/vendored")

    def test_a_stamp_without_the_field_is_None_not_a_guess(self):
        """Written before the field existed. The caller must fall back to this
        checkout and SAY so, not silently compare against whatever is handy."""
        self.stamp()
        self.assertIsNone(cli.installed_checkout(self.project))

    def test_no_stamp_at_all_is_None(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(cli.installed_checkout(tmp))

    def test_a_corrupt_stamp_is_None(self):
        with open(os.path.join(self.project, ".llm_chat",
                               "installed.json"), "w") as f:
            f.write("{not json")
        self.assertIsNone(cli.installed_checkout(self.project))

    def test_hashing_a_tree_that_is_gone_does_not_crash(self):
        """It must produce a value rather than raise, so the caller can report
        'wired from a tree that is gone' instead of dying inside doctor."""
        self.assertTrue(cli.wiring_fingerprint("/no/such/tree"))


class DirtyCheckoutTest(unittest.TestCase):
    """A drift notice that prescribes re-installing has to say what state the
    source is in.

    Reported by an agent that got the notice twice in minutes, checked, and
    found the source HEAD had not moved at all — the fingerprint was being
    shifted by uncommitted files, one of them the wake hook itself. They
    declined the fix, correctly: re-installing would have wired a live session
    to a half-finished hook, and the wake hook is what delivers the message
    telling you it broke.
    """

    def setUp(self):
        self.real = cli.subprocess.run

    def tearDown(self):
        cli.subprocess.run = self.real

    def answer(self, stdout, returncode=0):
        """Canned `status` output, with `rev-parse` answered SEPARATELY.

        It used to be one blanket lambda returning the same object to every
        call. That was invisible until `checkout_dirty` grew a second question
        — and then the stub handed `rev-parse --show-toplevel` a reply of
        " M bin/llm-chat-wake", which is not a path, so every one of these
        tests failed on a function that was working.

        The same shape as the Stub that answered by call order in
        test_hooks.py: a fixture keyed on WHEN it was asked rather than WHAT
        it was asked cannot survive its subject learning a new question, and
        cannot catch a bug about which question got asked. That is why the
        real-git fixture below exists as well — no stub of any keying could
        have caught wcs's report, because the defect WAS what git says.
        """
        class Done:
            pass

        def dispatch(argv, *a, **kw):
            done = Done()
            if "rev-parse" in argv:
                done.stdout, done.returncode = "/somewhere\n", 0
            else:
                done.stdout, done.returncode = stdout, returncode
            return done
        cli.subprocess.run = dispatch
        return cli.checkout_dirty("/somewhere")

    def test_uncommitted_changes_are_dirty(self):
        self.assertTrue(self.answer(" M bin/llm-chat-wake\n"))

    def test_a_clean_tree_is_clean(self):
        self.assertFalse(self.answer(""))

    def test_whitespace_only_output_is_clean(self):
        self.assertFalse(self.answer("  \n "))

    def test_not_a_checkout_is_UNKNOWN_not_clean(self):
        """Reporting 'clean' when git could not answer would put the confident
        wording back, which is the whole defect."""
        self.assertIsNone(self.answer("", returncode=128))

    def test_no_git_at_all_is_unknown(self):
        def explode(*a, **kw):
            raise OSError("no git")
        cli.subprocess.run = explode
        self.assertIsNone(cli.checkout_dirty("/somewhere"))

    def test_git_dying_on_the_STATUS_call_is_unknown(self):
        """The pair to the one above, and it exists because that one stopped
        doing what its name says.

        It blows up EVERY subprocess call, so once `checkout_dirty` grew a
        `rev-parse` in front of the status call, the explosion happened there
        instead and `checkout_dirty`'s own `except` went unexecuted — while
        the test kept passing, green and renamed by nobody. The coverage
        report is what noticed: one line, on the run after the change.

        A test that passes for a different reason than its name gives is not
        a weaker test, it is a test of something else.
        """
        def dispatch(argv, *a, **kw):
            if "rev-parse" in argv:
                class Done:
                    stdout, returncode = "/somewhere\n", 0
                return Done()
            raise OSError("git died mid-status")
        cli.subprocess.run = dispatch
        self.assertIsNone(cli.checkout_dirty("/somewhere"))

    def test_a_tree_that_is_NOT_ITS_OWN_CHECKOUT_is_never_called_dirty(self):
        """The status output is damning and it still must not be believed.

        `rev-parse` names a different tree than the one asked about, which is
        git saying "I am answering about somebody else". No amount of dirt in
        that answer is evidence about THIS directory.
        """
        class Done:
            pass

        def dispatch(argv, *a, **kw):
            done = Done()
            if "rev-parse" in argv:
                done.stdout, done.returncode = "/enclosing/repo\n", 0
            else:
                done.stdout, done.returncode = " M thirteen/files\n", 0
            return done
        cli.subprocess.run = dispatch
        self.assertIsNone(cli.checkout_dirty("/enclosing/repo/.lamp/llm_chat"))


class CapNumberTest(unittest.TestCase):
    """No document may state a cap number that disagrees with the code.

    `DEFAULT_MAX_MESSAGES` went from 200 to 600 and the whole suite stayed
    green, because NOTHING referenced it — while README and llms.txt stated
    200 in three places. A documented constant with no test binding the two is
    the stale-count family again, and this one had a security-shaped edge: the
    number in the docs is what an agent budgets its conversation against.

    This does not assert the value. Pinning 600 here would be a change
    detector that fails on every deliberate edit and teaches people to update
    it without thinking. It asserts AGREEMENT, so either may move as long as
    both do — and documents carrying no number at all pass trivially, which is
    the state this repo prefers for counts that drift.
    """

    # BOTH ALTERNATIVES ANCHOR ON "message cap". A looser second arm —
    # `cap[^.]{0,20}(\d+)` — was written first and immediately matched
    # "Capped at 2000 characters", which is the BRIEFING limit and has nothing
    # to do with this one. A scan whose false positives are other real limits
    # is worse than none: the first thing anybody does with it is widen the
    # exemption, and then it stops seeing the case it was built for.
    NEAR_CAP = re.compile(
        r"(\d+)[- ]message cap|message cap[^.\n]{0,20}?\b(\d{2,})\b",
        re.IGNORECASE)

    def documents(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(cli.__file__)))
        for name in ("README.md", "llms.txt"):
            path = os.path.join(root, name)
            try:
                with open(path) as handle:
                    yield name, handle.read().splitlines()
            except OSError:
                continue

    def test_no_doc_states_a_cap_that_disagrees_with_the_code(self):
        wrong = []
        for name, lines in self.documents():
            for number, line in enumerate(lines, 1):
                for found in self.NEAR_CAP.finditer(line):
                    stated = int(found.group(1) or found.group(2))
                    if stated != cli.DEFAULT_MAX_MESSAGES:
                        wrong.append("%s:%d  says %d, code says %d\n      %s"
                                     % (name, number, stated,
                                        cli.DEFAULT_MAX_MESSAGES,
                                        line.strip()[:90]))
        self.assertEqual(wrong, [], "cap numbers in prose that the code "
                                    "disagrees with:\n  " + "\n  ".join(wrong))

    def test_the_scan_can_actually_SEE_a_wrong_number(self):
        """The control. A scan that matches nothing passes for the same reason
        a correct document does, and this repo has shipped that shape twice —
        so the pattern is asserted against a line it must catch."""
        hits = [int(m.group(1) or m.group(2)) for m in
                self.NEAR_CAP.finditer("A room closes at its message cap "
                                       "(default 200) and refuses writes.")]
        self.assertIn(200, hits, "the pattern cannot see the exact sentence "
                                 "that was wrong in llms.txt")


class ServeCommandTest(unittest.TestCase):
    """Every start command binds loopback, and there is only one of them.

    `./zonai serve --port N` binds the IPv6 WILDCARD — every IPv6 interface —
    in front of a server with no authentication of any kind. Measured by wcs
    on their machine and reproduced on this one:

        ./zonai serve --port 7717             ->  TCP *:7717 (LISTEN)
        ./zonai serve --port 7717 --host=::1  ->  TCP [::1]:7717 (LISTEN)

    It had no symptom and could not have had one. Every client reaches this
    over `[::1]` whichever way it is bound, and IPv4 loopback being REFUSED
    (zonai#16) makes the bind look narrower than it is rather than wider.
    What kept it off the LAN was neither host having a routable IPv6 address
    that day — a property of the afternoon, not of the software.

    THE REASON THIS IS A TEST AND NOT A FIXED STRING. The command existed in
    four spellings across two scripts and two documents. A flag added to some
    of them is how somebody starts the wide one from a doc nobody updated,
    and nothing would ever fail. So the command has one definition and these
    assert that the definition carries the flag — not that any particular
    line of prose does.
    """

    def test_the_serve_command_binds_LOOPBACK(self):
        self.assertIn("--host=::1", cli.serve_command(7717))

    def test_the_port_is_the_one_asked_for(self):
        """A hardcoded 7717 would silently serve the wrong workspace for
        anyone running a second server, which the README documents doing."""
        self.assertIn("7718", cli.serve_command(7718))
        self.assertNotIn("7717", " ".join(cli.serve_command(7718)))

    def test_what_is_PRINTED_is_what_would_be_RUN(self):
        """`start_server` prints the command so a human can copy it. That is
        only worth doing if the two are the same list rather than two
        spellings that happen to agree today — they WERE two, and the drift
        between them is invisible until somebody pastes the printed one."""
        import inspect
        source = inspect.getsource(cli.start_server)
        self.assertIn("command = serve_command(", source)
        self.assertIn('" ".join(command)', source)
        self.assertIn("subprocess.Popen(\n            command,", source)

    def test_no_start_command_ANYWHERE_omits_the_bind(self):
        """The one that would actually have caught this. Prose is where the
        wide command survived, and a reader copying a doc gets no warning."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(cli.__file__)))
        wide = []
        for name in ("README.md", "llms.txt", "bin/llm_chat",
                     "bin/llm-chat-mcp", "bin/llm-chat-deliver",
                     "bin/llm-chat-wake"):
            path = os.path.join(root, name)
            try:
                with open(path) as handle:
                    lines = handle.read().splitlines()
            except OSError:
                continue
            for number, line in enumerate(lines, 1):
                if "zonai serve --port" not in line:
                    continue
                if "--host=" in line:
                    continue
                # The two lines that MUST show the wide form: they are the
                # measurement, quoted as evidence of what it does. Recognised
                # by the arrow rather than by line number, so moving them does
                # not silently re-permit a bare command somewhere else.
                if "->" in line and "LISTEN" in line:
                    continue
                wide.append("%s:%d  %s" % (name, number, line.strip()))
        self.assertEqual(wide, [], "start commands with no bind address:\n  "
                                   + "\n  ".join(wide))


class ServerBindTest(unittest.TestCase):
    """What is the server ACTUALLY listening on — asked of a real socket.

    Every other check in doctor reads a file somebody wrote. This one reads
    the socket, and it exists because correcting documents cannot reach a
    server that is already running: the bind is fixed at startup, and nothing
    restarts one.

    wcs's reading, after two more bare start commands turned up in THEIR repo
    — one of them beside the sentence "it runs LOCALLY — loopback only":

        the count is not four across two scripts and two documents, it is
        four plus however many exist in every repo that ever wrote down how
        to run you, and none of those are reachable from your test

    A scan of this repo cannot cross that boundary. doctor ships, so it can.

    REAL SOCKETS, NOT CANNED lsof OUTPUT. The first version of the parse took
    the last field of each row as the address. The last field is `(LISTEN)`;
    the address is two back, because TCP/UDP is its own column. It reported
    CANNOT TELL about a server it was looking straight at. A stub would have
    returned whatever output I typed — including the shape I had already got
    wrong — so the fixture binds sockets and lets lsof describe them.
    """

    # NO FIXED PORT. This class had one, and the mutation sweep found it: the
    # sweep runs eight shards at once, and IPv4 and IPv6 sockets on the same
    # port are DIFFERENT sockets that can coexist — so one shard's `::` bind
    # showed up as a row under another shard's `::1` test and turned loopback
    # into wide. Reproduced by running this class four times concurrently:
    # one FAILED and three skipped every test, because the losers hit
    # EADDRINUSE and skipped out.
    #
    # The skips are the worse half. A skip is not a pass, and under the sweep
    # the usual outcome would have been these tests silently not running at
    # all — a class about not mistaking an unknown for an answer, quietly
    # producing no answer.
    #
    # Port 0 lets the kernel pick a free one per process. `listening()`
    # returns the port it actually got, so the tests that need two sockets on
    # ONE port can still ask for it explicitly.

    def listening(self, family, host, port=0):
        """Bind and listen; return the socket's real port."""
        import socket
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            sock.listen(1)
        except OSError as why:
            sock.close()
            raise unittest.SkipTest("cannot bind %s: %s" % (host, why))
        self.addCleanup(sock.close)
        return sock.getsockname()[1]

    def bound(self, family, host):
        return cli.server_bind("http://localhost:%d"
                               % self.listening(family, host))

    def setUp(self):
        import shutil
        if not shutil.which("lsof"):
            raise unittest.SkipTest("no lsof — this test IS lsof's output, so "
                                    "a stub of it would prove nothing")

    def test_the_IPv6_WILDCARD_is_wide(self):
        """The exact thing `zonai serve --port N` does without `--host`."""
        import socket
        self.assertEqual(self.bound(socket.AF_INET6, "::"), "wide")

    def test_the_IPv4_WILDCARD_is_wide(self):
        import socket
        self.assertEqual(self.bound(socket.AF_INET, "0.0.0.0"), "wide")

    def test_IPv6_loopback_is_loopback(self):
        """What `--host=::1` produces, and the only reason to prefer it."""
        import socket
        self.assertEqual(self.bound(socket.AF_INET6, "::1"), "loopback")

    def test_IPv4_loopback_is_loopback(self):
        import socket
        self.assertEqual(self.bound(socket.AF_INET, "127.0.0.1"), "loopback")

    def free_port(self):
        """A port with nothing on it: take one, then give it back."""
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        return port

    def test_NOTHING_LISTENING_is_cannot_tell_not_loopback(self):
        """An unknown must never read as the safe answer. That collapse — an
        unknown wearing a confident answer's clothes — is the whole reason
        the wide bind survived months of being written down as loopback."""
        self.assertIsNone(cli.server_bind("http://localhost:%d"
                                          % self.free_port()))

    def test_a_server_on_ANOTHER_MACHINE_is_not_measured(self):
        """lsof describes THIS host. Running it for a remote server and
        reporting the answer would be the vendored-tree mistake again: right
        command, wrong namespace, confident answer.

        The port is one that IS listening here, so the test fails if the host
        check is dropped — a free port would pass either way."""
        import socket
        port = self.listening(socket.AF_INET6, "::")
        self.assertEqual(cli.server_bind("http://localhost:%d" % port), "wide")
        self.assertIsNone(cli.server_bind("http://example.com:%d" % port))

    def test_no_lsof_is_cannot_tell(self):
        """A machine without lsof gets an unknown, not a reassuring answer.

        THE FIRST VERSION OF THIS TEST PATCHED `cli.shutil.which` AND ITS OWN
        SANITY LINE CAUGHT IT. `cli.shutil` is not a copy — it is the same
        module object every other module imported — so assigning through it
        blinds `shutil.which` for the whole process, and this suite has a leak
        detector that exists because of exactly that. Replacing the module
        REFERENCE on `cli` is local; assigning an attribute THROUGH it is not,
        and the two lines look identical.
        """
        import shutil

        class NoTools:
            which = staticmethod(lambda name: None)
        real = cli.shutil
        cli.shutil = NoTools
        self.addCleanup(lambda: setattr(cli, "shutil", real))
        import socket
        port = self.listening(socket.AF_INET6, "::")
        self.assertIsNone(cli.server_bind("http://localhost:%d" % port),
                          "a port it could have measured, and no tool to do it")
        self.assertIsNotNone(shutil.which("lsof"),
                             "the stub escaped into the real shutil module")

    def canned(self, stdout, returncode=0):
        """lsof's OUTPUT, stubbed — for the two branches that are about this
        parser's tolerance rather than about what lsof does.

        The four tests above use real sockets on purpose, because a stub of
        lsof's format is worth nothing when the format is the thing I got
        wrong. These two are the opposite question: given output the parser
        cannot read, does it say so? No real socket produces that.
        """
        class Done:
            pass

        def run(*a, **kw):
            done = Done()
            done.stdout, done.returncode = stdout, returncode
            return done

        class Fake:
            pass
        fake = Fake()
        fake.run = run
        real = cli.subprocess
        cli.subprocess = fake
        self.addCleanup(lambda: setattr(cli, "subprocess", real))
        return cli.server_bind("http://localhost:7717")

    def test_lsof_BLOWING_UP_is_cannot_tell(self):
        class Fake:
            @staticmethod
            def run(*a, **kw):
                raise OSError("lsof died")
        real = cli.subprocess
        cli.subprocess = Fake
        self.addCleanup(lambda: setattr(cli, "subprocess", real))
        self.assertIsNone(cli.server_bind("http://localhost:7717"))

    def test_rows_it_CANNOT_PARSE_do_not_become_loopback(self):
        """The dangerous default. If an unreadable row fell through to the
        end, `wide` would still be False and the answer would be the
        reassuring one — a parser failure reported as a security property.
        Nothing readable means nothing measured."""
        self.assertIsNone(self.canned("COMMAND PID USER\nnonsense row here\n"))

    def test_a_readable_row_BESIDE_an_unreadable_one_still_counts(self):
        """Paired: skipping junk must not skip the evidence next to it."""
        self.assertEqual(
            self.canned("COMMAND PID USER\nnonsense row\n"
                        "zonai 1 me 0u IPv6 0x1 0t0 TCP *:7992 (LISTEN)\n"),
            "wide")

    def test_ONE_WIDE_SOCKET_DECIDES_even_beside_a_narrow_one(self):
        """A process can hold several. A narrow row must not vouch for a wide
        one sitting next to it — the server is reachable either way, and
        reading row-by-row until something says loopback would report the
        reassuring half of a two-row answer."""
        import socket
        # The narrow one first, on a kernel-chosen port; then the wide one on
        # THAT port. IPv4 and IPv6 sockets on one port are separate sockets,
        # which is exactly the coexistence being asserted — and, before the
        # fixed port went away, exactly how eight concurrent shards poisoned
        # each other's results.
        port = self.listening(socket.AF_INET, "127.0.0.1")
        self.assertEqual(cli.server_bind("http://localhost:%d" % port),
                         "loopback", "sanity: one narrow socket, so far")
        self.listening(socket.AF_INET6, "::", port)
        self.assertEqual(cli.server_bind("http://localhost:%d" % port), "wide")


class VendoredCopyIsNotDirtyTest(unittest.TestCase):
    """A COPY inside somebody else's repo, built with real git, because the
    defect was what git actually does.

    wcs reported doctor telling them the hooks serving their session ran from
    a tree with uncommitted changes. Their vendored payload was untouched;
    the thirteen dirty files were their OWN project's, and they were dirty
    precisely because they had just vendored a fresh copy. So the warning was
    loudest at the exact moment it was most wrong.

    `git -C <dir>` walks UP for a `.git`. A vendored payload carries none, so
    every git question asked from inside it is answered — with returncode 0,
    indistinguishable from a real answer — by whatever project it was dropped
    into. Right command, wrong namespace.

    THIS USES REAL GIT ON PURPOSE. Every stub in this file would have happily
    returned whatever I told it to, including the wrong thing, which is how
    the bug survived: the fixture and the code shared an assumption about
    what git answers for a directory with no `.git`. A test cannot check an
    assumption it is built on.
    """

    def setUp(self):
        import shutil
        import subprocess
        if not shutil.which("git"):
            raise unittest.SkipTest("no git on PATH — this test IS git's "
                                    "behaviour, so a stub would prove nothing")
        self.tmp = tempfile.TemporaryDirectory()
        self.consumer = os.path.realpath(self.tmp.name)
        self.vendored = os.path.join(self.consumer, ".lamp", "llm_chat")
        os.makedirs(os.path.join(self.vendored, "bin"))

        def git(*args):
            subprocess.run(("git", "-C", self.consumer) + args,
                           capture_output=True, check=True)
        git("init", "-q", ".")
        git("config", "user.email", "fixture@example.invalid")
        git("config", "user.name", "fixture")
        with open(os.path.join(self.consumer, "tracked.txt"), "w") as f:
            f.write("committed\n")
        git("add", "-A")
        git("commit", "-qm", "init")
        # The consumer is dirty. The vendored payload is not — nobody has
        # touched it. This is wcs's machine.
        with open(os.path.join(self.consumer, "tracked.txt"), "a") as f:
            f.write("edited\n")
        with open(os.path.join(self.vendored, "bin", "llm-chat-wake"), "w") as f:
            f.write("#!/bin/sh\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_git_really_does_answer_for_the_ENCLOSING_repo(self):
        """The premise, asserted rather than assumed. If a future git stops
        walking up, this fails and the rest of the class is moot — which is
        the news, not a broken test."""
        import subprocess
        done = subprocess.run(["git", "-C", self.vendored, "status",
                               "--porcelain"], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, "a confident answer, note")
        self.assertIn("tracked.txt", done.stdout,
                      "git reported a file that is not inside the tree asked "
                      "about — the whole defect in one line")

    def test_the_copy_is_not_its_own_checkout(self):
        self.assertIs(cli.own_checkout(self.vendored), False)

    def test_the_consumer_IS_its_own_checkout(self):
        self.assertIs(cli.own_checkout(self.consumer), True)

    def test_the_copy_is_UNKNOWN_rather_than_dirty(self):
        """The line that shipped said True here, and doctor turned it into
        '! THE HOOKS SERVING THIS SESSION RUN FROM A TREE WITH UNCOMMITTED
        CHANGES' — a claim about a payload it could not see."""
        self.assertIsNone(cli.checkout_dirty(self.vendored))

    def test_the_enclosing_repo_is_still_reported_dirty(self):
        """The fix must not buy its correctness by refusing to answer at all.
        The consumer really is dirty and really is its own checkout."""
        self.assertIs(cli.checkout_dirty(self.consumer), True)

    def test_a_directory_under_NO_repository_is_unknown(self):
        """Distinct from the copy case, and both are None: one is 'git has no
        opinion', the other is 'git has an opinion about someone else'."""
        with tempfile.TemporaryDirectory() as outside:
            self.assertIsNone(cli.checkout_dirty(outside))

    def test_an_EMPTY_toplevel_is_unknown_not_a_match(self):
        """rc=0 with nothing on stdout. `realpath("")` is the CURRENT
        DIRECTORY, not an error — so without this branch the verdict would
        depend on where the process happened to be standing, and would be
        True for anyone running doctor from the root of any checkout.
        """
        class Done:
            pass
        real = cli.subprocess.run

        def blank(*a, **kw):
            done = Done()
            done.stdout, done.returncode = "\n", 0
            return done
        cli.subprocess.run = blank
        self.addCleanup(lambda: setattr(cli.subprocess, "run", real))
        self.assertIsNone(cli.own_checkout(os.getcwd()))

    def test_a_checkout_reached_through_a_SYMLINK_is_still_its_own(self):
        """`realpath` on both sides. /tmp is a symlink on macOS, so without
        it every fixture in this class — and every checkout under /tmp on a
        contributor's machine — reads as somebody else's copy, and the fix
        would report UNKNOWN for trees it can see perfectly well."""
        link = os.path.join(os.path.dirname(self.consumer),
                            os.path.basename(self.consumer) + "-link")
        os.symlink(self.consumer, link)
        self.addCleanup(os.unlink, link)
        self.assertIs(cli.own_checkout(link), True)
        self.assertIs(cli.checkout_dirty(link), True)


class LeakDetectorTest(unittest.TestCase):
    """The detector that catches a test patching a shared module — itself
    defended, which it was not.

    Found by probing it: mutating its "nothing leaked" branch to always-true
    left the suite GREEN. So the rail everybody relies on to catch
    `mod.subprocess.run = stub` could be broken and nothing would say so. It
    is wired at two call sites and it does fire; it was simply nobody's job to
    notice if it stopped.
    """

    def test_it_reports_a_shared_callable_left_patched(self):
        """report_global_leaks prints to STDERR, not stdout — wrapping only
        redirect_stdout let this deliberate simulation leak its own "THE SUITE
        LEFT SHARED CALLABLES PATCHED" line straight to the real terminal on
        every run, indistinguishable from an actual failure. Its sibling test
        below already redirects the right stream."""
        import subprocess as real
        before = run.shared_callables()
        keep = real.run
        real.run = lambda *a, **k: None
        try:
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                leaked = run.report_global_leaks(before)
        finally:
            real.run = keep
        self.assertTrue(leaked)

    def test_an_unpatched_suite_reports_nothing(self):
        """Paired: a detector that always fires is one nobody leaves on."""
        before = run.shared_callables()
        self.assertFalse(run.report_global_leaks(before))

    def test_it_names_what_leaked(self):
        import subprocess as real
        before = run.shared_callables()
        keep = real.run
        real.run = lambda *a, **k: None
        try:
            err = io.StringIO()
            with redirect_stderr(err):
                run.report_global_leaks(before)
        finally:
            real.run = keep
        self.assertIn("subprocess.run", err.getvalue())


class ExecutableBitTest(unittest.TestCase):
    """Every entrypoint in bin/ has to be runnable.

    The Slack bridge shipped without its execute bit. Nothing noticed: the test
    suite imports these files rather than running them, the coverage runner
    imports them, the mutation sweep imports them — every instrument agreed it
    was 100% covered while the only thing a person actually does with it, type
    its name, failed with permission denied. A file can be perfect and
    unrunnable, and testing it by import cannot tell the difference.

    Discovery is shared with the sweep, so a file added tomorrow is checked
    without anyone remembering to add it here."""

    def test_every_bin_script_is_executable(self):
        scripts = mutate.discover_sources()
        self.assertTrue(scripts, "discovery found no scripts to check")
        for relative in scripts:
            with self.subTest(script=relative):
                # Discovery returns paths relative to the repo root, and this
                # runs from test/. Resolving them against the cwd made all four
                # fail identically, which reads as a real defect rather than as
                # the guard pointing at nothing.
                path = os.path.join(mutate.ROOT, relative)
                self.assertTrue(os.access(path, os.X_OK),
                                "%s is not executable — `chmod +x` it"
                                % relative)


class ContinuousIntegrationTest(unittest.TestCase):
    """The workflow is the one place that runs this suite off this machine.

    WHAT THIS CHECKS AND WHAT IT CANNOT. GitHub is the only real consumer of
    that file, so nothing here validates its YAML — an invalid workflow fails
    on GitHub, loudly, which is an honest failure. What is NOT honest is a
    workflow that parses perfectly and runs nothing: it goes green, the badge
    is green, and the suite has silently stopped running off this machine.
    That is this repo's recurring defect, so that is what is asserted.

    Written because verify reported the file UNCHECKED — it matched no rule,
    so nothing looked at it at all. A CI file nothing verifies is the same
    shape as the thing it was added to fix.
    """

    def setUp(self):
        self.path = os.path.join(mutate.ROOT, ".github", "workflows",
                                 "tests.yml")

    def test_the_workflow_exists(self):
        self.assertTrue(os.path.isfile(self.path),
                        "no CI workflow — the suite runs only where somebody "
                        "types a command")

    def test_it_can_be_RE_TRIGGERED_without_a_new_commit(self):
        """A run that never started cannot be restarted.

        A commit here came back `startup_failure` at 0s — GitHub could not
        launch the job — and `gh run rerun` refuses those outright: "This
        workflow run cannot be retried". With only push and pull_request
        triggers there was no way to get a verdict for that commit short of
        pushing another one, which means fabricating a change or leaving HEAD
        unverified.

        Neither is acceptable while "CI green" is the bar for saying
        something shipped, so `workflow_dispatch` is part of the wiring
        rather than a convenience.
        """
        with open(self.path) as f:
            self.assertIn("workflow_dispatch", f.read(),
                          "a failed-to-start run could not be re-triggered "
                          "without an invented commit")

    def test_IT_ACTUALLY_RUNS_THE_SUITE(self):
        """A workflow that runs nothing is green and means nothing."""
        with open(self.path) as f:
            text = f.read()
        self.assertIn("test/run.py", text,
                      "the workflow does not invoke the suite")

    def test_it_runs_on_push_AND_on_pull_request(self):
        """Push alone leaves a contributor's branch unmeasured until it is
        merged, which is the moment the check is worth least."""
        with open(self.path) as f:
            text = f.read()
        self.assertIn("pull_request:", text)
        self.assertIn("push:", text)

    def commands(self):
        """The workflow's executable lines, with comments stripped.

        Reading the whole file failed this the first time it ran — on the
        COMMENT explaining why there are no installs. A check whose fixture
        cannot tell a command from a sentence about commands reports the
        explanation as the violation, and would go on doing so however the
        file was fixed.
        """
        with open(self.path) as f:
            return "\n".join(line for line in f
                             if not line.lstrip().startswith("#"))

    def test_it_installs_NOTHING(self):
        """Every entrypoint here is stdlib-only on purpose — an MCP client
        that spawns bin/llm-chat-mcp should not need a pip install to talk
        to it. A workflow that quietly installed dependencies would make the
        suite pass in a way no consumer can reproduce."""
        self.assertNotIn("pip install", self.commands())

    def test_it_says_the_MUTATION_SWEEP_is_not_run_there(self):
        """The sweep is what says a behaviour is DEFENDED rather than merely
        covered, and it stays on the commit gate because it takes tens of
        minutes. A green tick that is read as "the tests would notice" is
        exactly the false comfort this project keeps removing, so the file has
        to say so where somebody reading it will see it."""
        with open(self.path) as f:
            text = f.read()
        self.assertIn("does NOT run the mutation sweep", text)


# WHERE EACH SHIPPED TRIGGER IS SUPPOSED TO BE WIRED, and it is a decision
# record rather than a list somebody maintains: the test below fails when a
# file in triggers/ has no entry here, so it cannot age quietly. That is the
# only shape of list this repo allows, and it is the difference between
# default-deny and the four complete-by-accident lists already removed from it.
#
# THE THREE VALUES ARE THREE DIFFERENT REGISTRIES, not a taxonomy for its own
# sake. A check that knew about one of them would report the others as unwired.
#
#   claude-hook   .claude/settings.local.json — machine-local, because the
#                 command is an absolute path and the tracked settings.json
#                 would fire it against a directory only this machine has.
#                 NOT VISIBLE ON A COLD CLONE, which is the whole reason this
#                 table exists separately from the registry.
#   game-loop     .game_loop/triggers.json — its own events (harden, stepback,
#                 session_start). Also gitignored, also absolute paths.
#   by-hand       nothing invokes it, deliberately, with the reason.
EXPECTED = {
    "answer-when-asked": "claude-hook",
    "authority-gate": "claude-hook",
    "piped-verdict": "claude-hook",
    "prose-through-shell": "claude-hook",
    "tell-the-consumers": "claude-hook",
    "write-through-interpreter": "claude-hook",
    "learnings-broadcast": "game-loop",
    "learnings-digest": "game-loop",
    "undocumented-surface": "game-loop",
    "issue-watch": "by-hand",
}

BY_HAND_REASON = {
    "issue-watch":
        "a long-running watcher started BY HAND — `sh triggers/issue-watch` — "
        "not a hook. It prints its current open set as a positive control on "
        "the first line precisely because nothing invokes it on a schedule, "
        "so a silent one is visibly broken rather than quietly absent.",
}


class EveryTriggerIsCLASSIFIEDTest(unittest.TestCase):
    """A shipped trigger is registered somewhere, or excused here in writing.

    THE FAILURE THIS ENCODES, and it is not hypothetical. README.md says:

        | `triggers/answer-when-asked` | `Stop` | refuses to end a turn while
          a question is unanswered |

    It was registered in NONE of `.claude/settings.json`,
    `.claude/settings.local.json`, `~/.claude/settings.json`, or
    `.game_loop/triggers.json`. It had tests, 100% coverage and a mutation,
    and it had never fired once — while `owed` sat reporting a question I had
    not answered. The documentation asserted a live guard; the guard was
    furniture.

    Third instance of configured-but-inert here, and the first where a
    document claimed it worked.

    DEFAULT-DENY, over the DIRECTORY. The set of triggers is read from
    `triggers/`, never from a list — a hand-kept list is the
    complete-by-accident defect this repo has now removed four times, most
    recently from the verdict guard I was measuring when I found this one. A
    new trigger that nobody has decided about fails this test on the commit
    that adds it, which is the moment it is cheapest to decide.

    WHAT IT CANNOT DO, said plainly because a rail is silent where it is
    blind: it cannot tell a REGISTERED trigger from a WORKING one. Registration
    is a fact about a config file. Whether the thing fires, and whether it
    fires usefully, is what the tests and the mutation sweep are for. This
    check only refuses the state where nobody has looked.

    showrunner's mechanism, adopted after they described it: enforcement lives
    in the tool, and what the tool enforces is that the unenforceable part was
    written down and is still there.
    """

    def registries(self):
        """Every place a trigger can legitimately be wired, and its contents.

        READ, not assumed. Two registries exist here for different reasons —
        Claude Code's hooks live in settings, and game_loop's event triggers
        live in its own file — and a check that knew about one of them would
        report half the triggers as unwired.
        """
        found = set()
        for relative in (".claude/settings.json",
                         ".claude/settings.local.json",
                         ".game_loop/triggers.json"):
            path = os.path.join(mutate.ROOT, relative)
            try:
                with open(path) as f:
                    text = f.read()
            except OSError:
                continue          # absent is not evidence of anything
            found.add((relative, text))
        return found

    def shipped(self):
        directory = os.path.join(mutate.ROOT, "triggers")
        return sorted(name for name in os.listdir(directory)
                      if os.path.isfile(os.path.join(directory, name))
                      and not name.startswith("."))

    @staticmethod
    def unclassified(shipped, expected):
        """Triggers nobody has decided about. A FUNCTION, so it can be asked
        about a trigger this repo does not have — asked only about the real
        directory, where everything IS classified, it would pass whether or
        not it worked."""
        return [name for name in shipped if name not in expected]

    def test_NOTHING_SHIPS_UNCLASSIFIED(self):
        """The half that works EVERYWHERE, including a cold clone.

        Both registries are gitignored — they hold absolute paths — so on CI
        neither is visible and registration cannot be checked at all. The
        first version of this test did not separate those two questions and
        failed on CI listing six triggers as unwired, which was true of the
        checkout and false of the world. A guard that cannot run where it
        ships is the defect this whole test was written to catch, and it had
        it on the first commit.

        So: WHERE a trigger belongs is tracked and always checkable. WHETHER
        it is really there can only be asked where the registry exists.
        """
        self.assertEqual(
            self.unclassified(self.shipped(), EXPECTED), [],
            "a trigger with no entry in EXPECTED — say which registry it "
            "belongs in, or mark it by-hand with a reason")

    def test_IT_ACTUALLY_REPORTS_ONE(self):
        """The direction the live check cannot exercise while the repo is
        clean, and the direction the whole thing exists for."""
        self.assertEqual(
            self.unclassified(["known", "nobody-decided"],
                              {"known": "claude-hook"}),
            ["nobody-decided"])

    # SPELLED OUT COUNTS TOO, and `one` deliberately absent. In prose "one"
    # is the indefinite article wearing a numeral — "the one test file every
    # change touches" means a single file, not a count that will rot.
    # showrunner measured two false positives from including it.
    WORD_COUNT = ("two|three|four|five|six|seven|eight|nine|ten|eleven|"
                  "twelve|dozen|hundreds?|thousands?")
    GROWS = "tests?|mutations?|entrypoints?|triggers?|assertions?"

    @classmethod
    def counted_in_prose(cls, text):
        """Hard-coded counts of things this repo GROWS.

        DIGITS AND WORDS. The first version was digits-only: it caught "242
        tests" and missed "the four entrypoints" sitting two lines away, which
        I then fixed BY HAND in the same edit. A check that misses the case
        beside the one it caught is the enumeration defect living inside the
        guard against stale claims — showrunner hit the identical thing from
        their side the same morning.

        A function, so it can be asked about text this repo does not contain,
        and so the word case could be proved to FAIL against the old pattern
        before it passed against this one. showrunner's widening looked
        correct because they had written `or "four" in phrase` into the
        assertion — the check agreeing for a reason unrelated to its claim,
        inside the test for a check about exactly that.
        """
        pattern = r"\b(?:\d[\d,]*|%s)\s+(?:%s)\b" % (cls.WORD_COUNT, cls.GROWS)
        return re.findall(pattern, text, re.I)

    def test_THE_DOCS_DO_NOT_COUNT_A_GROWING_SET(self):
        """A count in prose beside a set that grows is the reliable offender.

        README said "242 tests, 100% line coverage on the four entrypoints".
        Wrong twice — the suite passed 1,800 some time ago, and the floor
        covers fourteen files, not four. Nobody re-derived either number
        because nothing had to.

        lamp-owner's remedy from #learnings, and the reason it is deletion
        rather than correction: a corrected number falls behind again on the
        next commit, and there is no version of "242" that survives a test
        being added. What survives is the property the gate enforces —
        `--min 100` — which a reader can run.

        FOUND BY RUNNING MY OWN SUGGESTION AGAINST MY OWN DOCS. I told
        showrunner every rate should name a committed tool that reproduces
        it; they built that, it found three unsourced rates in their front
        door, and the symmetric sweep here found this. I did NOT build their
        net: measured over both front doors it flagged six blocks, of which
        four were the detector's own noise — a threshold read as a rate,
        prose read as a claim. A check that is two-thirds noise is the
        mostly-noise failure this repo has removed twice. This is the narrow
        rule the one real finding actually supports.
        """
        for name in ("README.md", "llms.txt"):
            with open(os.path.join(mutate.ROOT, name)) as f:
                text = f.read()
            # The paragraph explaining the deletion quotes the old sentence,
            # so the quote is excluded by the quoting itself: only counts
            # outside a blockquote are claims the doc is still making.
            live = "\n".join(line for line in text.splitlines()
                             if not line.lstrip().startswith(">"))
            with self.subTest(doc=name):
                self.assertEqual(
                    self.counted_in_prose(live), [],
                    "%s states a count of something that grows — say the "
                    "property instead, and let the gate carry the number"
                    % name)

    def test_IT_CATCHES_A_COUNT_IT_SHOULD(self):
        """The direction the live check cannot exercise now that the docs are
        clean, which is exactly when a broken guard looks healthiest."""
        self.assertEqual(
            self.counted_in_prose("the suite runs 242 tests and is fine"),
            ["242 tests"])
        self.assertEqual(
            self.counted_in_prose("100% line coverage, ratcheted at --min 100"),
            [], "a property with a number in it is not a count of a growing set")

    def test_a_count_SPELLED_OUT_is_caught_too(self):
        """The case the first version missed while catching its neighbour.

        `242 tests` was found by the check; `the four entrypoints`, two lines
        away in the same file, was found by me reading it. Both were wrong,
        both rot the same way, and only one of them had a guard.
        """
        self.assertEqual(
            self.counted_in_prose("coverage on the four entrypoints"),
            ["four entrypoints"])
        self.assertEqual(self.counted_in_prose("THREE TRIGGERS are wired"),
                         ["THREE TRIGGERS"])

    def test_THE_DELETED_SENTENCE_IS_ITS_OWN_FIXTURE(self):
        """README quotes the claim it removed, and that quote carries BOTH
        forms — `242 tests` and `four entrypoints`. So the widening is proved
        against real text in this repo rather than a synthetic string, and the
        blockquote exclusion is proved to be what keeps the live scan quiet
        rather than the pattern simply missing them.

        Two things fail together if either half breaks: a narrowed pattern
        stops finding the word form here, and a dropped blockquote rule makes
        the live check fire on a sentence the doc is explicitly not claiming.
        """
        with open(os.path.join(mutate.ROOT, "README.md")) as f:
            text = f.read()
        quoted = "\n".join(line for line in text.splitlines()
                           if line.lstrip().startswith(">"))
        found = [" ".join(hit.split()).lower()
                 for hit in self.counted_in_prose(quoted)]
        self.assertIn("242 tests", found)
        self.assertIn("four entrypoints", found,
                      "the spelled-out form is the one the first version "
                      "missed — if it is absent here the widening is undone")

    def test_ONE_is_not_a_count(self):
        """In prose it is the indefinite article wearing a numeral, and it
        does not rot: "the one test file every change touches" means a single
        file, and stays true however many tests are added. showrunner
        measured two false positives from including it."""
        self.assertEqual(
            self.counted_in_prose("the one test file every change touches"),
            [])

    def test_a_count_ACROSS_A_LINE_BREAK_is_still_a_count(self):
        """`205 tests` was invisible to the eye AND to a line-by-line scan
        because prose wrapped between the number and the noun. The whole file
        is matched, so a wrap changes nothing."""
        self.assertEqual(
            self.counted_in_prose("leaves all 205\ntests green"),
            ["205\ntests"])

    def test_BOTH_DOCS_SAY_WHICH_BUILD_TO_RUN(self):
        """`doctor` already warns when it is the wrong build — and an OLD copy
        cannot warn you about itself, which is exactly the case that bites.

        showrunner ran a vendored llm_chat for a week and reported their chat
        healthy. The two builds gave opposite readings of the same fact: the
        old one said a wake had landed and replies arrive on their own; the
        current one adds that a LATER wake was requested and never landed, so
        the idle path was dead. A stale build hands you the reassuring half of
        a two-part finding.

        So the rule has to be readable with NO build running, which means the
        docs — and a doc claim rots exactly as quietly as a code one. This
        asserts the sentence is there, the way `test_it_says_the_MUTATION_
        SWEEP_is_not_run_there` does for the CI file. It cannot check that the
        advice is good; only that the decision to give it is still written
        down.
        """
        for name in ("README.md", "llms.txt"):
            with self.subTest(doc=name):
                with open(os.path.join(mutate.ROOT, name)) as f:
                    text = f.read()
                self.assertIn("registered hooks", text,
                              "%s must say the hooks' directory is the build "
                              "that matters" % name)
                self.assertIn("llm-chat-deliver' .claude/settings.local.json",
                              text,
                              "%s must show HOW to read it, not just say to"
                              % name)

    def test_BUILD_LEAVINGS_ARE_NOT_TRIGGERS(self):
        """`triggers/__pycache__` is real and this check survives it by
        ACCIDENT — the listing filters `os.path.isfile`, and a directory
        happens not to be a file. Nothing decided about it.

        showrunner hit the sharp version: their parse check compiled hooks
        with `py_compile`, which left a `__pycache__` in the hooks directory,
        which their wiring net then reported as a hook nobody had registered.
        One check manufacturing the condition another check flags, with
        neither wrong on its own.

        Here the same artifact exists — `support.load` imports these scripts
        to test them — and the only thing standing between it and a spurious
        "unclassified trigger" is that filter. So it is pinned WITH THE
        REASON, on exactly the argument I put to showrunner about their own
        accidental case: the point is not that the behaviour is load-bearing,
        it is that the ACCIDENT is. A later `glob` or a dropped `isfile` would
        start reporting a build artifact as an unwired guard, and the failure
        would read as a wiring problem rather than a listing one.
        """
        self.assertNotIn("__pycache__", self.shipped())
        self.assertIn("piped-verdict", self.shipped(),
                      "the filter must not have thrown out the real ones too")

    def test_a_BY_HAND_trigger_carries_its_reason(self):
        """`by-hand` is the answer that asserts nothing invokes it, which is
        the same claim the defect made by accident. It costs a sentence."""
        for name, where in EXPECTED.items():
            if where == "by-hand":
                self.assertIn(name, BY_HAND_REASON,
                              "by-hand without a reason is a shrug")

    def test_an_ENTRY_names_a_trigger_that_still_exists(self):
        """The direction that rots quietly: an entry for a deleted trigger
        reads as a decision about something real."""
        gone = [name for name in EXPECTED if name not in self.shipped()]
        self.assertEqual(gone, [], "classified triggers that no longer exist")

    def test_where_a_REGISTRY_IS_VISIBLE_the_wiring_is_checked(self):
        """The other half, and it SKIPS OUT LOUD rather than passing quietly.

        On this machine both registries exist and every hook is verified
        present. On a cold clone neither does, and the honest report is that
        registration went unchecked — not a green tick over a question nobody
        asked.
        """
        registries = {name: text for name, text in self.registries()}
        local = registries.get(".claude/settings.local.json")
        loop = registries.get(".game_loop/triggers.json")
        if local is None and loop is None:
            self.skipTest("neither registry is visible — both are gitignored, "
                          "so registration cannot be checked from this "
                          "checkout. WHERE each trigger belongs is still "
                          "enforced above.")
        for name, where in sorted(EXPECTED.items()):
            text = {"claude-hook": local, "game-loop": loop}.get(where)
            if where == "by-hand" or text is None:
                continue
            with self.subTest(trigger=name):
                self.assertIn(name, text,
                              "%s is classified %s and is not in that "
                              "registry" % (name, where))


if __name__ == "__main__":
    unittest.main()
