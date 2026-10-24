"""MCP 2025-06-18, JSON-only Streamable HTTP subset."""
import json
import threading

from ..session import ApiError, uid

VERSION = "2025-06-18"


class MCP:
    def __init__(self, tools):
        self.tools = tools
        self.sessions, self.tokens = {}, {}
        self.lock = threading.RLock()

    def grant(self, token, context):
        with self.lock:
            self.tokens[token] = context

    def revoke(self, token):
        with self.lock:
            self.tokens.pop(token, None)
            for key in list(self.sessions):
                if self.sessions[key].get("token") == token:
                    del self.sessions[key]

    def handle(self, message, headers, method="POST"):
        sid = headers.get("Mcp-Session-Id")
        if method == "GET":
            return 405, None, {}
        if method == "DELETE":
            with self.lock:
                if sid not in self.sessions:
                    return 404, None, {}
                del self.sessions[sid]
            return 200, None, {}
        rpc_id = message.get("id") if isinstance(message, dict) else None
        def error(code, text, detail=None):
            return 200, {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": text, "data": {"details": detail}}}, {}
        if (not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str)
                or "id" in message and (isinstance(rpc_id, bool) or not isinstance(rpc_id, (int, str)))):
            return error(-32600, "Invalid Request")
        name, params = message["method"], message.get("params", {})
        if not isinstance(params, dict):
            return error(-32602, "Invalid params")
        if name == "initialize":
            if "id" not in message or not all(k in params for k in ("protocolVersion", "clientInfo", "capabilities")):
                return error(-32602, "Invalid initialize params")
            token = headers.get("X-Agent-Run-Token")
            with self.lock:
                if token and token not in self.tokens:
                    return 403, {"error": "Недействительный токен запуска"}, {}
                new_sid = uid()
                self.sessions[new_sid] = {"ready": False, "token": token}
            return 200, {"jsonrpc": "2.0", "id": rpc_id, "result": {"protocolVersion": VERSION,
                         "capabilities": {"tools": {}}, "serverInfo": {"name": "STE Assistant", "version": "0.2.0"}}}, {"Mcp-Session-Id": new_sid}
        with self.lock:
            if not sid:
                return 400, {"error": "Нужен Mcp-Session-Id"}, {}
            if sid not in self.sessions:
                return 404, {"error": "MCP-сессия завершена"}, {}
            session = self.sessions[sid]
            token = session["token"]
            context = dict(self.tokens.get(token, {}))
        if headers.get("MCP-Protocol-Version") != VERSION:
            return 400, {"error": "Неверная версия MCP"}, {}
        if token and not context:
            return 404, {"error": "Запуск завершён"}, {}
        if name == "notifications/initialized" and "id" not in message:
            session["ready"] = True
            return 202, None, {}
        if "id" not in message:
            return 202, None, {}
        if name != "ping" and not session["ready"]:
            return error(-32600, "Initialization is incomplete")
        try:
            if name == "ping":
                result = {}
            elif name == "tools/list":
                result = {"tools": self.tools.listing()}
            elif name == "tools/call":
                if set(params) - {"name", "arguments"} or not isinstance(params.get("name"), str):
                    return error(-32602, "Invalid tool params")
                if params["name"] not in self.tools.registry:
                    return error(-32602, "Unknown tool", "UNKNOWN_TOOL")
                try:
                    value = self.tools.call(params["name"], params.get("arguments", {}), context)
                    result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, allow_nan=False)}], "isError": False}
                except ApiError as exc:
                    if exc.code == "INVALID_ARGUMENTS":
                        return error(-32602, str(exc))
                    value = {"code": exc.code, "message": str(exc), "retryable": False, "details": exc.details}
                    result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "isError": True}
            else:
                return error(-32601, "Method not found", "UNKNOWN_METHOD")
            return 200, {"jsonrpc": "2.0", "id": rpc_id, "result": result}, {}
        except Exception:
            return error(-32603, "Internal error")
