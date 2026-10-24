"""Contract and real-localhost tests. FakeSlicer is explicitly not STE Slicer."""
import copy
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from collections import deque
from unittest import mock
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "plugin"))
from AIAssistant.session import SessionStore, ApiError
from AIAssistant.server import LocalServer
from AIAssistant.agent.contracts import validate, obj, NUMBER, strict_json
from AIAssistant.agent.task_state import new_map, merge_proposal, readiness, resolve_tool_unknowns, select_default_candidate, COLLECTIONS
from AIAssistant.agent.knowledge import Knowledge, condition
from AIAssistant.agent.recommend import recommend, describe
from AIAssistant.agent.llm_client import LLMClient, endpoint, image_content
from AIAssistant.agent.mcp_client import MCPClient
from AIAssistant.slicer_api import digest


def wait_until(predicate, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for test worker")


class FakeSlicer:
    """Deterministic test adapter; never used in the installed plugin."""
    def __init__(self, store):
        self.store = store
        self.preferences = {"base_url": "http://127.0.0.1:1/v1", "model": "test-model", "api_key": ""}
        self.revision = 0
        self.fail_change = False
        self.mutations = []
        self.objects = [{"id": "node-1", "name": "Test mesh", "printable": True, "dimensions_mm": [10, 20, 30], "file_refs": []}]
        self.context = {"available": True, "mode": {"id": "classic", "name": "3D"},
                        "profile": {"quality": {"id": "quality-1"}, "custom": {"id": "empty_quality_changes"}},
                        "printer": {"id": "printer-1", "name": "Fake printer"},
                        "extruders": [{"id": "extruder-1", "material": {"id": "material-1", "type": "PLA"}}]}

    def snapshot(self):
        return copy.deepcopy(self.context)

    def scene(self):
        return {"process_id": self.store.run_id, "objects": copy.deepcopy(self.objects), "configuration": self.snapshot(),
                "snapshot_ref": {"process_id": self.store.run_id, "fingerprint": digest([self.context, self.objects, self.revision])}}

    def submit(self, operations):
        for operation in operations:
            self.store.finish(operation["session_id"], operation["id"], "imported", object_ids=["node-1"])

    def call(self, name, args, expected_ref=None):
        if expected_ref and expected_ref != self.scene()["snapshot_ref"]:
            raise ApiError("Test scene changed", 409, "STALE_INPUT")
        if name == "preferences":
            self.preferences.update(args.get("values", {}))
            return copy.deepcopy(self.preferences)
        if name == "get_scene_snapshot":
            result = self.scene()
        elif name == "get_print_context":
            result = {"context": self.snapshot(), "objects": copy.deepcopy(self.objects), "settings": [],
                      "snapshot_ref": self.scene()["snapshot_ref"], "captured_at": "test"}
        elif name == "get_printer_capabilities":
            result = {"modes": ["classic", "cylindrical", "cylindrical_full"], "extruders": [{"id": "extruder-1"}]}
        elif name == "get_current_configuration":
            result = {"context": self.snapshot(), "snapshot_ref": self.scene()["snapshot_ref"], "settings": {key: 0.1 for key in args.get("keys", [])}}
        elif name == "list_materials":
            result = {"items": [{"id": "material-1", "compatibility": "pass"}], "next_cursor": None}
        elif name == "find_compatible_profiles":
            result = {"items": [{"id": "quality-1", "compatibility": "pass"}], "next_cursor": None}
        elif name == "validate_settings":
            result = {"valid": True}
        elif name == "start_slicing":
            self.mutations.append(name)
            oid = "slice-" + str(len(self.mutations))
            ref = self.scene()["snapshot_ref"]
            self.store.edit(args["session_id"], lambda s: (s["operations"].update({oid: {"id": oid, "kind": "slice", "plan_id": args["plan_id"], "status": "completed", "snapshot_ref": ref,
                           "result": {"time": {"value": 60, "unit": "s"}, "material": {"values": [0.5], "unit": "m"}}}}), s.update(workflow_state="result_ready")))
            result = {"operation_id": oid, "status": "slicing", "snapshot_ref": ref}
        elif name in ("save_project", "export_gcode"):
            self.mutations.append(name)
            result = {"filename": "project.3mf" if name == "save_project" else "result.gcode", "size": 100, "written": True}
        elif name == "choose_export_target":
            result = {"export_target_id": "test-target", "folder_name": "TEST ONLY"}
        else:
            if self.fail_change:
                raise ApiError("Test failure", 409, "PARTIAL_APPLICATION")
            self.mutations.append(name)
            self.revision += 1
            result = {"actual": args}
        return {"result": result, "snapshot_ref": self.scene()["snapshot_ref"]} if expected_ref else result


class ThreadedHTTP(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class Provider:
    def __init__(self, responder):
        self.requests, self.responder = [], responder
        provider = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                provider.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                result = provider.responder(body, len(provider.requests))
                if isinstance(result, tuple):
                    status, value = result
                else:
                    status, value = 200, {"choices": [{"finish_reason": "stop", "message": result}]}
                raw = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except OSError:
                    pass
        self.server = ThreadedHTTP(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port) + "/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SessionStore(self.temp.name)
        self.sid = self.store.listing()["active_session_id"]
        self.bridge = FakeSlicer(self.store)
        self.server = LocalServer(self.store, self.bridge)
        self.server.start()
        self.providers = []

    def tearDown(self):
        self.server.stop()
        self.server.runner.worker.join(3)
        for provider in self.providers:
            provider.close()
        self.temp.cleanup()

    def api(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=4)
        request_headers = {"X-Assistant-Token": self.server.token}
        request_headers.update(headers or {})
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
            request_headers["Content-Type"] = "application/json"
        connection.request(method, path, body, request_headers)
        response = connection.getresponse()
        raw, status, result_headers = response.read(), response.status, dict(response.getheaders())
        connection.close()
        return status, json.loads(raw) if raw else None, result_headers

    def rpc(self, message, sid=None, extra=None):
        headers = {"Accept": "application/json, text/event-stream"}
        if sid:
            headers.update({"Mcp-Session-Id": sid, "MCP-Protocol-Version": "2025-06-18"})
        headers.update(extra or {})
        return self.api("POST", "/mcp", message, headers)

    def mcp_id(self):
        status, reply, headers = self.rpc({"jsonrpc": "2.0", "id": "init-1", "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}})
        self.assertEqual(status, 200)
        self.assertEqual(reply["id"], "init-1")
        return headers["Mcp-Session-Id"]

    def proposal(self):
        session = self.store.internal(self.sid)
        source = next(ref for ref, value in session["sources"].items() if value["kind"] == "tool")
        refs = [{"id": source, "version": 1}]
        proposal = {name: [] for name in COLLECTIONS}
        proposal.update(summary="Тест: сохраняем исходный профиль. Инженерная пригодность этим тестом не подтверждена.",
                        candidates=[{"id": "candidate-1", "status": "selected", "mode": "classic", "material_id": "material-1", "profile_id": "quality-1", "extruder_id": "extruder-1",
                                     "orientation_proposal": None, "requirement_checks": [], "missing_fields": [], "evidence_refs": refs, "rejection_reasons": [], "source_refs": refs}],
                        decision_proposals=[{"id": "decision-1", "candidate_id": "candidate-1", "actions": [], "limitations": ["Тестовый адаптер"], "source_refs": refs}])
        return proposal

    def configured(self, responder, pipeline="single"):
        provider = Provider(responder)
        self.providers.append(provider)
        self.server.settings.update({"base_url": provider.url, "api_key": "test-secret-value", "model": "test-model", "pipeline": pipeline})
        return provider

    def import_model(self):
        self.store.edit(self.sid, lambda s: s["files"].update({"file-1": {"id": "file-1", "kind": "model", "name": "test.stl", "path": "not-used", "size": 1,
                      "status": "imported", "scene_present": True, "object_ids": ["node-1"], "run_id": self.store.run_id}}))
        self.store.message(self.sid, "initial-message", "Декоративный образец. Сохранить исходный профиль.", ["file-1"], self.bridge.snapshot())

    def test_mcp_lifecycle_schema_and_no_external_mutation(self):
        sid = self.mcp_id()
        request = {"jsonrpc": "2.0", "id": 42, "method": "tools/list"}
        self.assertEqual(self.rpc(request, sid)[1]["error"]["code"], -32600)
        self.assertEqual(self.rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)[:2], (202, None))
        reply = self.rpc(request, sid)[1]
        self.assertEqual(reply["id"], 42)
        tools = reply["result"]["tools"]
        self.assertGreaterEqual(len(tools), 20)
        self.assertTrue(all(t["inputSchema"]["additionalProperties"] is False for t in tools))
        bad = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "get_scene_snapshot", "arguments": {"path": "bad"}}}
        self.assertEqual(self.rpc(bad, sid)[1]["error"]["code"], -32602)
        bad["params"] = {"name": "set_print_mode", "arguments": {"mode": "classic", "plan_id": "invented", "action_id": "invented", "request_id": "invented"}}
        reply = self.rpc(bad, sid)[1]["result"]
        self.assertTrue(reply["isError"])
        self.assertEqual(json.loads(reply["content"][0]["text"])["code"], "APPROVAL_REQUIRED")
        self.assertEqual(self.bridge.mutations, [])
        self.assertEqual(self.api("GET", "/mcp")[0], 405)
        self.assertEqual(self.api("DELETE", "/mcp", headers={"Mcp-Session-Id": sid})[0], 200)
        self.assertEqual(self.rpc(request, sid)[0], 404)

    def test_mcp_parse_errors_unknown_method_and_tool(self):
        self.assertEqual(self.api("POST", "/mcp", b"{", {"Content-Type": "application/json"})[1]["error"]["code"], -32700)
        self.assertEqual(self.rpc([])[1]["error"]["code"], -32600)
        sid = self.mcp_id()
        self.rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
        self.assertEqual(self.rpc({"jsonrpc": "2.0", "id": 1, "method": "unknown"}, sid)[1]["error"]["code"], -32601)
        self.assertEqual(self.rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "unknown"}}, sid)[1]["error"]["code"], -32602)
        self.assertEqual(self.rpc({"jsonrpc": "2.0", "id": 3, "method": "ping"})[0], 400)

    def test_run_token_revoked_and_not_accepted_from_llm_arguments(self):
        token = "test-run-secret"
        self.server.mcp.grant(token, {"session_id": self.sid, "run_id": "rid", "phase": "analyzing"})
        client = MCPClient(self.server, token)
        client.initialize()
        self.assertEqual(client.call("get_scene_snapshot", {})["data"]["objects"][0]["id"], "node-1")
        self.server.mcp.revoke(token)
        with self.assertRaises(ApiError):
            client.call("get_scene_snapshot", {})

    def test_settings_secret_preservation_removal_and_real_wire_format(self):
        provider = self.configured(lambda body, n: {"role": "assistant", "content": "test-secret-value"})
        response = self.api("GET", "/api/settings/llm")[1]
        self.assertTrue(response["key_configured"])
        self.assertNotIn("test-secret-value", json.dumps(response))
        self.server.settings.update({"api_key": "", "model": "same-model"})
        self.assertEqual(self.server.settings.private()["api_key"], "test-secret-value")
        result = LLMClient(self.server.settings).complete([{"role": "user", "content": "hello"}])
        self.assertEqual(result["content"], "test-secret-value")
        self.assertEqual(provider.requests[0]["path"], "/v1/chat/completions")
        self.assertEqual(provider.requests[0]["headers"]["Authorization"], "Bearer test-secret-value")
        self.assertFalse(any(k.lower() in ("x-assistant-token", "x-agent-run-token") for k in provider.requests[0]["headers"]))
        self.assertEqual(provider.requests[0]["body"]["model"], "same-model")
        with open(self.store.path, encoding="utf-8") as source:
            self.assertNotIn("test-secret-value", source.read())
        self.server.settings.update({}, clear=True)
        self.assertFalse(self.server.settings.public()["key_configured"])

    def test_llm_auth_invalid_json_and_truncation(self):
        for result, expected in [((401, {}), "LLM_AUTH_ERROR"), ((200, b"{"), "INVALID_MODEL_OUTPUT"),
                                 ((200, {"choices": [{"finish_reason": "length", "message": {"role": "assistant", "content": "{"}}]}), "INVALID_MODEL_OUTPUT")]:
            provider = self.configured(lambda body, n, result=result: result)
            with self.assertRaises(ApiError) as caught:
                LLMClient(self.server.settings).complete([{"role": "user", "content": "test"}])
            self.assertEqual(caught.exception.code, expected)

    def test_complete_agent_mcp_and_provider_loop_then_approval_and_export(self):
        def responder(body, count):
            if count == 1:
                return {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "one", "type": "function", "function": {"name": "get_scene_snapshot", "arguments": "{}"}},
                    {"id": "two", "type": "function", "function": {"name": "get_printer_capabilities", "arguments": "{}"}}]}
            if "tools" in body:
                return {"role": "assistant", "content": "Данные получены"}
            return {"role": "assistant", "content": json.dumps(self.proposal(), ensure_ascii=False)}
        provider = self.configured(responder)
        self.import_model()
        status, started, _ = self.api("POST", "/api/sessions/" + self.sid + "/runs", {"request_id": "run-test"})
        self.assertEqual(status, 202)
        session = wait_until(lambda: self.store.get(self.sid) if self.store.get(self.sid)["runs"][started["run_id"]]["status"] in ("completed", "failed") else None)
        self.assertEqual(session["workflow_state"], "plan_ready", session["events"][-1])
        provider_tools = provider.requests[0]["body"]["tools"]
        self.assertTrue(all(t["function"]["name"] not in ("set_settings", "start_slicing") for t in provider_tools))
        tool_messages = [m for m in provider.requests[1]["body"]["messages"] if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tool_messages], ["one", "two"])
        self.assertNotIn("tools", provider.requests[-1]["body"])
        self.assertEqual([e["seq"] for e in session["events"]], list(range(1, len(session["events"]) + 1)))
        self.assertEqual(self.bridge.mutations, [])
        plan = next(iter(session["plans"].values()))
        prefix = "/api/sessions/" + self.sid + "/plans/" + plan["id"]
        self.assertEqual(self.api("POST", prefix + "/apply", {"request_id": "apply-1"})[0], 409)
        self.assertEqual(self.api("POST", prefix + "/approve", {"version": plan["version"]})[0], 200)
        result = self.api("POST", prefix + "/apply", {"request_id": "apply-1"})[1]
        wait_until(lambda: self.store.get(self.sid)["workflow_state"] == "result_ready")
        self.assertEqual(self.api("POST", prefix + "/apply", {"request_id": "apply-1"})[1], result)
        self.assertEqual(self.bridge.mutations.count("start_slicing"), 1)
        op = next(o for o in self.store.get(self.sid)["operations"].values() if o.get("kind") == "slice")
        exported = self.server.plans.export(self.sid, op["id"], "test-target", "export-1")
        self.assertEqual(len(exported["files"]), 2)
        self.server.plans.export(self.sid, op["id"], "test-target", "export-1")
        self.assertEqual(self.bridge.mutations.count("save_project"), 1)
        self.assertEqual(self.bridge.mutations.count("export_gcode"), 1)
        self.assertNotIn("test-secret-value", json.dumps(self.store.get(self.sid)))

    def test_cancel_discards_late_provider_response(self):
        arrived, release = threading.Event(), threading.Event()
        def responder(body, count):
            arrived.set()
            release.wait(3)
            return {"role": "assistant", "content": "Late text"}
        self.configured(responder)
        self.import_model()
        run = self.server.runner.start(self.sid, "cancel-1")
        self.assertTrue(arrived.wait(3))
        self.server.runner.cancel(self.sid, run["run_id"])
        release.set()
        wait_until(lambda: any(e.get("code") == "CANCELLED" for e in self.store.get(self.sid)["events"]))
        session = self.store.get(self.sid)
        self.assertEqual(session["workflow_state"], "cancelled")
        self.assertEqual(session["plans"], {})
        self.assertFalse(any(m["role"] == "assistant" for m in session["messages"]))

    def prepare_plan(self, actions=None):
        scene = self.bridge.scene()
        def seed(s):
            s["sources"]["source"] = {"id": "source", "version": 1, "kind": "tool"}
            s["task_map"].update(target_object_ids=["node-1"], snapshot_ref=scene["snapshot_ref"], readiness={"status": "ready_for_plan"},
                                 candidates=[{"id": "candidate-1", "status": "selected", "mode": "classic", "material_id": "material-1", "profile_id": "quality-1", "extruder_id": "extruder-1"}])
        self.store.edit(self.sid, seed)
        proposal = self.proposal()
        if actions:
            proposal["decision_proposals"][0]["actions"] = actions
        return self.server.plans.build(self.sid, proposal)

    def test_failed_analysis_still_returns_reference_settings(self):
        self.configured(lambda body, count: {"role": "assistant", "content": "stop" if "tools" in body else "{invalid"})
        self.import_model()
        self.store.edit(self.sid, lambda s: s["task"].update(description="Кронштейн под нагрузку, PETG, без поддержек"))
        run = self.server.runner.start(self.sid, "fallback-settings")
        wait_until(lambda: self.store.get(self.sid)["runs"][run["run_id"]]["status"] == "failed")
        session = wait_until(lambda: (lambda s: s if s["task_map"].get("recommended_settings") else None)(self.store.internal(self.sid)))
        settings = {row["key"]: row["value"] for row in session["task_map"]["recommended_settings"]["settings"]}
        self.assertEqual(settings["support_enable"], False)
        self.assertEqual(settings["wall_line_count"], 5)
        self.assertIn("Настройки слайсера под задачу", session["messages"][-1]["text"])

    def stepwise(self):
        """Answers each small step by its prompt and records which steps were asked."""
        asked = []
        def responder(body, count):
            system = body["messages"][0]["content"]
            if "выдели из текста" in system:
                asked.append("requirements")
                corrected = "10" if "Уточнение" in body["messages"][1]["content"] else "5"
                return {"role": "assistant", "content": json.dumps({"summary": "Кронштейн полки из PETG.", "requirements": [
                    {"key": "material", "value": "PETG", "unit": "", "strength": "preference"},
                    {"key": "load", "value": corrected, "unit": "кг", "strength": "hard"}]}, ensure_ascii=False)}
            if "найди пробелы" in system:
                asked.append("clarify")
                return {"role": "assistant", "content": json.dumps({"questions": [
                    {"field": "service_temperature", "text": "Рабочая температура?", "reason": "Влияет на материал", "options": ["Комнатная", "До 60 °C"]}]}, ensure_ascii=False)}
            if "справочник уже выбрал" in system:
                asked.append("material")
                return {"role": "assistant", "content": json.dumps({"reason": "PETG прочнее PLA и не боится влаги.", "risks": ["Ползучесть под нагрузкой"]}, ensure_ascii=False)}
            asked.append("summary")
            return {"role": "assistant", "content": json.dumps({"summary": "Кронштейн из PETG, 5 стенок.", "limitations": []}, ensure_ascii=False)}
        provider = self.configured(responder, "stepwise")
        self.import_model()
        self.store.edit(self.sid, lambda s: s["task"].update(description="Кронштейн под нагрузку, PETG, без поддержек"))
        return provider, asked

    def finished(self, run):
        return wait_until(lambda: (lambda s: s if s["runs"][run["run_id"]]["status"] in ("completed", "failed") else None)(self.store.get(self.sid)))

    def pending(self, prefix):
        return next(q for q in self.store.internal(self.sid)["task_map"]["questions"] if q["id"].startswith(prefix) and not q.get("answer"))

    def test_stepwise_splits_task_into_small_confirmed_steps(self):
        self.bridge.context["extruders"][0]["material"]["id"] = "enduse_petg"
        call = self.bridge.call
        def call_without_fan(name, args, expected_ref=None):
            if name == "validate_settings" and any(c["key"] == "cool_fan_speed" for c in args["changes"]):
                raise ApiError("Настройка недоступна: cool_fan_speed", 400, "INVALID_ARGUMENTS")
            return call(name, args, expected_ref)
        self.bridge.call = call_without_fan
        provider, asked = self.stepwise()
        session = self.finished(self.server.runner.start(self.sid, "step-1"))
        self.assertEqual(session["workflow_state"], "waiting_user", session["events"][-1])
        self.assertEqual(asked, ["requirements", "clarify"])
        self.assertIn("Я понял задачу так", session["messages"][-1]["text"])
        self.assertEqual([r["key"] for r in session["task_map"]["requirements"]], ["material", "load"])
        # Every request is small and carries neither tools nor the full task-map contract.
        for request in provider.requests:
            self.assertNotIn("tools", request["body"])
            self.assertLess(len(json.dumps(request["body"]["messages"], ensure_ascii=False)), 6000)
        clarify = self.pending("step-clar-")
        session = self.finished(self.server.runner.answers(self.sid, {"request_id": "a1", "answers": [{"question_id": clarify["id"], "value": "Комнатная"}]}))
        self.assertEqual(asked, ["requirements", "clarify"], "an answer must not restart earlier steps")
        self.assertIn("Комнатная", [r["value"] for r in session["task_map"]["requirements"]])
        confirm = self.pending("step-req-")
        self.finished(self.server.runner.answers(self.sid, {"request_id": "a2", "answers": [{"question_id": confirm["id"], "value": "Верно"}]}))
        self.assertEqual(asked, ["requirements", "clarify", "material"])
        material = self.pending("step-mat-")
        self.assertEqual(material["options"][0]["label"], "Согласен")
        session = self.finished(self.server.runner.answers(self.sid, {"request_id": "a3", "answers": [{"question_id": material["id"], "value": "Согласен"}]}))
        self.assertEqual(asked, ["requirements", "clarify", "material", "summary"])
        self.assertNotIn("risk_acceptance", session["task_map"])
        self.assertEqual(session["workflow_state"], "plan_ready", session["events"][-1])
        plan = next(iter(session["plans"].values()))
        changes = {c["key"]: c for c in plan["actions"][0]["arguments"]["changes"]}
        self.assertEqual(changes["support_enable"]["value"], False)
        self.assertEqual(changes["wall_line_count"]["target"], {"scope": "extruder", "extruder_id": "extruder-1"})
        self.assertEqual(changes["layer_height"]["target"], {"scope": "global"})
        self.assertNotIn("cool_fan_speed", changes)
        self.assertTrue(any("cool_fan_speed" in l for l in plan["limitations"]))
        # Agreeing with the material only accepts its selection, never proves a hard requirement.
        # Unverified properties remain visible as limitations without a separate risk answer.
        self.assertEqual(session["task_map"]["readiness"]["unverified_requirement_ids"], [r["id"] for r in session["task_map"]["requirements"] if r["key"] == "load"])
        self.assertTrue(any("не подтверждает" in line for line in plan["limitations"]))
        self.assertIn("result.gcode", session["messages"][-1]["text"])
        self.assertEqual(self.bridge.mutations, [])
        # The algorithm ends with G-code: approve, apply and slice, then export.
        prefix = "/api/sessions/" + self.sid + "/plans/" + plan["id"]
        self.assertEqual(self.api("POST", prefix + "/approve", {"version": plan["version"]})[0], 200)
        self.api("POST", prefix + "/apply", {"request_id": "apply-step"})
        wait_until(lambda: self.store.get(self.sid)["workflow_state"] == "result_ready")
        self.assertIn("set_settings", self.bridge.mutations)
        op = next(o for o in self.store.get(self.sid)["operations"].values() if o.get("kind") == "slice")
        exported = self.server.plans.export(self.sid, op["id"], "test-target", "export-step")
        self.assertIn("result.gcode", [f["filename"] for f in exported["files"]])

    def test_stepwise_correction_reextracts_and_agent_selects_agreed_material(self):
        call = self.bridge.call
        def call_with_petg(name, args, expected_ref=None):
            if name == "list_materials":
                return {"items": [{"id": "material-1", "extruder_id": "extruder-1", "compatibility": "pass"},
                                  {"id": "generic_petg", "extruder_id": "extruder-1", "compatibility": "pass"},
                                  {"id": "enduse_petg", "extruder_id": "extruder-1", "compatibility": "pass"}], "next_cursor": None}
            if name == "select_material":
                result = call(name, args, expected_ref)
                self.bridge.context["extruders"][0]["material"]["id"] = args["material_id"]
                return result
            return call(name, args, expected_ref)
        self.bridge.call = call_with_petg
        provider, asked = self.stepwise()
        self.finished(self.server.runner.start(self.sid, "step-2"))
        confirm = self.pending("step-req-")
        session = self.finished(self.server.runner.answers(self.sid, {"request_id": "b1", "answers": [{"question_id": confirm["id"], "value": "Нагрузка 10 кг"}]}))
        self.assertEqual(asked.count("requirements"), 2)
        self.assertIn("10", [r["value"] for r in session["task_map"]["requirements"]])
        confirm = self.pending("step-req-")
        self.finished(self.server.runner.answers(self.sid, {"request_id": "b2", "answers": [{"question_id": confirm["id"], "value": "Верно"}]}))
        material = self.pending("step-mat-")
        session = self.finished(self.server.runner.answers(self.sid, {"request_id": "b3", "answers": [{"question_id": material["id"], "value": "Согласен"}]}))
        # The slicer held material-1: agreeing lets the agent select Enduse PETG itself and finish the plan.
        self.assertEqual(self.bridge.mutations, ["select_material"])
        self.assertEqual(self.bridge.context["extruders"][0]["material"]["id"], "enduse_petg")
        self.assertNotIn("risk_acceptance", session["task_map"])
        self.assertEqual(session["workflow_state"], "plan_ready", session["events"][-1])
        self.assertEqual(next(iter(session["plans"].values()))["selected"]["material_id"], "enduse_petg")
        self.assertEqual(session["task_map"]["recommended_settings"]["material"]["basis"], "подтверждён пользователем")

    def test_accept_risk_answer_is_stored_and_restarts_analysis(self):
        self.server.runner.answers(self.sid, {"request_id": "risk1", "accept_risk": {"reason": "Проверка посадки"}})
        session = self.store.internal(self.sid)
        self.assertEqual(session["task_map"]["risk_acceptance"]["reason"], "Проверка посадки")
        self.assertIn("режим прототипа", session["messages"][-1]["text"])
        with self.assertRaises(ApiError):
            self.server.runner.answers(self.sid, {"request_id": "risk2", "accept_risk": {}})

    def test_stale_plan_manual_scene_edit_and_foreign_object(self):
        plan = self.prepare_plan()
        self.bridge.revision += 1
        with self.assertRaises(ApiError) as caught:
            self.server.plans.approve(self.sid, plan["id"], 1)
        self.assertEqual(caught.exception.code, "STALE_INPUT")
        self.store.invalidate_snapshot(self.bridge.scene()["snapshot_ref"])
        self.assertEqual(self.store.get(self.sid)["plans"][plan["id"]]["status"], "stale")
        self.bridge.objects.append({"id": "foreign", "name": "Foreign", "printable": True})
        with self.assertRaises(ApiError) as caught:
            self.prepare_plan()
        self.assertEqual(caught.exception.code, "FOREIGN_OBJECTS")

    def test_retry_429_is_bounded_and_preserves_selected_model(self):
        provider = self.configured(lambda body, count: (429, {}) if count == 1 else {"role": "assistant", "content": "ok"})
        with mock.patch("AIAssistant.agent.llm_client.time.sleep"):
            self.assertEqual(LLMClient(self.server.settings).complete([{"role": "user", "content": "test"}])["content"], "ok")
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(provider.requests[0]["body"]["model"], provider.requests[1]["body"]["model"])
        provider.responder = lambda body, count: (429, {})
        with mock.patch("AIAssistant.agent.llm_client.time.sleep"):
            with self.assertRaises(ApiError):
                LLMClient(self.server.settings).complete([{"role": "user", "content": "test"}])
        self.assertEqual(len(provider.requests), 5)

    def test_image_uses_base64_image_url_and_does_not_enter_history(self):
        from test_stage1 import PNG
        path = os.path.join(self.temp.name, "test.png")
        with open(path, "wb") as target:
            target.write(PNG)
        content = image_content({"path": path, "name": "test.png"})
        provider = self.configured(lambda body, count: {"role": "assistant", "content": "image accepted"})
        LLMClient(self.server.settings).complete([{"role": "user", "content": [content]}])
        image = provider.requests[0]["body"]["messages"][0]["content"][0]
        self.assertTrue(image["image_url"]["url"].startswith("data:image/png;base64,"))
        with open(self.store.path, encoding="utf-8") as source:
            self.assertNotIn("data:image", source.read())

    def test_structured_repair_stops_after_one_attempt(self):
        def responder(body, count):
            return {"role": "assistant", "content": "stop" if "tools" in body else "{invalid"}
        provider = self.configured(responder)
        self.import_model()
        run = self.server.runner.start(self.sid, "invalid-structured")
        wait_until(lambda: self.store.get(self.sid)["runs"][run["run_id"]]["status"] == "failed")
        self.assertEqual(len(provider.requests), 3)
        self.assertEqual(self.store.get(self.sid)["plans"], {})
        self.assertEqual(self.store.get(self.sid)["events"][-1]["code"], "INVALID_MODEL_OUTPUT")

    def test_new_message_supersedes_busy_run_and_stays_in_original_session(self):
        arrived, release = threading.Event(), threading.Event()
        def responder(body, count):
            if count == 1:
                arrived.set()
                release.wait(3)
            return {"role": "assistant", "content": "done" if "tools" in body else json.dumps(self.proposal())}
        self.configured(responder)
        self.import_model()
        first = self.server.runner.start(self.sid, "first")
        self.assertTrue(arrived.wait(3))
        other = self.store.create("Другой чат")
        self.store.message(self.sid, "new-message", "Уточнение задачи", [], self.bridge.snapshot())
        second = self.server.runner.start(self.sid, "second")
        release.set()
        wait_until(lambda: self.store.get(self.sid)["runs"][second["run_id"]]["status"] == "completed")
        self.assertEqual(self.store.get(self.sid)["runs"][first["run_id"]]["status"], "cancelled")
        self.assertEqual(self.store.get(other["id"])["messages"], [])
        self.assertTrue(all(m["run_id"] == second["run_id"] for m in self.store.get(self.sid)["messages"] if m["role"] == "assistant"))

    def test_export_rejects_old_slice_and_changed_input(self):
        plan = self.prepare_plan()
        self.server.plans.approve(self.sid, plan["id"], 1)
        self.server.plans.apply(self.sid, plan["id"])
        wait_until(lambda: self.store.get(self.sid)["workflow_state"] == "result_ready")
        operation = next(o for o in self.store.get(self.sid)["operations"].values() if o.get("kind") == "slice")
        self.bridge.revision += 1
        with self.assertRaises(ApiError) as caught:
            self.server.plans.export(self.sid, operation["id"], "target", "old-slice")
        self.assertEqual(caught.exception.code, "STALE_INPUT")
        self.assertNotIn("export_gcode", self.bridge.mutations)

    def test_event_cursor_and_connection_test_are_idempotent(self):
        self.store.event(self.sid, "test-run", "run_started")
        self.store.event(self.sid, "test-run", "run_finished")
        events = self.api("GET", "/api/sessions/" + self.sid + "/events?after=1")[1]
        self.assertEqual([e["seq"] for e in events["events"]], [2])
        self.configured(lambda body, count: {"role": "assistant", "content": "test-secret-value"})
        first = self.server.runner.connection_test(self.sid, "same-test")
        self.assertEqual(self.server.runner.connection_test(self.sid, "same-test"), first)
        wait_until(lambda: self.store.operation(self.sid, first["operation_id"])["status"] == "completed")
        self.assertNotIn("test-secret-value", json.dumps(self.store.get(self.sid)))

    def test_partial_application_never_starts_slicing(self):
        action = {"id": "a1", "tool": "set_settings", "arguments": {"changes": [{"key": "layer_height", "value": 0.2, "target": {"scope": "global"}}]},
                  "before": 0.1, "after": 0.2, "reason": "Test", "source_refs": [{"id": "source", "version": 1}]}
        plan = self.prepare_plan([action])
        self.server.plans.approve(self.sid, plan["id"], 1)
        self.bridge.fail_change = True
        self.server.plans.apply(self.sid, plan["id"])
        wait_until(lambda: self.store.get(self.sid)["workflow_state"] == "failed")
        self.assertNotIn("start_slicing", self.bridge.mutations)
        self.assertEqual(self.store.get(self.sid)["plans"][plan["id"]]["completed_actions"], [])

    def test_import_tool_requires_explicit_file_permission(self):
        self.store.edit(self.sid, lambda s: (s["files"].update({"permitted": {"id": "permitted", "name": "test.stl", "kind": "model", "path": "unused", "size": 1, "status": "queued", "object_ids": []}}),
                       s["operations"].update({"import-op": {"id": "import-op", "session_id": self.sid, "file_id": "permitted", "status": "queued"}})))
        token = "explicit-user-import"
        self.server.mcp.grant(token, {"session_id": self.sid, "phase": "importing", "import_operation_id": "import-op"})
        client = MCPClient(self.server, token)
        client.initialize()
        try:
            self.assertEqual(client.call("load_model", {"file_id": "permitted", "request_id": "import"})["status"], "imported")
            self.assertEqual(client.call("load_model", {"file_id": "permitted", "request_id": "import-again"})["object_ids"], ["node-1"])
            with self.assertRaises(ApiError):
                client.call("load_model", {"file_id": "foreign-file", "request_id": "wrong"})
        finally:
            client.close()
            self.server.mcp.revoke(token)


class StateTests(unittest.TestCase):
    def test_migration_preserves_history_and_backup_once(self):
        with tempfile.TemporaryDirectory() as root:
            store = SessionStore(root)
            sid = store.listing()["active_session_id"]
            store.message(sid, "one", "Старое описание", [], {})
            with open(store.path, encoding="utf-8") as source:
                data = json.load(source)
            data["version"] = 1
            for key in ("task_map", "runs", "events", "plans", "workflow_requests", "action_results", "workflow_state", "sources", "input_revision", "exports"):
                data["sessions"][sid].pop(key)
            with open(store.path, "w", encoding="utf-8") as target:
                json.dump(data, target)
            restored = SessionStore(root)
            with open(store.path + ".v1.bak", "rb") as source:
                original = source.read()
            self.assertEqual(restored.get(sid)["messages"][0]["text"], "Старое описание")
            self.assertEqual(SessionStore(root).get(sid)["messages"], restored.get(sid)["messages"])
            with open(store.path + ".v1.bak", "rb") as source:
                self.assertEqual(source.read(), original)
            self.assertEqual(restored.data["version"], 2)

    def test_corrupt_history_is_not_reset(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "sessions.json")
            with open(path, "w") as target:
                target.write("{invalid")
            with self.assertRaises(ValueError):
                SessionStore(root)
            with open(path) as source:
                self.assertEqual(source.read(), "{invalid")

    def test_finite_numbers_unknown_fields_and_duplicate_json_keys(self):
        for value in (float("nan"), float("inf"), True):
            with self.assertRaises(ApiError):
                validate(value, NUMBER)
        with self.assertRaises(ApiError):
            validate({"extra": 1}, obj())
        for text in ('{"x":NaN}', '{"x":1,"x":2}'):
            with self.assertRaises(ValueError):
                strict_json(text)

    def test_readiness_unknown_is_not_pass_and_assumption_is_advisory(self):
        task = new_map()
        scene = {"snapshot_ref": {"id": "s"}, "objects": [{"id": "n"}]}
        task.update(target_object_ids=["n"], snapshot_ref=scene["snapshot_ref"], candidates=[{"id": "c", "status": "selected", "requirement_checks": [], "missing_fields": []}],
                    requirements=[{"id": "r", "key": "strength", "strength": "hard", "status": "unknown"}])
        result = readiness(task, scene, {})
        self.assertEqual(result["status"], "ready_for_plan")
        self.assertEqual(result["unverified_requirement_ids"], ["r"])
        self.assertTrue(result["limitations"])
        task["requirements"] = []
        task["assumptions"] = [{"id": "a", "acceptance": "pending"}]
        self.assertEqual(readiness(task, scene, {})["status"], "ready_for_plan")
        task["assumptions"][0]["acceptance"] = "rejected"
        self.assertEqual(readiness(task, scene, {})["status"], "needs_input")
        task["assumptions"][0]["acceptance"] = "accepted"
        self.assertEqual(readiness(task, scene, {})["status"], "ready_for_plan")
        task["snapshot_ref"] = {"id": "different"}
        self.assertEqual(readiness(task, scene, {})["status"], "stale")

    def test_llm_cannot_write_facts_readiness_or_approval(self):
        proposal = {k: [] for k in COLLECTIONS}
        proposal.update(summary="test", facts=[])
        with self.assertRaises(ApiError):
            merge_proposal(new_map(), proposal, {})
        proposal.pop("facts")
        proposal["assumptions"] = [{"id": "a", "acceptance": "accepted"}]
        with self.assertRaises(ApiError):
            merge_proposal(new_map(), proposal, {})

    def test_knowledge_unknown_exclusion_conflict_and_empty_base(self):
        self.assertIsNone(condition({"field": "temperature", "op": "gte", "value": 40}, {}))
        self.assertTrue(condition({"field": "x", "op": "exists", "value": False}, {}))
        with self.assertRaises(ApiError):
            condition({"field": "x", "op": "eval", "value": "bad"}, {})
        with tempfile.TemporaryDirectory() as root:
            knowledge = Knowledge(root)
            self.assertEqual(knowledge.search("part")["items"], [])
            rule = {"id": "r1", "version": 1, "title": "Test", "status": "reviewed", "reviewed_by": "Test expert", "reviewed_at": "2026-10-24", "source": "test", "applies_when": [{"field": "temperature", "op": "gte", "value": 40}], "excluded_when": [], "required_facts": ["temperature"], "affected_decisions": ["material"], "recommendations": {"material": "A"}}
            other = dict(rule, id="r2", recommendations={"material": "B"})
            with open(os.path.join(root, "rules.json"), "w") as target:
                json.dump([rule, other], target)
            result = knowledge.search("part")
            self.assertEqual(result["items"][0]["applicability"], "unknown")
            self.assertFalse(result["items"][0]["usable_as_evidence"])
            self.assertTrue(result["conflicts"])
            self.assertEqual(knowledge.search("part", {"temperature": 10})["items"], [])
            self.assertTrue(knowledge.search("part", {"temperature": 50})["items"][0]["usable_as_evidence"])

    def test_reference_records_are_citable_but_reviewed_rules_still_need_conditions(self):
        with tempfile.TemporaryDirectory() as root:
            reference = {"id": "mat", "version": 1, "title": "PETG", "status": "reference", "reviewed_by": None, "reviewed_at": None,
                         "source": {"document": "TDS"}, "applies_when": [], "excluded_when": [], "required_facts": [],
                         "affected_decisions": ["material"], "recommendations": {}, "limits": {"hdt_0_45mpa_c": 68}}
            unsourced = dict(reference, id="bare", source={})
            with open(os.path.join(root, "materials.json"), "w") as target:
                json.dump([reference, unsourced], target)
            result = Knowledge(root).search("petg")
            usable = {row["id"]: row["usable_as_evidence"] for row in result["items"]}
            self.assertEqual(usable, {"mat": True, "bare": False})
            self.assertTrue(any(w.startswith("KNOWLEDGE_GAP") for w in result["warnings"]))
            self.assertTrue(any("status=reference" in w for w in result["warnings"]))

    def test_shipped_knowledge_base_is_valid_and_closes_the_gap(self):
        knowledge = Knowledge()
        result = knowledge.search("кронштейн PETG без поддержек", limit=50)
        ids = [row["id"] for row in result["items"]]
        self.assertIn("mat-petg", ids)
        self.assertIn("proc-load-bearing", ids)
        self.assertFalse(any(w.startswith("KNOWLEDGE_GAP") for w in result["warnings"]))
        self.assertEqual(result["conflicts"], [])

    def test_risk_acceptance_turns_unproven_requirements_into_accepted_risks(self):
        scene = {"snapshot_ref": {"id": "s"}, "objects": [{"id": "n"}]}
        task = new_map()
        task.update(target_object_ids=["n"], snapshot_ref=scene["snapshot_ref"],
                    candidates=[{"id": "c", "status": "selected", "requirement_checks": [], "missing_fields": []}],
                    requirements=[{"id": "r", "key": "strength", "strength": "hard", "status": "unknown"}],
                    unknowns=[{"id": "u-knowledge", "field": "strength", "blocking": True, "resolution": "knowledge"},
                              {"id": "u-user", "field": "environment", "blocking": True, "resolution": "user"}])
        self.assertEqual(readiness(task, scene, {})["status"], "needs_input")
        task["risk_acceptance"] = {"message_id": "m", "reason": "Прототип"}
        result = readiness(task, scene, {})
        self.assertEqual(result["status"], "needs_input")
        self.assertEqual(sorted(result["accepted_risk_ids"]), ["r", "u-knowledge"])
        self.assertEqual(result["blocking_unknown_ids"], ["u-user"])
        task["unknowns"][1]["resolved"] = True
        self.assertEqual(readiness(task, scene, {})["status"], "ready_for_plan")
        task["conflicts"] = [{"id": "x"}]
        self.assertEqual(readiness(task, scene, {})["status"], "blocked")

    def test_recommend_settings_for_bracket_and_gear(self):
        bracket = recommend("Кронштейн под нагрузку, PETG, без поддержек", {"mode": {"id": "classic"},
                            "extruders": [{"material": {"id": "enduse_petg_stereotech_ste320_0.4"}}]})
        values = {row["key"]: row["value"] for row in bracket["settings"]}
        self.assertEqual(bracket["material"]["id"], "enduse_petg")
        self.assertEqual((values["material_print_temperature"], values["material_bed_temperature"], values["cool_fan_speed"]), (235, 60, 20))
        self.assertEqual((values["wall_line_count"], values["infill_sparse_density"], values["support_enable"]), (5, 50, False))
        gear = recommend("Шестерня, рабочая температура до +70°C, температура стеклования (Tg) не ниже 85-90°C, УФ",
                         {"extruders": [{"material": {"id": "generic_pla_175"}}]})
        self.assertEqual(gear["material"]["id"], "enduse_abs")
        self.assertEqual({row["key"]: row["value"] for row in gear["settings"]}["layer_height"], 0.15)
        self.assertTrue(any("УФ" in w for w in gear["warnings"]))
        explicit = recommend("Шестерня из PETG, Tg не ниже 90 °C", None)
        self.assertEqual(explicit["material"]["id"], "enduse_petg")
        self.assertTrue(any("Подходят по температуре" in w for w in explicit["warnings"]))
        self.assertIn("Поддержки", describe(bracket))

    def test_default_candidate_prefers_current_mode_with_a_decision(self):
        task = new_map()
        task["candidates"] = [{"id": "a", "mode": "classic", "status": "possible"},
                              {"id": "b", "mode": "cylindrical_full", "status": "possible"},
                              {"id": "c", "mode": "cylindrical_full", "status": "possible"}]
        task["decision_proposals"] = [{"candidate_id": "a"}, {"candidate_id": "c"}]
        self.assertEqual(select_default_candidate(task, "cylindrical_full"), "c")
        self.assertEqual(select_default_candidate(task, "cylindrical_full"), None)
        task["candidates"][2]["status"] = "possible"
        self.assertEqual(select_default_candidate(task, "cylindrical"), "a")

    def test_endpoint_exactly_once_and_images_are_not_model_dumps(self):
        self.assertEqual(endpoint("https://example.test/v1/"), "https://example.test/v1/chat/completions")
        self.assertEqual(endpoint("https://example.test/v1/chat/completions/"), "https://example.test/v1/chat/completions")
        for value in ("file:///secret", "https://key:secret@example.test", "https://example.test?key=secret"):
            with self.assertRaises(ApiError):
                endpoint(value)

    def test_requirement_replacement_preserves_history_and_needs_new_user_source(self):
        sources = {"old": {"version": 1, "kind": "user"}, "new": {"version": 1, "kind": "user"}}
        row = {"id": "r1", "key": "material", "value": "PLA", "unit": None, "status": "known", "strength": "hard",
               "source_refs": [{"id": "old", "version": 1}], "object_ids": [], "supersedes": None}
        proposal = {key: [] for key in COLLECTIONS}
        proposal.update(summary="test", requirements=[row])
        task = merge_proposal(new_map(), proposal, sources)
        proposal["requirements"] = [dict(row, value="PETG")]
        with self.assertRaises(ApiError):
            merge_proposal(task, proposal, sources)
        proposal["requirements"] = [dict(row, id="r2", value="PETG", supersedes="r1", source_refs=[{"id": "new", "version": 1}])]
        replaced = merge_proposal(task, proposal, sources)
        self.assertEqual(replaced["history"][0]["value"], "PLA")
        self.assertEqual(replaced["requirements"][0]["value"], "PETG")

    def test_manual_check_cannot_prove_another_requirement(self):
        task = new_map()
        scene = {"objects": [{"id": "node"}], "snapshot_ref": {"id": "snapshot"}}
        task.update(target_object_ids=["node"], snapshot_ref=scene["snapshot_ref"],
                    requirements=[{"id": "strength", "key": "strength", "strength": "hard", "status": "known"}],
                    candidates=[{"id": "c", "status": "selected", "requirement_checks": [{"requirement_id": "strength", "status": "pass", "source_refs": [{"id": "manual", "version": 1}]}]}])
        sources = {"manual": {"kind": "manual", "version": 1, "requirement_id": "other"}}
        result = readiness(task, scene, sources)
        self.assertEqual(result["status"], "ready_for_plan")
        self.assertEqual(result["unverified_requirement_ids"], ["strength"])
        sources["manual"]["requirement_id"] = "strength"
        self.assertEqual(readiness(task, scene, sources)["status"], "ready_for_plan")
        self.assertEqual(readiness(task, scene, sources)["unverified_requirement_ids"], [])


class AdapterTests(unittest.TestCase):
    def test_tool_unknown_uses_current_measurement_and_keeps_absence_unknown(self):
        task = new_map()
        task.update(target_object_ids=["n"], unknowns=[{"id": "dims", "field": "dimensions_mm", "resolution": "tool"},
                                                    {"id": "missing", "field": "strength", "resolution": "tool"}])
        ref = {"id": "current"}
        sources = {"measured": {"id": "measured", "version": 1, "kind": "tool", "tool": "get_scene_snapshot",
                               "data": {"snapshot_ref": ref, "objects": [{"id": "n", "dimensions_mm": [10, 20, 30]}]}}}
        resolve_tool_unknowns(task, sources, {"id": "old"})
        self.assertFalse(task["unknowns"][0].get("resolved", False))
        resolve_tool_unknowns(task, sources, ref)
        self.assertTrue(task["unknowns"][0]["resolved"])
        self.assertFalse(task["unknowns"][1].get("resolved", False))

    def test_abandoned_qt_command_never_executes_later(self):
        from AIAssistant.slicer_bridge import SlicerBridge
        bridge = SlicerBridge.__new__(SlicerBridge)
        bridge.lock, bridge.commands, bridge.callbacks = threading.RLock(), deque(), deque()
        bridge.api = mock.Mock()
        done = threading.Event()
        bridge.commands.append({"name": "set_settings", "arguments": {}, "expected": None, "done": done, "abandoned": True})
        bridge._commands()
        self.assertTrue(done.is_set())
        bridge.api.dispatch.assert_not_called()

    def test_native_writers_cannot_be_dispatched_synchronously_on_qt(self):
        from AIAssistant.slicer_api import SlicerAPI
        bridge = mock.Mock()
        api = SlicerAPI(bridge)
        for name in ("save_project", "export_gcode"):
            with self.assertRaises(ApiError):
                api.dispatch(name, {"session_id": "sid", "export_target_id": "target"})
        bridge.app.getWorkspaceFileHandler.assert_not_called()
        bridge.app.getMeshFileHandler.assert_not_called()


if __name__ == "__main__":
    unittest.main()
