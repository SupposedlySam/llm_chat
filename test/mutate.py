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

    ("a doorbell is keyed by MEMBERSHIP, not identity", "bin/llm_chat",
     '    return "%s__%s.sock" % (channel, identity)',
     '    return "%s.sock" % identity',
     "four projects here answer to `owner`, so one waker binds the socket and "
     "the rest silently do not — then hear nothing, which looks like a quiet "
     "room rather than a fault"),

    ("a message rings the doorbells it wakes", "bin/llm_chat",
     '                 and ring(name, m)]',
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

    ("a dirty tree is never published", "triggers/lamp-publish",
     '    if dirty:\n        return ("uncommitted changes',
     '    if False:\n        return ("uncommitted changes',
     "a wish names a commit whose content nobody tested, and every consumer "
     "vendors it"),

    ("an unpushed commit is never published", "triggers/lamp-publish",
     '    if code == 0 and unpushed:',
     '    if False:',
     "the registry says a release is available and the consumer's fetch fails "
     "— worse than never blessing it at all"),

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

    "bin/llm_chat:installed_checkout": "present, absent, field-less and "
        "corrupt asserted directly — none may read as a guess",
    "bin/llm-chat-deliver:source_checkout": "recorded, absent and corrupt "
        "asserted directly via the notice that consumes it",
    "triggers/undocumented-surface:declared": "verbs, options and a missing "
        "source file asserted directly",
    "triggers/undocumented-surface:invented": "the reverse walk — backticks, "
        "fenced blocks, path-prefixed invocations, and the indented-prose "
        "false positive it produced on its first run, all asserted directly",
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

    "triggers/lamp-publish:geanie_for": "matched by path; missing, corrupt "
        "and unregistered asserted directly",
    "triggers/lamp-publish:git": "the one shell-out, asserted directly — "
        "which repo it runs in and what a failure returns",
    "triggers/lamp-publish:why_not": "every refusal asserted directly, paired "
        "with the case that MUST publish so a check that refuses everything "
        "cannot pass",
    "triggers/lamp-publish:main": "every branch asserted directly, including "
        "that a refusal is exit 0 and a gate refusal is exit 1",
    "triggers/lamp-publish:calling_repo": "both links asserted directly",

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
