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
from mutate import discover_sources  # noqa: E402


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


# Directories in THIS repo that the suite must never modify. A test that writes
# here has escaped its temp directory: one did, setting CLAUDE_PROJECT_DIR to the
# real checkout and leaving a junk room in .llm_chat/joined.json that the live
# hooks would then have polled on every tool call. Nothing in the run failed;
# it was found by accident. So the suite now proves it kept its hands to itself.
GUARDED = (".llm_chat", ".claude")

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


def fingerprint_repo():
    state = {}
    for relative in GUARDED:
        base = os.path.join(ROOT, relative)
        for dirpath, _, filenames in os.walk(base):
            for name in filenames:
                path = os.path.join(dirpath, name)
                try:
                    with open(path, "rb") as f:
                        state[path] = hashlib.sha256(f.read()).hexdigest()
                except OSError:
                    state[path] = "unreadable"
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
    return ok["value"], report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=float, default=None,
                    help="fail below this line-coverage percentage")
    ap.add_argument("--tests-only", action="store_true")
    args = ap.parse_args()

    before = fingerprint_repo()
    globals_before = shared_callables()

    if args.tests_only:
        passed = run_tests()
        if report_repo_damage(before, fingerprint_repo()):
            return 1
        if report_global_leaks(globals_before):
            return 1
        return 0 if passed else 1

    ok, report = measure()
    damaged = (report_repo_damage(before, fingerprint_repo())
               or report_global_leaks(globals_before))
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
