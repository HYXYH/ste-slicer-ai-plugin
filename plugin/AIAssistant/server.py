"""Python 3.5-compatible HTTP API; no third-party dependencies."""
import json
import logging
import mimetypes
import os
import binascii
import hmac
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlsplit, unquote

from .session import ApiError
from .uploads import receive, MAX_FILE_SIZE

log = logging.getLogger(__name__)
WEB_DIR = os.path.join(os.path.dirname(__file__), "web")


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

    def start(self):
        self.thread = threading.Thread(target=self.serve_forever, name="AI Assistant HTTP")
        self.thread.daemon = True
        self.thread.start()

    def stop(self):
        if self.thread:
            self.shutdown()
            self.thread.join(timeout=3)
        self.server_close()

    def browser_url(self):
        return self.origin + "/#token=" + self.token


class Handler(BaseHTTPRequestHandler):
    # One request per connection makes rejected/partial uploads unambiguous.
    protocol_version = "HTTP/1.0"

    def setup(self):
        BaseHTTPRequestHandler.setup(self)
        self.connection.settimeout(60)

    def log_message(self, format_string, *args):
        # Never write URLs, auth headers or uploaded task descriptions to access logs.
        pass

    def _send(self, status, data, content_type="application/json; charset=utf-8"):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
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
            data = json.loads(body.decode("utf-8"))
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

    def _handle(self, method):
        try:
            path = urlsplit(self.path).path
            if path in ("/", "/app.js", "/style.css") and method == "GET":
                if self.headers.get("Host") != self.server.origin[len("http://"):]:
                    raise ApiError("Недопустимый Host", 403)
                filename = "index.html" if path == "/" else path[1:]
                with open(os.path.join(WEB_DIR, filename), "rb") as source:
                    body = source.read()
                content_type = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8",
                                "style.css": "text/css; charset=utf-8"}[filename]
                self._send(200, body, content_type)
                return
            self._authorize()
            result, status = self._route(method, path)
            if result is not None:
                self._send(status, result)
        except ApiError as error:
            self._send(error.status, {"error": str(error)})
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
            result, operations = store.message(sid, body.get("request_id"), body.get("text", ""),
                                                body.get("file_ids", []), self.server.bridge.snapshot())
            if operations:
                try:
                    self.server.bridge.submit(operations)
                except Exception:
                    log.exception("Cannot enqueue model import")
                    for operation in operations:
                        store.finish(sid, operation["id"], "error", "Не удалось запустить импорт. Повторите отправку файла.")
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
