#!/usr/bin/env python3
"""Do two-party bursts actually happen, and would a limit fire on real work?

WHY THIS IS TRACKED. I proposed a runaway detector — two identities
alternating, nobody else speaking, the whole run inside a short window — from
the SHAPE of the politeness loop rather than from any measurement of it. This
project's own rule is that a rate needs a committed tool that reproduces it,
and the version of this check I nearly shipped had thresholds I had picked by
imagining the failure instead of looking at one.

It reads the store directly rather than `read --json`, because that surface
does not carry `created_at` — so no consumer of it can reason about timing at
all, which is its own finding.

    python3 test/audit_runaway.py [room ...]

With no arguments it audits every room this project has joined.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from support import load                                      # noqa: E402

cli = load("bin/llm_chat")


def runs_in(messages):
    """Every maximal two-party alternating run, longest first.

    Deliberately computed WITHOUT the proposed thresholds. A scan that only
    reports runs already over the limit answers "does my limit fire" and not
    "where would any limit sit", which is the question that decides the
    number.
    """
    ordered = sorted(messages, key=lambda m: m["seq"])
    found, run = [], []
    for message in ordered:
        who = message["from_identity"]
        if run:
            speakers = {m["from_identity"] for m in run} | {who}
            if who == run[-1]["from_identity"] or len(speakers) > 2:
                found.append(run)
                run = []
        run.append(message)
    found.append(run)
    return sorted((r for r in found if len(r) > 1), key=len, reverse=True)


def span_ms(run):
    stamps = [m.get("created_at") for m in run
              if isinstance(m.get("created_at"), (int, float))]
    if len(stamps) < 2:
        return None
    return max(stamps) - min(stamps)


def main(argv):
    rooms = argv[1:]
    if not rooms:
        try:
            rooms = sorted(cli.read_joined())
        except Exception as problem:
            print("cannot read joined rooms: %s" % problem)
            return 2
    if not rooms:
        print("no rooms to audit — name one as an argument")
        return 2

    server = cli.DEFAULT_SERVER
    total_runs = 0
    for room in rooms:
        try:
            messages = cli.rows(server, "messages", cli.eq("channel", room))
        except SystemExit as problem:
            print("#%-22s COULD NOT READ — %s" % (room, problem))
            continue
        if not messages:
            print("#%-22s empty" % room)
            continue
        runs = runs_in(messages)
        total_runs += len(runs)
        longest = runs[0] if runs else None
        print("#%-22s %4d messages, %3d two-party runs, longest %d turns"
              % (room, len(messages), len(runs),
                 len(longest) if longest else 0))
        for run in runs[:5]:
            span = span_ms(run)
            pair = sorted({m["from_identity"] for m in run})
            print("     %2d turns  %-28s %s"
                  % (len(run), " <-> ".join(pair),
                     "no timestamps" if span is None
                     else "%.1f min" % (span / 60000.0)))
    if not total_runs:
        print("\nNO TWO-PARTY RUNS AT ALL — the shape this was built for does "
              "not occur here.\n  Any threshold would be a statement about "
              "nothing.")
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
