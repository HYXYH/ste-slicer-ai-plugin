"""Regression checks for GUI starvation and large conversation histories."""
import copy
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from collections import deque
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "plugin"))
from AIAssistant.session import ApiError, SessionStore, workspace_view
from AIAssistant.server import Handler
from AIAssistant.slicer_api import SlicerAPI
from AIAssistant.slicer_bridge import SlicerBridge


class PerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SessionStore(self.temp.name)
        self.sid = self.store.listing()["active_session_id"]

    def test_workspace_excludes_large_evidence_without_mutating_or_losing_it(self):
        def populate(session):
            session["sources"]["large"] = {"data": "x" * 1000000}
            session["task_map"]["resources"] = {"catalog": "y" * 1000000}
            session["files"]["file"] = {"id": "file", "name": "cube.stl", "path": "private-path", "object_ids": ["node"]}
            session["events"].append({"id": "event", "seq": 1, "session_id": self.sid, "run_id": None, "created_at": "2026", "type": "tool_finished", "tool": "list_materials", "result": {"data": "z" * 1000000}})
        self.store.edit(self.sid, populate)
        original = self.store.internal(self.sid)
        projected = self.store.get(self.sid, compact=True)
        self.assertLess(len(json.dumps(projected)), 10000)
        self.assertNotIn("sources", projected)
        self.assertNotIn("resources", projected["task_map"])
        self.assertNotIn("path", projected["files"]["file"])
        self.assertTrue(projected["events"][0]["has_details"])
        self.assertEqual(self.store.event_detail(self.sid, "event")["result"]["data"], "z" * 1000000)
        projected["files"]["file"]["name"] = "changed"
        self.assertEqual(self.store.internal(self.sid), original)
        self.assertIn("sources", self.store.get(self.sid))

    def test_workspace_routes_select_and_rename_without_large_source_payloads(self):
        handler = types.SimpleNamespace(server=types.SimpleNamespace(store=self.store), _json=lambda: {})
        value, status = Handler._workflow(handler, "GET", "/api/sessions/" + self.sid + "/workspace")
        self.assertEqual(status, 200)
        self.assertEqual(value["id"], self.sid)
        self.assertNotIn("sources", value)
        second = self.store.create("Second")["id"]
        Handler._workflow(handler, "POST", "/api/sessions/" + self.sid + "/workspace")
        self.assertEqual(self.store.listing()["active_session_id"], self.sid)
        handler._json = lambda: {"title": "Renamed"}
        value, status = Handler._workflow(handler, "PATCH", "/api/sessions/" + second + "/workspace")
        self.assertEqual(value["title"], "Renamed")
        event = self.store.event(self.sid, None, "progress", message="ready")
        value, status = Handler._workflow(handler, "GET", "/api/sessions/" + self.sid + "/events/" + event["id"])
        self.assertEqual(value, event)
        with self.assertRaises(ApiError):
            self.store.event_detail(self.sid, "missing")

    def test_callback_burst_yields_to_qt_and_does_not_starve_commands(self):
        bridge = SlicerBridge.__new__(SlicerBridge)
        bridge.lock = threading.RLock()
        completed = []
        bridge.callbacks = deque(lambda i=i: completed.append(i) for i in range(100))
        command = {"name": "read", "arguments": {}, "expected": None, "abandoned": False, "done": threading.Event()}
        bridge.commands = deque([command])
        bridge.api = types.SimpleNamespace(dispatch=mock.Mock(return_value={"ok": True}))
        bridge._commands()
        self.assertGreater(len(completed), 0)
        self.assertLessEqual(len(completed), 4)
        self.assertTrue(command["done"].is_set())
        self.assertEqual(command["result"], {"ok": True})
        while bridge.callbacks:
            bridge._commands()
        self.assertEqual(completed, list(range(100)))

    def test_passive_snapshot_scan_is_skipped_while_slicing(self):
        bridge = SlicerBridge.__new__(SlicerBridge)
        bridge.lock, bridge.ticks = threading.RLock(), 40
        bridge.app = types.SimpleNamespace(getGlobalContainerStack=lambda: object())
        bridge._nodes = lambda: []
        bridge.store = mock.Mock()
        bridge.api = types.SimpleNamespace(slice_active=("sid", "oid", object()), get_scene_snapshot=mock.Mock(return_value={"snapshot_ref": {"fingerprint": "fresh"}}))
        with mock.patch("AIAssistant.slicer_bridge.read_context", return_value={}):
            bridge._refresh()
            bridge.api.get_scene_snapshot.assert_not_called()
            bridge.store.invalidate_snapshot.assert_not_called()
            bridge.api.slice_active = None
            bridge._refresh()
        bridge.store.invalidate_snapshot.assert_called_once_with({"fingerprint": "fresh"})

    def test_chat_file_associations_do_not_change_physical_scene_fingerprint(self):
        vector = types.SimpleNamespace(x=1., y=1., z=1.)
        mesh = types.SimpleNamespace(getVertices=lambda: types.SimpleNamespace(tobytes=lambda: b"vertices"), getIndices=lambda: None)
        node = types.SimpleNamespace(getMeshData=lambda: mesh, getBoundingBox=lambda: types.SimpleNamespace(width=20., height=20., depth=20.),
                                     getOrientation=lambda: types.SimpleNamespace(x=0., y=0., z=0., w=1.), getPosition=lambda: vector, getScale=lambda: vector,
                                     getWorldTransformation=lambda: types.SimpleNamespace(getData=lambda: [1., 0., 0., 1.]),
                                     getName=lambda: "cube", callDecoration=lambda key: None)
        values = {"layer_height": .2}
        stack = types.SimpleNamespace(extruders={}, getId=lambda: "machine", getAllKeys=lambda: values.keys(), getProperty=lambda key, kind: values[key])
        app = types.SimpleNamespace(getMultiBuildPlateModel=lambda: types.SimpleNamespace(activeBuildPlate=0))
        api = SlicerAPI(types.SimpleNamespace(app=app, store=self.store))
        api.stack, api.nodes = lambda target=None: stack, lambda: {"node": node}
        with mock.patch("AIAssistant.slicer_bridge.read_context", return_value={"updated_at": "ignored", "mode": "3D"}), mock.patch.object(self.store, "get", side_effect=AssertionError("Full history must not be copied for a file lookup")):
            before = api.get_scene_snapshot()
            self.store.edit(self.sid, lambda session: session["files"].update({"copy": {"id": "copy", "object_ids": ["node"]}}))
            after = api.get_scene_snapshot()
            self.assertEqual(before["snapshot_ref"], after["snapshot_ref"])
            self.assertNotEqual(before["objects"][0]["file_refs"], after["objects"][0]["file_refs"])
            values["layer_height"] = .3
            self.assertNotEqual(after["snapshot_ref"], api.get_scene_snapshot()["snapshot_ref"])


if __name__ == "__main__":
    unittest.main()
