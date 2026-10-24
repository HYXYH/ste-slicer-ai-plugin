"""Generation controls are tested without contacting an external provider."""
import io
import json
import socket
import unittest
from unittest import mock

import test_workflow as workflow
from AIAssistant.agent.llm_client import LLMClient, generation_options, HTTP_TIMEOUT
from AIAssistant.session import ApiError


class Settings:
    def private(self):
        return {"base_url": "https://api.timeweb.ai/v1", "model": "dashscope/qwen3.8-flash", "api_key": "test-only"}


class GenerationPolicyTests(unittest.TestCase):
    def test_extensions_are_scoped_to_exact_provider_and_model(self):
        for url, model in [("https://other.example/v1", "dashscope/qwen3.8-flash"),
                           ("https://api.timeweb.ai.evil.example/v1", "dashscope/qwen3.8-flash"),
                           ("https://api.timeweb.ai/v1", "openai/test-model"),
                           ("https://api.timeweb.ai/v1", "dashscope/qwen-other")]:
            self.assertEqual(generation_options(url, model, True), {})
        options = generation_options("https://api.timeweb.ai/v1/chat/completions", "dashscope/qwen3.8-flash")
        self.assertFalse(options["enable_thinking"])
        self.assertEqual(options["max_completion_tokens"], 8192)
        self.assertNotIn("response_format", options)

    def test_wire_controls_and_only_aggregate_reasoning_metrics(self):
        raw = {"choices": [{"message": {"role": "assistant", "content": "{}", "reasoning_content": "private-test-reasoning"}}],
               "usage": {"prompt_tokens": 10, "completion_tokens": 7, "completion_tokens_details": {"reasoning_tokens": 4}}}
        metrics = []
        client = LLMClient(Settings(), metrics.append)
        response = io.BytesIO(json.dumps(raw).encode("utf-8"))
        response.getcode = lambda: 200
        client.opener = mock.Mock()
        client.opener.open.return_value = response
        client.complete([{"role": "user", "content": "Return JSON"}], structured=True)
        request = client.opener.open.call_args[0][0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertFalse(payload["enable_thinking"])
        self.assertEqual(payload["max_completion_tokens"], 8192)
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(metrics[0]["reasoning_tokens"], 4)
        self.assertTrue(metrics[0]["reasoning_present"])
        self.assertNotIn("private-test-reasoning", json.dumps(metrics))
        self.assertNotIn("test-only", json.dumps(metrics))

    def test_timeout_respects_remaining_analysis_budget_and_is_not_retried(self):
        metrics = []
        client = LLMClient(Settings(), metrics.append)
        client.opener = mock.Mock()
        client.opener.open.side_effect = socket.timeout()
        with mock.patch("AIAssistant.agent.llm_client.time.monotonic", return_value=100):
            with self.assertRaises(ApiError) as caught:
                client.complete([], deadline=130)
        self.assertEqual(caught.exception.code, "LLM_TIMEOUT")
        self.assertEqual(client.opener.open.call_count, 1)
        self.assertEqual(client.opener.open.call_args[1]["timeout"], 30)
        self.assertEqual(metrics[0]["code"], "LLM_TIMEOUT")
        self.assertEqual(HTTP_TIMEOUT, 300)


class DirectDecisionTests(unittest.TestCase):
    setUp = workflow.WorkflowTests.setUp
    tearDown = workflow.WorkflowTests.tearDown
    configured = workflow.WorkflowTests.configured
    import_model = workflow.WorkflowTests.import_model
    proposal = workflow.WorkflowTests.proposal

    def test_valid_first_map_uses_one_request_and_keeps_fresh_scene_check(self):
        provider = self.configured(lambda body, count: {"role": "assistant", "content": json.dumps(self.proposal())})
        self.import_model()
        run = self.server.runner.start(self.sid, "direct-map")
        workflow.wait_until(lambda: self.store.get(self.sid)["runs"][run["run_id"]]["status"] == "completed")
        session = self.store.get(self.sid)
        self.assertEqual(len(provider.requests), 1)
        self.assertTrue(session["plans"])
        self.assertEqual(self.bridge.mutations, [])
        scenes = [e for e in session["events"] if e["type"] == "tool_finished" and e.get("tool") == "get_scene_snapshot"]
        self.assertEqual(len(scenes), 2)

    def test_valid_first_map_cannot_skip_stale_scene_or_forge_approval(self):
        def responder(body, count):
            self.bridge.revision += 1
            return {"role": "assistant", "content": json.dumps(self.proposal())}
        provider = self.configured(responder)
        self.import_model()
        run = self.server.runner.start(self.sid, "direct-stale")
        workflow.wait_until(lambda: self.store.get(self.sid)["runs"][run["run_id"]]["status"] in ("completed", "failed"))
        session = self.store.get(self.sid)
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(session["task_map"]["readiness"]["status"], "stale")
        self.assertEqual(session["plans"], {})
        self.assertEqual(self.bridge.mutations, [])


if __name__ == "__main__":
    unittest.main()
