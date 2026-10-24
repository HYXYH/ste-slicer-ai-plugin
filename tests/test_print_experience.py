"""Print evidence survives restarts and only isolated, verified trials affect recommendations."""
import copy
import io
import os
import tempfile
import unittest
import json
from unittest import mock

import test_workflow as workflow
from AIAssistant.session import ApiError, now, uid
from AIAssistant.experience import ExperienceStore, conditions, reuse, proposed_snapshot
from AIAssistant.print_diagnostics import DEFECTS
from AIAssistant.uploads import receive
from AIAssistant.agent.recommend import recommend
from AIAssistant.slicer_api import digest
from test_stage1 import PNG


class PrintExperienceTests(unittest.TestCase):
    tearDown = workflow.WorkflowTests.tearDown
    api = workflow.WorkflowTests.api
    def setUp(self):
        workflow.WorkflowTests.setUp(self)
        self.bridge.context["extruders"][0].update(nozzle_mm=.4)
        self.bridge.context["extruders"][0]["material"]["id"] = "proto_pla"
        self.bridge.objects[0].update(geometry_revision="mesh-sha", rotation_quaternion=[0, 0, 0, 1], scale=[1, 1, 1])
        self.values = {"material_print_temperature": 210, "speed_layer_0": 20., "speed_print": 50., "wall_line_count": 3,
                       "infill_sparse_density": 20, "layer_height": .2, "material_bed_temperature": 60, "cool_fan_speed": 40}
        self.target = {"scope": "extruder", "extruder_id": "extruder-1"}
        self.original_call = self.bridge.call
        def call(name, args, expected_ref=None):
            if name == "get_print_context":
                return self.snapshot()
            if name == "get_calibration_context":
                return {"context": self.bridge.snapshot(), "signature": digest(self.values), "profile_id": "quality-1"}
            if name == "get_setting_definitions":
                return {"items": [{"type": "float", "enabled": True, "settable_per_extruder": True, "unit": "°C",
                                   "minimum_value": 0, "maximum_value": 300}]}
            if name == "validate_settings":
                if args["changes"][0]["value"] > 220:
                    raise ApiError("Machine limit")
                return {"valid": True}
            if name == "set_settings":
                if expected_ref != self.snapshot()["snapshot_ref"]:
                    raise ApiError("Stale", 409)
                for change in args["changes"]:
                    self.values[change["key"]] = change["value"]
                self.bridge.revision += 1
                self.bridge.mutations.append(copy.deepcopy(args))
                return {"result": {"actual": args}, "snapshot_ref": self.snapshot()["snapshot_ref"]}
            return self.original_call(name, args, expected_ref)
        self.bridge.call = call
        self.feedback = self.server.print_feedback
        self.db = self.feedback.db
        self.add_operation("first")

    def snapshot(self):
        return {"context": self.bridge.snapshot(), "settings": [{"target": self.target, "values": copy.deepcopy(self.values)}],
                "objects": copy.deepcopy(self.bridge.objects), "snapshot_ref": self.bridge.scene()["snapshot_ref"], "captured_at": now()}

    def add_operation(self, oid):
        operation = {"id": oid, "kind": "slice", "plan_id": "original-plan", "status": "completed", "created_at": now(),
                     "print_snapshot": self.snapshot(), "snapshot_ref": self.snapshot()["snapshot_ref"]}
        self.store.edit(self.sid, lambda s: s["operations"].update({oid: operation}))
        return operation

    def report(self, operation="first", result="defects", defects=None, **extra):
        data = dict(operation_id=operation, request_id=uid(), printed=True, result=result,
                    defects=["stringing"] if defects is None and result == "defects" else defects or [],
                    text="", answers={}, measurements={"actual_mm": [10., 20., 30.]}, photo_ids=[])
        data.update(extra)
        return self.feedback.report(self.sid, data)

    def proposal(self):
        report = self.report()
        result = self.feedback.propose(self.sid, report["print"]["id"], {"defect": "stringing", "choice": "0", "external_checked": True})
        self.assertIn("adjustment", result)
        return result["adjustment"]

    def confirmed(self, outcome="resolved"):
        trial = self.proposal()
        self.feedback.apply(self.sid, trial["id"], True)
        self.add_operation("repeat")
        result = self.report("repeat", "good" if outcome == "resolved" else "defects")
        return self.feedback.evaluate(self.sid, trial["id"], {"repeat_print_id": result["print"]["id"], "outcome": outcome})

    def test_facts_saved_idempotently_with_original_slice_snapshot_and_reopened(self):
        self.values["material_print_temperature"] = 230
        result = self.report(request_id="report-one")
        pid = result["print"]["id"]
        self.assertEqual(result["print"]["snapshot"]["settings"][0]["values"]["material_print_temperature"], 210)
        self.assertEqual(self.report(request_id="report-one")["print"]["id"], pid)
        reopened = ExperienceStore(self.store.root)
        self.assertEqual(len(reopened.get(pid)["observations"]), 1)
        self.assertEqual(reopened.get(pid)["observations"][0]["data"]["kind"], "user_observation")
        self.assertEqual(self.store.internal(self.sid)["plans"], {})

    def test_no_defects_unknown_unchecked_and_free_text(self):
        self.assertEqual(len(DEFECTS), 18)
        self.assertEqual(self.report(result="good")["questions"], [])
        self.assertEqual(self.report(result="not_checked")["questions"], [])
        self.assertEqual(self.report(result="unknown")["questions"][0]["id"], "photos")
        result = self.report(defects=[], text="На детали паутинка")
        self.assertIn("stringing", result["print"]["observations"][-1]["data"]["defects"])
        self.assertLessEqual(len(result["questions"]), 2)

    def test_invalid_facts_are_rejected_without_mutation(self):
        for update in ({"printed": False}, {"defects": ["invented"]}, {"measurements": {"actual_mm": [float("nan"), 1, 2]}},
                       {"result": "good", "defects": ["stringing"]}, {"photo_ids": ["not-a-photo"]}):
            with self.assertRaises(ApiError):
                self.report(**update)
        self.assertEqual(self.db.library()["prints"], [])

    def test_hypothesis_never_applies_without_consent_and_is_not_reused(self):
        trial = self.proposal()
        self.assertEqual(self.bridge.mutations, [])
        self.assertEqual(self.db.find(self.snapshot())["lessons"], [])
        with self.assertRaises(ApiError):
            self.feedback.apply(self.sid, trial["id"], False)
        self.assertEqual(self.bridge.mutations, [])

    def test_one_change_verified_on_repeat_and_reused_after_restart(self):
        trial = self.confirmed()
        self.assertEqual(len(self.bridge.mutations), 1)
        self.assertEqual(len(self.bridge.mutations[0]["changes"]), 1)
        self.assertEqual(self.values["material_print_temperature"], 205)
        self.values["material_print_temperature"] = 210
        evidence = ExperienceStore(self.store.root).find(self.snapshot())
        self.assertEqual(len(evidence["lessons"]), 1)
        result = reuse({"settings": [], "warnings": [], "source_ids": []}, evidence)
        self.assertEqual(result["settings"][0]["value"], 205)
        self.assertEqual(result["settings"][0]["target"], self.target)
        self.assertEqual(trial["data"]["outcome"], "resolved")

    def test_improvement_is_supported_not_confirmed_and_failure_can_rollback(self):
        trial = self.confirmed("better")
        self.assertEqual(self.db.library()["lessons"][0]["degree"], "supported")
        self.assertEqual(self.db.find(self.snapshot())["lessons"], [])
        self.feedback.apply(self.sid, trial["id"], True, rollback=True)
        self.assertEqual(self.values["material_print_temperature"], 210)
        self.assertEqual(self.db.adjustment(trial["id"])["data"]["state"], "rolled_back")

    def test_mismatched_repeat_and_contradictory_resolution_are_rejected(self):
        trial = self.proposal()
        self.feedback.apply(self.sid, trial["id"], True)
        self.add_operation("repeat")
        repeated = self.report("repeat")
        with self.assertRaises(ApiError):
            self.feedback.evaluate(self.sid, trial["id"], {"repeat_print_id": repeated["print"]["id"], "outcome": "resolved"})
        self.values["speed_print"] = 100
        self.add_operation("changed")
        changed = self.report("changed", "good")
        with self.assertRaises(ApiError):
            self.feedback.evaluate(self.sid, trial["id"], {"repeat_print_id": changed["print"]["id"], "outcome": "resolved"})
        self.assertEqual(self.db.library()["lessons"][0]["degree"], "hypothesis")

    def test_incompatible_material_nozzle_mode_printer_model_and_other_settings_do_not_match(self):
        self.confirmed()
        original = self.snapshot()
        for mutate in (lambda s: s["context"]["extruders"][0]["material"].update(id="enduse_petg"),
                       lambda s: s["context"]["extruders"][0].update(nozzle_mm=.6),
                       lambda s: s["context"]["mode"].update(id="cylindrical"),
                       lambda s: s["context"]["printer"].update(id="other"),
                       lambda s: s["objects"][0].update(geometry_revision="other-model"),
                       lambda s: s["settings"][0]["values"].update(speed_print=200)):
            candidate = copy.deepcopy(original)
            mutate(candidate)
            self.assertEqual(self.db.find(candidate)["lessons"], [])

    def test_stale_proposal_limits_manual_branches_and_external_checks(self):
        result = self.report()
        pid = result["print"]["id"]
        self.assertIn("manual", self.feedback.propose(self.sid, pid, {"defect": "stringing", "choice": "0", "external_checked": False}))
        self.assertIn("manual", self.feedback.propose(self.sid, pid, {"defect": "stringing", "choice": "1", "external_checked": True}))
        trial = self.proposal()
        self.bridge.revision += 1
        with self.assertRaises(ApiError):
            self.feedback.apply(self.sid, trial["id"], True)
        self.values["material_print_temperature"] = 200
        self.add_operation("cold")
        cold = self.report("cold")["print"]["id"]
        self.assertIn("manual", self.feedback.propose(self.sid, cold, {"defect": "stringing", "choice": "0", "external_checked": True}))

    def test_api_exposes_library_and_reports_but_not_a_no_consent_mutation(self):
        status, state, _ = self.api("GET", "/api/sessions/" + self.sid + "/print-feedback")
        self.assertEqual(status, 200)
        self.assertEqual(len(state["catalog"]), 18)
        trial = self.proposal()
        status, _, _ = self.api("POST", "/api/sessions/" + self.sid + "/print-feedback/" + trial["id"] + "/apply", {"consent": False})
        self.assertGreaterEqual(status, 400)
        status, library, _ = self.api("GET", "/api/experience")
        self.assertEqual(status, 200)
        self.assertEqual(library["lessons"][0]["degree"], "hypothesis")

    def test_llm_tool_receives_scoped_observations_and_verified_lessons(self):
        self.confirmed()
        evidence = self.server.tools.call("search_print_experience", {}, {"session_id": self.sid})
        self.assertEqual(evidence["data"]["lessons"][0]["degree"], "confirmed")
        self.bridge.context["extruders"][0]["nozzle_mm"] = .6
        self.assertEqual(self.server.tools.call("search_print_experience", {}, {"session_id": self.sid})["data"]["lessons"], [])

    def test_reference_recommendation_reuses_a_verified_setting_and_rejects_other_material(self):
        self.values["material_print_temperature"] = 205
        self.add_operation("first")
        original = self.report(defects=["adhesion"])["print"]
        trial = self.feedback.propose(self.sid, original["id"], {"defect": "adhesion", "choice": "1", "external_checked": True})["adjustment"]
        self.feedback.apply(self.sid, trial["id"], True)
        self.add_operation("repeat")
        repeated = self.report("repeat", "good")["print"]
        self.feedback.evaluate(self.sid, trial["id"], {"repeat_print_id": repeated["id"], "outcome": "resolved"})
        result = recommend("PLA, декоративная модель", self.bridge.snapshot(), experience=lambda result: reuse(
            result, self.db.find(proposed_snapshot(self.snapshot(), result))))
        self.assertEqual(next(s["value"] for s in result["settings"] if s["key"] == "speed_layer_0"), 18)
        other = recommend("PETG", self.bridge.snapshot())
        self.assertEqual(proposed_snapshot(self.snapshot(), other), {})

    def test_photos_survive_chat_deletion_and_same_gcode_can_have_multiple_prints(self):
        attachment = receive(self.store, self.sid, "print.png", io.BytesIO(PNG), len(PNG))
        first = self.report(photo_ids=[attachment["id"]])["print"]
        second = self.report()["print"]
        self.assertNotEqual(first["id"], second["id"])
        photo = first["observations"][0]["data"]["photos"][0]
        self.store.delete(self.sid)
        with open(os.path.join(self.db.root, photo["id"]), "rb") as source:
            self.assertEqual(source.read(), PNG)
        self.assertEqual(len(ExperienceStore(self.store.root).library()["prints"]), 2)

    def test_single_map_llm_receives_experience_in_current_data(self):
        self.report()
        provider = workflow.WorkflowTests.configured(self, lambda body, count: {"role": "assistant", "content": json.dumps(workflow.WorkflowTests.proposal(self))})
        self.store.edit(self.sid, lambda s: s["task_map"].update(target_object_ids=["node-1"]))
        run = self.server.runner.start(self.sid, "experience-llm")
        workflow.wait_until(lambda: self.store.internal(self.sid)["runs"][run["run_id"]]["status"] in ("completed", "failed"))
        payload = json.loads(provider.requests[0]["body"]["messages"][1]["content"])
        evidence = payload["current_data"]["print_experience"]["data"]
        self.assertTrue(evidence["observations"])
        self.assertEqual(evidence["lessons"], [])

    def test_repeat_plan_preserves_one_trial_setting_and_requires_normal_approval(self):
        selected = {"mode": "classic", "material_id": "proto_pla", "profile_id": "quality-1", "extruder_id": "extruder-1"}
        def original(s):
            s["plans"]["original-plan"] = {"id": "original-plan", "selected": selected, "target_object_ids": ["node-1"],
                                           "limitations": ["Original limitation"], "status": "applied"}
        self.store.edit(self.sid, original)
        trial = self.proposal()
        self.feedback.apply(self.sid, trial["id"], True)
        plan = self.feedback.repeat_plan(self.sid, trial["id"])
        self.assertEqual(plan["status"], "validated")
        self.assertIsNone(plan["approval"])
        self.assertEqual(plan["actions"], [])
        self.assertIn("Original limitation", plan["limitations"])
        self.assertEqual(self.values["material_print_temperature"], 205)
        self.assertEqual(self.store.internal(self.sid)["workflow_state"], "plan_ready")


