"""Internal agent client: all tool invocations cross the real localhost endpoint."""
import json
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError

from .contracts import fail, strict_json
from .mcp_server import VERSION


class MCPClient:
    def __init__(self, server, run_token):
        self.url, self.token, self.run_token = server.origin + "/mcp", server.token, run_token
        self.sid, self.sequence = None, 0
        self.opener = build_opener(ProxyHandler({}))

    def request(self, method, params=None, notification=False):
        self.sequence += 1
        body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notification:
            body["id"] = self.sequence
        headers = {"X-Assistant-Token": self.token, "Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream", "X-Agent-Run-Token": self.run_token}
        if self.sid:
            headers.update({"Mcp-Session-Id": self.sid, "MCP-Protocol-Version": VERSION})
        request = Request(self.url, json.dumps(body, allow_nan=False).encode("utf-8"), headers)
        try:
            with self.opener.open(request, timeout=35) as response:
                self.sid = response.headers.get("Mcp-Session-Id", self.sid)
                if notification:
                    return None
                result = strict_json(response.read(8 * 1024 * 1024).decode("utf-8"))
        except HTTPError as error:
            error.close()
            fail("Ошибка локального MCP-соединения", "TOOL_TIMEOUT", 502)
        except (OSError, ValueError):
            fail("Ошибка локального MCP-соединения", "TOOL_TIMEOUT", 502)
        if result.get("id") != self.sequence or "error" in result:
            fail("MCP отклонил запрос", "INVALID_ARGUMENTS")
        return result["result"]

    def initialize(self):
        self.request("initialize", {"protocolVersion": VERSION, "capabilities": {}, "clientInfo": {"name": "STE agent", "version": "0.2.0"}})
        self.request("notifications/initialized", notification=True)

    def call(self, name, arguments):
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        value = strict_json(result["content"][0]["text"])
        if result.get("isError"):
            fail(value["message"], value["code"], 409)
        return value

    def close(self):
        if self.sid:
            request = Request(self.url, headers={"X-Assistant-Token": self.token, "Mcp-Session-Id": self.sid,
                              "MCP-Protocol-Version": VERSION}, method="DELETE")
            try:
                self.opener.open(request, timeout=2).close()
            except HTTPError as error:
                error.close()
            except OSError:
                pass
            self.sid = None
