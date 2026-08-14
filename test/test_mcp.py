"""The MCP server: JSON-RPC framing over stdio, and the tool table it exposes.

Shells out to the CLI through one seam, `run_cli`, exactly as bin/llm_chat
shells out to zonai through one seam, `call` — so a test replaces that single
function instead of the shared `subprocess` module, and never risks leaking a
stub into a test file that runs after it.
"""
import argparse
import io
import json
import os
import subprocess as real_subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from support import load  # noqa: E402


class RunCli:
    """Stands in for run_cli(argv, timeout) -> (returncode, text)."""

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []

    def __call__(self, argv, timeout):
        self.calls.append((argv, timeout))
        if self.outputs:
            return self.outputs.pop(0)
        return 0, ""


class McpTestCase(unittest.TestCase):
    def setUp(self):
        self.mod = load("llm-chat-mcp")

    def dispatch(self, message):
        return self.mod.dispatch(message)

    def call(self, name, arguments):
        """Dispatch a tools/call and return the argv the (faked) CLI saw."""
        fake = RunCli((0, "ok"))
        self.mod.run_cli = fake
        self.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}})
        return fake.calls[0][0]

    def run_main(self, *messages):
        payload = "".join(json.dumps(m) + "\n" for m in messages)
        out = io.StringIO()
        stdin = sys.stdin
        sys.stdin = io.StringIO(payload)
        try:
            with redirect_stdout(out):
                code = self.mod.main()
        finally:
            sys.stdin = stdin
        lines = [ln for ln in out.getvalue().splitlines() if ln.strip()]
        return code, [json.loads(ln) for ln in lines]


class InitializeTest(McpTestCase):
    def test_echoes_the_requested_protocol_version(self):
        resp = self.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": "2025-03-26"}})
        self.assertEqual(resp["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(resp["result"]["serverInfo"]["name"], "llm_chat")
        self.assertEqual(resp["result"]["capabilities"], {"tools": {}})

    def test_falls_back_to_a_default_version_when_none_is_given(self):
        resp = self.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {}})
        self.assertEqual(resp["result"]["protocolVersion"],
                         self.mod.DEFAULT_PROTOCOL_VERSION)


class PingTest(McpTestCase):
    def test_replies_with_an_empty_result(self):
        resp = self.dispatch({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        self.assertEqual(resp["result"], {})


class ToolsListTest(McpTestCase):
    def test_lists_the_expected_tool_set(self):
        """A fixture of the PUBLISHED surface — adding or removing a tool is a
        change to what clients see, so it should not be silent. It is not a
        check that the CLI and the tool table agree; that is
        CliCorrespondenceTest below, which asks the real parser."""
        resp = self.dispatch({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        names = {t["name"] for t in resp["result"]["tools"]}
        self.assertEqual(names, {
            "open", "join", "setup", "say", "sync", "mode", "pending", "read",
            "leave", "owed", "delete", "reopen", "invite", "channels",
            "briefing", "identify", "doctor", "fingerprint", "reload",
            "maintenance",
        })

    def test_every_tool_carries_a_description_and_object_schema(self):
        resp = self.dispatch({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        for tool in resp["result"]["tools"]:
            self.assertTrue(tool["description"])
            self.assertEqual(tool["inputSchema"]["type"], "object")


class DispatchTest(McpTestCase):
    def test_a_notification_gets_no_response_even_for_a_known_method(self):
        self.assertIsNone(self.dispatch({"jsonrpc": "2.0", "method": "ping"}))

    def test_a_notification_for_an_unknown_method_still_gets_no_response(self):
        self.assertIsNone(self.dispatch({"jsonrpc": "2.0", "method": "nope"}))

    def test_an_unknown_method_on_a_request_is_a_protocol_error(self):
        resp = self.dispatch({"jsonrpc": "2.0", "id": 9, "method": "nope"})
        self.assertEqual(resp["error"]["code"], -32601)

    def test_a_malformed_call_is_an_internal_error_not_a_crash(self):
        resp = self.dispatch({"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                              "params": {"name": "say", "arguments": "oops"}})
        self.assertEqual(resp["error"]["code"], -32603)


class ToolsCallTest(McpTestCase):
    def test_unknown_tool_name_is_a_protocol_error(self):
        resp = self.dispatch({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                              "params": {"name": "nope", "arguments": {}}})
        self.assertEqual(resp["error"]["code"], -32602)

    def test_a_missing_required_argument_is_a_protocol_error(self):
        resp = self.dispatch({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                              "params": {"name": "say",
                                        "arguments": {"text": "hi"}}})
        self.assertEqual(resp["error"]["code"], -32602)
        self.assertIn("channel", resp["error"]["message"])

    def test_a_successful_call_is_not_flagged_as_an_error(self):
        fake = RunCli((0, "sent #1 to room as me"))
        self.mod.run_cli = fake
        resp = self.dispatch({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                              "params": {"name": "say",
                                        "arguments": {"channel": "room",
                                                      "text": "hi",
                                                      "identity": "me"}}})
        self.assertFalse(resp["result"]["isError"])
        self.assertIn("sent #1", resp["result"]["content"][0]["text"])
        self.assertEqual(fake.calls[0][0], ["say", "room", "hi", "--as", "me"])

    def test_a_nonzero_exit_is_flagged_as_an_error(self):
        self.mod.run_cli = RunCli((1, "room is closed"))
        resp = self.dispatch({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                              "params": {"name": "say",
                                        "arguments": {"channel": "room",
                                                      "text": "hi"}}})
        self.assertTrue(resp["result"]["isError"])

    def test_empty_output_still_produces_content(self):
        self.mod.run_cli = RunCli((0, "   "))
        resp = self.dispatch({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                              "params": {"name": "channels", "arguments": {}}})
        self.assertEqual(resp["result"]["content"][0]["text"], "(no output)")

    def test_the_server_flag_is_threaded_before_the_subcommand(self):
        fake = RunCli((0, "ok"))
        self.mod.run_cli = fake
        self.dispatch({"jsonrpc": "2.0", "id": 11, "method": "tools/call",
                       "params": {"name": "channels",
                                 "arguments": {"server": "http://x:1"}}})
        self.assertEqual(fake.calls[0][0], ["--server", "http://x:1", "channels"])
        self.assertEqual(fake.calls[0][1], self.mod.TOOLS_BY_NAME["channels"]["timeout"])


class ArgvBuildersTest(McpTestCase):
    """Every optional flag, at least once, so its branch is exercised and not
    just present."""

    def test_open_with_every_optional_field(self):
        argv = self.call("open", {"channel": "c", "identity": "me",
                                  "broadcast": True, "topic": "t",
                                  "briefing": "rules", "max_messages": 50})
        self.assertEqual(argv, ["open", "c", "--as", "me", "--broadcast",
                                "--topic", "t", "--briefing", "rules",
                                "--max-messages", "50"])

    def test_open_bare(self):
        self.assertEqual(self.call("open", {"channel": "c"}), ["open", "c"])

    def test_join_with_every_optional_field(self):
        argv = self.call("join", {"channel": "c", "identity": "me",
                                  "topic": "t", "max_messages": 50})
        self.assertEqual(argv, ["join", "c", "--as", "me", "--topic", "t",
                                "--max-messages", "50"])

    def test_setup_with_every_optional_field(self):
        argv = self.call("setup", {"channel": "c", "identity": "me",
                                   "topic": "t", "max_messages": 50,
                                   "in_checkout": True})
        self.assertEqual(argv, ["setup", "c", "--as", "me", "--topic", "t",
                                "--max-messages", "50", "--in-checkout"])

    def test_say_with_text_only(self):
        self.assertEqual(self.call("say", {"channel": "c", "text": "hi"}),
                         ["say", "c", "hi"])

    def test_say_with_file_instead_of_text(self):
        argv = self.call("say", {"channel": "c", "file": "/tmp/msg.txt"})
        self.assertEqual(argv, ["say", "c", "--file", "/tmp/msg.txt"])

    def test_say_addressed_to_specific_identities(self):
        argv = self.call("say", {"channel": "c", "text": "hi", "to": "a,b"})
        self.assertEqual(argv, ["say", "c", "hi", "--to", "a,b"])

    def test_say_to_all(self):
        argv = self.call("say", {"channel": "c", "text": "hi", "to_all": True})
        self.assertEqual(argv, ["say", "c", "hi", "--to-all"])

    def test_say_to_none(self):
        argv = self.call("say", {"channel": "c", "text": "hi", "to_none": True})
        self.assertEqual(argv, ["say", "c", "hi", "--to-none"])

    def test_sync_takes_no_arguments(self):
        self.assertEqual(self.call("sync", {}), ["sync"])

    def test_mode_with_yes_and_identity(self):
        argv = self.call("mode", {"channel": "c", "mode": "broadcast",
                                  "identity": "me", "yes": True})
        self.assertEqual(argv, ["mode", "c", "broadcast", "--as", "me", "--yes"])

    def test_mode_bare(self):
        argv = self.call("mode", {"channel": "c", "mode": "ordinary"})
        self.assertEqual(argv, ["mode", "c", "ordinary"])

    def test_pending(self):
        argv = self.call("pending", {"channel": "c", "identity": "me"})
        self.assertEqual(argv, ["pending", "c", "--as", "me"])

    def test_read_with_peek_all_and_json(self):
        argv = self.call("read", {"channel": "c", "identity": "me",
                                  "peek": True, "all": True, "json": True})
        self.assertEqual(argv, ["read", "c", "--as", "me", "--peek", "--all",
                                "--json"])

    def test_read_bare(self):
        self.assertEqual(self.call("read", {"channel": "c"}), ["read", "c"])

    def test_leave_without_identity(self):
        self.assertEqual(self.call("leave", {"channel": "c"}), ["leave", "c"])

    def test_leave_with_identity(self):
        argv = self.call("leave", {"channel": "c", "identity": "me"})
        self.assertEqual(argv, ["leave", "c", "--as", "me"])

    def test_leave_with_ask(self):
        argv = self.call("leave", {"channel": "c", "ask": True})
        self.assertEqual(argv, ["leave", "c", "--ask"])

    def test_reopen_without_max_messages(self):
        self.assertEqual(self.call("reopen", {"channel": "c"}),
                         ["reopen", "c"])

    def test_reopen_with_max_messages(self):
        argv = self.call("reopen", {"channel": "c", "max_messages": 300})
        self.assertEqual(argv, ["reopen", "c", "--max-messages", "300"])

    def test_invite(self):
        self.assertEqual(self.call("invite", {"channel": "c"}), ["invite", "c"])

    def test_channels_with_json_and_all(self):
        argv = self.call("channels", {"json": True, "all": True})
        self.assertEqual(argv, ["channels", "--json", "--all"])

    def test_channels_bare(self):
        self.assertEqual(self.call("channels", {}), ["channels"])

    def test_briefing_with_text(self):
        argv = self.call("briefing", {"channel": "c", "text": "be nice",
                                      "identity": "me"})
        self.assertEqual(argv, ["briefing", "c", "be nice", "--as", "me"])

    def test_briefing_with_file(self):
        argv = self.call("briefing", {"channel": "c", "file": "/tmp/rules.md"})
        self.assertEqual(argv, ["briefing", "c", "--file", "/tmp/rules.md"])

    def test_identify(self):
        self.assertEqual(self.call("identify", {"identity": "me"}),
                         ["identify", "me"])

    def test_doctor(self):
        self.assertEqual(self.call("doctor", {}), ["doctor"])

    def test_fingerprint_of_a_specific_tree(self):
        argv = self.call("fingerprint", {"of": "/some/checkout"})
        self.assertEqual(argv, ["fingerprint", "--of", "/some/checkout"])

    def test_fingerprint_bare(self):
        self.assertEqual(self.call("fingerprint", {}), ["fingerprint"])

    def test_reload_bare(self):
        self.assertEqual(self.call("reload", {}), ["reload"])

    def test_reload_with_force_and_i_know(self):
        argv = self.call("reload", {"force": True, "i_know": True})
        self.assertEqual(argv, ["reload", "--force", "--i-know"])


class RunCliTest(McpTestCase):
    """run_cli's own internals — the only tests here that touch `subprocess`,
    and only as an attribute on THIS loaded module, never the shared one."""

    def test_stdin_is_devnull_so_a_file_dash_cannot_hang_or_race(self):
        """`say --file -` and `briefing --file -` read the CLI's stdin. Left
        unset, that is inherited from THIS server's own stdin — the same pipe
        `main()` reads JSON-RPC frames from — so a client passing file: "-"
        would race this process's read loop against the child's on the same
        fd. DEVNULL turns that into a harmless immediate EOF instead."""
        captured = {}

        def fake_run(cmd, **kwargs):
            captured.update(kwargs)

            class FakeResult:
                stdout, stderr, returncode = "ok", "", 0
            return FakeResult()

        self.mod.subprocess = type("Sub", (), {
            "run": staticmethod(fake_run),
            "DEVNULL": real_subprocess.DEVNULL,
            "TimeoutExpired": real_subprocess.TimeoutExpired,
        })
        self.mod.run_cli(["channels"], 5)
        self.assertEqual(captured.get("stdin"), real_subprocess.DEVNULL)

    def test_a_timeout_is_reported_rather_than_raised(self):
        def explode(*a, **kw):
            raise real_subprocess.TimeoutExpired(cmd="x", timeout=1)

        self.mod.subprocess = type("Sub", (), {
            "run": staticmethod(explode),
            "DEVNULL": real_subprocess.DEVNULL,
            "TimeoutExpired": real_subprocess.TimeoutExpired,
        })
        code, text = self.mod.run_cli(["channels"], 1)
        self.assertEqual(code, 1)
        self.assertIn("timed out", text)

    def test_stdout_and_stderr_are_combined_in_order(self):
        class FakeResult:
            stdout = "out\n"
            stderr = "err\n"
            returncode = 3

        self.mod.subprocess = type("Sub", (), {
            "run": staticmethod(lambda *a, **kw: FakeResult()),
            "DEVNULL": real_subprocess.DEVNULL,
        })
        code, text = self.mod.run_cli(["channels"], 5)
        self.assertEqual(code, 3)
        self.assertEqual(text, "out\nerr\n")


class MainLoopTest(McpTestCase):
    def test_reads_one_message_per_line_and_replies_per_request(self):
        code, responses = self.run_main(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["id"], 1)

    def test_blank_lines_between_messages_are_skipped(self):
        out = io.StringIO()
        stdin = sys.stdin
        sys.stdin = io.StringIO(
            "\n\n" + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n")
        try:
            with redirect_stdout(out):
                code = self.mod.main()
        finally:
            sys.stdin = stdin
        self.assertEqual(code, 0)
        self.assertEqual(len(out.getvalue().strip().splitlines()), 1)

    def test_unparseable_json_is_ignored_not_fatal(self):
        out = io.StringIO()
        stdin = sys.stdin
        sys.stdin = io.StringIO(
            "not json\n" + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n")
        try:
            with redirect_stdout(out):
                code = self.mod.main()
        finally:
            sys.stdin = stdin
        self.assertEqual(code, 0)
        self.assertEqual(len(out.getvalue().strip().splitlines()), 1)

    def test_a_json_scalar_line_is_ignored(self):
        out = io.StringIO()
        stdin = sys.stdin
        sys.stdin = io.StringIO(
            "42\n" + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n")
        try:
            with redirect_stdout(out):
                code = self.mod.main()
        finally:
            sys.stdin = stdin
        self.assertEqual(code, 0)
        self.assertEqual(len(out.getvalue().strip().splitlines()), 1)


class CliCorrespondenceTest(McpTestCase):
    """The MCP server and the CLI, checked against each other rather than
    against each other's descriptions.

    Every other test in this file replaces `run_cli` and asserts the argv a
    builder produced against a fixture written beside it. Both come from the
    same belief about what bin/llm_chat accepts, so they agree with each other
    no matter what the CLI actually does: rename a flag there and all of them
    stay green while every real MCP call fails. The two tests here are the
    only ones that consult the real parser, so they are the only ones that can
    notice the two halves drifting apart.
    """

    # Verbs deliberately not exposed over MCP. Empty, and an entry here should
    # have to be argued for — the reason to leave one out is that it cannot
    # work over MCP, not that nobody got to it.
    NOT_EXPOSED = set()

    def setUp(self):
        super().setUp()
        self.parser = load("llm_chat").build_parser()

    def maximal(self, tool, skip=()):
        """Every property in a tool's OWN schema, valued by its declared type.

        This was a hand-written dict, which made the tests below only as
        complete as a fixture somebody remembered to update. `leave` gained an
        `ask` property and the dict did not, so the flag it emits was never
        emitted here and the check silently stopped covering it — the same
        fixture-written-from-the-same-belief failure this whole class exists to
        catch, one level up, in the thing doing the catching.

        Read off the schema, a property is exercised the moment it is
        declared. Values need only be well-formed: combinations the CLI refuses
        for other reasons (text with file, to with to_all) still PARSE, and
        that exclusivity is enforced after parsing and covered where it lives.
        """
        args = {}
        for name, spec in tool["schema"]["properties"].items():
            if name in skip:
                continue
            if "enum" in spec:
                args[name] = spec["enum"][0]
            elif spec.get("type") == "boolean":
                args[name] = True
            elif spec.get("type") == "integer":
                args[name] = 5
            else:
                args[name] = "x"
        return args

    def argv_for(self, tool, skip=()):
        args = self.maximal(tool, skip)
        return self.mod._server_argv(args) + tool["build"](args)

    def cli_verbs(self):
        for action in self.parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                return set(action.choices)
        self.fail("the CLI parser has no subcommands")

    def test_every_cli_verb_is_exposed_as_a_tool(self):
        """Named for what it checks. The tool list this replaces was a set
        literal copied by hand, so a verb added to the CLI left it green and
        the MCP silently short of a tool."""
        self.assertEqual(self.cli_verbs() - self.NOT_EXPOSED,
                         set(self.mod.TOOLS_BY_NAME))

    def test_every_built_argv_is_accepted_by_the_real_parser(self):
        """argparse rejects an unknown or misspelled flag before any handler
        runs, so this fails on exactly the drift the argv fixtures cannot
        see."""
        for tool in self.mod.TOOLS:
            argv = self.argv_for(tool)
            with self.subTest(tool=tool["name"], argv=argv):
                try:
                    with redirect_stderr(io.StringIO()):
                        self.parser.parse_args(argv)
                except SystemExit:
                    self.fail("the CLI rejects the argv the %s tool builds: %s"
                              % (tool["name"], " ".join(argv)))

    def test_every_DECLARED_property_actually_reaches_the_argv(self):
        """A property in the schema that no builder reads is worse than a
        missing one.

        The schema is what the model sees, so a declared-but-ignored field is
        an instruction to pass something that then does nothing — silently.
        `say --to-none` arriving as a no-op does not fail, it just wakes
        everybody, and neither end can tell it was dropped. Nothing else here
        would notice: the argv fixtures only assert what the builder DOES
        emit, never that everything offered was.
        """
        for tool in self.mod.TOOLS:
            required = set(tool["schema"].get("required") or ())
            for name in tool["schema"]["properties"]:
                if name in required:
                    continue      # its absence is refused, which is different
                with self.subTest(tool=tool["name"], property=name):
                    self.assertNotEqual(
                        self.argv_for(tool), self.argv_for(tool, skip=(name,)),
                        "the %s tool declares %r and builds the same argv "
                        "with or without it" % (tool["name"], name))


if __name__ == "__main__":
    unittest.main()
