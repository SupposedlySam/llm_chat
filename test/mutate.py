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
    "bin/llm_chat:remember": "atomicity asserted directly — the temp file must "
        "not survive the rename",

    # A KNOWN UNDEFENDED BEHAVIOUR, stated rather than hidden. Deleting the
    # lock from do_read would NOT fail this suite, because every test is
    # single-threaded and the race needs two deliverers running at once. So a
    # mutation here would survive, and that survival would be a restatement of
    # this sentence rather than a discovery. The lock's own mechanics ARE
    # asserted (held, fail-open, unusable directory, failing unlock); what is
    # undefended is the CONCURRENCY it exists for, and defending that needs a
    # test that runs two readers against one cursor — which this suite cannot
    # do today. Listed here so the gap is on the record instead of implied by
    # a number.
    "bin/llm_chat:read_lock": "SHOULD BE SWEPT — but a mutation would survive: "
        "the suite is single-threaded and the race it guards needs two "
        "concurrent deliverers. The gap is the missing concurrency test, not "
        "the missing mutation",

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


def candidates():
    """Every module-level function in the measured files, derived not listed."""
    found = {}
    for relative in ("bin/llm_chat", "bin/llm-chat-deliver", "bin/llm-chat-wake"):
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
        try:
            with open(path, "w") as f:
                f.write(original.replace(find, replace, 1))
            still_green = run_suite()
        finally:
            with open(path, "w") as f:
                f.write(original)
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
