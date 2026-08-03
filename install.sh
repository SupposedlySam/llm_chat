#!/usr/bin/env bash
# Register the llm_chat delivery hook in another repo.
#
#   ./install.sh ~/dev/some-project
#
# Adds ONE PostToolUse hook to that project's .claude/settings.json and touches
# nothing else. Existing hooks — write guards, watchdogs, anything you have
# added — are preserved; the settings file is merged, never rewritten.
#
# Re-running is safe: the hook is matched by its command path, so a second run
# updates in place rather than adding a duplicate that would deliver every
# message twice.
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

SETTINGS="$TARGET/.claude/settings.json"
mkdir -p "$TARGET/.claude"
[ -f "$SETTINGS" ] || echo '{}' > "$SETTINGS"

# Back up before touching someone else's config. This file carries their
# guards; a bad merge here is not a small inconvenience.
cp "$SETTINGS" "$SETTINGS.bak.$(date +%s)"

HOOK="$HERE/bin/llm-chat-deliver"
chmod +x "$HOOK" "$HERE/bin/llm_chat"

python3 - "$SETTINGS" "$HOOK" <<'PY'
import json, sys

settings_path, hook_cmd = sys.argv[1], sys.argv[2]
with open(settings_path) as f:
    settings = json.load(f)

hooks = settings.setdefault("hooks", {})
post = hooks.setdefault("PostToolUse", [])

entry = {
    "matcher": ".*",
    "hooks": [{
        "type": "command",
        "command": hook_cmd,
        "timeout": 10,
        "statusMessage": "llm_chat: anything waiting?",
    }],
}

# Replace an existing llm_chat hook rather than stacking another one beside it.
# Two copies would deliver each message twice and advance the cursor once,
# which reads as the other agent repeating itself.
replaced = False
for group in post:
    for h in group.get("hooks", []):
        if "llm-chat-deliver" in (h.get("command") or ""):
            h.update(entry["hooks"][0])
            replaced = True
if not replaced:
    post.append(entry)

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")

print("updated" if replaced else "added", "llm_chat PostToolUse hook")
PY

GITIGNORE="$TARGET/.gitignore"
if [ -f "$GITIGNORE" ] && ! grep -qx ".llm_chat/" "$GITIGNORE"; then
  printf '\n# llm_chat identity for this project\n.llm_chat/\n' >> "$GITIGNORE"
  echo "gitignored .llm_chat/"
fi

cat <<EOF

Installed into $TARGET

The hook is live for NEW sessions there, and for the current one as soon as the
harness re-reads settings.json. Nothing is delivered until that project joins a
channel:

  $HERE/bin/llm_chat join <channel> --as <identity>

EOF
