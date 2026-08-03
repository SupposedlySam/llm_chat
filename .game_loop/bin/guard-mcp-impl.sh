#!/usr/bin/env bash
# guard-mcp — classify an MCP tool call BEFORE it runs, and refuse the ones that mutate. A PreToolUse
# hook matched on `mcp__.*` (LOUD rung: fails at the point of misuse, with a reason).
#
# THE HOLE THIS CLOSES (issue #23): the write guard reads Bash command text. A Claude Code session
# with MCP servers connected can take an irreversible action with NO shell command at all — a
# `DELETE FROM ...` through a database server, a send or a delete through a mail/chat server, a
# force-operation through a git-host server. None of those arrive as Bash, so a guard that only reads
# Bash never sees them. guard-writes-impl.sh has said so in prose since it was written:
#
#     "DOES NOT: catch mutations made via MCP tools ..."
#
# A gap stated in prose is a gap that gets walked through, because the note does not stop anything
# (INV1: enforcement lives in tools, never in instructions). MCP tools are a first-class effector, on
# par with the shell; INV3 ("everything outside this repo is READ-ONLY") was enforced on one of them.
#
# THE MODEL — three-way, fail CLOSED on the third:
#   ALLOW    the tool NAME's verb is read-only AND no argument carries a mutation.
#   DENY     the NAME's verb mutates, OR the ARGUMENT SHAPE carries one (a mutating SQL statement, a
#            destructive flag, a mutating request method) — the argument always wins, so a read-named
#            `query` tool carrying `DELETE FROM` is still denied.
#   DENY     anything else. Unclassifiable is refused, not waved through.
# Per-server semantics are undocumented-vendor-shaped: there is no schema that says which of a
# server's tools mutate. So this matches the two things that ARE observable — the tool NAME and the
# ARGUMENT SHAPE — and treats "I do not recognise this verb" as a refusal rather than a permission.
# That is the opposite default from the Bash write guard, and deliberately: see guard-mcp.sh for why
# the INV5 hazard that forces THAT guard open does not exist on this path.
#
# SCOPE — what this DOES and does NOT catch (a guard that overstates its reach buys false confidence):
#   DOES: refuse an MCP call whose tool name carries a mutating/irreversible verb (delete, send,
#         push, merge, deploy, force, run, ...), in first position or anywhere for the hard verbs.
#   DOES: refuse an MCP call whose arguments contain a mutating SQL statement (DELETE FROM, DROP,
#         TRUNCATE, INSERT, UPDATE ... SET, ALTER, GRANT/REVOKE), a destructive flag (--force,
#         --hard, rm -rf), a truthy force/delete/overwrite flag, or a mutating request method.
#   DOES: refuse a call it cannot classify at all.
#   DOES NOT: know what a server actually DOES. Classification is a heuristic over a NAME and an
#         ARGUMENT BLOB. A server whose read-named tool mutates behind the scenes is invisible here,
#         and always will be from this side of the wire — the name is all the client is given.
#   DOES NOT: see effects DOWNSTREAM of a call it allowed (a read that trips a server-side webhook,
#         a search that bills, a fetch that marks a message seen).
#   DOES NOT: see a mutation encoded where it cannot read it — a base64 or binary blob, a stored
#         procedure or template id whose EXPANSION mutates, a saved-query handle, an opaque cursor.
#   DOES NOT: see MCP calls made anywhere this hook is not installed (another checkout, another MCP
#         client, a server driven out-of-band). Hooks gate this session, not the server.
#   DOES NOT: replace the MCP server's own permission model, or the credentials it was handed.
#         Access is not permission (INV3) — but this guard never SEES the credential.
#   DOES: over-refuse, on purpose. Prose in an argument that merely mentions `--force`, and any verb
#         not in either list, are denied. Fail-closed spends the error budget on refusing safe calls
#         rather than on permitting irreversible ones; the human is the escape hatch either way.
#   Do not read silence here as safety.
#
# THE ESCAPE HATCH IS THE HUMAN, deliberately, and it is the SAME one the shell mutators use — no env
# override, single-use, logged forever:
#     game_loop authorize --path mcp__<server>__<tool> --reason "<their exact words>"
# An MCP authorization is spelled with the tool name and is spendable ONLY by an MCP call; a
# filesystem authorization is spelled with a path and cannot be spent here. One authorization, one
# call, one permanent log line.

set -uo pipefail
payload=$(cat)

GAMELOOP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"    # .game_loop/
CONFIG_F="$GAMELOOP_DIR/config.json"

# State is per-session: an authorization is granted IN a session and spendable only THERE. Mirrors
# guard-writes-impl.sh and set_session() in bin/game_loop — payload session_id, then env, then the
# repo-global legacy file.
SID=$(printf '%s' "$payload" | python3 -c '
import json, os, re, sys
try:
    sid = json.load(sys.stdin).get("session_id") or ""
except (ValueError, OSError):
    sid = ""
sid = sid or os.environ.get("GAME_LOOP_SESSION") or os.environ.get("CLAUDE_CODE_SESSION_ID") or ""
print(re.sub(r"[^A-Za-z0-9._-]", "-", sid.strip())[:64])' 2>/dev/null)
if [ -n "$SID" ]; then
  STATE_F="$GAMELOOP_DIR/sessions/$SID/state.json"
else
  STATE_F="$GAMELOOP_DIR/state.json"
fi

deny() {
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":%s}}\n' \
    "$(printf '%s' "$1" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
  exit 0
}

# One classification pass. Prints "ALLOW", or "DENY" followed by the reason body.
#
# NB: this Python is embedded in a $(...) here-doc, so it must contain NO backtick, NO dollar-paren,
# and NO literal here-doc operator — any of those derails bash's parse of the surrounding $(...).
verdict=$(GAMELOOP_DIR="$GAMELOOP_DIR" CONFIG_F="$CONFIG_F" STATE_F="$STATE_F" SID="$SID" \
          python3 - "$payload" <<'PY'
import datetime, json, os, re, sys

try:
    payload = json.loads(sys.argv[1])
except (ValueError, IndexError):
    payload = {}
if not isinstance(payload, dict):
    payload = {}

tool = payload.get("tool_name") or ""

# Not an MCP call: not this guard's business. The matcher already scopes us, but a hook can be
# invoked by hand or mis-registered, and a guard that denies tools it was never meant to see is a
# guard that gets switched off.
if not tool.startswith("mcp__"):
    print("ALLOW")
    sys.exit(0)

parts = tool.split("__")
leaf = parts[-1] if len(parts) > 2 else ""
server = parts[1] if len(parts) > 2 else ""
server_prefix = "__".join(parts[:2]) + "__" if len(parts) > 2 else tool


def tokens(name):
    """Split a vendor tool name into lowercase words: camelCase, snake_case, kebab, digits."""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)
    return [w for w in re.split(r"[^A-Za-z0-9]+", s.lower()) if w]


# Read-only verbs. Tight on purpose: a verb that is only USUALLY read-only belongs in neither list,
# where it lands in the ambiguous bucket and is refused.
READ_VERBS = {
    "get", "list", "read", "search", "find", "view", "show", "fetch", "describe", "inspect",
    "count", "stat", "status", "diff", "log", "logs", "head", "tail", "query", "select",
    "check", "exists", "browse", "enumerate", "peek", "print", "info",
}

# Mutating / irreversible verbs, recognised in FIRST position (the verb slot of a vendor name).
MUTATE_VERBS = {
    "create", "delete", "remove", "destroy", "drop", "truncate", "purge", "wipe", "empty",
    "insert", "update", "upsert", "put", "patch", "post", "write", "edit", "modify", "set",
    "add", "append", "send", "email", "mail", "reply", "comment", "publish", "deploy",
    "release", "push", "force", "merge", "rebase", "reset", "revert", "restore", "rollback",
    "apply", "exec", "execute", "run", "invoke", "trigger", "dispatch", "start", "stop",
    "restart", "cancel", "kill", "terminate", "shutdown", "reboot", "flash", "install",
    "uninstall", "upgrade", "downgrade", "move", "rename", "copy", "upload", "download",
    "transfer", "archive", "unarchive", "close", "reopen", "approve", "unapprove", "submit",
    "assign", "unassign", "invite", "revoke", "grant", "ban", "kick", "mute", "lock", "unlock",
    "enable", "disable", "subscribe", "unsubscribe", "sync", "migrate", "provision", "scale",
    "rotate", "sign", "pay", "charge", "refund", "order", "book", "schedule", "clear",
}

# Verbs so destructive they count ANYWHERE in the name, not just in the verb slot — this is what
# catches `branchForcePush` or `getOrDeleteRecord`. Kept small and unambiguous: each of these is
# almost never a NOUN in a read-only tool name, so it does not misfire on `getLatestRelease`.
HARD_VERBS = {"delete", "destroy", "truncate", "purge", "wipe", "force"}

words = tokens(leaf) or tokens(tool)
# Some servers repeat their own name inside the tool name (`mcp__pebble__pebble_run_command`).
# Strip that prefix so the real VERB lands in the verb slot instead of the server's name — otherwise
# every tool on such a server reads as unclassifiable, and fail-closed becomes fail-useless.
srv = set(tokens(server))
while len(words) > 1 and words[0] in srv:
    words = words[1:]
first = words[0] if words else ""

# A project may teach the guard which of a server's tools are read-only, for the ambiguous ones it
# cannot name for itself: config.json -> mcp_read_only_tools, exact tool names or `mcp__server__`
# prefixes. This ONLY resolves ambiguity. It can never override a mutating verb or a mutating
# argument — an allowlist that can silence a detected mutation is a bypass with a friendly name.
allow_names = []
try:
    with open(os.environ["CONFIG_F"]) as f:
        allow_names = [str(x) for x in (json.load(f).get("mcp_read_only_tools") or [])]
except (OSError, ValueError, KeyError):
    pass
named_read_only = any(tool == a or (a.endswith("__") and tool.startswith(a)) for a in allow_names)

# ── argument shape ───────────────────────────────────────────────────────────────────────────────
SQL_MUTATION = re.compile(
    r"(?is)\b(?:"
    r"delete\s+from"
    r"|drop\s+(?:table|database|schema|index|view|collection|role|user)"
    r"|truncate\s+(?:table\s+)?[\w.]"
    r"|insert\s+into"
    r"|update\s+[\w.]+\s+set\b"
    r"|alter\s+(?:table|database|schema)"
    r"|create\s+(?:table|database|schema|index|view)"
    r"|replace\s+into"
    r"|merge\s+into"
    r"|grant\s+[\w,\s]+\bon\b"
    r"|revoke\s+[\w,\s]+\bon\b"
    r")")

DESTRUCTIVE_TEXT = re.compile(
    r"(?i)(--force\b|--hard\b|--no-verify\b|\bforce-push\b|\brm\s+-[a-zA-Z]*[rf]|\bshutdown\b"
    r"|\breboot\b|\bmkfs\b|\bdd\s+if=)")

# Key names whose truthy value is itself the destructive decision.
FLAG_WORDS = ("force", "delete", "destroy", "remove", "purge", "overwrite", "hard", "cascade")

MUTATING_METHODS = {"DELETE", "PUT", "POST", "PATCH"}


def leaves(node, path=""):
    """Flatten tool_input to (keypath, scalar) pairs — arguments nest, and a mutation three levels
    down is still a mutation."""
    out = []
    if isinstance(node, dict):
        for k, v in node.items():
            out.extend(leaves(v, path + "/" + str(k)))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            out.extend(leaves(v, path + "/" + str(i)))
    else:
        out.append((path or "/", node))
    return out


findings = []
for keypath, val in leaves(payload.get("tool_input") or {}):
    key = keypath.lower()
    if isinstance(val, str):
        if SQL_MUTATION.search(val):
            findings.append("a mutating SQL statement in argument " + keypath)
        if DESTRUCTIVE_TEXT.search(val):
            findings.append("a destructive verb in argument " + keypath)
        if val.strip().upper() in MUTATING_METHODS:
            findings.append("a mutating request method in argument " + keypath)
    truthy = val is True or (isinstance(val, str) and val.strip().lower() == "true")
    if truthy and any(w in key for w in FLAG_WORDS):
        findings.append("a destructive flag set true: " + keypath)
findings = list(dict.fromkeys(findings))

# ── verdict ──────────────────────────────────────────────────────────────────────────────────────
hard_hit = sorted(set(words) & HARD_VERBS)
if findings:
    kind = "MUTATING"
    why = ("its ARGUMENTS carry a mutation:\n" + "\n".join("    - " + f for f in findings))
elif hard_hit:
    kind = "MUTATING"
    why = "its NAME carries an irreversible verb: '" + hard_hit[0] + "'"
elif first in MUTATE_VERBS:
    kind = "MUTATING"
    why = "its NAME's verb mutates: '" + first + "'"
elif first in READ_VERBS or named_read_only:
    print("ALLOW")
    sys.exit(0)
else:
    kind = "UNCLASSIFIABLE"
    why = ("its verb '" + (first or "?") + "' is in neither the read-only nor the mutating list, so "
           "whether it is reversible is UNKNOWN")


def consume_authorization(tool_name):
    """Spend a human authorization that names this MCP tool. Same primitive as the write guard's —
    arm, gate, CONSUME — but matched on the TOOL NAME rather than a path prefix, because an MCP call
    has no path. `game_loop authorize` realpaths what it is given, so the recorded value is
    "<cwd>/mcp__server__tool"; both the whole string and its basename are tried, and only candidates
    that look like an MCP tool name count. That keeps the two hatches separate in both directions: a
    path authorization can never be spent by an MCP call, nor an MCP one by a filesystem write."""
    state_f = os.environ["STATE_F"]
    try:
        with open(state_f) as f:
            st = json.load(f)
    except (OSError, ValueError):
        return False
    for a in st.get("authorized", []):
        if a.get("uses_left", 0) <= 0:
            continue
        recorded = a.get("path", "") or ""
        for cand in (recorded, os.path.basename(recorded)):
            if not cand.startswith("mcp__"):
                continue
            if tool_name == cand or tool_name.startswith(cand):
                a["uses_left"] -= 1
                try:
                    with open(state_f, "w") as f:
                        json.dump(st, f, indent=2)
                        f.write("\n")
                    log_f = os.path.join(os.environ["GAMELOOP_DIR"], "log.jsonl")
                    with open(log_f, "a") as f:
                        rec = {"t": datetime.datetime.now().isoformat(timespec="seconds")}
                        sid = os.environ.get("SID", "")
                        if sid:
                            rec["sid"] = sid[:8]
                        rec.update({"kind": "authorized_mcp", "tool": tool_name,
                                    "reason": a.get("reason"), "uses_left": a["uses_left"]})
                        f.write(json.dumps(rec) + "\n")
                except OSError:
                    return False
                return True
    return False


if consume_authorization(tool):
    print("ALLOW")
    sys.exit(0)

if kind == "MUTATING":
    body = ("BLOCKED: MCP tool call classified as MUTATING -> " + tool + "\n\n"
            "This call was refused because " + why + "\n\n"
            "MCP tools are a first-class effector, on par with the shell: a delete, a send or a\n"
            "force-operation through a server is just as irreversible as one through Bash, and the\n"
            "write guard never sees it. An unattended run does not fire these.")
else:
    body = ("BLOCKED: MCP tool call could not be classified -> " + tool + "\n\n"
            "This guard FAILS CLOSED: " + why + ". It reads only the tool NAME and the ARGUMENT\n"
            "SHAPE — no server tells it which of its tools mutate — so an unrecognised verb is\n"
            "refused rather than assumed safe.\n\n"
            "If this tool is genuinely read-only, teach the guard instead of bypassing it:\n"
            "  .game_loop/config.json -> mcp_read_only_tools: [\"" + tool + "\"]\n"
            "  (or \"" + server_prefix + "\" for the whole server). That list only resolves\n"
            "  ambiguity — it can never silence a mutating verb or a mutating argument.")

print("DENY")
print(body + "\n\n"
      "If the human has explicitly authorized this one call, record their words and try again:\n"
      "  game_loop authorize --path " + tool + " --reason \"<their exact words>\"\n"
      "One authorization, one call, logged permanently. That is the only escape hatch, by design.")
PY
)

if [ "$(printf '%s\n' "$verdict" | head -1)" = "DENY" ]; then
  deny "$(printf '%s\n' "$verdict" | tail -n +2)"
fi

exit 0
