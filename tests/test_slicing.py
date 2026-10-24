"""Real slicing adapter lifecycle with explicit backend signals and Qt queue stand-ins."""
import copy
import json
import os
import sys
import tempfile
import types
import unittest
from collections import deque
from enum import IntEnum
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "plugin"))
from AIAssistant.session import ApiError, SessionStore
from AIAssistant.slicer_api import SlicerAPI


class BackendState(IntEnum):
    NotStarted, Processing, Done, Error, Disabled = range(1, 6)


class Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def disconnect(self, slot):
        self.slots.remove(slot)

    def emit(self, value):
        for slot in list(self.slots):
            slot(value)


class Duration(int):
    valid = True


class SlicingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SessionStore(self.temp.name)
        self.sid = self.store.listing()["active_session_id"]
        self.store.edit(self.sid, lambda session: session.update(workflow_state="slicing"))
        self.snapshot_ref = {"process_id": self.store.run_id, "fingerprint": "initial"}
        self.callbacks = deque()
        self.failure_snapshots = []
        self.backend = types.SimpleNamespace(processingProgress=Signal(), backendStateChange=Signal(),
                                             forceSlice=mock.Mock())
        self.scene = types.SimpleNamespace(gcode_dict={0: ["; test G-code\nG0 X1\n"]})
        self.app = mock.Mock()
        self.app.getBackendManager.return_value.getCurrentBackend.return_value = self.backend
        self.app.getController.return_value.getScene.return_value = self.scene
        self.app.getMultiBuildPlateModel.return_value.activeBuildPlate = 0
        self.app.getPrintInformation.return_value = types.SimpleNamespace(
            printTimes=lambda: {"travel": Duration(12), "print": Duration(48)}, materialLengths=[1.25, 0.5])
        self.failure = mock.Mock(side_effect=self.capture_failure)
        self.bridge = types.SimpleNamespace(app=self.app, store=self.store, post=self.callbacks.append,
                                            on_slice_failure=self.failure)
        self.api = SlicerAPI(self.bridge)
        self.api.get_scene_snapshot = mock.Mock(side_effect=lambda: {"snapshot_ref": copy.deepcopy(self.snapshot_ref)})
        backend_module = types.ModuleType("UM.Backend.Backend")
        backend_module.BackendState = BackendState
        modules = {"UM": types.ModuleType("UM"), "UM.Backend": types.ModuleType("UM.Backend"),
                   "UM.Backend.Backend": backend_module}
        self.module_patch = mock.patch.dict(sys.modules, modules)
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)

    def persisted_session(self):
        with open(self.store.path, encoding="utf-8") as source:
            return json.load(source)["sessions"][self.sid]

    def capture_failure(self, sid, oid):
        self.failure_snapshots.append((sid, oid, self.store.get(sid), self.persisted_session()))

    def drain_callbacks(self):
        while self.callbacks:
            self.callbacks.popleft()()

    def start(self):
        return self.api.start_slicing(self.sid, "approved-plan")

    def assert_disconnected(self):
        self.assertIsNone(self.api.slice_active)
        self.assertEqual(self.backend.processingProgress.slots, [])
        self.assertEqual(self.backend.backendStateChange.slots, [])

    def finish(self, state):
        self.backend.backendStateChange.emit(state)
        self.drain_callbacks()

    def assert_failed_slice(self, state):
        result = self.start()
        oid = result["operation_id"]
        progress_slot = self.backend.processingProgress.slots[0]
        state_slot = self.backend.backendStateChange.slots[0]
        # Two terminal notifications can already be queued when the first is handled.
        self.backend.backendStateChange.emit(state)
        self.backend.backendStateChange.emit(state)
        self.failure.assert_not_called()
        self.drain_callbacks()
        operation = self.store.operation(self.sid, oid)
        self.assertEqual(operation["status"], "error")
        self.assertIsNone(operation["result"])
        self.assertTrue(operation["error"])
        self.failure.assert_called_once_with(self.sid, oid)
        self.assert_disconnected()
        _, _, current, persisted = self.failure_snapshots[0]
        for session in (current, persisted):
            self.assertEqual(session["operations"][oid]["status"], "error")
            self.assertEqual(session["workflow_state"], "failed")
            source = session["sources"][oid]
            self.assertEqual(source["tool"], "start_slicing")
            self.assertEqual(source["data"]["code"], "SLICE_FAILED")
        before = self.store.get(self.sid)
        progress_slot(0.99)
        state_slot(BackendState.Done)
        self.drain_callbacks()
        self.assertEqual(self.store.get(self.sid), before)
        self.failure.assert_called_once_with(self.sid, oid)

    def test_start_records_operation_and_connects_signals_before_force_slice(self):
        observed = []

        def force_slice():
            sid, oid, backend = self.api.slice_active
            operation = self.persisted_session()["operations"][oid]
            observed.append((sid, backend, operation))
            self.assertEqual(len(self.backend.processingProgress.slots), 1)
            self.assertEqual(len(self.backend.backendStateChange.slots), 1)

        self.backend.forceSlice.side_effect = force_slice
        result = self.start()
        self.backend.forceSlice.assert_called_once_with()
        self.assertEqual(result["status"], "slicing")
        self.assertEqual(result["snapshot_ref"], self.snapshot_ref)
        sid, backend, operation = observed[0]
        self.assertEqual(sid, self.sid)
        self.assertIs(backend, self.backend)
        self.assertEqual(operation["id"], result["operation_id"])
        self.assertEqual(operation["kind"], "slice")
        self.assertEqual(operation["plan_id"], "approved-plan")
        self.assertEqual(operation["status"], "slicing")
        self.assertIsNone(operation["progress"])
        self.assertEqual(operation["snapshot_ref"], self.snapshot_ref)
        self.failure.assert_not_called()

    def test_progress_updates_memory_only_when_posted_callback_runs(self):
        oid = self.start()["operation_id"]
        self.backend.processingProgress.emit(0.35)
        self.assertIsNone(self.store.operation(self.sid, oid)["progress"])
        self.assertEqual(len(self.callbacks), 1)
        self.drain_callbacks()
        self.assertIsNone(self.persisted_session()["operations"][oid]["progress"])
        operation = self.store.operation(self.sid, oid)
        self.assertEqual(operation["progress"], 0.35)
        self.assertEqual(operation["status"], "slicing")
        self.failure.assert_not_called()

    def test_progress_burst_queues_once_and_never_rewrites_history(self):
        oid = self.start()["operation_id"]
        revision = self.store.get(self.sid)["revision"]
        with mock.patch.object(self.store, "_save", wraps=self.store._save) as save:
            for amount in range(1000):
                self.backend.processingProgress.emit(amount / 1000.)
            self.assertEqual(len(self.callbacks), 1)
            self.drain_callbacks()
            self.assertEqual(self.store.operation(self.sid, oid)["progress"], .999)
            self.assertEqual(self.store.get(self.sid)["revision"], revision)
            save.assert_not_called()
            self.finish(BackendState.Done)
        self.assertEqual(self.persisted_session()["operations"][oid]["status"], "completed")
        self.assertEqual(self.persisted_session()["operations"][oid]["progress"], .999)

    def test_done_with_gcode_saves_metrics_and_disconnects_signals(self):
        oid = self.start()["operation_id"]
        self.backend.backendStateChange.emit(BackendState.Done)
        self.backend.backendStateChange.emit(BackendState.Done)
        self.drain_callbacks()
        session = self.persisted_session()
        operation = session["operations"][oid]
        self.assertEqual(operation["status"], "completed")
        self.assertIsNone(operation["error"])
        self.assertEqual(operation["result"], {"time": {"value": 60, "unit": "s"},
                                               "material": {"values": [1.25, 0.5], "unit": "m"}})
        self.assertEqual(session["workflow_state"], "result_ready")
        self.assertEqual(session["events"][-1]["operation_id"], oid)
        self.assertEqual(len([event for event in session["events"] if event.get("operation_id") == oid]), 1)
        self.assert_disconnected()
        self.failure.assert_not_called()

    def test_nonterminal_states_keep_operation_active_until_done(self):
        oid = self.start()["operation_id"]
        before = self.store.get(self.sid)
        self.backend.backendStateChange.emit(BackendState.NotStarted)
        self.backend.backendStateChange.emit(BackendState.Processing)
        self.assertEqual(len(self.callbacks), 0)
        self.drain_callbacks()
        self.assertEqual(self.store.get(self.sid), before)
        self.assertEqual(self.api.slice_active[1], oid)
        self.failure.assert_not_called()
        self.finish(BackendState.Done)
        self.assertEqual(self.store.operation(self.sid, oid)["status"], "completed")
        self.assert_disconnected()

    def test_error_records_failure_before_requesting_reanalysis_once(self):
        self.assert_failed_slice(BackendState.Error)

    def test_reset_after_processing_finishes_interrupted_operation(self):
        oid = self.start()["operation_id"]
        self.backend.backendStateChange.emit(BackendState.Processing)
        self.backend.backendStateChange.emit(BackendState.NotStarted)
        self.drain_callbacks()
        self.assertEqual(self.store.operation(self.sid, oid)["status"], "error")
        self.assertEqual(self.persisted_session()["workflow_state"], "failed")
        self.assert_disconnected()
        self.failure.assert_called_once_with(self.sid, oid)

    def defer_until_validation(self):
        checker = types.SimpleNamespace(needToWaitForResult=True)
        self.app.getMachineErrorChecker.return_value = checker
        delayed = deque()
        qt = types.ModuleType("PyQt5.QtCore")
        qt.QTimer = types.SimpleNamespace(singleShot=lambda milliseconds, callback: delayed.append(callback))
        patch = mock.patch.dict(sys.modules, {"PyQt5": types.ModuleType("PyQt5"), "PyQt5.QtCore": qt})
        patch.start()
        self.addCleanup(patch.stop)
        return checker, delayed

    def test_pending_validation_defers_engine_and_ignores_earlier_states(self):
        checker, delayed = self.defer_until_validation()
        oid = self.start()["operation_id"]
        self.backend.forceSlice.assert_not_called()
        self.backend.backendStateChange.emit(BackendState.Done)
        self.backend.backendStateChange.emit(BackendState.NotStarted)
        self.assertFalse(self.callbacks)
        delayed.popleft()()
        self.backend.forceSlice.assert_not_called()
        checker.needToWaitForResult = False
        delayed.popleft()()
        self.backend.forceSlice.assert_called_once_with()
        self.finish(BackendState.Done)
        self.assertEqual(self.store.operation(self.sid, oid)["status"], "completed")

    def test_validation_timeout_ends_operation_without_launching_engine(self):
        checker, delayed = self.defer_until_validation()
        with mock.patch("AIAssistant.slicer_api.time.monotonic", return_value=0):
            oid = self.start()["operation_id"]
        with mock.patch("AIAssistant.slicer_api.time.monotonic", return_value=31):
            delayed.popleft()()
        operation = self.store.operation(self.sid, oid)
        self.assertEqual(operation["status"], "error")
        self.assertIn("30 секунд", operation["error"])
        self.backend.forceSlice.assert_not_called()
        self.assert_disconnected()

    def test_changed_scene_during_validation_requires_new_approval(self):
        checker, delayed = self.defer_until_validation()
        oid = self.start()["operation_id"]
        self.snapshot_ref["fingerprint"] = "changed during validation"
        checker.needToWaitForResult = False
        delayed.popleft()()
        self.assertEqual(self.store.operation(self.sid, oid)["status"], "stale")
        self.backend.forceSlice.assert_not_called()
        self.failure.assert_not_called()
        self.assert_disconnected()

    def test_deferred_launch_failure_is_saved_instead_of_escaping_qt_callback(self):
        checker, delayed = self.defer_until_validation()
        oid = self.start()["operation_id"]
        checker.needToWaitForResult = False
        self.backend.forceSlice.side_effect = RuntimeError("engine unavailable")
        delayed.popleft()()
        self.assertEqual(self.store.operation(self.sid, oid)["status"], "error")
        self.failure.assert_called_once_with(self.sid, oid)
        self.assert_disconnected()

    def test_disabled_records_failure_before_requesting_reanalysis_once(self):
        self.assert_failed_slice(BackendState.Disabled)

    def test_done_without_gcode_records_failure_before_requesting_reanalysis_once(self):
        self.scene.gcode_dict = {}
        self.assert_failed_slice(BackendState.Done)

    def test_stale_scene_records_stale_without_requesting_reanalysis(self):
        oid = self.start()["operation_id"]
        self.snapshot_ref["fingerprint"] = "scene changed while slicing"
        self.finish(BackendState.Error)
        session = self.persisted_session()
        self.assertEqual(session["operations"][oid]["status"], "stale")
        self.assertEqual(session["workflow_state"], "stale")
        self.assertNotIn(oid, session["sources"])
        self.assert_disconnected()
        self.failure.assert_not_called()

    def test_late_callbacks_from_previous_slice_cannot_change_current_slice(self):
        first_oid = self.start()["operation_id"]
        old_progress = self.backend.processingProgress.slots[0]
        old_state = self.backend.backendStateChange.slots[0]
        self.finish(BackendState.Done)
        second_oid = self.start()["operation_id"]
        self.assertNotEqual(first_oid, second_oid)
        before = self.store.get(self.sid)
        old_progress(0.95)
        old_state(BackendState.Error)
        self.drain_callbacks()
        self.assertEqual(self.store.get(self.sid), before)
        self.assertEqual(self.api.slice_active[1], second_oid)
        self.finish(BackendState.Done)
        self.assertEqual(self.store.operation(self.sid, second_oid)["status"], "completed")
        self.failure.assert_not_called()

    def test_missing_backend_rejects_start_without_creating_operation(self):
        self.app.getBackendManager.return_value.getCurrentBackend.return_value = None
        with self.assertRaises(ApiError) as caught:
            self.start()
        self.assertEqual(caught.exception.code, "SLICE_FAILED")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.store.get(self.sid)["operations"], {})
        self.backend.forceSlice.assert_not_called()
        self.assert_disconnected()
        self.failure.assert_not_called()

    def test_busy_rejects_second_start_without_creating_another_operation(self):
        oid = self.start()["operation_id"]
        with self.assertRaises(ApiError) as caught:
            self.start()
        self.assertEqual(caught.exception.code, "SLICE_FAILED")
        self.assertEqual(set(self.store.get(self.sid)["operations"]), {oid})
        self.backend.forceSlice.assert_called_once_with()
        self.assertEqual(self.api.slice_active[1], oid)

    def test_force_slice_exception_cleans_up_and_persists_launch_failure(self):
        error = RuntimeError("Engine failed to launch")

        def fail_launch():
            self.backend.processingProgress.emit(0.1)
            self.backend.backendStateChange.emit(BackendState.Error)
            raise error

        self.backend.forceSlice.side_effect = fail_launch
        with self.assertRaises(RuntimeError) as caught:
            self.start()
        self.assertIs(caught.exception, error)
        session = self.persisted_session()
        self.assertEqual(len(session["operations"]), 1)
        operation = next(iter(session["operations"].values()))
        self.assertEqual(operation["status"], "error")
        self.assertTrue(operation["error"])
        self.assertEqual(session["workflow_state"], "failed")
        self.assertEqual(session["sources"], {})
        self.assert_disconnected()
        before = self.store.get(self.sid)
        self.drain_callbacks()
        self.assertEqual(self.store.get(self.sid), before)
        self.failure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
