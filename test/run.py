#!/usr/bin/env python3
"""Run the suite and gate on line coverage. Stdlib only — `python3 test/run.py`.

    python3 test/run.py              # tests + coverage report
    python3 test/run.py --min 100    # fail unless every line is executed
    python3 test/run.py --tests-only # skip coverage (faster inner loop)

Coverage comes from `trace`, which ships with Python, because the runtime this
tests refuses a `pip install` and a suite that needs installing is a suite that
stops being run.

WHAT THIS MEASURES, AND WHAT IT DOES NOT. It counts lines executed. A line
executed by a test that asserts nothing counts here exactly the same as one
defended by a test that fails when the behaviour breaks — so 100% is evidence
that nothing is UNVISITED, never evidence that anything is CORRECT. It is the
measure, not the goal. Treat a coverage number as a list of places nobody has
looked, and read the tests for whether they would fail.

Warnings are errors: a suite that prints warnings trains you to ignore its
output, and the first real one then goes unread.
"""
import argparse
import ast
import fcntl
import hashlib
import os
import sys
import trace
import unittest
import warnings

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIN = os.path.join(ROOT, "bin")

# DISCOVERED, not listed. This was a hardcoded tuple of three names, and the
# moment a fourth script arrived it reported a confident 100% over a set that
# had quietly stopped containing it — while the mutation sweep, whose identical
# defect had already been fixed, saw the new file immediately. Two sibling
# tools, one fixed, one not, disagreeing about the same repo.
#
# Imported rather than reimplemented: two copies of a discovery rule drift, and
# a completeness check that has drifted reports completeness about the wrong
# set. That is the whole family of failures this file exists to talk about.
import mutate
from mutate import discover_sources  # noqa: E402


def sweep_in_progress():
    """Is a mutation sweep running right now?

    Asked of the sweep's OWN lock rather than an environment variable a child
    could inherit by accident or a caller could set to silence the check. If
    the lock is free, nothing is sweeping and a mutation in the tree is
    genuinely stranded.

    Fails CLOSED: if the lock cannot be examined at all, this says no sweep,
    which leaves the stranded check running. A guard that switches itself off
    when it cannot tell is not a guard.
    """
    try:
        handle = open(mutate.LOCK, "a")
    except OSError:
        return False
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True            # somebody holds it: a sweep is mid-measurement
    finally:
        handle.close()         # releases the lock we just took, if we got it
    return False


def stranded_mutations():
    """Mutations left applied in the working tree by a sweep that was killed.

    mutate.py restores in a `finally`, which SIGKILL does not run. So a sweep
    killed with -9 leaves the repo holding a deliberately broken program — and
    other agents invoke bin/llm_chat and bin/llm-chat-wake by ABSOLUTE PATH
    into this tree, so they run it too, for as long as nobody notices.

    Nobody noticed. Four mutations sat stranded across two files: the
    supersession check removed from the waker, and `chan_count_placeholder is
    not defined` in the CLI — which is the exact NameError a neighbouring agent
    reported hours earlier and which retired its waker permanently. The damage
    was attributed to a transient sweep window; it was not transient, it was
    left behind.

    Checked here because this runs on every verify and every commit, so the
    window between stranding and screaming is one check rather than hours.
    """
    # A SWEEP IN PROGRESS IS NOT A STRANDING, and failing to say so made this
    # guard silently destroy the one it sits beside.
    #
    # The sweep writes a mutation and then runs this suite to see whether
    # anything notices. This check saw the mutation, refused to run at all,
    # and returned 1 — which the sweep read as "the suite went red, so the
    # behaviour is defended". Every mutation came back `caught` because the
    # tests never executed. 130 of 133 in the run that found this.
    #
    # Two guards, each right alone, and the newer one silently emptied the
    # older. Asking whether a sweep holds its lock is the difference between
    # "this tree is broken" and "this tree is mid-measurement".
    if sweep_in_progress():
        return []
    stranded = []
    for name, relative, find, replace, _ in mutate.MUTATIONS:
        path = os.path.join(ROOT, relative)
        try:
            with open(path) as f:
                source = f.read()
        except OSError:
            continue
        if find not in source and replace in source:
            stranded.append((name, relative))
    return stranded


def measured():
    """Repo-relative paths, shared with the mutation sweep.

    This returned BASENAMES and joined them onto bin/, which was invisible while
    every measured file happened to live there. The moment discovery reached
    triggers/ it went looking for bin/learnings-broadcast and crashed — the
    lucky failure. A quieter version of the same assumption would have measured
    the wrong file and reported a number.
    """
    return discover_sources()

def executable_lines(path):
    """Statement lines the tracer could plausibly report, via the parser.

    Heuristics get this wrong in the direction that LIES: the first version
    counted every line of a module docstring as executable, which inflated the
    denominator and understated coverage. `ast` knows what a statement is.

    Excluded on purpose, and only these: the `if __name__ == "__main__"` guard
    and its body, which exist to be run as a script and cannot be reached by
    importing the module. An exclusion is a stated decision; anything else
    uncovered is reported as a gap to explain.
    """
    with open(path) as f:
        source = f.read()
    tree = ast.parse(source)

    guarded = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test = node.test
            if (isinstance(test, ast.Compare)
                    and isinstance(test.left, ast.Name)
                    and test.left.id == "__name__"):
                guarded.add(node.lineno)
                for child in ast.walk(node):
                    if isinstance(child, ast.stmt):
                        guarded.add(child.lineno)

    lines = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt):
            continue
        # a bare string expression is a docstring, not work
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            continue
        if node.lineno in guarded:
            continue
        lines.add(node.lineno)
    return lines


# The GITIGNORED state directories the suite must never modify. A test that
# writes here has escaped its temp directory: one did, setting
# CLAUDE_PROJECT_DIR to the real checkout and leaving a junk room in
# .llm_chat/joined.json that the live hooks would then have polled on every
# tool call. Nothing in the run failed; it was found by accident.
#
# THIS LIST USED TO BE THE WHOLE GUARD, and that was the defect wcs named in
# #learnings: "a guard that names directories reports all-clear about a set
# that stopped containing everything." It watched two directories, so a test
# escaping into bin/, triggers/, lib/ or the repo root was invisible — and
# bin/ is where the mutation sweep edits files in place, which has already
# stranded four of them here for hours.
#
# Everything git can enumerate is now asked of git instead (see
# guarded_paths), so a directory added next week is covered the day it is
# created rather than the day somebody remembers this tuple. What stays named
# here is only what git is TOLD to ignore, which is a genuinely closed set:
# state our own hooks write.
#
# wcs's literal form was `git ls-files --cached --others`, without
# --exclude-standard. Measured here that is 189 files including an 815MB
# SQLite database, so the principle is adopted and the command is not.
GUARDED_IGNORED = (".llm_chat", ".claude")

# Shared callables a test might swap and forget to restore. Reaching into a
# module the code under test imported — `mod.subprocess.run = stub` — patches
# the REAL module for every test that follows, and nothing puts it back. That
# happened here: the shell tests then "ran" install.sh, received a canned exit
# 0, and asserted against files nothing had written. Nineteen failures with one
# cause, and none of them pointed at it.
def shared_callables():
    import subprocess
    return {"subprocess.run": subprocess.run, "os.kill": os.kill,
            "os.makedirs": os.makedirs}


def report_global_leaks(before):
    after = shared_callables()
    leaked = [name for name in before if before[name] is not after[name]]
    if not leaked:
        return False
    print("\nTHE SUITE LEFT SHARED CALLABLES PATCHED: %s" % ", ".join(leaked),
          file=sys.stderr)
    print("  A test patched a module the code under test imported, so every"
          "\n  test after it ran against the stub. Patch the attribute on the"
          "\n  module under test instead, or restore it.", file=sys.stderr)
    return True


# Written by the LIVE hooks on every tool call in this repo, asynchronously
# with respect to the suite — `llm-chat-deliver` stamps post-tool-use, the
# waker stamps its own. Their contents are evidence about the SESSION, not
# about the suite, and this check cannot tell the two apart: a marker that
# appears mid-run means an agent ran a tool, which is the normal state of
# working here.
#
# Including them made the gate fail intermittently on its own author. It cost
# a refused `lamp publish` whose reason was then discarded by a pager, and the
# failure was written up as unreproducible — it was not, it was this, and it
# only reproduces while something is actively using the repo.
#
# THE LIST WAS SHORTER THAN ITS OWN REASONING. It named only `probe`, while the
# paragraph above says "the waker stamps its own" — and the waker's stamps are
# `wake.pid` and `wake.exit`, written every time it starts or is superseded.
# So the same defect the comment describes came back through the two files the
# comment mentions and the list omitted, and it cost a second refused
# `lamp publish`: 891 tests OK, 100% coverage, exit 1, because a waker
# restarted during a 20-second run.
#
# It reproduces only while an agent is working in this repo, which is exactly
# when a release is being cut, and never while nothing is running — which is
# how it survived a green gate minutes earlier.
#
# AND IT WENT SHORT AGAIN, which is why the last entry is now a PREFIX. Adding
# `wake.alive` to the waker put a third stamp beside the two named here, and
# naming files one at a time means the list is complete on the day it is
# written and silently incomplete afterwards — the exact criticism this
# module's own `guarded_paths` makes of hand-written lists, two functions
# down. `wake.` covers pid, exit, alive, rewake and landed, and covers the
# next one nobody has thought of yet. A rule about shape outlives a list of
# names.
#
# `.llm_chat/sessions/` is deliberately NOT excluded, though live hooks write
# there too. A test escaping into session state is a thing that has actually
# happened here — the bridge's question-tracking wrote into the real repo
# mid-suite — and the guard caught it. Losing that to silence a rarer race
# would be trading a real catch for a quiet gate.
#
# THIRD TIME. `slack-replies.json` failed a release while a live bridge polled
# during the run, and the maintenance queue the waker now writes on its
# heartbeat would have been the fourth. Enumerating by hand has now been wrong
# once per file added, so the LIST is still here but a test derives the
# expected set from the live processes' own path constants — see
# test_gate.py's test_every_LIVE_WRITTEN_path_is_excluded. Adding a state file
# to a background process without adding it here now fails a test that names
# it, instead of failing a release that does not.
UNGUARDED = (os.path.join(".llm_chat", "probe"),
             os.path.join(".llm_chat", "wake."),
             os.path.join(".llm_chat", "slack-"),
             os.path.join(".llm_chat", "maintenance."))


def guarded_paths():
    """Every file the suite must leave alone: git's answer, plus the ignored
    state directories git deliberately will not mention.

    Asking git is the half that cannot go stale. A hand-written directory list
    is complete on the day it is written and silently incomplete afterwards,
    which is how bin/ and triggers/ went unwatched while the mutation sweep
    was editing them in place.

    A git failure falls back to the named directories rather than raising:
    this guard runs inside every suite run, including in throwaway trees that
    are not repositories, and a check that cannot start is worse than one
    watching less. It is narrower, not silent — the fallback still catches the
    escape that motivated the guard.
    """
    seen = set()
    try:
        for relative in mutate.tracked_files():
            seen.add(os.path.join(ROOT, relative))
    except Exception:
        pass                      # not a checkout; the named dirs still apply
    for relative in GUARDED_IGNORED:
        base = os.path.join(ROOT, relative)
        for dirpath, _, filenames in os.walk(base):
            for name in filenames:
                seen.add(os.path.join(dirpath, name))
    return seen


def fingerprint_repo():
    state = {}
    for path in guarded_paths():
        # Matched per FILE, not per directory. The exclusion used to be
        # applied to `dirpath`, which worked only because the one entry was a
        # directory (`.llm_chat/probe/`). Adding a plain file to the list
        # would then have changed nothing at all — the tuple would name it,
        # the walk would still hash it, and the fix would look applied while
        # the gate kept failing.
        if any(os.path.relpath(path, ROOT).startswith(skip)
               for skip in UNGUARDED):
            continue
        try:
            with open(path, "rb") as f:
                state[path] = hashlib.sha256(f.read()).hexdigest()
        except OSError:
            continue              # absent is not evidence; only a CHANGE is
    return state


def report_repo_damage(before, after):
    changed = sorted(set(before) ^ set(after))
    changed += sorted(k for k in set(before) & set(after) if before[k] != after[k])
    if not changed:
        return False
    print("\nTHE SUITE MODIFIED THE REPO IT TESTS:", file=sys.stderr)
    for path in changed:
        print("  %s" % os.path.relpath(path, ROOT), file=sys.stderr)
    print("  A test escaped its temp directory. Fix the test, not this check.",
          file=sys.stderr)
    return True


def suite():
    loader = unittest.TestLoader()
    return loader.discover(start_dir=HERE, pattern="test_*.py", top_level_dir=HERE)


def run_tests():
    warnings.simplefilter("error", ResourceWarning)
    runner = unittest.TextTestRunner(verbosity=2)
    return runner.run(suite()).wasSuccessful()


def body_spans(relative):
    """{"file:func": (first executable line, last line)} for every def in a file.

    NOT (lineno, end_lineno). A function's `def` line runs at IMPORT, so a
    span starting there reports every function in an imported module as
    executed — including ones nothing ever calls. That is the check answering
    a different question than the one asked, which is the defect this whole
    function exists to find, and I wrote it that way first.

    The docstring is skipped too: it is a constant folded into __doc__ at
    compile time, not a statement the tracer sees on a call.
    """
    path = os.path.join(ROOT, relative)
    try:
        with open(path) as f:
            tree = ast.parse(f.read())
    except (OSError, SyntaxError, ValueError, UnicodeDecodeError):
        return {}
    spans = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = node.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]
        if not body:
            continue                      # nothing but a docstring to run
        spans["%s:%s" % (relative, node.name)] = (body[0].lineno,
                                                  node.end_lineno)
    return spans


def inert_exclusions(counts):
    """Excluded functions the suite NEVER EXECUTES, which is not an exclusion.

    THE FAILURE THIS ENCODES. `test/mutate.py:sweep_in_a_copy` sat in
    NOT_SWEPT with the reason "asserted directly by stubbing it and checking
    main() calls it before touching a source file". Both halves true, and
    together a claim about the CALL SITE — the only test that touched it
    replaced it with a lambda. The body had never run under a test, and it
    contains `max(child.wait() for child in running)`, which reduces eight
    shards to the number `verify` reads. `return 0` there would have made
    every sweep this project ever ran report success.

    A mutation cannot find this: aimed at that function it is applied inside
    the copy, whose main() never calls it, so the mutant cannot run itself.
    The verdict would be SURVIVED, which reads as "undefended" and is a
    different and much smaller claim than "never executes".

    WHY AN EXCLUSION IS THE RIGHT PLACE TO LOOK. For everything under the
    coverage floor, this can never fire — 100% line coverage already proves
    execution. The floor is built from `discover_sources`, which excludes
    test/ ("test/ measures; it is not the thing measured"), so the gate's own
    files have no floor. NOT_SWEPT is the only list that names functions in
    them, and it is exactly where a gap gets to wear the words of a decision.

    WHAT THIS MISSES, stated rather than implied: a function in test/ that
    nothing sweeps AND nothing excludes is invisible to this, because nothing
    names it. `report_unaccounted` catches that for the floored files and has
    no jurisdiction here. Closing it would mean giving test/ a floor of its
    own, which is a bigger decision than this one and has not been made.
    """
    import mutate
    inert = []
    for key, why in sorted(mutate.NOT_SWEPT.items()):
        relative = key.rsplit(":", 1)[0]
        span = body_spans(relative).get(key)
        if span is None:
            continue          # not a function — a file-level or module entry
        path = os.path.join(ROOT, relative)
        start, end = span
        ran = any(os.path.abspath(f) == path and start <= n <= end
                  for (f, n) in counts)
        if not ran:
            inert.append((key, why))
    return inert


def report_inert_exclusions(counts):
    if not (inert := inert_exclusions(counts)):
        return False
    print("\nEXCLUDED, AND NEVER EXECUTED — which is not the same claim:",
          file=sys.stderr)
    for key, why in inert:
        print("  %s\n      excused as: %s" % (key, why), file=sys.stderr)
    print("\n  An exclusion says a behaviour is covered some other way. Nothing"
          "\n  ran these at all, so whatever the reason describes, it is not"
          "\n  this function. Either give it a test or say plainly that it is"
          "\n  unexecuted — both are honest; the current entry is not.",
          file=sys.stderr)
    return True


def measure():
    """Run the suite under trace and return {script: (covered, total, missing)}."""
    tracer = trace.Trace(count=1, trace=0,
                         ignoredirs=[sys.prefix, sys.exec_prefix])
    warnings.simplefilter("error", ResourceWarning)
    ok = {"value": False}

    def go():
        runner = unittest.TextTestRunner(verbosity=0)
        ok["value"] = runner.run(suite()).wasSuccessful()

    tracer.runfunc(go)
    counts = tracer.results().counts

    report = {}
    for script in measured():
        path = os.path.join(ROOT, script)
        executable = executable_lines(path)
        hit = {n for (f, n) in counts if os.path.abspath(f) == path}
        missing = sorted(executable - hit)
        report[script] = (len(executable) - len(missing), len(executable), missing)
    # `counts` goes back too, because the same traced run answers a second
    # question — whether every EXCLUDED function was executed at all — and
    # tracing the suite twice to ask it would double the gate.
    return ok["value"], report, counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=float, default=None,
                    help="fail below this line-coverage percentage")
    ap.add_argument("--tests-only", action="store_true")
    args = ap.parse_args()

    # BEFORE anything else. A stranded mutation makes every result below a
    # measurement of a program nobody meant to ship, and the tests may even
    # pass — the point is that the TREE is wrong, not that the suite is red.
    if (stranded := stranded_mutations()):
        print("STRANDED MUTATIONS — this working tree is holding code a "
              "mutation sweep\nmeant to revert. A sweep killed with -9 skips "
              "its restore, and other agents\nrun these files by absolute "
              "path, so they are running it too.\n")
        for name, relative in stranded:
            print("  %-45s in %s" % (name, relative))
        print("\nRepair the tree before trusting anything below.")
        return 1

    before = fingerprint_repo()
    globals_before = shared_callables()

    if args.tests_only:
        passed = run_tests()
        if report_repo_damage(before, fingerprint_repo()):
            return 1
        if report_global_leaks(globals_before):
            return 1
        return 0 if passed else 1

    ok, report, counts = measure()
    damaged = (report_repo_damage(before, fingerprint_repo())
               or report_global_leaks(globals_before)
               or report_inert_exclusions(counts))
    print("\n=== line coverage (executed / executable) ===")
    total_hit = total = 0
    for script, (hit, count, missing) in sorted(report.items()):
        total_hit += hit
        total += count
        pct = 100.0 * hit / count if count else 100.0
        print("  %-20s %4d/%-4d  %5.1f%%" % (script, hit, count, pct))
        if missing:
            print("      not executed: %s" % _ranges(missing))
    overall = 100.0 * total_hit / total if total else 100.0
    print("  %-20s %4d/%-4d  %5.1f%%" % ("TOTAL", total_hit, total, overall))
    print("\n  Coverage says what nobody LOOKED at. It cannot say whether a"
          "\n  test would fail if the behaviour broke — read the tests for that.")

    if damaged:
        return 1
    if not ok:
        print("\nTESTS FAILED", file=sys.stderr)
        return 1
    if args.min is not None and overall + 1e-9 < args.min:
        print("\nCOVERAGE %.1f%% is below the required %.1f%%"
              % (overall, args.min), file=sys.stderr)
        return 1
    return 0


def _ranges(numbers):
    out, start, previous = [], numbers[0], numbers[0]
    for n in numbers[1:]:
        if n == previous + 1:
            previous = n
            continue
        out.append(str(start) if start == previous else "%d-%d" % (start, previous))
        start = previous = n
    out.append(str(start) if start == previous else "%d-%d" % (start, previous))
    return ", ".join(out)


if __name__ == "__main__":
    sys.exit(main())
