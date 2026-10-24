"""Real Qt Quick component and local API; all slicer and provider data is synthetic."""
import json
import copy
import os
import sys
import time
import types
import unittest
from unittest import mock

import test_workflow as workflow
from test_stage1 import STL
import test_workspace as workspace_tests

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
try:
    from PyQt5.QtCore import QUrl, QObject, Qt, qInstallMessageHandler
    from PyQt5.QtWidgets import QApplication
    from PyQt5.QtGui import QFontDatabase, QFont
    from PyQt5.QtQuickWidgets import QQuickWidget
    from PyQt5.QtQml import QQmlExpression
    from PyQt5.QtTest import QTest
    from AIAssistant.assistant_controller import AssistantController
    HAS_QT = True
except ImportError:
    HAS_QT = False


@unittest.skipUnless(HAS_QT, "Install requirements-dev.txt for Qt Quick tests")
class AssistantTabTests(unittest.TestCase):
    configured = workflow.WorkflowTests.configured
    proposal = workflow.WorkflowTests.proposal
    process_bridge = workspace_tests.WorkspaceTests.process_bridge

    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication(["assistant-tab-test"])
        cls.qt.setQuitOnLastWindowClosed(False)
        if not QFontDatabase().families() and os.name == "nt":
            for name in ("segoeui.ttf", "segoeuib.ttf", "seguisb.ttf", "consola.ttf", "seguisym.ttf"):
                QFontDatabase.addApplicationFont(os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts", name))
            cls.qt.setFont(QFont("Segoe UI", 10))

    def setUp(self):
        workflow.WorkflowTests.setUp(self)
        self.process_bridge()
        self.controller = AssistantController(self.server)
        self.controller.set_active(True)
        self.widget = None
        self.messages = []
        self.handler = qInstallMessageHandler(lambda kind, context, text: self.messages.append(text))
        self.pump(lambda: self.controller.session is not None)

    def tearDown(self):
        if self.widget:
            self.widget.rootObject().setProperty("backend", None)
            self.widget.close()
            self.widget.deleteLater()
        self.controller.shutdown()
        self.qt.processEvents()
        workflow.WorkflowTests.tearDown(self)
        qInstallMessageHandler(self.handler)
        self.controller.deleteLater()
        self.qt.processEvents()

    def pump(self, condition, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.qt.processEvents()
            if condition():
                return
            time.sleep(.01)
        self.fail("Qt tab did not complete: " + self.controller.message + "\n" + "\n".join(self.messages[-5:]))

    def panel(self):
        self.widget = QQuickWidget()
        self.widget.setResizeMode(QQuickWidget.SizeRootObjectToView)
        self.widget.resize(1050, 730)
        path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "plugin", "AIAssistant", "qml", "AssistantPanel.qml"))
        self.widget.setSource(QUrl.fromLocalFile(path))
        self.assertEqual(self.widget.status(), QQuickWidget.Ready, "\n".join(e.toString() for e in self.widget.errors()))
        self.widget.rootObject().setProperty("backend", self.controller)
        self.widget.show()
        self.qt.processEvents()

    def screenshot(self, name):
        root = os.environ.get("ASSISTANT_NATIVE_SCREENSHOT_DIR")
        if root:
            QTest.qWait(100)
            self.assertTrue(self.widget.grabFramebuffer().save(os.path.join(root, name)))

    def test_real_qml_tab_upload_plan_and_analytics_keep_message_text_inert(self):
        self.configured(lambda body, count: {"role":"assistant", "content":json.dumps(self.proposal())})
        self.panel()
        path = os.path.join(self.temp.name, "Шестерня.stl")
        with open(path, "wb") as target:
            target.write(STL)
        self.controller.uploadUrls([QUrl.fromLocalFile(path)])
        self.pump(lambda: bool(self.controller.session["files"]) and self.sid not in self.controller.busy)
        self.controller.setDraft("Геометрический образец. Сохранить текущие настройки, масштаб и ориентацию. <img src='file:///secret'>")
        self.controller.send()
        self.pump(lambda: self.controller.session["workflow_state"] == "plan_ready")
        self.assertEqual(self.controller.draft, "")
        self.assertTrue(self.controller.view["planId"])
        self.assertEqual(self.bridge.mutations, [])
        self.screenshot("assistant-tab-plan.png")
        self.widget.rootObject().setProperty("modal", "chats")
        self.screenshot("assistant-tab-chats.png")
        self.widget.rootObject().setProperty("modal", "calibration")
        self.screenshot("assistant-tab-calibration.png")
        self.widget.rootObject().setProperty("modal", "")
        self.controller.applyPlan()
        self.pump(lambda: self.controller.session["workflow_state"] == "result_ready")
        self.controller.loadAnalytics(30)
        self.pump(lambda: self.controller.report is not None)
        self.screenshot("assistant-tab-analytics.png")
        self.controller.searchAnalytics("несуществующая задача")
        self.assertIn("Нет задач по выбранным условиям",self.controller.view["report"])
        self.controller.searchAnalytics("")
        # Analytics links have their own action registry and can never execute a workflow action.
        link = next(k for k, a in self.controller.analytics_actions.items() if a["kind"] == "session")
        self.controller.invokeReportLink("assistant:" + link)
        self.pump(lambda: not self.controller.analytics_mode)
        self.assertNotIn("file:///secret", "\n".join(self.messages))
        self.assertFalse(any("ReferenceError" in m or "TypeError" in m or "Cannot assign" in m or "Binding loop" in m for m in self.messages), "\n".join(self.messages))

    def test_switch_merge_and_leave_tab_preserve_drafts_and_server(self):
        self.controller.setDraft("Черновик А")
        first = self.controller.current
        self.controller.newChat()
        self.pump(lambda: self.controller.current != first and self.controller.session is not None)
        second = self.controller.current
        self.controller.setDraft("Черновик Б")
        self.controller.select(first)
        self.pump(lambda: self.controller.session is not None)
        self.assertEqual(self.controller.draft, "Черновик А")
        self.controller.mergeChats([first, second], "Объединённый чат")
        self.pump(lambda: self.controller.current not in (first, second) and self.controller.session is not None)
        self.assertEqual(len(self.store.listing()["sessions"]), 3)
        self.assertEqual(self.controller.session["plans"], {})
        self.controller.set_active(False)
        self.assertFalse(self.controller.timer.isActive())
        self.assertTrue(self.server.thread.is_alive())
        self.controller.set_active(True)
        self.assertTrue(self.controller.timer.isActive())

    def test_transient_undefined_backend_during_stage_switch_has_no_qml_warning(self):
        self.panel()
        self.controller.setDraft("Черновик при возврате из Prepare")
        for unused in range(3):
            expression = QQmlExpression(self.widget.rootContext(), self.widget.rootObject(), "backend = undefined")
            expression.evaluate()
            self.assertFalse(expression.hasError(), expression.error().toString())
            self.qt.processEvents()
            self.widget.rootObject().setProperty("backend", self.controller)
            self.controller.draftChanged.emit()
            self.qt.processEvents()
        self.assertEqual(self.controller.draft, "Черновик при возврате из Prepare")
        self.assertFalse(any("Unable to assign" in message or "TypeError" in message or "ReferenceError" in message for message in self.messages), "\n".join(self.messages))

    def test_progress_does_not_rebuild_chat_or_request_assessment_while_slicing(self):
        session = copy.deepcopy(self.controller.session)
        session["revision"] += 1
        session["workflow_state"] = "slicing"
        session["operations"]["slice"] = {"id": "slice", "kind": "slice", "status": "slicing", "progress": None}
        self.controller.receive_session(session)
        changed, progress = [], []
        self.controller.changed.connect(lambda: changed.append(True))
        self.controller.progressChanged.connect(lambda: progress.append(True))
        update = copy.deepcopy(session)
        update["operations"]["slice"]["progress"] = .48
        with mock.patch("AIAssistant.assistant_controller.session_document") as render:
            self.controller.receive_session(update)
            render.assert_not_called()
        self.assertEqual(self.controller.sliceProgress, .48)
        self.assertEqual(changed, [])
        self.assertEqual(progress, [True])
        self.controller.last_assessment = 0
        self.controller.pending.clear()
        with mock.patch.object(self.controller.client, "call") as call:
            self.controller.poll()
        paths = [row[0][0] for row in call.call_args_list]
        self.assertFalse(any(path.endswith("/assessment") for path in paths))
        self.assertIn(self.controller.path() + "/workspace", paths)

    def test_calibration_plan_requires_import_and_approval_preserves_settings(self):
        self.controller.createCalibration()
        self.pump(lambda: bool((self.controller.session or {}).get("calibration_for")))
        child = self.controller.current
        fid = next(iter(self.controller.session["files"]))
        self.controller.importFile(fid)
        self.pump(lambda: self.controller.session["files"][fid]["status"] == "imported")
        self.bridge.objects[0]["dimensions_mm"] = [20.,20.,20.]
        self.controller.analyze()
        self.pump(lambda: self.controller.session["workflow_state"] == "plan_ready")
        plan = next(iter(self.controller.session["plans"].values()))
        self.assertEqual(plan["actions"], [])
        self.assertEqual(self.bridge.mutations, [])
        self.controller.applyPlan()
        self.pump(lambda: self.controller.session["workflow_state"] == "result_ready")
        self.assertEqual(self.bridge.mutations, ["start_slicing"])
        self.controller.saveCalibration("20.1","19.95","20.0",True,True,True,True)
        self.pump(lambda: bool(self.store.internal(child).get("calibration_results")))
        self.controller.select(self.sid)
        self.pump(lambda: self.controller.assessment.get("calibration") is not None)
        self.assertEqual(self.controller.assessment["calibration"]["x_mm"],20.1)

    def test_stage_contract_exposes_native_tab_sidebar_and_lifecycle(self):
        import importlib
        class Stage(QObject):
            def __init__(self):
                super().__init__()
                self.parts = {}
            def addDisplayComponent(self, name, path):
                self.parts[name] = QUrl.fromLocalFile(path)
            def getDisplayComponent(self, name):
                return self.parts.get(name, QUrl())
            def getPluginId(self):
                return "AIAssistant"
        stage_module = types.ModuleType("UM.Stage")
        stage_module.Stage = Stage
        resources_module = types.ModuleType("UM.Resources")
        resources_module.Resources = types.SimpleNamespace(getPath=lambda kind:self.temp.name)
        callbacks = []
        app = types.SimpleNamespace(engineCreatedSignal=types.SimpleNamespace(connect=callbacks.append), ResourceTypes=types.SimpleNamespace(QmlFiles=1))
        extension = types.SimpleNamespace(app=app, controller=self.controller, ensure_started=lambda:self.controller)
        with mock.patch.dict(sys.modules, {"UM":types.ModuleType("UM"),"UM.Stage":stage_module,"UM.Resources":resources_module}):
            sys.modules.pop("AIAssistant.assistant_stage", None)
            module = importlib.import_module("AIAssistant.assistant_stage")
            stage = module.AssistantStage(extension)
            callbacks[0]()
            self.assertEqual(stage.stageId,"AIAssistant")
            self.assertTrue(stage.mainComponent.toLocalFile().endswith("AssistantStage.qml"))
            self.assertTrue(stage.sidebarComponent.toLocalFile().endswith("PrepareSidebar.qml"))
            self.assertIs(stage.assistant,self.controller)
            stage.onStageSelected()
            self.assertTrue(self.controller.timer.isActive())
            stage.onStageDeselected()
            self.assertFalse(self.controller.timer.isActive())
        sys.modules.pop("AIAssistant.assistant_stage", None)


if __name__ == "__main__":
    unittest.main()
