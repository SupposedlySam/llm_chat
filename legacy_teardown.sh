#!/usr/bin/env bash
# Remove llm_chat from a repo, so it can be set up fresh.
#
#   ./legacy_teardown.sh ~/dev/some-project           # do it
#   ./legacy_teardown.sh --dry-run ~/dev/some-project # show what it would do
#
# Undoes everything install.sh and `llm_chat setup` leave in a target repo, and
# cleans up after OLDER installs too — the ones that wrote hooks into the
# tracked .claude/settings.json and dropped .claude/settings.json.bak.<epoch>
# beside it. That legacy state is the reason this exists: re-installing alone
# will not remove a hook from a file the installer no longer writes to.
#
# What it does, in order:
#   1. stops this project's background waker, if one is polling
#   2. leaves any joined channels (skip with --keep-membership)
#   3. strips llm_chat hooks from settings.local.json AND settings.json
#   4. deletes .llm_chat/ and any legacy .claude/settings.json.bak.*
#   5. removes the .gitignore entry it added for .llm_chat/
#
# It only ever removes things it can positively identify as ours: hooks whose
# command names llm-chat-deliver or llm-chat-wake, and the exact comment+entry
# pair install.sh writes. Anything else in those files is left alone.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY=0
KEEP_MEMBERSHIP=0
PURGE_GITIGNORE=0
TARGET=""

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --keep-membership) KEEP_MEMBERSHIP=1 ;;
    --purge-gitignore) PURGE_GITIGNORE=1 ;;
    -h|--help)
      sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
    *)  TARGET="$1" ;;
  esac
  shift
done

if [ -z "$TARGET" ]; then
  echo "usage: ./legacy_teardown.sh [--dry-run] [--keep-membership] [--purge-gitignore] <path-to-repo>" >&2
  exit 2
fi
if [ ! -d "$TARGET" ]; then
  echo "no such directory: $TARGET" >&2
  exit 1
fi
TARGET="$(cd "$TARGET" && pwd)"

say() { if [ "$DRY" = 1 ]; then echo "  would $*"; else echo "  $*"; fi; }

# The JSON and .gitignore surgery lives in real files rather than inline
# heredocs: bash mis-parses parentheses inside a heredoc nested in a command
# substitution, which fails at PARSE time — the script dies before doing
# anything, but only once the Python is complex enough to contain them.
WORK="$(mktemp -d "${TMPDIR:-/tmp}/llm_chat-teardown.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

cat > "$WORK/strip_hooks.py" <<'PY'
import json, os, sys
path, dry = sys.argv[1], sys.argv[2] == "1"
OURS = ("llm-chat-deliver", "llm-chat-wake")
try:
    with open(path) as fh:
        s = json.load(fh)
except (OSError, ValueError):
    print("skip malformed"); raise SystemExit
removed = 0
hooks = s.get("hooks", {})
for event in ("PostToolUse", "Stop"):
    entries = hooks.get(event)
    if not isinstance(entries, list):
        continue
    kept_groups = []
    for g in entries:
        original = g.get("hooks", [])
        kept = [h for h in original
                if not any(o in (h.get("command") or "") for o in OURS)]
        removed += len(original) - len(kept)
        if kept or not original:
            g["hooks"] = kept
            kept_groups.append(g)
    if kept_groups:
        hooks[event] = kept_groups
    else:
        hooks.pop(event, None)
if not hooks:
    s.pop("hooks", None)
if removed and not dry:
    # An emptied settings.local.json is ours to delete; settings.json may be
    # the project's own file and is only ever rewritten, never removed.
    if not s and path.endswith("settings.local.json"):
        os.unlink(path)
        print("removed %d hook(s), deleted empty file" % removed)
        raise SystemExit
    with open(path, "w") as fh:
        json.dump(s, fh, indent=2); fh.write("\n")
print("removed %d hook(s)" % removed if removed else "nothing of ours")
PY

cat > "$WORK/clean_gitignore.py" <<'PY'
import sys
path, dry, purge = sys.argv[1], sys.argv[2] == "1", sys.argv[3] == "1"
pairs = [("# llm_chat identity for this project", ".llm_chat/")]
if purge:
    pairs.append(("# machine-local hook config (absolute paths)",
                  ".claude/settings.local.json"))
lines = open(path).read().splitlines()
out, i, dropped = [], 0, 0
while i < len(lines):
    hit = None
    for p in pairs:
        if lines[i].strip() == p[0] and i + 1 < len(lines) and lines[i+1].strip() == p[1]:
            hit = p
            break
    if hit:
        i += 2
        dropped += 1
        while out and not out[-1].strip():   # the blank line we added with it
            out.pop()
        continue
    out.append(lines[i]); i += 1
if dropped and not dry:
    text = "\n".join(out).strip("\n")
    open(path, "w").write(text + "\n" if text else "")
print("removed %d entry/entries" % dropped if dropped else "nothing of ours")
PY

cat > "$WORK/list_joined.py" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    raise SystemExit
for ch, e in (d or {}).items():
    print("\t".join([ch, e.get("identity", ""), e.get("server", "")]))
PY

echo "llm_chat teardown — $TARGET"
[ "$DRY" = 1 ] && echo "  (dry run — nothing will be changed)"

# 1. Stop the waker. It is a background poller armed at turn-end; left running
#    it would keep polling for a project that is no longer set up.
PIDFILE="$TARGET/.llm_chat/wake.pid"
if [ -f "$PIDFILE" ]; then
  WPID="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [ -n "$WPID" ] && kill -0 "$WPID" 2>/dev/null; then
    say "stop waker (pid $WPID)"
    [ "$DRY" = 1 ] || kill "$WPID" 2>/dev/null || true
  fi
fi

# 2. Leave the rooms BEFORE forgetting who we are. Deleting joined.json first
#    would strand the membership server-side: the room would keep listing this
#    project as present, and the other agent would wait for a reply that can no
#    longer come.
JOINED="$TARGET/.llm_chat/joined.json"
if [ "$KEEP_MEMBERSHIP" = 0 ] && [ -f "$JOINED" ]; then
  while IFS=$'\t' read -r channel identity server; do
    [ -z "$channel" ] && continue
    say "leave #$channel as $identity"
    if [ "$DRY" = 0 ]; then
      CLAUDE_PROJECT_DIR="$TARGET" python3 "$HERE/bin/llm_chat" \
        --server "$server" leave "$channel" --as "$identity" >/dev/null 2>&1 || true
    fi
  done < <(python3 "$WORK/list_joined.py" "$JOINED")
fi

# 3. Strip our hooks from both settings files.
for f in settings.local.json settings.json; do
  P="$TARGET/.claude/$f"
  [ -f "$P" ] || continue
  RESULT="$(python3 "$WORK/strip_hooks.py" "$P" "$DRY")"
  [ "$RESULT" = "nothing of ours" ] || say "$f: $RESULT"
done

# 4. Local state, plus the backups older installs leaked into the repo.
if [ -d "$TARGET/.llm_chat" ]; then
  say "delete .llm_chat/"
  [ "$DRY" = 1 ] || rm -rf "$TARGET/.llm_chat"
fi
for b in "$TARGET"/.claude/settings.json.bak.*; do
  [ -e "$b" ] || continue
  say "delete legacy backup $(basename "$b")"
  [ "$DRY" = 1 ] || rm -f "$b"
done
if [ -d "$TARGET/.claude" ] && [ -z "$(ls -A "$TARGET/.claude" 2>/dev/null)" ]; then
  say "remove empty .claude/"
  [ "$DRY" = 1 ] || rmdir "$TARGET/.claude"
fi

# 5. The .gitignore entry. The .claude/settings.local.json line is KEPT by
#    default: other tools put machine-specific absolute paths in that file too,
#    and un-ignoring it could get someone's local config committed. Removing
#    our line is safe; removing theirs is not our call. --purge-gitignore opts
#    into taking both.
GITIGNORE="$TARGET/.gitignore"
if [ -f "$GITIGNORE" ]; then
  RESULT="$(python3 "$WORK/clean_gitignore.py" "$GITIGNORE" "$DRY" "$PURGE_GITIGNORE")"
  [ "$RESULT" = "nothing of ours" ] || say ".gitignore: $RESULT"
fi

echo
if [ "$DRY" = 1 ]; then
  echo "Dry run only. Re-run without --dry-run to apply."
else
  echo "Done. Set up fresh with:"
  echo "  $HERE/bin/llm_chat setup <channel> --as <identity>"
  echo "run from inside $TARGET."
fi
