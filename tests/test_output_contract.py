"""Exercise contract failures that a cooperative fixed mock cannot reveal."""
import copy
import io
import json
import unittest
from unittest import mock
from urllib.error import HTTPError

import test_workflow as workflow
from AIAssistant.agent.contracts import validate
from AIAssistant.agent.llm_client import LLMClient, generation_options, schema_unsupported
from AIAssistant.agent.output_contract import proposal_schema, provider_schema
from AIAssistant.agent.task_state import COLLECTIONS, ROW_SCHEMAS


class Settings:
    def private(self):
        return {"base_url": "https://api.timeweb.ai/v1", "model": "dashscope/qwen3.8-flash", "api_key": "test-only"}


def response(content="{}"):
    result = io.BytesIO(json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]}).encode("utf-8"))
    result.getcode = lambda: 200
    return result


class SchemaTests(unittest.TestCase):
    def test_schema_comes_from_server_rows_and_keeps_server_owned_state_out(self):
        original = copy.deepcopy(ROW_SCHEMAS)
        schema = proposal_schema()
        self.assertEqual(set(schema["required"]), set(COLLECTIONS + ("summary",)))
        for name in COLLECTIONS:
            self.assertEqual(schema["properties"][name]["items"]["required"], ROW_SCHEMAS[name]["required"])
            self.assertEqual(schema["properties"][name]["items"]["properties"]["source_refs"]["minItems"], 1)
        self.assertNotIn("approval", schema["properties"])
        self.assertNotIn("facts", schema["properties"])
        wire = provider_schema(schema)
        self.assertNotIn("uniqueItems", json.dumps(wire))
        self.assertIn("uniqueItems", json.dumps(schema))
        self.assertEqual(ROW_SCHEMAS, original)

    def test_source_ids_and_versions_are_correlated_without_raw_evidence(self):
        schema = proposal_schema({"source-a": {"version": 3, "data": "private-evidence"}, "source-b": {"version": 7}})
        pairs = schema["$defs"]["source_ref"]["oneOf"]
        self.assertEqual([(p["properties"]["id"]["enum"], p["properties"]["version"]["enum"]) for p in pairs],
                         [(["source-a"], [3]), (["source-b"], [7])])
        self.assertEqual(schema["properties"]["candidates"]["items"]["properties"]["source_refs"]["items"], {"$ref": "#/$defs/source_ref"})
        self.assertNotIn("private-evidence", json.dumps(schema))

    def test_known_provider_receives_schema_but_metrics_do_not_copy_it(self):
        schema = proposal_schema({"source-a": {"version": 1}})
        metrics = []
        client = LLMClient(Settings(), metrics.append)
        client.opener = mock.Mock()
        client.opener.open.return_value = response()
        client.complete([], structured=schema)
        payload = json.loads(client.opener.open.call_args[0][0].data.decode("utf-8"))
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertTrue(payload["response_format"]["json_schema"]["strict"])
        self.assertNotIn("uniqueItems", json.dumps(payload["response_format"]))
        self.assertNotIn("source-a", json.dumps(metrics))
        self.assertEqual(metrics[0]["generation_options"]["response_format"], {"type": "json_schema"})

    def test_only_explicit_unsupported_schema_allows_one_object_fallback(self):
        body = json.dumps({"error": {"message": "response_format json_schema is not supported"}}).encode("utf-8")
        metrics = []
        client = LLMClient(Settings(), metrics.append)
        client.opener = mock.Mock()
        client.opener.open.side_effect = [HTTPError("https://api.timeweb.ai/v1", 400, "Bad request", {}, io.BytesIO(body)), response()]
        client.complete([], structured=proposal_schema())
        payloads = [json.loads(c[0][0].data.decode("utf-8")) for c in client.opener.open.call_args_list]
        self.assertEqual([p["response_format"]["type"] for p in payloads], ["json_schema", "json_object"])
        self.assertEqual(metrics[0]["code"], "LLM_FORMAT_UNSUPPORTED")
        for code in ("wrong API key", "max_completion_tokens is not supported", "invalid schema field"):
            self.assertFalse(schema_unsupported(json.dumps({"error": {"message": code}}).encode("utf-8")))
        self.assertFalse(schema_unsupported(b"not-json"))

    def test_images_keep_object_mode_with_local_contract_validation(self):
        client = LLMClient(Settings())
        client.opener = mock.Mock()
        client.opener.open.return_value = response()
        client.complete([{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}], structured=proposal_schema())
        payload = json.loads(client.opener.open.call_args[0][0].data.decode("utf-8"))
        self.assertEqual(payload["response_format"], {"type": "json_object"})


class RepairTests(unittest.TestCase):
    setUp = workflow.WorkflowTests.setUp
    tearDown = workflow.WorkflowTests.tearDown
    configured = workflow.WorkflowTests.configured
    import_model = workflow.WorkflowTests.import_model
    proposal = workflow.WorkflowTests.proposal

    def test_missing_field_reports_its_path_and_repairs_initial_map_once(self):
        def responder(body, count):
            proposal = self.proposal()
            validate(proposal, proposal_schema())
            if count == 1:
                del proposal["candidates"][0]["extruder_id"]
            else:
                self.assertIn("candidates[0]", body["messages"][-1]["content"])
                self.assertIn("extruder_id", body["messages"][-1]["content"])
            return {"role": "assistant", "content": json.dumps(proposal)}
        provider = self.configured(responder)
        self.import_model()
        run = self.server.runner.start(self.sid, "missing-field")
        workflow.wait_until(lambda: self.store.get(self.sid)["runs"][run["run_id"]]["status"] == "completed")
        session = self.store.get(self.sid)
        self.assertEqual(len(provider.requests), 2)
        self.assertIn('"additionalProperties":false', provider.requests[0]["body"]["messages"][0]["content"])
        rejected = [e for e in session["events"] if e["type"] == "model_output_rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertIn("candidates[0]", rejected[0]["message"])
        self.assertIn("extruder_id", rejected[0]["message"])
        self.assertTrue(session["plans"])
        self.assertEqual(self.bridge.mutations, [])

    def test_invented_source_is_rejected_without_silent_replacement(self):
        def responder(body, count):
            proposal = self.proposal()
            if count == 1:
                proposal["candidates"][0]["source_refs"] = [{"id": "invented-source", "version": 1}]
            return {"role": "assistant", "content": json.dumps(proposal)}
        provider = self.configured(responder)
        self.import_model()
        run = self.server.runner.start(self.sid, "bad-source")
        workflow.wait_until(lambda: self.store.get(self.sid)["runs"][run["run_id"]]["status"] == "completed")
        session = self.store.get(self.sid)
        rejected = [e for e in session["events"] if e["type"] == "model_output_rejected"]
        self.assertIn("candidates[0].source_refs[0]", rejected[0]["message"])
        self.assertIn("invented-source", provider.requests[1]["body"]["messages"][-1]["content"])
        self.assertNotIn("invented-source", session["sources"])
        self.assertEqual(self.bridge.mutations, [])

    def test_two_wrong_maps_stop_without_plan_or_mutation(self):
        def responder(body, count):
            proposal = self.proposal()
            proposal["candidates"][0]["mode"] = "5D"
            return {"role": "assistant", "content": json.dumps(proposal)}
        provider = self.configured(responder)
        self.import_model()
        run = self.server.runner.start(self.sid, "two-invalid-maps")
        workflow.wait_until(lambda: self.store.get(self.sid)["runs"][run["run_id"]]["status"] == "failed")
        session = self.store.get(self.sid)
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(len([e for e in session["events"] if e["type"] == "model_output_rejected"]), 2)
        self.assertEqual(session["plans"], {})
        self.assertEqual(self.bridge.mutations, [])

    def test_old_tool_history_does_not_enter_the_provider_source_enum(self):
        def responder(body, count):
            proposal = self.proposal()
            if count == 1:
                del proposal["candidates"][0]["extruder_id"]
            return {"role": "assistant", "content": json.dumps(proposal)}
        provider = self.configured(responder)
        self.import_model()
        self.store.edit(self.sid, lambda s: s["sources"].update({"old-" + str(i): {
            "id": "old-" + str(i), "version": 1, "kind": "tool", "tool": "get_scene_snapshot", "data": {"large": "x" * 1000}}
            for i in range(300)}))
        # Send the policy to our localhost provider only; no external connection.
        with mock.patch("AIAssistant.agent.llm_client.generation_options", side_effect=lambda url, model, structured:
                        generation_options("https://api.timeweb.ai/v1", "dashscope/qwen3.8-flash", structured)):
            run = self.server.runner.start(self.sid, "bounded-schema-sources")
            workflow.wait_until(lambda: self.store.get(self.sid)["runs"][run["run_id"]]["status"] == "completed")
        schema = provider.requests[1]["body"]["response_format"]["json_schema"]["schema"]
        serialized = json.dumps(schema)
        self.assertNotIn("old-", serialized)
        self.assertLess(len(serialized), 20000)
        self.assertEqual(len([key for key in self.store.internal(self.sid)["sources"] if key.startswith("old-")]), 300)

    def test_validation_events_never_store_the_raw_failed_response(self):
        self.configured(lambda body, count: {"role": "assistant", "content": '{"api_key":"test-secret-value", "private":"raw-failed-payload"'})
        self.import_model()
        run = self.server.runner.start(self.sid, "safe-diagnostics")
        workflow.wait_until(lambda: self.store.get(self.sid)["runs"][run["run_id"]]["status"] == "failed")
        session = self.store.get(self.sid)
        diagnostics = json.dumps([e for e in session["events"] if e["type"] == "model_output_rejected"])
        self.assertIn("JSON", diagnostics)
        self.assertNotIn("raw-failed-payload", diagnostics)
        self.assertNotIn("test-secret-value", diagnostics)


if __name__ == "__main__":
    unittest.main()
