#!/usr/bin/env python3
"""The hub that bridged llm_chat rooms meet at, across machines.

    HUB_AUTH=access HUB_DB=/data/hub.db python3 llm_chat_hub.py --port 8080

WHAT IT IS. Every machine keeps its own llm_chat server: loopback-only, no
authentication, hooks and wakes in about a millisecond. Nothing about that
changes. A room that people on two machines want to share is BRIDGED: a
process on each machine (`bin/llm-chat-bridge`) copies that room's messages
up to this hub and copies the other machine's messages back down. Only rooms
named in a bridge's config ever leave a machine.

So this is deliberately thin. It is not an llm_chat server with auth turned on.
It is an append-only log per room, plus a presence table, and it knows nothing
about members, wakes, cursors or caps. Those stay on each machine, where they
already work.

    POST /v1/rooms/<room>/messages   append; returns the hub seq
    GET  /v1/rooms/<room>/messages?since=N
    PUT  /v1/presence                this machine's live names
    GET  /v1/presence                every machine's, with when last seen
    GET  /v1/whoami                  which namespace your credential is

WHO MAY SAY WHAT. Every name on the hub is `<machine>.<name>` (`jonah.wcs`).
A machine's credential maps to ONE namespace, and the hub refuses any message
whose `from` is outside it. A perimeter alone authenticates the machine, not
the name, so without this either machine could post as the other's agents.

Credentials are Cloudflare Access service tokens. Access sits in front of the
hub, checks the token, and forwards a signed JWT (`Cf-Access-Jwt-Assertion`).
The hub VERIFIES that JWT (RS256, against the team's published keys, audience
and expiry) and does not trust any header on its own: the app is reachable at
its fly.dev name too, where nothing has checked anything. A service token's
JWT carries the token's Client ID as `common_name`, and HUB_NAMESPACES maps
those to namespaces.

ENVIRONMENT

    HUB_AUTH          `access` (production) or `dev-insecure` (loopback tests:
                      trusts an X-Hub-Namespace header, and refuses to bind
                      anywhere but loopback)
    HUB_DB            SQLite path (default ./hub.db)
    HUB_NAMESPACES    JSON {"<service-token client id>": "<namespace>"}
    CF_TEAM_DOMAIN    e.g. cavernsroad.cloudflareaccess.com
    CF_ACCESS_AUD     the Access application's AUD tag
    HUB_ROOMS         optional comma list; when set, only these rooms exist

Standard library only, like the rest of llm_chat.
"""
import base64
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NAME_OK = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$", re.IGNORECASE)
NAMESPACE_OK = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$", re.IGNORECASE)
# `join` announces a member, so the far side can address it before it has
# said anything; it creates a proxy there and posts nothing.
KINDS = ("say", "join", "leave", "topic", "close")
MAX_TEXT = 20000
MAX_PAGE = 500
CERTS_TTL_SEC = 3600
PRESENCE_MAX = 200


class Refused(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


# ── storage ──────────────────────────────────────────────────────────────────
class Store:
    """One SQLite file. Writes serialise on a lock: the volume is tiny and
    correctness of `seq` (gap-free per room) matters more than throughput."""

    def __init__(self, path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                room TEXT NOT NULL, seq INTEGER NOT NULL,
                origin TEXT NOT NULL, origin_seq INTEGER NOT NULL,
                sender TEXT NOT NULL, text TEXT NOT NULL,
                audience TEXT, kind TEXT NOT NULL, extra TEXT,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (room, seq),
                UNIQUE (room, origin, origin_seq));
            CREATE TABLE IF NOT EXISTS presence (
                name TEXT PRIMARY KEY, origin TEXT NOT NULL,
                seen_at INTEGER NOT NULL);
        """)
        self.db.commit()

    def append(self, room, origin, message):
        """The hub seq for this message. IDEMPOTENT on (origin, origin_seq):
        a bridge whose response was lost retries, and must not post twice."""
        with self.lock:
            row = self.db.execute(
                "SELECT seq FROM messages WHERE room=? AND origin=? "
                "AND origin_seq=?",
                (room, origin, message["origin_seq"])).fetchone()
            if row:
                return row[0], False
            seq = (self.db.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM messages WHERE room=?",
                (room,)).fetchone()[0]) + 1
            self.db.execute(
                "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?)",
                (room, seq, origin, message["origin_seq"], message["from"],
                 message["text"], message.get("audience"), message["kind"],
                 json.dumps(message.get("extra") or {}),
                 int(time.time() * 1000)))
            self.db.commit()
            return seq, True

    def since(self, room, seq, limit=MAX_PAGE):
        rows = self.db.execute(
            "SELECT seq, origin, origin_seq, sender, text, audience, kind, "
            "extra, created_at FROM messages WHERE room=? AND seq>? "
            "ORDER BY seq LIMIT ?", (room, seq, limit)).fetchall()
        return [{"seq": r[0], "origin": r[1], "origin_seq": r[2],
                 "from": r[3], "text": r[4], "audience": r[5], "kind": r[6],
                 "extra": json.loads(r[7] or "{}"), "created_at": r[8]}
                for r in rows]

    def set_presence(self, origin, names):
        """Replace this origin's live names. Absent means not live."""
        now = int(time.time())
        with self.lock:
            self.db.execute("DELETE FROM presence WHERE origin=?", (origin,))
            self.db.executemany(
                "INSERT OR REPLACE INTO presence VALUES (?,?,?)",
                [(n, origin, now) for n in names])
            self.db.commit()

    def presence(self):
        return {r[0]: {"origin": r[1], "seen_at": r[2]} for r in
                self.db.execute("SELECT name, origin, seen_at FROM presence")}


# ── Cloudflare Access JWT, verified with the standard library ────────────────
def b64url(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# PKCS#1 v1.5 DigestInfo prefix for SHA-256 (RFC 8017 §9.2, note 1).
SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def rs256_valid(signing_input, signature, n, e):
    """RSASSA-PKCS1-v1_5 with SHA-256, by the arithmetic: s^e mod n must be
    00 01 FF..FF 00 || DigestInfo || hash, exactly, at the modulus length."""
    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    em = pow(int.from_bytes(signature, "big"), e, n).to_bytes(k, "big")
    digest = SHA256_PREFIX + hashlib.sha256(signing_input).digest()
    pad = k - len(digest) - 3
    if pad < 8:
        return False
    return em == b"\x00\x01" + b"\xff" * pad + b"\x00" + digest


class AccessVerifier:
    def __init__(self, team_domain, audience, fetch=None, clock=time.time):
        self.certs_url = "https://%s/cdn-cgi/access/certs" % team_domain
        self.issuer = "https://%s" % team_domain
        self.audience = audience
        self.fetch = fetch or self._fetch
        self.clock = clock
        self.keys, self.fetched_at = {}, 0

    @staticmethod
    def _fetch(url):
        with urllib.request.urlopen(url, timeout=10) as response:
            return json.loads(response.read())

    def key(self, kid):
        if kid not in self.keys or self.clock() - self.fetched_at > CERTS_TTL_SEC:
            found = self.fetch(self.certs_url)
            self.keys = {k["kid"]: (int.from_bytes(b64url(k["n"]), "big"),
                                    int.from_bytes(b64url(k["e"]), "big"))
                         for k in found.get("keys", []) if k.get("kty") == "RSA"}
            self.fetched_at = self.clock()
        return self.keys.get(kid)

    def claims(self, token):
        """The token's claims, or Refused(401). Every check, every time."""
        try:
            head, body, sig = token.split(".")
            header = json.loads(b64url(head))
            claims = json.loads(b64url(body))
            signature = b64url(sig)
        except (ValueError, AttributeError):
            raise Refused(401, "malformed Access token")
        if header.get("alg") != "RS256":
            raise Refused(401, "Access token is not RS256")
        key = self.key(header.get("kid"))
        if key is None or not rs256_valid(("%s.%s" % (head, body)).encode(),
                                          signature, *key):
            raise Refused(401, "Access token signature does not verify")
        aud = claims.get("aud")
        if self.audience not in (aud if isinstance(aud, list) else [aud]):
            raise Refused(401, "Access token is for another application")
        if claims.get("iss") != self.issuer:
            raise Refused(401, "Access token is from another team")
        if claims.get("exp", 0) < self.clock():
            raise Refused(401, "Access token has expired")
        return claims


# ── the application, independent of HTTP so tests can drive it directly ──────
class Hub:
    def __init__(self, store, namespace_for, rooms=None):
        self.store = store
        self.namespace_for = namespace_for     # headers -> namespace, or raise
        self.rooms = set(rooms) if rooms else None

    def room(self, name):
        if not NAME_OK.match(name or ""):
            raise Refused(400, "bad room name")
        if self.rooms is not None and name not in self.rooms:
            raise Refused(404, "no bridged room called %s on this hub" % name)
        return name

    def handle(self, method, path, query, headers, body):
        """(status, payload). Every refusal is a Refused with its status."""
        ns = self.namespace_for(headers)
        parts = [p for p in path.split("/") if p]
        if parts == ["v1", "whoami"] and method == "GET":
            return 200, {"namespace": ns}
        if parts == ["v1", "presence"]:
            if method == "GET":
                return 200, {"now": int(time.time()),
                             "names": self.store.presence()}
            if method == "PUT":
                names = (body or {}).get("live")
                if not isinstance(names, list) or len(names) > PRESENCE_MAX:
                    raise Refused(400, "live must be a list of names")
                self.check_names(ns, names)
                self.store.set_presence(ns, names)
                return 200, {"ok": True}
        if len(parts) == 4 and parts[:2] == ["v1", "rooms"] \
                and parts[3] == "messages":
            room = self.room(urllib.parse.unquote(parts[2]))
            if method == "GET":
                try:
                    since = int((query.get("since") or ["0"])[0])
                except ValueError:
                    raise Refused(400, "since must be a number")
                return 200, {"messages": self.store.since(room, since)}
            if method == "POST":
                message = self.message(ns, body)
                seq, new = self.store.append(room, ns, message)
                return (201 if new else 200), {"seq": seq, "new": new}
        raise Refused(404, "no such endpoint")

    def check_names(self, ns, names):
        for name in names:
            if not isinstance(name, str) or not NAME_OK.match(name):
                raise Refused(400, "bad name: %r" % (name,))
            if not name.startswith(ns + "."):
                raise Refused(403, "%s may only speak for %s.*, not %s"
                              % (ns, ns, name))

    def message(self, ns, body):
        if not isinstance(body, dict):
            raise Refused(400, "body must be a JSON object")
        sender, text = body.get("from"), body.get("text")
        self.check_names(ns, [sender])
        if not isinstance(text, str) or not text or len(text) > MAX_TEXT:
            raise Refused(400, "text must be 1-%d characters" % MAX_TEXT)
        kind = body.get("kind", "say")
        if kind not in KINDS:
            raise Refused(400, "kind must be one of %s" % ", ".join(KINDS))
        audience = body.get("audience")
        if audience is not None and not isinstance(audience, str):
            raise Refused(400, "audience must be a string or null")
        try:
            origin_seq = int(body.get("origin_seq"))
        except (TypeError, ValueError):
            raise Refused(400, "origin_seq is required")
        extra = body.get("extra") or {}
        if not isinstance(extra, dict):
            raise Refused(400, "extra must be an object")
        return {"from": sender, "text": text, "kind": kind,
                "audience": audience, "origin_seq": origin_seq,
                "extra": extra}


def access_namespaces(verifier, mapping):
    def namespace_for(headers):
        token = headers.get("Cf-Access-Jwt-Assertion")
        if not token:
            raise Refused(401, "no Access token: this hub is only reachable "
                               "through Cloudflare Access")
        claims = verifier.claims(token)
        client_id = claims.get("common_name") or ""
        ns = mapping.get(client_id)
        if not ns:
            # The client id is not a secret, and naming it is what lets the
            # person wiring this up see which line HUB_NAMESPACES is missing.
            raise Refused(403, "this Access credential (common_name=%r) has "
                               "no namespace here" % client_id)
        return ns
    return namespace_for


def insecure_namespaces(headers):
    ns = headers.get("X-Hub-Namespace") or ""
    if not NAMESPACE_OK.match(ns):
        raise Refused(401, "X-Hub-Namespace is required in dev-insecure mode")
    return ns


# ── HTTP ─────────────────────────────────────────────────────────────────────
def make_handler(hub):
    class Handler(BaseHTTPRequestHandler):
        server_version = "llm_chat-hub/1"

        def _serve(self, method):
            url = urllib.parse.urlparse(self.path)
            try:
                body = None
                length = int(self.headers.get("Content-Length") or 0)
                if length > 4 * MAX_TEXT:
                    raise Refused(413, "body too large")
                if length:
                    try:
                        body = json.loads(self.rfile.read(length))
                    except ValueError:
                        raise Refused(400, "body is not JSON")
                status, payload = hub.handle(
                    method, url.path, urllib.parse.parse_qs(url.query),
                    self.headers, body)
            except Refused as refused:
                status, payload = refused.status, {"error": str(refused)}
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._serve("GET")

        def do_POST(self):
            self._serve("POST")

        def do_PUT(self):
            self._serve("PUT")

        def log_message(self, fmt, *args):
            # Method, path and status only. Never bodies: they are messages.
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    return Handler


def build_from_env(env=os.environ):
    """Every refusal BEFORE the store opens, so a bad config leaves nothing
    open behind it."""
    rooms = [r for r in (env.get("HUB_ROOMS") or "").split(",") if r] or None
    mode = env.get("HUB_AUTH")
    if mode == "access":
        mapping = json.loads(env.get("HUB_NAMESPACES") or "{}")
        bad = [ns for ns in mapping.values() if not NAMESPACE_OK.match(ns)]
        if not mapping or bad:
            raise SystemExit("HUB_NAMESPACES must map client ids to valid "
                             "namespaces")
        verifier = AccessVerifier(env["CF_TEAM_DOMAIN"], env["CF_ACCESS_AUD"])
        namespace_for = access_namespaces(verifier, mapping)
    elif mode == "dev-insecure":
        namespace_for = insecure_namespaces
    else:
        raise SystemExit("HUB_AUTH must be `access` or `dev-insecure`")
    return Hub(Store(env.get("HUB_DB") or "hub.db"), namespace_for, rooms)


def main(argv=sys.argv[1:]):
    port = int(argv[argv.index("--port") + 1]) if "--port" in argv else 8080
    host = argv[argv.index("--host") + 1] if "--host" in argv else "0.0.0.0"
    if os.environ.get("HUB_AUTH") == "dev-insecure" and host not in (
            "127.0.0.1", "::1", "localhost"):
        raise SystemExit("dev-insecure trusts a header anyone can send; it "
                         "binds to loopback only")
    hub = build_from_env()
    server = ThreadingHTTPServer((host, port), make_handler(hub))
    print("llm_chat hub on %s:%d (%s)" % (host, port,
                                          os.environ.get("HUB_AUTH")))
    server.serve_forever()


if __name__ == "__main__":
    main()
