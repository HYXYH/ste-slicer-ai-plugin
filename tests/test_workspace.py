"""Workspace changes preserve provenance and use measured evidence, not LLM confidence."""
import copy
import io
import json
import unittest

import test_workflow as workflow
from test_stage1 import STL
from AIAssistant.session import ApiError
from AIAssistant.uploads import receive
from unittest import mock


class WorkspaceTests(unittest.TestCase):
    setUp = workflow.WorkflowTests.setUp
    tearDown = workflow.WorkflowTests.tearDown
    api = workflow.WorkflowTests.api

    def process_bridge(self):
        self.process_signature = "process-1"
        call = self.bridge.call
        def read(name, args, expected_ref=None):
            if name == "get_calibration_context":
                return {"signature": self.process_signature, "context": self.bridge.snapshot(), "profile_id": "quality-1"}
            return call(name, args, expected_ref)
        self.bridge.call = read

    def test_merge_copies_history_and_files_but_never_plans_approvals_or_runs(self):
        f = receive(self.store, self.sid, "original.stl", io.BytesIO(STL), len(STL))
        self.store.message(self.sid, "m1", "Деталь А", [f["id"]], {})
        self.store.finish(self.sid, next(iter(self.store.get(self.sid)["operations"])), "imported", object_ids=["node-1"])
        self.store.edit(self.sid, lambda s: s["plans"].update({"old": {"status": "applied", "approval": {"yes": True}}}))
        second = self.store.create("Второй")
        self.store.message(second["id"], "m2", "Дополнительные условия", [], {})
        original = [self.store.internal(sid) for sid in (self.sid, second["id"])]
        status, merged, _ = self.api("POST", "/api/sessions/merge", {"session_ids": [self.sid, second["id"]], "title": "Общий чат", "request_id": "merge1"})
        self.assertEqual(status, 201)
        self.assertEqual(merged["plans"], {})
        self.assertEqual(merged["runs"], {})
        self.assertEqual(merged["task_map"]["readiness"]["status"], "needs_data")
        self.assertEqual([self.store.internal(sid) for sid in (self.sid, second["id"])], original)
        cloned = next(iter(self.store.internal(merged["id"])["files"].values()))
        self.assertNotEqual(cloned["id"], f["id"])
        self.assertEqual(cloned["origin"]["file_id"], f["id"])
        with open(cloned["path"], "rb") as source:
            self.assertEqual(source.read(), STL)
        self.assertNotIn("path", json.dumps(merged))
        duplicate = self.api("POST", "/api/sessions/merge", {"session_ids": [self.sid, second["id"]], "title": "Общий чат", "request_id": "merge1"})[1]
        self.assertEqual(duplicate["id"], merged["id"])
        self.store.rename(merged["id"], "Переименованный общий чат")
        self.assertEqual(self.server.workspace.merge([self.sid, second["id"]], "Общий чат", "merge1")["id"], merged["id"])

    def test_score_is_deterministic_ignores_llm_claims_and_threshold_does_not_authorize_plan(self):
        self.process_bridge()
        first = self.server.workspace.assessment(self.sid)
        self.store.edit(self.sid, lambda s: s["messages"].append({"id": "fake", "role": "assistant", "text": "Уверенность 99%", "file_ids": []}))
        second = self.server.workspace.assessment(self.sid)
        self.assertEqual({k: v for k, v in first.items() if k != "session_revision"}, {k: v for k, v in second.items() if k != "session_revision"})
        self.assertFalse(first["recommend_calibration"])
        self.assertEqual(sum(c["score"] for c in first["components"]), first["score"])
        self.server.workspace.save_answers(self.sid, {"load": "не применяется", "temperature": "20 °C", "environment": "помещение"})
        self.assertEqual(self.api("PATCH", "/api/sessions/" + self.sid + "/assessment", {"threshold": 1})[0], 400)
        result = self.store.get(self.sid)
        self.assertEqual(result["plans"], {})
        self.assertIn("Условия эксплуатации", result["task"]["description"])
        self.assertEqual(self.bridge.mutations, [])

    def test_cube_is_real_20mm_mesh_and_measurements_require_matching_process_and_slice(self):
        self.process_bridge()
        child = self.server.workspace.create_calibration(self.sid, "cube1")
        self.assertEqual(self.server.workspace.create_calibration(self.sid, "cube1")["id"], child["id"])
        attachment = next(iter(self.store.internal(child["id"])["files"].values()))
        with open(attachment["path"], encoding="ascii") as source:
            vertices = [tuple(float(v) for v in line.strip().split()[1:]) for line in source if line.strip().startswith("vertex ")]
        self.assertEqual(len(vertices), 36)
        self.assertEqual([max(v[i] for v in vertices) - min(v[i] for v in vertices) for i in range(3)], [20, 20, 20])
        body = {"x_mm":20.,"y_mm":20.,"z_mm":20.,"adhesion_ok":True,"layers_ok":True,"surface_ok":True,"printed":True,"process_signature":"process-1"}
        with self.assertRaises(ApiError):
            self.server.workspace.record_calibration(child["id"], body)
        self.store.edit(child["id"], lambda s: (s["operations"].update({"slice":{"kind":"slice","status":"completed","plan_id":"plan"}}), s["plans"].update({"plan":{"status":"applied"}})))
        before = self.server.workspace.assessment(self.sid)["score"]
        self.server.workspace.record_calibration(child["id"], body)
        a = self.server.workspace.assessment(self.sid)
        self.assertEqual(a["score"], before)
        self.assertEqual(a["calibration"]["x_mm"], 20.)
        self.assertNotIn("calibration", [c["id"] for c in a["components"]])
        self.process_signature = "different-settings"
        b = self.server.workspace.assessment(self.sid)
        self.assertIsNone(b["calibration"])
        self.assertEqual(b["score"], before)
        with self.assertRaises(ApiError) as caught:
            self.server.workspace.record_calibration(child["id"], body)
        self.assertEqual(caught.exception.code, "STALE_INPUT")

    def test_busy_merge_and_nonfinite_measurements_are_rejected(self):
        self.process_bridge()
        second = self.store.create()
        self.store.edit(self.sid, lambda s: s.update(workflow_state="slicing"))
        with self.assertRaises(ApiError):
            self.server.workspace.merge([self.sid, second["id"]], "Тест", "busy")
        child = self.server.workspace.create_calibration(second["id"], "c2")
        self.store.edit(child["id"], lambda s: (s["operations"].update({"slice":{"kind":"slice","status":"completed","plan_id":"p"}}), s["plans"].update({"p":{"status":"applied"}})))
        with self.assertRaises(ApiError):
            self.server.workspace.record_calibration(child["id"], {"x_mm":float("nan"),"y_mm":20.,"z_mm":20.,"adhesion_ok":True,"layers_ok":True,"surface_ok":True,"printed":True,"process_signature":"process-1"})

    def test_failed_cube_generates_targeted_questions_and_answers_do_not_fake_a_pass(self):
        self.process_bridge()
        child = self.server.workspace.create_calibration(self.sid, "failed-cube")
        self.store.edit(child["id"], lambda s: (s["operations"].update({"slice":{"kind":"slice","status":"completed","plan_id":"p"}}), s["plans"].update({"p":{"status":"applied"}})))
        self.server.workspace.record_calibration(child["id"], {"x_mm":20.8,"y_mm":20.,"z_mm":20.,"adhesion_ok":False,"layers_ok":True,"surface_ok":False,"printed":True,"process_signature":"process-1"})
        a = self.server.workspace.assessment(self.sid)
        fields = [q["field"] for q in a["calibration_questions"]]
        self.assertIn("x_mm", a["failed_checks"])
        self.assertTrue(a["recommend_calibration"])
        self.assertIn("dimensions_check", fields)
        self.assertIn("adhesion_context", fields)
        self.assertIn("surface_context", fields)
        self.assertNotIn("layer_context", fields)
        before = a["score"]
        b = self.server.workspace.save_answers(self.sid, {"adhesion_context":"Отрыв по углам"})
        self.assertEqual(b["score"], before)
        self.assertEqual(b["failed_checks"], a["failed_checks"])
        self.assertNotIn("adhesion_context", [q["field"] for q in b["calibration_questions"]])

    def test_failed_file_copy_does_not_publish_or_erase_a_chat(self):
        receive(self.store, self.sid, "original.stl", io.BytesIO(STL), len(STL))
        second = self.store.create("Второй")
        before = self.store.listing()
        with mock.patch("AIAssistant.workspace.shutil.copyfile", side_effect=OSError("test disk failure")):
            with self.assertRaises(OSError):
                self.server.workspace.merge([self.sid, second["id"]], "Общий", "broken-copy")
        self.assertEqual(self.store.listing(), before)

    def test_return_from_cube_reimport_replaces_only_this_models_old_ids(self):
        from AIAssistant.session import SessionStore
        f = receive(self.store, self.sid, "part.stl", io.BytesIO(STL), len(STL))
        result, operations = self.store.message(self.sid, "first", "Основная деталь", [f["id"]], {})
        self.store.finish(self.sid, operations[0]["id"], "imported", object_ids=["old-model"])
        self.store.edit(self.sid, lambda s: s["task_map"].update(target_object_ids=["old-model","separate-model"]))
        self.store.reconcile_scene(set())
        result, operations = self.store.message(self.sid, "reload", "", [f["id"]], {})
        self.store.finish(self.sid, operations[0]["id"], "imported", object_ids=["reloaded-model"])
        self.assertEqual(set(self.store.get(self.sid)["task_map"]["target_object_ids"]), {"reloaded-model","separate-model"})
        restarted = SessionStore(self.temp.name)
        self.assertEqual(restarted.get(self.sid)["task_map"]["target_object_ids"], [])
