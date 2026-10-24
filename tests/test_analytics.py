"""Read-only analytics contracts. HTTP is real; slicer/provider data is explicit test data."""
import copy
import csv
import http.client
import io
import json
import tempfile
import unittest

import test_workflow as workflow
from test_workflow import wait_until
from AIAssistant.analytics import Analytics, usage_counts
from AIAssistant.agent.llm_client import LLMClient
from AIAssistant.session import SessionStore, ApiError

AT = "2026-10-24T18:00:00Z"
RECENT = "2026-10-24T12:00:00Z"
OLD = "2026-09-01T12:00:00Z"


class AggregationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SessionStore(self.temp.name)
        self.sid = self.store.listing()["active_session_id"]
        self.session = self.store.data["sessions"][self.sid]
        self.session.update(created_at=OLD, updated_at=RECENT)
        self.analytics = Analytics(self.store)

    def tearDown(self):
        self.temp.cleanup()

    def event(self, eid, kind, date=RECENT, **data):
        event = dict(id=eid, type=kind, created_at=date)
        event.update(data)
        self.session["events"].append(event)

    def seed_run(self, rid, status, date=RECENT):
        self.session["runs"][rid] = {"id": rid, "status": status, "created_at": "2026-10-24T11:59:00Z" if date == RECENT else date,
                                         "finished_at": date if status not in ("queued", "running") else None}

    def valid_slice(self, oid="slice-1", seconds=60, material=None):
        ref = {"process_id": self.store.run_id, "fingerprint": "test-only"}
        self.session["plans"][oid] = dict(id=oid, status="applied", input_revision=self.session["input_revision"], snapshot_ref=ref, applied_snapshot_ref=ref, approval={"test": True})
        self.session["operations"][oid] = dict(id=oid, kind="slice", status="completed", plan_id=oid, snapshot_ref=ref, created_at=RECENT,
                                            result={"time": {"value": seconds, "unit": "s"}, "material": material or {"values": [0.5], "unit": "m"}})

    def test_legacy_unknown_is_distinct_from_measured_zero_and_read_only(self):
        before = copy.deepcopy(self.store.data)
        report = self.analytics.snapshot(30, AT)
        self.assertIsNone(report["llm"]["total_tokens"])
        self.assertIsNone(report["llm"]["p50_ms"])
        self.assertIsNone(report["summary"]["estimated_print_seconds"])
        self.assertEqual(self.store.data, before)
        self.event("zero", "llm_request", attempt=1, outcome="success", usage={"total_tokens": 0}, duration_ms=0)
        report = self.analytics.snapshot(30, AT)
        self.assertEqual(report["llm"]["total_tokens"], 0)
        self.assertEqual(report["llm"]["p50_ms"], 0)
        self.assertEqual(report["llm"]["usage_reported_requests"], 1)

    def test_usage_validation_missing_partial_and_non_finite(self):
        self.assertEqual(usage_counts({"prompt_tokens": 2, "completion_tokens": 3})["total_tokens"], 5)
        self.assertIsNone(usage_counts({"prompt_tokens": 2})["total_tokens"])
        for value in (True, -1, 1.5, float("inf"), "100", 10 ** 13):
            self.assertIsNone(usage_counts({"total_tokens": value})["total_tokens"])
        self.valid_slice(seconds=float("nan"), material={"values": [1, float("inf")], "unit": "m"})
        report = self.analytics.snapshot(30, AT)
        self.assertEqual(report["summary"]["ready_results"], 1)
        self.assertIsNone(report["summary"]["estimated_print_seconds"])
        self.assertEqual(report["summary"]["estimated_material"], {})
        json.dumps(report, allow_nan=False)

    def test_period_boundaries_runs_and_timeline_do_not_change_current_state(self):
        self.seed_run("a", "completed")
        self.seed_run("b", "failed")
        self.seed_run("c", "cancelled")
        self.seed_run("d", "interrupted")
        self.seed_run("e", "running")
        self.seed_run("old", "completed", OLD)
        self.seed_run("future", "completed", "2027-01-01T00:00:00Z")
        self.event("boundary", "llm_request", date="2026-10-17T18:00:00Z", outcome="success", usage={"total_tokens": 4})
        self.event("before", "llm_request", date="2026-10-17T17:59:59Z", outcome="success", usage={"total_tokens": 8})
        self.session["workflow_state"] = "waiting_user"
        report = self.analytics.snapshot(7, AT)
        self.assertEqual(report["summary"]["runs"], 5)
        self.assertEqual(report["summary"]["analysis_completion_rate"], 50)
        self.assertEqual(report["summary"]["active_runs"], 1)
        self.assertEqual(report["runs"]["p50_ms"], 60000)
        self.assertEqual(report["llm"]["total_tokens"], 4)
        self.assertEqual(sum(d["runs"] for d in report["timeline"]), 5)
        self.assertEqual(report["sessions"][0]["workflow_state"], "waiting_user")
        self.assertLessEqual(len(self.analytics.snapshot(365, AT)["timeline"]), 31)
        self.assertEqual(self.analytics.snapshot(0, AT)["summary"]["runs"], 6)

    def test_inactive_task_excluded_but_old_task_with_new_event_included(self):
        self.session["updated_at"] = OLD
        self.assertEqual(self.analytics.snapshot(7, AT)["summary"]["tasks"], 0)
        self.event("new", "error", code="LLM_TIMEOUT")
        self.assertEqual(self.analytics.snapshot(7, AT)["summary"]["tasks"], 1)
        for value in (-1, 366, True, "7"):
            with self.assertRaises(ApiError):
                self.analytics.snapshot(value, AT)

    def test_attempts_duplicates_percentiles_and_tool_fallback(self):
        self.event("one", "llm_request", attempt=1, outcome="error", duration_ms=10)
        self.event("two", "llm_request", attempt=2, outcome="success", duration_ms=40, usage={"prompt_tokens": 2, "completion_tokens": 3})
        self.session["events"].append(copy.deepcopy(self.session["events"][-1]))
        self.event("three", "llm_request", attempt=1, outcome="success", duration_ms=100, usage={"total_tokens": 8})
        self.event("start", "tool_started", date="2026-10-24T11:59:59Z", tool="get_scene_snapshot", operation_id="op")
        self.event("end", "tool_finished", tool="get_scene_snapshot", operation_id="op", result={"private": "not in report"})
        self.event("err", "tool_failed", tool="get_scene_snapshot", operation_id="op2", duration_ms=10)
        report = self.analytics.snapshot(30, AT)
        self.assertEqual(report["llm"]["requests"], 3)
        self.assertEqual(report["llm"]["retries"], 1)
        self.assertEqual(report["llm"]["failed_requests"], 1)
        self.assertEqual(report["llm"]["total_tokens"], 13)
        self.assertEqual(report["llm"]["usage_reported_requests"], 2)
        self.assertEqual(report["llm"]["p50_ms"], 40)
        self.assertEqual(report["llm"]["p95_ms"], 100)
        self.assertEqual(report["tools"][0]["p95_ms"], 1000)
        self.assertEqual(report["tools"][0]["calls"], 2)
        self.assertEqual(report["tools"][0]["failures"], 1)
        self.assertNotIn("not in report", json.dumps(report))

    def test_only_latest_valid_slice_and_explicit_material_units(self):
        self.valid_slice("old", 120)
        self.session["operations"]["old"]["created_at"] = OLD
        self.valid_slice("new", 30, {"values": [5, 6], "unit": "g"})
        report = self.analytics.snapshot(30, AT)
        self.assertEqual(report["summary"]["estimated_print_seconds"], 30)
        self.assertEqual(report["summary"]["estimated_material"], {"g": 11})
        self.assertEqual(report["summary"]["ready_results"], 1)
        for change in (lambda: self.session["plans"]["new"].update(status="stale"),
                       lambda: self.session.update(input_revision=99),
                       lambda: self.session["plans"]["old"].update(approval=None)):
            change()
        self.assertEqual(self.analytics.snapshot(30, AT)["summary"]["ready_results"], 0)

    def test_restart_or_snapshot_mismatch_excludes_estimates(self):
        self.valid_slice()
        self.session["plans"]["slice-1"]["applied_snapshot_ref"] = {"different": True}
        self.assertEqual(self.analytics.snapshot(30, AT)["summary"]["ready_results"], 0)
        self.valid_slice()
        self.store.run_id = "new-process"
        self.assertEqual(self.analytics.snapshot(30, AT)["summary"]["ready_results"], 0)

    def test_saved_bundle_deduplicates_and_rejects_partial_stale_empty(self):
        files = [{"filename": name, "written": True, "size": 20} for name in ("project.3mf", "result.gcode")]
        good = dict(operation_id="one", files=files)
        self.session["exports"] = [good, copy.deepcopy(good), dict(operation_id="partial", files=files, partial=True),
                                   dict(operation_id="missing", files=files[:1]), dict(operation_id="stale", files=[dict(f, stale=True) for f in files]),
                                   dict(operation_id="empty", files=[dict(f, size=0) for f in files])]
        self.assertEqual(self.analytics.snapshot(30, AT)["summary"]["saved_bundles"], 1)

    def test_current_blockers_latest_knowledge_and_resolved_items(self):
        task = self.session["task_map"]
        task.update(questions=[{"answer": None}, {"answer": "yes"}], assumptions=[{"id": "irrelevant", "acceptance": "pending"}],
                    conflicts=[{"resolved": True}], unknowns=[{"blocking": True}, {"blocking": True, "resolved": True}])
        task["readiness"].update(pending_assumption_ids=["rejected-selected"], failed_check_ids=["a", "a"])
        self.session["workflow_state"] = "plan_ready"
        self.event("knowledge", "tool_finished", tool="search_knowledge", result={"data": {"warnings": ["KNOWLEDGE_GAP: test"]}})
        report = self.analytics.snapshot(30, AT)
        self.assertEqual(set(report["sessions"][0]["issues"]), {"questions", "assumptions", "checks", "unknowns", "approval", "knowledge"})
        self.assertEqual(report["summary"]["tasks_needing_attention"], 1)
        self.assertEqual(report["sessions"][0]["pending_checks"], 1)
        self.event("knowledge-fixed", "tool_finished", tool="search_knowledge", result={"data": {"warnings": []}})
        self.assertNotIn("knowledge", self.analytics.snapshot(30, AT)["sessions"][0]["issues"])

    def test_current_error_outside_period_and_backend_failure_without_error_event(self):
        self.session["workflow_state"] = "failed"
        self.assertIn("failed", self.analytics.snapshot(7, AT)["sessions"][0]["issues"])
        self.event("old-error", "error", date=OLD, code="LLM_TIMEOUT")
        report = self.analytics.snapshot(7, AT)
        self.assertIn("LLM_TIMEOUT", report["sessions"][0]["issues"])
        self.assertEqual(report["errors"], [])
        self.session["operations"]["backend-error"] = dict(id="backend-error", kind="slice", status="error")
        self.event("backend-finished", "progress", operation_id="backend-error")
        report = self.analytics.snapshot(7, AT)
        self.assertEqual(report["sessions"][0]["issues"], ["SLICE_FAILED"])
        self.assertEqual(report["errors"], [{"code": "SLICE_FAILED", "count": 1}])
        self.assertEqual(report["summary"]["tasks_needing_attention"], 1)


class AnalyticsHTTPTests(unittest.TestCase):
    setUp = workflow.WorkflowTests.setUp
    tearDown = workflow.WorkflowTests.tearDown
    api = workflow.WorkflowTests.api
    configured = workflow.WorkflowTests.configured
    import_model = workflow.WorkflowTests.import_model
    prepare_plan = workflow.WorkflowTests.prepare_plan
    proposal = workflow.WorkflowTests.proposal

    def raw(self, path, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=4)
        c.request("GET", path, headers=headers or {})
        r = c.getresponse()
        result = r.status, r.read(), dict(r.getheaders())
        c.close()
        return result

    def test_routes_period_validation_auth_and_csv_excel_safety(self):
        headers = {"X-Assistant-Token": self.server.token}
        for path in ("/api/analytics", "/api/analytics/export"):
            self.assertEqual(self.raw(path)[0], 401)
            self.assertEqual(self.raw(path, dict(headers, Origin="https://example.test"))[0], 403)
            self.assertEqual(self.raw(path, dict(headers, Host="evil.test"))[0], 403)
            for query in ("?days=-1", "?days=366", "?days=no", "?days=", "?days=7&days=30", "?unknown=7"):
                self.assertEqual(self.raw(path + query, headers)[0], 400)
        self.store.rename(self.sid, "=HYPERLINK(\"bad\");тест\nстрока")
        self.store.edit(self.sid, lambda s: s["messages"].append({"text": "raw-private-prompt"}))
        status, raw, result_headers = self.raw("/api/analytics/export?days=0", headers)
        self.assertEqual(status, 200)
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertIn("attachment", result_headers["Content-Disposition"])
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig")), delimiter=";"))
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[1][1].startswith("'=HYPERLINK"))
        self.assertNotIn(b"raw-private-prompt", raw)
        self.assertEqual(self.raw("/analytics.js")[0], 200)

    def test_wire_metrics_retries_usage_and_observer_failure_do_not_change_transport(self):
        payload = {"choices": [{"message": {"role": "assistant", "content": "ok"}}], "usage": {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12}}
        provider = self.configured(lambda body, count: (429, {}) if count == 1 else (200, payload))
        operation = self.server.runner.connection_test(self.sid, "metric-test")
        wait_until(lambda: self.store.get(self.sid)["operations"][operation["operation_id"]]["status"] == "completed")
        events = [e for e in self.store.get(self.sid)["events"] if e["type"] == "llm_request"]
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual([e["http_status"] for e in events], [429, 200])
        self.assertTrue(all(e["duration_ms"] >= 0 for e in events))
        report = self.api("GET", "/api/analytics?days=0")[1]
        self.assertEqual(report["llm"]["total_tokens"], 12)
        self.assertEqual(report["llm"]["requests"], 2)
        self.assertEqual(report["llm"]["retries"], 1)
        self.assertEqual(report["llm"]["usage_reported_requests"], 1)
        self.assertNotIn("test-secret-value", json.dumps(events))
        self.assertNotIn("messages", json.dumps(events))
        def broken_observer(metric):
            raise RuntimeError("storage unavailable")
        self.assertEqual(LLMClient(self.server.settings, broken_observer).complete([])["content"], "ok")
        self.assertEqual(len(provider.requests), 3)

    def test_invalid_response_records_failure_and_secrets_are_redacted(self):
        self.configured(lambda body, count: (200, []))
        metrics = []
        with self.assertRaises(ApiError) as error:
            LLMClient(self.server.settings, metrics.append).complete([])
        self.assertEqual(error.exception.code, "INVALID_MODEL_OUTPUT")
        self.assertEqual(metrics[0]["outcome"], "error")
        self.assertEqual(metrics[0]["code"], "INVALID_MODEL_OUTPUT")
        self.store.rename(self.sid, "test-secret-value")
        status, report, _ = self.api("GET", "/api/analytics")
        self.assertEqual(status, 200)
        self.assertNotIn("test-secret-value", json.dumps(report))
        raw = self.raw("/api/analytics/export", {"X-Assistant-Token": self.server.token})[1]
        self.assertNotIn(b"test-secret-value", raw)

    def test_analysis_persists_metrics_and_tool_durations_without_mutations(self):
        self.configured(lambda body, count: {"role": "assistant", "content": "stop" if "tools" in body else "{invalid"})
        self.import_model()
        run = self.server.runner.start(self.sid, "analytics-analysis")
        wait_until(lambda: self.store.get(self.sid)["runs"][run["run_id"]]["status"] == "failed")
        report = self.api("GET", "/api/analytics")[1]
        self.assertEqual(report["summary"]["failed_runs"], 1)
        self.assertEqual(report["llm"]["requests"], 3)
        self.assertIsNone(report["llm"]["total_tokens"])
        self.assertTrue(all(t["p50_ms"] is not None for t in report["tools"]))
        self.assertIn("knowledge", report["sessions"][0]["issues"])
        self.assertEqual(self.bridge.mutations, [])

    def test_apply_export_and_failed_mutation_are_measured_once(self):
        plan = self.prepare_plan()
        self.server.plans.approve(self.sid, plan["id"], 1)
        self.server.plans.apply(self.sid, plan["id"])
        wait_until(lambda: self.store.get(self.sid)["workflow_state"] == "result_ready")
        operation = next(o for o in self.store.get(self.sid)["operations"].values() if o.get("kind") == "slice")
        path = "/api/sessions/" + self.sid + "/exports"
        body = {"operation_id": operation["id"], "export_target_id": "test-target", "request_id": "export-metric"}
        exported = self.api("POST", path, body)[1]
        self.assertEqual(self.api("POST", path, body)[1], exported)
        wait_until(lambda: self.store.get(self.sid)["operations"][exported["operation_id"]]["status"] == "completed")
        report = self.api("GET", "/api/analytics")[1]
        tools = {t["name"]: t for t in report["tools"]}
        for name in ("start_slicing", "save_project", "export_gcode"):
            self.assertEqual(tools[name]["calls"], 1)
            self.assertIsNotNone(tools[name]["p50_ms"])
        self.assertEqual(report["summary"]["ready_results"], 1)
        self.assertEqual(report["summary"]["saved_bundles"], 1)
        action = {"id": "a1", "tool": "set_settings", "arguments": {"changes": [{"key": "layer_height", "value": 0.2, "target": {"scope": "global"}}]},
                  "before": 0.1, "after": 0.2, "reason": "Test", "source_refs": [{"id": "source", "version": 1}]}
        failed = self.prepare_plan([action])
        self.server.plans.approve(self.sid, failed["id"], 1)
        self.bridge.fail_change = True
        self.server.plans.apply(self.sid, failed["id"])
        wait_until(lambda: self.store.get(self.sid)["workflow_state"] == "failed")
        report = self.api("GET", "/api/analytics")[1]
        failed_tool = next(t for t in report["tools"] if t["name"] == "set_settings")
        self.assertEqual(failed_tool["calls"], 1)
        self.assertEqual(failed_tool["failures"], 1)
        self.assertIsNotNone(failed_tool["p50_ms"])
        self.assertEqual(self.bridge.mutations.count("start_slicing"), 1)


if __name__ == "__main__":
    unittest.main()
