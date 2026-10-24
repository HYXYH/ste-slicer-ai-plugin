"""Prompt-size regressions and evidence preservation, without native slicer claims."""
import copy
import json
import unittest

import test_workflow as workflow
from test_workflow import wait_until
from AIAssistant.agent.context import analysis_context, compact_result
from AIAssistant.agent.task_state import new_map


class ContextProjectionTests(unittest.TestCase):
    def session(self):
        task = new_map()
        task.update(facts=[{"id": "scene-source", "key": "scene", "source_refs": [{"id": "scene-source", "version": 1}],
                            "value": {"objects": [{"id": "n", "dimensions_mm": [1, 2, 3]}]}}],
                    resources={"catalog": {"items": ["duplicated-catalog"]}, "physical_availability": "unknown"})
        return {"task_map": task, "sources": {"scene-source": {"id": "scene-source", "version": 1, "kind": "tool", "tool": "get_scene_snapshot",
                    "data": {"snapshot_ref": {"fingerprint": "s"}, "objects": [{"id": "n", "dimensions_mm": [1, 2, 3]}]}}}, "messages": []}

    def current(self, session):
        return {"scene": {"data": copy.deepcopy(session["sources"]["scene-source"]["data"]), "source_refs": [{"id": "scene-source", "version": 1}]}}

    def test_historical_tool_growth_does_not_grow_prompt_or_change_history(self):
        session = self.session()
        for i in range(300):
            sid = "old-{}".format(i)
            session["sources"][sid] = {"id": sid, "kind": "tool", "version": 1, "data": {"payload": "historical-payload" * 1000}}
            session["task_map"]["history"].append({"superseded": "old-requirement" * 1000})
        before = copy.deepcopy(session)
        context = analysis_context(session, self.current(session))
        self.assertLess(len(json.dumps(context)), 5000)
        self.assertNotIn("historical-payload", json.dumps(context))
        self.assertEqual(context["task_map"]["history_count"], 300)
        self.assertEqual(context["sources"]["scene-source"]["version"], 1)
        self.assertEqual(context["current_data"]["scene"]["data"]["objects"][0]["dimensions_mm"], [1, 2, 3])
        self.assertEqual(session, before)
        context["current_data"]["scene"]["data"]["objects"].clear()
        self.assertEqual(session, before)

    def test_older_active_user_and_manual_evidence_stay_available(self):
        session = self.session()
        session["sources"].update({"old-user": {"id": "old-user", "kind": "user", "version": 3, "text": "active-hard-requirement"},
                                    "manual": {"id": "manual", "kind": "manual", "version": 1, "requirement_id": "r", "reason": "actual-check"},
                                    "old-tool": {"id": "old-tool", "kind": "tool", "version": 2, "tool": "get_current_configuration", "data": {"snapshot_ref": {"fingerprint": "old"}}}})
        session["task_map"]["requirements"] = [{"id": "r", "value": 10, "unit": "mm", "strength": "hard", "source_refs": [{"id": "old-user", "version": 3}, {"id": "manual", "version": 1}, {"id": "old-tool", "version": 2}]}]
        for i in range(25):
            sid = "message-{}".format(i)
            session["messages"].append({"id": sid, "role": "user", "text": "recent-message"})
            session["sources"][sid] = {"id": sid, "kind": "user", "version": 1, "text": "recent-message"}
        context = analysis_context(session, self.current(session))
        self.assertEqual(context["sources"]["old-user"]["text"], "active-hard-requirement")
        self.assertEqual(context["sources"]["manual"]["reason"], "actual-check")
        self.assertEqual(context["sources"]["manual"]["requirement_id"], "r")
        self.assertTrue(context["sources"]["old-tool"]["historical"])
        self.assertFalse(context["sources"]["scene-source"]["historical"])
        self.assertNotIn("message-0", context["sources"])
        self.assertNotIn("text", context["sources"]["message-24"])
        self.assertEqual(context["task_map"]["requirements"], session["task_map"]["requirements"])

    def test_unknown_observation_is_summary_but_matching_and_reviewed_rules_are_full(self):
        rows = [{"id": "unrelated", "status": "observation", "applicability": "unknown", "provided_requirements": "unrelated-industrial-load", "applies_when": [{"field": "task_brief", "op": "eq", "value": "Other part"}], "missing_fields": ["task_brief"]},
                {"id": "matched", "status": "observation", "applicability": "pass", "provided_requirements": "active-specification"},
                {"id": "expert", "status": "reviewed", "applicability": "unknown", "recommendations": {"layer_height": 0.2}, "limits": {"temperature": 70}}]
        original = {"data": {"items": rows, "next_cursor": "20", "warnings": ["KNOWLEDGE_GAP"]}, "source_refs": [{"id": "knowledge-source", "version": 2}]}
        projected = compact_result("search_knowledge", original)
        self.assertNotIn("provided_requirements", projected["data"]["items"][0])
        self.assertEqual(projected["data"]["items"][0]["applies_when"], rows[0]["applies_when"])
        self.assertEqual(projected["data"]["items"][1], rows[1])
        self.assertEqual(projected["data"]["items"][2], rows[2])
        self.assertEqual(projected["source_refs"], original["source_refs"])
        self.assertEqual(projected["data"]["next_cursor"], "20")
        self.assertEqual(original["data"]["items"][0]["provided_requirements"], "unrelated-industrial-load")


class ContextWorkflowTests(unittest.TestCase):
    setUp = workflow.WorkflowTests.setUp
    tearDown = workflow.WorkflowTests.tearDown
    configured = workflow.WorkflowTests.configured
    import_model = workflow.WorkflowTests.import_model
    proposal = workflow.WorkflowTests.proposal

    def test_repeated_reads_reuse_evidence_then_finish_pairs_and_validate_fresh_scene(self):
        def responder(body, count):
            if "tools" in body:
                return {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "repeat-scene", "type": "function", "function": {"name": "get_scene_snapshot", "arguments": "{}"}},
                    {"id": "repeat-config", "type": "function", "function": {"name": "get_current_configuration", "arguments": "{}"}}]}
            return {"role": "assistant", "content": json.dumps(self.proposal())}
        provider = self.configured(responder)
        self.import_model()
        run = self.server.runner.start(self.sid, "reuse-context")
        wait_until(lambda: self.store.get(self.sid)["runs"][run["run_id"]]["status"] in ("completed", "failed"))
        session = self.store.get(self.sid)
        self.assertEqual(session["workflow_state"], "plan_ready", session["events"][-1])
        self.assertEqual(len(provider.requests), 2)
        self.assertNotIn("tools", provider.requests[-1]["body"])
        pairs = [m for m in provider.requests[-1]["body"]["messages"] if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in pairs], ["repeat-scene", "repeat-config"])
        counts = [e["tool"] for e in session["events"] if e["type"] == "tool_started"]
        self.assertEqual(counts.count("get_scene_snapshot"), 2)  # Initial and mandatory final fresh read.
        self.assertEqual(counts.count("get_current_configuration"), 1)
        self.assertEqual(self.bridge.mutations, [])

    def test_cached_initial_scene_does_not_approve_changed_scene(self):
        def responder(body, count):
            if "tools" in body:
                self.bridge.revision += 1
                return {"role": "assistant", "content": "finish"}
            return {"role": "assistant", "content": json.dumps(self.proposal())}
        self.configured(responder)
        self.import_model()
        run = self.server.runner.start(self.sid, "changed-scene")
        wait_until(lambda: self.store.get(self.sid)["runs"][run["run_id"]]["status"] in ("completed", "failed"))
        session = self.store.get(self.sid)
        self.assertEqual(session["task_map"]["readiness"]["status"], "stale")
        self.assertEqual(session["plans"], {})
        self.assertEqual(self.bridge.mutations, [])


if __name__ == "__main__":
    unittest.main()
