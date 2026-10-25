"""Local-provider explanations preserve approved plans and the user's task input."""
import json
import threading
import unittest

import test_workflow as workflow
from AIAssistant.documents import task_input
from AIAssistant.agent.context import analysis_context
from AIAssistant.agent.steps import Steps
from AIAssistant.parameter_explanations import parameter_rows
from AIAssistant.session import SessionStore


class ParameterExplanationTests(unittest.TestCase):
    setUp = workflow.WorkflowTests.setUp
    tearDown = workflow.WorkflowTests.tearDown
    configured = workflow.WorkflowTests.configured
    proposal = workflow.WorkflowTests.proposal
    prepare_plan = workflow.WorkflowTests.prepare_plan
    api = workflow.WorkflowTests.api

    def plan(self):
        plan = self.prepare_plan([{"id": "settings", "tool": "set_settings", "arguments": {"changes": [
            {"key": "infill_sparse_density", "value": 60, "target": {"scope": "extruder", "extruder_id": "extruder-1"}},
            {"key": "support_enable", "value": False, "target": {"scope": "global"}}]},
            "before": None, "after": None, "reason": "Увеличенное заполнение для нагрузки", "source_refs": [{"id": "source", "version": 1}]}])
        def seed(s):
            s["workflow_state"] = "plan_ready"
            s["task"]["description"] = "Кронштейн полки"
            s["task_map"]["requirements"] = [{"key": "load", "value": 5, "unit": "кг"}]
            s["task_map"]["recommended_settings"] = {"settings": [{"key": "infill_sparse_density", "value": 60,
                                                                     "reason": "Нагруженная деталь"}]}
        self.store.edit(self.sid, seed)
        return plan

    def request(self, plan, parameter="setting-0-0", request_id="explain-1", **changes):
        body = dict({"version": plan["version"], "parameter_id": parameter, "request_id": request_id}, **changes)
        return self.api("POST", "/api/sessions/" + self.sid + "/plans/" + plan["id"] + "/explain", body)

    def finished(self, result):
        return workflow.wait_until(lambda: (lambda s: s if s["operations"][result["operation_id"]]["status"] in
                                            ("completed", "error", "interrupted") else None)(self.store.internal(self.sid)))

    def test_explanation_is_visible_in_chat_and_preserves_the_plan_and_task(self):
        provider = self.configured(lambda body, n: {"role": "assistant", "content": "Заполнение 60% увеличивает количество пластика внутри детали."})
        plan = self.plan()
        before = self.store.internal(self.sid)
        status, result, _ = self.request(plan)
        self.assertEqual(status, 202)
        session = self.finished(result)
        self.assertEqual(session["operations"][result["operation_id"]]["status"], "completed")
        self.assertIn("Заполнение: 60 %", session["messages"][-2]["text"])
        self.assertIn("60% увеличивает", session["messages"][-1]["text"])
        for field in ("plans", "task_map", "task", "input_revision", "runs", "workflow_state", "sources"):
            self.assertEqual(session[field], before[field], field)
        self.assertEqual(task_input(session), task_input(before))
        self.assertNotIn("Почему", json.dumps(analysis_context(session, {}), ensure_ascii=False))
        brief, _ = Steps(None, None, None).brief(session, {})
        self.assertNotIn("Почему", brief)
        self.assertEqual(self.bridge.mutations, [])
        payload = provider.requests[0]["body"]
        self.assertNotIn("tools", payload)
        data = json.loads(payload["messages"][1]["content"])
        self.assertEqual(data["requirements"], [{"key": "load", "value": 5, "unit": "кг"}])
        self.assertEqual(data["reasons"], ["Нагруженная деталь"])
        self.assertEqual(data["parameter"]["value"], "60 %")
        self.assertNotIn("printer", data)
        self.assertNotIn("task", data)
        self.assertEqual(self.request(plan)[1], result)
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(self.request(plan, parameter="mode")[0], 409)
        self.server.plans.approve(self.sid, plan["id"], plan["version"])
        self.assertEqual(self.store.internal(self.sid)["plans"][plan["id"]]["status"], "approved")
        second = self.store.create("Второй чат")
        merged = self.server.workspace.merge([self.sid, second["id"]], "Объединённый чат", "merge-explanations")
        merged_session = self.store.internal(merged["id"])
        self.assertNotIn("Почему", task_input(merged_session))
        self.assertNotIn("Почему", Steps(None, None, None).brief(merged_session, {})[0])
        self.store.message(self.sid, "new-condition", "Деталь будет на улице", [], {})
        self.assertNotIn("Почему", self.store.internal(self.sid)["task"]["description"])

    def test_each_parameter_including_selection_and_zero_values_can_be_explained(self):
        self.configured(lambda body, n: {"role": "assistant", "content": "Пояснение выбранного значения."})
        plan = self.plan()
        rows = parameter_rows(plan, self.bridge.snapshot())
        self.assertEqual([r["id"] for r in rows], ["mode", "material", "profile", "setting-0-0", "setting-0-1"])
        self.assertEqual(rows[-1]["value"], "нет")
        for row in rows:
            status, result, _ = self.request(plan, row["id"], "explain-" + row["id"])
            self.assertEqual(status, 202)
            self.assertEqual(self.finished(result)["operations"][result["operation_id"]]["status"], "completed")

    def test_invalid_parameter_version_and_payload_do_not_start_a_request(self):
        provider = self.configured(lambda body, n: {"role": "assistant", "content": "unused"})
        plan = self.plan()
        for kwargs, expected in [({"parameter": "unknown"}, 404), ({"version": 2}, 409), ({"extra": "not allowed"}, 400)]:
            self.assertEqual(self.request(plan, **kwargs)[0], expected)
        self.store.edit(self.sid, lambda s: s["plans"][plan["id"]].update(status="stale"))
        self.assertEqual(self.request(plan)[0], 409)
        self.assertEqual(provider.requests, [])
        self.assertEqual(self.store.internal(self.sid)["messages"], [])

    def test_provider_error_and_late_answer_leave_settings_unchanged(self):
        self.configured(lambda body, n: (401, {}))
        plan = self.plan()
        result = self.request(plan)[1]
        session = self.finished(result)
        self.assertEqual(session["operations"][result["operation_id"]]["status"], "error")
        self.assertEqual(session["plans"][plan["id"]], plan)
        arrived, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def respond(body, n):
            arrived.set()
            release.wait(4)
            return {"role": "assistant", "content": "Ответ по старому плану"}
        self.configured(respond)
        result = self.request(plan, request_id="late-answer")[1]
        self.assertTrue(arrived.wait(3))
        self.assertEqual(self.request(plan, parameter="mode", request_id="while-busy")[0], 409)
        self.store.message(self.sid, "change-task", "Уточнение нагрузки", [], {})
        release.set()
        session = self.finished(result)
        self.assertEqual(session["operations"][result["operation_id"]]["status"], "interrupted")
        self.assertNotIn("Ответ по старому плану", " ".join(m["text"] for m in session["messages"]))
        self.assertEqual(self.bridge.mutations, [])

    def test_a_replaced_plan_cannot_receive_new_explanation_requests(self):
        provider = self.configured(lambda body, n: {"role": "assistant", "content": "unused"})
        previous = self.plan()
        current = self.plan()
        self.assertNotEqual(previous["id"], current["id"])
        self.assertEqual(self.request(previous)[0], 409)
        self.assertEqual(provider.requests, [])

    def test_restart_replaces_unfinished_explanation_placeholder(self):
        def seed(s):
            s["operations"]["explanation"] = {"id": "explanation", "kind": "parameter_explanation", "status": "running", "answer_message_id": "answer"}
            s["messages"].append({"id": "answer", "role": "assistant", "text": "Готовлю пояснение…", "file_ids": [], "origin": "parameter_explanation"})
        self.store.edit(self.sid, seed)
        session = SessionStore(self.temp.name).internal(self.sid)
        self.assertEqual(session["operations"]["explanation"]["status"], "interrupted")
        self.assertIn("прервано", session["messages"][-1]["text"])


if __name__ == "__main__":
    unittest.main()
