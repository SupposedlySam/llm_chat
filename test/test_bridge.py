"""Bridging rooms between machines through the hub.

Two machines are simulated in-process: each has its own fake llm_chat server
and its own project, and runs the real CLI code (`cli.run`) for every call the
bridge makes. The hub is the real hub, on a temporary SQLite file, reached
through its `handle` method rather than HTTP. So everything between "an agent
says something on Jonah's Mac" and "Morgan's agent is woken" is production
code; only the two network hops are short-circuited.
"""
import io
import json
import os
import random
import sys
import tempfile
import time
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import FakeServer, load  # noqa: E402

cli = load("llm_chat")
bridge_mod = load("llm-chat-bridge")
hub_mod = load(os.path.join("hub", "llm_chat_hub.py"), "llm_chat_hub")

ROOM = "wcs-desk"


class Machine:
    """One Mac: its own llm_chat server and its own bridge project."""

    def __init__(self, name, root):
        self.name = name
        self.fake = FakeServer()
        self.project = os.path.join(root, name)
        os.makedirs(self.project, exist_ok=True)

    def run(self, args, server=None, text=None, project=None):
        """The bridge's CLI seam, served by the real CLI against this
        machine's fake server. Without `project` it is an agent of this
        machine acting from its own repo."""
        if project is None:
            who = args[args.index("--as") + 1] if "--as" in args else "anon"
            project = os.path.join(self.project, "agents", who)
        os.makedirs(project, exist_ok=True)
        path = None
        if text is not None:
            path = os.path.join(project, ".relay")
            with open(path, "w") as f:
                f.write(text)
            args = list(args) + ["--file", path]
        saved = (sys.argv, cli.call, os.environ.get("CLAUDE_PROJECT_DIR"),
                 os.environ.pop("CLAUDE_CODE_SESSION_ID", None))
        sys.argv = ["llm_chat"] + list(args)
        cli.call = self.fake.call
        os.environ["CLAUDE_PROJECT_DIR"] = project
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.run()
        finally:
            sys.argv, cli.call = saved[0], saved[1]
            if saved[2] is None:
                os.environ.pop("CLAUDE_PROJECT_DIR", None)
            else:
                os.environ["CLAUDE_PROJECT_DIR"] = saved[2]
            if saved[3] is not None:
                os.environ["CLAUDE_CODE_SESSION_ID"] = saved[3]
        return code or 0, out.getvalue(), err.getvalue()

    def say(self, identity, text, *flags):
        code, out, err = self.run(["say", ROOM, text, "--as", identity]
                                  + list(flags))
        assert code == 0, err
        return out

    def messages(self):
        return sorted(self.fake.tables.get("messages", []),
                      key=lambda m: m["seq"])


class HubClient:
    """The bridge's hub seam, served by the real hub's `handle`."""

    def __init__(self, hub, namespace, fail=None):
        self.hub, self.namespace, self.fail = hub, namespace, fail

    def request(self, method, path, body=None):
        if self.fail and self.fail(method, path):
            return 500, {"error": "simulated outage"}
        url = urllib.parse.urlparse(path)
        try:
            return self.hub.handle(method, url.path,
                                   urllib.parse.parse_qs(url.query),
                                   {"X-Hub-Namespace": self.namespace}, body)
        except hub_mod.Refused as refused:
            return refused.status, {"error": str(refused)}


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store = hub_mod.Store(os.path.join(self.tmp.name, "hub.db"))
        self.addCleanup(store.db.close)
        self.hub = hub_mod.Hub(store, hub_mod.insecure_namespaces)
        real_host = cli.host_sessions
        cli.host_sessions = lambda: []
        self.addCleanup(lambda: setattr(cli, "host_sessions", real_host))
        self.jonah = self.machine("jonah", "wcs")
        self.morgan = self.machine("morgan", "owner")

    def machine(self, name, agent):
        m = Machine(name, self.tmp.name)
        m.fake.channel(ROOM, created_by=agent)
        m.fake.membership(ROOM, agent)
        m.bridge = bridge_mod.Bridge(
            {"hub": "dev", "namespace": name, "rooms": [ROOM],
             "reader": "hub-bridge", "poll_sec": 1},
            hub=HubClient(self.hub, name), cli=m.run,
            state_path=os.path.join(m.project, "bridge-state.json"),
            projects=os.path.join(m.project, "bridge"))
        return m

    def cycle(self, *machines):
        with redirect_stderr(io.StringIO()):
            for m in machines or (self.jonah, self.morgan):
                m.bridge.cycle()

    def said_on_hub(self):
        """The hub's messages, without the member announcements."""
        return [m for m in self.hub.store.since(ROOM, 0)
                if m["kind"] != "join"]

    def from_(self, machine, sender):
        return [m for m in machine.messages() if m["from_identity"] == sender]

    # ── the acceptance test wcs asked for ────────────────────────────────────
    def test_each_side_WAKES_the_other_and_nothing_ECHOES(self):
        self.cycle()                                   # readers join
        self.morgan.say("owner", "is the desk live?")
        self.cycle(self.morgan, self.jonah)
        [arrived] = self.from_(self.jonah, "morgan.owner")
        self.assertEqual(arrived["text"], "is the desk live?")
        chan = self.jonah.fake.get_channel(ROOM)
        self.assertTrue(cli.wakes(arrived, "wcs", chan),
                        "a plain message from Morgan's agent wakes Jonah's")

        self.jonah.say("wcs", "yes, claim posted", "--to", "morgan.owner")
        self.cycle(self.jonah, self.morgan)
        [back] = self.from_(self.morgan, "jonah.wcs")
        self.assertEqual(back["audience"], "owner",
                         "addressed on Jonah's Mac to morgan.owner, it must "
                         "arrive on Morgan's addressed to owner")
        self.assertTrue(cli.wakes(back, "owner",
                                  self.morgan.fake.get_channel(ROOM)))

        for machine in (self.jonah, self.morgan):
            self.assertEqual(
                [m["text"] for m in machine.messages()
                 if m["from_identity"].startswith(machine.name + ".")], [],
                "a machine's own message must never come back into its room "
                "under a proxy name")
        before = (len(self.jonah.messages()), len(self.morgan.messages()))
        for _ in range(4):
            self.cycle()
        self.assertEqual((len(self.jonah.messages()),
                          len(self.morgan.messages())), before,
                         "nothing may be relayed back where it came from")
        self.assertEqual(len(self.said_on_hub()), 2)

    def test_an_address_to_SOMEONE_ELSE_does_not_wake_the_local_agent(self):
        self.cycle()
        self.morgan.fake.membership(ROOM, "reviewer")
        self.morgan.say("owner", "for you", "--to", "reviewer")
        self.cycle(self.morgan, self.jonah)
        [arrived] = self.from_(self.jonah, "morgan.owner")
        self.assertEqual(arrived["audience"], "morgan.reviewer")
        self.assertFalse(cli.wakes(arrived, "wcs",
                                   self.jonah.fake.get_channel(ROOM)))

    def test_a_remote_agent_is_ADDRESSABLE_before_it_has_spoken(self):
        """The proxy used to appear only when one of its messages arrived, so
        the first contact could not be `--to morgan.owner`."""
        self.cycle(self.morgan, self.jonah)
        self.assertIsNotNone(
            self.jonah.fake.get_membership(ROOM, "morgan.owner"))
        self.assertEqual(self.from_(self.jonah, "morgan.owner"), [],
                         "an announcement posts nothing")
        self.jonah.say("wcs", "first contact", "--to", "morgan.owner")
        self.cycle(self.jonah, self.morgan)
        [arrived] = self.from_(self.morgan, "jonah.wcs")
        self.assertEqual(arrived["audience"], "owner")

    def test_the_backlog_before_the_bridge_is_NOT_shipped(self):
        self.jonah.say("wcs", "private note from before the bridge")
        self.cycle()
        self.assertEqual(self.said_on_hub(), [])

    # ── departures, topic, close ─────────────────────────────────────────────
    def test_a_TOPIC_change_is_replayed_as_a_topic_change(self):
        self.cycle()
        code, _, err = self.morgan.run(["topic", ROOM, "claims and offers",
                                        "--as", "owner"])
        self.assertEqual(code, 0, err)
        self.cycle(self.morgan, self.jonah)
        self.assertEqual(self.jonah.fake.get_channel(ROOM)["topic"],
                         "claims and offers")

    def test_a_LEAVE_is_replayed_as_a_leave(self):
        self.cycle()
        self.morgan.say("owner", "hello")
        self.cycle(self.morgan, self.jonah)
        self.morgan.fake.membership(ROOM, "other")     # so owner may leave
        self.morgan.fake.get_channel(ROOM)["created_by"] = "other"
        code, _, err = self.morgan.run(["leave", ROOM, "--as", "owner"])
        self.assertEqual(code, 0, err)
        self.cycle(self.morgan, self.jonah)
        self.assertEqual(
            self.jonah.fake.get_membership(ROOM, "morgan.owner")["done"], 1)

    def test_a_CLOSE_is_replayed_as_a_close(self):
        self.cycle()
        self.morgan.say("owner", "hello")
        self.cycle(self.morgan, self.jonah)
        code, _, err = self.morgan.run(["close", ROOM, "--as", "owner",
                                        "--reason", "desk retired"])
        self.assertEqual(code, 0, err)
        self.cycle(self.morgan, self.jonah)
        chan = self.jonah.fake.get_channel(ROOM)
        self.assertEqual(chan["closed"], 1)
        self.assertEqual(chan["closed_reason"], "desk retired")

    # ── failure handling ─────────────────────────────────────────────────────
    def test_the_up_mark_never_passes_a_message_the_hub_REFUSED(self):
        self.cycle()
        self.jonah.bridge.hub.fail = lambda method, path: method == "POST"
        self.jonah.say("wcs", "first")
        self.cycle(self.jonah)
        self.assertEqual(self.said_on_hub(), [])
        self.jonah.bridge.hub.fail = None
        self.cycle(self.jonah)
        self.assertEqual([m["text"] for m in self.said_on_hub()],
                         ["first"])

    def test_a_down_relay_that_FAILS_is_retried_not_skipped(self):
        self.cycle()
        self.morgan.say("owner", "one")
        self.cycle(self.morgan)
        self.jonah.fake.channel("unrelated")       # noise: harmless
        real = self.jonah.bridge.deliver
        self.jonah.bridge.deliver = lambda room, message: False
        self.cycle(self.jonah)
        self.assertEqual(self.from_(self.jonah, "morgan.owner"), [])
        self.jonah.bridge.deliver = real
        self.cycle(self.jonah)
        self.assertEqual(len(self.from_(self.jonah, "morgan.owner")), 1)

    def test_an_addressee_who_LEFT_does_not_wedge_the_room(self):
        self.cycle()
        self.jonah.say("wcs", "hello")
        self.cycle(self.jonah, self.morgan)
        self.jonah.fake.membership(ROOM, "other")
        self.jonah.fake.get_channel(ROOM)["created_by"] = "other"
        self.jonah.run(["leave", ROOM, "--as", "wcs"])
        self.cycle(self.jonah)                     # the departure goes up
        self.morgan.say("owner", "still there?", "--to", "jonah.wcs")
        self.morgan.say("owner", "next")
        self.cycle(self.morgan, self.jonah)
        texts = [m["text"] for m in self.from_(self.jonah, "morgan.owner")]
        self.assertEqual(texts[-2:], ["still there?", "next"])

    def test_a_BROADCAST_room_is_refused(self):
        """Checked every cycle, not only at start: a room switched to
        broadcast after it was bridged must stop leaving the machine. The
        message comes AFTER the reader joined, so only the refusal can be
        what keeps it off the hub."""
        self.cycle(self.jonah)
        self.jonah.fake.get_channel(ROOM)["broadcast"] = 1
        self.jonah.say("wcs", "announcement")
        self.cycle(self.jonah)
        self.assertEqual(self.said_on_hub(), [])

    # ── liveness ─────────────────────────────────────────────────────────────
    def test_a_REMOTE_agent_reads_as_live_while_its_bridge_publishes(self):
        self.cycle()
        self.morgan.say("owner", "hi")
        self.cycle(self.morgan, self.jonah)
        cli.host_sessions = lambda: [{"pid": 1, "cwd": self.morgan.project,
                                      "sessionId": "sid-owner"}]
        d = os.path.join(self.morgan.project, ".llm_chat", "sessions",
                         "sid-owner")
        os.makedirs(d)
        with open(os.path.join(d, "joined.json"), "w") as f:
            json.dump({ROOM: {"identity": "owner"}}, f)
        with redirect_stderr(io.StringIO()):
            self.morgan.bridge.publish_presence()
        store = os.path.join(self.tmp.name, "store")
        saved = os.environ.get("LLM_CHAT_STORE")
        os.environ["LLM_CHAT_STORE"] = store
        self.addCleanup(lambda: os.environ.__setitem__("LLM_CHAT_STORE", saved)
                        if saved is not None
                        else os.environ.pop("LLM_CHAT_STORE", None))
        path = cli.remote_presence_path()
        self.jonah.bridge.fetch_presence(path)
        cli.host_sessions = lambda: []
        live = cli.live_identities()
        here, away, unknown = cli.liveness_split(["morgan.owner", "wcs"], live)
        self.assertEqual(here, ["morgan.owner"])
        self.assertIn("hub-bridge", live, "the running bridge is live too")

        with open(path) as f:
            record = json.load(f)
        record["updated"] -= cli.REMOTE_FRESH_SEC + 1
        with open(path, "w") as f:
            json.dump(record, f)
        self.assertNotIn("morgan.owner", cli.live_identities(),
                         "a stopped bridge claims nothing")

    def test_a_bridge_never_reports_its_OWN_machine_as_remote(self):
        self.hub.store.set_presence("jonah", ["jonah.wcs"])
        self.hub.store.set_presence("morgan", ["morgan.owner"])
        path = os.path.join(self.tmp.name, "remote.json")
        self.jonah.bridge.fetch_presence(path)
        with open(path) as f:
            self.assertEqual(sorted(json.load(f)["live"]), ["morgan.owner"])

    def test_the_two_copies_of_the_presence_path_agree(self):
        env = {"LLM_CHAT_STORE": "/x/store"}
        saved = os.environ.get("LLM_CHAT_STORE")
        os.environ["LLM_CHAT_STORE"] = "/x/store"
        try:
            self.assertEqual(
                bridge_mod.presence_path("http://localhost:7717", env),
                cli.remote_presence_path("http://localhost:7717"))
        finally:
            if saved is None:
                os.environ.pop("LLM_CHAT_STORE")
            else:
                os.environ["LLM_CHAT_STORE"] = saved


class AudienceTest(unittest.TestCase):
    def test_up_namespaces_local_names_and_keeps_proxies(self):
        self.assertEqual(bridge_mod.audience_up("wcs,morgan.owner", "jonah",
                                                ["morgan.owner"]),
                         "jonah.wcs,morgan.owner")
        for special in (None, "*", "-"):
            self.assertEqual(bridge_mod.audience_up(special, "jonah", []),
                             special)

    def test_the_audience_becomes_the_same_flags_on_the_far_side(self):
        self.assertEqual(bridge_mod.audience_flags(None), [])
        self.assertEqual(bridge_mod.audience_flags("*"), ["--to-all"])
        self.assertEqual(bridge_mod.audience_flags("-"), ["--to-none"])
        self.assertEqual(bridge_mod.audience_flags("a,b"), ["--to", "a,b"])

    def test_down_strips_this_machine_and_keeps_the_others(self):
        self.assertEqual(bridge_mod.audience_down("jonah.wcs,ann.x", "jonah"),
                         ("wcs,ann.x", ["ann.x"]))


class HubTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = hub_mod.Store(os.path.join(tmp.name, "h.db"))
        self.addCleanup(store.db.close)
        self.hub = hub_mod.Hub(store, hub_mod.insecure_namespaces)

    def post(self, ns, body):
        return self.hub.handle("POST", "/v1/rooms/r/messages", {},
                               {"X-Hub-Namespace": ns}, body)

    def test_a_machine_may_speak_ONLY_for_its_own_namespace(self):
        with self.assertRaises(hub_mod.Refused) as refused:
            self.post("jonah", {"from": "morgan.owner", "text": "hi",
                                "origin_seq": 1})
        self.assertEqual(refused.exception.status, 403)
        with self.assertRaises(hub_mod.Refused):
            self.hub.handle("PUT", "/v1/presence", {},
                            {"X-Hub-Namespace": "jonah"},
                            {"live": ["morgan.owner"]})

    def test_a_RETRY_is_not_a_second_message(self):
        body = {"from": "jonah.wcs", "text": "hi", "origin_seq": 7}
        first = self.post("jonah", body)
        again = self.post("jonah", body)
        self.assertEqual(first[1]["seq"], again[1]["seq"])
        self.assertEqual(again[0], 200)
        self.assertEqual(len(self.hub.store.since("r", 0)), 1)

    def test_seq_is_per_room_and_gap_free(self):
        for i in range(3):
            self.post("jonah", {"from": "jonah.a", "text": str(i),
                                "origin_seq": i})
        self.assertEqual([m["seq"] for m in self.hub.store.since("r", 0)],
                         [1, 2, 3])
        self.assertEqual([m["seq"] for m in self.hub.store.since("r", 2)],
                         [3])

    def test_no_namespace_header_is_refused_in_dev_mode(self):
        with self.assertRaises(hub_mod.Refused) as refused:
            self.hub.handle("GET", "/v1/whoami", {}, {}, None)
        self.assertEqual(refused.exception.status, 401)

    def test_a_ROOM_LIST_limits_what_exists(self):
        self.hub.rooms = {"wcs-desk"}
        with self.assertRaises(hub_mod.Refused) as refused:
            self.hub.handle("GET", "/v1/rooms/other/messages", {},
                            {"X-Hub-Namespace": "jonah"}, None)
        self.assertEqual(refused.exception.status, 404)

    def test_dev_insecure_will_not_bind_beyond_loopback(self):
        saved = os.environ.get("HUB_AUTH")
        os.environ["HUB_AUTH"] = "dev-insecure"
        try:
            with self.assertRaises(SystemExit):
                hub_mod.main(["--host", "0.0.0.0"])
        finally:
            if saved is None:
                os.environ.pop("HUB_AUTH")
            else:
                os.environ["HUB_AUTH"] = saved


class OverHttpTest(unittest.TestCase):
    """The same hub, over real HTTP on a loopback port, reached by the
    bridge's real client. The smoke test did this by hand; this keeps it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        store = hub_mod.Store(os.path.join(tmp.name, "h.db"))
        self.addCleanup(store.db.close)
        hub = hub_mod.Hub(store, hub_mod.insecure_namespaces)
        # NOT the threading server production uses: that hands each request
        # to a new thread, where the coverage tracer cannot follow it.
        from http.server import HTTPServer
        self.server = HTTPServer(("127.0.0.1", 0), hub_mod.make_handler(hub))
        self.server.timeout = 0.05
        self.addCleanup(self.server.server_close)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.config = {"hub": self.url, "namespace": "jonah",
                       "rooms": [ROOM], "dev_namespace_header": True}

    def over_http(self, fn):
        """Run the CLIENT in a thread and serve on THIS one, so the handler
        runs where the coverage tracer can see it. Returns fn's result."""
        import threading
        result = {}
        worker = threading.Thread(target=lambda: result.update(v=fn()))
        with redirect_stderr(io.StringIO()):
            worker.start()
            while worker.is_alive():
                self.server.handle_request()
        return result.get("v")

    def served_in_background(self, fn):
        """The mirror image: the SERVER in a thread and the client here, so
        the client's code is the side the tracer sees."""
        import threading
        stop = threading.Event()

        def serve():
            while not stop.is_set():
                self.server.handle_request()
        worker = threading.Thread(target=serve)
        with redirect_stderr(io.StringIO()):
            worker.start()
            try:
                return fn()
            finally:
                stop.set()
                worker.join()

    def client(self, **over):
        return bridge_mod.Hub(dict(self.config, **over))

    def test_append_read_and_presence_round_trip(self):
        hub = self.client()
        status, body = self.over_http(lambda: hub.request(
            "POST", "/v1/rooms/%s/messages" % ROOM,
            {"from": "jonah.wcs", "text": "hi", "origin_seq": 1}))
        self.assertEqual((status, body["seq"]), (201, 1))
        status, body = self.over_http(lambda: hub.request(
            "GET", "/v1/rooms/%s/messages?since=0" % ROOM))
        self.assertEqual([m["text"] for m in body["messages"]], ["hi"])
        self.assertEqual(self.over_http(lambda: hub.request(
            "PUT", "/v1/presence", {"live": ["jonah.wcs"]}))[0], 200)

    def test_a_refusal_arrives_as_its_status_and_reason(self):
        status, body = self.served_in_background(lambda: self.client().request(
            "POST", "/v1/rooms/%s/messages" % ROOM,
            {"from": "morgan.owner", "text": "forged", "origin_seq": 1}))
        self.assertEqual(status, 403)
        self.assertIn("may only speak for jonah", body["error"])

    def test_check_says_who_this_machine_is_and_catches_a_mismatch(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.over_http(
                lambda: bridge_mod.check(self.config)), 0)
            self.assertEqual(self.over_http(lambda: bridge_mod.check(
                dict(self.config, namespace="morgan",
                     dev_namespace_header=False))), 1)
            self.assertEqual(bridge_mod.check(None), 1)
        self.assertIn("this machine is 'jonah'", out.getvalue())

    def test_main_runs_check_from_a_config_path(self):
        path = os.path.join(self.dir, "bridge.json")
        with open(path, "w") as f:
            json.dump(self.config, f)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.over_http(lambda: bridge_mod.main(
                ["--check", "--config", path])), 0)
            self.assertEqual(bridge_mod.main(["--help"]), 0)

    def test_the_bridge_NAMES_itself_rather_than_sending_pythons_agent(self):
        """Cloudflare's bot protection answers `Python-urllib/x` with a 403
        before Access even looks at the token — measured on the first real
        deploy, where curl with the same token got a 200."""
        agent = self.client().headers.get("User-Agent", "")
        self.assertTrue(agent.startswith("llm-chat-bridge/"), agent)

    def test_an_UNREACHABLE_hub_is_a_status_not_a_crash(self):
        self.server.server_close()
        status, body = self.client().request("GET", "/v1/whoami")
        self.assertEqual(status, 0)
        self.assertIn("unreachable", body["error"])

    def test_a_200_that_is_NOT_JSON_is_a_refusal(self):
        """Cloudflare Access answers a token it did not accept with its HTML
        login page, as a 200. `--check` crashed on it."""
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class LoginPage(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"<html>Sign in</html>")

            def log_message(self, *a):
                pass

        page = HTTPServer(("127.0.0.1", 0), LoginPage)
        self.addCleanup(page.server_close)
        import threading
        threading.Thread(target=page.handle_request, daemon=True).start()
        status, body = bridge_mod.Hub(dict(
            self.config, hub="http://127.0.0.1:%d" % page.server_address[1])
        ).request("GET", "/v1/whoami")
        self.assertEqual(status, 401)
        self.assertIn("login page", body["error"])

    def test_an_access_credential_is_sent_and_a_missing_secret_refused(self):
        hub = bridge_mod.Hub(dict(self.config, access={
            "client_id": "abc.access",
            "client_secret_command": ["echo", "s3cret"]}))
        self.assertEqual(hub.headers["CF-Access-Client-Secret"], "s3cret")
        with self.assertRaises(SystemExit):
            bridge_mod.Hub(dict(self.config, access={"client_id": "abc"}))


class ConfigTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "bridge.json")

    def write(self, text):
        with open(self.path, "w") as f:
            f.write(text)

    def test_absent_is_None_and_defaults_fill_in(self):
        self.assertIsNone(bridge_mod.load_config(self.path))
        self.write('{"hub": "h", "namespace": "n", "rooms": ["r"]}')
        config = bridge_mod.load_config(self.path)
        self.assertEqual((config["reader"], config["poll_sec"]),
                         ("hub-bridge", 3))

    def test_missing_keys_and_bad_json_are_refused_by_name(self):
        self.write('{"hub": "h"}')
        with self.assertRaises(SystemExit) as stop:
            bridge_mod.load_config(self.path)
        self.assertIn("namespace, rooms", str(stop.exception))
        self.write("{not json")
        with self.assertRaises(SystemExit):
            bridge_mod.load_config(self.path)

    def test_the_cli_runs_as_a_subprocess_with_no_session(self):
        project = os.path.join(os.path.dirname(self.path), "p")
        code, out, _ = bridge_mod.run_cli(["fingerprint"], project=project)
        self.assertEqual(code, 0)
        self.assertTrue(out.strip())


class HubEnvTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = os.path.join(tmp.name, "h.db")

    def build(self, **env):
        hub = hub_mod.build_from_env(dict(env, HUB_DB=self.db))
        self.addCleanup(hub.store.db.close)
        return hub

    def test_each_mode_builds_and_a_bad_config_is_refused(self):
        self.assertEqual(self.build(HUB_AUTH="dev-insecure",
                                    HUB_ROOMS="a,b").rooms, {"a", "b"})
        self.build(HUB_AUTH="access", CF_TEAM_DOMAIN="t", CF_ACCESS_AUD="x",
                   HUB_NAMESPACES='{"id.access": "jonah"}')
        for env in ({"HUB_AUTH": "access", "HUB_NAMESPACES": "{}"},
                    {"HUB_AUTH": "access",
                     "HUB_NAMESPACES": '{"id": "bad.ns"}'},
                    {"HUB_AUTH": "none"}):
            with self.subTest(env=env):
                with self.assertRaises(SystemExit):
                    self.build(**env)

    def test_the_certs_fetch_reads_json(self):
        path = os.path.join(os.path.dirname(self.db), "certs.json")
        with open(path, "w") as f:
            json.dump({"keys": []}, f)
        self.assertEqual(hub_mod.AccessVerifier._fetch("file://" + path),
                         {"keys": []})


# ── Cloudflare Access tokens, signed here with a throwaway RSA key ───────────
def _probable_prime(bits, rng):
    while True:
        n = rng.getrandbits(bits) | (1 << bits - 1) | 1
        if all(n % p for p in (3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37)) and \
                all(pow(rng.randrange(2, n - 1), n - 1, n) == 1
                    for _ in range(20)):
            return n


def _keypair(bits=1024, seed=7):
    rng = random.Random(seed)
    e = 65537
    while True:
        p, q = _probable_prime(bits // 2, rng), _probable_prime(bits // 2, rng)
        phi = (p - 1) * (q - 1)
        if p != q and phi % e:
            return p * q, e, pow(e, -1, phi)


KEY = _keypair()


def _b64(data):
    import base64
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def token(claims, alg="RS256", kid="k1", key=KEY):
    import hashlib
    n, e, d = key
    head = _b64(json.dumps({"alg": alg, "kid": kid}).encode())
    body = _b64(json.dumps(claims).encode())
    k = (n.bit_length() + 7) // 8
    digest = hub_mod.SHA256_PREFIX + hashlib.sha256(
        ("%s.%s" % (head, body)).encode()).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(digest) - 3) + b"\x00" + digest
    sig = pow(int.from_bytes(em, "big"), d, n).to_bytes(k, "big")
    return "%s.%s.%s" % (head, body, _b64(sig))


class AccessTokenTest(unittest.TestCase):
    TEAM = "team.cloudflareaccess.com"

    def setUp(self):
        n, e, _ = KEY
        certs = {"keys": [{"kid": "k1", "kty": "RSA",
                           "n": _b64(n.to_bytes((n.bit_length() + 7) // 8,
                                                "big")),
                           "e": _b64(e.to_bytes(3, "big"))}]}
        self.verifier = hub_mod.AccessVerifier(self.TEAM, "AUD1",
                                               fetch=lambda url: certs)
        self.namespace_for = hub_mod.access_namespaces(
            self.verifier, {"abc.access": "jonah", "other.access": "morgan"})

    def claims(self, **over):
        c = {"aud": ["AUD1"], "iss": "https://" + self.TEAM,
             "exp": time.time() + 60, "common_name": "abc.access"}
        c.update(over)
        return c

    def ns(self, tok):
        return self.namespace_for({"Cf-Access-Jwt-Assertion": tok})

    def test_a_valid_service_token_maps_to_its_namespace(self):
        self.assertEqual(self.ns(token(self.claims())), "jonah")

    def test_a_body_swapped_onto_a_real_signature_is_refused(self):
        """Jonah's valid token, with the body edited to claim Morgan's client
        id — a MAPPED one, so only the signature check can refuse it."""
        tampered = token(self.claims()).split(".")
        tampered[1] = _b64(json.dumps(self.claims(
            common_name="other.access")).encode())
        with self.assertRaises(hub_mod.Refused) as refused:
            self.ns(".".join(tampered))
        self.assertIn("signature", str(refused.exception))

    def test_every_way_to_be_wrong_is_refused(self):
        tampered = token(self.claims()).split(".")
        tampered[1] = _b64(json.dumps(self.claims(
            common_name="other.access")).encode())
        cases = {
            "tampered body": ".".join(tampered),
            "other app": token(self.claims(aud=["AUD2"])),
            "other team": token(self.claims(iss="https://evil.example")),
            "expired": token(self.claims(exp=time.time() - 1)),
            "alg none": token(self.claims(), alg="none"),
            "unknown key": token(self.claims(), kid="k2"),
            "no token": None,
            "junk": "not.a.jwt",
        }
        for name, tok in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(hub_mod.Refused):
                    self.namespace_for({} if tok is None else
                                       {"Cf-Access-Jwt-Assertion": tok})

    def test_a_valid_token_with_NO_namespace_names_its_client_id(self):
        with self.assertRaises(hub_mod.Refused) as refused:
            self.ns(token(self.claims(common_name="new.access")))
        self.assertEqual(refused.exception.status, 403)
        self.assertIn("new.access", str(refused.exception))


if __name__ == "__main__":
    unittest.main()
