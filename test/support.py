"""Test support: load the entrypoints, and stand in for the server.

Stdlib only, like everything else here. `An agent should not need a pip install
to say hello` applies to the tests too — a suite that needs installing is a
suite that stops being run.

Two problems to solve:

1. The entrypoints are scripts, not modules: `bin/llm_chat`, `bin/llm-chat-wake`
   and `bin/llm-chat-deliver` have no `.py` suffix and two have hyphens, so
   `import` cannot reach them. `SourceFileLoader` can.

2. Almost every behaviour worth defending runs against the zonai server. Mocking
   `rows`/`create`/`update` individually would leave the query construction —
   `eq`, `gt`, `and_`, and the shapes handed to /db — untested, and those are
   exactly where the wire conventions bite. So the fake replaces `call()`, the
   single seam where HTTP happens, and implements the endpoints for real
   against a dict. Everything above it is the production code path.
"""
import importlib.util
import json
import os
from importlib.machinery import SourceFileLoader

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "bin")


def load(script, name=None):
    """Load one of this repo's scripts as a module.

    Accepts a bare name for the bin/ scripts that predate triggers/, and a
    repo-relative path for anything else.
    """
    path = os.path.join(BIN, script) if os.sep not in script \
        else os.path.join(os.path.dirname(BIN), script)
    modname = name or os.path.basename(script).replace("-", "_")
    loader = SourceFileLoader(modname, path)
    spec = importlib.util.spec_from_loader(modname, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


# Every column name the production code actually put on the wire during a run.
# The fake accepts any column, so on its own it would agree with a client that
# had drifted from the Dart schema — a mock that says yes to whatever it is
# asked cannot catch a rename. test/contract.py checks this against the schemas.
OBSERVED_COLUMNS = {}


def _observe(table, where=None, obj=None):
    seen = OBSERVED_COLUMNS.setdefault(table, set())
    if obj:
        seen.update(obj)
    stack = [where] if where else []
    while stack:
        clause = stack.pop()
        if not isinstance(clause, dict):
            continue
        if "column" in clause:
            seen.add(clause["column"])
        stack.extend(clause.get("conditions") or [])


class FakeServer:
    """An in-memory zonai, faithful to the parts llm_chat actually uses.

    Rows are dicts; ids are sequential strings. `where` clauses are evaluated
    with the same operators the CLI builds, so a test that constructs a bad
    query fails here rather than passing against a mock that ignored it.
    """

    def __init__(self):
        self.tables = {}
        self._next_id = 0
        self.calls = []          # so a test can assert what was sent

    # ── query evaluation ────────────────────────────────────────────────────
    def _matches(self, row, where):
        if where is None:
            return True
        kind = where.get("type")
        if kind == "eq":
            return row.get(where["column"]) == where["value"]
        if kind == "gt":
            value = row.get(where["column"])
            return value is not None and value > where["value"]
        if kind == "and":
            return all(self._matches(row, c) for c in where["conditions"])
        raise AssertionError("fake server got an unsupported clause: %r" % kind)

    # ── the one seam ────────────────────────────────────────────────────────
    def call(self, server, method, path, body=None, query=None, timeout=10):
        self.calls.append((method, path, body, query))
        if method == "GET" and path == "/db/list":
            table = self.tables.setdefault(query["table"], [])
            where = query.get("where")
            _observe(query["table"], where=where)
            return {"data": {"items": [dict(r) for r in table
                                       if self._matches(r, where)]}}
        if method == "POST" and path == "/db":
            table = self.tables.setdefault(body["table"], [])
            _observe(body["table"], obj=body["object"])
            row = dict(body["object"])
            self._next_id += 1
            row.setdefault("id", "id%d" % self._next_id)
            table.append(row)
            return {"data": dict(row)}
        if method == "PATCH" and path == "/db":
            table = self.tables.setdefault(body["table"], [])
            fields = {}
            for update in body["updates"]:
                fields.update(update["object"])
            _observe(body["table"], where=body.get("where"), obj=fields)
            hit = 0
            for row in table:
                if self._matches(row, body.get("where")):
                    row.update(fields)
                    hit += 1
            return {"data": {"updated": hit}}
        raise AssertionError("fake server got an unexpected request: %s %s"
                             % (method, path))

    # ── convenience for arranging state ─────────────────────────────────────
    def channel(self, name, **overrides):
        row = {"id": "chan-" + name, "name": name, "topic": None,
               "created_by": "someone", "closed": 0, "closed_reason": None,
               "broadcast": 0,
               "max_messages": 200, "message_count": 0, "created_at": 0}
        row.update(overrides)
        self.tables.setdefault("channels", []).append(row)
        return row

    def membership(self, channel, identity, **overrides):
        row = {"id": "mem-%s-%s" % (channel, identity), "channel": channel,
               "identity": identity, "seen_seq": 0, "done": 0, "created_at": 0}
        row.update(overrides)
        self.tables.setdefault("memberships", []).append(row)
        return row

    def message(self, channel, seq, identity, text):
        row = {"id": "msg-%s-%d" % (channel, seq), "channel": channel,
               "seq": seq, "from_identity": identity, "text": text,
               "created_at": 0}
        self.tables.setdefault("messages", []).append(row)
        for chan in self.tables.get("channels", []):
            if chan["name"] == channel:
                chan["message_count"] = max(chan.get("message_count", 0), seq)
        return row

    def get_channel(self, name):
        for chan in self.tables.get("channels", []):
            if chan["name"] == name:
                return chan
        return None

    def get_membership(self, channel, identity):
        for row in self.tables.get("memberships", []):
            if row["channel"] == channel and row["identity"] == identity:
                return row
        return None


def write_settings(project, **events):
    """Write a .claude/settings.local.json with the given hook events."""
    d = os.path.join(project, ".claude")
    os.makedirs(d, exist_ok=True)
    hooks = {}
    for event, commands in events.items():
        hooks[event] = [{"hooks": [{"type": "command", "command": c}
                                   for c in commands]}]
    path = os.path.join(d, "settings.local.json")
    with open(path, "w") as f:
        json.dump({"hooks": hooks}, f)
    return path
