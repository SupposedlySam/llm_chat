"""The MCP server: JSON-RPC framing over stdio, and the tool table it exposes.

Shells out to the CLI through one seam, `run_cli`, exactly as bin/llm_chat
shells out to zonai through one seam, `call` — so a test replaces that single
function instead of the shared `subprocess` module, and never risks leaking a
stub into a test file that runs after it.
"""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout

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
    def test_lists_every_cli_subcommand_as_a_tool(self):
        resp = self.dispatch({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        names = {t["name"] for t in resp["result"]["tools"]}
        self.assertEqual(names, {"setup", "open", "join", "say", "read",
                                 "leave", "reopen", "invite", "channels",
                                 "doctor", "fingerprint", "reload"})

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
                                        "arguments": {"channel": "room"}}})
        self.assertEqual(resp["error"]["code"], -32602)
        self.assertIn("text", resp["error"]["message"])

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

    def call(self, name, arguments):
        fake = RunCli((0, "ok"))
        self.mod.run_cli = fake
        self.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}})
        return fake.calls[0][0]

    def test_setup_with_every_optional_field(self):
        argv = self.call("setup", {"channel": "c", "identity": "me",
                                   "topic": "t", "max_messages": 50,
                                   "in_checkout": True})
        self.assertEqual(argv, ["setup", "c", "--as", "me", "--topic", "t",
                                "--max-messages", "50", "--in-checkout"])

    def test_open_with_every_optional_field(self):
        argv = self.call("open", {"channel": "c", "identity": "me",
                                  "topic": "t", "max_messages": 50})
        self.assertEqual(argv, ["open", "c", "--as", "me", "--topic", "t",
                                "--max-messages", "50"])

    def test_join_with_every_optional_field(self):
        argv = self.call("join", {"channel": "c", "identity": "me",
                                  "topic": "t", "max_messages": 50})
        self.assertEqual(argv, ["join", "c", "--as", "me", "--topic", "t",
                                "--max-messages", "50"])

    def test_say_without_identity(self):
        argv = self.call("say", {"channel": "c", "text": "hi"})
        self.assertEqual(argv, ["say", "c", "hi"])

    def test_read_with_peek_and_all(self):
        argv = self.call("read", {"channel": "c", "identity": "me",
                                  "peek": True, "all": True})
        self.assertEqual(argv, ["read", "c", "--as", "me", "--peek", "--all"])

    def test_read_bare(self):
        self.assertEqual(self.call("read", {"channel": "c"}), ["read", "c"])

    def test_leave_without_identity(self):
        self.assertEqual(self.call("leave", {"channel": "c"}), ["leave", "c"])

    def test_leave_with_identity(self):
        argv = self.call("leave", {"channel": "c", "identity": "me"})
        self.assertEqual(argv, ["leave", "c", "--as", "me"])

    def test_reopen_without_max_messages(self):
        self.assertEqual(self.call("reopen", {"channel": "c"}),
                         ["reopen", "c"])

    def test_reopen_with_max_messages(self):
        argv = self.call("reopen", {"channel": "c", "max_messages": 300})
        self.assertEqual(argv, ["reopen", "c", "--max-messages", "300"])

    def test_invite(self):
        self.assertEqual(self.call("invite", {"channel": "c"}), ["invite", "c"])

    def test_channels(self):
        self.assertEqual(self.call("channels", {}), ["channels"])

    def test_doctor(self):
        self.assertEqual(self.call("doctor", {}), ["doctor"])

    def test_fingerprint(self):
        self.assertEqual(self.call("fingerprint", {}), ["fingerprint"])

    def test_reload_bare(self):
        self.assertEqual(self.call("reload", {}), ["reload"])

    def test_reload_with_force_and_i_know(self):
        argv = self.call("reload", {"force": True, "i_know": True})
        self.assertEqual(argv, ["reload", "--force", "--i-know"])


class RunCliTest(McpTestCase):
    """run_cli's own internals — the only tests here that touch `subprocess`,
    and only as an attribute on THIS loaded module, never the shared one."""

    def test_a_timeout_is_reported_rather_than_raised(self):
        import subprocess as real_subprocess

        def explode(*a, **kw):
            raise real_subprocess.TimeoutExpired(cmd="x", timeout=1)

        self.mod.subprocess = type("Sub", (), {
            "run": staticmethod(explode),
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


if __name__ == "__main__":
    unittest.main()
