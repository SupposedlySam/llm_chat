#!/usr/bin/env bash
# Register the llm_chat delivery hook in another repo.
#
#   ./install.sh ~/dev/some-project
#
# Adds ONE PostToolUse hook to that project's .claude/settings.LOCAL.json and
# touches nothing else. Existing hooks — write guards, watchdogs, anything you
# have added — are preserved; the file is merged, never rewritten.
#
# It goes in settings.local.json, not settings.json, because the command is an
# ABSOLUTE path to this machine's llm_chat checkout. settings.json is tracked in
# most repos, so committing it there ships every cloner a hook that fires after
# every tool call against a directory only this machine has. settings.local.json
# is the documented home for machine-specific config and is conventionally
# ignored. Reported from a public repo by the agent it happened to.
#
# A previous install that wrote into settings.json is migrated out of it here,
# so the tracked file stops carrying this machine's path.
#
# Re-running is safe: the hook is matched by its command path in BOTH files, so
# a second run updates in place rather than adding a duplicate that would
# deliver every message twice.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Writing to a shared repo's .gitignore is a decision belonging to that repo,
# not to a tool one developer is installing. In a monorepo, naming one person's
# tooling in a committed file is a policy question, and it used to be assumed.
NO_GITIGNORE=0
TARGET=""
for arg in "$@"; do
  case "$arg" in
    --no-gitignore|--no-ignore) NO_GITIGNORE=1 ;;
    -*) echo "unknown option: $arg" >&2; exit 2 ;;
    *) TARGET="$arg" ;;
  esac
done

if [ -z "$TARGET" ]; then
  echo "usage: ./install.sh <path-to-repo> [--no-gitignore]" >&2
  exit 2
fi
if [ ! -d "$TARGET" ]; then
  echo "no such directory: $TARGET" >&2
  exit 1
fi

LOCAL="$TARGET/.claude/settings.local.json"
SHARED="$TARGET/.claude/settings.json"
mkdir -p "$TARGET/.claude"
[ -f "$LOCAL" ] || echo '{}' > "$LOCAL"

# Back up before touching someone else's config — a bad merge here is not a
# small inconvenience. Kept OUTSIDE the repo: a .bak dropped beside the file is
# untracked and unignored, so the next `git add -A` commits it. Reported by an
# agent whose commit gate caught it on the way out.
BACKUPS="${TMPDIR:-/tmp}/llm_chat-settings-backups"
mkdir -p "$BACKUPS"
cp "$LOCAL" "$BACKUPS/$(basename "$TARGET").settings.local.json.$(date +%s)"

HOOK="$HERE/bin/llm-chat-deliver"
WAKE="$HERE/bin/llm-chat-wake"
MCP="$HERE/bin/llm-chat-mcp"
chmod +x "$HOOK" "$WAKE" "$MCP" "$HERE/bin/llm_chat"

python3 - "$LOCAL" "$SHARED" "$HOOK" "$WAKE" <<'PY'
import json, os, sys

local_path, shared_path, hook_cmd, wake_cmd = sys.argv[1:5]

def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None

def save(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")

OURS = ("llm-chat-deliver", "llm-chat-wake")


def strip_llm_chat(settings):
    """Drop our hooks wherever they already are; report whether any were found.

    Both events, because a repo may carry an older install that had only the
    PostToolUse one.
    """
    found = False
    hooks = settings.get("hooks", {})
    for event in ("PostToolUse", "Stop", "SessionStart"):
        entries = hooks.get(event)
        if not isinstance(entries, list):
            continue
        kept_groups = []
        for group in entries:
            original = group.get("hooks", [])
            kept = [h for h in original
                    if not any(o in (h.get("command") or "") for o in OURS)]
            if len(kept) != len(original):
                found = True
            if kept or not original:
                group["hooks"] = kept
                kept_groups.append(group)
        if kept_groups:
            hooks[event] = kept_groups
        else:
            hooks.pop(event, None)
    if not hooks:
        settings.pop("hooks", None)
    return found

# Migrate: a previous install put an absolute path into the TRACKED file. Take
# it out, and only rewrite that file if we actually removed something — never
# touch a tracked file just to reformat it.
migrated = False
shared = load(shared_path)
if shared is not None and strip_llm_chat(shared):
    save(shared_path, shared)
    migrated = True

local = load(local_path)
if local is None:
    print("refusing to touch malformed .claude/settings.local.json", file=sys.stderr)
    sys.exit(1)

# Replace rather than stack: two copies deliver each message twice and advance
# the cursor once, which reads as the other agent repeating itself.
replaced = strip_llm_chat(local)
hooks = local.setdefault("hooks", {})

# Fast path: fires after every tool call, so a reply reaches an agent that is
# WORKING within one tool call.
hooks.setdefault("PostToolUse", []).append({
    "matcher": ".*",
    "hooks": [{
        "type": "command",
        "command": hook_cmd,
        "timeout": 10,
        "statusMessage": "llm_chat: anything waiting?",
    }],
})

# START path, and the waker cannot serve it. The waker is registered on
# SessionStart too, but it is `asyncRewake` with a week-long timeout — it
# blocks in the background and returns only when it delivers, so it has no
# way to put a line in front of a session that is starting. This one is
# synchronous and answers immediately.
#
# It exists because of #20: after a host restart the poll keeps running and
# wakes stop landing, `doctor` diagnoses it precisely, and nothing surfaces
# that diagnosis — a message addressed to an agent sat 32 minutes while two
# agents in one room each concluded the other had gone quiet. The restart is
# the moment the session goes deaf, so the session start is where it has to be
# said. It also hands over anything that arrived while the session was down,
# which used to wait for the first tool call.
hooks.setdefault("SessionStart", []).append({
    "hooks": [{
        "type": "command",
        "command": hook_cmd,
        "timeout": 10,
        "statusMessage": "llm_chat: anything waiting?",
    }],
})

# Idle path: PostToolUse cannot fire when no tools are firing, so an agent that
# has ended its turn would never hear anything. asyncRewake lets this block in
# the background after turn-end and wake the session on arrival. The long
# timeout is the listen window, not a stall: it exits as soon as it delivers,
# when every room closes, or when its own budget elapses.
#
# Registered on BOTH events, and SessionStart is not optional. Stop alone arms
# the listener only when a turn ENDS — so a session that starts and never takes
# a turn (a window reload, a resume) has nothing listening and cannot be woken
# by anything, permanently. Observed exactly that way: a reload left this agent
# stalled until a human typed at it. SessionStart re-arms on start, which is
# what makes a reload recoverable rather than a one-way door. Same shape as the
# doorbell in ~/dev/agentic_trading, which pairs the two for the same reason.
wake_entry = {
    "hooks": [{
        "type": "command",
        "command": wake_cmd,
        "asyncRewake": True,
        "timeout": 604800,
        "statusMessage": "llm_chat: listening for replies",
    }],
}
import copy
hooks.setdefault("Stop", []).append(copy.deepcopy(wake_entry))
hooks.setdefault("SessionStart", []).append(copy.deepcopy(wake_entry))
save(local_path, local)

print(("updated" if replaced else "added"),
      "llm_chat hooks (deliver on PostToolUse+SessionStart, waker on "
      "Stop+SessionStart)"
      + (" (migrated out of tracked settings.json)" if migrated else ""))
PY

# Record WHICH hook scripts this repo was wired from. Asking "is a hook missing
# from settings.json" catches an absent hook and nothing else — not a hook whose
# script was rewritten behind an unchanged command line, which no reading of the
# registration can see. The stamp catches every kind of drift; the hook
# comparison is what makes it actionable by naming which one.
FP="$(python3 "$HERE/bin/llm_chat" fingerprint 2>/dev/null || echo unknown)"
mkdir -p "$TARGET/.llm_chat"
python3 - "$TARGET/.llm_chat/installed.json" "$FP" "$HERE" <<'PY'
import json, sys, time
path, fingerprint, checkout = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path, "w") as f:
    json.dump({"fingerprint": fingerprint, "checkout": checkout,
               "at": int(time.time())}, f, indent=2)
    f.write("\n")
PY
echo "stamped install ($FP)"

# THE SKILL — installed ONCE, machine-wide, not per repo. It used to be copied
# into every target's own .claude/skills/, on the reasoning that a file in
# THAT repo, visible to git, was evidence a human authorized llm_chat there
# specifically. That reasoning did not hold: llm_chat is not a per-repo
# capability to begin with — the server is loopback-only, and the hooks and
# .llm_chat/ state installed above are BOTH already gitignored. Duplicating
# the skill into every repo bought no sharing, only copies to keep in sync.
#
# What actually needs to be per-repo is the EVIDENCE that a human authorized
# llm_chat HERE — and that already exists, two steps up: .llm_chat/installed.json,
# gitignored, written only by this script, timestamped before any message
# could reference it. The skill's own instructions point an agent at
# `llm_chat doctor` to read that evidence, rather than asking "is this 500-line
# file physically present in my repo" to answer a question the file's presence
# elsewhere on the machine cannot answer either way.
#
# Placeholder substituted here rather than left generic: this checkout's own
# path IS the answer to "which llm_chat" for whoever runs this script, and a
# machine-wide file has no per-repo template to fall back on later.
SKILL_DIR="$HOME/.claude/skills/llm-chat"
mkdir -p "$SKILL_DIR"
if sed "s#<path-to-llm_chat-checkout>#$HERE#g" "$HERE/templates/skill/SKILL.md" \
    > "$SKILL_DIR/SKILL.md" 2>/dev/null; then
  echo "installed skill  ~/.claude/skills/llm-chat/SKILL.md (machine-wide)"
else
  echo "WARNING: could not install the skill from $HERE/templates/skill/SKILL.md"
  echo "  Hooks are wired, but no skill points an agent at \`llm_chat doctor\`"
  echo "  before it trusts an invite."
fi

# Migrate away from the OLD per-repo copy, the same way settings.json gets
# migrated above: remove it here rather than leave a stale duplicate a human
# has to notice and clean up by hand. Only ever removes exactly what this
# script itself would have written — the file and, once empty, the directory
# — never a skill something else put there under the same name.
OLD_SKILL="$TARGET/.claude/skills/llm-chat/SKILL.md"
if [ -f "$OLD_SKILL" ]; then
  rm -f "$OLD_SKILL"
  rmdir "$TARGET/.claude/skills/llm-chat" 2>/dev/null || true
  rmdir "$TARGET/.claude/skills" 2>/dev/null || true
  echo "removed the old per-repo skill copy — .claude/skills/llm-chat/SKILL.md"
fi

# Both entries are per-machine: the identity would let a teammate's checkout
# claim this project's seat in a room, and settings.local.json holds an
# absolute path to this machine's checkout. Neither may be committable.
#
# ASK GIT, NOT ONE FILE. This used to `grep -qsx` the target's .gitignore, so a
# repo that ignored these paths ANYWHERE ELSE looked un-ignored and the lines
# were appended again on every run. `.git/info/exclude` is the git-provided
# place for exactly this — "ignore my tooling in my clone without telling my
# teammates about it" — and a shared monorepo will reasonably choose it.
#
# Re-running is not optional: a changed hook script makes the wiring stale and
# llm_chat itself demands a re-install. So the old check re-added lines a repo
# had deliberately moved, on every upgrade, and the only defence was a human
# remembering to revert them. Twice in one day, reported.
#
# `git check-ignore` consults .gitignore, .git/info/exclude and
# core.excludesFile together — the same resolution git itself uses, so the
# guard finally matches the question being asked.
GITIGNORE="$TARGET/.gitignore"
# THREE OUTCOMES, not two. `git check-ignore` exits 0 for ignored, 1 for not
# ignored, and 128 when it could not answer at all — no git on PATH, or a
# target that is not a repository. Folding 128 into "not ignored" is a check
# failing safe into a real answer: on a machine without git it would silently
# append the entries on EVERY run, which is precisely the bug this replaced,
# recreated for the one operator least able to see why.
#
# Writing is still the right action when we cannot tell — the identity must
# not become committable — but it stops being silent.
CANNOT_TELL=""
already_ignored() {
  git -C "$TARGET" check-ignore -q "$1" 2>/dev/null
  case $? in
    0) return 0 ;;
    1) return 1 ;;
    *) CANNOT_TELL="yes"; return 1 ;;
  esac
}
add_ignore() {
  if already_ignored "$1"; then return; fi
  if [ "$NO_GITIGNORE" = 1 ]; then
    UNIGNORED="$UNIGNORED $1"
    return
  fi
  if [ -s "$GITIGNORE" ]; then printf '\n' >> "$GITIGNORE"; fi
  printf '# %s\n%s\n' "$2" "$1" >> "$GITIGNORE"
  echo "gitignored $1"
}
UNIGNORED=""
add_ignore ".llm_chat/" "llm_chat identity for this project"
add_ignore ".claude/settings.local.json" "machine-local hook config (absolute paths)"

if [ -n "$CANNOT_TELL" ]; then
  echo "NOTE: could not ask git whether those paths are already ignored"
  echo "  (no git on PATH, or $TARGET is not a repository). Acted as though"
  echo "  they were not, which is the safe direction — but if you keep these"
  echo "  ignored somewhere this cannot see, every re-install will add them"
  echo "  again and this line is the only warning you will get."
fi

# DECLINING MUST NOT BE SILENT. Writing to a shared repo's .gitignore is that
# repo's decision, not this installer's — but the protection being declined is
# real, so the operator has to be choosing an unignored state rather than
# stumbling into one. Named paths, not a general warning.
if [ -n "$UNIGNORED" ]; then
  echo ""
  echo "NOT IGNORED, and --no-gitignore means nothing was written:"
  for path in $UNIGNORED; do echo "  $path"; done
  echo "  Nothing here stops these being committed. .llm_chat/ holds this"
  echo "  project's identity — committed, a teammate's checkout claims its seat"
  echo "  in a room — and settings.local.json holds absolute paths to THIS"
  echo "  machine. .git/info/exclude prevents a commit exactly as well as"
  echo "  .gitignore and tells nobody else about your tools:"
  for path in $UNIGNORED; do
    echo "    echo '$path' >> $TARGET/.git/info/exclude"
  done
fi

# Register the MCP server too, so the same CLI shows up as structured tools
# for an MCP client, not just delivered messages. Local scope, for the same
# reason the hook went into settings.local.json rather than settings.json:
# the command is an absolute path to this machine's checkout, and `claude
# mcp add` for local scope already keeps that out of anything tracked
# (~/.claude.json, not the repo). Remove-then-add rather than checking first,
# matching "replace rather than stack" above — re-running always converges on
# the current checkout's path rather than leaving a stale one in place.
#
# Best-effort: the hook is what makes this install work at all, and neither a
# missing `claude` binary nor an older CLI without `mcp add` may fail the rest
# of it over an optional convenience.
MCP_NAME="llm_chat"
if command -v claude >/dev/null 2>&1; then
  if MCP_OUT="$(
      cd "$TARGET" \
        && { claude mcp remove "$MCP_NAME" --scope local >/dev/null 2>&1 || true; } \
        && claude mcp add --scope local "$MCP_NAME" -- python3 "$MCP" 2>&1
    )"; then
    echo "registered $MCP_NAME as an MCP server (local scope) in $TARGET"
  else
    echo "could not register the MCP server — the hook above is unaffected. Register by hand:"
    echo "  cd $TARGET && claude mcp add --scope local $MCP_NAME -- python3 $MCP"
    printf '%s\n' "$MCP_OUT" | sed 's/^/  /'
  fi
else
  echo "\`claude\` not on PATH, skipped the MCP server — register by hand once it is:"
  echo "  cd $TARGET && claude mcp add --scope local $MCP_NAME -- python3 $MCP"
fi

cat <<EOF

Installed into $TARGET

The hook went into .claude/settings.local.json (machine-local, gitignored) — not
the tracked settings.json, because its command is an absolute path to this
machine.

It is live for NEW sessions there. An ALREADY-RUNNING session may or may not
pick it up — both outcomes have been observed on one machine, so do not count
on it. If nothing is ever delivered, start a new session; \`llm_chat read
<channel>\` works either way and never depends on the hook.

Nothing is delivered until that project joins a channel:

  $HERE/bin/llm_chat join <channel> --as <identity>

EOF
