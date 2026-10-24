"""An unavailable target profile must ask for input, never fabricate a plan."""
import copy
import json
import unittest

import test_workflow as workflow
from AIAssistant.agent.contracts import validate
from AIAssistant.agent.output_contract import proposal_schema
from AIAssistant.agent.task_state import merge_proposal, readiness
from AIAssistant.session import ApiError


class MissingProfileTests(unittest.TestCase):
    setUp = workflow.WorkflowTests.setUp
    tearDown = workflow.WorkflowTests.tearDown
    configured = workflow.WorkflowTests.configured
    import_model = workflow.WorkflowTests.import_model
    proposal = workflow.WorkflowTests.proposal
    prepare_plan = workflow.WorkflowTests.prepare_plan
    stepwise = workflow.WorkflowTests.stepwise
    finished = workflow.WorkflowTests.finished
    pending = workflow.WorkflowTests.pending

    def test_null_profile_completes_with_input_needed_and_no_plan(self):
        def responder(body, count):
            proposal = self.proposal()
            proposal["candidates"][0]["profile_id"] = None
            validate(proposal, proposal_schema())
            return {"role": "assistant", "content": json.dumps(proposal)}
        provider = self.configured(responder)
        self.import_model()
        run = self.server.runner.start(self.sid, "missing-profile")
        workflow.wait_until(lambda: self.store.get(self.sid)["runs"][run["run_id"]]["status"] == "completed")
        session = self.store.get(self.sid)
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(session["workflow_state"], "waiting_user")
        self.assertEqual(session["task_map"]["readiness"]["status"], "needs_input")
        self.assertIn("profile_id", session["task_map"]["candidates"][0]["missing_fields"])
        self.assertTrue(any("STE Slicer" in reason for reason in session["task_map"]["readiness"]["reasons"]))
        self.assertEqual(session["plans"], {})
        self.assertEqual(self.bridge.mutations, [])
        self.assertFalse(any(e["type"] == "model_output_rejected" for e in session["events"]))

    def test_target_configuration_failure_can_be_saved_as_incomplete_variant(self):
        self.bridge.context["mode"] = {"id": "cylindrical_full", "name": "5D"}
        original_call = self.bridge.call
        def call(name, args, expected_ref=None):
            if name == "find_compatible_profiles" and args.get("mode") == "classic":
                raise ApiError("Выберите режим и материал в STE Slicer", 409, "TARGET_CONFIGURATION_UNKNOWN")
            return original_call(name, args, expected_ref)
        self.bridge.call = call
        def responder(body, count):
            if count == 1:
                return {"role": "assistant", "content": None, "tool_calls": [{"id": "target-profile", "type": "function", "function": {
                    "name": "find_compatible_profiles", "arguments": '{"mode":"classic","material_id":"new-material"}'}}]}
            self.assertIn("TARGET_CONFIGURATION_UNKNOWN", body["messages"][-1]["content"])
            proposal = self.proposal()
            proposal["candidates"][0].update(profile_id=None, material_id="new-material", status="possible")
            return {"role": "assistant", "content": json.dumps(proposal)}
        self.configured(responder)
        self.import_model()
        run = self.server.runner.start(self.sid, "target-profile-unknown")
        workflow.wait_until(lambda: self.store.get(self.sid)["runs"][run["run_id"]]["status"] == "completed")
        session = self.store.get(self.sid)
        self.assertEqual(session["workflow_state"], "waiting_user")
        self.assertEqual(session["task_map"]["readiness"]["status"], "needs_input")
        self.assertEqual(self.bridge.context["mode"]["id"], "cylindrical_full")
        self.assertEqual(session["plans"], {})
        self.assertEqual(self.bridge.mutations, [])

    def test_empty_profile_stays_invalid_and_missing_marker_is_derived_without_mutation(self):
        self.import_model()
        self.store.edit(self.sid, lambda s: s["sources"].update({"source-1": {"id": "source-1", "version": 1, "kind": "tool"}}))
        session = self.store.internal(self.sid)
        proposal = self.proposal()
        proposal["candidates"][0]["profile_id"] = ""
        with self.assertRaises(ApiError):
            merge_proposal(session["task_map"], proposal, session["sources"])
        proposal["candidates"][0]["profile_id"] = None
        original = copy.deepcopy(proposal)
        updated = merge_proposal(session["task_map"], proposal, session["sources"])
        self.assertEqual(proposal, original)
        self.assertIn("profile_id", updated["candidates"][0]["missing_fields"])

    def test_missing_profile_cannot_be_hidden_by_empty_missing_fields(self):
        task = {"target_object_ids": ["node-1"], "snapshot_ref": {"s": 1}, "requirements": [], "constraints": [], "conflicts": [],
                "assumptions": [], "unknowns": [], "candidates": [{"id": "c", "status": "selected", "profile_id": None,
                "missing_fields": [], "requirement_checks": []}]}
        result = readiness(task, {"snapshot_ref": {"s": 1}, "objects": [{"id": "node-1"}]}, {})
        self.assertEqual(result["status"], "needs_input")

    def test_stepwise_material_and_prototype_consent_cannot_bypass_missing_profile(self):
        self.bridge.context["extruders"][0]["material"]["id"] = "enduse_petg"
        self.bridge.context["profile"]["quality"]["id"] = None
        provider, asked = self.stepwise()
        self.finished(self.server.runner.start(self.sid, "step-missing-profile"))
        confirm = self.pending("step-req-")
        clarify = self.pending("step-clar-")
        self.finished(self.server.runner.answers(self.sid, {"request_id": "confirm-profile-task", "answers": [
            {"question_id": clarify["id"], "value": "Комнатная"},
            {"question_id": confirm["id"], "value": "Верно"}]}))
        material = self.pending("step-mat-")
        session = self.finished(self.server.runner.answers(self.sid, {"request_id": "confirm-profile-material", "answers": [
            {"question_id": material["id"], "value": "Согласен"}]}))
        self.assertEqual(asked, ["requirements", "clarify", "material", "summary"])
        self.assertEqual(len(provider.requests), 4)
        self.assertNotIn("risk_acceptance", session["task_map"])
        session = self.finished(self.server.runner.answers(self.sid, {"request_id": "profile-risk", "accept_risk": {"reason": "Проверка образца"}}))
        task = session["task_map"]
        self.assertTrue(task["risk_acceptance"])
        self.assertTrue(task["readiness"]["accepted_risk_ids"])
        self.assertTrue(task["readiness"]["limitations"])
        self.assertIsNone(task["candidates"][0]["profile_id"])
        self.assertEqual(task["readiness"]["status"], "needs_input")
        self.assertEqual(session["workflow_state"], "waiting_user")
        self.assertEqual(session["plans"], {})
        self.assertEqual(self.bridge.mutations, [])

    def test_plan_validator_refuses_missing_profile_even_if_readiness_is_forged(self):
        plan = self.prepare_plan()
        plan["selected"]["profile_id"] = None
        session = self.store.internal(self.sid)
        with self.assertRaises(ApiError) as caught:
            self.server.plans.validate(session, plan, self.bridge.scene())
        self.assertEqual(caught.exception.code, "NO_COMPATIBLE_PROFILE")
        self.assertEqual(self.bridge.mutations, [])


if __name__ == "__main__":
    unittest.main()
