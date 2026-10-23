import ast
import base64
import http.client
import io
import json
import os
import socket
import struct
import sys
import tempfile
import unittest
import zipfile
from urllib.parse import quote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "plugin"))
from AIAssistant.session import ApiError, SessionStore
from AIAssistant.server import LocalServer, Handler
from AIAssistant.uploads import receive
from AIAssistant.slicer_bridge import SlicerBridge, read_context

STL = b"test".ljust(80, b" ") + struct.pack("<I", 1) + struct.pack(
    "<12fH", 0., 0., 1., 0., 0., 0., 10., 0., 0., 0., 10., 0., 0)
OBJ = b"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")


def three_mf(model=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("3D/3dmodel.model", model or
                         '<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
                         '<resources><object id="1" type="model"><mesh><vertices>'
                         '<vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/>'
                         '<vertex x="0" y="10" z="0"/></vertices><triangles>'
                         '<triangle v1="0" v2="1" v3="2"/></triangles></mesh></object></resources>'
                         '<build><item objectid="1"/></build></model>')
    return output.getvalue()


class RecordingBridge:
    def __init__(self):
        self.submissions = []

    def snapshot(self):
        return {"available": True, "printer": {"name": "Test printer"}}

    def submit(self, operations):
        self.submissions.extend(operations)


class MemorySocket:
    """Exercise BaseHTTPRequestHandler's actual wire parser without opening a client socket."""
    def __init__(self, data):
        self.incoming = io.BytesIO(data)
        self.outgoing = io.BytesIO()

    def makefile(self, *args):
        return self.incoming

    def sendall(self, data):
        self.outgoing.write(data)

    def settimeout(self, value):
        pass


class HTTPWorkflowTests(unittest.TestCase):
    def setUp(self):
        scratch = os.path.join(ROOT, ".local-assistant-data", "tests")
        os.makedirs(scratch, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.store = SessionStore(self.temp.name)
        self.bridge = RecordingBridge()
        self.server = LocalServer(self.store, self.bridge)
        self.live_http = os.environ.get("ASSISTANT_LIVE_HTTP") == "1"
        if self.live_http:
            self.server.start()
        self.sid = self.store.listing()["active_session_id"]

    def tearDown(self):
        self.server.stop()
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None, authorized=True):
        request_headers = dict(headers or {})
        if authorized:
            request_headers["X-Assistant-Token"] = self.server.token
        if isinstance(body, dict):
            body = json.dumps(body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if self.live_http:
            connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=4)
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
        else:
            body = body or b""
            request_headers.setdefault("Host", "127.0.0.1:" + str(self.server.server_port))
            if body or method in ("POST", "PATCH"):
                request_headers.setdefault("Content-Length", str(len(body)))
            wire = "{} {} HTTP/1.0\r\n".format(method, path)
            wire += "".join("{}: {}\r\n".format(k, v) for k, v in request_headers.items())
            sock = MemorySocket(wire.encode("ascii") + b"\r\n" + body)
            Handler(sock, ("127.0.0.1", 1), self.server)
            connection = MemorySocket(sock.outgoing.getvalue())
            response = http.client.HTTPResponse(connection)
            response.begin()
        content = response.read()
        content_type = response.getheader("Content-Type")
        status = response.status
        if self.live_http:
            connection.close()
        return status, json.loads(content.decode("utf-8")) if "application/json" in content_type else content

    def upload(self, name="деталь.STL", data=STL, sid=None):
        return self.request("POST", "/api/sessions/{}/files".format(sid or self.sid), data,
                            {"X-File-Name": quote(name), "Content-Type": "application/octet-stream"})

    def message(self, fid, request_id="retry-1", sid=None, text="Кронштейн под нагрузку"):
        return self.request("POST", "/api/sessions/{}/messages".format(sid or self.sid),
                            {"request_id": request_id, "text": text, "file_ids": [fid]})

    def test_complete_flow_is_idempotent_and_session_scoped(self):
        status, attachment = self.upload()
        self.assertEqual(status, 201)
        fid = attachment["id"]
        status, result = self.message(fid)
        self.assertEqual(status, 201)
        self.assertEqual(len(result["operation_ids"]), 1)
        self.assertEqual(self.message(fid)[1], result)
        self.assertEqual(len(self.bridge.submissions), 1)
        status, second = self.request("POST", "/api/sessions", {"title": "Вторая задача"})
        second_id = second["id"]
        self.assertEqual(self.message(fid, sid=second_id)[0], 404)
        # Switching conversations while the first job completes must not redirect its result.
        oid = result["operation_ids"][0]
        self.store.finish(self.sid, oid, "imported", object_ids=["node-1"])
        self.assertEqual(self.request("GET", "/api/sessions/" + second_id)[1]["messages"], [])
        first = self.request("GET", "/api/sessions/" + self.sid)[1]
        self.assertEqual(first["task"]["status"], "ready")
        self.assertEqual(first["files"][fid]["status"], "imported")
        self.assertNotIn("path", first["files"][fid])
        self.assertNotIn("requests", first)
        self.assertEqual(first["messages"][0]["context"]["printer"]["name"], "Test printer")
        self.assertEqual(self.message(fid, text="Другой текст")[0], 409)
        self.assertEqual(self.request("GET", "/api/sessions/{}/operations/{}".format(self.sid, oid))[1]["status"], "imported")
        self.assertEqual(self.request("GET", "/api/sessions/{}/operations/{}".format(second_id, oid))[0], 404)

    def test_rename_select_reload_and_reimport_after_restart(self):
        attachment = self.upload()[1]
        result = self.message(attachment["id"])[1]
        self.store.finish(self.sid, result["operation_ids"][0], "imported", object_ids=["old-node"])
        self.assertEqual(self.request("PATCH", "/api/sessions/" + self.sid, {"title": "Печать шестерни"})[0], 200)
        self.request("POST", "/api/sessions", {})
        self.request("POST", "/api/sessions/" + self.sid + "/select")
        restarted = SessionStore(self.temp.name)
        self.assertEqual(restarted.listing()["active_session_id"], self.sid)
        recovered = restarted.get(self.sid)
        self.assertEqual(recovered["title"], "Печать шестерни")
        self.assertEqual(recovered["files"][attachment["id"]]["status"], "saved")
        self.assertFalse(recovered["files"][attachment["id"]]["scene_present"])
        self.assertEqual(recovered["task"]["status"], "needs_model")
        result, operations = restarted.message(self.sid, "new-import", "", [attachment["id"]], {})
        self.assertEqual(len(operations), 1)

    def test_invalid_uploads_and_unsupported_formats_leave_no_files(self):
        for name, content in [("wrong.exe", STL), ("../bad.stl", STL),
                              ("bad.stl", b"not a mesh"), ("empty.stl", b""),
                              ("bad.png", b"picture"), ("bad.3mf", b"not a zip"),
                              ("external.obj", OBJ + b"mtllib material.mtl\n")]:
            with self.subTest(name=name):
                self.assertGreaterEqual(self.upload(name, content)[0], 400)
        self.assertEqual(self.store.get(self.sid)["files"], {})
        for root, directories, files in os.walk(os.path.join(self.temp.name, "uploads")):
            self.assertEqual(files, [])

    def test_supported_files_and_protected_image_preview(self):
        for name, content in [("triangle.obj", OBJ), ("triangle.3mf", three_mf()),
                              ("picture.png", PNG), ("ascii.stl", b"solid test\nfacet normal 0 0 1\nouter loop\n"
                               b"vertex 0 0 0\nvertex 10 0 0\nvertex 0 10 0\nendloop\nendfacet\nendsolid test\n")]:
            with self.subTest(name=name):
                self.assertEqual(self.upload(name, content)[0], 201)
        image = self.upload("test.png", PNG)[1]
        path = "/api/sessions/{}/files/{}".format(self.sid, image["id"])
        self.assertEqual(self.request("GET", path)[1], PNG)
        self.assertEqual(self.request("GET", path, authorized=False)[0], 401)

    def test_rejects_xml_entities_and_zip_traversal(self):
        entity = " " * 5000 + '<!DOCTYPE model [<!ENTITY x "oops">]><model>&x;</model>'
        self.assertEqual(self.upload("entities.3mf", three_mf(entity))[0], 400)
        data = io.BytesIO(three_mf())
        with zipfile.ZipFile(data, "a") as archive:
            archive.writestr("../../outside", "payload")
        self.assertEqual(self.upload("paths.3mf", data.getvalue())[0], 400)

    def test_api_authorization_origin_host_and_static_traversal(self):
        self.assertEqual(self.request("GET", "/api/sessions", authorized=False)[0], 401)
        self.assertEqual(self.request("POST", "/api/sessions", {}, {"Origin": "https://example.org"})[0], 403)
        self.assertEqual(self.request("GET", "/api/sessions", headers={"Host": "attacker.example"})[0], 403)
        self.assertEqual(self.request("GET", "/../sessions.json")[0], 404)
        self.assertEqual(self.request("GET", "/", authorized=False)[0], 200)
        self.assertEqual(self.request("POST", "/api/sessions", b"not json", {"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.request("POST", "/api/sessions", b"[]")[0], 415)

    def test_oversized_upload_rejected_before_reading_body(self):
        status, body = self.request("POST", "/api/sessions/" + self.sid + "/files", b"",
                                    {"Content-Length": str(101 * 1024 * 1024), "X-File-Name": "big.stl"})
        self.assertEqual(status, 413)

    def test_early_rejection_returns_json_with_upload_still_in_flight(self):
        if not self.live_http:
            self.skipTest("Requires ASSISTANT_LIVE_HTTP=1 and a real TCP connection")
        for name, authorized, length, expected in [
                ("../bad.stl", True, 32768, 400),
                ("wrong.exe", True, 32768, 415),
                ("model.stl", False, 32768, 401),
                ("model.stl", True, 101 * 1024 * 1024, 413)]:
            with self.subTest(name=name, status=expected):
                with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=4) as connection:
                    headers = ("POST /api/sessions/{}/files HTTP/1.0\r\n"
                               "Host: 127.0.0.1:{}\r\n"
                               "X-File-Name: {}\r\n"
                               "Content-Length: {}\r\n").format(
                                   self.sid, self.server.server_port, quote(name), length)
                    if authorized:
                        headers += "X-Assistant-Token: {}\r\n".format(self.server.token)
                    connection.sendall((headers + "\r\n").encode("ascii"))
                    response = http.client.HTTPResponse(connection)
                    try:
                        # Rejection must arrive without waiting for the declared body.
                        response.begin()
                        self.assertEqual(response.status, expected)
                        # Bytes already in flight must not destroy the JSON response.
                        connection.sendall(b"x" * 32768)
                        connection.shutdown(socket.SHUT_WR)
                        self.assertIn("error", json.loads(response.read().decode("utf-8")))
                        self.assertEqual(connection.recv(1), b"")
                    finally:
                        response.close()
        self.assertEqual(self.store.get(self.sid)["files"], {})

    def test_interrupted_import_and_scene_removal(self):
        fid = self.upload()[1]["id"]
        result = self.message(fid)[1]
        oid = result["operation_ids"][0]
        restarted = SessionStore(self.temp.name)
        recovered = restarted.get(self.sid)
        self.assertEqual(recovered["operations"][oid]["status"], "interrupted")
        self.assertEqual(recovered["task"]["status"], "needs_attention")
        self.store.finish(self.sid, oid, "imported", object_ids=["n"])
        self.store.reconcile_scene({"n"})
        self.assertTrue(self.store.get(self.sid)["files"][fid]["scene_present"])
        self.store.reconcile_scene(set())
        self.assertEqual(self.store.get(self.sid)["files"][fid]["status"], "saved")


class ImportCompletionTests(unittest.TestCase):
    def test_import_is_confirmed_only_by_matching_file_in_scene(self):
        class Mesh:
            def __init__(self, path): self.path = path
            def getFileName(self): return self.path
        class Node:
            def __init__(self, path): self.mesh = Mesh(path)
            def getMeshData(self): return self.mesh
        bridge = SlicerBridge.__new__(SlicerBridge)
        bridge.active = {"before": set(), "path": os.path.normcase(os.path.abspath("test.stl")),
                         "started": __import__("time").monotonic()}
        results = []
        bridge._finish = lambda *args, **kwargs: results.append((args, kwargs))
        bridge._nodes = lambda: [Node("another.stl")]
        bridge._check_import()
        self.assertEqual(results, [])
        bridge._nodes = lambda: [Node("test.stl")]
        bridge._check_import()
        self.assertEqual(results[0][0], ("imported",))
        self.assertEqual(len(results[0][1]["object_ids"]), 1)

    def test_absent_printer_is_explicit(self):
        class App:
            def getGlobalContainerStack(self): return None
        self.assertFalse(read_context(App())["available"])


class CompatibilityTests(unittest.TestCase):
    def test_sources_parse_as_python35(self):
        # On modern Python this catches f-strings, variable annotations, async generators, etc.
        if sys.version_info < (3, 8):
            self.skipTest("feature_version requires modern Python")
        for root, directories, files in os.walk(os.path.join(ROOT, "plugin", "AIAssistant")):
            for name in files:
                if name.endswith(".py"):
                    with open(os.path.join(root, name), encoding="utf-8") as source:
                        ast.parse(source.read(), filename=name, feature_version=(3, 5))


if __name__ == "__main__":
    unittest.main()
