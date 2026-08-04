#!/usr/bin/env python3
"""Break each defended behaviour on purpose, and require the suite to notice.

    python3 test/mutate.py

100% line coverage says every line was VISITED. It cannot say any of them was
DEFENDED — a test that executes a line and asserts nothing counts identically to
one that fails the moment the behaviour changes. This is the check that tells
the two apart: revert a real fix, run the suite, and demand red.

A mutation that SURVIVES is the finding. It means the lines are covered, the
suite is green, and the behaviour is not actually protected — precisely the
false comfort a coverage number invites.

Each mutation below reverts a fix this project actually shipped, so the
"before" state is not hypothetical: it is the bug that was in the code.

Every mutation is applied to a COPY of the file and restored in a finally, so an
interrupted run cannot leave the repo mutated.
"""
import ast
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# (name, file, find, replace-with, what breaking it should mean)
MUTATIONS = [
    ("cursor high-water", "bin/llm_chat",
     'high_water = max((m["seq"] for m in fetched), default=since)',
     'high_water = chan_count_placeholder(server, name, since)',
     "a message landing mid-read is stepped over and lost forever"),

    ("self-echo filter", "bin/llm_chat",
     'waiting = [m for m in waiting if m["from_identity"] != identity]',
     'waiting = list(waiting)',
     "an agent reads its own words as new input and answers itself"),

    ("closed-room refusal at join", "bin/llm_chat",
     'if chan is not None and chan.get("closed"):',
     'if False:',
     "join reports success into a room that cannot be spoken in"),

    ("leave forgets the room", "bin/llm_chat",
     'if joined.pop(name, None) is not None:',
     'if False:',
     "joined.json grows forever and both hooks poll dead rooms"),

    ("project root walk-up", "bin/llm_chat",
     'here = probe = os.path.abspath(os.getcwd())',
     'return os.path.abspath(os.getcwd())',
     "a subdirectory becomes a second identity for one project"),

    ("cap warning before the wall", "bin/llm_chat",
     'if seq >= cap * 0.9:',
     'if False:',
     "an agent hits the message cap mid-thought with no warning"),

    ("supersession checked before polling", "bin/llm-chat-wake",
     "        if superseded():\n            return 0\n        if orphaned():\n            return 0",
     "        pass",
     "a superseded waker claims messages and delivers them nowhere"),

    ("orphan detection", "bin/llm-chat-wake",
     "    return PARENT != 1 and os.getppid() != PARENT",
     "    return False",
     "a waker outlives its session and silently consumes its messages"),

    ("the probe mark records its event", "bin/llm-chat-wake",
     'with open(os.path.join(d, "wake-%s" % event), "w") as f:',
     'with open(os.path.join(d, "wake-ignored"), "w") as f:',
     "'did SessionStart fire?' becomes unanswerable again"),

    ("delivery cap", "bin/llm-chat-deliver",
     "[:MAX_PER_DELIVERY]",
     "[:]",
     "one delivery can be large enough to derail a turn"),

    ("the read lock serialises claim-and-advance", "bin/llm_chat",
     "    with read_lock():\n        member = get_membership",
     "    if True:\n        member = get_membership",
     "two deliverers claim the same messages and the cursor advances once, "
     "so the other agent reads it as you repeating yourself"),

    ("broadcast rooms are auto-joined locally", "bin/llm_chat",
     '        remember(name, identity, server, broadcast=True)',
     '        pass',
     "an agent is a member server-side and never polls the room, because both "
     "hooks read the LOCAL record to decide what to poll"),

    ("only the --general form is broadcast", "triggers/learnings-broadcast",
     '    if not general:\n        return None',
     '    if False:\n        return None',
     "every local harden goes to every agent on the machine, as its incident "
     "form, which is how a shared channel becomes one nobody reads"),

    ("the retro digest drops my own posts", "triggers/learnings-digest",
     '            if "(you)" not in who and body]',
     '            if body]',
     "a retro that hands back your own learnings is a mirror where a window "
     "was wanted, and it reads as though others had been consulted"),

    ("the Slack bridge skips its own posts", "bin/llm-chat-slack",
     '    if message.get("bot_id") or message.get("subtype") == "bot_message":\n        return False',
     '    if False:\n        return False',
     "every relay comes back from Slack, is posted into llm_chat, wakes the "
     "room and relays again — forever"),

    ("upgrade notice fires once per session", "bin/llm-chat-deliver",
     "    if os.path.exists(marker):\n        return \"\"",
     "    if False:\n        return \"\"",
     "a standing gap becomes standing noise on every tool call"),
]


# DEFAULT-DENY. The list above is hand-written, and a hand-written list is a
# denylist wearing a checkmark: a behaviour nobody added is undefended AND does
# not appear as a gap, which is this tool's own version of the failure it exists
# to find. Reported independently by two other agents about their equivalents
# within the same hour; the shape of this fix is theirs.
#
# So the candidate set is DERIVED, every candidate must be accounted for, and an
# unaccounted one FAILS the run exactly like a surviving mutation.
#
# Not everything is swept: each entry costs a full suite run, so sweeping all 54
# would take minutes and produce a check nobody runs — its own kind of failure.
# The rest are excluded HERE, with reasons, and the reasons have to be true. An
# honest "should be swept, is not yet" is worth more than a false exclusion,
# because a false one is exactly the gaming this whole family of checks is about.
NOT_SWEPT = {
    # Asserted directly, so a mutation would be redundant rather than absent:
    # these have tests that fail the moment their behaviour changes.
    "bin/llm_chat:b": "wire convention asserted directly (0/1, not true/false)",
    "bin/llm_chat:now_ms": "wire convention asserted directly (epoch millis)",
    "bin/llm_chat:eq": "query shape asserted directly",
    "bin/llm_chat:gt": "query shape asserted directly",
    "bin/llm_chat:and_": "query shape asserted directly",
    "bin/llm_chat:valid": "name rules asserted directly over good and bad cases",
    "bin/llm_chat:port_of": "asserted directly for explicit, default and https ports",
    "bin/llm_chat:call": "every branch asserted directly, including both error shapes",
    "bin/llm_chat:rows": "error-to-exit asserted directly",
    "bin/llm_chat:create": "error-to-exit asserted directly",
    "bin/llm_chat:update": "error-to-exit asserted directly",
    "bin/llm_chat:read_joined": "missing and corrupt records asserted directly",
    "bin/llm_chat:identity_for": "fallback and refusal asserted directly",
    "bin/llm_chat:joined_path": "derived from project_dir, which IS swept",
    "bin/llm_chat:server_up": "all three answers asserted directly",
    "bin/llm_chat:wiring_fingerprint": "stability and missing-file asserted directly",
    "bin/llm_chat:installed_fingerprint": "present and absent asserted directly",
    "bin/llm_chat:host": "all three hosts asserted directly",
    "bin/llm_chat:waker_alive": "live, dead, absent and unreadable asserted directly",
    "bin/llm_chat:message_text": "all four paths asserted directly, including the refusal",
    "bin/llm_chat:invite": "content asserted directly, with and without a topic",
    "bin/llm_chat:get_channel": "trivial lookup, exercised by every room test",
    "bin/llm_chat:get_membership": "trivial lookup, exercised by every room test",
    "bin/llm_chat:hook_report": "registered/fired/events asserted directly",
    "bin/llm-chat-deliver:_project_dir": "identical to the CLI's, which IS swept",
    "bin/llm-chat-wake:_project_dir": "identical to the CLI's, which IS swept",
    "bin/llm-chat-wake:joined_rooms": "missing and corrupt records asserted directly",
    "bin/llm-chat-deliver:missing_hooks": "asserted directly, including malformed shapes",
    "bin/llm-chat-deliver:stale_install": "all four outcomes asserted directly",
    "bin/llm-chat-wake:poll": "all three outcomes asserted directly",

    "bin/llm-chat-wake:superseded": "its CALL SITE is swept (the "
        "before-polling ordering); the comparison itself is asserted directly "
        "for held, lost and unreadable pidfiles",
    "bin/llm_chat:read_lock": "its CALL SITE is swept — removing `with "
        "read_lock()` from do_read is caught by a two-thread test that gets the "
        "message delivered twice; the contextmanager's own mechanics (held, "
        "fail-open, unusable directory, failing unlock) are asserted directly",
    "bin/llm_chat:identity_path": "a path join; exercised by every identity test",
    "bin/llm_chat:project_identity": "present, absent and corrupt asserted directly",
    "bin/llm_chat:resolve_identity": "all four precedence cases asserted directly "
        "— explicit, per-channel, project, and the refusal naming both ways out",
    "bin/llm_chat:do_identify": "SHOULD BE SWEPT — writing the identity and "
        "reporting what it auto-joined are both asserted, but nothing proves the "
        "write is atomic the way remember's is",
    "bin/llm_chat:remember": "atomicity asserted directly — the temp file must "
        "not survive the rename",

    # The Slack bridge. Everything network- or CLI-facing is behind a seam and
    # asserted directly against a fake; what is swept is the one check whose
    # absence is an infinite loop.
    "bin/llm-chat-slack:__init__": "field assignment on the Slack client",
    "bin/llm-chat-slack:_call": "URL, body, query and auth header asserted "
        "directly by inspecting the request that would have gone out",
    "bin/llm-chat-slack:post": "asserted directly — endpoint, body and token",
    "bin/llm-chat-slack:history": "asserted directly — query form and cursor",
    "bin/llm-chat-slack:load_config": "every branch asserted directly, "
        "including the game_loop fallback and precedence between them",
    "bin/llm-chat-slack:read_cursor": "missing and corrupt asserted directly",
    "bin/llm-chat-slack:write_cursor": "atomicity asserted directly",
    "bin/llm-chat-slack:waiting_for_human": "asserted directly, including that "
        "it never passes --all, which would relay the human's own answers back",
    "bin/llm-chat-slack:say": "asserted directly, including that it sends via "
        "--file so a Slack message containing backticks survives",
    "bin/llm-chat-slack:check": "every Slack error branch asserted directly, "
        "and that --check posts nothing",
    "bin/llm-chat-slack:main": "both entry paths and the loop asserted directly",
    "bin/llm-chat-slack:pump_out": "SHOULD BE SWEPT — the lost-message report "
        "on a Slack outage is the only thing standing between a dropped "
        "escalation and silence",
    "bin/llm-chat-slack:pump_in": "SHOULD BE SWEPT — cursor advance past bot "
        "messages is asserted, but nothing proves the ordering guarantee",

    # The game_loop triggers. Both are thin scripts over the CLI, and what
    # matters in each is asserted directly against a fake subprocess; the two
    # guards whose absence changes what other agents SEE are swept.
    "triggers/learnings-broadcast:calling_repo": "all three links of the "
        "precedence chain asserted directly, plus set-but-empty",
    "triggers/learnings-digest:calling_repo": "same, asserted directly",
    "triggers/learnings-broadcast:send": "asserted directly — that it sends via "
        "--file, that the file holds the message while the CLI runs, and that "
        "it does not outlive the call",
    "triggers/learnings-broadcast:main": "every branch asserted directly, "
        "including that an unreadable payload reports instead of crashing "
        "inside another tool's output",
    "triggers/learnings-digest:split_messages": "asserted directly, including "
        "the multi-line case that line-slicing would split",
    "triggers/learnings-digest:render": "asserted directly, including that it "
        "does not claim truncation when it showed everything",
    "triggers/learnings-digest:fetch": "asserted directly — that it passes "
        "--peek and --all, which is the whole design",
    "triggers/learnings-digest:main": "every branch asserted directly, "
        "including that a read failure is loud rather than an empty digest",

    # Honest gaps. These SHOULD be swept and are not yet. Saying so beats an
    # exclusion that is technically true and practically a dodge.
    "bin/llm_chat:do_setup": "SHOULD BE SWEPT — the in-checkout guard and the "
                             "server-reuse branch are both worth a mutation",
    "bin/llm_chat:start_server": "SHOULD BE SWEPT — the bootstrap-step ordering "
                                 "is load-bearing for a fresh clone",
    "bin/llm_chat:do_reload": "SHOULD BE SWEPT — the two refusal guards are the "
                              "whole point of the verb",
    "bin/llm_chat:do_doctor": "SHOULD BE SWEPT — the LISTENING NOW branch is new "
                              "and already caught a live failure in another agent",
    "bin/llm_chat:do_channels": "SHOULD BE SWEPT — hiding closed rooms is a "
                                "behaviour a regression could silently undo",
    "bin/llm_chat:do_reopen": "SHOULD BE SWEPT — the cap refusal is easy to lose",
    "bin/llm_chat:install_hook": "SHOULD BE SWEPT — failure reporting only",
    "bin/llm_chat:main": "dispatch only; every subcommand asserted directly",
    "bin/llm_chat:message_source_placeholder": "unused",
    "bin/llm-chat-deliver:mark_fired": "SHOULD BE SWEPT — best-effort by design, "
                                       "but its silence on failure is load-bearing",
    "bin/llm-chat-deliver:main": "the notice and delivery paths ARE swept via "
                                 "upgrade_notice and the cap",
    "bin/llm-chat-wake:main": "the loop's exits are swept via superseded/orphaned",
    "bin/llm-chat-wake:wake": "exit code and stderr asserted directly",
    "bin/llm-chat-wake:still_worth_listening": "all outcomes asserted directly",
    "bin/llm-chat-wake:claim_pidfile": "SHOULD BE SWEPT — newest-wins is the "
                                       "property that stops N wake-ups per message",
}


def tracked_files():
    """Every file this repo ships, asked of git.

    Not a directory walk: a walk needs somebody to name the directories, and a
    directory list is a denylist wearing a different hat. git already knows the
    answer, it excludes build output and gitignored site wiring for free, and it
    cannot forget a folder somebody added last week.

    If git cannot answer — no checkout, no git on PATH — this raises rather than
    returning an empty list. An empty denominator makes every accounting report
    read 100%, which is the exact false green this whole module exists to catch;
    failing loudly is the only honest response to "I don't know what to measure".
    """
    # --others --exclude-standard as well as the index: a file written five
    # minutes ago and not yet `git add`ed is EXACTLY when it is unmeasured, and
    # a denominator that waits for staging reports completeness over the old set
    # at the one moment the set is changing. --exclude-standard keeps gitignored
    # site wiring out, which is the reason not to just walk the tree.
    done = subprocess.run(
        ["git", "-C", ROOT, "ls-files", "--cached", "--others",
         "--exclude-standard"], capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError("cannot enumerate sources: git ls-files said %r"
                           % (done.stderr or "").strip())
    return [line for line in done.stdout.splitlines() if line.strip()]


def discover_sources():
    """Every Python file this repo ships, found by PARSING rather than by naming.

    THIS SCANNED bin/ AND ONLY bin/, and the day triggers/ was added it went on
    reporting "0 unaccounted" about a set that had quietly stopped containing
    everything. That is the same defect as the hardcoded tuple this replaced,
    moved out one level: from a list of FILES to a list of DIRECTORIES. Asking
    git removes the list rather than lengthening it, which is the only version
    of this fix that does not have a next level.

    A hardcoded tuple was here, and it listed exactly the three files that
    exist — complete today, and complete BY ACCIDENT. The next script added to
    bin/ would be invisible to it while the accounting kept reporting "0
    unaccounted", which is this tool's own defect one level further out again:
    the denylist moved from the mutation list, to the function scan, to the
    FILE list. A sibling project found the identical thing inside the very
    measurement it used to find its file-level gap.

    The predicate is NOT "it ends in .py" — all three entrypoints are
    extension-less, so a glob returns nothing at all. Nor is it merely "it
    parses as Python": ast.parse ACCEPTS JSON and YAML, because both are valid
    Python expressions. A sibling project ran that version before adopting it
    and got eleven files of which four were config, which is not a stray entry
    but a majority of noise — and a list that is mostly noise is the standing-
    warning failure we have each already shipped once. So: parses AND declares
    something (def, class, or import).

    NOT COVERED, stated rather than implied: install.sh and legacy_teardown.sh.
    They have no AST and this harness cannot mutate them, so they are outside
    this denominator entirely — their behaviour is defended by test_shell.py,
    which runs them for real, but no mutation proves those tests would fail. A
    denominator that silently excludes a LANGUAGE is the same false green as
    one that excludes a file, so it is named here.
    """
    sources = []
    for relative in tracked_files():
        path = os.path.join(ROOT, relative)
        # test/ measures; it is not the thing measured. .game_loop/ is the
        # VENDORED harness — someone else's source, refreshed wholesale by their
        # installer, and mutating it would report our tests failing to catch
        # bugs in a file we do not own and cannot fix here. Both named with a
        # reason rather than silently missing, which is the rule this module
        # enforces on everything else.
        if not os.path.isfile(path):
            continue
        if relative.startswith("test/") or relative.startswith(".game_loop/"):
            continue
        try:
            with open(path) as f:
                tree = ast.parse(f.read())
        except (OSError, SyntaxError, ValueError, UnicodeDecodeError):
            continue
        if not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef, ast.Import, ast.ImportFrom))
                   for node in ast.walk(tree)):
            continue
        sources.append(relative)
    return sorted(sources)


def candidates():
    """Every module-level function in the measured files, derived not listed."""
    found = {}
    for relative in discover_sources():
        path = os.path.join(ROOT, relative)
        with open(path) as f:
            source = f.read()
        tree = ast.parse(source)
        # ast.walk, not tree.body. Enumerating only module level is correct for
        # these three files today — 54 top-level defs, 54 defs total, no classes
        # and no nested functions, measured. But that is a property of the
        # current code, not of the enumerator: add a class tomorrow and the
        # candidate set silently shrinks while the accounting still reports
        # "0 unaccounted". A sibling project hit exactly that — a discriminator
        # that undercounted made the gap hide in the candidate set rather than
        # in the exclusions, which is the harder place to see it.
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found["%s:%s" % (relative, node.name)] = (node.lineno,
                                                          node.end_lineno)
    return found


def swept_functions():
    """Which function each mutation lands in, resolved from its anchor.

    Derived rather than declared: a hand-maintained mapping would drift from
    the mutations it describes, which is the same defect one level down.
    """
    hit = set()
    spans = candidates()
    for _, relative, find, _, _ in MUTATIONS:
        path = os.path.join(ROOT, relative)
        with open(path) as f:
            source = f.read()
        index = source.find(find)
        if index < 0:
            continue
        line = source.count("\n", 0, index) + 1
        for key, (start, end) in spans.items():
            if key.startswith(relative + ":") and start <= line <= end:
                hit.add(key)
    return hit


def report_unaccounted():
    """Fail on any candidate that is neither swept nor explicitly excluded."""
    everything = set(candidates())
    swept = swept_functions()
    unaccounted = sorted(everything - swept - set(NOT_SWEPT))
    print("\ncandidates %d — swept %d, excluded with a reason %d, unaccounted %d"
          % (len(everything), len(swept), len(everything & set(NOT_SWEPT)),
             len(unaccounted)))
    gaps = sorted(k for k, why in NOT_SWEPT.items()
                  if why.startswith("SHOULD BE SWEPT") and k in everything)
    if gaps:
        print("  declared gaps (excluded, but they should not be):")
        for key in gaps:
            print("    %s" % key)
    if unaccounted:
        print("\nUNACCOUNTED — nobody decided about these, so they are"
              "\nundefended AND invisible, which is the failure this tool exists"
              "\nto find, in this tool:", file=sys.stderr)
        for key in unaccounted:
            print("    %s" % key, file=sys.stderr)
        return True
    return False


def run_suite():
    done = subprocess.run([sys.executable, os.path.join(HERE, "run.py"),
                           "--tests-only"],
                          cwd=ROOT, capture_output=True, text=True)
    return done.returncode == 0


def main():
    print("Reverting %d shipped fixes; each must turn the suite RED.\n"
          % len(MUTATIONS))
    survivors = []
    for name, relative, find, replace, consequence in MUTATIONS:
        path = os.path.join(ROOT, relative)
        with open(path) as f:
            original = f.read()
        if find not in original:
            print("  ?? %-38s ANCHOR MISSING in %s" % (name, relative))
            survivors.append((name, "anchor no longer present — mutation stale"))
            continue
        # Restore the TIMESTAMPS as well as the bytes. Rewriting the original
        # content still bumps mtime, and the commit gate reads mtime to decide
        # whether a file has changed since its checks last ran — so running
        # this sweep marked every file it touched as freshly modified, and the
        # gate then refused the commit because the evidence predated the
        # change. The evidence did not predate anything; the instrument had
        # altered the thing it was measuring.
        stat = os.stat(path)
        try:
            with open(path, "w") as f:
                f.write(original.replace(find, replace, 1))
            still_green = run_suite()
        finally:
            with open(path, "w") as f:
                f.write(original)
            os.utime(path, (stat.st_atime, stat.st_mtime))
        if still_green:
            print("  !! %-38s SURVIVED" % name)
            print("     %s" % consequence)
            survivors.append((name, consequence))
        else:
            print("  ok %-38s caught" % name)

    print()
    if survivors:
        print("%d mutation(s) SURVIVED — those behaviours are covered but not "
              "defended:" % len(survivors))
        for name, why in survivors:
            print("  - %s: %s" % (name, why))
        return 1
    print("Every reverted fix was caught.")
    return 1 if report_unaccounted() else 0


if __name__ == "__main__":
    sys.exit(main())
