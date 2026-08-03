#!/usr/bin/env bash
# guard-writes — deny any mutation outside this repo. A PreToolUse hook (LOUD rung: fails at the
# point of misuse, with a reason). This is the guardrail that makes unattended running safe: an agent
# left alone all night cannot touch anything outside the project it was pointed at.
#
# WHY THIS CANNOT LIVE IN CLAUDE.md: "don't touch other projects" written as an instruction is a
# promise, and the whole premise of game_loop is that promises break under long sessions and compaction.
# A hook holds whether or not the agent remembers it.
#
# THE MODEL: an ALLOWLIST, not a denylist. Writes are permitted only under the repo, the OS temp dir,
# this project's agent-memory dir, and anything in config.json -> allow_write_roots. Everything else
# is denied by default. A denylist ("block ~/dev, ~/.ssh, ...") defaults to UNPROTECTED and silently
# misses whatever nobody remembered to list; an allowlist defaults to PROTECTED.
#
# SCOPE — what this DOES and does NOT catch (a guard that overstates its reach buys false confidence):
#   DOES: Write/Edit/NotebookEdit whose target resolves outside the allow roots.
#   DOES: Bash mutators (rm/mv/cp/mkdir/chmod/... , shell redirects, git writes, sed -i) whose
#         resolved target is outside the allow roots. Paths resolved by realpath, `cd` tracked across
#         segments, every offending path collected (not just the first).
#   DOES: Bash invoking a configured deploy/publish verb, anywhere (config.json -> deploy_verbs).
#   DOES NOT: catch mutations made via MCP tools, an interpreter one-liner (`python3 -c 'os.remove(..)'`),
#             a path built from a shell variable (`rm $TARGET/x`), or a script that mutates without
#             naming the path on the command line. Those need tool-level matching this does not do.
#             Do not read silence here as safety.
#   DOES: at `git commit`, NAME the staged files this session never wrote — a warning about a
#         commit widened past the work (a directory-wide formatter, a codemod, `git add -A`). It is
#         STATED, NEVER BLOCKED: sweeping edits are sometimes the intent.
#   DOES: accept a commit's PROVENANCE, declared by REF — `game_loop attribute --merge <ref>`. The
#         files are recomputed from the ref by game_loop, never taken as a supplied list, and the
#         declaration is single-use and logged. What a named ref carries stops being reported; what
#         NOTHING accounts for becomes the only output. Stricter, not quieter (issue #29).
#   DOES NOT: know about any edit it did not see as a Write/Edit/NotebookEdit. A file written
#             through Bash (a heredoc, `sed -i`, a script or formatter), by a sibling session with
#             no attribution declared, or before this session started is absent from that set and
#             gets named as excess; and with no recorded edits at all the check says nothing. It
#             reads the INDEX, so `git commit -a`, an explicit pathspec, and `--no-verify` all pass
#             it unexamined. Silence there is not evidence that a commit is tight.
#   DOES NOT: verify that an attribution was HONEST about intent. It verifies only that the refs
#             resolve and recomputes what they carry — a real ref deliberately chosen to blanket a
#             file is not detectable here. It is narrowed to one commit and written to log.jsonl
#             with its refs and reason instead, which makes it attributable rather than impossible.
#   DOES: at `git commit`, check the owed-checks record of the TREE the commit lands in — a git
#         worktree carries its own .game_loop/ and is gated on that, never on the tree this script
#         happens to sit in. A commit landing in a tree that has no .game_loop/ is REFUSED rather
#         than checked against somebody else's record.
#   DOES NOT: find the tree through a path this script cannot resolve. The target comes from the
#             same scan as everything else, so a `cd $SOMEVAR` or a path built from a variable
#             leaves the target reading as the project root, and the project's own record is used.
#             Nor does it read a commit's tree from git config it never runs: GIT_DIR/GIT_WORK_TREE
#             in the environment of the command being run are not consulted.
#   DOES NOT: parse shell grammar. Command substitution, subshells and loops are read as flat text,
#             and an UNQUOTED redirect target is cut at the first metacharacter — so a real target
#             whose name literally contains `)`, `;`, `&` or `|` is checked only up to that
#             character. Cutting yields a PREFIX of the path, which is still outside the allow
#             roots in every ordinary case, so this loses precision in the message, not protection.
#
# THE ESCAPE HATCH IS THE HUMAN, deliberately. There is no env-var override — a guard the agent can
# switch off is not a guard. A single mutation outside the repo is unlocked only by
# `game_loop authorize --path <prefix> --reason "<their words>"`, which is single-use and logged forever.

set -uo pipefail
payload=$(cat)

# THIS script's .game_loop/ — the project the session was pointed at, since the hook is registered as
# "$CLAUDE_PROJECT_DIR"/.game_loop/bin/guard-writes.sh. Deliberately SESSION-WIDE, not per git
# worktree, and the distinction matters once a session works in several trees at once (#28):
#   SESSION STATE (state.json, and the edited set beside it) stays HERE. An authorization is granted
#     by a human to a SESSION; `game_loop authorize` writes it wherever the human ran it, and one
#     granted in the project must be spendable by the same session working in a worktree — splitting
#     it per tree would silently lose the human's only escape hatch (INV5). The session is one
#     session however many trees it touches.
#   log.jsonl stays HERE too. It is the permanent record `status` reads the ruled-out list back
#     from, and worktrees are deleted when their work merges — a per-tree log would take the history
#     with it, and fragment the one file that is supposed to outlive every session.
#   THE COMMIT GATE does NOT stay here. What a change owes, and whether the evidence is newer than
#     the change, are facts about a TREE — its files, its mtimes, its record. That one is resolved
#     from the tree the commit targets; see the commit scan below.
GAMELOOP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"    # .game_loop/
REPO="${CLAUDE_PROJECT_DIR:-$(dirname "$GAMELOOP_DIR")}"
CONFIG_F="$GAMELOOP_DIR/config.json"

REPO_REAL=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$REPO" 2>/dev/null)
SLUG=$(python3 -c 'import re,sys; print(re.sub(r"[^a-zA-Z0-9]", "-", sys.argv[1]))' "$REPO_REAL" 2>/dev/null)

# State is per-session: an authorization is granted IN a session and spendable only THERE. The hook
# payload's session_id is authoritative; env is the fallback; neither → the repo-global legacy file
# (human terminal, old harness). Mirrors set_session() in bin/game_loop and bin/watchdog.
SID=$(printf '%s' "$payload" | python3 -c '
import json, os, re, sys
sid = json.load(sys.stdin).get("session_id") or os.environ.get("GAME_LOOP_SESSION") or os.environ.get("CLAUDE_CODE_SESSION_ID") or ""
print(re.sub(r"[^A-Za-z0-9._-]", "-", sid.strip())[:64])' 2>/dev/null)
if [ -n "$SID" ]; then
  STATE_F="$GAMELOOP_DIR/sessions/$SID/state.json"
else
  STATE_F="$GAMELOOP_DIR/state.json"
fi
# The paths THIS session wrote, beside its state and scoped the same way: a sibling session's edits
# are not this session's work, exactly as a sibling's mandate is not its mandate.
EDITED_F="$(dirname "$STATE_F")/edited.txt"

deny() {
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":%s}}\n' \
    "$(printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
  exit 0
}

# A WARNING, not a decision. Carries NO permissionDecision, so the tool call proceeds exactly as it
# would have and the permission flow is untouched; the text is injected into the model's context
# (PreToolUse -> hookSpecificOutput.additionalContext, "Text injected into model context" — read out
# of the harness binary itself, bin/claude.exe). A harness that does not honour the field drops the
# text and still runs the command: this degrades to silence, never to a block.
note() {
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":%s}}\n' \
    "$(printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
  exit 0
}

# Record a path this Crawler wrote, so the commit gate can compare the session's actual work against
# a commit's blast radius (issue #21). Append-only, one repo-relative path per line. This runs at
# PreToolUse, i.e. BEFORE the write lands, so a denied or failed write still records — the set errs
# toward TOO MANY files, which costs a missed warning, never a false accusation.
record_edit() {
  FP="$1" REPO_REAL="$REPO_REAL" EDITED_F="$EDITED_F" python3 <<'PY' 2>/dev/null
import os, sys
repo = os.environ["REPO_REAL"]
real = os.path.realpath(os.environ["FP"])
if real == repo or not real.startswith(repo + os.sep):
    sys.exit(0)                               # outside the repo: never staged here, never our business
rel = os.path.relpath(real, repo)
f = os.environ["EDITED_F"]
try:
    with open(f) as fh:
        already = rel in fh.read().split("\n")
except OSError:
    already = False
if already:
    sys.exit(0)
try:
    os.makedirs(os.path.dirname(f), exist_ok=True)
    with open(f, "a") as fh:                  # append: concurrent hooks interleave lines, never clobber
        fh.write(rel + "\n")
except OSError:
    pass
PY
}

# A human-authorized, single-use exception (`game_loop authorize`). Takes the offending realpath;
# prints "yes" iff a live authorization covers it, in which case the authorization is CONSUMED and
# the spend logged — one authorization buys one mutation, whichever tool performs it. Shared by the
# Write/Edit and Bash branches so the escape hatch behaves identically on both paths. No env
# override: it cannot be set without writing a permanent log entry carrying the human's own words.
consume_authorization() {
  OFFENDER="$1" GAMELOOP_DIR="$GAMELOOP_DIR" STATE_F="$STATE_F" SID="$SID" python3 <<'PY'
import json, os, sys, datetime
state_f = os.environ["STATE_F"]
log_f = os.path.join(os.environ["GAMELOOP_DIR"], "log.jsonl")
sid = os.environ.get("SID", "")
off = os.environ["OFFENDER"]
try:
    with open(state_f) as f:
        st = json.load(f)
except (OSError, ValueError):
    sys.exit(0)
for a in st.get("authorized", []):
    if a.get("uses_left", 0) <= 0:
        continue
    root = a.get("path", "")
    if off == root or off.startswith(root + os.sep):
        a["uses_left"] -= 1
        try:
            with open(state_f, "w") as f:
                json.dump(st, f, indent=2); f.write("\n")
            with open(log_f, "a") as f:
                rec = {"t": datetime.datetime.now().isoformat(timespec="seconds")}
                if sid:
                    rec["sid"] = sid[:8]
                rec.update({"kind": "authorized_write", "path": off,
                            "reason": a.get("reason"), "uses_left": a["uses_left"]})
                f.write(json.dumps(rec) + "\n")
        except OSError:
            sys.exit(0)
        print("yes")
        break
PY
}

tool=$(printf '%s' "$payload" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("tool_name",""))' 2>/dev/null)

case "$tool" in
  Write|Edit|NotebookEdit)
    fp=$(printf '%s' "$payload" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("tool_input",{}).get("file_path",""))' 2>/dev/null)
    [ -z "$fp" ] && exit 0
    # Prints "yes" when the target is inside an allow root, else the resolved realpath — which is
    # what an authorization is matched against (authorize records real prefixes, not raw tool input).
    verdict=$(REPO_REAL="$REPO_REAL" SLUG="$SLUG" CONFIG_F="$CONFIG_F" FP="$fp" python3 <<'PY'
import json, os
repo = os.environ["REPO_REAL"]
home = os.path.expanduser("~")
allow = [repo, "/tmp", "/private/tmp", "/var/folders",
         os.path.join(home, ".claude", "projects", os.environ["SLUG"])]
try:
    with open(os.environ["CONFIG_F"]) as f:
        allow += [os.path.expanduser(p) for p in (json.load(f).get("allow_write_roots") or [])]
except (OSError, ValueError):
    pass
allow = [os.path.realpath(p) for p in allow]
real = os.path.realpath(os.environ["FP"])
print("yes" if any(real == a or real.startswith(a + os.sep) for a in allow) else real)
PY
)
    if [ "$verdict" = "yes" ]; then
      record_edit "$fp"                        # an in-repo write IS this session's work (issue #21)
      exit 0
    fi
    if [ -n "$verdict" ]; then
      consumed=$(consume_authorization "$verdict")
      [ "$consumed" = "yes" ] && exit 0
    fi
    deny "BLOCKED: write outside this repo → $fp

Everything outside this project is READ-ONLY by default (this is the guardrail that makes unattended
runs safe). If the repo genuinely needs that content, COPY it in and edit the copy. If the human has
explicitly authorized this path:  game_loop authorize --path <prefix> --reason \"<their words>\""
    ;;

  Bash)
    cmd=$(printf '%s' "$payload" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("tool_input",{}).get("command",""))' 2>/dev/null)
    [ -z "$cmd" ] && exit 0

    # A heredoc/quoted DATA body (e.g. a commit message piped through a here-doc into cat) is DATA, not
    # executed shell — scanning it for redirects / mutators / deploy verbs false-positives on ordinary
    # prose. scan_cmd is $cmd with the bodies of here-docs fed to a known DATA SINK (cat/tee) removed,
    # and the quoted arguments of known MESSAGE-BEARING flags (-m/--notes/--reason/…) blanked: those
    # strings are prose their commands never execute, and scanning them denies commit messages that
    # merely mention a redirect or a deploy verb. Bodies of here-docs fed to an interpreter
    # (bash/sh/python/…) are KEPT — they DO run and must stay guarded, and so are all other quoted
    # strings (bash -c '…' executes). Unknown consumer -> KEPT (fail safe: a false positive, never a
    # silent bypass).
    #
    # NB: this Python is embedded in a $(...) here-doc, so it must contain NO backtick, NO dollar-paren,
    # and NO literal here-doc operator — any of those derails bash's parse of the surrounding $(...).
    # The here-doc operator is therefore built from chr(60); the consumer is found by a plain word scan.
    scan_cmd=$(CMD="$cmd" python3 <<'PY'
import os, re, sys
cmd = os.environ["CMD"]
DATA_SINKS = {"cat", "tee"}
HD = chr(60) + chr(60)                       # the here-doc operator, with no literal one in this file
opener = re.compile(re.escape(HD) + r"-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
lines = cmd.split("\n")
out, i = [], 0
while i < len(lines):
    line = lines[i]
    out.append(line)
    found = opener.findall(line)
    if found:
        # The consumer is the last command word BEFORE the here-doc operator — after dropping
        # redirect clauses, or the redirect TARGET would be mistaken for the consumer
        # (in a line like: cat with a redirect, then the operator, the consumer is cat).
        pre = re.sub(r">>?\s*[^\s;&|<>]*", " ", line.split(HD, 1)[0])
        words = re.findall(r"[A-Za-z0-9_./]+", pre)
        is_data = (os.path.basename(words[-1]) if words else "") in DATA_SINKS
        delims = [d for _q, d in found]
        i += 1
        di = 0
        while i < len(lines) and di < len(delims):
            body = lines[i]
            if body.strip() == delims[di]:
                out.append(body)             # keep the delimiter line itself
                di += 1
            elif not is_data:
                out.append(body)             # code here-doc: body executes — keep it in the scan
            # data here-doc: drop the body line (it is data, not shell)
            i += 1
        continue
    i += 1

# Blank the quoted argument of message-bearing flags. These strings are DATA to their command
# (a commit message, a note, a reason) — never executed — so redirects or deploy verbs mentioned
# inside them must not be flagged. Quoted strings NOT behind one of these flags are kept: an
# interpreter argument (a -c script) executes and must stay guarded.
MSG_FLAGS = ("-m|--message|--notes|--reason|--body|--title|--assert|--learning|--question"
             "|--predict|--doing|--milestone|--set")
QSTR = "\"(?:[^\"\\\\]|\\\\.)*\"|'[^']*'"
text = "\n".join(out)
text = re.sub("(?:^|(?<=\\s))(" + MSG_FLAGS + ")(=|\\s+)(" + QSTR + ")",
              lambda m: m.group(1) + m.group(2) + "\"\"", text)
sys.stdout.write(text)
PY
)

    # 0. A commit is when a change becomes real. Refuse one whose owed checks (.game_loop/verify.yaml)
    #    have not run SINCE the change. No-op when verify.yaml is empty, so it costs nothing until you
    #    opt in. --no-verify skips it, out loud and on the record. Gates `git commit` only, not every
    #    write: a check per keystroke is ceremony that gets switched off.
    #    Gates only commits that TARGET THIS repo: verify.yaml describes THIS repo's owed checks, and a
    #    commit made in some other repository (a clone in a scratch root, a sibling project) owes that
    #    repo's checks, not ours. The target is resolved per segment — `cd` tracked, `git -C` honored —
    #    the same way the mutation scanner resolves paths.
    #    The scan also collects every OTHER segment chained into the command. A denial here means the
    #    WHOLE body never ran — including edits/deletions chained before the commit — and the natural
    #    retry (just the commit) silently drops them, with a commit message still describing work that
    #    never happened. The denial must therefore name what else was in the command (issue #9).
    #    THE TREE IS PART OF THE ANSWER (issue #28). The hook is registered as
    #    "$CLAUDE_PROJECT_DIR"/.game_loop/bin/guard-writes.sh, so this script's own location always
    #    resolves to the MAIN checkout. A commit made in a git WORKTREE was therefore gated on the
    #    main checkout's verified.json and the main checkout's file mtimes — a different set of files
    #    in a different state. Wrong in both directions: a freshly verified worktree was refused for
    #    the main tree's unrelated staleness, and a worktree could commit a gated file while the main
    #    record sat green from a run that never saw it. The second defeats the gate's whole premise —
    #    the evidence was about another tree entirely. So the tree is resolved HERE, from the same
    #    answer the repo-scoping check already computes, and its own .game_loop/ is what gets checked.
    #    First output line: "yes"/"" (a repo-targeting commit is present); SECOND line: which
    #    .game_loop/ that commit is answerable to —
    #      root:<dir>          check this one (the target tree's own record)
    #      undetermined:<dir>  a tree we can NAME but cannot CHECK -> refuse, saying so
    #    each further line: one chained non-commit segment, truncated for display.
    #    Only a tree STRICTLY INSIDE the project moves the answer; the project itself, a project that
    #    lives inside a larger checkout, and a target git can name no tree for all keep this script's
    #    own .game_loop — so a repo without worktrees behaves exactly as it always did.
    commit_scan=$(REPO_REAL="$REPO_REAL" GAMELOOP_DIR="$GAMELOOP_DIR" SCAN_CMD="$scan_cmd" python3 - "$payload" <<'PY'
import json, os, re, shlex, subprocess, sys
payload = json.loads(sys.argv[1])
cmd = os.environ.get("SCAN_CMD", "")
repo = os.environ["REPO_REAL"]
cwd = payload.get("cwd") or repo
home = os.path.expanduser("~")
found = False
target = None
others = []


def tree_of(path):
    """The git tree a commit made in `path` actually lands in. A worktree is its own tree, holding
    its own files at its own mtimes — and its own .game_loop/. Empty when git can name no tree."""
    try:
        r = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True)
    except OSError:
        return ""
    out = r.stdout.strip()
    return os.path.realpath(out) if r.returncode == 0 and out else ""


for seg in re.split(r"&&|\|\||;|\||\n", cmd):
    seg = seg.strip()
    if not seg:
        continue
    try:
        argv = shlex.split(seg)
    except ValueError:
        argv = seg.split()
    if not argv:
        continue
    verb = os.path.basename(argv[0])
    args = argv[1:]
    if verb == "cd" and args:
        nxt = os.path.expanduser(args[0].replace("$HOME", home))
        cwd = nxt if os.path.isabs(nxt) else os.path.join(cwd, nxt)
        continue   # a bare cd is navigation, not lost work — track it, don't report it
    if verb == "git" and "commit" in args and "--no-verify" not in args:
        tgt = cwd
        if "-C" in args and args.index("-C") + 1 < len(args):
            c = os.path.expanduser(args[args.index("-C") + 1].replace("$HOME", home))
            tgt = c if os.path.isabs(c) else os.path.join(cwd, c)
        if os.path.realpath(tgt).startswith(os.path.realpath(repo).rstrip(os.sep) + os.sep) \
                or os.path.realpath(tgt) == os.path.realpath(repo):
            found = True
            if target is None:
                target = tgt          # the tree whose record this commit is answerable to (#28)
            continue
    others.append(seg if len(seg) <= 70 else seg[:67] + "...")

answerable = ""
if found:
    own = "root:" + os.environ["GAMELOOP_DIR"]
    root = os.path.realpath(repo).rstrip(os.sep)
    top = tree_of(target)
    if top and top != root and top.startswith(root + os.sep):
        # A DIFFERENT tree, nested inside the project: a worktree, or a repo of its own. Its files
        # are the ones being committed, so its record is the only evidence that describes them.
        gl = os.path.join(top, ".game_loop")
        answerable = ("root:" + gl if os.path.exists(os.path.join(gl, "bin", "verify"))
                      else "undetermined:" + top)
    else:
        answerable = own
print("yes" if found else "")
print(answerable)
for o in others:
    print(o)
PY
)
    commit_here=$(printf '%s\n' "$commit_scan" | head -1)
    commit_root=$(printf '%s\n' "$commit_scan" | sed -n '2p')
    chained_segs=$(printf '%s\n' "$commit_scan" | tail -n +3 | grep -v '^$' || true)
    if [ "$commit_here" = "yes" ]; then
      case "$commit_root" in
        undetermined:*)
          deny "BLOCKED: this commit lands in a tree that carries no game_loop, so its owed checks cannot be read.

    the commit's tree:  ${commit_root#undetermined:}
    the tree these checks describe:  $REPO_REAL

.game_loop/verify.yaml and the record of when each check last ran belong to ONE tree. Checking a
DIFFERENT tree's record would answer a question about files this commit does not contain — and report
confidence either way. That is the false green this gate exists to prevent, so it refuses instead (INV6).

Install game_loop in that tree so it carries its own record, commit from the tree the checks describe,
or commit with --no-verify to skip the gate out loud and on the record."
          ;;
      esac
      GAMELOOP_TARGET="${commit_root#root:}"
      # The blast-radius check below reads an INDEX; it must read the index of the tree being
      # committed, or it compares this session's work against staged files from somewhere else.
      # Unchanged whenever the target resolves to this script's own tree.
      TARGET_TREE="$REPO_REAL"
      [ "$GAMELOOP_TARGET" != "$GAMELOOP_DIR" ] && TARGET_TREE="$(dirname "$GAMELOOP_TARGET")"
      if ! "$GAMELOOP_TARGET/bin/verify" --check >/tmp/.game_loop_verify 2>&1; then
        # ORDERING NOTE: this hook runs at PreToolUse, BEFORE the command body executes. Bundling
        # `verify` and `git commit` in ONE call can never pass — the check runs before your verify
        # line does. Run them as two separate calls.
        chained_hint=""
        if printf '%s' "$cmd" | grep -qE 'bin/verify|\bverify\b'; then
          chained_hint="
YOU CHAINED verify WITH THIS COMMIT IN ONE COMMAND. That can't work: this gate runs BEFORE the
command body, so your verify line hasn't executed yet. Run verify as a SEPARATE, EARLIER call."
        fi
        # Issue #9: every other denial here is safe to retry naively; this one is not. Re-running
        # just the commit after a denial silently drops the chained edits the commit message still
        # describes. Name the chained work, so the retry that loses it can't happen quietly.
        if [ -n "$chained_segs" ]; then
          seg_count=$(printf '%s\n' "$chained_segs" | wc -l | tr -d ' ')
          chained_hint="$chained_hint
THIS COMMAND CHAINS $seg_count OTHER OPERATION(S) WITH THE COMMIT:
$(printf '%s\n' "$chained_segs" | sed 's/^/    /')
This gate runs BEFORE the command body, so NONE of them executed. When you retry, re-run the
WHOLE command — retrying only the commit silently loses the rest, while the commit message
still describes it."
        fi
        deny "$(cat /tmp/.game_loop_verify)

A green check from BEFORE your change is evidence about code that no longer exists.
Run ./.game_loop/bin/verify, or commit with --no-verify to skip it on the record.$chained_hint"
      fi

      # The gate above asks whether the change was VERIFIED. It never asks whether it was INTENDED,
      # and one command widens a commit far past the work: a formatter aimed at a whole directory
      # reformatted a dozen files nobody had opened, `git add -A` swept them in, and the message
      # described something else entirely (issue #21). A commit's blast radius and the session's
      # actual work are different sets, and only the session knows the second one — so name the
      # excess. STATED, NEVER BLOCKED: sweeping edits are sometimes exactly the intent; the failure
      # is that it happens silently, inside a diff nobody re-reads.
      # Silent by design wherever it cannot reason: no recorded edits, no readable index, no git.
      # A commit's PROVENANCE is the third thing this needs to know and could not be told (issue
      # #29). See the attribution block inside the Python below.
      blast_note=$(REPO_REAL="$REPO_REAL" EDITED_F="$EDITED_F" CONFIG_F="$CONFIG_F" \
                   GAMELOOP_DIR="$GAMELOOP_DIR" TARGET_TREE="$TARGET_TREE" SID="$SID" \
                   STATE_F="$STATE_F" python3 <<'PY'
import datetime, json, os, subprocess, sys
from fnmatch import fnmatch

repo = os.environ["REPO_REAL"]
# The tree this commit lands in — its index is the blast radius (#28). Equal to the repo itself for
# every commit outside a worktree, which is the only shape this ever saw before.
tree = os.environ.get("TARGET_TREE") or repo
try:
    with open(os.environ["EDITED_F"]) as f:
        edited = set(x.strip() for x in f if x.strip())
except OSError:
    edited = set()
if not edited:
    sys.exit(0)          # nothing observed at all — no evidence, so no accusation

# PROVENANCE — the third bucket (issue #29). The edited set is session-wide and correct, which is
# exactly why it can never contain what a SIBLING session wrote on a branch: when this session lands
# that work, `git merge` brings the files in and every one reads as excess. Across ~14 integration
# commits the warning fired on 8, naming only legitimate files — and a warning that is wrong every
# time is one people learn to scroll past.
# So a session may DECLARE a commit's provenance: `game_loop attribute --merge <ref> --reason "..."`.
# The declaration names REFS; game_loop recomputed the files from each ref itself (never from a
# supplied list — that recomputation is the check, and an unresolvable ref is refused there). Here we
# only read the result back, union it, and CONSUME the declaration exactly like an authorization.
# The effect is a STRICTER check, not a quieter one: a file nothing accounts for stops being one line
# among ten legitimate ones and becomes the only line.
state_f = os.environ.get("STATE_F", "")
attributed, att_refs, att_reason, att_live, st = set(), [], None, [], None
try:
    with open(state_f) as f:
        st = json.load(f)
except (OSError, ValueError):
    st = None
for rec in (st or {}).get("attributed", []) if isinstance(st, dict) else []:
    if rec.get("uses_left", 0) <= 0:
        continue                                  # already spent: one declaration, one commit
    att_live.append(rec)
    attributed.update(rec.get("files") or [])
    att_refs.extend(rec.get("refs") or [])
    if att_reason is None:
        att_reason = rec.get("reason")


def git(*args):
    try:
        r = subprocess.run(["git", "-C", tree] + list(args), capture_output=True, text=True)
    except OSError:
        return None
    return r.stdout if r.returncode == 0 else None


top = git("rev-parse", "--show-toplevel")
staged = git("diff", "--cached", "--name-only")
if top is None or staged is None:
    sys.exit(0)          # not an index this can read — degrade to silence, never to noise
top = os.path.realpath(top.strip())

# CONSUME, here and not a line earlier: a declaration is spent by the next commit this check
# actually EXAMINES. Above this point the check said nothing at all (no recorded edits, no readable
# index), and burning an attribution on a commit it never spoke about would leave the commit it was
# written for facing the full, wrong list. Same semantics as an authorization: one buys one, and the
# spend is logged with the refs, so a widening of what this check accepts is readable forever.
if att_live:
    for rec in att_live:
        rec["uses_left"] = rec.get("uses_left", 1) - 1
    try:
        with open(state_f, "w") as f:
            json.dump(st, f, indent=2)
            f.write("\n")
        spend = {"t": datetime.datetime.now().isoformat(timespec="seconds")}
        if os.environ.get("SID"):
            spend["sid"] = os.environ["SID"][:8]
        spend.update({"kind": "attributed_merge", "refs": list(dict.fromkeys(att_refs)),
                      "files": len(attributed), "reason": att_reason})
        with open(os.path.join(os.environ["GAMELOOP_DIR"], "log.jsonl"), "a") as f:
            f.write(json.dumps(spend) + "\n")
    except OSError:
        pass

# Generated and vendored output is somebody else's work by definition. Without these exemptions the
# warning fires on every lockfile and stops being read, and a guard nobody reads is a guard routed
# around. Extend per project with config.json -> generated_globs.
# game_loop's own runtime state first: this check writes edited.txt and log.jsonl itself, and a
# guard that accuses a commit of the files the guard just wrote is a guard nobody trusts. (Normally
# git-ignored — this covers an install whose .gitignore never got those entries.)
EXEMPT = [".game_loop/sessions/*", ".game_loop/edited.txt", ".game_loop/log.jsonl",
          ".game_loop/state.json", ".game_loop/verified.json", ".game_loop/limits.json",
          "vendor/*", "*/vendor/*", "node_modules/*", "*/node_modules/*",
          "third_party/*", "*/third_party/*", "dist/*", "*/dist/*", "build/*", "*/build/*",
          "*.lock", "*-lock.json", "*-lock.yaml", "*.g.dart", "*.freezed.dart", "*.pb.go",
          "*_pb2.py", "*.generated.*", "*.min.js", "*.min.css", "*.snap"]
try:
    with open(os.environ["CONFIG_F"]) as f:
        EXEMPT += (json.load(f).get("generated_globs") or [])
except (OSError, ValueError):
    pass

total, unedited, from_merge = 0, [], 0
for line in staged.splitlines():
    p = line.strip()
    if not p:
        continue
    total += 1
    rel = os.path.relpath(os.path.join(top, p), repo)   # git prints from ITS root, which may not be ours
    if rel.startswith(".."):
        continue                                       # staged outside our root: not ours to judge
    # In a worktree, rel carries the worktree prefix (which is what the edited set records, so the
    # two still compare) — but the exemptions describe a path INSIDE a tree, so they are matched
    # against the tree-relative form too. Same list, same answer, for a commit in the repo itself.
    forms = [rel] if os.path.realpath(tree) == repo else [rel, p]
    if rel in edited or any(fnmatch(x, g) for x in forms for g in EXEMPT):
        continue
    if any(x in attributed for x in forms):
        from_merge += 1        # a named ref carries it, and game_loop recomputed that — not excess
        continue
    unedited.append(rel)
if not unedited:
    sys.exit(0)

n = len(unedited)
if att_live:
    # THE WHOLE OUTPUT is now what nothing accounts for. This is stricter than the old shape, not
    # quieter: the same file used to be one line among ten legitimate ones in a warning everyone had
    # learned to skip, and it is now the only line.
    lines = ["COMMIT INCLUDES %d FILE%s NOTHING ACCOUNTS FOR — NOT THIS SESSION, NOT ANY "
             "ATTRIBUTED MERGE" % (n, "" if n == 1 else "S")]
    lines += ["    " + p for p in unedited[:10]]
    if n > 10:
        lines.append("    ... %d more" % (n - 10))
    lines += [
        "",
        "AND THAT IS THE ENTIRE LIST. %d other staged file%s came in with an attributed merge:"
        % (from_merge, "" if from_merge == 1 else "s"),
        "    " + (", ".join(dict.fromkeys(att_refs)) or "(no ref named)"),
        "    reason: " + (att_reason or "(none given)"),
        "recomputed by game_loop from those refs, never from a list of filenames you supplied. So the",
        "lines above are STRICTLY the unexplained ones — read them, they are not padding.",
        "Not intended? Unstage them:",
        "    git restore --staged <path>     (and 'git checkout -- <path>' to drop the change itself)",
    ]
else:
    lines = ["COMMIT INCLUDES %d FILE%s THIS SESSION NEVER EDITED" % (n, "" if n == 1 else "S")]
    lines += ["    " + p for p in unedited[:10]]
    if n > 10:
        lines.append("    ... %d more" % (n - 10))
    lines += [
        "",
        "A formatter, a codemod, a --fix run or a dependency update probably widened this, and",
        "'git add -A' swept it in. Intended? Say so in the commit message. Not intended? Unstage them:",
        "    git restore --staged <path>     (and 'git checkout -- <path>' to drop the change itself)",
        "",
        "LANDING WORK A SIBLING SESSION PRODUCED ON A BRANCH? That is provenance, not excess, and this",
        "set cannot see it. Declare it by REF and it drops out, leaving only what nothing accounts for:",
        "    game_loop attribute --merge <ref> [--merge <ref> ...] --reason \"<why this commit "
        "carries them>\"",
    ]
lines += [
    "",
    "WHAT THIS SET SEES: files THIS session wrote through Write/Edit/NotebookEdit, plus the files a",
    "live `game_loop attribute --merge <ref>` declaration accounts for (recomputed from the ref, and",
    "spent by this commit). A file you created or rewrote through Bash (a heredoc, 'sed -i', a script",
    "or formatter you ran), one a sibling session wrote with no attribution, or one already changed",
    "before this session started is NOT in it and is listed above even when it was the point. And",
    "with no recorded edits this check says nothing at all — silence here is not evidence that a",
    "commit is tight.",
]
try:
    rec = {"t": datetime.datetime.now().isoformat(timespec="seconds")}
    if os.environ.get("SID"):
        rec["sid"] = os.environ["SID"][:8]
    rec.update({"kind": "commit_unedited", "staged": total, "unedited": n,
                "attributed": from_merge, "files": unedited[:10]})
    with open(os.path.join(os.environ["GAMELOOP_DIR"], "log.jsonl"), "a") as f:
        f.write(json.dumps(rec) + "\n")
except OSError:
    pass
sys.stdout.write("\n".join(lines))
PY
)

      # The gate above asks whether the LISTED checks are stale. It cannot ask about a path nobody
      # listed: verify.yaml maps globs to commands, so a file matching no glob owes nothing and the
      # gate passes it in silence. That is issue #25 — a whole package built, hand-tested and
      # committed while `verify --check` reported clean, because green meant "nothing LISTED here is
      # stale", not "nothing is unverified". So name the staged paths no rule claims.
      # STATED, NEVER BLOCKED, and for the same reason the manifest ships empty: on a fresh install
      # every path is unchecked, and refusing there would block the first commit with the fix —
      # writing the rules — sitting behind the gate (INV5).
      cov_json=$("$GAMELOOP_DIR/bin/verify" --coverage --staged --porcelain 2>/dev/null || true)
      cov_note=$(COV="$cov_json" python3 <<'PY'
import json, os
try:
    cov = json.loads(os.environ.get("COV") or "")
except ValueError:
    raise SystemExit(0)              # no answer is not an accusation — degrade to silence
unchecked = cov.get("unchecked") or []
if not unchecked:
    raise SystemExit(0)
n = len(unchecked)
if not cov.get("rules"):
    lines = ["NOTHING IN THIS COMMIT IS CHECKED — .game_loop/verify.yaml has no rules, so the owed-"
             "checks",
             "gate passed by having nothing to say about %d staged file%s." % (n, "" if n == 1 else "s")]
else:
    lines = ["THIS COMMIT CARRIES %d STAGED FILE%s NO RULE CHECKS"
             % (n, "" if n == 1 else "S")]
    lines += ["    " + p for p in unchecked[:10]]
    if n > 10:
        lines.append("    ... %d more" % (n - 10))
lines += [
    "",
    "A green verify means 'nothing LISTED is stale' — never 'nothing is unverified'. A path no glob",
    "matches owes nothing and passes in silence, which is how a whole new package gets built, tested",
    "by hand and committed with the gate reporting clean.",
    "Add a rule in .game_loop/verify.yaml that FAILS when the thing breaks, or say out loud that it",
    "needs none:",
    '    "unchecked-ok":',
    '      - "<glob>"',
    "",
    "STATED, NEVER BLOCKED — the manifest ships empty, so refusing here would block a fresh install's",
    "first commit. It reads the INDEX, so `git commit -a`, a pathspec commit and --no-verify pass it",
    "unexamined, and it says nothing about paths that did not change. Silence here is not evidence",
    "that a commit was checked.",
]
print("\n".join(lines))
PY
)
    fi

    # 1. Configured deploy/publish verbs — denied anywhere, no path needed.
    deploy_hit=$(CONFIG_F="$CONFIG_F" CMD="$scan_cmd" python3 <<'PY'
import json, os, re
defaults = ["npm publish", "yarn publish", "pnpm publish", "twine upload",
            "gh release create", "docker push"]
verbs = list(defaults)
try:
    with open(os.environ["CONFIG_F"]) as f:
        verbs += (json.load(f).get("deploy_verbs") or [])
except (OSError, ValueError):
    pass
cmd = os.environ["CMD"]
for v in verbs:
    # The boundary class includes quote chars: a deploy verb at the start of an interpreter arg
    # (a -c script) executes just the same, and message-flag strings were already blanked upstream.
    pat = r"(^|[\s;&|'\"])" + r"\s+".join(re.escape(w) for w in v.split())
    if re.search(pat, cmd):
        print(v)
        break
PY
)
    if [ -n "$deploy_hit" ]; then
      deny "BLOCKED: deploy/publish verb '$deploy_hit'.

This is an irreversible, outward-facing action (a real publish/release/deploy). An unattended agent
does not fire these. If it is genuinely needed, escalate to the human — that is the only escape
hatch, by design. (Configured in .game_loop/config.json -> deploy_verbs.)"
    fi

    # 2. Mutation aimed OUTSIDE the allow roots, decided by RESOLVING PATHS — not matching names.
    offender=$(REPO_REAL="$REPO_REAL" SLUG="$SLUG" CONFIG_F="$CONFIG_F" SCAN_CMD="$scan_cmd" python3 - "$payload" <<'PY'
import json, os, re, shlex, sys

payload = json.loads(sys.argv[1])
cmd = os.environ.get("SCAN_CMD", "")          # here-doc DATA bodies already stripped (see scan_cmd)
cwd = payload.get("cwd") or os.environ["REPO_REAL"]
home = os.path.expanduser("~")

allow = [os.environ["REPO_REAL"], "/tmp", "/private/tmp", "/var/folders",
         os.path.join(home, ".claude", "projects", os.environ["SLUG"])]
try:
    with open(os.environ["CONFIG_F"]) as f:
        allow += [os.path.expanduser(p) for p in (json.load(f).get("allow_write_roots") or [])]
except (OSError, ValueError):
    pass
allow = [os.path.realpath(p) for p in allow]

MUTATORS = {"rm", "rmdir", "touch", "mkdir", "chmod", "chown", "ln", "dd", "truncate", "tee"}
GIT_WRITES = {"commit", "push", "reset", "rebase", "checkout", "clean", "apply", "restore", "mv"}


def under(path, root):
    return path == root or path.startswith(root + os.sep)


# Standard character devices: discard sinks and the console/std streams. A redirect to one of these
# (e.g. `2>/dev/null`, `>/dev/stderr`) never writes a real out-of-repo file, so it must not be flagged.
# Matched on the LITERAL path — /dev/stdout & friends are symlinks realpath would resolve away (to a
# tty or a pipe), which would then read as an out-of-repo file and deny.
STD_DEVICES = {"/dev/null", "/dev/zero", "/dev/stdin", "/dev/stdout", "/dev/stderr", "/dev/tty",
               "/dev/random", "/dev/urandom"}


def is_sink(p):
    """True for a NON-FILESYSTEM sink: writing to it mutates nothing on disk, so it is exempt
    outright, never merely 'outside the repo'. Normalized first, so /dev/./null is the same sink."""
    p = os.path.normpath(p)
    return p in STD_DEVICES or p.startswith("/dev/fd/")


def offends(raw, cwd):
    """Return the offending realpath, or None if this path is inside an allow root."""
    p = os.path.expanduser(raw.replace("$HOME", home))
    if not os.path.isabs(p):
        p = os.path.join(cwd, p)
    if is_sink(p):
        return None
    real = os.path.realpath(p)
    return None if any(under(real, a) for a in allow) else real


def redirect_targets(seg):
    """Redirect targets in one segment, QUOTE-AWARE in both directions: a redirect character inside
    quotes is data (a sed script, prose in a message) and must not be flagged, while a QUOTED target
    after a real, unquoted redirect is a genuine write and must be — the naive regex missed those,
    because a captured token starting with a quote char never resolves to an absolute path.

    An UNQUOTED target ends at the first shell metacharacter, `)` included (issue #16). The shell
    ends the word there, so the guard must too: in `$(find . 2>/dev/null)` the target is /dev/null,
    not `/dev/null)`. Swallowing the paren both denied a discard sink and, for a genuinely
    suspicious path, named a target nobody could act on. A QUOTED target keeps its metacharacters —
    inside quotes they are part of the filename, which is exactly what the shell would write to."""
    targets, i, n, q = [], 0, len(seg), None
    while i < n:
        c = seg[i]
        if q:
            if c == q:
                q = None
        elif c in "'\"":
            q = c
        elif c == ">":
            j = i + 1
            if j < n and seg[j] == ">":
                j += 1
            while j < n and seg[j] in " \t":
                j += 1
            if j < n and seg[j] in "'\"":
                k = seg.find(seg[j], j + 1)
                if k != -1:
                    targets.append(seg[j + 1:k])
                    i = k
            else:
                k = j
                while k < n and seg[k] not in " \t;&|<>)":
                    k += 1
                if k > j:
                    targets.append(seg[j:k])
                    i = k - 1
        i += 1
    return targets


offenders = []
# Split on shell separators AND newlines. Omitting \n would collapse a multi-line command into one
# segment whose verb is its first token, so a mutating later line would never be checked.
for seg in re.split(r"&&|\|\||;|\||\n", cmd):
    try:
        argv = shlex.split(seg)
    except ValueError:
        argv = seg.split()
    if not argv:
        continue
    verb = os.path.basename(argv[0])
    args = argv[1:]
    pathish = [a for a in args if not a.startswith("-") and "&" not in a and a not in (">", ">>")]

    if verb == "cd" and pathish:
        nxt = os.path.expanduser(pathish[0].replace("$HOME", home))
        cwd = nxt if os.path.isabs(nxt) else os.path.join(cwd, nxt)
        continue

    check = []
    if verb == "cp":
        check = pathish[-1:]                       # cp checks its DESTINATION only (reading is fine)
    elif verb == "mv":
        check = pathish                            # mv mutates source AND destination
    elif verb in MUTATORS:
        check = pathish
    elif verb == "sed" and "-i" in args:
        check = pathish
    elif verb == "git" and any(a in GIT_WRITES for a in args):
        check = pathish                            # catches `git -C <path> commit`
    check.extend(redirect_targets(seg))            # redirects mutate regardless of the verb

    for raw in check:
        bad = offends(raw, cwd)
        if bad:
            offenders.append(bad)

for o in dict.fromkeys(offenders):
    print(o)
PY
)

    if [ -n "$offender" ]; then
      consumed=$(consume_authorization "$offender")
      [ "$consumed" = "yes" ] && exit 0
      deny "BLOCKED: mutating command targets a path outside this repo → $offender

Everything outside this project is READ-ONLY by default. READING elsewhere is fine, and so is copying
OUT of it: \`cp <their path> <repo path>\` is allowed. Copy what you need in and work on the copy.

If the human has explicitly authorized this specific path, record their words and try again:
  game_loop authorize --path <prefix> --reason \"<their exact words>\"
One authorization, one mutation, logged permanently. That is the only escape hatch, by design."
    fi

    # LAST, deliberately: these warnings are the only non-blocking output here, so they are emitted
    # after every check that can deny. A denial means the command never ran, and a warning about a
    # commit that did not happen is noise. `note` exits, so the two are joined into one body — a
    # commit can be both widened past the work AND carrying paths nothing checks.
    commit_note="${blast_note:-}"
    if [ -n "${cov_note:-}" ]; then
      [ -n "$commit_note" ] && commit_note="$commit_note
"
      commit_note="$commit_note$cov_note"
    fi
    [ -n "$commit_note" ] && note "$commit_note"
    ;;
esac

exit 0
