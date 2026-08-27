#!/usr/bin/env python3
"""Which `unchecked-ok` entries does the suite actually read?

An exemption in `.game_loop/verify.yaml` says: *somebody looked at this path
and concluded no command could meaningfully FAIL on it.* That is a claim, and
unlike prose its subject can be ENUMERATED — the repo knows which files a
command opens. gameloop's line, which is sharper than mechanisable-or-not:

    can the claim's subject be named in the repo, or only described?

Their measurement in game_loop found four stale exemptions. They asked for a
second repo before anyone calls the shape general. This is that measurement.

HOW IT DECIDES. It runs the real suite with `open` recorded, so an entry is
reported only when a test genuinely opened a matching file during a real run.
Static grepping would guess; this watches. What it misses is stated below
rather than left for somebody to discover:

  - A FILE READ BY SOMETHING OTHER THAN THE SUITE — a hook, the CLI in normal
    use, a trigger fired by the harness — is invisible here. The claim is "no
    COMMAND fails on these files", and this only watches one command.
  - A FILE READ THROUGH os.open, subprocess, or a C extension is not seen.
    Everything in this repo is stdlib Python reading with `open`, so that is a
    narrow gap today and will not announce itself if that changes.
  - AN ENTRY WITH NO MATCHING FILE AT ALL is not reported here. That is the
    vacuous-exemption question, which `verify` already answers; reporting it
    twice would make one number look like two findings.
  - A WHOLE-TREE SWEEP IS NOT EVIDENCE and is excluded — see the criterion
    where it is applied. The threshold separating a sweep from a test is a
    judgement in a gap of about two, so the distribution is printed on every
    run and the number is not to be quoted without it.

RESULT HERE: 3 of 6 stale — `*.md`, `llms.txt` and `.game_loop/**`, with
LICENSE, `.gitignore` and `pubspec.lock` coming back honest. Two of the three
had already been found by reading; `.game_loop/**` had not, and it is real:
`WiredTriggersResolveTest` asserts every wired trigger command resolves, so a
command genuinely can fail on a file under that directory.

All three live in the policy file, which this session's write guard refuses to
edit — correctly. So this reports and cannot fix, which is the right split.

    python3 test/audit_exemptions.py
"""
import builtins
import fnmatch
import io
import os
import re
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import run as gate                                            # noqa: E402

POLICY = os.path.join(ROOT, ".game_loop", "verify.yaml")


def exemptions(path):
    """The `unchecked-ok` list. Parsed narrowly, on purpose.

    A YAML library is not available (stdlib only, the same constraint the
    hooks run under), and a loose parse that silently returned [] would
    report a clean bill from a file it could not read — the failure this
    whole thread is about. So: find the key, take the indented `- "..."`
    lines under it, and stop at the first line that is not one.
    """
    try:
        with open(path) as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None
    found, collecting = [], False
    for line in lines:
        if line.startswith("\"unchecked-ok\":") or line.startswith("unchecked-ok:"):
            collecting = True
            continue
        if collecting:
            match = re.match(r"\s+-\s+[\"']([^\"']+)[\"']\s*$", line)
            if not match:
                if line.strip() and not line.strip().startswith("#"):
                    break
                continue
            found.append(match.group(1))
    return found


def covered_by(pattern, relative):
    """Does this exemption claim this file? `<dir>/**` means anything under."""
    if pattern.endswith("/**"):
        return relative.startswith(pattern[:-2])
    return fnmatch.fnmatch(relative, pattern)


def main():
    claims = exemptions(POLICY)
    if claims is None:
        print("CANNOT REPORT — %s is unreadable. Every line below would be a "
              "statement\n  about this tool, not about the policy." % POLICY)
        return 4
    if not claims:
        print("CANNOT REPORT — parsed 0 exemptions out of %s. An empty list "
              "and a parser\n  that has stopped understanding the file look "
              "identical from here." % POLICY)
        return 4

    # ATTRIBUTE EVERY OPEN TO ITS CALLER, and drop the harness's own.
    #
    # The first version counted any open at all and reported 6 of 6 exemptions
    # as stale — including LICENSE, which no test so much as mentions. The
    # reader was `fingerprint_repo()`, the runner's repo-damage check, which
    # opens every tracked file to hash it. That is bookkeeping ABOUT the
    # files, not a command that could fail ON them, and counting it made the
    # instrument report the maximum possible answer.
    #
    # Which is how I knew it was wrong: gameloop's run of the same idea had
    # LICENSE and .gitignore come back honest, and said that was "what makes
    # the four a verdict rather than a list". An audit that flags everything
    # has not measured anything — the same defect as the corpus scanner that
    # reported the guard's own remedy as 317 misses.
    # A UNIVERSAL READER IS NOT EVIDENCE ABOUT ANY PARTICULAR EXEMPTION, and
    # that is the criterion rather than a list of module names.
    #
    # `unchecked-ok` exempts a path from the PER-PATH rules that say what a
    # change owes. A check that applies to every tracked file holds whatever
    # is on that list, so it cannot distinguish a stale entry from a fresh
    # one. gameloop's formulation, after their instrument and mine hit the
    # same wall from opposite sides.
    #
    # This audit's first run reported 6 of 6 stale — including LICENSE, which
    # no test so much as mentions — because the repo-damage check hashes every
    # tracked file. The second still said 6, because `mutate.py` parses every
    # tracked file for its candidate set: a second whole-tree sweep, in a
    # different module, for a different reason. Naming those two would have
    # worked until the third one arrived under a name nobody had listed.
    #
    # So the rule is the PROPERTY: a call site that opens a large share of the
    # tree is a sweep, and a sweep is excluded. Nothing to rename, nothing to
    # forget, and a new sweeper is handled the day it is written.
    #
    # THE THRESHOLD IS A JUDGEMENT AND THIS COMMENT SAID IT WAS NOT. It read
    # "the gap between a sweep and a test is three orders of magnitude here" —
    # written before measuring. Measured, over 113 tracked files:
    #
    #     51  test/mutate.py:3029     sweep: every tracked file, AST-parsed
    #     18  test/mutate.py:3074     sweep
    #     14  test/mutate.py:3046     sweep
    #      6  test/test_wiring.py:604 the start-command scan, over a NAMED list
    #      3  ...and down from there
    #
    # 14 against 6 is a factor of two, not a chasm. So the distribution is
    # printed on every run rather than summarised: a reader can see where the
    # line fell and disagree with it, which is the only honest form for a
    # number somebody picked. A sum is not a distribution.
    tracked = 0
    try:
        tracked = sum(1 for _ in gate.mutate.tracked_files())
    except Exception:
        pass
    if tracked < 20:
        print("CANNOT REPORT — resolved %d tracked files. The sweep rule "
              "below is a\n  fraction of that number, so it cannot be applied "
              "to it." % tracked)
        return 4
    sweep_floor = max(8, tracked // 10)

    sites, real = {}, builtins.open

    def watch(file, *args, **kwargs):
        try:
            frame = sys._getframe(1)
            site = "%s:%d" % (frame.f_code.co_filename, frame.f_lineno)
            path = os.path.abspath(file)
            if path.startswith(ROOT + os.sep):
                sites.setdefault(site, set()).add(path)
        except Exception:
            pass
        return real(file, *args, **kwargs)

    builtins.open = watch
    try:
        quiet = io.StringIO()
        with redirect_stdout(quiet), redirect_stderr(quiet):
            unittest.TextTestRunner(stream=quiet, verbosity=0).run(gate.suite())
    finally:
        builtins.open = real

    sweeps = {s: p for s, p in sites.items() if len(p) >= sweep_floor}
    inside = set()
    for site, paths in sites.items():
        if site in sweeps:
            continue
        for path in paths:
            inside.add(os.path.relpath(path, ROOT))

    print("tracked files                  %d" % tracked)
    print("call sites that opened a file  %d" % len(sites))
    print("  of those, WHOLE-TREE SWEEPS  %d  (>= %d files each; excluded)"
          % (len(sweeps), sweep_floor))
    for site, paths in sorted(sweeps.items(), key=lambda kv: -len(kv[1])):
        print("      %4d files  %s" % (len(paths), os.path.relpath(
            site.rsplit(":", 1)[0], ROOT) + ":" + site.rsplit(":", 1)[1]))
    # THE WHOLE DISTRIBUTION, every run. The threshold is a judgement in a
    # gap of about two, so summarising it would hide the one thing a reader
    # needs in order to disagree.
    print("  files opened per call site, largest first:")
    for site, paths in sorted(sites.items(), key=lambda kv: -len(kv[1]))[:8]:
        where = site.replace(ROOT + os.sep, "")
        print("      %4d  %-46s %s" % (len(paths), where,
                                       "SWEEP" if site in sweeps else ""))
    print("exemptions declared            %d" % len(claims))
    print("files the suite opened in-tree %d" % len(inside))
    stale = []
    for claim in claims:
        hits = sorted(r for r in inside if covered_by(claim, r))
        if hits:
            stale.append((claim, hits))
    print("exemptions the suite READS     %d" % len(stale))
    for claim, hits in stale:
        print("\n  %s" % claim)
        for hit in hits[:6]:
            print("      the suite reads %s" % hit)
    if not stale:
        print("\n  none — every exemption still describes files no command "
              "opens.")
    # REPORTS, does not gate. Whether a read makes an exemption wrong is a
    # judgement about that path, and failing a build on it would move the
    # decision from a person to a filename match.
    return 0


if __name__ == "__main__":
    sys.exit(main())
