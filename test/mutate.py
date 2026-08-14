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
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LOCK = os.path.join(HERE, ".mutate.lock")


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

    ("read --json emits JSON and nothing else", "bin/llm_chat",
     '    if as_json:\n',
     '    if False:\n',
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
     '    if not yes:',
     '    if False:',
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

    ("the leak detector is itself defended", "test/run.py",
     '    if not leaked:',
     '    if True:',
     "the rail that catches a test patching a shared module can break with "
     "nothing noticing — found by probing it, which is what the probe is for"),

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
     '    if get_membership(server, name, identity) is None:',
     '    if False:',
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

    ("a healthy doorbell is never stolen", "bin/llm-chat-wake",
     '            probe.connect(path)\n            probe.close()\n            return None',
     '            probe.connect(path)\n            probe.close()',
     "a second waker takes the socket from a live listener, so the first "
     "agent goes deaf while the second believes it is covering"),

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
     '    if missing:',
     '    if False:',
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

    ("CANNOT TELL is not permission to run", "bin/llm_chat",
     "        if quiet is None:",
     "        if False:",
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

    ("say checks the exit code", "bin/llm-chat-slack",
     "    if done.returncode != 0:",
     "    if False:",
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

    ("a failed member lookup is not an empty room", "bin/llm-chat-slack",
     "    if members:",
     "    if True:",
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

    ("a live waker is recorded where it destroys nothing",
     "bin/llm-chat-wake",
     "    record_alive(_polling_server())",
     '    record_exit("running")',
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
    "test/mutate.py:probe": "all three outcomes asserted by running it — "
        "caught, survived, no-anchor and ambiguous, exit codes read unpiped",
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
    "bin/llm_chat:checkout_dirty": "dirty, clean, not-a-checkout and no-git "
        "asserted directly — UNKNOWN must not read as clean",
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
    stat = os.stat(path)
    try:
        with open(path, "w") as f:
            f.write(original.replace(old, new, 1))
        still_green = run_suite()
    finally:
        with open(path, "w") as f:
            f.write(original)
        os.utime(path, (stat.st_atime, stat.st_mtime))
    if still_green:
        print("SURVIVED — nothing defends this. Build the guard, then add a "
              "mutation\n  so it stays defended.")
        return 1
    print("CAUGHT — something already defends this. Find out WHAT before "
          "building\n  anything; a second rail for a rule that has one is a "
          "risky refactor for nothing.")
    return 0


def main():
    if "--probe" in sys.argv:
        ap = argparse.ArgumentParser(prog="mutate.py --probe")
        ap.add_argument("--probe", required=True, help="file to mutate")
        ap.add_argument("--old", required=True)
        ap.add_argument("--new", required=True)
        args = ap.parse_args()
        return probe(args.probe, args.old, args.new)

    _lock = sole_sweep()   # held for the life of the process
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
