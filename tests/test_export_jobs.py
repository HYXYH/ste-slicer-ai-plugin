"""Native-shaped worker/Qt handshake regression; no actual slicer or print is claimed."""
import copy
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
import zipfile
from collections import deque
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "plugin"))
from AIAssistant.session import ApiError
from AIAssistant.slicer_api import SlicerAPI
from AIAssistant.slicer_bridge import SlicerBridge
from AIAssistant.agent.mcp_client import MCPClient

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PyQt5.QtCore import QTimer
    from PyQt5.QtWidgets import QApplication
    HAS_QT = True
except ImportError:
    HAS_QT = False


class Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, job):
        for slot in list(self.slots):
            slot(job)


@unittest.skipUnless(HAS_QT, "Install requirements-dev.txt for native-shaped Qt export checks")
class ExportJobTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication(["export-job-test"])
        cls.qt.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = self.temp.name
        self.addCleanup(self.temp.cleanup)
        self.qt_thread = threading.current_thread().ident
        self.calls, self.qt_callbacks, self.workers = [], deque(), []
        self.allow_finish = threading.Event()
        self.reference = {"process_id": "test-process", "fingerprint": "original"}
        self.behavior = "success"
        self.heartbeats = 0
        self.writer_calls = 0
        self.bridge = SlicerBridge.__new__(SlicerBridge)
        self.bridge.lock = threading.RLock()
        self.bridge.qt_thread, self.bridge.closed, self.bridge.active = self.qt_thread, False, None
        self.bridge.commands, self.bridge.callbacks = deque(), deque()
        self.bridge.store = types.SimpleNamespace()
        self.bridge.app = mock.Mock()
        self.bridge._nodes = lambda: []
        self.api = self.bridge.api = SlicerAPI(self.bridge)
        self.api.get_scene_snapshot = lambda: {"snapshot_ref": copy.deepcopy(self.reference)}
        self.api.exports["target"] = {"session_id": "sid", "path": self.root, "overwrite": False}
        self.api.dispatch = mock.Mock(side_effect=self.api.dispatch)
        harness = self

        class Writer:
            class OutputMode:
                BinaryMode, TextMode = 1, 2

            def on_qt(self, function):
                # STE 7.10 call_on_qt_thread always queues and waits, including
                # when its caller is already on Qt. This must run in a worker.
                harness.assertNotEqual(threading.current_thread().ident, harness.qt_thread)
                completed = threading.Event()
                result = {}
                def handle():
                    try:
                        harness.assertEqual(threading.current_thread().ident, harness.qt_thread)
                        result["value"] = function()
                    except Exception as error:
                        result["error"] = error
                    finally:
                        completed.set()
                with harness.bridge.lock:
                    harness.qt_callbacks.append(handle)
                if not completed.wait(3):
                    raise AssertionError("Writer deadlocked waiting for Qt")
                if "error" in result:
                    raise result["error"]
                return result.get("value")

            def write(self, stream, nodes, mode):
                harness.writer_calls += 1
                if not harness.allow_finish.wait(3):
                    raise AssertionError("Test did not release writer")
                if harness.behavior == "error":
                    raise OSError("simulated native write error")
                if harness.behavior == "false":
                    stream.write(b"partial" if mode == 1 else "partial")
                    return False
                if mode == 1:
                    def workspace():
                        if harness.behavior == "corrupt":
                            stream.write(b"not a zip")
                        else:
                            with zipfile.ZipFile(stream, "w") as archive:
                                archive.writestr("3D/3dmodel.model", "<model/>")
                                archive.writestr("SteSlicer/version.ini", "[versions]\nsteslicer_version=TEST\n")
                    self.on_qt(workspace)
                else:
                    preview = self.on_qt(lambda: ";PREVIEW:TEST ONLY\n")
                    stream.write(preview + ";LAYER:0\nG0 X1\nG1 X2 E0.1\n")
                return True

        self.writer = Writer()
        self.bridge.app.getWorkspaceFileHandler.return_value.getWriter.return_value = self.writer
        self.bridge.app.getMeshFileHandler.return_value.getWriter.return_value = self.writer

        class WriteFileJob:
            def __init__(self, writer, stream, nodes, mode):
                self.writer, self.stream, self.nodes, self.mode = writer, stream, nodes, mode
                self.finished, self.result = Signal(), False

            def setFileName(self, path):
                self.path = path

            def setAddToRecentFiles(self, value):
                harness.assertFalse(value)

            def start(self):
                harness.assertEqual(threading.current_thread().ident, harness.qt_thread)
                if harness.behavior == "start-error":
                    raise OSError("simulated job startup failure")
                worker = threading.Thread(target=self.run)
                worker.daemon = True
                harness.workers.append(worker)
                worker.start()

            def run(self):
                try:
                    self.result = self.writer.write(self.stream, self.nodes, self.mode)
                except Exception:
                    self.result = False
                self.finished.emit(self)

            def getResult(self):
                return self.result

        module = types.ModuleType("UM.FileHandler.WriteFileJob")
        module.WriteFileJob = WriteFileJob
        self.module_patch = mock.patch.dict(sys.modules, {"UM.FileHandler.WriteFileJob": module})
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)
        self.timer = QTimer()
        self.timer.setInterval(5)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.bridge.timer = mock.Mock()
        self.addCleanup(self.timer.stop)
        self.addCleanup(self.release_workers)

    def tick(self):
        self.heartbeats += 1
        self.bridge._commands()
        with self.bridge.lock:
            callback = self.qt_callbacks.popleft() if self.qt_callbacks else None
        if callback:
            callback()

    def pump(self, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.qt.processEvents()
            if predicate():
                return
            time.sleep(.001)
        self.fail("Qt export did not complete; heartbeat={}".format(self.heartbeats))

    def release_workers(self):
        self.allow_finish.set()
        if self.api.export_active:
            self.pump(lambda: self.api.export_active is None or self.bridge.closed)
        for worker in self.workers:
            worker.join(1)

    def start_call(self, kind="save_project", sid="sid"):
        observed = {}
        def request():
            try:
                observed["result"] = self.bridge.call(kind, {"session_id": sid, "export_target_id": "target"}, copy.deepcopy(self.reference))
            except ApiError as error:
                observed["error"] = error.code
            finally:
                observed["done"] = True
        worker = threading.Thread(target=request)
        worker.daemon = True
        self.workers.append(worker)
        worker.start()
        self.calls.append(observed)
        return observed

    def assert_no_temporary_files(self):
        self.assertFalse([name for name in os.listdir(self.root) if name.startswith(".assistant-")])

    def test_both_native_handshakes_finish_and_qt_keeps_processing(self):
        for kind, filename in (("save_project", "project.3mf"), ("export_gcode", "result.gcode")):
            self.allow_finish.clear()
            result = self.start_call(kind)
            self.pump(lambda: self.api.export_active is not None)
            before = self.heartbeats
            self.pump(lambda: self.heartbeats >= before + 6)
            self.assertFalse(result.get("done"))
            self.assertFalse(os.path.exists(os.path.join(self.root, filename)))
            self.allow_finish.set()
            self.pump(lambda: result.get("done"))
            self.assertNotIn("error", result)
            written = result["result"]["result"]
            self.assertTrue(written["written"])
            self.assertEqual(written["size"], os.path.getsize(os.path.join(self.root, filename)))
            self.assertEqual(result["result"]["snapshot_ref"], self.reference)
            self.assert_no_temporary_files()
        with zipfile.ZipFile(os.path.join(self.root, "project.3mf")) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn("SteSlicer/version.ini", archive.namelist())
        with open(os.path.join(self.root, "result.gcode"), encoding="utf-8") as source:
            self.assertIn("G1 X2 E0.1", source.read())

    def test_errors_and_invalid_zip_never_replace_an_existing_project(self):
        destination = os.path.join(self.root, "project.3mf")
        with open(destination, "wb") as target:
            target.write(b"original project")
        self.api.exports["target"]["overwrite"] = True
        for behavior in ("error", "false", "corrupt"):
            with self.subTest(behavior=behavior):
                self.behavior = behavior
                self.allow_finish.set()
                result = self.start_call()
                self.pump(lambda: result.get("done"))
                self.assertEqual(result["error"], "EXPORT_FAILED")
                with open(destination, "rb") as source:
                    self.assertEqual(source.read(), b"original project")
                self.assert_no_temporary_files()

    def test_target_ownership_collision_and_explicit_replacement(self):
        self.allow_finish.set()
        result = self.start_call(sid="other-session")
        self.pump(lambda: result.get("done"))
        self.assertEqual(result["error"], "EXPORT_FAILED")
        self.assertEqual(self.writer_calls, 0)
        for overwrite, expected in ((False, "EXPORT_EXISTS"), (True, None)):
            with open(os.path.join(self.root, "project.3mf"), "wb") as target:
                target.write(b"existing")
            self.api.exports["target"]["overwrite"] = overwrite
            result = self.start_call()
            self.pump(lambda: result.get("done"))
            self.assertEqual(result.get("error"), expected)
        self.assert_no_temporary_files()

    def test_scene_change_or_destination_race_discards_new_file(self):
        result = self.start_call()
        self.pump(lambda: self.api.export_active is not None)
        self.reference["fingerprint"] = "changed while writing"
        self.allow_finish.set()
        self.pump(lambda: result.get("done"))
        self.assertEqual(result["error"], "STALE_INPUT")
        self.assertFalse(os.path.exists(os.path.join(self.root, "project.3mf")))
        self.assert_no_temporary_files()
        self.allow_finish.clear()
        result = self.start_call()
        self.pump(lambda: self.api.export_active is not None)
        with open(os.path.join(self.root, "project.3mf"), "wb") as target:
            target.write(b"external file")
        self.allow_finish.set()
        self.pump(lambda: result.get("done"))
        self.assertEqual(result["error"], "EXPORT_EXISTS")
        with open(os.path.join(self.root, "project.3mf"), "rb") as source:
            self.assertEqual(source.read(), b"external file")
        self.assert_no_temporary_files()

    def test_export_timeout_cannot_publish_late_and_does_not_block_qt_commands(self):
        self.bridge.EXPORT_TIMEOUT = .15
        result = self.start_call()
        self.pump(lambda: self.api.export_active is not None)
        concurrent = self.start_call("export_gcode")
        self.pump(lambda: concurrent.get("done"))
        self.assertEqual(concurrent["error"], "BUSY")
        self.pump(lambda: result.get("done"))
        self.assertEqual(result["error"], "TOOL_TIMEOUT")
        self.assertGreater(self.heartbeats, 5)
        self.allow_finish.set()
        self.pump(lambda: self.api.export_active is None)
        self.assertFalse(os.path.exists(os.path.join(self.root, "project.3mf")))
        self.assert_no_temporary_files()

    def test_shutdown_releases_waiter_and_discards_late_file(self):
        result = self.start_call()
        self.pump(lambda: self.api.export_active is not None)
        task = self.api.export_active
        self.bridge.close()
        self.pump(lambda: result.get("done"))
        self.assertEqual(result["error"], "INTERRUPTED")
        self.allow_finish.set()
        self.pump(lambda: task.temporary and not os.path.exists(task.temporary))
        self.assertFalse(os.path.exists(os.path.join(self.root, "project.3mf")))

    def test_native_export_cannot_wait_on_its_own_qt_thread(self):
        with self.assertRaises(ApiError):
            self.bridge.call("save_project", {"session_id": "sid", "export_target_id": "target"})
        self.assertEqual(self.writer_calls, 0)

    def test_shutdown_after_verification_cleans_file_awaiting_publication(self):
        self.allow_finish.set()
        queued = threading.Event()
        def hold_completion(callback):
            queued.set()
            return True
        with mock.patch.object(self.bridge, "post", side_effect=hold_completion):
            result = self.start_call()
            self.pump(queued.is_set)
            task = self.api.export_active
            self.assertTrue(os.path.exists(task.temporary))
            self.bridge.close()
            self.pump(lambda: result.get("done"))
            self.assertEqual(result["error"], "INTERRUPTED")
            self.assertFalse(os.path.exists(task.temporary))
            self.assertFalse(os.path.exists(task.destination))

    def test_job_startup_failure_releases_reservation_and_removes_temporary(self):
        self.behavior = "start-error"
        result = self.start_call()
        self.pump(lambda: result.get("done"))
        self.assertEqual(result["error"], "EXPORT_FAILED")
        self.assertIsNone(self.api.export_active)
        self.assert_no_temporary_files()


class ExportMCPTimeoutTests(unittest.TestCase):
    def test_mcp_allows_export_bridge_deadline_and_keeps_normal_tool_deadline(self):
        server = types.SimpleNamespace(origin="http://127.0.0.1:1", token="TEST")
        client = MCPClient(server, "TEST")
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.headers = {}
        response.read.side_effect = lambda size: json.dumps({"id": client.sequence, "result": {}}).encode("utf-8")
        client.opener = mock.Mock()
        client.opener.open.return_value = response
        for name, deadline in (("save_project", 310), ("export_gcode", 310), ("get_scene_snapshot", 35)):
            client.request("tools/call", {"name": name})
            self.assertEqual(client.opener.open.call_args.kwargs["timeout"], deadline)


if __name__ == "__main__":
    unittest.main()
