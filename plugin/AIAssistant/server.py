"""Python 3.5-compatible HTTP API; no third-party dependencies."""
import json
import logging
import mimetypes
import os
import binascii
import hmac
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlsplit, unquote, parse_qs

from .session import ApiError
from .uploads import receive, MAX_FILE_SIZE

log = logging.getLogger(__name__)
WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
CONNECTION_DRAIN_TIMEOUT = 1.0
CONNECTION_DRAIN_LIMIT = 1024 * 1024


class LocalServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, store, bridge):
        self.store = store
        self.bridge = bridge
        self.token = binascii.hexlify(os.urandom(32)).decode("ascii")
        HTTPServer.__init__(self, ("127.0.0.1", 0), Handler)
        self.origin = "http://127.0.0.1:{}".format(self.server_port)
        self.thread = None
        from .agent.tools import Tools
        from .agent.settings import Settings
        from .agent.mcp_server import MCP
        from .agent.plans import Plans
        from .agent.runner import Runner
        self.settings = Settings(bridge)
        from .analytics import Analytics
        self.analytics = Analytics(store)
        self.tools = Tools(store, bridge)
        self.mcp = MCP(self.tools)
        self.plans = Plans(self)
        self.tools.authorize_change = self.plans.authorize
        self.runner = Runner(self)
        self.bridge.on_slice_failure = lambda sid, oid: self.runner.start(sid, "slice-error-" + oid)

    def start(self):
        self.thread = threading.Thread(target=self.serve_forever, name="AI Assistant HTTP")
        self.thread.daemon = True
        self.thread.start()

    def stop(self):
        self.runner.close()
        self.plans.close()
        if self.thread:
            self.shutdown()
            self.thread.join(timeout=3)
        self.server_close()

    def browser_url(self):
        return self.origin + "/#token=" + self.token

    def shutdown_request(self, request):
        # A close with unread upload bytes can reset the connection on Windows,
        # hiding an already-sent JSON error. Send EOF first, then drain briefly.
        try:
            request.shutdown(socket.SHUT_WR)
            deadline = time.monotonic() + CONNECTION_DRAIN_TIMEOUT
            remaining = CONNECTION_DRAIN_LIMIT
            while remaining:
                timeout = deadline - time.monotonic()
                if timeout <= 0:
                    break
                request.settimeout(timeout)
                chunk = request.recv(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass
        finally:
            self.close_request(request)


class Handler(BaseHTTPRequestHandler):
    # One request per connection makes rejected/partial uploads unambiguous.
    protocol_version = "HTTP/1.0"

    def setup(self):
        BaseHTTPRequestHandler.setup(self)
        self.connection.settimeout(60)

    def log_message(self, format_string, *args):
        # Never write URLs, auth headers or uploaded task descriptions to access logs.
        pass

    def _send(self, status, data, content_type="application/json; charset=utf-8", headers=None):
        if data is None:
            data = b""
        if not isinstance(data, bytes):
            data = json.dumps(self.server.settings.redact(data), ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' blob:; "
                         "style-src 'self'; script-src 'self'; connect-src 'self'; "
                         "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(data)

    def _length(self, maximum):
        if self.headers.get("Transfer-Encoding"):
            raise ApiError("Transfer-Encoding не поддерживается")
        values = self.headers.get_all("Content-Length", [])
        if len(values) != 1:
            raise ApiError("Нужен один Content-Length", 411)
        try:
            length = int(values[0])
        except ValueError:
            raise ApiError("Некорректный Content-Length")
        if length < 0 or length > maximum:
            raise ApiError("Превышен размер запроса", 413)
        return length

    def _json(self):
        if self.headers.get_content_type() != "application/json":
            raise ApiError("Ожидается application/json", 415)
        length = self._length(1024 * 1024)
        body = self.rfile.read(length)
        if len(body) != length:
            raise ApiError("Запрос получен не полностью")
        try:
            from .agent.contracts import strict_json
            data = strict_json(body.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise ApiError("Некорректный JSON")
        if not isinstance(data, dict):
            raise ApiError("Ожидается JSON-объект")
        return data

    def _authorize(self):
        if self.headers.get("Host") != self.server.origin[len("http://"):]:
            raise ApiError("Недопустимый Host", 403)
        origin = self.headers.get("Origin")
        if origin and origin != self.server.origin:
            raise ApiError("Недопустимый Origin", 403)
        token = self.headers.get("X-Assistant-Token", "")
        if not hmac.compare_digest(token.encode("utf-8"), self.server.token.encode("ascii")):
            raise ApiError("Откройте чат через меню AI Assistant в слайсере", 401)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_DELETE(self):
        self._handle("DELETE")

    def _handle(self, method):
        try:
            path = urlsplit(self.path).path
            if path in ("/", "/app.js", "/analytics.js", "/style.css") and method == "GET":
                if self.headers.get("Host") != self.server.origin[len("http://"):]:
                    raise ApiError("Недопустимый Host", 403)
                filename = "index.html" if path == "/" else path[1:]
                with open(os.path.join(WEB_DIR, filename), "rb") as source:
                    body = source.read()
                content_type = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8",
                                "analytics.js": "text/javascript; charset=utf-8", "style.css": "text/css; charset=utf-8"}[filename]
                self._send(200, body, content_type)
                return
            self._authorize()
            if path == "/mcp":
                self._mcp(method)
                return
            result, status = self._route(method, path)
            if result is not None:
                self._send(status, result)
        except ApiError as error:
            self._send(error.status, {"error": str(error), "code": error.code, "details": error.details})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return
        except Exception:
            log.exception("AI Assistant HTTP request failed")
            try:
                self._send(500, {"error": "Ошибка сервера. Подробности в журнале слайсера."})
            except OSError:
                pass

    def _route(self, method, path):
        store = self.server.store
        workflow = self._workflow(method, path)
        if workflow is not None:
            return workflow
        if path == "/api/context" and method == "GET":
            return self.server.bridge.snapshot(), 200
        if path == "/api/sessions":
            if method == "GET":
                return store.listing(), 200
            if method == "POST":
                return store.create(self._json().get("title", "Новая задача")), 201
        parts = path.strip("/").split("/")
        if len(parts) < 3 or parts[:2] != ["api", "sessions"]:
            raise ApiError("Маршрут не найден", 404)
        sid = parts[2]
        if len(parts) == 3:
            if method == "GET":
                return store.get(sid), 200
            if method == "PATCH":
                return store.rename(sid, self._json().get("title")), 200
        if len(parts) == 4 and parts[3] == "select" and method == "POST":
            return store.select(sid), 200
        if len(parts) == 4 and parts[3] == "files" and method == "POST":
            length = self._length(MAX_FILE_SIZE)
            name = unquote(self.headers.get("X-File-Name", ""))
            return receive(store, sid, name, self.rfile, length), 201
        if len(parts) == 4 and parts[3] == "messages" and method == "POST":
            body = self._json()
            result, operations = store.message(sid, body.get("request_id"), self.server.settings.redact(body.get("text", "")),
                                                body.get("file_ids", []), self.server.bridge.snapshot())
            if operations:
                try:
                    self.server.bridge.submit(operations)
                except Exception:
                    log.exception("Cannot enqueue model import")
                    for operation in operations:
                        store.finish(sid, operation["id"], "error", "Не удалось запустить импорт. Повторите отправку файла.")
            if self.server.settings.public()["key_configured"] and "run_id" not in result:
                run = self.server.runner.start(sid, "message-" + body["request_id"])
                result.update(run)
                store.edit(sid, lambda s: s["requests"][body["request_id"]]["result"].update(run))
            return result, 201
        if len(parts) == 5 and parts[3] == "operations" and method == "GET":
            return store.operation(sid, parts[4]), 200
        if len(parts) == 5 and parts[3] == "files" and method == "GET":
            attachment = store.file(sid, parts[4])
            if attachment["kind"] != "image":
                raise ApiError("Просмотр поддерживается только для изображений", 415)
            with open(attachment["path"], "rb") as source:
                data = source.read()
            self._send(200, data, mimetypes.guess_type(attachment["name"])[0] or "application/octet-stream")
            return None, 200
        raise ApiError("Маршрут не найден", 404)

    def _mcp(self, method):
        message = None
        if method == "POST":
            from .agent.contracts import strict_json
            if self.headers.get_content_type() != "application/json":
                raise ApiError("Ожидается application/json", 415)
            length = self._length(1024 * 1024)
            try:
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError()
                message = strict_json(body.decode("utf-8"))
            except (ValueError, UnicodeError):
                self._send(200, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                return
        status, result, headers = self.server.mcp.handle(message, self.headers, method)
        self._send(status, result, headers=headers)

    def _workflow(self, method, path):
        from .agent.contracts import validate, obj, ID
        from .session import uid
        server, store = self.server, self.server.store
        if path in ("/api/analytics", "/api/analytics/export") and method == "GET":
            query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
            if set(query) - {"days"} or len(query.get("days", ["30"])) != 1:
                raise ApiError("Неизвестные или повторяющиеся параметры отчёта")
            try:
                days = int(query.get("days", ["30"])[0])
            except ValueError:
                raise ApiError("Неверный период отчёта")
            report = server.analytics.snapshot(days)
            if path.endswith("/export"):
                self._send(200, server.analytics.csv(server.settings.redact(report)), "text/csv; charset=utf-8",
                           headers={"Content-Disposition": 'attachment; filename="assistant-analytics.csv"'})
                return None, 200
            return report, 200
        if path == "/api/settings/llm":
            if method == "GET":
                return server.settings.public(), 200
            if method == "PATCH":
                return server.settings.update(self._json()), 200
        if path == "/api/settings/llm/clear-key" and method == "POST":
            validate(self._json(), obj())
            return server.settings.update({}, clear=True), 200
        if path == "/api/settings/llm/test" and method == "POST":
            body = self._json()
            validate(body, obj({"session_id": ID, "request_id": ID}, ["session_id", "request_id"]))
            store.get(body["session_id"])
            return server.runner.connection_test(body["session_id"], body["request_id"]), 202
        parts = path.strip("/").split("/")
        if len(parts) < 4 or parts[:2] != ["api", "sessions"]:
            return None
        sid, action = parts[2:4]
        if action not in ("events", "runs", "answers", "plans", "export-target", "exports"):
            return None
        store.get(sid)
        if action == "events" and len(parts) == 4 and method == "GET":
            try:
                after = int(parse_qs(urlsplit(self.path).query).get("after", ["0"])[0])
                if after < 0:
                    raise ValueError()
            except ValueError:
                raise ApiError("Неверный номер события")
            events = store.get(sid)["events"]
            return {"events": [e for e in events if e["seq"] > after][:100], "last_seq": events[-1]["seq"] if events else 0}, 200
        if action == "runs" and len(parts) == 4 and method == "POST":
            body = self._json()
            validate(body, obj({"request_id": ID}, ["request_id"]))
            return server.runner.start(sid, body["request_id"]), 202
        if action == "runs" and len(parts) == 6 and parts[5] == "cancel" and method == "POST":
            validate(self._json(), obj())
            return server.runner.cancel(sid, parts[4]), 200
        if action == "answers" and len(parts) == 4 and method == "POST":
            return server.runner.answers(sid, self._json()), 202
        if action == "plans" and len(parts) >= 5:
            plan = store.get(sid)["plans"].get(parts[4])
            if not plan:
                raise ApiError("План не найден", 404)
            if len(parts) == 5 and method == "GET":
                return plan, 200
            if len(parts) == 6 and method == "POST":
                body = self._json()
                if parts[5] == "approve":
                    validate(body, obj({"version": {"type": "integer", "minimum": 1}}, ["version"]))
                    return server.plans.approve(sid, parts[4], body["version"]), 200
                if parts[5] == "apply":
                    validate(body, obj({"request_id": ID}, ["request_id"]))
                    result = server.plans.apply(sid, parts[4], body["request_id"])
                    return result, 202
        if action in ("export-target", "exports") and len(parts) == 4 and method == "POST":
            body = self._json()
            if action == "export-target":
                validate(body, obj({"overwrite": {"type": "boolean"}, "request_id": ID}, ["request_id"]))
            else:
                validate(body, obj({"operation_id": ID, "export_target_id": ID, "request_id": ID}, ["operation_id", "export_target_id", "request_id"]))
            key = action + ":" + body["request_id"]
            def reserve(s):
                prior = s["workflow_requests"].get(key)
                if prior:
                    if prior["payload"] != body:
                        raise ApiError("request_id уже использован", 409)
                    return prior["result"], False
                oid = uid()
                result = {"operation_id": oid, "state": "queued"}
                s["operations"][oid] = {"id": oid, "kind": action, "status": "queued"}
                s["workflow_requests"][key] = {"payload": body, "result": result}
                return result, True
            result, fresh = store.edit(sid, reserve)
            if fresh:
                def execute():
                    try:
                        if action == "export-target":
                            data = server.bridge.call("choose_export_target", {"session_id": sid, "overwrite": body.get("overwrite", False)})
                        else:
                            data = server.plans.export(sid, body["operation_id"], body["export_target_id"], body["request_id"])
                        values = {"status": "completed", "result": data}
                    except ApiError as error:
                        values = {"status": "error", "error": str(error), "code": error.code}
                    except Exception:
                        values = {"status": "error", "error": "Операция не подтверждена. Проверьте слайсер.", "code": "EXPORT_FAILED"}
                    store.edit(sid, lambda s: s["operations"][result["operation_id"]].update(values))
                worker = threading.Thread(target=execute, name="STE export")
                worker.daemon = True
                worker.start()
            return result, 202
        return None
