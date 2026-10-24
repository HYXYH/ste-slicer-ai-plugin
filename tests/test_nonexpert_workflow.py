"""Uncertain operating conditions explain recommendations without requiring certification."""
import copy
import json
import unittest

import test_workflow as workflow
from AIAssistant.agent.task_state import new_map, readiness
from AIAssistant.native_view import session_document


class NonexpertWorkflowTests(unittest.TestCase):
    setUp = workflow.WorkflowTests.setUp
    tearDown = workflow.WorkflowTests.tearDown
    configured = workflow.WorkflowTests.configured
    import_model = workflow.WorkflowTests.import_model
    proposal = workflow.WorkflowTests.proposal
    finished = workflow.WorkflowTests.finished
    pending = workflow.WorkflowTests.pending
    stepwise = workflow.WorkflowTests.stepwise

    def test_single_unknown_answer_builds_plan_with_explained_assumptions(self):
        def responder(body, count):
            proposal = self.proposal()
            refs = proposal["candidates"][0]["source_refs"]
            proposal["requirements"] = [{"id": "load", "key": "load", "value": 5, "unit": "кг", "status": "known",
                                         "strength": "hard", "source_refs": refs, "object_ids": ["node-1"], "supersedes": None}]
            proposal["unknowns"] = [{"id": "temperature-gap", "field": "service_temperature", "reason": "Нагрев детали неизвестен",
                                     "affected_decisions": ["material"], "blocking": True, "resolution": "user", "rule_refs": [], "source_refs": refs}]
            proposal["questions"] = [{"id": "temperature-question", "field": "service_temperature", "text": "Деталь будет нагреваться?",
                                      "reason": "От нагрева зависит выбор пластика", "affected_decisions": ["material"], "options": [],
                                      "allow_free_text": True, "allow_unknown": False, "required": True, "source_refs": refs}]
            proposal["assumptions"] = [{"id": "environment-assumption", "key": "environment", "value": "В помещении", "unit": None,
                                        "reason": "Место использования не указано", "source_refs": refs}]
            proposal["candidates"][0]["missing_fields"] = ["load", "service_temperature"]
            return {"role": "assistant", "content": json.dumps(proposal, ensure_ascii=False)}

        self.configured(responder)
        self.import_model()
        session = self.finished(self.server.runner.start(self.sid, "nonexpert-start"))
        self.assertEqual(session["workflow_state"], "waiting_user")
        question = next(q for q in session["task_map"]["questions"] if q["id"] == "temperature-question")
        self.assertTrue(question["allow_unknown"])
        self.store.edit(self.sid, lambda s: s["task_map"]["questions"][0].update(allow_unknown=False))
        session = self.finished(self.server.runner.answers(self.sid, {"request_id": "nonexpert-unknown", "answers": [
            {"question_id": question["id"], "value": "Не знаю"}]}))
        self.assertEqual(session["workflow_state"], "plan_ready", session["events"][-1])
        task = session["task_map"]
        self.assertNotIn("risk_acceptance", task)
        self.assertEqual(task["assumptions"][0]["acceptance"], "pending")
        self.assertFalse(task["unknowns"][0].get("resolved"))
        self.assertEqual(task["readiness"]["unverified_requirement_ids"], ["load"])
        self.assertEqual(task["readiness"]["blocking_unknown_ids"], [])
        plan = next(iter(session["plans"].values()))
        self.assertTrue(any("Нагрев детали неизвестен" in line for line in plan["limitations"]))
        self.assertTrue(any("В помещении" in line for line in plan["limitations"]))
        self.assertTrue(any("не подтверждает" in line for line in plan["limitations"]))
        self.assertEqual(self.bridge.mutations, [])
        self.server.plans.approve(self.sid, plan["id"], plan["version"])
        self.server.plans.apply(self.sid, plan["id"])
        workflow.wait_until(lambda: self.store.get(self.sid)["workflow_state"] == "result_ready")
        self.assertIn("start_slicing", self.bridge.mutations)

    def test_stepwise_unknown_answer_keeps_limitation_and_reaches_plan(self):
        self.bridge.context["extruders"][0]["material"]["id"] = "enduse_petg"
        self.stepwise()
        self.finished(self.server.runner.start(self.sid, "step-unknown-start"))
        clarify, confirm = self.pending("step-clar-"), self.pending("step-req-")
        self.assertTrue(clarify["allow_unknown"])
        self.finished(self.server.runner.answers(self.sid, {"request_id": "step-unknown-answer", "answers": [
            {"question_id": clarify["id"], "value": "Не знаю"}, {"question_id": confirm["id"], "value": "Верно"}]}))
        material = self.pending("step-mat-")
        session = self.finished(self.server.runner.answers(self.sid, {"request_id": "step-unknown-material", "answers": [
            {"question_id": material["id"], "value": "Согласен"}]}))
        self.assertEqual(session["workflow_state"], "plan_ready", session["events"][-1])
        self.assertNotIn("risk_acceptance", session["task_map"])
        plan = next(iter(session["plans"].values()))
        self.assertTrue(any("Рабочая температура: данных нет" in line for line in plan["limitations"]))

    def test_native_view_has_no_certification_actions_even_for_saved_checks(self):
        session = self.store.get(self.sid)
        session["task_map"]["readiness"].update(failed_check_ids=["load"], accepted_risk_ids=["load"],
                                                  limitations=["Нагрузка готовой детали неизвестна"])
        session["task_map"]["assumptions"] = [{"id": "a", "key": "environment", "value": "В помещении",
                                               "reason": "Условия не указаны", "acceptance": "pending"}]
        markup, actions = session_document(session)
        self.assertIn("Нагрузка готовой детали неизвестна", markup)
        self.assertIn("Что учтено при подборе", markup)
        self.assertNotIn("Ручная инженерная проверка", markup)
        self.assertNotIn("принять риск", markup)
        self.assertNotIn("Режим прототипа", markup)
        self.assertFalse(any(a["kind"] == "risk" or a.get("field") == "manual_checks" or
                             "assumptions" in a.get("payload", {}) for a in actions.values()))


class UncertainConditionsTests(unittest.TestCase):
    def test_knowledge_is_advisory_but_tool_data_and_conflicts_still_block(self):
        scene = {"snapshot_ref": {"id": "s"}, "objects": [{"id": "n"}]}
        task = new_map()
        task.update(target_object_ids=["n"], snapshot_ref=scene["snapshot_ref"],
                    candidates=[{"id": "c", "status": "selected", "requirement_checks": []}],
                    unknowns=[{"id": "knowledge", "field": "strength", "resolution": "knowledge", "blocking": True,
                               "reason": "Допустимая нагрузка неизвестна"},
                              {"id": "tool", "field": "dimensions_mm", "resolution": "tool", "blocking": True}])
        original = copy.deepcopy(task)
        result = readiness(task, scene, {})
        self.assertEqual(task, original)
        self.assertEqual(result["status"], "needs_input")
        self.assertEqual(result["blocking_unknown_ids"], ["tool"])
        self.assertTrue(result["limitations"])
        task["unknowns"][1]["resolved"] = True
        self.assertEqual(readiness(task, scene, {})["status"], "ready_for_plan")
        task["conflicts"] = [{"id": "conflict"}]
        self.assertEqual(readiness(task, scene, {})["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
