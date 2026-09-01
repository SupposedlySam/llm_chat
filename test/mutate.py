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
import argparse
import ast
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LOCK = os.path.join(HERE, ".mutate.lock")


# HOW MANY MUTATIONS ARE STILL KILLED BY AN EXCEPTION RATHER THAN MEASURED.
#
# The first sweep that actually ran tests found thirteen. Two were fixed
# immediately — both were a test calling `json.loads` on output the code under
# test produced, so a verb broken into printing prose made the test ERROR
# before the comparison that would have named the defect ever ran. `support.
# parsed` is the remedy and the pattern to copy.
#
# The rest are real debt and are listed by name in every sweep. This number is
# a ratchet: it may be lowered, never raised. Raising it is how a measured gap
# becomes a permanent one.
#
# AT ZERO SINCE #22, and the ratchet is the whole point of it having been 11.
# Ten mutations went red by RAISING, so for those ten nothing had ever
# established whether the behaviour was defended — and "unknown" reads a lot
# like "fine" when it is one line in a green run. They survived two consecutive
# handbacks that named them as carried, which is where carried turns into
# hidden.
#
# What fixing them took, and the shape is worth keeping: about half were
# fragile TESTS — an index into a table that a neutered producer never wrote,
# so the assertion died on the way to the thing it was going to check. The
# other half were bad MUTATIONS, which is the more interesting half: a
# mutation that also breaks a bounds check, or a format string, or drops the
# argument a `%s` still needs, does not measure a behaviour. It measures that
# the program stops. Reverting the WORDING a fix introduced measures the fix;
# deleting the branch around it measures nothing.
#
# RAISING THIS ABOVE ZERO IS NOT A SETTING CHANGE — two things stop being true
# the moment it is nonzero, and neither is enforced anywhere else:
#
#   1. The comparison in `sweep_exit_code` runs PER SHARD against this global
#      number. `share_crashed > CEILING` implies `total_crashed > CEILING`, so
#      at 0 the two are identical; above 0, crashes spread thinly across eight
#      workers could each sit under the ceiling and total more than it. The
#      count would have to be aggregated in the parent first.
#   2. A sweep with permitted crashes reaches the end of `sweep_exit_code` and
#      prints "Every reverted fix in this share was caught BY A FAILING
#      ASSERTION" — under a report naming ones that were not. That sentence is
#      only honest because a crash cannot get past the ceiling today.
CRASHED_CEILING = 0

# Long enough for the suite plus a heavily loaded machine — eight shards
# compete for the cores — and short enough that a hang is reported rather than
# waited on. The suite takes about 40s alone; two shards once sat at 46
# MINUTES because a mutation made it block forever, which is how this number
# came to exist.
SUITE_DEADLINE = 600

# Set in the child so the copy does not copy itself forever.
IN_COPY = "LLM_CHAT_SWEEP_ISOLATED"
# "<index>/<total>" — which slice of the mutation list this copy owns.
SHARD = "LLM_CHAT_SWEEP_SHARD"
# The real checkout, told to a shard running inside a throwaway copy.
LIVE_ROOT = "LLM_CHAT_SWEEP_LIVE_ROOT"


def my_share(mutations):
    """The slice of the list this copy is responsible for.

    Strided rather than chunked, so a run of slow mutations in one part of the
    list does not land entirely on one worker while the others finish early.
    """
    where = os.environ.get(SHARD)
    if not where:
        return list(mutations)
    try:
        index, total = (int(part) for part in where.split("/", 1))
    except ValueError:
        return list(mutations)
    if total < 1 or not 0 <= index < total:
        return list(mutations)
    return list(mutations)[index::total]


def sweep_in_a_copy():
    """Run the whole sweep against a COPY of this repo, and return its code.

    A SWEEP MUTATES A TREE OTHER AGENTS ARE RUNNING. `bin/llm_chat` and
    `bin/llm-chat-wake` are invoked by ABSOLUTE PATH from every other repo on
    this machine, so for as long as each mutation is applied, those agents are
    running a deliberately broken program. That is not hypothetical: it is how
    `chan_count_placeholder is not defined` reached a neighbour and retired its
    waker.

    It was survivable while each mutation lasted about a second, because the
    stranded-mutation check was refusing to run the suite and the sweep was
    measuring nothing. Making the sweep real made each mutation last a full
    test run — forty times the exposure, on a machine with five live agents.

    So the honest sweep and the safe sweep are the same change. This is the fix
    the module docstring has described as "written, never verified, and
    reverted rather than shipped unmeasured" — verified now, because it is no
    longer optional.

    The copy carries .git deliberately: `discover_sources` asks git what this
    repo ships, and a copy without it would silently measure a different set.
    """
    # HOW MANY COPIES. A full test run per mutation makes a serial sweep about
    # an hour and a half, and a gate that takes that long is a gate people
    # skip — which is the same failure as the one this sweep was just found to
    # have: a check that does not really run. Splitting the list across
    # independent copies is the only lever, since each mutation genuinely
    # needs the whole suite.
    workers = max(1, min(8, (os.cpu_count() or 2) - 1))
    parent = tempfile.mkdtemp(prefix="llm_chat-sweep-")
    try:
        copies = []
        for shard in range(workers):
            copy = os.path.join(parent, "repo%d" % shard)
            # THE TREE IS LIVE WHILE THIS RUNS. `cp -R` failed here with
            # "slack-asked.json.tmp: No such file or directory" — the bridge
            # wrote a temp file, cp listed it, the bridge renamed it, cp went
            # to read a name that no longer existed. A copier that walks a
            # directory somebody else is writing has to tolerate that.
            #
            # rsync does, and the excludes make it moot as well as faster:
            # nothing under .llm_chat/ or .zonai/data/ is read by the suite
            # (tests use temp dirs and a fake server), and those are exactly
            # the two directories live processes write.
            #
            # `.llm_chat/` IS COPIED, and excluding it was a wrong fix that
            # broke every shard: run.py's damage guard fingerprints that
            # directory, so a copy without it saw the suite CREATE it and
            # reported the creation as damage — "already red (0 failed, 0
            # errored)", which is the guard being right about a tree I had
            # made wrong.
            #
            # rsync's exit 24 is "some source files vanished while copying",
            # which is precisely the race and is benign: the vanished file was
            # a temp nobody needs. Accepted rather than treated as failure.
            done = subprocess.run(
                ["rsync", "-a", "--exclude", ".zonai/data/",
                 ROOT + "/", copy + "/"],
                capture_output=True, text=True)
            if done.returncode == 24:
                done.returncode = 0
            if done.returncode != 0:
                # No rsync: fall back, and accept the race rather than
                # refusing to sweep at all.
                done = subprocess.run(["cp", "-R", ROOT, copy],
                                      capture_output=True, text=True)
            if done.returncode != 0:
                print("could not copy the tree to sweep it: %s"
                      % (done.stderr or "").strip()[:300])
                return 1
            copies.append(copy)
        print("sweeping %d copies under %s — the live tree is not touched,\n"
              "because other agents run bin/llm_chat out of it by absolute "
              "path.\n" % (workers, parent))
        running = []
        for shard, copy in enumerate(copies):
            env = dict(os.environ)
            env[IN_COPY] = "1"
            env[SHARD] = "%d/%d" % (shard, workers)
            # WHERE THE LIVE TREE IS. A shard runs inside a copy that this
            # function deletes on the way out, so a killer ledger written
            # beside the shard's own test/ is thrown away with it — the
            # speedup would be measured once and never seen again. The
            # ledger is the one thing a shard has to write OUTWARDS.
            env[LIVE_ROOT] = ROOT
            # -u because a long run's progress is the only sign it is alive;
            # buffered, it is indistinguishable from a hang.
            running.append(subprocess.Popen(
                [sys.executable, "-u",
                 os.path.join(copy, "test", "mutate.py")] + sys.argv[1:],
                cwd=copy, env=env))
        # Every shard is waited on before any verdict is returned. Returning
        # early would leave copies mutating files in the background and report
        # a result that had not finished being measured.
        return max(child.wait() for child in running)
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def sole_sweep():
    """Refuse to run while another sweep is running. Returns the held handle.

    Two sweeps mutate and restore the SAME files, so each sees the other's
    mutation as its own anchor being wrong. The symptom is ANCHOR MISSING on
    behaviours whose anchors are plainly present, and a run four times slower
    than it should be. Both happened here in one session, and the second time
    the false ANCHOR MISSING was read as a real defect and chased.

    NOTE, unfixed and stated rather than implied: a sweep mutates the LIVE
    tree, and other agents invoke bin/llm_chat by absolute path into it. For
    the few seconds each mutation is applied they are running a deliberately
    broken program — which is how `chan_count_placeholder is not defined`
    reached a neighbouring agent and retired its waker. Running each mutation
    in a copied tree is the real fix and is not done; it was written, never
    verified, and reverted rather than shipped unmeasured.
    """
    handle = open(LOCK, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit(
            "another mutation sweep is already running.\n"
            "  Two sweeps mutate the same files and corrupt each other's "
            "anchors — the\n  symptom is ANCHOR MISSING on anchors that are "
            "plainly there. Wait for it."
        )
    return handle

# (name, file, find, replace-with, what breaking it should mean)
MUTATIONS = [
    ("the compile runs under the SDK the host embeds", "bin/llm_chat",
     "    env = dict(os.environ, DART_SDK=sdk)",
     "    env = dict(os.environ)",
     "the workers and their .aot snapshots are built by whatever dart "
     "raindrop resolves first — which is FVM's, ahead of PATH, on any machine "
     "with ~/fvm — so a newer SDK produces a snapshot the host cannot load, "
     "Isolate.spawnUri aborts the VM in snapshot_utils.cc instead of throwing, "
     "and the server dies on the FIRST /db request with the client seeing a "
     "closed connection and no status code"),

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

    ("a publish that stranded consumers is SAID, not noted",
     "triggers/tell-the-consumers",
     "    behind = left_behind(output_of(payload))\n"
     "    if not behind or already_told(behind):\n        return 0",
     "    behind = []\n    if True:\n        return 0",
     "a release goes out, the checkouts left on the old copy are named in the "
     "output, and nothing turns that into a message — which has now happened "
     "three times across releases that fixed message LOSS, each time with me "
     "writing 'four consumers are behind' in a summary the consumers cannot "
     "read"),

    ("the consumer notice needs the report's SHAPE, not its phrase",
     "triggers/tell-the-consumers",
     "    return [(path, was, now)\n"
     "            for path, was, now in BEHIND.findall(text[found.end():])]",
     "    return [(\"?\", \"?\", \"?\")]",
     "the count line alone fires it, so grepping a publish log or reading a "
     "summary that quotes the sentence produces the nag — and a PostToolUse "
     "hook that cries wolf after every Bash call is one you stop reading "
     "within a day"),

    ("the same news is not announced twice",
     "triggers/tell-the-consumers",
     "        with open(STATE) as f:\n"
     "            if f.read().strip() == key:\n                return True",
     "        with open(STATE) as f:\n"
     "            f.read()",
     "every later publish re-announces consumers who were already told, so "
     "the notice becomes the thing you scroll past — lamp-owner's learning "
     "about a nudge printed on every run, applied to the trigger built from "
     "it"),

    ("an unreadable room list is not an EMPTY room list",
     "bin/llm-chat-deliver",
     "            unreadable.append(path)",
     "            pass",
     "a corrupt joined.json makes the agent DEAF while every sender sees "
     "delivery succeed, every room still lists them, and doctor agrees with "
     "the silence because it shares the resolver — no party in the system is "
     "positioned to notice"),

    ("the TERMINAL case is not empty either", "bin/llm-chat-deliver",
     "    if unreadable:",
     "    if False:",
     "an agent whose only joined.json is a corrupt SESSION file runs off the "
     "end of the candidate loop and gets {} — deaf by the same mechanism the "
     "fallthrough was supposed to fix, and it is the agent least likely to "
     "have anyone checking on it (wcs)"),

    ("a temp file is named after its WRITER, not its destination",
     "bin/llm_chat",
     '    return "%s.tmp.%d" % (path, os.getpid())',
     '    return path + ".tmp"',
     "two processes writing the same state file share ONE temp file, so the "
     "loser's bytes survive in a complete, valid, silently stale file — on a "
     "read-modify-write like joined.json that loses an entry outright "
     "(lamp-owner, who lost three wishes to it with every publish printing "
     "granted)"),

    ("the waker's liveness mark is a HEARTBEAT", "bin/llm-chat-wake",
     "        record_alive(_polling_server())\n"
     "        wait_for_ring(bells, HEARTBEAT_SEC)",
     "        wait_for_ring(bells, HEARTBEAT_SEC)",
     "the mark is written once before the loop, so a waker that armed and then "
     "died — killed, crashed, or wedged after the machine slept — leaves a "
     "file identical to a healthy one, and a dead waker becomes "
     "indistinguishable from a quiet room (gameloop)"),

    ("a stale heartbeat is named even though the pid is alive", "bin/llm_chat",
     "            elif beat > 3 * WAKER_HEARTBEAT_SEC:",
     "            elif False:",
     "doctor reports `polling now: yes` on the strength of a live pid while "
     "the process is stopped or blocked on a socket nobody will ring — the "
     "exact claim the heartbeat exists to stop it making"),

    ("auto-reload SAYS when it can never fire", "bin/llm_chat",
     "        here = live_here()\n"
     "        if here is not None and len(here) > 1:",
     "        here = None\n        if False:",
     "a feature that declines whenever the project holds more than one live "
     "session is switched on, looks armed, and can only fire in the "
     "configuration where the manual version was already cheap — silently, "
     "forever, which is indistinguishable from working (#17)"),

    # ANCHORED ON THE FRAGMENT THE TEST READS. The first version removed only
    # the opening string literal of a concatenated message — and the text the
    # assertion looks for lived in the NEXT fragment, so the mutation changed
    # the message and the test could not tell. It SURVIVED, which is the third
    # time today a fixture answered the same for both branches.
    ("the reload refusal offers the free option FIRST", "bin/llm_chat",
     '            "  YOU PROBABLY DO NOT NEED ONE. Hooks are read at session '
     'start, "\n'
     '            "so a NEW\\n  conversation in this same window comes up '
     'with the "\n'
     '            "rewired hooks while every\\n  session above keeps its '
     'context. "',
     '            "  "',
     "the refusal presents a binary — reload by hand or --i-know — and both "
     "end every conversation in the window, when a NEW session in the same "
     "window picks up the rewired hooks and costs nothing (#17)"),

    ("an owner cannot abandon an open room they created", "bin/llm_chat",
     '    if not ask and chan is not None and chan.get("created_by") == '
     "identity \\\n            and not chan.get(\"closed\"):",
     "    if False:",
     "the creator of a help channel walks out and the room stays OPEN — "
     "questions land there, wake nobody, and `owed` cannot see them because a "
     "room you are done with owes nothing. showrunner's lockout report sat "
     "three hours in #llm_chat_owner for exactly this reason"),

    ("an unchanged room costs no subprocess", "bin/llm-chat-deliver",
     "        if channel in quiet:\n            continue",
     "        if False:\n            continue",
     "delivery goes back to spawning one full `read` — a fresh Python "
     "interpreter and three requests — per joined room after EVERY tool call, "
     "whether or not the room has anything. With R rooms and N live sessions "
     "that is 3·R·N requests and R·N spawns per tick, which is the load the "
     "429s appear under"),

    ("a room absent from the listing is not RULED OUT",
     "bin/llm-chat-deliver",
     "        if channel in counts and marks.get(channel) == counts[channel]:",
     "        if marks.get(channel) == counts.get(channel):",
     "a room the listing never mentioned reads as quiet whenever we have no "
     "watermark for it either — None == None — so a room nobody can see gets "
     "skipped forever. The pre-check must return what is PROVABLY quiet, and "
     "absent from the answer is not proof of anything"),

    ("a failed listing rules NOTHING out", "bin/llm-chat-deliver",
     "        if done.returncode != 0:\n            return set(), {}",
     "        if False:\n            return set(), {}",
     "a `channels` call that printed nothing and FAILED is read as an empty "
     "listing, which rules every room out of nothing — but a caller that "
     "cannot see the rooms must not conclude they are quiet"),

    ("two collapses that CANCEL are one three-way", "bin/llm_chat",
     "    stub_is_mine = any(name == mine and not has for name, has in sessions)",
     "    stub_is_mine = any(not has for name, has in sessions)",
     "a human at a terminal is told THIS SESSION IS THE STUB. With no session "
     "id the two `any(...)` calls collapse in OPPOSITE directions and cancel, "
     "so the alarm they gate together cannot fire and control reaches the "
     "`elif not mine` that says the true thing. The replacement reads like the "
     "same question and removes the cancellation — the obligation is on the "
     "PAIR and every check anyone would naturally write is per-expression "
     "(auditor)"),

    ("the test and the message agree on what can be MISSING", "bin/llm_chat",
     '    owner_of_record = (recorded or {}).get("identity")',
     '    owner_of_record = recorded["identity"] if recorded else None',
     "`leave` raises KeyError on a joined.json record with no identity — the "
     "comparison reads the key with `.get`, so it admits the key may be "
     "absent, and a record without it fails that comparison and lands in the "
     "one branch that subscripts it. This program tolerates a corrupt joined "
     "file everywhere else; crashing the verb you would reach for to get out "
     "is the worst place to stop"),

    ("leaving does not clobber another identity's local record",
     "bin/llm_chat",
     "    owner_of_record = (recorded or {}).get(\"identity\")\n"
     "    if recorded is None or owner_of_record == identity:\n"
     "        forget(name)",
     "    forget(name)",
     "`leave --as owner` deletes a joined.json record that says `showrunner`, "
     "so a live server-side membership exists that the client will not use — "
     "`channels` shows you a member while every call answers 'you have not "
     "joined', and the write was made by a departing identity on behalf of "
     "one that had not departed"),

    ("leaving or deleting forgets the room", "bin/llm_chat",
     '    if joined.pop(channel, None) is None:',
     '    if True:',
     "joined.json grows forever and both hooks poll dead rooms — and after a "
     "delete they poll a room that no longer exists at all"),

    ("project root walk-up", "bin/llm_chat",
     'here = probe = os.path.abspath(os.getcwd())',
     'return os.path.abspath(os.getcwd())',
     "a subdirectory becomes a second identity for one project"),

    ("cap warning before the wall", "bin/llm_chat",
     'if seq >= cap * 0.9:',
     'if False:',
     "an agent hits the message cap mid-thought with no warning"),

    ("supersession checked before polling", "bin/llm-chat-wake",
     '        if superseded():\n            record_exit("superseded by a newer waker (healthy)")\n            return 0',
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

    ("channels --json emits JSON and nothing else", "bin/llm_chat",
     '    if as_json:\n        # The DISCOVERY surface as data.',
     '    if False:\n        # The DISCOVERY surface as data.',
     "a discovery tool asking for a machine format gets prose and goes back "
     "to parsing a rendering, which is the defect this exists to remove"),

    # `    if as_json:` appears three times in this file — owed, read and one
    # more — and the sweep mutates the FIRST match. So this spent its life
    # neutering `owed --json` while claiming to measure `read --json`, and
    # reported caught about a behaviour it never touched. The following line
    # is what makes it name its own site.
    ("read --json emits JSON and nothing else", "bin/llm_chat",
     "    if as_json:\n"
     "        # ONE RECORD PER MESSAGE, because the rendered transcript is "
     "not a\n",
     "    if False:\n"
     "        # ONE RECORD PER MESSAGE, because the rendered transcript is "
     "not a\n",
     "a consumer asking for a machine format gets prose, so it goes back to "
     "parsing a rendering — the defect this exists to remove"),

    ("a directly-wired consumer is not told to re-install", "bin/llm_chat",
     '    direct = (wired_from is not None',
     '    direct = (False and wired_from is not None',
     "doctor and the release broadcast tell the same population opposite "
     "things, so a permanently-wrong STALE line teaches everyone to skip it — "
     "and it will be right one day"),

    ("drift is measured against the tree you were WIRED FROM", "bin/llm_chat",
     '    wired_from = installed_checkout(project)',
     '    wired_from = None',
     "a vendored consumer is permanently STALE against a tree it never used, "
     "and the remedy repoints its hooks and undoes the vendoring"),

    ("the delivery hook compares the same tree", "bin/llm-chat-deliver",
     '    source = stamp.get("checkout") or ROOT',
     '    source = ROOT',
     "the same false STALE, on every session, from the hook that runs "
     "automatically rather than the command somebody chooses to run"),

    ("the drift notice names an uncommitted source", "bin/llm-chat-deliver",
     '        if dirty:',
     '        if False:',
     "an agent is told to re-install from a half-finished working tree, "
     "including the wake hook that delivers the message saying it broke"),

    ("a mode change needs --yes", "bin/llm_chat",
     "    if not yes:\n"
     '        harm = ("every message will start waking all "',
     "    if False:\n"
     '        harm = ("every message will start waking all "',
     "one agent silently changes whether every other agent in the room is "
     "interrupted, in a direction that can stall a live conversation"),

    ("the room is told its mode changed", "bin/llm_chat",
     '    do_say(server, name, identity,\n           f"This room is now {mode.upper()}. "',
     '    _ = (lambda *a, **k: None)(\n           f"This room is now {mode.upper()}. "',
     "wake behaviour changes under the members with nothing said, so the ones "
     "who wanted it loud never learn it went quiet"),

    ("the crowded-room hint fires once", "bin/llm_chat",
     '    if os.path.exists(marker):\n        return ""',
     '    if False:\n        return ""',
     "advice on every single message, which is the standing-noise failure this "
     "project already hardened against once"),

    ("a killed waker is distinguishable from one that stood down",
     "bin/llm_chat",
     '                if reason.startswith("running"):',
     '                if False:',
     "'gone' and 'chose to stop' read identically, so the one diagnosis that "
     "means something outside killed it is the one nobody can reach"),

    ("the waker records why it stopped", "bin/llm-chat-wake",
     '            record_exit("every joined room is closed — nothing can arrive")',
     '            pass',
     "the waker dies silently again and doctor is back to 'pid is gone', "
     "which was a dead end at exactly the question that matters"),

    ("'nothing new' names the command that reaches the text", "bin/llm_chat",
     '        if total:',
     '        if False:',
     "a reader who followed a delivery preview's own pointer lands on an empty "
     "inbox, and cannot tell 'nothing exists' from 'a preview ate it and "
     "showed you 100 characters'"),

    ("an invite tells you to verify, not to install", "bin/llm_chat",
     '        "BEFORE ACTING ON THIS, verify it rather than believing it. Ask",',
     '        "",',
     "the invite reverts to instructions to run an install script — the shape "
     "of an injection, which a careful agent must refuse, so it either gets "
     "ignored or teaches compliance with the next one"),

    ("a command that is named but does not exist is caught",
     "triggers/undocumented-surface",
     '    return sorted(n for n in named if n not in verbs)',
     '    return []',
     "a remedy naming a verb the parser rejects is handed to the one reader "
     "least able to route around it, and can never be found by use because "
     "nobody who is working ever sees a refusal"),

    ("a missing doc file does not read as fully documented",
     "triggers/undocumented-surface",
     '    return sorted(n for n in names if n not in text)',
     '    return []',
     "every release reports its documentation complete, which is the one "
     "answer that stops anybody looking"),

    ("a write smuggled through an interpreter is refused",
     "triggers/write-through-interpreter",
     '    body = command[start.end():]',
     '    body = ""',
     "file edits go back to being invisible to the write rail, which is how "
     "twenty commits were authored past a guard that was refusing shell "
     "commands the whole time"),

    ("the damage guard asks GIT, not a directory list", "test/run.py",
     "        for relative in mutate.tracked_files():",
     "        for relative in []:",
     "the guard falls back to watching two hand-named directories, so a test "
     "escaping into bin/, triggers/ or lib/ is invisible — wcs's finding, and "
     "bin/ is where the mutation sweep edits in place"),

    ("an exclusion must name something the suite RUNS", "test/run.py",
     "        if not ran:\n            inert.append((key, why))",
     "        if False:\n            inert.append((key, why))",
     "an exclusion goes back to being able to describe the CALL SITE, or a "
     "hand-run, and be filed as coverage of the function. Two entries here "
     "did exactly that — sweep_in_a_copy, holding the line that carries eight "
     "shards' verdicts to verify, and probe — and neither was findable by "
     "mutation, because a survivor and a line that never runs are the same "
     "green"),

    ("the span skips the def line, which runs at IMPORT", "test/run.py",
     '        spans["%s:%s" % (relative, node.name)] = (body[0].lineno,\n'
     "                                                  node.end_lineno)",
     '        spans["%s:%s" % (relative, node.name)] = (node.lineno,\n'
     "                                                  node.end_lineno)",
     "every function in an imported module reads as executed, including ones "
     "nothing ever calls, so the inert check passes on precisely the case it "
     "exists to catch — a guard that is silent exactly where it is blind, "
     "which is the shape it was written to find"),

    ("the leak detector is itself defended", "test/run.py",
     '    if not leaked:',
     '    if True:',
     "the rail that catches a test patching a shared module can break with "
     "nothing noticing — found by probing it, which is what the probe is for"),

    # A MUTATION AIMED AT THIS FILE CANNOT SPELL ITS OWN ANCHOR ON ONE LINE.
    # The first attempt used the bare `if` line and came back AMBIGUOUS with
    # two matches — the code, and this entry quoting it. Any single-line
    # anchor for mutate.py has that problem by construction. A MULTI-LINE one
    # does not: the value below holds a real newline, while the source text of
    # the entry holds the two characters `\` and `n`, so only the code
    # matches. Worth knowing before assuming the ambiguity means the anchor
    # was wrong.
    # ANCHORED ON THE `except` LINE, not on the `return` alone. Aimed at the
    # return, the replacement would be `_exit_with(stop, EXIT_REFUSED)` —
    # which is exactly what the SystemExit branch four lines down already
    # says. That is not stranding on its own: `stranded_mutations` requires
    # the anchor to be MISSING as well, and 28 mutations here share a
    # replacement with ordinary code harmlessly. It matters the day this
    # anchor goes stale for some unrelated edit, because then both halves are
    # true at once and a clean tree reports an unreverted mutation — a false
    # alarm arriving exactly when somebody is already debugging something
    # else. Naming the branch costs one line and removes the coincidence.
    ("a throttle exits DIFFERENTLY from a refusal", "bin/llm_chat",
     "    except Throttled as stop:\n"
     "        return _exit_with(stop, EXIT_THROTTLED)",
     "    except Throttled as stop:\n"
     "        return _exit_with(stop, EXIT_REFUSED)",
     "wait and stop collapse back into exit 1 at the process boundary, so a "
     "consumer has to regex this project's prose to tell them apart — which "
     "showrunner did, over a message that was reworded the same week (#30). A "
     "wrong answer is silent both ways: closures recorded that never "
     "happened, or retries refused that would have worked"),

    ("a name NOBODY TYPED is treated as inferred", "bin/llm_chat",
     '               identity_inferred=getattr(args, "identity", None) is None)',
     "               identity_inferred=False)",
     "the collision note is wired to a flag that is never true, so it can "
     "never fire — the guard is present, tested, and unreachable from "
     "production, which is a shape this repo has now found three times in a "
     "week. The check runs only on an INFERRED name because asking the host "
     "costs 230ms and the suite makes enough sends to add twelve minutes to "
     "every gate run"),

    ("a send under ANOTHER LIVE SESSION'S name is said", "bin/llm_chat",
     "    if not others:\n        return None",
     "    if True:\n        return None",
     "a message posted under an identity a different live session is holding "
     "in that very room goes out silently — which is how #198 in "
     "#llm_chat_owner came to be permanently attributed to an agent that did "
     "not write it. auditor's shell had kept its cwd in this checkout and "
     "identity resolves per calling project, so nothing was wrong except that "
     "nobody said anything"),

    ("the collision is scoped to THIS ROOM", "bin/llm_chat",
     '    here = "joined #%s" % name\n'
     "    others = [s for s in live.get(identity, [])\n"
     "              if here in (s.get(\"llm_chat_how\") or [])",
     '    here = "joined #%s" % name\n'
     "    others = [s for s in live.get(identity, [])\n"
     "              if True",
     "the note fires whenever ANY other live session holds the name, and "
     "`who` reports `owner` held by three sessions right now — lamp's, "
     "game_loop's and this one — because every repo's agent calls itself "
     "owner in its own owner-room. It would fire on nearly every send here, "
     "and a warning that always fires is furniture nobody reads"),

    ("an UNREACHABLE server is not a permanent refusal", "bin/llm_chat",
     "    if res.get(\"unreachable\"):\n        raise Unreachable(problem)",
     "    if False:\n        raise Unreachable(problem)",
     "a server nobody has started exits 1 again — the code llms.txt documents "
     "as `do not retry; the answer will not change`, and the one #30's own "
     "comment told showrunner to record as failed. A spin-down running while "
     "zonai is briefly down then records closures that never happened. This "
     "shipped that way for a day AFTER 3 and 4 landed, because the branch is "
     "commented `not retried` — which is about this program's budget inside "
     "one invocation, not about whether the caller should ever try again"),

    ("unreachable is a FLAG, not a phrase to match", "bin/llm_chat",
     '                             f"not 127.0.0.1 (zonai#16).",\n'
     '                    "unreachable": True}',
     '                             f"not 127.0.0.1 (zonai#16)."}',
     "the exit code has nothing to derive itself from, so the only way back "
     "to it is matching the error sentence — which is exactly what #30 was "
     "filed about a consumer doing, moved inside the program where it is "
     "harder to see and just as fragile"),

    ("a PARTIAL write is not a throttle", "bin/llm_chat",
     "    except Indeterminate as stop:\n"
     "        return _exit_with(stop, EXIT_INDETERMINATE)",
     "    except Indeterminate as stop:\n"
     "        return _exit_with(stop, EXIT_THROTTLED)",
     "a half-created room tells the caller to retry, and a second `open` "
     "succeeds while silently discarding the topic and briefing — so obeying "
     "the advice is how you lose them. The two states have opposite remedies "
     "and this is the line that keeps them apart"),

    ("argparse's own exit codes are not ours to rewrite", "bin/llm_chat",
     "        if isinstance(stop.code, int):\n            return stop.code",
     "        if False:\n            return stop.code",
     "`--help` starts exiting 1 and a usage error stops being 2, so the "
     "wrapper that exists to publish an exit-code contract becomes the thing "
     "that broke the one already there"),

    ("an exit code does not cost the explanation", "bin/llm_chat",
     "    if stop.code:\n        print(stop.code, file=sys.stderr)",
     "    if False:\n        print(stop.code, file=sys.stderr)",
     "catching SystemExit stops Python printing it, so every refusal goes "
     "SILENT — the caller gets a number and no reason, which trades a wrong "
     "exit code for no explanation at all"),

    ("prose on a command line is REFUSED, not preferred against",
     "bin/llm_chat",
     '    if len(args.text) > MAX_SHELL_TEXT or "\\n" in args.text:',
     "    if False:",
     "a shell gets prose again and eats the backticks out of it before this "
     "program starts, and the send reports success — seven messages in "
     "#learnings are damaged that way, by two agents, over months, one of them "
     "ours. The hazard was documented in the docstring AND the --help the "
     "whole time; documented is not enforced"),

    ("a MULTI-LINE message is prose whatever its length", "bin/llm_chat",
     '    if len(args.text) > MAX_SHELL_TEXT or "\\n" in args.text:',
     "    if len(args.text) > MAX_SHELL_TEXT:",
     "a short multi-line message goes through the shell unchecked. seq 200 — "
     "one of the seven, and ours — was three lines, so the length half alone "
     "would not have caught it"),

    ("a trigger nobody decided about is REPORTED", "test/test_wiring.py",
     "        return [name for name in shipped if name not in expected]",
     "        return []",
     "a shipped trigger that is registered nowhere and excused nowhere passes "
     "silently again. `answer-when-asked` was exactly that — documented in "
     "README as a live Stop guard refusing to end a turn with a question "
     "outstanding, registered in none of the four registries, never fired "
     "once, with tests and full coverage and a mutation that could none of "
     "them see whether anything invoked it"),

    ("reading $? after a truncator is refused", "triggers/piped-verdict",
     "    if READS_STATUS.search(command[match.end():]):\n"
     '        return ("status", match.group(0).strip())',
     "    if False:\n"
     '        return ("status", match.group(0).strip())',
     "`llm_chat owed --json | head -3; echo $?` is allowed again — this "
     "project's OWN verdict verb, missed because the guard fired on a "
     "hand-kept list its own commands were never added to. Measured against "
     "the shapes showrunner actually reported: 4 of 6 lying commands refused "
     "before this, 7 of 7 after. Reading `$?` after a truncator IS the "
     "truncator's status, and no version of that wants it"),

    ("WHICH failure it was survives the boolean", "bin/llm-chat-mcp",
     '    return "exit %d: %s\\n\\n%s" % (code, OUTCOMES.get(code, "see the payload"),\n'
     "                                  body)",
     "    return body",
     "every exit code collapses into `isError` again, so through the MCP a "
     "throttle, a permanent refusal, an unreachable server and `owed`'s COULD "
     "NOT LOOK are one value — and they call for opposite responses. A gate "
     "that cannot tell 'you owe nothing' from 'I could not look' fails open "
     "in silence, which is issue #1 in this repo"),

    ("a killer that ERRORED did not measure anything", "test/mutate.py",
     '    return counts.get("failures", 0) > 0\n',
     '    return counts.get("failures", 0) + counts.get("errors", 0) > 0\n',
     "the killer cache starts accepting a CRASH as proof that something "
     "defends the behaviour — the exact crash-versus-kill confusion this file "
     "exists to prevent, now cached and reused for every future sweep so the "
     "wrong reading never gets taken again"),

    ("every sweep worker keeps its OWN ledger", "test/mutate.py",
     '    name = "killers.%s.json" % index if index else "killers.json"\n',
     '    name = "killers.json"\n',
     "eight shards write one file and replace each other, so seven-eighths of "
     "what a sweep learned is thrown away. The next sweep runs the full suite "
     "for most mutations and the cache looks like it simply did not work — a "
     "performance bug that reads as the feature being useless"),

    ("the accounting line's terms are DISJOINT", "test/mutate.py",
     '    only_excluded = len(everything & excluded - swept)\n'
     '    both = len(everything & excluded & swept)',
     '    only_excluded = len(everything & excluded)\n'
     '    both = len(everything & excluded & swept)',
     "the published sum goes back to double-counting the 47 candidates that "
     "are both swept and excluded — it read `125 + 225 + 0 = 303` with a left "
     "side of 350, in the one tool whose entire claim is that its numbers can "
     "be trusted. Reported by an agent who did the addition by hand; nothing "
     "in the tool did"),

    ("the crash ceiling fires IN A SHARD", "test/mutate.py",
     '    if len(crashed) > CRASHED_CEILING:\n'
     '        print("\\n%d mutations CRASHED, and the ceiling is %d. A NEW "',
     '    if len(crashed) > CRASHED_CEILING and not share:\n'
     '        print("\\n%d mutations CRASHED, and the ceiling is %d. A NEW "',
     "the ceiling goes back to being unreachable. `sweep_in_a_copy` sets "
     "SHARD on all eight workers, so `not share` is false in every process "
     "that runs the sweep for real — the guard was live only in the tests, "
     "which reach it BECAUSE they do not set SHARD. It shipped that way with "
     "100% line coverage and `verify: all owed checks passed ✓` printed over "
     "a report naming a CRASHED mutation in all three of the run's sweeps"),


    ("content goes only to whoever it was addressed to",
     "bin/llm-chat-deliver",
     '    mine = [m for m in waiting if addressed_to_me(m, identity)]',
     '    mine = list(waiting)',
     "every message lands in full in every member's context — measured at 8.6x "
     "amplification and half a million tokens before this"),

    ("two workspaces do not share a doorbell directory", "bin/llm_chat",
     '    tag = hashlib.sha256(workspace_key(server).encode()).hexdigest()[:10]',
     '    tag = "shared"',
     "two servers on one machine are two workspaces, and their agents silently "
     "steal each other's sockets — one binds, the other goes deaf with no "
     "error anywhere"),

    ("one server spelled two ways is ONE workspace", "bin/llm_chat",
     '    return (server or DEFAULT_SERVER).strip().rstrip("/").lower()',
     '    return (server or DEFAULT_SERVER)',
     "a trailing slash partitions a workspace: the ringer and the listener "
     "reach the server through different paths (--server, LLM_CHAT_SERVER, "
     "the default) and will not always spell it the same, so one of them "
     "binds in a directory the other never rings"),

    ("IDENTITY IS KEYED BY SESSION, not by project", "bin/llm_chat",
     '    return os.path.join(base, "sessions", sid) if sid else base',
     "    return base",
     "two sessions in one checkout are one agent again: `identify` in either "
     "renames the other, the delivery hook hands one session's messages to "
     "the other, and the session actually asked never wakes because the "
     "cursor already advanced — measured, in one hour, twice"),

    ("the delivery hook scopes to the session it was invoked for",
     "bin/llm-chat-deliver",
     '        candidates.append(os.path.join(PROJECT, ".llm_chat", "sessions", sid,\n                                       "joined.json"))',
     "        pass",
     "a human's question is delivered to whichever session's hook fires "
     "first, which answers under the wrong name about unrelated work"),

    ("a session that never chose a name gets a UNIQUE one", "bin/llm_chat",
     '    return ("%s-%s" % (stem or "agent", sid.replace("-", "")[:8]))[:64]',
     '    return "agent"',
     "every session in a repo defaults to the same name, so they collide by "
     "CONVENTION instead of by file — worse, because it looks deliberate and "
     "the transcript gives no way to tell them apart"),

    ("the project file is a fallback, not a shared write", "bin/llm_chat",
     "    for path in (joined_path(), os.path.join(project_state_dir(),",
     "    for path in (os.path.join(project_state_dir(),",
     "a session reads the shared file even after it has its own, so leaving a "
     "room silently re-inherits it on the next read"),

    ("leaving a room CLEARS the debt", "bin/llm_chat",
     '    if member.get("done"):\n        return None',
     "    if False:\n        return None",
     "an agent is blocked forever by a conversation it correctly finished — "
     "`leave` is the documented way to say 'nothing left to add', so a debt "
     "surviving it makes the one honest exit permanently unavailable"),

    ("owed reports HAVING ANSWERED, not having read", "bin/llm_chat",
     '            and m["seq"] > last_spoke',
     "            and m[\"seq\"] > 0",
     "every message ever addressed to this agent is owed forever, including "
     "ones it already answered — a gate that fires always is turned off, and "
     "then catches nothing"),

    ("could not look is not nothing owed", "triggers/answer-when-asked",
     "    if code == 2:",
     "    if False:",
     "a failed check ends the turn as though nobody were waiting — the "
     "fail-open-in-silence shape this repo shipped once already, in the "
     "bridge, on this same escalation path"),

    ("hang_up removes only THIS room's doorbells", "bin/llm_chat",
     '        if not (name.startswith(prefix) and name.endswith(".sock")):',
     "        if False:",
     "deleting one room unlinks every doorbell on the machine, so every other "
     "agent's waker is holding a socket file senders can no longer reach — "
     "deaf, with no error anywhere"),

    ("a failed delete is loud, not partial", "bin/llm_chat",
     '    res = call(server, "DELETE", "/db", {"table": table, "where": where})\n    if "error" in res:',
     '    res = call(server, "DELETE", "/db", {"table": table, "where": where})\n    if False:',
     "a half-finished delete reports success, leaving messages and "
     "memberships belonging to a channel that is gone and that nothing here "
     "knows how to find"),

    ("a room where everyone is done closes itself", "bin/llm_chat",
     '    if members and all(m.get("done") for m in members):',
     '    if False:',
     "a room nobody is in stays open and keeps accepting messages, so a "
     "finished conversation is indistinguishable from a live one in the "
     "discovery listing"),

    ("delete refuses without --yes", "bin/llm_chat",
     "    if not yes:\n        print(f\"NOT DELETED.",
     "    if False:\n        print(f\"NOT DELETED.",
     "the only irreversible verb here runs on a bare command, destroying a "
     "transcript that is usually the only record a decision was made"),

    ("delete requires membership", "bin/llm_chat",
     "    if get_membership(server, name, identity) is None:\n"
     "        raise SystemExit(\n"
     '            f"{identity} has not joined {name}, so cannot delete it.\\n"',
     "    if False:\n"
     "        raise SystemExit(\n"
     '            f"{identity} has not joined {name}, so cannot delete it.\\n"',
     "an agent that was never in a room can destroy somebody else's "
     "conversation without ever having been party to it"),

    ("delete removes only THIS room's messages", "bin/llm_chat",
     '    remove(server, "messages", eq("channel", name))',
     '    remove(server, "messages", {})',
     "every message in every room is destroyed and the command reports "
     "success — the where-clause IS the safety property"),

    ("an answer lands in the thread that ASKED", "bin/llm-chat-slack",
     '        if thread_ts:\n            body["thread_ts"] = thread_ts',
     "        if False:\n            pass",
     "every agent reply is a new top-level message, so a human watching the "
     "thread they asked in sees silence while the answer appears elsewhere in "
     "the channel — on a phone that is indistinguishable from no answer"),

    ("two pending questions post at ROOT rather than guessing",
     "bin/llm-chat-slack",
     "    return outstanding[0] if len(outstanding) == 1 else None",
     "    return outstanding[0] if outstanding else None",
     "an answer attaches to the OLDEST outstanding question rather than the "
     "one it answers, so the human watches their newest question sit "
     "unanswered in the channel while the reply is buried in a thread they "
     "had finished with — worse than no threading, because top-level would at "
     "least have been findable"),

    ("a human can end an exchange without a reply", "bin/llm-chat-slack",
     "    if NO_REPLY_NEEDED.search(text):\n        return [\"--to-none\"]",
     "    if False:\n        return [\"--to-none\"]",
     "'No response needed' becomes a debt the stop-gate refuses to let the "
     "turn end without clearing, so the mechanism built to stop an agent "
     "going silent compels replies to messages that asked for none"),

    ("only an ADDRESSED question creates a thread debt", "bin/llm-chat-slack",
     '    if not addressing or addressing[0] != "--to" or len(addressing) < 2:',
     "    if False:",
     "an @here or a top-level message makes every later reply from every "
     "agent land in one arbitrary thread"),

    ("a STALE rewake is not evidence it landed", "bin/llm-chat-wake",
     "    if time.time() - float(pending.get(\"at\") or 0) > REWAKE_GRACE:\n        return",
     "    if False:\n        return",
     "a turn beginning an hour after the rewake was requested is recorded as "
     "proof the host wakes, restoring the false green this marker exists to "
     "remove — doctor would say 'listening' on a host that never wakes"),

    ("THREAD REPLIES REACH THE ROOM", "bin/llm-chat-slack",
     "    return relayed + pump_threads(config, slack, messages, threads, "
     "members)",
     "    return relayed",
     "the documented PRIMARY reply path is dead: conversations.history does "
     "not return thread replies, so the one gesture a human on a phone is "
     "told to use is the one that cannot arrive — and top-level wakes nobody, "
     "so the agent that asked waits forever"),

    ("a thread reply is relayed ONCE", "bin/llm-chat-slack",
     '            if not at or at == ts or float(at) <= float(last):',
     "            if not at or at == ts or False:",
     "every reply in a thread is re-relayed on every poll, so one answer from "
     "a human becomes an unbounded stream into the room"),

    ("a blind parent is rate-limited but a VISIBLE one is not",
     "bin/llm-chat-slack",
     "        if hint is not None:\n            if hint == 0 or (seen.get(ts) or {}).get(\"count\") == hint:",
     "        if False:\n            if hint == 0 or (seen.get(ts) or {}).get(\"count\") == hint:",
     "an in-window thread whose reply_count history ALREADY reported as grown "
     "waits out the blind-poll leash before being fetched, delaying a reply "
     "the bridge can see by up to RECHECK_SEC for no reason"),

    ("threads are enumerated from the MAP, not the history window",
     "bin/llm-chat-slack",
     "    for ts in watched_parents(threads, seen, hints):",
     "    for ts in watched_parents(hints, seen, hints):",
     "a parent older than the cursor is never asked about again, so ONE "
     "unrelated top-level message permanently deafens every existing thread — "
     "worse than no threading, because it works until the next message and "
     "reads as intermittent rather than absent"),

    ("a failed read is not an empty room", "bin/llm-chat-slack",
     "    if done.returncode != 0:\n        return None",
     "    if False:\n        return None",
     "'I could not look' becomes 'nobody has said anything' — a bridge whose "
     "agent->Slack half is dead prints nothing and relays nothing, measured "
     "on a live setup for an entire session"),

    ("--check exercises the llm_chat read too", "bin/llm-chat-slack",
     '    if waiting_for_human(config["room"], config["identity"]) is None:',
     "    if False:",
     "the one command whose purpose is 'is the wiring live?' passes with half "
     "the wiring dead, which is the worst possible false green on an "
     "escalation path"),

    ("a bridge stops when its room is deleted", "bin/llm-chat-slack",
     "        if room_is_gone(config):",
     "        if False:",
     "the bridge relays an empty room to Slack forever, holding a token and "
     "paying a request per poll for a conversation that no longer exists"),

    ("a bridge does NOT stop when it merely cannot tell", "bin/llm-chat-slack",
     '    if done.returncode != 0:\n        print("  (could not list rooms: %s)"',
     '    if False:\n        print("  (could not list rooms: %s)"',
     "a brief server outage reads as deletion and tears down the human's "
     "escalation path, taking its cursor and thread map with it"),

    ("a doorbell is keyed by MEMBERSHIP, not identity", "bin/llm_chat",
     '    return "%s__%s.sock" % (channel, identity)',
     '    return "%s.sock" % identity',
     "four projects here answer to `owner`, so one waker binds the socket and "
     "the rest silently do not — then hear nothing, which looks like a quiet "
     "room rather than a fault"),

    ("a message rings the doorbells it wakes", "bin/llm_chat",
     '                 and ring(name, m, server)]',
     '                 and False]',
     "the poll is gone and nothing replaced it, so an idle agent is never "
     "signalled and only hears anything when it happens to start a new waker"),

    ("you may speak only as yourself", "bin/llm_chat",
     '    if not known or identity in known:\n        return',
     '    if True:\n        return',
     "a message lands in a shared transcript under another agent's name, "
     "unattributable to every reader and impossible to edit afterwards"),

    # THE PROBE IS THE GUARD, not the `return None` that follows it. The first
    # version of this mutation deleted that return — and the sweep reported it
    # SURVIVED, correctly: without it the code simply falls through to
    # `bell.bind(path)`, which fails with EADDRINUSE on a live socket and
    # returns None anyway. Same answer by a different road, so no test could
    # ever have told the difference. An equivalent mutant, dressed as a gap.
    #
    # What would actually steal a healthy doorbell is unlinking without
    # asking, so that is what this reverts to.
    ("a healthy doorbell is never stolen", "bin/llm-chat-wake",
     "        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
     "        probe.settimeout(1)\n"
     "        try:\n"
     "            probe.connect(path)\n"
     "            probe.close()\n"
     "            return None          # somebody healthy already holds it\n"
     "        except OSError:\n"
     "            probe.close()\n"
     "            try:\n"
     "                os.unlink(path)  # stale; its owner is gone\n"
     "            except OSError:\n"
     "                return None",
     "        try:\n"
     "            os.unlink(path)\n"
     "        except OSError:\n"
     "            return None",
     "a second waker unlinks a LIVE listener's socket and binds its own, so "
     "the first agent goes deaf holding a socket nobody can reach while the "
     "second believes it is covering the room"),

    ("a failing CLI never reads as 'every room is closed'", "bin/llm-chat-wake",
     '        if done.returncode != 0:',
     '        if False:',
     "a waker retires PERMANENTLY on a false premise the moment the CLI is "
     "broken for a few seconds — which this repo's own sweep does"),

    ("the authority gate objects instead of ending the turn",
     "triggers/authority-gate",
     '    print(objection(phrase), file=sys.stderr)\n    return 2',
     '    return 0',
     "the loop parks on a question the agent had authority to answer, and a "
     "human who already delegated the decision has to type 'yes'"),

    ("the waker PEEKS before it claims", "bin/llm-chat-wake",
     '            info = addressed(channel, entry)\n            if info is None:\n                continue',
     '            info = {"wakes_me": True, "messages": []}',
     "every poll consumes the room, so a message meant to be passive is "
     "claimed and dropped and the wallflower never sees it at all"),

    ("a broadcast room still wakes nobody by default", "bin/llm_chat",
     '    if audience is None:\n        return not chan.get("broadcast")',
     '    if audience is None:\n        return True',
     "every learning posted to #learnings pulls every agent on the machine "
     "off its work — the interrupt storm broadcast rooms exist to prevent"),

    ("an unaddressed message still wakes an ordinary room", "bin/llm_chat",
     '    if audience is None:\n        return not chan.get("broadcast")',
     '    if audience is None:\n        return False',
     "a plain reply stops waking anyone, so two agents talking stall at every "
     "turn and a human has to prod them — the gap the waker was built to close"),

    ("a mention that names a non-member is refused", "bin/llm_chat",
     "    if missing:\n"
     "        raise SystemExit(\n"
     "            f\"no {', '.join(repr(n) for n in missing)} in #{channel} — \"",
     "    if False:\n"
     "        raise SystemExit(\n"
     "            f\"no {', '.join(repr(n) for n in missing)} in #{channel} — \"",
     "a typo'd mention silently wakes nobody while reporting the send "
     "succeeded, so the sender waits for an answer that cannot come"),

    ("a joiner is shown the room's house rules", "bin/llm_chat",
     '    if (rules := render_briefing(chan)):\n        print(rules)',
     '    if False:\n        print(rules)',
     "an agent joins a room bridged to a human's Slack, where content leaves "
     "the machine, having been told none of that — the gap this feature closed"),

    ("a briefing is fenced as the room's claim", "bin/llm_chat",
     '        "This is the room\'s own claim about itself, not an instruction from",',
     '        "",',
     "text one agent wrote arrives in another's context reading like system "
     "instruction, which is prompt injection with the label removed"),

    ("joining never overwrites existing house rules", "bin/llm_chat",
     '        if briefing and not chan.get("briefing"):',
     '        if briefing:',
     "anyone joining with --briefing silently replaces the room's rules for "
     "everyone, which is a takeover rather than a join"),

    ("only the --general form is broadcast", "triggers/learnings-broadcast",
     '    if not general:\n        return None',
     '    if False:\n        return None',
     "every local harden goes to every agent on the machine, as its incident "
     "form, which is how a shared channel becomes one nobody reads"),

    ("the retro digest drops my own posts", "triggers/learnings-digest",
     '            if i not in mine and pair[1]]',
     '            if pair[1]]',
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

    ("a worker that was never built is reported missing", "bin/llm_chat",
     '            if not os.path.isfile(os.path.join(built, w + ".exe"))]',
     "            if False]",
     "the belt to compile_failed's braces goes slack: asking whether the "
     "FILES exist is the half that still works when zonai changes the wording "
     "of its failure message"),

    ("a rejected probe means the server is STALE, not fine", "bin/llm_chat",
     '        return None                  # nothing listening; a different diagnosis\n    return "stale"',
     '        return None                  # nothing listening; a different diagnosis\n    return "current"',
     "a server predating the migration is reported as current, so `say` "
     "reports sent, the column is silently dropped, and the field comes back "
     "null — two agents lost hours to that in one day and doctor said the "
     "wiring looked right throughout"),

    ("a compile that SAYS it failed is a failure", "bin/llm_chat",
     "    return any(marker in output for marker in COMPILE_FAILURE)",
     "    return False",
     "zonai prints 'Failed to compile rules:' and exits 0, so the step that "
     "reports a build error is the one step nothing reads — the exit code is "
     "checked and the message that contradicts it is thrown away"),

    ("a compile that produces nothing refuses to serve", "bin/llm_chat",
     "    missing = missing_workers()",
     "    missing = []",
     "`zonai compile` exits 0 while printing that it failed, so the bootstrap "
     "starts a server with no rules worker — it accepts connections and 500s "
     "every /db request, which presents as a wire bug rather than a build one"),

    ("a verdict read through a pager is refused", "triggers/piped-verdict",
     "    if MERGES_STDERR.search(with_pager):",
     "    if False:",
     "`cmd 2>&1 | tail -N` reports TAIL's exit status and discards the region "
     "the traceback is in — a failing suite was read as passing and reported "
     "to a human as a clean gate, and a publish failure became unreproducible "
     "because the command asking for the reason threw it away"),

    ("a heredoc BODY ends at its delimiter", "triggers/piped-verdict",
     '        end = re.search(r"^\\s*%s\\s*$" % re.escape(found.group(1)),\n'
     "                        after, re.M)",
     "        end = None",
     "every heredoc is treated as unterminated, so everything after the "
     "opener is discarded and a real offence written AFTER a commit message "
     "goes unseen — the guard reads as silent rather than as broken"),

    ("prose in a heredoc is not a command", "triggers/piped-verdict",
     "    command = strip_heredocs(command)",
     "    command = command",
     "a commit message DESCRIBING the offence is refused as the offence — "
     "this guard blocked its own commit message, and a guard that cannot be "
     "described in a commit message is one whose reasons never get written "
     "down"),

    ("a verdict NAMED in argument position is not being run",
     "triggers/piped-verdict",
     "    elif i < len(tokens) and SUBCOMMAND.match(tokens[i]):",
     "    elif i < len(tokens):",
     "`grep -n x test/mutate.py | head` and `cat test/run.py | head` are "
     "refused because the filename sits in argument position — the false "
     "alarm that gets a guard switched off within the hour, and the same "
     "mistake this repo already corrected once in the remedy counter"),

    ("a server that cannot be reached is NOT an hour of silence",
     "bin/llm_chat",
     "    except SystemExit:\n"
     "        return None                 # could not look; NOT \"nothing "
     "happened\"",
     "    except SystemExit:\n        pass",
     "an unreachable server reads as perfect quiet, so deferred work runs in "
     "the middle of a busy afternoon — absence reported as a clean bill of "
     "health, which is the inversion this repo has spent a day removing "
     "everywhere else"),

    ("a vacuum that freed nothing is not a success", "bin/llm_chat",
     "    if after > expected * 2 and after > 8_000_000:",
     "    if False:",
     "VACUUM in WAL mode rebuilds into the log and the main file only shrinks "
     "when a checkpoint truncates it — so with a server holding the database "
     "open it returns cleanly, frees zero bytes, and the task is marked done. "
     "Measured on the real thing: 407 pages of content, 1.6MB, inside an "
     "853MB file, reported as 'reclaimed 0.0 MB' and finished"),

    ("the checkpoint is what actually truncates the file", "bin/llm_chat",
     '            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")',
     "            pass",
     "the VACUUM alone leaves every freed byte on disk in WAL mode, which is "
     "the mode this database runs in — the whole job silently does nothing"),

    ("the quiet number says what it could NOT see", "bin/llm_chat",
     "    if not os.path.isfile(mark):",
     "    if False:",
     "an absent PostToolUse mark — 'no agent is wired into this project' — is "
     "printed as though tool activity had been counted and found absent, so a "
     "number derived from one signal reads as covering both"),

    # NOT `if False:`, which was this mutation until #22. The guard both
    # carries the message AND protects the unpack two lines below, so
    # removing it raised TypeError before any assertion ran — the behaviour
    # was never measured, only the crash. Granting the permission instead
    # states the same consequence and leaves the program able to run.
    ("CANNOT TELL is not permission to run", "bin/llm_chat",
     "        if quiet is None:\n"
     "            # CANNOT TELL is not permission. A server that cannot be "
     "reached",
     "        if quiet is None:\n"
     "            quiet = (QUIET_SECONDS, 'assumed')\n"
     "        if False:\n"
     "            # CANNOT TELL is not permission. A server that cannot be "
     "reached",
     "the run path treats 'could not measure the silence' as 'the silence is "
     "long enough' and starts a database rewrite while agents are working"),

    ("an agent running a tool counts as activity", "bin/llm_chat",
     "    if at and (newest is None or at > newest[0]):\n"
     '        newest = (at, "an agent running a tool")',
     "    if False:\n"
     '        newest = (at, "an agent running a tool")',
     "'no messages' is taken for 'nobody working', so a database rewrite "
     "starts under an agent an hour deep in a silent task — the exact "
     "interruption the queue exists to avoid"),

    ("the queue holds NAMES, never commands", "bin/llm_chat",
     "        if task not in MAINTENANCE:",
     "        if False:",
     "any string can be queued for unattended execution; this runs on a "
     "loopback server with no authentication where any agent in any room can "
     "write the queue file, so 'persuade an agent to write a file' becomes "
     "arbitrary code execution"),

    ("the registry is checked again at RUN time", "bin/llm_chat",
     "        if not known:",
     "        if False:",
     "the queue file is on disk and anything that can write to the project "
     "can edit it, so checking only at queue time leaves the gate open at the "
     "one moment that matters — the moment of execution"),

    ("a failed task is NOT marked done", "bin/llm_chat",
     "        if ok:\n            entry[\"done\"] = True",
     '        entry["done"] = True',
     "a vacuum refused because the server holds the database open is recorded "
     "as finished, so the work never happens and the queue reports success"),

    # The REPLACEMENT has to be a string that could not occur naturally.
    # The first version of this mutation replaced the line with `        "",`
    # — which appears all over the file — so the stranded-mutation detector,
    # whose test is `find absent and replace present`, reported a stranding
    # every time and refused to run the suite at all. A mutation's replace
    # text is a MARKER as well as a change.
    ("the answer gate offers an exit that wakes nobody",
     "triggers/answer-when-asked",
     # KEEPS THE %s ON PURPOSE. The first version of this mutation dropped it,
     # so the line below it fed an argument to a format string that no longer
     # took one and the trigger raised TypeError — eight tests died and not
     # one of them disagreed with anything, which is #22's whole complaint. A
     # mutation has to break the BEHAVIOUR and leave the program able to run,
     # or it measures nothing.
     '        "    llm_chat say %s \\"done: <what you found>\\" --to-none"',
     '        "    llm_chat leave %s"',
     "the gate demands an answer, the etiquette forbids trivial ones because "
     "every message wakes the room, and the only remedy left on offer is "
     "`leave` — which stands down a headless agent's waker and made a "
     "Crawler's verdict recoverable only from its transcript (#15)"),

    # AIMED AT THE COMPREHENSION, not at an early return. The first version
    # reverted `if not session: return []`, which SURVIVED — rightly, because
    # `room in session` already covers it and the two were one rule spelled
    # twice. The redundant spelling is gone; this reverts the one that acts.
    ("a project store NOTHING shadows is left alone", "bin/llm_chat",
     "            if room in session\n"
     '            and entry.get("identity") != session[room].get("identity")]',
     "            if True]",
     "an agent with no session store is told its own live membership is a "
     "stale relic, and `sync --repair` would delete the only record it has — "
     "measured: one of seven checkouts on this machine runs entirely on the "
     "project file (#16)"),

    ("a shadowed project entry is REPORTED", "bin/llm_chat",
     "    shadowed = shadowed_project_rooms(project)\n    if shadowed:",
     "    shadowed = []\n    if False:",
     "an entry naming an identity that left the room sits in a file nothing "
     "writes and nothing mentions, inert only because today's control flow "
     "never reaches it — and any future fallback hands that agent a departed "
     "identity as its membership"),

    ("a transient 429 does not block a turn-end",
     "triggers/answer-when-asked",
     "    while (code == 2 and tried < RETRIES\n"
     "           and all_throttled(payload.get(\"unreachable\"))):",
     "    while False:",
     "a rate limit that clears in seconds ends the turn instead, and the only "
     "exit on offer is the bypass — so typing it becomes the cheapest way to "
     "clear a transient, and an agent that types it for a transient will type "
     "it for a real outage (#18)"),

    ("an outage is NOT retried", "triggers/answer-when-asked",
     "    return bool(rooms) and all(r.get(\"rate_limited\") for r in rooms)",
     "    return True",
     "a refused connection is waited on three times before the gate reports "
     "it — waiting does not start a server, and an empty unreachable list "
     "(which `all()` calls true) would retry a failure that named no rooms"),

    # ONE MUTATION ON THIS LINE, not two. The raise moved out of `rows` into
    # `refuse` when the writes were routed through it, so this anchor went
    # stale and reported SURVIVED — which reads as an undefended behaviour
    # rather than as a broken measurement. A second mutation written against
    # the new location would then have shared its anchor, which is the
    # ambiguity the sweep refuses. Merged instead, with both reasons.
    ("the rate-limit KIND survives to the caller", "bin/llm_chat",
     "    raise (Throttled if res.get(\"rate_limited\") else SystemExit)"
     "(problem)",
     "    raise SystemExit(problem)",
     "the flag `call` already determined is thrown away one line later, so a "
     "transient and an outage reach the turn-end gate as the same thing with "
     "only prose to tell them apart — which is why #18 blocked on a 429 that "
     "cleared twenty seconds later. And it is now the ONLY raiser: `rows` "
     "kept the distinction while `create`, `update` and `remove` lost it, "
     "which is exactly backwards, because the limiter appears WRITE-scoped "
     "and the throttled operations were the ones handing back an "
     "indistinguishable error"),

    ("a 429 is retried instead of handed to the caller", "bin/llm_chat",
     "            if e.code == 429 and wait is not None:",
     "            if False:",
     "the exit from a rate-limited state is itself rate limited, so the state "
     "is absorbing: `leave` — the documented remedy — returns 429 too, and "
     "the only way out is the override, which typed routinely stops being "
     "read (#15)"),

    ("the retry gives up rather than hanging", "bin/llm_chat",
     "                time.sleep(min(offered, wait) if offered else wait)",
     "                time.sleep(offered or wait)",
     "a Retry-After of 60 is honoured literally inside a Stop hook, so a "
     "throttle becomes a hang — worse than the error it replaces, and "
     "invisible because the hook simply never returns"),

    ("owed costs the same whatever the room count", "bin/llm_chat",
     "    if store is not None:\n"
     "        chan = store.channel(name)\n"
     "        member = store.membership(name, identity) if chan else None",
     "    if False:\n"
     "        chan = store.channel(name)\n"
     "        member = store.membership(name, identity) if chan else None",
     "every room costs three requests again, so an orchestrator holding eight "
     "spends twenty-four on one turn-end check and rate-limits the server it "
     "is gating on — and the agent in the most rooms is by construction the "
     "one coordinating everybody (#14)"),

    ("a reload refuses when a window holds MORE THAN ONE session",
     "bin/llm_chat",
     "    if here is not None and len(here) > 1 and not i_know:",
     "    if False:",
     "a reload takes the whole WINDOW, so a second conversation in it loses "
     "whatever turn was in flight — the title guard identifies a window and "
     "cannot see inside it, and one-session-per-repo is exactly the setup "
     "where this goes unnoticed until it does not"),

    # Anchor moved when the spawn was lifted out of `wake` into main() — it
    # was forking a real detached process every time a test asserted the
    # exit-2 contract. A stale anchor is reported as ANCHOR MISSING rather
    # than passing quietly, which is the only reason this was noticed.
    ("a missed wake is NOTICED at all", "bin/llm-chat-wake",
     "    note_rewake()\n    watch_for_a_missed_wake()\n    wake(blocks)",
     "    note_rewake()\n    wake(blocks)",
     "nothing outlives the exit, so a wake the harness ignores is never seen "
     "by anything: no turn means no Stop, no Stop means no waker, no waker "
     "means nobody looks — the circularity that lets an idle session go deaf "
     "in silence"),

    ("reloading on a missed wake is OPT-IN", "bin/llm-chat-wake",
     "    if os.path.isfile(AUTO_RELOAD_PATH):",
     "    if True:",
     "every project gets its window reloaded by a chat tool that decided a "
     "message was late — UI automation ending whatever turn was in flight, "
     "on machines whose owner never asked for it"),

    ("a SESSION START is not a wake landing", "bin/llm-chat-wake",
     '    if event != "Stop":',
     "    if False:",
     "a window reload inside the grace window writes a receipt for a wake "
     "that never happened — and a reload is exactly what tends to happen near "
     "an unanswered wake, so doctor reported the path healthy for 90 minutes "
     "while every wake failed"),

    ("what is WAITING beats what landed once", "bin/llm_chat",
     "                stuck = waiting_longer_than_the_last_wake(server, "
     "project,\n                                                          "
     "landed)",
     "                stuck = None",
     "doctor reports 'a wake landed' and stops, so a message queued right now "
     "with no wake behind it is invisible — the agent reading it told a human "
     "twice that the mechanism worked"),

    ("a message OLDER than the landing is not evidence", "bin/llm_chat",
     "            if landed_at and at <= landed_at:\n                continue",
     "            if False:\n                continue",
     "every unread message counts against the wake path, including ones the "
     "recorded wake may well have delivered — the check cries wolf on a "
     "healthy path, which is how a diagnostic gets ignored and then the real "
     "failure goes unread with it"),

    ("a landing marker of unknown provenance is not confirmed", "bin/llm_chat",
     "    return None if event is None else event == \"Stop\"",
     "    return True",
     "every marker written before this distinction existed reads as a "
     "confirmed turn, which is every marker anybody already has — the bug "
     "preserved for exactly the people upgrading to the fix for it"),

    # THE ANCHOR CARRIES ITS NEXT LINE, and that is not decoration.
    # `    if done.returncode != 0:` appears THREE times in this file, and the
    # sweep replaces the first match — so this mutation spent its life
    # neutering `read`'s check instead of `say`'s, was defended by read's
    # tests, and reported `caught` about a behaviour it never touched. It
    # showed up as a SURVIVOR the moment the sweep started running tests,
    # which is the only reason anybody looked.
    ("say checks the exit code", "bin/llm-chat-slack",
     "    if done.returncode != 0:\n"
     "        why = (done.stderr or done.stdout or \"\").strip().splitlines()",
     "    if False:\n"
     "        why = (done.stderr or done.stdout or \"\").strip().splitlines()",
     "a relay refused by the CLI — a closed room, the message cap, a server "
     "that went away — reports success, the caller counts it, and the cursor "
     "moves past a message that never left. subprocess.run does not raise on "
     "a non-zero exit, so the count going UP is why nobody saw it"),

    ("a failed relay does not move the mark", "bin/llm-chat-slack",
     "                    stuck = True\n                    break",
     "                    pass",
     "the high-water mark advances past a thread reply that never arrived, so "
     "it is never looked at again — on the one channel where the sender is a "
     "person, waiting for an answer that was silently dropped"),

    ("an owed retry is not a CHECKED thread", "bin/llm-chat-slack",
     '            seen[ts] = {"count": (seen.get(ts) or {}).get("count"),\n'
     '                        "seen_ts": newest, "checked_at": 0}',
     '            seen[ts] = {"count": count if count is not None else '
     'replied,\n                        "seen_ts": newest, "checked_at": '
     "now()}",
     "a thread holding a failed relay records the new count and a fresh "
     "timestamp, so both skips in watched_parents fire and the retry never "
     "happens — the mark was held back correctly and nothing ever went back "
     "to look"),

    ("a name in a THREAD REPLY is honoured too", "bin/llm-chat-slack",
     "                addressing = route(reply, threads, members)",
     "                addressing = route(reply, threads)",
     "`@build fix this` typed in a thread wakes the thread's owner instead of "
     "build — the name-tagging path never reached the reply route, so the "
     "feature worked in the channel and silently did not in threads"),

    ("a name beats @here", "bin/llm-chat-slack",
     "    named = addressed_names(text, members)\n"
     "    if named:\n"
     '        return ["--to", ",".join(named)]',
     "    named = []",
     "'@baccompat do something. @here' wakes every agent on the machine to "
     "deliver one instruction to one of them — the human named somebody "
     "precisely to avoid that, and reached for @here only because it was the "
     "one gesture that reliably woke anybody"),

    ("a name MENTIONED is not a name ADDRESSED", "bin/llm-chat-slack",
     "        if hit.group(0).lstrip().startswith(\"@\") or "
     "is_vocative(text, hit):",
     "        if True:",
     "'I think the build is stuck' wakes build, so every status report "
     "becomes an interrupt — the over-delivery the audience rules exist to "
     "stop, arriving through the feature meant to narrow them"),

    ("a name inside a longer word is not a name", "bin/llm-chat-slack",
     '    return re.compile(r"(?<![A-Za-z0-9])@?" + r"[\\s._-]*".join(parts)\n'
     '                      + r"(?![A-Za-z0-9])", re.IGNORECASE)',
     '    return re.compile(r"[\\s._-]*".join(parts), re.IGNORECASE)',
     "'rebuilding the buildings' wakes the agent called build — the "
     "substring implementation that passes every other test in the file"),

    ("a bridge command is answered, not relayed", "bin/llm-chat-slack",
     "            if verb and answer_bridge_command(",
     "            if False and answer_bridge_command(",
     "'@llm_chat list' is posted into the room, so asking who is in it wakes "
     "everybody in it — the exact cost the command exists to avoid"),

    ("an unknown bridge verb is not swallowed", "bin/llm-chat-slack",
     '    if verb != "list":\n        return False',
     '    if verb != "list":\n        return True',
     "'@llm_chat lsit' vanishes into the bridge instead of reaching the room "
     "— a typo becomes silence, which is indistinguishable from the bridge "
     "being down"),

    # THE WORDING, not the branch. `if True:` sent the empty case down the
    # has-members path and it died on `members[0]` — an IndexError where the
    # assertion about what the human is TOLD should have been. The fix this
    # reverts was always the sentence, so the sentence is what to revert.
    ("a failed member lookup is not an empty room", "bin/llm-chat-slack",
     '        body = ("Could not read the members of *%s* just now — that is "\n'
     '                "\'could not look\', not \'nobody is there\'." % room)',
     '        body = ("*%s* has no members." % room)',
     "'could not look' is reported as 'nobody is there', sending a human off "
     "to debug an empty room that is full — the distinction this project has "
     "now made in four separate files"),

    ("the text is out before the cursor moves", "bin/llm_chat",
     "        return _render(server, name, identity, fetched, all_messages, "
     "as_json,\n                       commit_cursor)",
     "        commit_cursor()\n"
     "        return _render(server, name, identity, fetched, all_messages, "
     "as_json,\n                       lambda: None)",
     "the cursor advances before the message is printed, so a read that "
     "reached the server and then lost its output — an 8-second subprocess "
     "timeout, a killed child — marks the message read and delivers it to "
     "nobody; reported as pending 0 beside owed seq 41, two facts that cannot "
     "both be true"),

    ("a successful read STILL advances the cursor", "bin/llm_chat",
     "    commit_cursor()\n    return waiting",
     "    return waiting",
     "leaving the cursor alone on failure becomes leaving it alone at all — "
     "every message is redelivered on every read, forever, which is the "
     "opposite failure and just as useless"),

    ("a new waker does not bury why the last one stopped",
     "bin/llm-chat-wake",
     "        history = read_exits() + [record]",
     "        history = [record]",
     "the waker that starts AFTER a failure overwrites the record of the one "
     "that failed — reported with the file reading pid 503, a waker armed "
     "during the reload that came after the missed message, while the reason "
     "anybody wanted was gone"),

    # ANCHORED WITH ITS COMMENT, because the heartbeat added a SECOND
    # `record_alive(_polling_server())` inside the loop and the four-space
    # form is a substring of the eight-space one. The sweep's ambiguity check
    # caught it the moment it appeared, which is the check doing its job on
    # the same day it was added.
    ("a live waker is recorded where it destroys nothing",
     "bin/llm-chat-wake",
     "    # the record of the dead one it replaced — see record_exit, and "
     "#11.\n    record_alive(_polling_server())",
     "    # the record of the dead one it replaced — see record_exit, and "
     '#11.\n    record_exit("running")',
     "`running` goes back into the exit history, where it is not an exit and "
     "displaces the real one — this is the exact write that produced the "
     "reported file, so the history would be kept and then immediately "
     "overwritten by the next waker to start"),

    ("an identity split is not reported as an empty project",
     "bin/llm-chat-wake",
     "        elsewhere = sessions_holding_rooms()",
     "        elsewhere = []",
     "a waker armed under a reload's new session id stands down saying "
     "'nothing to listen for', which is true and useless — the agent goes "
     "permanently deaf while the rooms sit under the previous id, and the "
     "symptom is indistinguishable from the host ignoring asyncRewake"),

    ("a stub is told apart from a member by its joined.json", "bin/llm_chat",
     '            found.append((name, os.path.isfile(os.path.join(path,\n'
     '                                                            "joined.json"))))',
     "            found.append((name, True))",
     "every session directory reads as holding rooms, so the stub a window "
     "reload leaves behind — a read.lock and nothing else — is reported as a "
     "member and the split it causes stays invisible"),

    ("the ONE-RECORD exit format is still readable", "bin/llm_chat",
     "    if isinstance(found, dict):\n        found = [found]",
     "    if isinstance(found, dict):\n        found = []",
     "the record an already-installed waker wrote is discarded at upgrade "
     "time — which is the moment somebody is interrogating it, so the fix for "
     "losing exit records would lose one on its way in"),

    ("a waker does not report an identity split with ITSELF",
     "bin/llm-chat-wake",
     "        if name == _SID:\n            continue",
     "        if False:\n            continue",
     "a session's own EMPTY joined.json counts as somebody holding rooms, so "
     "every ordinary empty session accuses itself of a split — the false "
     "alarm that gets a diagnostic ignored"),

    ("the record a handover buried is surfaced", "bin/llm_chat",
     "    if not records[-1][\"reason\"].startswith(SUPERSEDING):\n"
     "        return None",
     "    if True:\n        return None",
     "doctor shows only the supersede sitting on top — the healthy handover — "
     "and stays silent about the stop underneath it, which is the answer to "
     "the question being asked"),

    ("one corrupt lock does not hide every other window", "bin/llm_chat",
     "                found = json.load(f)\n"
     "        except (OSError, ValueError):\n            continue",
     "                found = json.load(f)\n"
     "        except (OSError, ValueError):\n            return None",
     "a single unreadable ~/.claude/ide lock file hides every window after it "
     "in the listing, so the address is reported as absent for projects that "
     "have one — and absent is the answer that reads as 'not a VSCode agent'"),

    # TWO CALLERS ASK THE HOST THE SAME WAY, so each anchor carries its own
    # next line. Without that they matched two places and the sweep refused
    # both rather than guess — which is the right refusal and a useless
    # measurement. `live_identities` arrived second and made the older anchor
    # ambiguous; a mutation that cannot be applied defends nothing.
    ("nobody home is not the same as could not ask", "bin/llm_chat",
     "    sessions = host_sessions()\n    if sessions is None:\n"
     "        return None\n"
     "    root = os.path.abspath(project or project_dir())",
     "    sessions = host_sessions() or []\n"
     "    root = os.path.abspath(project or project_dir())",
     "a host that cannot be asked reports as 'no live session in this "
     "project', so a missed wake reads as an agent that simply went home — "
     "the two states this check exists to separate, collapsed by the one line "
     "that separates them"),

    ("an unaskable host does not empty the identity mapping", "bin/llm_chat",
     "    sessions = host_sessions()\n    if sessions is None:\n"
     "        return None\n    live = {}",
     "    sessions = host_sessions() or []\n    live = {}",
     "`who` answers 'nobody is live' to a question that was never asked, and "
     "`say --to` reports LEFT FOR every member on any machine where the host "
     "cannot be reached — the same inversion at both ends of the mapping"),

    ("a sibling directory is not this project", "bin/llm_chat",
     "        if cwd == root or cwd.startswith(root + os.sep):",
     "        if cwd.startswith(root):",
     "`/x/llm_chat_old` matches `/x/llm_chat`, so doctor reports a live agent "
     "in a project nobody is working in and a missed wake looks like somebody "
     "sitting there deaf"),

    ("the host disagreeing about who you are is named", "bin/llm_chat",
     '    if mine and not any((s.get("sessionId") or "") == mine '
     "for s in here):",
     "    if False:",
     "the environment and the host can disagree about this session's id after "
     "a window reload, and issue #12 is that nothing noticed — this is the "
     "one source that belongs to the host rather than to us, reporting "
     "agreement it never checked"),

    ("a stub session is named rather than reported healthy", "bin/llm_chat",
     "    if stub_is_mine and others_hold:",
     "    if False:",
     "a session whose rooms stayed behind after a window reload reads as "
     "healthy — every other check reports at PROJECT level, so the wiring is "
     "right and the rooms exist while this session's waker looks at nothing"),

    ("prose the shell will RUN is refused before it ships",
     "triggers/prose-through-shell",
     "        if LIVE_BACKTICK.search(text):",
     "        if False:",
     "a backticked word inside a double-quoted --comment is executed by the "
     "shell and its empty output pasted in — a public comment closing issue "
     "#10 posted with the word missing, and gh answered ok, because the "
     "substitution that ate it fails quietly by design"),

    ("single quotes are read BEFORE double quotes",
     "triggers/prose-through-shell",
     '        elif char == "\'":\n'
     '            close = code.find("\'", i + 1)\n'
     "            i = n if close < 0 else close + 1",
     '        elif char == "\'":\n'
     "            i += 1",
     "a double quote inside '...' is treated as an opener, so the scanner "
     "pairs it with one far away and reports a quoted region that does not "
     "exist — the guard starts refusing commands that are fine, which is how "
     "a rail gets switched off within the hour"),

    ("a QUOTED heredoc delimiter means the body is data",
     "triggers/prose-through-shell",
     "              for quoted, body in bodies if not quoted]",
     "              for quoted, body in bodies]",
     "`git commit -F - <<'MSG'` is refused for backticks in a message that "
     "the shell never touches — every commit message in this repo is written "
     "that way, and it is the remedy this guard's own refusal recommends"),

    ("a leftover per-repo skill copy is reported", "bin/llm_chat",
     '    if not os.path.isfile(path):\n        return ""',
     '    if True:\n        return ""',
     "every repo installed under the old per-repo scheme keeps a skill file "
     "that is stale about WHEN TO TRUST AN INVITE, and nothing reports it — "
     "the migration lives in install.sh, so it only ever reaches the repos "
     "somebody was already re-running it in"),

    ("the MCP and the CLI are checked against each other, not each other's "
     "descriptions", "bin/llm_chat",
     # BACK TO `--peek-at`, which is the rename this check could not see until
     # #23. argparse allowed abbreviations, so `--peek` parsed as a prefix of
     # `--peek-at`: the correspondence test went green and the CLI died later
     # on `args.peek` deep in a handler — four tests errored, none failed, and
     # #22 could only call it unmeasured. It was swapped to `--no-advance`
     # then, purely because that is not a prefix, with the note that the
     # abbreviation hazard was its own issue. Abbreviations are off now, so
     # the harder form is measurable and the placeholder is retired.
     '    p.add_argument("--peek", action="store_true", help="do not advance your cursor")',
     '    p.add_argument("--peek-at", action="store_true", help="do not advance your cursor")',
     "bin/llm-chat-mcp builds argv this parser rejects, and every argv fixture "
     "in test_mcp.py stays green — both halves were written from the same "
     "belief about what the CLI accepts, so they agree with each other "
     "whatever it actually does, and the break only shows in a live session"),

    ("an entrypoint the AGENT-facing doc never names is reported",
     "triggers/undocumented-surface",
     "        if name not in text:\n            missing.append(name)",
     "        if False:\n            missing.append(name)",
     "bin/llm-chat-mcp shipped with three mentions in README.md and none in "
     "llms.txt: every other check pools the docs, so the name counted as "
     "documented while the whole integration surface stayed invisible to the "
     "readers that file exists for"),

    ("the DIVERGENT-BUILD warning is on the hook's side of the gap",
     "bin/llm-chat-deliver",
     "    divergent = divergent_checkouts()\n"
     "    if not gap and not drift and not divergent:",
     "    divergent = []\n    if not gap and not drift:",
     "the only remaining warning lives in the CLI you TYPE, which is silent "
     "in exactly the case it exists for — an OLD vendored copy does not "
     "contain the check, so it fires when your copy is NEWER and never when "
     "it is older, and older is the common direction because a vendored "
     "payload goes stale by sitting still"),

    ("a vendored copy of the SAME build is not a divergence",
     "bin/llm-chat-deliver",
     "        if theirs and theirs != mine:",
     "        if theirs:",
     "a repo whose hooks were installed FROM its vendored copy is correctly "
     "configured and gets nagged about itself on every session, which is the "
     "cry-wolf failure this project has already paid for twice — and the "
     "second time the lesson was that a line permanently wrong for its "
     "reader teaches them to skip it"),

    ("an unknowable fingerprint claims no divergence",
     "bin/llm-chat-deliver",
     "    mine = fingerprint_of(ROOT)\n    if not mine:\n        return []",
     "    mine = fingerprint_of(ROOT)\n    if False:\n        return []",
     "a failed hash makes every second checkout look divergent, so the "
     "warning fires for everybody who has vendored ANY build — including the "
     "matching one it is meant to stay quiet about"),

    ("the hook path is read as a PATH, not matched as a substring",
     "bin/llm_chat",
     "        if os.path.basename(stripped) == hook:",
     "        if hook in stripped:",
     "a wrapper called `run-llm-chat-deliver-first` yields its own directory "
     "as the checkout, so doctor names a tree nothing runs from — and a tree "
     "named wrongly and confidently is worse here than no tree named at all, "
     "because the remedy it prints sends you somewhere real that is not it"),

    ("a shared file cannot name ONE of several sessions", "bin/llm_chat",
     "        if len(waiting) != 1:\n            continue",
     "        if False:\n            continue",
     "every undeclared session in a checkout is claimed by whatever the "
     "project store says, so a DEAD identity reads as live and the caller "
     "nudges a room nobody is reading — #21, and it is the one direction this "
     "verb must never fail in"),

    ("a shared file naming SEVERAL identities names none of them",
     "bin/llm_chat",
     "            name = names.pop() if len(names) == 1 else None",
     "            name = names.pop() if names else None",
     "a checkout that talks under several names attributes an arbitrary one "
     "of them — whichever the set happens to yield — to a session that "
     "declared nothing, which is a coin flip presented as a fact"),

    ("a session's own DECLARATION is read at all", "bin/llm_chat",
     '    if name:\n        out.append((name, "declared"))',
     "    if False:\n        out.append((name, \"declared\"))",
     "the strongest evidence there is goes unread, so a session that said who "
     "it is falls back to being guessed at from a shared file — and I told "
     "#19's reporter this file was not written per session, which was false "
     "and is exactly the belief that makes inference look unavoidable"),

    ("who lists a session ONCE per identity, not once per reason",
     "bin/llm_chat",
     "        for row in rows:\n"
     "            if row.get(\"sessionId\") == sid:\n                break",
     "        for row in []:\n"
     "            if row.get(\"sessionId\") == sid:\n                break",
     "an agent sitting in four rooms under one name is printed four times, "
     "which `who` did on its first real run and which makes any count taken "
     "from this mapping a count of memberships wearing a session's name"),

    ("doctor reads the missed-wake record too", "bin/llm_chat",
     "                missed = missed_since_the_last_wake(project, landed)\n"
     "                if missed is not None:",
     "                missed = None\n                if False:",
     "the queue-based check is the only contradiction left, and it can only "
     "speak while something is UNREAD — so a wake that failed and whose "
     "message was then collected by a tool call reads as healthy, which is "
     "the ordinary outcome and is what gameloop passed to a human as "
     "'llm_chat is healthy' an hour before the hook contradicted it"),

    ("a spent miss does not contradict a newer landing", "bin/llm_chat",
     "    if not at or at <= float(landed_at or 0):\n        return None",
     "    if not at:\n        return None",
     "a wake that failed once and was followed by one that worked keeps "
     "reporting the path broken forever, so the loudest line in `doctor` "
     "becomes permanently wrong for anybody who has ever had a miss — and a "
     "warning that cries wolf is one this project has already paid to learn "
     "about twice"),

    ("being served by a tree you are EDITING is said out loud", "bin/llm_chat",
     "    if direct and checkout_dirty():",
     "    if False:",
     "the line above calls the directly-wired state reassuring — 'already "
     "running the current scripts, nothing to do' — and CURRENT is not "
     "COMMITTED: for whoever maintains the checkout those scripts are "
     "uncommitted work, a half-saved hook takes effect on the next tool call, "
     "and a sweep mutating this tree once reached a neighbouring agent and "
     "retired its waker"),

    ("a WIDE bind is not read as loopback", "bin/llm_chat",
     '    if bind == "wide":',
     '    if bind == "never":',
     "the only check that reaches a server started before `--host=::1` "
     "existed — the bind is fixed at startup and nothing restarts a running "
     "server, so every document in every repo can be correct while every "
     "live server is still listening on all interfaces with no auth; and "
     "falling through leaves the reassuring `loopback` line printed over a "
     "wide one, which is worse than printing nothing"),

    ("a COPY is told the report cannot see it", "bin/llm_chat",
     "    elif direct and own_checkout(source) is False:",
     "    elif False:",
     "teaching the dirtiness check to refuse a tree it cannot see stops the "
     "false alarm and leaves this population with NO line at all — and 'could "
     "not look' then renders exactly like 'looked, nothing to say', which is "
     "the same collapse one level up; wcs's version of what it costs is that "
     "a consumer who checks concludes the diagnostic is broken (right, by "
     "luck) and one who does not treats a blessed payload as unblessed"),

    ("only the tree the HOOKS run from is anybody's business",
     "bin/llm_chat",
     "    if direct and checkout_dirty():",
     "    if checkout_dirty():",
     "a consumer wired to its own vendored copy is nagged about OUR working "
     "tree, which it is not being served by and cannot act on — the cry-wolf "
     "failure this file has already paid for twice"),

    ("a compile that built NOTHING is refused", "bin/llm_chat",
     "    missing = missing_workers()\n    if missing:",
     "    missing = missing_workers()\n    if False:",
     "a server comes up on a checkout whose workers were never produced and "
     "500s every /db request — and the check went untested for so long "
     "because it only ever fired in a COLD CLONE, which is precisely where "
     "the suite had never been run"),

    ("doctor says when it is not the build the HOOKS run", "bin/llm_chat",
     "    elsewhere = sorted(t for t in hook_trees\n"
     "                       if os.path.abspath(t) != os.path.abspath(ROOT))",
     "    elsewhere = []",
     "a repo that vendors llm_chat runs its own months-old copy while the "
     "hooks run current code from wherever install.sh was invoked, so the "
     "diagnosis describes a different program than the one delivering — "
     "gameloop read a stale doctor all day and found it through an argparse "
     "error rather than through the tool whose job it is"),

    ("the hook's PATH is kept, not just its name", "bin/llm_chat",
     "                            where = hook_checkout(cmd, e)\n"
     "                            if where:\n"
     "                                trees.add(where)",
     "                            where = None\n"
     "                            if False:\n"
     "                                trees.add(where)",
     "the one fact saying WHICH BUILD delivers your messages is in hand at "
     "that line and thrown away, which is exactly how it came to be missing "
     "for as long as it was"),

    ("a skill naming TWO checkouts names neither", "bin/llm_chat",
     "    return named.pop() if len(named) == 1 else None",
     "    return named.pop() if named else None",
     "one of several paths is offered as THE checkout agents are sent to, "
     "which is a coin flip presented as a fact — and `set.pop` makes it a "
     "different coin flip per run"),

    ("say ECHOES what it stored", "bin/llm_chat",
     '    if first:\n        shown = first[0] if len(first[0]) <= 72 '
     'else first[0][:71] + "…"\n        print("  stored: %s" % shown)',
     "    if False:\n        pass",
     "a shell-mangled message and an intact one print identically and the "
     "send succeeded, so there is no error to notice — the incident in "
     "llms.txt was recovered only by an agent re-reading its own message "
     "after noticing a sentence had lost its subject, which is the weakest "
     "detector available"),

    ("the echo reports the LENGTH as well as the first line", "bin/llm_chat",
     '    print("sent #%d to %s as %s  (%d chars)"\n'
     "          % (seq, name, identity, len(text or \"\")))",
     '    print("sent #%d to %s as %s" % (seq, name, identity))',
     "a truncation that leaves the first line intact prints identically to "
     "the whole message, so the one corruption the echo cannot show is the "
     "one it was cheapest to cover"),

    ("a machine-wide skill naming another BUILD is reported", "bin/llm_chat",
     "    theirs, mine = wiring_fingerprint(named), wiring_fingerprint(ROOT)\n"
     "    if not theirs or not mine or theirs == mine:\n        return \"\"",
     '    return ""',
     "`~/.claude/skills/` is ONE file for the whole machine and install.sh "
     "rewrites it with whatever checkout it was run from, so every agent can "
     "be sent to a stale copy with nobody told — measured here pointing at a "
     "vendored tree with no `who`, no `--since`, and `--to-a` still meaning "
     "--to-all"),

    ("the same build at another path stays silent", "bin/llm_chat",
     "    if not theirs or not mine or theirs == mine:",
     "    if not theirs or not mine:",
     "every machine with a second checkout of the SAME build gets the warning "
     "on every doctor run, which is the fires-at-the-wrong-population failure "
     "this project has already paid for twice"),

    ("the passive pointer names a RANGE, not the whole room",
     "bin/llm-chat-deliver",
     '        bound = ("--since %d" % (min(seqs) - 1)) if seqs else "--all"',
     '        bound = "--all"',
     "recovering the handful of messages a pointer named costs every message "
     "ever sent in that room — 466,052 characters to reach three lines, over "
     "the tool-result cap — and it grows without limit, so the recovery path "
     "costs more than the delivery the truncation exists to avoid"),

    ("--since never advances a cursor", "bin/llm_chat",
     "    if since_seq is not None:\n        peek = True",
     "    if False:\n        peek = True",
     "re-reading history marks unread messages read on the way past, so the "
     "command a pointer hands you to RECOVER something silently consumes "
     "whatever else was waiting"),

    ("the cursor guard compares against the CURSOR, not the bound",
     "bin/llm_chat",
     '            if not peek and high_water > member.get("seen_seq", 0):',
     "            if not peek and high_water > since:",
     "reading an older range yields a high_water above the requested bound "
     "but BELOW where the cursor sits, so the commit REWINDS it and every "
     "message in between is delivered again — the two numbers were identical "
     "for as long as `since` could only be the cursor or zero, which is what "
     "made the proxy look like the invariant"),

    ("the SLACK BRIDGE is not the CLI", "triggers/undocumented-surface",
     '    return bare == tool or bare.endswith("/" + tool)',
     "    return bare.endswith(tool)",
     "`@llm_chat list` and the typo example `@llm_chat lsit` come back as CLI "
     "commands that do not exist — two standing false positives in a report "
     "whose only job is to be acted on, and a list that is mostly noise stops "
     "being read"),

    ("formatting is stripped before the name is matched",
     "triggers/undocumented-surface",
     '    bare = word.strip("`\'\\"*()[]\\\\,.:;")',
     "    bare = word",
     "a remedy written as \"`llm_chat close`\" — the way nearly every remedy "
     "in this repo is written — stops being recognised as the CLI at all, so "
     "the ghost check goes quiet about the source strings it exists to read. "
     "This is the REGRESSION the exact-match fix introduced, and a mutation "
     "is the only thing that would have caught it, because removing the "
     "strip makes the report SHORTER and a shorter report reads like progress"),

    ("a hook gives up on a throttle sooner than a direct caller",
     "bin/llm_chat",
     "    return HOOK_RETRY_WAITS if os.environ.get(HOOK_ENV) else RETRY_WAITS",
     "    return RETRY_WAITS",
     "a hook retries inside a subprocess timeout that cannot outlast the "
     "window — measured at >=40s against an 8s kill — so it spends its whole "
     "deadline to fail the same way, while the next poll would have collected "
     "the message for free (#24)"),

    ("the throttle advice travels WITH the error", "bin/llm_chat",
     '    if res.get("advice"):\n        problem += "\\n" + res["advice"]',
     "    if False:\n        problem += \"\\n\" + res[\"advice\"]",
     "a bare `HTTP 429 Rate limit exceeded` leaves a caller unable to tell "
     "whether the write landed — which is #27 exactly, a case where it had"),

    ("removing NOTHING is not a failure", "bin/llm_chat",
     '        if "not found" in (res["error"] + body).lower():\n'
     "            return {}",
     "        if False:\n            return {}",
     "`delete` dies on any room that was never spoken in — zonai answers a "
     "DELETE matching no rows with a 404, and the abort leaves the room "
     "behind while reporting a table name rather than a cause (#28). Every "
     "room a 429'd `open` created is in that state"),

    # THE REPLACEMENT MUST NOT BE ORDINARY CODE. This one was a bare
    # `return {}`, which is a line the tree legitimately contains — so the
    # stranded-mutation check found it in a clean tree and refused to run,
    # reporting a sweep that had been killed when none had. A mutation's
    # replacement is a fingerprint as well as a change.
    ("only NOTHING MATCHED is swallowed, not every error", "bin/llm_chat",
     '        if "not found" in (res["error"] + body).lower():\n'
     "            return {}\n        refuse(res)",
     '        if True:\n            return {}\n        refuse(res)',
     "a server that is down, a rejected predicate or a permission failure all "
     "report success while the rows are still there — which is the same "
     "told-it-worked-when-it-did-not defect as #27, in the one verb with no "
     "undo"),

    ("a failure AFTER the room exists says the room exists", "bin/llm_chat",
     "    except SystemExit as stop:\n        if not created_here:\n"
     "            raise",
     "    except SystemExit as stop:\n        if True:\n            raise",
     "`open` is two writes and a 429 between them leaves the room created "
     "while the caller sees only the error — so it opens the room again, "
     "which succeeds and SILENTLY DISCARDS the corrected topic and briefing "
     "(#27). A dispatched agent lost its only back-channel to this"),

    ("a room that already existed is not called a PARTIAL success",
     "bin/llm_chat",
     "        if not created_here:\n            raise",
     "        if False:\n            raise",
     "every failure on an existing room claims it was just created, which is "
     "a new false statement traded for the old one"),

    ("a second open SAYS what it ignored", "bin/llm_chat",
     "        ignored = [what for what, given in\n"
     "                   ((\"topic\", topic), (\"briefing\", briefing))\n"
     "                   if given and chan.get(what) != given]",
     "        ignored = []",
     "the corrected house rules a caller passes on the recovery attempt are "
     "dropped without a word — documented as 'recorded only if the channel is "
     "new' and still silent at the moment it matters, which the reporter "
     "caught only because the rendered briefing quoted the attempt they had "
     "been told failed"),

    ("the remedy `leave` recommends is a REAL verb", "bin/llm_chat",
     '    p = sub.add_parser("close", help="end a room: transcript kept, '
     'nobody "\n                                     "woken here again")',
     '    p = sub.add_parser("closs", help="end a room: transcript kept, '
     'nobody "\n                                     "woken here again")',
     "`leave`'s refusal offers `llm_chat close <room> --reason` as its FIRST "
     "remedy and the parser has no such choice — an agent closing 24 finished "
     "rooms in a loop got 24 argparse usage errors, which read as 'you called "
     "it wrong' rather than 'that verb does not exist' (#26)"),

    ("close SAYS SO IN THE ROOM before shutting it", "bin/llm_chat",
     "    announce(server, name, identity,\n"
     "             f\"#{name} is now CLOSED — {reason}\\n\"",
     "    _ = (lambda *a, **k: None)(\n"
     "             f\"#{name} is now CLOSED — {reason}\\n\"",
     "`say` refuses on a closed channel, so a closure announced after the "
     "fact is one nobody in the room ever hears about — they simply find the "
     "door locked the next time they try to speak"),

    # ANCHORED ON THE REFUSAL, not on the lookup. The membership check is the
    # same line in four verbs, so the bare form matched four places and the
    # sweep refused it rather than guess — a correct refusal and a useless
    # measurement. The sentence below it is what makes this one `close`.
    ("close refuses a NON-MEMBER", "bin/llm_chat",
     "    if get_membership(server, name, identity) is None:\n"
     "        raise SystemExit(\n"
     "            f\"{identity} is not a member of {name}, so it is not "
     "yours to \"",
     "    if False:\n        raise SystemExit(\n"
     "            f\"{identity} is not a member of {name}, so it is not "
     "yours to \"",
     "anyone can end a room they were never in, which is not the same trade "
     "as `reopen` being open to all — reviving a room is recoverable and "
     "ending one stops every member being woken"),

    ("a cap can be raised BEFORE the room shuts", "bin/llm_chat",
     "        if max_messages is None:\n"
     '            print(f"{name} is already open")\n            return',
     '        print(f"{name} is already open")\n        return',
     "`--max-messages` goes back to doing nothing on an open room, so the only "
     "way to raise a cap is to let the room CLOSE first — the remedy reachable "
     "only after the harm, and worst in #llm_chat_owner, where the agents "
     "arriving at a shut door are the ones who could not get connected in the "
     "first place"),

    ("--to-a does not silently mean --to-all", "bin/llm_chat",
     '        kwargs.setdefault("allow_abbrev", False)',
     "        pass",
     "`--to-a` becomes --to-all and `--to-n` becomes --to-none — opposite "
     "outcomes one keystroke apart, resolved silently, in the one place this "
     "project already refuses to guess between audience flags (#23)"),

    ("the abbreviation refusal names what you MEANT", "bin/llm_chat",
     "        if meant:\n"
     '            message += ("\\n  abbreviations are OFF here: `%s` is not a '
     'flag, "',
     '        if False:\n            message += ("\\n  abbreviations are OFF '
     'here: `%s` is not a flag, "',
     "the refusal is bare argparse — `unrecognized arguments: --to-a` and "
     "nothing more — so turning abbreviations off has moved the confusion "
     "rather than removed it, which is the only thing that made the change an "
     "improvement"),

    ("the hint can see flags defined on SUBCOMMANDS", "bin/llm_chat",
     "                if isinstance(action, argparse._SubParsersAction):\n"
     "                    stack.extend(action.choices.values())",
     "                if False:\n"
     "                    stack.extend(action.choices.values())",
     "`unrecognized arguments` is raised by the TOP-LEVEL parser, whose own "
     "actions are only --server and --help, so every hint about a real flag "
     "goes silent while the refusal keeps firing — measured exactly that way "
     "before it recursed"),

    ("a wake and a note left for the dead are worded APART", "bin/llm_chat",
     '        awake = [m for m in woken if m in live]\n'
     '        gone = [m for m in woken if m not in live]',
     '        awake = list(woken)\n        gone = []',
     "`say --to lead-ml` reads 'wakes lead-ml' whether that session is running "
     "or ended four days ago, which is exactly what an orchestrator acted on "
     "three times in an hour before filing #19 — and the failure is silent in "
     "the direction that costs most, because the sender stops and waits"),

    # `do_who` asks the same question the same way, so each anchor carries
    # the line after it. Two matches make the sweep refuse both rather than
    # guess which one a name meant — a correct refusal and a useless
    # measurement, and it is the second time this pair of callers has done it.
    ("an unaskable host is not a claim that everyone is DEAD", "bin/llm_chat",
     "    live = live_identities()\n    if live is None:\n"
     "        # Could not ask the host.",
     "    live = live_identities() or {}\n    if False:\n"
     "        # Could not ask the host.",
     "on any machine where the host cannot be asked — no `claude` on PATH, a "
     "different harness — every message reports LEFT FOR every member, so the "
     "one line that was added to be believed becomes the line nobody can"),

    ("`who` exits NONZERO when the host could not be asked", "bin/llm_chat",
     "    live = live_identities()\n    if live is None:\n        if as_json:",
     "    live = live_identities() or {}\n    if False:\n        if as_json:",
     "'nobody is running' and 'nothing answered' both print an empty list, so "
     "the exit status is the only thing carrying the difference to a script — "
     "and collapsing exactly that pair is what both open issues are about"),

    # REPLACES an anchor that #21 deleted. The old mutation pointed at a
    # two-path lookup that no longer exists; the BEHAVIOUR it defended — a
    # session with no store of its own still being findable — survived the
    # rewrite and needs an anchor that survived with it. A stale mutation is
    # not a neutral leftover: it reports SURVIVED, which reads as an
    # undefended behaviour rather than as a broken measurement.
    ("a live session's identity survives having no session store",
     "bin/llm_chat",
     "    for cwd, waiting in unclaimed.items():",
     "    for cwd, waiting in []:",
     "measured across this machine one checkout runs entirely on the project "
     "file, so that agent reads as dead while it is answering — and the #19 "
     "wording would then be confidently wrong about the very case it exists "
     "to report"),

    ("a missed wake is SAID, not merely filed", "bin/llm-chat-deliver",
     "    missed = missed_wake_note()",
     "    missed = ''",
     "the waker has been spawning a detached watcher to record a rewake that "
     "went nowhere, into a file nothing in this repo ever opened — which is "
     "#20 exactly: a message sat 32 minutes, `doctor` could state the live "
     "state precisely, and two agents in one room each concluded the other "
     "had gone quiet"),

    ("a LATER landing retires the miss", "bin/llm-chat-deliver",
     "    if wake_landed_since(at):\n        return \"\"",
     '    if False:\n        return ""',
     "a wake that failed once is announced as the live state for as long as "
     "the record sits there — caught against real state, where a miss from "
     "the previous evening rendered as news seventeen hours later, which is "
     "the same past-printed-as-present defect `doctor` already carries a scar "
     "for"),

    ("only a LATER landing counts, not any landing", "bin/llm-chat-deliver",
     '            return float((json.load(f) or {}).get("at") or 0) > when',
     '            return float((json.load(f) or {}).get("at") or 0) >= 0',
     "any landing ever recorded in this checkout retires every future miss, "
     "so the report goes permanently silent on exactly the machines where a "
     "wake HAS worked once — which is all of them, and it is the direction "
     "that looks healthy"),

    ("the missed-wake line is said ONCE per miss", "bin/llm-chat-deliver",
     '            if int((f.read() or "0").strip() or 0) >= at:\n'
     "                return \"\"",
     '            if False:\n                return ""',
     "it fires on every tool call instead, and a warning on a loop is read "
     "for a day and filtered out for good — which is this project's own "
     "finding about crying wolf, applied to the one warning it would be "
     "expensive to stop believing"),

    ("a turn still running is not a missed wake", "bin/llm-chat-wake",
     "    if tool_ran_since(at):\n        return 0",
     "    if False:\n        return 0",
     "`wake_landing` only consumes the note when a turn ENDS, so any turn "
     "longer than the grace window records a miss — and the record is now "
     "spoken, so the session that WAS woken gets told its wake never landed, "
     "which is how a report stops being believed"),

    ("a stale tool mark is not a running turn", "bin/llm-chat-wake",
     '            os.path.join(STATE, "probe", "post-tool-use")) > at',
     '            os.path.join(STATE, "probe", "post-tool-use")) > 0',
     "every project that has ever run a tool has that mark, so a presence "
     "check silences the missed-wake report everywhere at once and leaves it "
     "looking healthy"),

    ("the delivery hook answers the event that actually fired",
     "bin/llm-chat-deliver",
     '        event = payload.get("hook_event_name") or event',
     "        pass",
     "it is registered on SessionStart now, and a reply stamped PostToolUse "
     "is one the host is entitled to discard — silently, in the exact place "
     "it was added for: a session starting up deaf"),

    ("the delivery hook is on SessionStart", "install.sh",
     'hooks.setdefault("SessionStart", []).append({\n    "hooks": [{\n'
     '        "type": "command",\n        "command": hook_cmd,',
     'if False:\n    hooks.setdefault("SessionStart", []).append({\n'
     '    "hooks": [{\n        "type": "command",\n'
     '        "command": hook_cmd,',
     "the waker is on SessionStart and cannot speak there — asyncRewake with "
     "a week-long timeout blocks in the background rather than answering — so "
     "a session that starts deaf after a host restart learns it from nothing, "
     "which is the 32 minutes in #20"),
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
    "bin/llm_chat:sdk_version": "asserted directly: a real SDK root and a "
        "directory that is not one",
    "bin/llm_chat:matching_dart_sdk": "asserted directly over the three cases "
        "that matter — DART_SDK matching, DART_SDK set to the WRONG version, "
        "and a prefix that must not count as a match",
    "bin/llm_chat:host_dart_version": "asserted directly against the real "
        "binary, and for the unextracted-cache case that must read as None",
    "bin/llm_chat:dart_sdk_candidates": "a generator of PATHS, not a decision. "
        "Every ordering property it has is asserted through "
        "matching_dart_sdk, which is the only caller and IS covered above; "
        "mutating it would only reorder a list whose consumer is checked",
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
    "bin/llm_chat:message_text": "all six paths asserted directly, including both "
                                 "refusals and the boundary one char either side; the "
                                 "shell-prose refusal also carries two mutations",
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
    "bin/llm_chat:announce": "both branches asserted directly — a normal "
        "departure produces the message, and one a closed or capped room "
        "refuses does not stop the leave that called it",

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

    "bin/llm_chat:installed_checkout": "present, absent, field-less and "
        "corrupt asserted directly — none may read as a guess",
    "bin/llm-chat-deliver:source_checkout": "recorded, absent and corrupt "
        "asserted directly via the notice that consumes it",
    "triggers/undocumented-surface:declared": "verbs, options and a missing "
        "source file asserted directly",
    "triggers/undocumented-surface:invented": "the reverse walk — backticks, "
        "fenced blocks, path-prefixed invocations, and the indented-prose "
        "false positive it produced on its first run, all asserted directly",
    "triggers/undocumented-surface:assembled_remedies": "counted rather than "
        "asserted, so the caveat retires itself at zero. ONE fixture holds "
        "prose, another tool's remedy and two real ones, asserting the counter "
        "separates them — a count looks measured even when it measured the "
        "wrong population, and both the too-wide and too-narrow subjects are "
        "probed to CAUGHT",
    "triggers/undocumented-surface:stale_values": "asserted directly — a "
        "remedy naming no accepted value is caught, one naming a valid value "
        "is not, and the f-string-assembled case that CANNOT be checked is "
        "pinned as a limit rather than left to be discovered",
    "triggers/undocumented-surface:bare_words": "placeholders, flags and "
        "non-remedy lines asserted directly",
    "triggers/undocumented-surface:nested": "drives the PARTLY CHECKED "
        "coverage line, asserted through the report",
    "triggers/undocumented-surface:named_in_strings": "the denominator. "
        "Remedies in unbackticked source strings, docstring prose, and "
        "sentence-shaped mentions all asserted directly — the last two are "
        "false positives this produced and had to be told apart",
    "triggers/undocumented-surface:verbs_from_help": "asserted against the "
        "real CLI, which registers two verbs through a loop the regex cannot "
        "see, plus the unrunnable and exploding fallbacks",
    "triggers/undocumented-surface:main": "gap, no-gap, never-blocks and the "
        "disclaimer-on-success asserted directly — a green that implies the "
        "docs are GOOD is the more expensive lie",
    "triggers/write-through-interpreter:offending_write": "asserted against "
        "five VERBATIM smuggled writes from this session and seven legitimate "
        "commands; the allow-cases matter more, since a guard that blocks the "
        "test runner is turned off within the hour",
    "triggers/piped-verdict:refusal": "asserted directly — both kinds must "
        "carry the two-line remedy and name the escape hatch, or the refusal "
        "is a wall rather than a redirection",
    "triggers/piped-verdict:main": "every branch asserted directly, including "
        "the visible escape hatch and the non-Bash tool",
    "bin/llm-chat-slack:bridge_command": "the recogniser is asserted in five "
        "spellings a human might type and refused in three sentences that "
        "merely CONTAIN the words — anchored end to end, because a bridge "
        "that eats 'ask llm_chat list for me' is eating conversation",
    "bin/llm-chat-slack:is_vocative": "all three marks of address asserted "
        "directly — at the start, after a greeting, wrapped in commas — plus "
        "the determiner rule that keeps 'the build, which is stuck' a "
        "sentence ABOUT the build. The mention-versus-address distinction it "
        "exists for is swept",
    "bin/llm-chat-slack:members_of": "every way it can fail is asserted "
        "directly and they all answer the same way: a dead subprocess, "
        "unparseable output, a room that is not listed, and a room with no "
        "member list. All four are 'could not look', and the paired sweep "
        "checks that is never reported as 'nobody is there'",
    "bin/llm_chat:last_activity": "its refusal is swept — an unreachable "
        "server must not read as silence — and the rest is asserted directly: "
        "a recent message, a recent tool run, the newest of the two winning, "
        "a zero timestamp not counting as now, and every message pushing the "
        "deadline out, which is the debounce itself",
    "bin/llm_chat:quiet_for": "a two-line wrapper whose only decision — None "
        "stays None rather than becoming a number — is swept through "
        "last_activity and asserted directly in both directions",
    "bin/llm_chat:describe_quiet": "asserted directly in all three states: "
        "how much longer to wait, due now, and CANNOT TELL reading as cannot "
        "tell rather than as silence",
    "bin/llm_chat:do_maintenance": "every action asserted directly — queue "
        "refuses an unknown name and names the known ones, queueing twice "
        "does not stack, cancel of something absent is not an error, list "
        "shows attempts and finished work, run with nothing due says why. "
        "The dispatch is exercised through the real parser, so an argparse "
        "dest that did not match would fail here rather than in production",
    "bin/llm_chat:run_maintenance": "the three refusals are swept or asserted "
        "— CANNOT TELL is not permission, the threshold holds a minute under "
        "the line, and a second runner finds the lock taken. The empty-queue "
        "path is asserted to not even ask the server, because it runs on "
        "every waker heartbeat",
    "bin/llm_chat:_run_queued": "the registry re-check and the not-marked-"
        "done rule are both swept; the rest is asserted directly, including "
        "that a task raising does not break the turn, that the attempt "
        "history is capped, and that a task queued AFTER the quiet check "
        "waits for the next pass",
    "bin/llm_chat:vacuum_store": "asserted against a real SQLite file with "
        "real freed pages, a missing file, and a genuinely locked one — the "
        "last being the case that decides whether this is safe to run "
        "unattended beside a live server",
    "bin/llm_chat:read_maintenance": "corrupt JSON, three wrong shapes and "
        "the round trip asserted directly; it is the same tolerate-anything "
        "reader as read_exits, whose migration branch is swept",
    "bin/llm_chat:write_maintenance": "asserted by round trip through every "
        "queue test in the file; it is an atomic replace with no decision in "
        "it",
    "bin/llm_chat:maintenance_path": "a path join, pinned by every test in "
        "test_maintenance.py reading back what the CLI wrote",
    "bin/llm_chat:maintenance_lock": "a path join, exercised by the "
        "second-runner test which takes the lock by hand and asserts the run "
        "declines",
    "bin/llm_chat:_ago": "presentation only — it turns a timestamp into "
        "minutes for a listing, and both the known and unknown cases are "
        "asserted through the list output",
    "bin/llm-chat-wake:run_maintenance": "all three paths asserted directly: "
        "it asks the CLI, it does not ask when there is no server, and a "
        "failure never breaks the waker it runs inside",
    "bin/llm-chat-mcp:_build_maintenance": "covered by the correspondence "
        "tests, which are stronger than a sweep here: every declared property "
        "must reach the argv, and every built argv must be accepted by the "
        "real CLI parser",
    "bin/llm_chat:commit_cursor": "its two properties are swept as ORDER "
        "rather than as body — that the text is out before it runs, and that "
        "it still runs on success. Both are asserted directly as well: a read "
        "that dies mid-render leaves seen_seq at 0 and the message readable by "
        "the next reader, a successful one advances and then says 'nothing "
        "new', --json commits on its own exit path, and --peek commits nothing",
    "bin/llm_chat:_render": "every branch through it is asserted directly — "
        "the rendered path, the --json path, the nothing-new path with and "
        "without earlier messages, the own-post filter and the closed-room "
        "note. It is do_read's body, moved so that committing the cursor is "
        "the LAST act of every exit rather than the first act of the function",
    "bin/llm-chat-wake:watch_for_a_missed_wake": "the spawn itself is swept — "
        "removing it is the circularity coming back — and its two refusals "
        "are asserted directly: no pending note and a corrupt one both spawn "
        "nothing, and a Popen that raises never breaks the wake it runs one "
        "line before",
    "bin/llm_chat:auto_reload_allowed": "a file-exists check whose only "
        "decision — off unless turned on — is swept in the waker, where it "
        "decides whether a window gets reloaded, and asserted directly in "
        "both directions through the switch",
    "bin/llm_chat:auto_reload_path": "a path join, pinned by the switch tests "
        "writing it and auto_reload_allowed reading it back",
    "bin/llm_chat:do_auto_reload": "both directions asserted directly, "
        "including that turning it OFF when it was never on is not an error, "
        "and that the switch says what it will and will not do. Reached "
        "through the real parser, which asserts it short-circuits before "
        "do_reload — otherwise turning the opt-in on would reload the window",
    # ── what this tool cannot measure about ITSELF ──────────────────────────
    #
    # Both of these are asserted directly in test_gate.py and deliberately not
    # swept, because sweeping them is self-referential in a way that produces
    # a verdict about the sweep rather than about the behaviour.
    #
    # `sweep_in_progress` is what allows the suite to run at all while a
    # mutation is applied. Disabling it during a sweep means no test executes
    # — the exact failure it exists to prevent — so the sweep would report
    # CRASHED and learn nothing about whether anything defends it.
    #
    # `sweep_in_a_copy` is checked in main(), and the mutated copy's main() is
    # already past that line by the time anything could observe it. The test
    # that calls main() would, with the guard removed, start a real nested
    # sweep inside the outer one.
    #
    # Stated here rather than left as two absences, because a mutation list
    # that quietly omits the things hardest to test is the failure this file
    # exists to name.
    "test/run.py:sweep_in_progress": "asserted directly in both directions — "
        "lock held means a mutation is expected, lock free means the stranded "
        "check still runs. NOT swept: disabling it stops the suite executing "
        "during a sweep, so the sweep could only report a crash about itself",
    "test/mutate.py:sweep_in_a_copy": "the body asserted directly with the "
        "copier and the children stubbed — every shard waited on, one red "
        "shard failing the sweep, all-green returning 0, each shard told its "
        "own index and given its own copy, and a failed copy stopping rather "
        "than sweeping a partial tree. NOT swept, structurally: a mutation "
        "here is applied inside the COPY, whose main() has IN_COPY set and "
        "therefore never calls this function, so the mutant cannot run "
        "itself. THIS REASON USED TO READ 'asserted directly by stubbing it "
        "and checking main() calls it' — which is a claim about the CALL "
        "SITE, filed as coverage of the function. A raise at the top of the "
        "body SURVIVED a full suite: it was INERT, and the line it contains "
        "is `max(child.wait() ...)`, which carries eight shards' verdicts "
        "back to verify. Had that read `return 0`, every sweep ever run here "
        "would have reported success",
    "bin/llm_chat:__init__": "Store's three fetches — the whole point of it — "
        "are swept as a REQUEST COUNT: five rooms must cost at most four "
        "calls, asserted by counting them rather than by timing, because a "
        "timing test passes on a fast machine while the count creeps back",
    "bin/llm_chat:channel": "a dict lookup, and its answer is asserted "
        "equal to the unbatched path's in the same test — speed that changed "
        "the verdict is worth nothing",
    "bin/llm_chat:membership": "a dict lookup; the membership rules it feeds "
        "(a room you have left owes nothing, a room you never joined owes "
        "nothing) are swept where they are decided, in owed_in",
    "bin/llm_chat:in_channel": "a dict lookup returning [] for a room with no "
        "messages, which is asserted directly — a quiet room must report no "
        "debt rather than raise",
    "triggers/tell-the-consumers:notice": "asserted directly — it must name "
        "the checkouts by repo, carry the version arrow, point at `llm_chat "
        "channels` rather than guessing a room, and say that a summary to the "
        "human is not telling THEM, which is the actual failure it exists for",
    "triggers/tell-the-consumers:output_of": "both streams asserted directly, "
        "plus a payload with no tool_response and one whose tool_response is "
        "not a dict — this runs after every Bash call, so odd input must "
        "return \"\" rather than raise",
    "bin/llm-chat-wake:own_tmp": "the CLI's copy is swept; all three are "
        "asserted equal to each other and asserted not to be the "
        "destination-named form, which is the whole property. Duplicated the "
        "same way doorbell_dir is, and pinned the same way — three standalone "
        "scripts with no shared module",
    "bin/llm-chat-slack:own_tmp": "as above; this file's four state writes "
        "are the ones most exposed, since the bridge is a long-lived loop "
        "writing while commands run beside it",
    "bin/llm_chat:do_owed": "its three exit codes are the contract other "
        "things gate on and all three are asserted directly — nothing owed, "
        "something owed, and COULD NOT LOOK outranking a debt. The batching "
        "it grew is swept in owed_in as a request COUNT; the per-server "
        "grouping and the surviving 429 text are asserted with two servers "
        "and a throttled one, because a reason replaced by 'could not reach' "
        "is what made a throttle read as an outage",
    "bin/llm_chat:load_store": "a closure that opens one of the two stores "
        "from the SAME root and answers {} for absent or corrupt. Both of "
        "those are asserted directly — no project store, no session store, "
        "and a corrupt project store all report nothing shadowed — and the "
        "reason it is a closure at all is swept: reading one store from the "
        "argument and the other from ambient state compared one project's "
        "relic against another project's membership",
    "bin/llm_chat:heartbeat_age": "a stamp read and subtracted; every way of "
        "having no stamp is asserted directly — absent, corrupt, and present "
        "with no timestamp — and all three report CANNOT SAY rather than "
        "healthy, because absence of evidence was the thing being fixed. The "
        "reporting it feeds is swept, and the interval it compares against is "
        "pinned equal to the waker's own",
    "bin/llm_chat:host_sessions": "every way of not getting an answer is "
        "asserted directly and they all return None: a `claude` that is not "
        "installed, a non-zero exit, unparseable output and two wrong shapes. "
        "The one decision that matters — None is not an empty list — is swept "
        "through live_here",
    "bin/llm_chat:report_who_is_home": "all four states asserted through the "
        "rendered report: nobody home, somebody named, could-not-ask, and the "
        "host disagreeing with the environment about who this session is. "
        "The last is swept, because it is issue #12 answered by a third "
        "source and reporting agreement it never checked is the failure",
    "bin/llm_chat:last_server": "both directions asserted directly — the "
        "recorded server is printed, and an UNRECORDED one is not filled in "
        "with the default, which would be a definite claim about an unknown "
        "in the command whose job is removing uncertainty",
    "bin/llm-chat-wake:read_exits": "the one-record format, the list format "
        "and three shapes of corruption asserted directly; it is the waker's "
        "copy of waker_exits, whose migration branch is swept",
    "bin/llm-chat-wake:record_alive": "asserted directly — a live waker's pid "
        "lands in wake.alive, and the paired test asserts `running` no longer "
        "reaches the exit history at all, which is the behaviour that matters",
    "bin/llm-chat-wake:_polling_server": "a best-effort field in a diagnostic "
        "record, asserted directly against a joined room; it swallows "
        "everything on purpose, because an exception here would break the "
        "exit it is describing",
    "triggers/prose-through-shell:refusal": "asserted directly — it must name "
        "the offending snippet, carry the --body-file remedy, and say the "
        "heredoc delimiter has to be QUOTED, which is the load-bearing half "
        "everyone drops",
    "triggers/prose-through-shell:main": "every branch asserted directly, "
        "including the visible escape hatch and the non-Bash tool",
    "triggers/prose-through-shell:snippet": "presentation only — it decides "
        "which 40 characters of a refused sentence get printed, and the "
        "refusal is asserted to contain the backticked word either way",
    "triggers/prose-through-shell:split_heredocs": "quoted, bare and "
        "UNTERMINATED delimiters asserted directly, in both directions: the "
        "quoted body must stay silent and the bare one must fire. The sibling "
        "guard's version of this function is swept, and it is the same "
        "function — an unterminated heredoc that swallows the rest of the "
        "command makes the guard read as silent rather than as broken",
    "triggers/write-through-interpreter:refusal": "asserted directly — it must "
        "name Write/Edit, or the refusal is a wall rather than a redirection",
    "triggers/write-through-interpreter:main": "every branch asserted "
        "directly, including the visible escape hatch",
    "test/mutate.py:probe": "all SIX outcomes asserted by tests that call it "
        "with run_suite and the lock stubbed — caught, survived, crashed, "
        "already-red, no-anchor, ambiguous — plus that the tree is restored "
        "bytes AND mtime, and restored even when the suite raises mid-probe. "
        "NOT swept: probing the prober from inside a sweep nests two of them "
        "on one flock. THIS REASON USED TO READ 'all three outcomes asserted "
        "by running it', which was a HAND-RUN recorded as coverage — nothing "
        "in the suite called this function at all. Caught by "
        "run.py:inert_exclusions on its first execution, one hour after the "
        "identical entry for sweep_in_a_copy was found by hand",
    "bin/llm-chat-deliver:read_watermarks": "all three outcomes asserted "
        "directly — absent file, unreadable file, and a good one — and the "
        "two that matter are exercised through the pre-check as well, where "
        "an empty result must rule NOTHING out. NOT swept separately: every "
        "mutation of it is a mutation of `rooms_that_cannot_have_anything`'s "
        "answer, which is swept, and reverting it there is what shows the "
        "consequence",
    "bin/llm-chat-deliver:note_delivered": "asserted directly in all three "
        "directions: written after a successful read, left alone after a "
        "failed one, and an unwritable path costing a redundant read rather "
        "than the turn. NOT swept: the behaviour that matters is whether the "
        "NEXT tick skips the room, which is the swept pre-check reading what "
        "this wrote",
    "bin/llm-chat-deliver:addressed_to_me": "every audience form asserted "
        "directly, including that unaddressed WAKES you without being for you",
    "bin/llm-chat-deliver:render_channel": "full-text-for-mine, pointer-for-"
        "the-rest, both caps and the saving asserted directly",
    "bin/llm_chat:doorbell_dir": "pinned equal to the waker's copy, which is "
        "the only property that matters about a duplicated convention",
    "bin/llm_chat:doorbell_name": "pinned equal to the waker's copy; the "
        "cross-room collision it exists to prevent is swept",
    "bin/llm_chat:ring": "listener, no listener, stale socket and junk on "
        "disk all asserted directly",
    "bin/llm-chat-wake:doorbell_dir": "pinned equal to the CLI's copy",
    "bin/llm-chat-wake:workspace_key": "pinned equal to the CLI's copy through "
        "doorbell_dir, which is the only thing either side uses it for — and "
        "the CLI's copy IS swept, so a divergence between them fails the "
        "pinning test while the behaviour itself stays defended",
    "bin/llm-chat-wake:open_doorbell": "bind, healthy-holder, stale reclaim "
        "and three failure paths asserted directly",
    "bin/llm-chat-wake:open_doorbells": "one bell per joined room asserted "
        "directly, including that TWO identities in one project are both "
        "reachable — the hole that made this a membership key",
    "bin/llm-chat-wake:doorbell_name": "pinned equal to the CLI's copy, and "
        "asserted not to collide across rooms or identities",
    "bin/llm-chat-wake:wait_for_ring": "rung, timed out, no-doorbell and a "
        "failing accept asserted directly — accept failing must still report "
        "a ring, or a real wake is dropped",
    "bin/llm_chat:refuse_impersonation": "every branch asserted directly, "
        "including the second-identity case that is not a loophole",
    "bin/llm_chat:waker_exit": "missing, corrupt and reason-less records "
        "asserted directly — none of them may read as healthy",
    "bin/llm-chat-wake:record_exit": "every exit path asserted directly, "
        "including that an unwritable state dir cannot break the exit",
    "bin/llm-chat-wake:on_term": "asserted directly — SIGTERM is the healthy "
        "handover and the one most likely to be misread as a crash",
    "bin/llm_chat:checkout_dirty": "dirty, clean, not-a-checkout, no-git and "
        "a tree that is not its OWN checkout asserted directly — UNKNOWN must "
        "not read as clean, and a copy's enclosing repo must not read as it",
    "bin/llm_chat:__call__": "AccumulateNames and RefuseRepeat — accumulation, the comma form, mixing, "
        "dedup, order, whitespace and the no-flag sentinel asserted directly — "
        "a dropped addressee is the failure being fixed",
    "bin/llm_chat:shared_name_note": "ambiguous, unique, same-project-twice, "
        "host-unaskable, both sentinels and multi-name asserted directly — a "
        "line that fires on every message is one nobody reads",
    "bin/llm_chat:bridge_for": "all five verdicts asserted directly, "
        "including that NO RECORD is not STOPPED and that a heartbeat for "
        "another room does not vouch for this one",
    "bin/llm_chat:bridge_note": "every state's wording asserted directly, "
        "including that a LIVE bridge prints nothing — a line that fires for "
        "every human room is one the reader learns to skip",
    "bin/llm_chat:human_age": "minutes, hours, days and None asserted "
        "directly; the wrong unit is not a rounding error when somebody is "
        "deciding whether their escalation reached a person",
    "bin/llm-chat-slack:beat": "contents, directory creation and the "
        "unwritable case asserted directly — a diagnostic that can take down "
        "the bridge it describes is worse than no diagnostic",
    "bin/llm_chat:server_bind": "wide, loopback, nothing-listening, remote, "
        "no-lsof and unparseable asserted directly — the four bind shapes "
        "against REAL sockets, because a stub of lsof's format proves nothing "
        "when the format is what the first version got wrong",
    "bin/llm_chat:serve_command": "the bind flag and the port asserted "
        "directly, plus a scan proving no doc or script spells the command "
        "without a bind — the prose copies are where the wide one survived",
    "bin/llm_chat:own_checkout": "own, copy, no-repo and symlinked-own "
        "asserted directly against REAL git, because the defect it exists for "
        "was what git actually answers and no stub could have caught it",
    "bin/llm_chat:do_mode": "both directions, both refusals and the passive "
        "notice asserted directly; the guards whose absence is silent are swept",
    "bin/llm_chat:do_sync": "asserted directly, including that it writes LOCAL "
        "state and that a project with no identity is an opt-out not an error",
    "bin/llm_chat:crowded_room_hint": "member thresholds and suppression "
        "asserted directly; the fire-once guard is swept",
    "bin/llm-chat-wake:sync_broadcasts": "asserted directly, including that a "
        "failure is swallowed — it is the least important thing the waker does",

    # The audience feature. What is swept is every guard whose absence is
    # SILENT — a wrong wake is noticed immediately, a missing one never is.
    "bin/llm_chat:audience_for": "every flag, combination and refusal asserted "
        "directly, including a sentinel passed as a name",
    "bin/llm_chat:describe_audience": "every branch asserted directly",
    "bin/llm_chat:say_reach": "woken, passive and empty-room asserted directly",
    "bin/llm_chat:do_pending": "asserted directly, including that a second "
        "call returns the same answer — which is what proves it consumed nothing",
    "bin/llm-chat-wake:addressed": "asserted directly that it calls `pending` "
        "and never `read`, and that an outage is a non-answer rather than a wake",
    "bin/llm-chat-wake:who_addressed": "asserted directly",
    "bin/llm-chat-slack:route": "all three cases asserted directly, plus "
        "Slack's <!here> encoding, precedence over threads, and that an email "
        "address is not a mention",
    "bin/llm-chat-slack:read_threads": "missing and corrupt asserted directly",
    "bin/llm-chat-slack:remember_thread": "asserted directly, including the "
        "bound and that the OLDEST entries are the ones dropped",

    "bin/llm_chat:install_report": "both states asserted directly, including "
        "that they are not confusable — 'NOT INSTALLED' contains 'INSTALLED', "
        "and a substring grep during an earlier version of this work matched "
        "a temp dir named skilltest and reported the feature present when it "
        "was absent",
    "bin/llm_chat:invite": "asserted directly that it points at verification "
        "rather than installation, and still carries the commands",
    "bin/llm_chat:render_briefing": "attribution, fencing, the empty case and "
        "a hostile briefing all asserted directly",
    "bin/llm_chat:do_briefing": "every branch asserted directly, including "
        "that an oversized briefing is refused without partially writing",

    "triggers/authority-gate:asks_permission": "asserted directly against "
        "three VERBATIM escalations from this session and four real reports "
        "that must stay quiet — both directions measured, not imagined",
    "triggers/authority-gate:last_assistant_text": "every shape asserted "
        "directly — blocks, tool-only turns, corrupt lines, missing file",
    "triggers/authority-gate:already_judged": "asserted directly, including "
        "that an unwritable dir suppresses rather than loops",
    "triggers/authority-gate:objection": "asserted directly — it must quote "
        "the phrase and pose the theirs-or-yours question",
    "triggers/authority-gate:main": "every branch asserted directly",

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

    # The MCP server (bin/llm-chat-mcp). Everything CLI-facing is behind one
    # seam, run_cli, and asserted directly against a fake exactly the way the
    # Slack bridge's _call is above; nothing here is network- or process-
    # facing enough to need a live mutation instead of a precise assertion.
    "bin/llm-chat-mcp:run_cli": "every branch — stdin=DEVNULL so `--file -` "
        "cannot hang or race this server's own stdin, timeout handling, and "
        "stdout/stderr ordering — asserted directly against a faked "
        "subprocess module",
    "bin/llm-chat-mcp:dispatch": "every branch — notification silence "
        "regardless of method, unknown method, ToolError, a generic "
        "exception, and a successful call — asserted directly",
    "bin/llm-chat-mcp:handle_initialize": "asserted directly, including the "
        "protocol-version fallback",
    "bin/llm-chat-mcp:handle_ping": "trivial, asserted directly",
    "bin/llm-chat-mcp:handle_tools_list": "asserted directly, including that "
        "every listed tool carries a description and an object schema",
    "bin/llm-chat-mcp:handle_tools_call": "every branch — unknown tool, a "
        "missing required argument, success, a non-zero exit, empty output, "
        "and the server flag threaded before the subcommand — asserted "
        "directly",
    "bin/llm-chat-mcp:main": "every branch — one response per request, "
        "notifications silenced, blank lines skipped, unparseable JSON and "
        "non-dict frames ignored rather than fatal — asserted directly",
    "bin/llm-chat-mcp:_require": "both the raise and the pass-through "
        "asserted directly",
    "bin/llm-chat-mcp:_server_argv": "asserted directly, with and without a "
        "server override",
    "bin/llm-chat-mcp:_as_flag": "asserted directly by every builder test "
        "that does and does not pass identity",
    "bin/llm-chat-mcp:_topic_flag": "asserted directly by every builder test "
        "that does and does not pass topic",
    "bin/llm-chat-mcp:_max_messages_flag": "asserted directly by every "
        "builder test that does and does not pass max_messages",
    "bin/llm-chat-mcp:_error": "asserted directly by every test that checks "
        "an error response's shape",
    "bin/llm-chat-mcp:_result": "asserted directly by every successful "
        "dispatch",
    "bin/llm-chat-mcp:_build_open": "every branch, with and without every "
        "optional field, asserted directly for the exact argv produced",
    "bin/llm-chat-mcp:_build_join": "asserted directly for the exact argv "
        "produced with every optional field set",
    "bin/llm-chat-mcp:_build_setup": "asserted directly for the exact argv "
        "produced with every optional field set",
    "bin/llm-chat-mcp:_build_say": "every branch — text, file, to, to_all, "
        "to_none — asserted directly for the exact argv produced",
    "bin/llm-chat-mcp:_build_sync": "trivial, asserted directly (constant "
        "argv)",
    "bin/llm-chat-mcp:_build_mode": "both branches asserted directly for "
        "the exact argv produced",
    "bin/llm-chat-mcp:_build_pending": "asserted directly for the exact "
        "argv produced",
    "bin/llm-chat-mcp:_build_read": "every branch asserted directly for "
        "the exact argv produced",
    "bin/llm_chat:session_id": "a one-line env read, asserted through every "
        "scoping test — both halves of the split are exercised by setting and "
        "clearing the variable explicitly",
    "bin/llm_chat:project_state_dir": "the pre-session location, asserted "
        "directly by the migration tests; a constant path with no branch",
    "bin/llm_chat:project_identity_file": "asserted directly — a session "
        "keeps its own name over a project one, and inherits it when it has "
        "none",
    "bin/llm-chat-slack:now":"a one-line seam over time.time(), swapped in "
        "tests so the age and recheck bounds are assertable without sleeping; "
        "mutating it asserts nothing about behaviour",
    "bin/llm-chat-slack:read_asked": "asserted through the outbound threading "
        "tests — an answer landing in the right thread IS this file being "
        "read; the bound is asserted directly",
    "bin/llm-chat-slack:write_asked": "asserted directly for the bound, and "
        "through every outbound test for the round trip",
    "bin/llm-chat-wake:note_rewake": "asserted directly — the note exists "
        "after a rewake is requested, and its absence is what makes the "
        "landing check say nothing",
    "bin/llm-chat-wake:wake_landing": "every branch asserted directly — "
        "fresh, stale, absent, corrupt and unwritable; the stale case is "
        "additionally swept, being the one that could restore the false green",
    "bin/llm_chat:wake_landing": "asserted directly — present, absent and "
        "corrupt, plus that it names the host when it cannot confirm",
    "bin/llm-chat-slack:read_reply_state":"asserted through the relay tests "
        "— a reply arriving once and only once IS this file being read and "
        "written correctly; the bound is asserted directly",
    "bin/llm-chat-slack:write_reply_state": "asserted directly for the bound, "
        "and through every relay test for the round trip",
    "bin/llm-chat-slack:replies": "asserted directly against the HTTP seam — "
        "the endpoint, that it is a query rather than a body, and the ts",
    "triggers/answer-when-asked:refusal": "asserted directly — it must name "
        "the room, the asker, the question and the exact say command, because "
        "a gate that only says 'you owe something' sends the agent looking, "
        "and the looking is where the turn gets abandoned",
    "triggers/answer-when-asked:owed": "every outcome asserted directly — "
        "crash, unparseable, and the exit code passed through unchanged",
    "bin/llm-chat-mcp:_build_owed": "asserted directly for the exact argv, "
        "with and without --json; the CLI-correspondence tests additionally "
        "prove every flag it emits is one the parser accepts",
    "bin/llm-chat-mcp:_build_delete":"asserted directly for the exact argv, "
        "with and without --yes; the CLI-correspondence tests additionally "
        "prove the flag it emits is one this parser accepts",
    "bin/llm-chat-mcp:_build_leave":"both branches asserted directly for "
        "the exact argv produced",
    "bin/llm-chat-mcp:_build_close": "asserted directly for the exact argv "
        "produced, with and without an identity, and that a missing reason is "
        "refused before the CLI is reached; the CLI-correspondence tests "
        "additionally prove every flag it emits is one the parser accepts",
    "bin/llm-chat-mcp:_build_reopen": "both branches asserted directly for "
        "the exact argv produced",
    "bin/llm-chat-mcp:_build_invite": "trivial, asserted directly",
    "bin/llm-chat-mcp:_build_channels": "both flags, and their absence, "
        "asserted directly for the exact argv produced",
    "bin/llm-chat-mcp:_build_briefing": "both branches — text, file — "
        "asserted directly for the exact argv produced",
    "bin/llm-chat-mcp:_build_identify": "asserted directly for the exact "
        "argv produced",
    "bin/llm-chat-mcp:_build_doctor": "trivial, asserted directly (constant "
        "argv)",
    "bin/llm-chat-deliver:other_checkouts": "every branch asserted directly — "
        "a copy found, the hooks' OWN tree excluded (with ROOT moved inside "
        "the project, or the walk could never reach it and the test would "
        "pass for the wrong reason), no descent into a checkout already "
        "found, the depth limit, and the skipped directories",
    "bin/llm-chat-deliver:fingerprint_of": "asserted directly in all three "
        "outcomes — the argv it asks the CLI for, a raising subprocess, and "
        "an empty answer — and the one thing that could go wrong silently, "
        "a failure read as a hash, is swept where it bites in "
        "divergent_checkouts",
    "bin/llm_chat:human_wait": "seconds, minutes, zero, negative, an HTTP-date "
        "and an empty header asserted directly — None is the signal to say the "
        "window is UNKNOWN rather than invent a phrase for it",
    "bin/llm_chat:throttled_advice": "both branches asserted directly, and "
        "asserted to be OPPOSITE — the hook's message is reassurance that "
        "something else will collect this, the direct caller's is a warning "
        "that nothing will and nothing was written. The branch it turns on is "
        "swept in retry_waits, which reads the same variable",
    "bin/llm_chat:skill_checkout": "all three outcomes asserted directly — a "
        "path read out of the skill TEXT, a file naming no checkout, and no "
        "file at all — and the thing that could go wrong silently, naming the "
        "wrong tree, is swept where it bites in divergent_skill_report. Not "
        "swept itself because every mutation available to it raises rather "
        "than measures: dropping the `if found else None` IndexErrors on a "
        "file with no command in it, which is a crash and not a verdict",
    "bin/llm_chat:_read_json": "a two-line reader whose only decision is that "
        "absent, unparseable and not-a-dict all become None — asserted "
        "through every caller that has a corrupt-file test, and the callers "
        "are where getting it wrong would show",
    # THROUGH ITS CALLER, not directly, and the wording matters because these
    # reasons are the only record of WHY a function is not swept. This one
    # said "asserted directly" the day it was written; the tests go through
    # `live_identities`. showrunner's point in #learnings: an exclusion is a
    # claim, and a claim written once and checked never is the thing that
    # rots. Corrected the same week rather than left to be believed.
    "bin/llm_chat:session_attributions": "every kind of evidence asserted "
        "through live_identities, which is its only caller — declared, "
        "per-room, both together, and the ORDER — and the two rules it "
        "enforces are swept where they bite, in live_identities",
    "bin/llm-chat-mcp:_build_who": "both branches asserted directly for the "
        "exact argv produced; the CLI-correspondence tests additionally prove "
        "the flag it emits is one this parser accepts",
    "bin/llm-chat-mcp:_build_fingerprint": "both branches asserted directly "
        "for the exact argv produced",
    "bin/llm-chat-mcp:_build_reload": "both branches asserted directly for "
        "the exact argv produced",

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
    """Fail on any candidate that is neither swept nor explicitly excluded.

    THE PUBLISHED NUMBERS RECONCILE, and they did not. `excluded` was
    intersected with the candidate set and `swept` was not, so the line read
    `candidates 293 — swept 115, excluded 222` and 115 + 222 = 337. `swept`
    was counting mutations aimed at functions outside the denominator —
    nested helpers, and the entrypoints' own module-level code — which are
    real mutations but not candidates.

    Reported by an agent who ran the whole sweep on another machine to check
    #22 from the outside, read the line, assumed an accounting bug, and went
    to the source before filing. It is only a display defect and
    `unaccounted 0` was never affected — the subtraction that produces it
    deliberately uses the UNintersected set, which is correct, since a
    mutation covers its target wherever that target lives.

    But it lands in the one tool whose entire claim is that its numbers can be
    trusted, three lines from a docstring about the hazard of reporting a
    clean number over a set that had quietly stopped containing the thing. A
    summary whose own arithmetic does not close invites a reader to distrust
    the figure it most wants believed.

    So both are printed: the part of `swept` inside the denominator, which
    adds up, and the total, which is the true number of mutations.

    AND IT STILL DID NOT ADD UP, for a second reason the first fix did not
    look for. Intersecting `swept` with the denominator fixed one overcount
    and left another: the two sets OVERLAP. 47 candidates are swept AND carry
    a NOT_SWEPT reason — entries whose reason reads "asserted directly", kept
    deliberately as a note that the function is covered twice over. Adding the
    two sets counts each of those once each. The line went on reading
    `125 + 225 + 0 = 303`, whose left side is 350.

    A partition has to be DISJOINT to be summed, which is the thing neither
    version checked. So the middle term is now "excluded AND NOT swept", the
    overlap is reported on its own line rather than folded into either, and
    the closing sum is ASSERTED rather than printed — `report_unaccounted`
    fails if its own arithmetic does not close. The first fix printed a sum
    and trusted it; a number that is only displayed cannot notice it is
    wrong, and this is the one tool whose whole claim is that its numbers can
    be trusted.
    """
    everything = set(candidates())
    swept = swept_functions()
    excluded = set(NOT_SWEPT)
    unaccounted = sorted(everything - swept - excluded)
    here = len(everything & swept)
    # DISJOINT BY CONSTRUCTION: swept, then excluded-and-not-swept, then what
    # neither claims. Subtracting `swept` in the middle term is what makes the
    # three sum to the denominator.
    only_excluded = len(everything & excluded - swept)
    both = len(everything & excluded & swept)
    print("\ncandidates %d — swept %d, excluded with a reason and NOT swept "
          "%d,\nunaccounted %d  (%d + %d + %d = %d)"
          % (len(everything), here, only_excluded, len(unaccounted),
             here, only_excluded, len(unaccounted),
             here + only_excluded + len(unaccounted)))
    if here + only_excluded + len(unaccounted) != len(everything):
        print("\nTHIS TOOL'S OWN ARITHMETIC DOES NOT CLOSE: the three groups "
              "above sum to\n%d, and there are %d candidates. They are meant "
              "to partition the set, so\none of them is counting something "
              "twice or missing something. Nothing else\nhere is trustworthy "
              "until that is explained."
              % (here + only_excluded + len(unaccounted), len(everything)),
              file=sys.stderr)
        return True
    if both:
        print("  %d of the excluded also carry a mutation — their reason says "
              "the\n  behaviour is asserted directly, and the mutation was "
              "kept anyway. Counted\n  under `swept` above, not twice." % both)
    outside = len(swept) - here
    if outside:
        print("  %d further mutation(s) target functions outside that set — "
              "nested\n  helpers and module-level code, which are swept but "
              "are not candidates." % outside)
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


# unittest's own summary line: `FAILED (failures=2, errors=1)`, either half
# optional. Parsed rather than recounted, because it is the runner's own
# arithmetic and a second implementation of it would be a second thing to be
# wrong.
VERDICT = re.compile(r"(failures|errors)=(\d+)")


def run_suite():
    """(green, failures, errors, which tests failed) — not just green.

    The fourth element is what lets `sweep_verdict` ask a smaller question
    next time: the ids recorded here are the tests that actually killed this
    mutation, so the next sweep can run those instead of all 1827.

    A MUTANT KILLED BY AN EXCEPTION IS NOT A MEASUREMENT. Reading only the
    exit code says a reverted fix "turned the suite red" when all that
    happened was a crash: the assertions meant to measure the behaviour never
    ran, and a crash proves the line is load-bearing, not that anything
    watches what it does.

    Proved against this harness before it was changed — a mutation whose whole
    body was `raise RuntimeError` was reported CAUGHT — something already
    defends this. Nothing did.

    showrunner's finding, arriving in #learnings; their version took down a
    whole test group and read as thin coverage. Same root, different symptom,
    and their remedy is the one adopted: refuse to print a verdict beside a
    run that crashed.
    """
    # A DEADLINE, because a mutation can make the suite HANG rather than fail
    # — a socket that never gets its ring, a retry loop with no ceiling — and
    # a hung shard stalls the whole sweep forever. Two shards sat at 46
    # minutes on a run whose others finished in twelve. Reported as its own
    # verdict below: a hang measures nothing, exactly like a crash, and
    # waiting for it measures nothing either.
    try:
        argv = [sys.executable, os.path.join(HERE, "run.py"), "--tests-only",
                "--name-failures"]
        done = subprocess.run(argv,
                              cwd=ROOT, capture_output=True, text=True,
                              timeout=SUITE_DEADLINE)
    except subprocess.TimeoutExpired:
        return None, 0, 0, []
    text = (done.stdout or "") + (done.stderr or "")
    counts = {"failures": 0, "errors": 0}
    for kind, n in VERDICT.findall(text):
        counts[kind] = int(n)
    return (done.returncode == 0, counts["failures"], counts["errors"],
            failing_ids(text))


# The one-line marker `run.py` prints its failing test ids on. Defined HERE
# because run.py already imports from this module and the reverse would be a
# cycle — and because two copies of a marker string is two things to get out
# of step, which is the defect this file exists to find.
FAILED_IDS = "FAILED-IDS: "

KILLERS = os.path.join(HERE, "killers.json")
KILLER_DEADLINE = 120


def killers_dir():
    """The LIVE tree's test/, not whatever copy this shard is running in."""
    live = os.environ.get(LIVE_ROOT)
    return os.path.join(live, "test") if live else HERE


def killers_path():
    """Where THIS worker writes what it learned.

    ONE FILE PER SHARD, and it has to be. Eight workers each learn the killers
    for their own 28 mutations; a single shared file means eight writers
    replacing each other and seven-eighths of the run's findings thrown away —
    so the next sweep would still run the whole suite for most mutations and
    the speedup would look like it had not worked.

    Separate files instead of a lock: they are written once each, at the end,
    and `read_killers` merges them. Nothing needs to coordinate.
    """
    share = os.environ.get(SHARD)
    index = share.split("/", 1)[0] if share else None
    name = "killers.%s.json" % index if index else "killers.json"
    return os.path.join(killers_dir(), name)


def read_killers():
    """Every worker's ledger, merged. Unreadable ones are simply absent.

    A missing or corrupt file is not an error here: the mutations it covered
    escalate to the full suite and rewrite it. This cache can only ever cost
    time, never correctness, which is why nothing about it is defended
    harder than this.
    """
    merged = {}
    directory = killers_dir()
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return merged
    for name in names:
        if not (name == "killers.json" or (name.startswith("killers.")
                                           and name.endswith(".json"))):
            continue
        try:
            with open(os.path.join(directory, name)) as f:
                found = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(found, dict):
            merged.update(found)
    return merged


def ask_the_killers(name, known):
    """Did this mutation's KNOWN killers fail? True only if one of them did.

    THE WHOLE DEV-CYCLE FIX. A sweep asks one question per mutation — does
    anything notice — and it was answering it by running 1737 tests, 223
    times. But a mutation that was killed by `test_cli.WhoTest.test_x`
    yesterday is almost certainly killed by the same test today, and that
    test takes milliseconds.

    SOUND FOR THE SAME REASON THE REDUCED SUITE IS. A named killer that fails
    is a real measurement: an assertion looked and disagreed. Running fewer
    tests can only MISS a kill, never invent one — so a False here means
    "ask the bigger question", not "survivor". The caller escalates.

    RECORDED FROM REAL KILLS, NEVER GUESSED, which is what keeps it honest as
    the tests move. A renamed or deleted killer simply does not fail, the
    mutation escalates to the full suite, and the ledger is rewritten from
    what actually killed it this time. Staleness costs one slow run and
    repairs itself; it cannot produce a wrong verdict.
    """
    if not known:
        return False
    try:
        done = subprocess.run(
            [sys.executable, "-m", "unittest", "-v"] + list(known),
            cwd=HERE, capture_output=True, text=True,
            timeout=KILLER_DEADLINE)
    except (subprocess.TimeoutExpired, OSError):
        return False
    text = (done.stdout or "") + (done.stderr or "")
    counts = dict((kind, int(n)) for kind, n in VERDICT.findall(text))
    # FAILURES, not errors, and not the exit code. A killer that ERRORED
    # crashed on the way to its assertion, which measures nothing — the
    # distinction this whole file is built on.
    return counts.get("failures", 0) > 0


def note_killers(name, ids, ledger):
    if ids and ledger.get(name) != ids:
        ledger[name] = ids
        return True
    return False


def save_killers(ledger):
    path = killers_path()
    try:
        tmp = path + ".%d.tmp" % os.getpid()
        with open(tmp, "w") as f:
            json.dump(ledger, f, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        pass          # a lost ledger costs speed, never correctness


def failing_ids(output):
    for line in (output or "").splitlines():
        if line.startswith(FAILED_IDS):
            try:
                found = json.loads(line[len(FAILED_IDS):])
                return found if isinstance(found, list) else []
            except ValueError:
                return []
    return []


def sweep_verdict(baseline, path, original, find, replace, name, ledger):
    """One mutation's verdict: ask the tests that killed it last time, first.

    THE DEV CYCLE WAS THE PROBLEM. Gating a two-file change took twenty
    minutes, because this sweep asks one question per mutation — does anything
    notice — and it answered by running 1827 tests, 223 times. But a mutation
    killed by `test_cli.WhoTest.test_x` yesterday is almost certainly killed
    by the same test today, and that test takes milliseconds.

    SOUND, because a named killer that fails is a real measurement: an
    assertion looked and disagreed. Running fewer tests can only MISS a kill,
    never invent one — so "the killers passed" means ask the whole suite, not
    "survivor". Escalation is always to the FULL suite, and the ledger is
    rewritten from whatever actually killed it there, so a renamed or deleted
    killer repairs itself at the cost of one slow run.

    A REDUCED SUITE WAS TRIED HERE FIRST AND MEASURED WORSE. The idea was to
    drop the two REAL-WORLD modules — 30 of the suite's 59 seconds — and
    re-run anything that did not die against the full suite. Sound, and
    slower: on a cold ledger the reduced run is pure overhead for every
    mutation that then escalates, and each escalation cost three suite runs
    instead of one. Measured on this repo: 57 of 223 mutations in 40 minutes,
    against ~20 minutes for the whole sweep before the change. Deleted rather
    than tuned — the win was never in the suite's size, it was in not running
    a suite at all.
    """
    # ASK THE NAMED KILLERS FIRST — milliseconds, and it answers most of the
    # time. A killer that still fails is a real measurement; one that does not
    # means "ask the bigger question", never "survivor".
    if ask_the_killers(name, ledger.get(name)):
        return "measured", "its known killer(s) disagreed"

    after = run_suite()
    verdict, why = killed_by_measurement(baseline, after)
    if verdict == "measured":
        note_killers(name, after[3], ledger)
    return verdict, why


def killed_by_measurement(before, after):
    """Did the mutation turn the suite red by being SEEN, or by crashing?

    (verdict, why) where verdict is "measured", "crashed" or "survived".

    New FAILURES mean an assertion looked at the behaviour and disagreed —
    that is a measurement. New ERRORS with no new failures mean something blew
    up on the way, and whatever was going to check the behaviour may never
    have run. The second is not a pass and is not a fail; it is an unmeasured
    result, and printing a kill count beside it is the lie this exists to
    stop.
    """
    # SLICED, not unpacked. `run_suite` also hands back WHICH tests failed, so
    # the sweep can ask them directly next time instead of asking 1737 tests
    # the same question — and a verdict is still a delta over the counts only.
    _, failures_before, errors_before = before[:3]
    green, failures, errors = after[:3]
    if green is None:
        return "hung", ("the suite did not finish within %ds — a mutation "
                        "that HANGS measures nothing, and waiting for it "
                        "measures nothing either" % SUITE_DEADLINE)
    if green:
        return "survived", "the suite stayed green"
    if failures > failures_before:
        return "measured", "%d assertion(s) disagreed" % (
            failures - failures_before)
    if errors > errors_before:
        return "crashed", (
            "%d test(s) ERRORED and none FAILED — the suite went red by "
            "raising, not by measuring, so whatever was going to check this "
            "may never have run" % (errors - errors_before))
    return "crashed", ("the suite went red without a new failure or error "
                       "this could attribute")


def probe(relative, old, new):
    """Is this behaviour ALREADY defended? One command, before you build.

    The lesson this exists for: I found a rule stated in a docstring and
    violated at twenty sites, and started rewriting all twenty. It was already
    enforced — report_global_leaks() catches exactly that leak, is wired, and
    fires. A risky refactor for a rail that already existed.

    "Check whether it is already enforced" is a judgement, and judgements do
    not harden; writing it down would be the prose-dressed-as-a-rule failure
    it is trying to prevent. What IS checkable is whether a guard is load-
    bearing, and the tool for that is the same mutation the sweep uses — only
    run BEFORE building instead of after, when the answer can still change what
    you do.

    Three outcomes, never two, the same contract this project and a sibling
    both converged on independently:

        0  CAUGHT     something already defends this. Do not build.
        1  SURVIVED   nothing does. Build it, then add a permanent mutation.
        2  NO ANCHOR  the text is not there — you are probing a behaviour that
                      does not exist yet, which is not the same as undefended.
    """
    path = os.path.join(ROOT, relative)
    try:
        with open(path) as f:
            original = f.read()
    except OSError as problem:
        print("cannot read %s: %s" % (relative, problem))
        return 2
    if old not in original:
        print("NO ANCHOR — %r is not in %s.\n"
              "  Nothing was mutated, so a passing suite here would mean "
              "nothing.\n  Probe text that exists, or accept that the "
              "behaviour is not written yet." % (old[:60], relative))
        return 2
    if original.count(old) > 1:
        print("AMBIGUOUS — %r appears %d times in %s.\n"
              "  Be more specific, or the mutation is not the one you meant."
              % (old[:60], original.count(old), relative))
        return 2
    # HELD FOR THE SAME REASON THE SWEEP HOLDS IT: while this lock is taken,
    # run.py knows a mutation in the tree is expected rather than stranded.
    # Without it the stranded check refuses to run the suite, returns 1, and
    # this reads that as CAUGHT — which is how the sweep came to report 133
    # defended behaviours while executing no tests at all.
    _held = sole_sweep()
    stat = os.stat(path)
    # THE CLEAN RUN IS THE CONTROL. Without it a suite that already has a
    # failure would make every mutation look measured, and this tool would be
    # confidently wrong in the direction of "do not build".
    before = run_suite()
    # BAIL BEFORE MUTATING, not after. This check used to sit below the second
    # run, so an already-red suite cost a whole extra suite AND put a
    # deliberately broken program into a tree five other agents execute by
    # absolute path — for a verdict already known to be unattributable. The
    # comment eight lines up says that window is the hazard this file works
    # hardest to keep short; the code opened it for no information at all.
    # Found by writing the first test that ever called this function.
    if not before[0]:
        print("CANNOT TELL — the suite was ALREADY red before this mutation "
              "(%d failed, %d errored).\n  Nothing here can be attributed. "
              "Fix the suite first." % (before[1], before[2]))
        return 2
    try:
        with open(path, "w") as f:
            f.write(original.replace(old, new, 1))
        after = run_suite()
    finally:
        with open(path, "w") as f:
            f.write(original)
        os.utime(path, (stat.st_atime, stat.st_mtime))
    verdict, why = killed_by_measurement(before, after)
    if verdict == "survived":
        print("SURVIVED — nothing defends this. Build the guard, then add a "
              "mutation\n  so it stays defended.")
        return 1
    if verdict == "crashed":
        print("CRASHED, NOT MEASURED — %s.\n"
              "  The suite went red, so the old version of this tool would "
              "have said CAUGHT.\n  It is not: a crash proves the line is "
              "load-bearing, not that anything\n  watches what it DOES. "
              "Whatever was going to check it may never have run.\n\n"
              "  Write the assertion so it FAILS rather than raises — "
              "`(x or {}).get(\"k\")`\n  instead of `x[\"k\"]` — then probe "
              "again. Until then this told you nothing." % why)
        return 2
    print("CAUGHT — something already defends this, and %s. Find out WHAT "
          "before\n  building anything; a second rail for a rule that has one "
          "is a risky\n  refactor for nothing." % why)
    return 0


def red_suite_refusal(baseline):
    """Why the sweep will not run, INCLUDING which tests were already failing.

    The refusal itself is right: a sweep measured against a red suite cannot
    attribute anything to a mutation. It was also unactionable, because it
    printed the two counts and dropped `baseline[3]` — the list of tests that
    failed, which `run_suite` already returns and whose docstring says it
    exists so a later run can ask a smaller question.

    The nightly ran red five nights with this exact message and no name, on a
    macOS runner, while the ubuntu `tests` workflow stayed green on the same
    commit. So the subject is a test that skips on ubuntu and runs there — and
    nobody could tell which, because the one instrument that had the answer
    printed a count instead.

    Not reproducible from outside CI: green here, green cold-cloned, green with
    an empty HOME, with no DART_SDK, with `claude` off PATH, and with all of
    those at once. When a failure exists only on the runner, the runner's own
    output is the only instrument there is.

    NO NAMES IS ITS OWN REPORTED STATE rather than an empty list rendered as
    nothing: a suite that failed without saying what is a different fact from
    one that named its failures, and collapsing them is the shape this repo
    keeps removing.
    """
    said = ["REFUSING TO SWEEP: the suite is already red (%d failed, %d "
            "errored).\nNothing measured against it could be attributed to a "
            "mutation." % (baseline[1], baseline[2])]
    failed = baseline[3] if len(baseline) > 3 else None
    if failed:
        said.append("Already failing BEFORE any mutation was applied:")
        said.extend("    %s" % test for test in failed)
    else:
        said.append("It did not report WHICH tests — so this run cannot say, "
                    "and that is its own defect.")
    return "\n".join(said)


def sweep_exit_code(crashed, survivors, share):
    """The sweep's verdict, as a function, because as a branch it never ran.

    A CEILING, NOT A PASS. Thirteen mutations were killing their tests by
    raising rather than by being measured — a debt that existed invisibly for
    as long as the sweep was reporting `caught` about a suite it never ran.
    Blocking every commit until all of them are rewritten would be a gate
    nobody can satisfy, and a gate nobody can satisfy gets switched off;
    letting them pass silently is how they got here. So: they are named, they
    do not count as caught, and the number may only go DOWN.

    THE CEILING USED TO BE GUARDED BY `if crashed and not share:` AND SO NEVER
    FIRED IN PRODUCTION. `sweep_in_a_copy` sets SHARD on every worker it
    spawns, and the sweep always runs through it, so `not share` was false in
    every process that ever evaluated the condition. The only caller reaching
    it was the test suite, which calls into this with no SHARD set. The
    result: 100% line coverage, `verify: all owed checks passed ✓`, and one
    CRASHED mutation named in the output of all three sweeps of the run that
    finally caught it. A guard whose only caller is its own test is not a
    guard — and coverage cannot tell the two apart, because the line really
    was executed.

    It is a FUNCTION now for that reason and not for tidiness. The decision
    was unreachable from a test at the value of `share` production uses, so
    the only thing that could have caught this was reading the log of a
    twenty-minute sweep closely enough to notice the exit code disagreed with
    the report. That is not a check.

    WHAT THE SHARDED CHECK STILL MISSES, stated because a share sees its own
    crashes and not the whole list: comparing a SHARE's count against the
    global ceiling can only under-report. `share_crashed > CEILING` implies
    `total_crashed > CEILING`, so this never fails a run that should pass —
    but with a ceiling above 0, crashes spread thinly across eight shards
    could each sit under it and total more. At CEILING = 0 the two are
    identical, which is the only reason this is sufficient today. Raising the
    ceiling means aggregating in the parent before comparing.
    """
    if len(crashed) > CRASHED_CEILING:
        print("\n%d mutations CRASHED, and the ceiling is %d. A NEW "
              "behaviour is being\nmeasured by an exception rather than "
              "by an assertion — fix it here rather\nthan raising the "
              "number." % (len(crashed), CRASHED_CEILING))
        return 1
    if crashed and not share:
        print("\n(%d of a permitted %d — this number may only go down.)"
              % (len(crashed), CRASHED_CEILING))
    if survivors:
        return 1
    # REACHED ONLY WHEN NOTHING CRASHED, and that is the early return above
    # doing it rather than a condition here. This sentence used to print
    # unconditionally, six lines below a report naming a mutation that was NOT
    # caught by an assertion — the skimmable summary saying the opposite of
    # the detail, and the detail is the half that gets scrolled past. Both
    # were symptoms of one cause: the ceiling did not fire in a shard, so
    # control carried on past it.
    #
    # A SECOND GUARD HERE WOULD BE DEAD CODE AT CRASHED_CEILING = 0. I wrote
    # one, and the mutation that reverts it SURVIVED — not because nothing
    # defends it but because nothing can reach it. The warning that belongs
    # here instead is at the constant, where someone would raise it.
    print("Every reverted fix in this share was caught BY A FAILING "
          "ASSERTION.")
    # THE ACCOUNTING IS A PROPERTY OF THE WHOLE LIST, not of a share, so one
    # worker owns it. Printed by every shard it would be eight identical
    # reports of the same set, and eight chances to read a repeat as a
    # confirmation.
    if share and not share.startswith("0/"):
        return 0
    return 1 if report_unaccounted() else 0


def main():
    if "--probe" in sys.argv:
        ap = argparse.ArgumentParser(prog="mutate.py --probe")
        ap.add_argument("--probe", required=True, help="file to mutate")
        ap.add_argument("--old", required=True)
        ap.add_argument("--new", required=True)
        args = ap.parse_args()
        return probe(args.probe, args.old, args.new)

    # ISOLATE FIRST. Everything below mutates source files, and until this
    # returns we are standing in a tree five other agents execute.
    if os.environ.get(IN_COPY) != "1":
        return sweep_in_a_copy()

    _lock = sole_sweep()   # held for the life of the process
    mine = my_share(MUTATIONS)
    share = os.environ.get(SHARD)
    print("Reverting %d shipped fixes%s; each must turn the suite red BY "
          "FAILING AN\nASSERTION. A crash is not a measurement.\n"
          % (len(mine), "" if not share else " (shard %s of %d total)"
             % (share, len(MUTATIONS))))
    # THE CONTROL, taken once. Every verdict below is a diff against it, so a
    # suite that is already red cannot make every mutation look measured.
    #
    # IT MUST MATCH the runs it is compared against — a delta is only
    # meaningful between two readings of the same suite.
    baseline = run_suite()
    ledger = read_killers()
    if not baseline[0]:
        # NAME THEM. This printed the two counts and dropped `baseline[3]`,
        # which is the list of tests that failed — a value `run_suite` already
        # returns, and whose own docstring says it exists so a later run can
        # ask a smaller question.
        #
        # The refusal is correct: a sweep measured against a red suite
        # attributes nothing. It was also unactionable. The nightly ran red
        # five nights running with this exact message and no name, on a macOS
        # runner, while the ubuntu `tests` workflow stayed green on the same
        # commit — so the subject is a test that skips on ubuntu and runs
        # there, and nobody could tell which without a name.
        #
        # Not reproducible from outside CI either: green here, green
        # cold-cloned, green with an empty HOME, with no DART_SDK, with
        # `claude` off PATH, and with all of those at once. When a failure
        # only exists on the runner, the runner's own output is the only
        # instrument there is, and it was throwing the answer away.
        print(red_suite_refusal(baseline))
        return 1
    survivors, crashed = [], []
    for name, relative, find, replace, consequence in mine:
        path = os.path.join(ROOT, relative)
        with open(path) as f:
            original = f.read()
        # AMBIGUOUS IS NOT ACCEPTABLE HERE EITHER, and only `probe` used to
        # say so. The sweep replaced the FIRST match, so a mutation whose
        # anchor appeared three times quietly neutered a different function
        # than the one it named — and then reported `caught`, because the
        # place it actually hit was well defended. That is a mutation lying
        # about which behaviour it measured, which is worse than one that
        # fails to find its anchor at all.
        if original.count(find) > 1:
            print("  ?? %-38s AMBIGUOUS in %s (%d matches)"
                  % (name, relative, original.count(find)))
            survivors.append(
                (name, "anchor matches %d places — the sweep would mutate the "
                       "first, which may not be the one this names"
                 % original.count(find)))
            continue
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
            verdict, why = sweep_verdict(baseline, path, original,
                                         find, replace, name, ledger)
        finally:
            with open(path, "w") as f:
                f.write(original)
            os.utime(path, (stat.st_atime, stat.st_mtime))
        if verdict == "survived":
            print("  !! %-38s SURVIVED" % name)
            print("     %s" % consequence)
            survivors.append((name, consequence))
        elif verdict == "hung":
            # A HANG IS A SURVIVOR, not a crash. Nothing measured the
            # behaviour AND the sweep cannot finish, so it fails the run
            # rather than counting against the crash ceiling — the ceiling is
            # for known debt, and a hang is a new defect every time.
            print("  !! %-38s HUNG" % name)
            print("     %s" % why)
            survivors.append((name, why))
        elif verdict == "crashed":
            # NOT counted as caught. The suite went red, which the old version
            # read as proof; it is not. A crash says the line is load-bearing,
            # never that anything watches what it does.
            print("  ?? %-38s CRASHED, not measured" % name)
            print("     %s" % why)
            crashed.append((name, why))
        else:
            # SAY WHICH QUESTION ANSWERED IT. A cache that cannot be seen
            # working is one nobody can tell has stopped working — the whole
            # run would just get quietly slower again, and the only symptom
            # would be a number nobody is watching. `by name` means the
            # ledger answered in milliseconds; a bare `caught` means this
            # mutation cost a full suite and taught the ledger something.
            print("  ok %-38s caught%s"
                  % (name, " by name" if why.startswith("its known killer")
                     else ""))

    print()
    if crashed:
        print("%d mutation(s) CRASHED rather than being measured. The suite "
              "went red,\nso this used to print `caught` beside them — it is "
              "not the same claim.\nThe assertions meant to see the behaviour "
              "may never have run:" % len(crashed))
        for name, why in crashed:
            print("  ? %s: %s" % (name, why))
        print("\n  Remedy: make the assertion FAIL rather than raise — "
              "`(x or {}).get(\"k\")`\n  instead of `x[\"k\"]` — so a "
              "neutered producer flips it instead of\n  killing the test that "
              "was going to check it.")
    if survivors:
        print("%d mutation(s) SURVIVED — those behaviours are covered but not "
              "defended:" % len(survivors))
        for name, why in survivors:
            print("  - %s: %s" % (name, why))
    # EVERY WORKER WRITES, each to its own file, because each learned the
    # killers for a different 28 mutations. Letting shard 0 own this the way
    # it owns the accounting would discard seven-eighths of what the run
    # found — and the next sweep would look like the cache had not worked.
    save_killers(ledger)
    return sweep_exit_code(crashed, survivors, share)


if __name__ == "__main__":
    sys.exit(main())
