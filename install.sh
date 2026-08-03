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
chmod +x "$HOOK" "$HERE/bin/llm_chat"

python3 - "$LOCAL" "$SHARED" "$HOOK" <<'PY'
import json, os, sys

local_path, shared_path, hook_cmd = sys.argv[1], sys.argv[2], sys.argv[3]

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

def strip_llm_chat(settings):
    """Drop our hook wherever it already is, and report whether we found one."""
    found = False
    hooks = settings.get("hooks", {})
    post = hooks.get("PostToolUse")
    if not isinstance(post, list):
        return False
    kept_groups = []
    for group in post:
        kept = [h for h in group.get("hooks", [])
                if "llm-chat-deliver" not in (h.get("command") or "")]
        if len(kept) != len(group.get("hooks", [])):
            found = True
        if kept:
            group["hooks"] = kept
            kept_groups.append(group)
        elif not group.get("hooks"):
            kept_groups.append(group)   # a group that was empty before us
    hooks["PostToolUse"] = kept_groups
    if not kept_groups:
        hooks.pop("PostToolUse", None)
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
local.setdefault("hooks", {}).setdefault("PostToolUse", []).append({
    "matcher": ".*",
    "hooks": [{
        "type": "command",
        "command": hook_cmd,
        "timeout": 10,
        "statusMessage": "llm_chat: anything waiting?",
    }],
})
save(local_path, local)

print(("updated" if replaced else "added"), "llm_chat PostToolUse hook"
      + (" (migrated out of tracked settings.json)" if migrated else ""))
PY

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

It is live for NEW sessions there, and for the current one as soon as the
harness re-reads settings. Nothing is delivered until that project joins a
channel:

  $HERE/bin/llm_chat join <channel> --as <identity>

EOF
