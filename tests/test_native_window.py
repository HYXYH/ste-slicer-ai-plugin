"""Native UI checks use offscreen Qt, a local test provider and a fake slicer."""
import json
import os
import sys
import time
import unittest

import test_workflow as workflow
from test_stage1 import STL
from AIAssistant.native_view import session_document, analytics_document

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PyQt5.QtCore import QUrl, QTimer, Qt
    from PyQt5.QtGui import QFontDatabase, QFont
    from PyQt5.QtWidgets import QApplication, QDialog, QLineEdit, QLabel, QPushButton
    from AIAssistant.native_window import AssistantWindow
    HAS_QT = True
except ImportError:
    HAS_QT = False


class NativePresentationTests(unittest.TestCase):
    setUp = workflow.WorkflowTests.setUp
    tearDown = workflow.WorkflowTests.tearDown

    def test_user_markup_cannot_add_links_resources_or_actions(self):
        def seed(session):
            session["messages"].append({"role": "assistant", "text": '<img src="file:///secret"><a href="assistant:0">run</a>', "file_ids": []})
            session["task_map"]["questions"] = [{"id": "q", "text": '<b>Вопрос</b>', "reason": "Тест", "options": [{"label": '<script>run</script>'}], "allow_free_text": True}]
        self.store.edit(self.sid, seed)
        markup, actions = session_document(self.store.get(self.sid))
        self.assertNotIn('<img', markup)
        self.assertNotIn('<script>', markup)
        self.assertIn('&lt;img', markup)
        answer = next(a for a in actions.values() if a["kind"] == "answer")
        self.assertEqual(answer["payload"]["answers"][0]["value"], '<script>run</script>')
        self.assertEqual(answer["session_id"], self.sid)
        report = self.server.analytics.snapshot(30)
        report["sessions"][0]["title"] = '<img src="https://example.com">'
        self.assertNotIn('<img', analytics_document(report)[0])


@unittest.skipUnless(HAS_QT, "Install requirements-dev.txt for offscreen native Qt checks")
class NativeWindowTests(unittest.TestCase):
    configured = workflow.WorkflowTests.configured
    proposal = workflow.WorkflowTests.proposal

    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication(["native-assistant-test"])
        cls.qt.setQuitOnLastWindowClosed(False)
        # Windows' offscreen platform has no system font database. Load test fonts explicitly.
        if not QFontDatabase().families() and os.name == "nt":
            for name in ("segoeui.ttf", "seguisb.ttf", "segoeuib.ttf", "consola.ttf"):
                QFontDatabase.addApplicationFont(os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts", name))
            cls.qt.setFont(QFont("Segoe UI", 10))

    def pump(self, condition, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.qt.processEvents()
            if condition():
                return
            time.sleep(.01)
        self.fail("Native UI condition did not complete")

    def setUp(self):
        workflow.WorkflowTests.setUp(self)
        self.window = AssistantWindow(None, self.server, self.temp.name)
        self.window.present()
        self.pump(lambda: self.window.session is not None)

    def tearDown(self):
        self.window.shutdown()
        self.qt.processEvents()
        workflow.WorkflowTests.tearDown(self)
        self.window.deleteLater()
        self.qt.processEvents()

    def click_action(self, kind):
        key = next(k for k, a in self.window.chat.actions.items() if a["kind"] == kind)
        self.window.action(self.window.chat, QUrl("assistant:" + key))

    def screenshot(self, filename):
        root = os.environ.get("ASSISTANT_NATIVE_SCREENSHOT_DIR")
        if root:
            os.makedirs(root, exist_ok=True)
            self.qt.processEvents()
            self.assertTrue(self.window.grab().save(os.path.join(root, filename)))

    def test_native_upload_analyze_approve_export_and_analytics(self):
        self.configured(lambda body, count: {"role": "assistant", "content": json.dumps(self.proposal())})
        path = os.path.join(self.temp.name, "Проверочная шестерня.stl")
        with open(path, "wb") as target:
            target.write(STL)
        self.window.upload([path])
        self.pump(lambda: len(self.window.session["files"]) == 1 and self.sid not in self.window.busy)
        self.window.editor.setPlainText("Проверочная шестерня. Сохранить исходные настройки и масштаб; геометрический образец.")
        self.window.send()
        self.pump(lambda: self.window.session["workflow_state"] == "plan_ready")
        self.assertIn("Проверочная шестерня.stl", self.window.chat.toPlainText())
        self.assertEqual(self.window.editor.toPlainText(), "")
        self.assertEqual(self.window.pending.count(), 0)
        self.assertEqual(self.bridge.mutations, [])
        self.screenshot("native-window-chat.png")
        self.click_action("approve")
        self.pump(lambda: self.window.session["workflow_state"] == "result_ready")
        self.assertIn("start_slicing", self.bridge.mutations)
        # Accept the UI's export dialog in this offscreen test. No native app or user files are touched.
        def accept_export():
            dialog = next(w for w in self.window.findChildren(QDialog) if w.isVisible())
            dialog.accept()
        QTimer.singleShot(10, accept_export)
        self.click_action("export")
        self.pump(lambda: bool(self.window.session["exports"]))
        self.assertEqual(self.bridge.mutations[-2:], ["save_project", "export_gcode"])
        self.window.analytics()
        self.pump(lambda: self.window.report is not None)
        self.assertEqual(self.window.report["summary"]["saved_bundles"], 1)
        self.screenshot("native-window-analytics.png")
        self.window.search.setText("несуществующая задача")
        self.assertIn("Нет задач по выбранным условиям", self.window.analytics_view.toPlainText())

    def test_close_reopen_preserves_window_drafts_and_does_not_stop_server(self):
        self.window.editor.setPlainText("Черновик первой задачи")
        original = self.current_session()
        self.window.new_session()
        self.pump(lambda: self.window.current != original and self.window.session is not None)
        self.window.editor.setPlainText("Другой черновик")
        self.window.select(original)
        self.pump(lambda: self.window.session is not None)
        self.assertEqual(self.window.editor.toPlainText(), "Черновик первой задачи")
        self.window.resize(1040, 740)
        self.window.close()
        self.assertFalse(self.window.isVisible())
        self.assertFalse(self.window.client.stopped)
        self.assertTrue(self.server.thread.is_alive())
        self.window.present()
        self.assertEqual(self.window.width(), 1040)
        self.assertEqual(self.window.editor.toPlainText(), "Черновик первой задачи")
        self.assertTrue(self.window.timer.isActive())

    def current_session(self):
        return self.window.current

    def test_bad_token_fails_and_unknown_document_links_do_nothing(self):
        self.window.client.token = "wrong-token"
        errors = []
        values = []
        self.window.client.call("/api/settings/llm", values.append, failure=errors.append)
        self.pump(lambda: bool(errors))
        self.assertEqual(values, [])
        before = self.store.get(self.sid)["revision"]
        self.window.action(self.window.chat, QUrl("https://example.com"))
        self.window.action(self.window.chat, QUrl("assistant:9999"))
        self.assertEqual(self.store.get(self.sid)["revision"], before)
        self.assertFalse(self.window.chat.openExternalLinks())
        self.assertFalse(self.window.chat.openLinks())

    def test_connection_form_confirms_saved_key_without_returning_secret(self):
        self.configured(lambda body, count: {"role": "assistant", "content": "Да"})
        captured = []
        def fill_form():
            dialog = next(w for w in self.window.findChildren(QDialog) if w.isVisible())
            key = next(w for w in dialog.findChildren(QLineEdit) if w.echoMode() == QLineEdit.Password)
            key.setText("native-test-secret")
            next(w for w in dialog.findChildren(QPushButton) if w.text() == "Сохранить").click()
            def saved():
                if key.isEnabled() and not key.text():
                    captured.extend(w.text() for w in dialog.findChildren(QLabel))
                    dialog.accept()
                else:
                    QTimer.singleShot(10, saved)
            QTimer.singleShot(10, saved)
        QTimer.singleShot(10, fill_form)
        # Bound the modal test even if the request unexpectedly fails.
        deadline = QTimer(self.window)
        deadline.setSingleShot(True)
        deadline.timeout.connect(lambda: [d.reject() for d in self.window.findChildren(QDialog) if d.isVisible()])
        deadline.start(4000)
        self.window.connection_dialog(self.server.settings.public())
        deadline.stop()
        self.assertIn("Ключ сохранён в STE Slicer", captured)
        self.assertEqual(self.server.settings.private()["api_key"], "native-test-secret")
        self.assertNotIn("native-test-secret", json.dumps(self.server.settings.public()))


if __name__ == "__main__":
    unittest.main()
