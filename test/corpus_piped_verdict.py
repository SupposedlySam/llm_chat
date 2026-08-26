#!/usr/bin/env python3
"""What does `triggers/piped-verdict` MISS over a real corpus of commands?

WHY THIS FILE EXISTS AND WHY IT IS TRACKED. Two separate admissions, both
made in #llm_chat_owner this week:

  - I reported the guard as "7 of 7 refused, 0 missed". That was over seven
    shapes another agent handed me, not over a population. auditor's line for
    it: ZERO KNOWN MISSES IS WHAT A SILENT-GAP LIST LOOKS LIKE FROM THE
    INSIDE. The guard's VERDICT rule is a hand-kept list of command names, and
    a name not in the set is ALLOWED — so every gap in it is silent by
    construction and can never produce a visible failure.
  - Every number I sent that agent came from scratchpad scripts that no longer
    exist. They could not audit one of them, and neither could I. Their remedy,
    which I agreed with and had not adopted: the instrument lands in the repo,
    so a number without a committed tool is visibly a number without one.

This is that tool. It implements NO predicate of its own — it imports the
shipped hook and asks it — because a standalone classifier measures something
other than what ships, which is the exact defect that produced their retracted
8-of-8 table.

    python3 test/corpus_piped_verdict.py [transcript.jsonl ...]

With no arguments it reads this project's own Claude Code transcripts.
"""
import glob
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from support import load                                    # noqa: E402

guard = load("triggers/piped-verdict")

# A COMMAND THAT READS A VERDICT THROUGH A TRUNCATOR, defined here ONLY to
# find candidates in the corpus — never to decide them. The guard decides.
# Deliberately WIDER than the guard: this is the population the guard is
# measured against, and a population narrowed to what the guard already
# catches would report zero misses by construction, which is the thing being
# measured.
TRUNCATED = re.compile(r"(\|&|\|)\s*(tail|head)\b")
READS_STATUS = re.compile(r"\$\?|\bPIPESTATUS\b")


def commands(paths):
    """Every Bash command actually issued, in order, with duplicates kept.

    Duplicates are kept on purpose: a shape typed forty times is forty
    chances for the guard to be wrong, and de-duplicating would report the
    rate at which SHAPES are missed rather than at which COMMANDS are.
    """
    for path in paths:
        try:
            with open(path) as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    for block in (row.get("message") or {}).get("content") or []:
                        if not isinstance(block, dict):
                            continue
                        if block.get("name") != "Bash":
                            continue
                        command = (block.get("input") or {}).get("command")
                        if command:
                            yield path, command
        except OSError:
            continue


def main(argv):
    paths = argv[1:] or sorted(glob.glob(os.path.expanduser(
        "~/.claude/projects/-Users-supposedlysam-dev-llm-chat/*.jsonl")))
    if not paths:
        print("no transcripts found — pass them as arguments")
        return 2

    total = truncating = reads_status = fired = missed = benign = 0
    misses = []
    for _, command in commands(paths):
        total += 1
        # THE GUARD'S OWN HEREDOC STRIP, because the question is what it
        # missed of what it could SEE. Without this the population includes
        # prose ABOUT the defect — the messages describing it quote
        # `echo "exit=$?"` and `| head` as examples — and two of those were
        # reported as misses on the first run. The guard had stripped them
        # correctly; the measurement had not.
        visible = guard.strip_heredocs(command)
        if not TRUNCATED.search(visible):
            continue
        truncating += 1
        status = bool(READS_STATUS.search(visible))
        reads_status += status
        if guard.offence(command):
            fired += 1
        elif status:
            # A status read through a truncator IN THE SAME SEGMENT is the
            # only arrangement where the number read can be the truncator's.
            # Across a `;` it belongs to an earlier command — which is the
            # guard's own recommended remedy, and 317 of the first run's 319
            # "misses" were exactly that.
            for segment in re.split(r"[;&]{1,2}|\|\|", visible):
                found = TRUNCATED.search(segment)
                if found and READS_STATUS.search(segment[found.end():]):
                    missed += 1
                    misses.append(" ".join(command.split())[:110])
                    break
            else:
                benign += 1

    print("transcripts read            %d" % len(paths))
    print("Bash commands               %d" % total)
    print("  piping into head/tail     %d" % truncating)
    print("  ...and reading $?         %d" % reads_status)
    print("  REFUSED by the guard      %d" % fired)
    print("  allowed, $? is an EARLIER command's   %d" % benign)
    print("  MISSED (status through a truncator)   %d" % missed)
    if misses:
        print()
        for snippet in misses:
            print("  %s" % snippet)
    # NOT an assertion. This reports; it does not gate. A corpus is a fact
    # about one machine's history, and failing a build on it would make the
    # number something people route around rather than read.
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
