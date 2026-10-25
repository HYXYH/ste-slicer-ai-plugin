"""Deterministic evidence, actual local HTTP/photo payload and optional calibration lifecycle."""
import copy
import hashlib
import json
import os
import tempfile
import unittest
from unittest import mock

import test_workflow as workflow
import test_print_experience as experience_tests
import test_knowledge_base as knowledge_tests
from test_stage1 import PNG
from AIAssistant.calibration_assessment import assess, catalog, checked, load_policy, similarity, posterior
from AIAssistant.experience import ExperienceStore, conditions
from AIAssistant.session import ApiError, SessionStore, uid, now
from AIAssistant.agent.steps import Steps
from AIAssistant.agent.llm_client import image_content, authorized_images, recipient, vision_unsupported
from AIAssistant.agent.runner import text_only
from AIAssistant.uploads import receive
import io


class AssessmentTests(unittest.TestCase):
    def setUp(self):
        knowledge_tests.KnowledgeBaseTests.setUp(self)
        self.snapshot["settings"][0]["values"].update(material_bed_temperature=60, speed_layer_0=20)
    printed = knowledge_tests.KnowledgeBaseTests.printed

    def decision(self, text="Декоративная деталь", costs=None):
        return assess(self.snapshot, self.db.library(limit=None), text, load_policy(), costs)

    def record(self, status="pass", feature="adhesion", operation=None, snapshot=None):
        data = {"result": "good" if status == "pass" else "defects", "defects": [], "reported_defects": [], "checks": {feature: status}, "photos": [], "measurements": {}, "answers": {}, "text": ""}
        return self.db.record("chat", {"id": operation or uid(), "print_snapshot": snapshot or self.snapshot}, data, uid())

    def test_cold_start_only_when_useful_and_six_risk_groups(self):
        result = self.decision()
        self.assertFalse(result["offer"])
        self.assertEqual(len(result["groups"]), 6)
        self.assertTrue(all(f["mean"] is None for f in result["features"]))
        self.snapshot["objects"][0]["dimensions_mm"] = [140, 80, 70]
        self.assertTrue(self.decision()["offer"])
        self.assertFalse(self.decision(costs={"main_loss": 1, "test_cost": 2})["offer"])
        self.assertIsNone(self.decision()["utility"])

    def test_checked_features_only_and_independent_weight_cap(self):
        for _ in range(7):
            self.record(operation="one-slice")
        result = self.decision("Мосты и ретракт")
        features = {r["id"]: r for r in result["features"]}
        self.assertEqual(features["adhesion"]["effective_trials"], 1)
        self.assertEqual(features["bridges"]["effective_trials"], 0)
        self.assertIsNone(features["adhesion"]["mean"])
        self.printed("good")
        self.assertEqual({r["id"]: r for r in self.decision()["features"]}["bridges"]["effective_trials"], 0)

    def test_sufficient_experience_suppresses_and_failed_test_changes_decision(self):
        for _ in range(3):
            self.record(feature="adhesion")
            self.record(feature="extrusion")
        result = self.decision()
        self.assertFalse(result["offer"])
        self.assertEqual(result["unchecked"], [])
        self.record("fail")
        self.assertTrue(self.decision()["offer"])

    def test_hardware_isolation_geometry_discount_and_exclusions(self):
        old = self.record()
        policy = load_policy()
        altered = copy.deepcopy(self.snapshot)
        altered["objects"][0]["geometry_revision"] = "other-stl"
        self.assertEqual(similarity(self.snapshot, altered, "adhesion", policy), 1)
        altered["objects"][0]["dimensions_mm"] = [120, 30, 10]
        self.assertEqual(similarity(self.snapshot, altered, "adhesion", policy), .5)
        for section, field, value in [("printer", "id", "p2"), ("mode", "id", "cylindrical")]:
            altered = copy.deepcopy(self.snapshot)
            altered["context"][section][field] = value
            self.assertEqual(similarity(self.snapshot, altered, "adhesion", policy), 0)
        for field, value in [("nozzle_mm", .6), ("material", {"id": "ABS"})]:
            altered = copy.deepcopy(self.snapshot)
            altered["context"]["extruders"][0][field] = value
            self.assertEqual(similarity(self.snapshot, altered, "adhesion", policy), 0)
        self.db.revise_observation(old["observations"][0]["id"], old["observations"][0]["data"], True, "Ошибка")
        self.assertEqual(next(f for f in self.decision()["features"] if f["id"] == "adhesion")["effective_trials"], 0)

    def test_specialized_selection_universal_and_no_5d_or_temperature_tower(self):
        self.record("fail", "stringing")
        result = self.decision("Ретракт и паутинка")
        self.assertEqual(result["test"]["id"], "stringing")
        universal = self.decision("Точность, ретракт, мосты, нависания и мелкие элементы")
        self.assertEqual(universal["test"]["id"], "universal")
        self.snapshot["context"]["mode"]["id"] = "cylindrical"
        self.assertFalse(self.decision()["offer"])
        self.assertIsNone(self.decision()["test"])
        for row in catalog():
            path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "plugin", "AIAssistant", "models", row["file"])
            with open(path, "rb") as source:
                self.assertEqual(hashlib.sha256(source.read()).hexdigest(), row["sha256"])
            self.assertNotIn("bonding", row["checks"])
            self.assertNotIn("tower", row["file"].lower())

    def test_beta_formula_fractional_counts_and_policy_overrides(self):
        result = posterior([("one", True, .5, "p1"), ("one", False, .5, "p2"), ("two", True, 1, "p3"), ("three", True, 1, "p4")], load_policy())
        self.assertEqual(result["alpha"], 3.5)
        self.assertEqual(result["beta"], 1.5)
        self.assertEqual(result["mean"], .7)
        path = os.path.join(self.temp.name, "calibration-policy.json")
        with open(path, "w") as target:
            json.dump({"minimum_effective_trials": 1}, target)
        self.assertEqual(load_policy(self.temp.name)["minimum_effective_trials"], 1)
        with open(path, "w") as target:
            json.dump({"geometry_transfer_weight": 2}, target)
        with self.assertRaises(ApiError):
            load_policy(self.temp.name)

    def test_utility_requires_all_empirical_terms_and_real_costs(self):
        for i in range(3):
            control = self.record("fail", "stringing")
            data = {"result": "defects", "defects": ["stringing"], "checks": {"stringing": "fail"}, "text": "", "photos": [], "answers": {}, "measurements": {},
                    "calibration": {"test_id": "stringing", "control_print_ids": [control["id"]]}}
            test = self.db.record("test-chat", {"id": uid(), "print_snapshot": self.snapshot}, data, uid())
            changed = copy.deepcopy(self.snapshot)
            changed["settings"][0]["values"]["material_print_temperature"] = 205
            repeat = self.record("pass", "stringing", snapshot=changed)
            trial = self.db.propose(test["id"], test["observations"][0]["id"], {"defect": "stringing", "key": "material_print_temperature", "choice": "0", "target": self.snapshot["settings"][0]["target"],
                "before": 210, "after": 205, "reason": "Снизить температуру", "state": "proposed", "snapshot_ref": {}})
            self.db.update_adjustment(trial["id"], {"outcome": "resolved", "repeat_print_id": repeat["id"], "applied_snapshot": changed, "state": "evaluated"}, "observation")
        result = self.decision("Ретракт", {"main_loss": 100, "test_cost": 1})
        self.assertEqual(result["method"], "utility")
        self.assertGreater(result["utility"], 0)
        self.assertTrue(result["offer"])
        self.assertIsNone(self.decision("Ретракт")["utility"])


class LifecycleTests(unittest.TestCase):
    setUp = experience_tests.PrintExperienceTests.setUp
    tearDown = workflow.WorkflowTests.tearDown
    snapshot = experience_tests.PrintExperienceTests.snapshot
    add_operation = experience_tests.PrintExperienceTests.add_operation
    api = workflow.WorkflowTests.api

    def setup_plan(self):
        self.bridge.objects[0]["dimensions_mm"] = [150, 80, 70]
        plan = {"id": uid(), "status": "validated", "created_at": now(), "snapshot_ref": self.snapshot()["snapshot_ref"], "input_revision": 0,
                "selected": {"mode": "classic", "material_id": "proto_pla", "profile_id": "quality-1", "extruder_id": "extruder-1"}, "actions": [], "limitations": []}
        self.store.edit(self.sid, lambda s: s["plans"].update({plan["id"]: plan}))
        self.server.calibration.consider(self.sid, plan)
        decision = self.server.calibration.state(self.sid)["decision"]
        self.assertTrue(decision["offer"])
        return decision

    def started(self):
        decision = self.setup_plan()
        def submit(operations):
            for op in operations:
                dims = self.store.internal(self.sid)["intelligent_calibration"]["test"]["dimensions_mm"]
                self.bridge.objects.append({"id": "test-node", "name": "Calibration test", "dimensions_mm": dims, "printable": True, "file_refs": []})
                self.store.finish(op["session_id"], op["id"], "imported", object_ids=["test-node"])
        self.bridge.submit = submit
        state = self.server.calibration.start(self.sid, decision["id"])
        return decision, state

    def test_offer_skip_is_persistent_and_does_not_change_slicer(self):
        decision = self.setup_plan()
        self.server.calibration.command(self.sid, "skip", {"decision_id": decision["id"]})
        self.assertEqual(self.bridge.mutations, [])
        reopened = SessionStore(self.store.root)
        self.assertEqual(reopened.internal(self.sid)["calibration_dismissed"], decision["id"])
        plan = next(iter(self.store.internal(self.sid)["plans"].values()))
        self.server.calibration.consider(self.sid, plan)
        self.assertFalse(self.server.calibration.state(self.sid)["decision"]["offer"])

    def test_start_import_prepare_cancel_and_restart_keep_the_same_chat(self):
        decision, state = self.started()
        self.assertEqual(len(self.store.listing()["sessions"]), 1)
        self.assertEqual(self.server.calibration.start(self.sid, decision["id"])["run"]["id"], state["run"]["id"])
        self.assertEqual(self.bridge.mutations, [])
        prepared = self.server.calibration.prepare(self.sid)
        self.assertEqual(prepared["run"]["state"], "test_ready")
        plan = self.store.internal(self.sid)["plans"][prepared["run"]["plan_ids"][-1]]
        self.assertIn("remove_objects", [a["tool"] for a in plan["actions"]])
        self.assertEqual(self.bridge.mutations, [])
        reopened = SessionStore(self.store.root)
        self.assertEqual(reopened.internal(self.sid)["intelligent_calibration"]["id"], state["run"]["id"])
        self.assertEqual(reopened.internal(self.sid)["intelligent_calibration"]["state"], "test_ready")
        self.server.calibration.command(self.sid, "cancel", {})
        self.assertEqual(self.server.calibration.state(self.sid)["run"]["state"], "cancelled")
        self.assertNotIn("Подготовить тест", self.store.internal(self.sid)["task"]["description"])
        original = copy.deepcopy(self.bridge.objects[0])
        self.bridge.objects = [o for o in self.bridge.objects if o["id"] == "test-node"]
        with self.assertRaises(ApiError):
            self.server.calibration.resume_main(self.sid, self.bridge.scene())
        original["id"] = "restored-node"
        self.bridge.objects = [original]
        self.server.calibration.resume_main(self.sid, self.bridge.scene())
        self.assertEqual(self.store.internal(self.sid)["task_map"]["target_object_ids"], ["restored-node"])
        self.assertNotIn("resume_main_snapshot", self.store.internal(self.sid))

    def test_report_is_saved_in_shared_sqlite_and_checks_are_limited(self):
        _, state = self.started()
        state = self.server.calibration.prepare(self.sid)
        plan_id = state["run"]["plan_ids"][-1]
        self.bridge.objects = [o for o in self.bridge.objects if o["id"] == "test-node"]
        operation = self.add_operation("test-slice")
        self.store.edit(self.sid, lambda s: s["operations"][operation["id"]].update(plan_id=plan_id))
        body = {"operation_id": operation["id"], "request_id": uid(), "printed": True, "result": "good", "defects": [], "text": "Тест напечатан", "answers": {}, "measurements": {}, "photo_ids": [], "checks": {"adhesion": "pass", "extrusion": "pass"}}
        report = self.feedback.report(self.sid, body)
        self.assertEqual(report["print"]["observations"][-1]["data"]["print_kind"], "calibration")
        self.assertEqual(self.server.calibration.state(self.sid)["run"]["state"], "review")
        self.assertEqual(len(self.server.knowledge_base.snapshot()["prints"]), 1)
        self.server.calibration.command(self.sid, "finish", {})
        self.assertEqual(self.server.calibration.state(self.sid)["run"]["state"], "completed")
        self.assertEqual(len(self.db.library(limit=None)["prints"]), 1)

    def test_api_routes_and_selected_photo_consent(self):
        decision = self.setup_plan()
        status, result, _ = self.api("GET", "/api/sessions/" + self.sid + "/intelligent-calibration")
        self.assertEqual(status, 200)
        self.assertEqual(result["decision"]["id"], decision["id"])
        from AIAssistant.agent.settings import Settings
        attachment = receive(self.store, self.sid, "print.png", io.BytesIO(PNG), len(PNG))
        settings = self.server.settings.private()
        target = recipient(settings)
        body = dict(operation_id="first", request_id=uid(), printed=True, result="unknown", defects=[], text="", answers={}, measurements={}, photo_ids=[attachment["id"]], llm_photo_consent=True, llm_recipient="wrong")
        with self.assertRaises(ApiError):
            self.feedback.report(self.sid, body)
        body["llm_recipient"] = target
        self.feedback.report(self.sid, body)
        self.assertIn(attachment["id"], authorized_images(self.store.internal(self.sid), settings))
        self.assertNotIn(attachment["id"], authorized_images(self.store.internal(self.sid), dict(settings, model="other")))

    def test_approved_plan_slice_export_feedback_one_change_repeat_and_rollback(self):
        self.started()
        state = self.server.calibration.prepare(self.sid)
        original_call = self.bridge.call
        def call(name, args, expected_ref=None):
            if name in ("remove_objects", "start_slicing"):
                if expected_ref != self.snapshot()["snapshot_ref"]:
                    raise ApiError("Stale", 409)
                if name == "remove_objects":
                    self.bridge.objects = [o for o in self.bridge.objects if o["id"] not in args["object_ids"]]
                    self.bridge.revision += 1
                    result = {"actual": args}
                else:
                    oid = "slice-" + uid()
                    self.add_operation(oid)
                    self.store.edit(self.sid, lambda s: (s["operations"][oid].update(plan_id=args["plan_id"]), s.update(workflow_state="result_ready")))
                    result = {"operation_id": oid, "status": "slicing"}
                return {"result": result, "snapshot_ref": self.snapshot()["snapshot_ref"]}
            return original_call(name, args, expected_ref)
        self.bridge.call = call
        def apply_slice(pid):
            plan = self.store.internal(self.sid)["plans"][pid]
            self.server.plans.approve(self.sid, pid, plan["version"])
            self.server.plans.apply(self.sid, pid, uid())
            workflow.wait_until(lambda: self.store.internal(self.sid)["workflow_state"] in ("result_ready", "failed"))
            self.assertEqual(self.store.internal(self.sid)["workflow_state"], "result_ready")
            return next(o for o in self.store.internal(self.sid)["operations"].values() if o.get("plan_id") == pid and o.get("kind") == "slice")
        first = apply_slice(state["run"]["plan_ids"][-1])
        exported = self.server.plans.export(self.sid, first["id"], "test-target", uid())
        self.assertEqual(len(exported["files"]), 2)
        def report(oid):
            return self.feedback.report(self.sid, {"operation_id": oid, "request_id": uid(), "printed": True, "result": "defects", "defects": ["adhesion"],
                "checks": {"adhesion": "fail"}, "text": "Плохой первый слой", "answers": {}, "measurements": {}, "photo_ids": []})["print"]
        original = report(first["id"])
        trial = self.feedback.propose(self.sid, original["id"], {"defect": "adhesion", "choice": "1", "external_checked": True})["adjustment"]
        with self.assertRaises(ApiError):
            self.feedback.apply(self.sid, trial["id"], False)
        self.feedback.apply(self.sid, trial["id"], True)
        self.assertEqual(self.values["speed_layer_0"], 18)
        repeat_plan = self.feedback.repeat_plan(self.sid, trial["id"])
        repeated = report(apply_slice(repeat_plan["id"])["id"])
        evaluated = self.feedback.evaluate(self.sid, trial["id"], {"repeat_print_id": repeated["id"], "outcome": "unchanged"})
        self.assertEqual(evaluated["data"]["state"], "evaluated")
        self.assertEqual(self.db.find(self.snapshot())["lessons"], [])
        self.feedback.apply(self.sid, trial["id"], True, rollback=True)
        self.assertEqual(self.values["speed_layer_0"], 20)
        self.assertEqual(self.db.adjustment(trial["id"])["data"]["state"], "rolled_back")
        self.server.calibration.command(self.sid, "finish", {})
        self.assertEqual(len(self.db.library(limit=None)["prints"]), 2)

    def test_photo_reaches_real_local_http_provider_and_stepwise_transport(self):
        self.started()
        state = self.server.calibration.prepare(self.sid)
        self.bridge.objects = [o for o in self.bridge.objects if o["id"] == "test-node"]
        operation = self.add_operation("photo-slice")
        self.store.edit(self.sid, lambda s: s["operations"][operation["id"]].update(plan_id=state["run"]["plan_ids"][-1]))
        provider = workflow.WorkflowTests.configured(self, lambda body, count: {"role": "assistant", "content": json.dumps({"summary": "Проверьте первый слой", "defects": ["adhesion"], "question": "Где начинается отрыв?"})}, pipeline="stepwise")
        attachment = receive(self.store, self.sid, "selected.png", io.BytesIO(PNG), len(PNG))
        unselected = receive(self.store, self.sid, "private.png", io.BytesIO(PNG), len(PNG))
        self.feedback.report(self.sid, {"operation_id": operation["id"], "request_id": uid(), "printed": True, "result": "unknown", "defects": [], "checks": {}, "text": "Осмотрите фото", "answers": {}, "measurements": {},
            "photo_ids": [attachment["id"]], "llm_photo_consent": True, "llm_recipient": recipient(self.server.settings.private())})
        workflow.wait_until(lambda: self.server.calibration.state(self.sid)["run"].get("visual", {}).get("status") == "ready")
        content = provider.requests[0]["body"]["messages"][-1]["content"]
        images = [p for p in content if p["type"] == "image_url"]
        self.assertEqual(len(images), 1)
        self.assertTrue(images[0]["image_url"]["url"].startswith("data:image/png;base64,"))
        session = self.store.internal(self.sid)
        allowed = authorized_images(session, self.server.settings.private())
        self.assertEqual(allowed, {attachment["id"]})
        stored = self.db.library(limit=None)["prints"][0]["observations"][-1]["data"]
        self.assertEqual(stored["visual_hypothesis"]["kind"], "assistant_hypothesis")
        self.assertFalse(stored["visual_hypothesis"]["confirmed"])
        from AIAssistant.agent.llm_client import LLMClient
        from AIAssistant.agent.contracts import obj
        client = LLMClient(self.server.settings)
        steps = Steps(client.complete, lambda *args: "bad", lambda *args: None,
                      images=[image_content(self.store.file(self.sid, fid)) for fid in allowed])
        schema = obj({"summary": {"type": "string"}, "defects": {"type": "array"}, "question": {"type": "string"}}, ["summary", "defects", "question"])
        steps.ask("Проверь выбранное изображение", {"text": "Первый слой"}, schema, "photo_step")
        sent = provider.requests[-1]["body"]["messages"][-1]["content"]
        self.assertEqual(len([p for p in sent if p["type"] == "image_url"]), 1)


class PhotoTransportTests(unittest.TestCase):
    def test_changed_recipient_is_blocked_before_any_http_request(self):
        from AIAssistant.agent.llm_client import LLMClient
        settings = mock.Mock()
        settings.private.return_value = {"base_url": "http://127.0.0.1:1/v1", "model": "new-model", "api_key": "test-key"}
        client = LLMClient(settings)
        client.opener = mock.Mock()
        with self.assertRaises(ApiError) as error:
            client.complete([{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,TEST"}, "_authorized_recipient": "old-recipient"}]}])
        self.assertEqual(error.exception.code, "VISION_AUTHORIZATION_EXPIRED")
        client.opener.open.assert_not_called()

    def test_stepwise_payload_contains_only_explicitly_provided_images(self):
        captured = []
        def complete(messages, **kwargs):
            captured.append(messages)
            return {"content": '{"summary":"ok","limitations":[]}'}
        image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,EXPLICITLY_SELECTED_TEST_IMAGE"}}
        steps = Steps(complete, lambda *args: "bad", lambda *args: None, images=[image])
        from AIAssistant.agent.steps import SUMMARY_SCHEMA
        steps.ask("Review", {"text": "Selected print"}, SUMMARY_SCHEMA, "summary")
        self.assertEqual(captured[0][1]["content"][1], image)
        plain = text_only(captured[0])
        self.assertNotIn("image_url", json.dumps(plain))
        self.assertTrue(vision_unsupported(b'{"error":"This model does not support image input"}'))
        self.assertFalse(vision_unsupported(b'{"error":"Incorrect API key"}'))


if __name__ == "__main__":
    unittest.main()
