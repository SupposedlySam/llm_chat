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
TARGET="${1:-}"

if [ -z "$TARGET" ]; then
  echo "usage: ./install.sh <path-to-repo>" >&2
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
chmod +x "$HOOK" "$WAKE" "$HERE/bin/llm_chat"

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
      "llm_chat hooks (PostToolUse + Stop/SessionStart waker)"
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

# Created when absent, not just appended to. Both entries are per-machine: the
# identity would let a teammate's checkout claim this project's seat in a room,
# and settings.local.json holds an absolute path to this machine's checkout.
# A global ~/.config/git/ignore may already cover the latter — this does not
# assume every machine has one.
GITIGNORE="$TARGET/.gitignore"
add_ignore() {
  if ! grep -qsx "$1" "$GITIGNORE"; then
    if [ -s "$GITIGNORE" ]; then printf '\n' >> "$GITIGNORE"; fi
    printf '# %s\n%s\n' "$2" "$1" >> "$GITIGNORE"
    echo "gitignored $1"
  fi
}
add_ignore ".llm_chat/" "llm_chat identity for this project"
add_ignore ".claude/settings.local.json" "machine-local hook config (absolute paths)"

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
