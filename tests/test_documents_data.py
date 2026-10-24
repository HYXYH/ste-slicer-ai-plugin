"""User documents, common task data and chronological tool operations."""
import copy
import io
import json
import os
import unittest
import zipfile
from xml.sax.saxutils import escape

import test_workflow as workflow
import test_workspace as workspace
from AIAssistant.documents import extract_docx, MAX_TEXT
from AIAssistant.uploads import receive
from AIAssistant.session import ApiError, SessionStore
from AIAssistant.task_data import set_answer
from AIAssistant.activity import conversation_timeline
from AIAssistant.native_view import session_document


def docx(text="Рабочая температура: 70 °C", table=True):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        content = '<w:p><w:r><w:t>' + escape(text) + '</w:t></w:r></w:p>'
        if table:
            content += '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Нагрузка</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>75 Н·м</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
        archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>' + content + '</w:body></w:document>')
    return stream.getvalue()


class DocumentDataTests(unittest.TestCase):
    setUp = workflow.WorkflowTests.setUp
    tearDown = workflow.WorkflowTests.tearDown
    api = workflow.WorkflowTests.api
    configured = workflow.WorkflowTests.configured
    import_model = workflow.WorkflowTests.import_model
    proposal = workflow.WorkflowTests.proposal
    finished = workflow.WorkflowTests.finished
    process_bridge = workspace.WorkspaceTests.process_bridge

    def upload(self, raw=None):
        raw = docx() if raw is None else raw
        return receive(self.store, self.sid, "Техническое задание.docx", io.BytesIO(raw), len(raw))

    def test_docx_paragraphs_tables_preview_and_session_boundaries(self):
        uploaded = self.upload()
        self.assertEqual(uploaded["kind"], "document")
        self.assertIn("Нагрузка\t75 Н·м", uploaded["document_text"])
        sid = "/api/sessions/" + self.sid
        status, value, _ = self.api("GET", sid + "/files/" + uploaded["id"] + "/text")
        self.assertEqual(status, 200)
        self.assertIn("70 °C", value["text"])
        other = self.store.create()["id"]
        self.assertEqual(self.api("GET", "/api/sessions/" + other + "/files/" + uploaded["id"] + "/text")[0], 404)
        message, operations = self.store.message(self.sid, "doc-message", "Изучите ТЗ", [uploaded["id"]], {})
        self.assertEqual(operations, [])
        compact = self.store.get(self.sid, compact=True)
        self.assertNotIn("document_text", compact["files"][uploaded["id"]])
        markup, actions = session_document(self.store.get(self.sid))
        self.assertIn("Текст извлечён", markup)
        self.assertTrue(any(a["kind"] == "document" for a in actions.values()))
        self.assertFalse(any(a["kind"] == "import" for a in actions.values()))

    def test_empty_corrupt_entities_and_oversized_documents_are_explicit_errors(self):
        for raw in (b"broken zip", docx("", False), docx("x" * (MAX_TEXT + 1), False)):
            with self.assertRaises(ApiError):
                self.upload(raw)
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, "w") as archive:
            archive.writestr("word/document.xml", b'<!DOCTYPE x [<!ENTITY x "bad">]><x/>')
        with self.assertRaises(ApiError):
            self.upload(raw.getvalue())
        self.assertEqual(self.store.internal(self.sid)["files"], {})
        folder = os.path.join(self.temp.name, "uploads", self.sid)
        self.assertEqual(os.listdir(folder), [])
        with self.assertRaises(ApiError):
            receive(self.store, self.sid, "old.doc", io.BytesIO(b"old"), 3)

    def test_repository_briefs_extract_without_truncation(self):
        root = os.path.join(workflow.ROOT, "data")
        paths = [os.path.join(folder, name) for folder, _, names in os.walk(root) for name in names if name.endswith(".docx")]
        self.assertTrue(paths)
        for path in paths:
            self.assertTrue(extract_docx(path).strip(), path)

    def test_documents_reach_single_pipeline_with_source_ids(self):
        uploaded = self.upload()
        provider = self.configured(lambda body, count: {"role": "assistant", "content": json.dumps(self.proposal())})
        self.import_model()
        self.store.message(self.sid, "doc", "Требования во вложении", [uploaded["id"]], {})
        session = self.finished(self.server.runner.start(self.sid, "analyze-doc"))
        self.assertEqual(session["runs"][next(iter(session["runs"]))]["status"], "completed")
        context = json.loads(provider.requests[0]["body"]["messages"][1]["content"])
        self.assertEqual(context["documents"][0]["id"], uploaded["id"])
        self.assertIn("75 Н·м", context["documents"][0]["text"])
        self.assertEqual(session["sources"][uploaded["id"]]["file_id"], uploaded["id"])

    def test_documents_reach_stepwise_pipeline_and_credit_the_common_data_map(self):
        self.process_bridge()
        uploaded = self.upload()
        def responder(body, count):
            if "выдели из текста" in body["messages"][0]["content"]:
                content = {"summary": "Шестерня", "requirements": [
                    {"key": "part_purpose", "value": "Шестерня привода", "unit": "", "strength": "preference"},
                    {"key": "service_temperature", "value": "70", "unit": "°C", "strength": "preference"}]}
            else:
                content = {"questions": []}
            return {"role": "assistant", "content": json.dumps(content)}
        provider = self.configured(responder, "stepwise")
        self.import_model()
        self.store.message(self.sid, "doc", "Изучите ТЗ", [uploaded["id"]], {})
        session = self.finished(self.server.runner.start(self.sid, "step-doc"))
        self.assertIn("75 Н·м", provider.requests[0]["body"]["messages"][1]["content"])
        data = self.server.workspace.assessment(self.sid)
        self.assertTrue(data["fields"]["temperature"]["known"])
        self.assertTrue(any("Из ТЗ" in source for source in data["fields"]["temperature"]["sources"]))
        self.assertEqual(len(session["task_map"]["requirements"]), 2)

    def test_data_can_reach_100_without_cube_and_explicit_answers_replace_conflicts(self):
        self.process_bridge()
        self.import_model()
        values = {"purpose": "Декоративная фигурка", "load": "не применяется", "temperature": "20 °C", "environment": "помещение"}
        data = self.server.workspace.save_answers(self.sid, values)
        self.assertEqual(data["score"], 100)
        self.assertEqual(data["status"], "sufficient")
        self.assertEqual([c["maximum"] for c in data["components"]], [30, 30, 40])
        self.assertNotIn("threshold", data)
        self.assertNotIn("calibration", [c["id"] for c in data["components"]])
        def conflict(s):
            original = next(r for r in s["task_map"]["requirements"] if r["key"] == "temperature")
            s["task_map"]["requirements"].append(dict(copy.deepcopy(original), id="other-temperature", value="90 °C"))
        self.store.edit(self.sid, conflict)
        data = self.server.workspace.assessment(self.sid)
        self.assertEqual(data["score"], 90)
        self.assertEqual(data["status"], "conflict")
        data = self.server.workspace.save_answers(self.sid, {"temperature": "20 °C"})
        self.assertEqual(data["score"], 100)
        self.assertEqual(data["status"], "sufficient")
        self.assertTrue(self.store.internal(self.sid)["task_map"]["history"])
        data = self.server.workspace.save_answers(self.sid, {"load": "не знаю"})
        self.assertEqual(data["score"], 90)

    def test_actions_are_single_operations_between_messages_and_survive_restart(self):
        self.store.message(self.sid, "ask", "Прочитайте настройки", [], {})
        self.store.event(self.sid, "run", "tool_started", operation_id="op", tool="get_current_configuration", arguments={"keys": ["layer_height"]})
        self.store.event(self.sid, "run", "tool_finished", operation_id="op", tool="get_current_configuration", result={"settings": {"layer_height": .2}}, duration_ms=125)
        self.store.edit(self.sid, lambda s: s["messages"].append({"id": "reply", "role": "assistant", "created_at": "9999", "text": "Прочитано", "file_ids": []}))
        timeline = self.store.get(self.sid, compact=True)["timeline"]
        group = next(row for row in timeline if row["type"] == "activity")
        self.assertEqual(len(group["operations"]), 1)
        self.assertIn("1", group["summary"])
        self.assertIn("layer_height", group["operations"][0]["arguments"])
        self.assertEqual(timeline[-1]["id"], "reply")
        restarted = SessionStore(self.temp.name)
        self.assertEqual(restarted.get(self.sid, compact=True)["timeline"], timeline)
        markup, _ = session_document(self.store.get(self.sid), True)
        self.assertLess(markup.index("Выполнено действий"), markup.index("Прочитано"))
        self.assertNotIn("Показать историю действий", markup)
