"""State and actions shared by the in-slicer QML tab. All HTTP calls are asynchronous."""
import copy
import json
import os
import time
from urllib.parse import quote

from PyQt5.QtCore import QObject, QTimer, QUrl, pyqtProperty, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import QFileDialog, QDialog, QVBoxLayout, QLabel, QPushButton, QPlainTextEdit
from .native_window import LocalClient, AssistantWindow, request_id
from .native_view import FLOW, TOOLS, SETTINGS, session_document, analytics_document, human
from .user_view import field_label
from .analytics_view import analytics_projection

PARAMETERS = {key: label for key, (label, unit) in SETTINGS.items()}


class AssistantController(QObject):
    changed = pyqtSignal()
    draftChanged = pyqtSignal()
    progressChanged = pyqtSignal()

    def __init__(self, server, parent=None):
        super().__init__(parent)
        self.server = server
        self.client = LocalClient(server, self)
        self.current, self.session = None, None
        self.listing, self.context, self.assessment, self.config = [], {}, {}, {}
        self.report = None
        self.analytics_actions, self.config_version = {}, 0
        self.report_search, self.report_filter = "", "all"
        self.report_issue = ""
        self.drafts, self.busy, self.pending = {}, set(), set()
        self.message, self.prompt, self.actions = "", None, {}
        self.history, self.analytics_mode, self.disposed = False, False, False
        self.chats_mode, self.chats_return_id = False, None
        self.expanded_activity = set()
        self.expanded_requirements = set()
        self.days, self.last_report, self.last_assessment = 30, 0, 0
        self._slice_progress, self._document_key, self._document = -1., None, ("", {})
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.poll)
        self._view = {}
        self.update_buttons()

    @pyqtProperty('QVariantMap', notify=changed)
    def view(self):
        return self._view

    @pyqtProperty(str, notify=draftChanged)
    def draft(self):
        return self.draft_for(self.current)["text"] if self.current else ""

    @pyqtProperty(float, notify=progressChanged)
    def sliceProgress(self):
        return self._slice_progress

    def receive_progress(self, session):
        operations = [row for row in session.get("operations", {}).values() if row.get("kind") == "slice" and row["status"] == "slicing"]
        operation = max(operations, key=lambda row: row.get("created_at", "")) if operations else {}
        value = operation.get("progress")
        value = max(0., min(1., float(value))) if value is not None else -1.
        if value != self._slice_progress:
            self._slice_progress = value
            self.progressChanged.emit()

    @pyqtSlot(str)
    def setDraft(self, value):
        if self.current:
            self.draft_for(self.current).update(text=value[:20000], request=None)

    def draft_for(self, sid):
        return self.drafts.setdefault(sid, {"text": "", "files": [], "request": None, "initialized": False})

    def path(self, sid=None):
        return "/api/sessions/" + quote(sid or self.current, safe="")

    def set_active(self, active):
        if active:
            self.timer.start()
            if not self.current:
                self.client.call("/api/sessions", self.boot)
            self.poll()
        else:
            self.timer.stop()

    def boot(self, value):
        self.listing = value["sessions"]
        self.select(value["active_session_id"])

    def notice(self, message):
        self.message = message
        self.update_buttons()

    def shutdown(self):
        self.disposed = True
        self.timer.stop()
        self.client.stop()

    @pyqtSlot(str)
    def select(self, sid):
        if not sid:
            return
        self.current, self.session = sid, None
        self.receive_progress({})
        self.assessment, self.prompt = {}, None
        self.analytics_mode, self.chats_mode, self.history = False, False, False
        self.last_assessment = 0
        self.draftChanged.emit()
        self.update_buttons()
        self.client.call(self.path(sid) + "/workspace", self.receive_session, "POST", {})
        self.poll()

    def receive_session(self, session):
        if session["id"] != self.current or self.session and session["revision"] < self.session["revision"]:
            return
        self.receive_progress(session)
        same_revision = self.session and session["revision"] == self.session["revision"]
        self.session = session
        if same_revision:
            return
        if session.get("workflow_state") not in ("applying", "slicing", "exporting", "waiting_import"):
            self.last_assessment = 0
        d = self.draft_for(self.current)
        if not d["initialized"]:
            used = set(fid for m in session["messages"] for fid in m.get("file_ids", []))
            d["files"] = [fid for fid in session["files"] if fid not in used]
            d["initialized"] = True
        self.update_buttons()

    def poll(self):
        if self.disposed:
            return
        routes = [("/api/context", self.receive_context), ("/api/sessions", self.receive_listing)]
        if self.current:
            sid = self.current
            routes.append((self.path(sid) + "/workspace", self.receive_session))
            changing = (self.session or {}).get("workflow_state") in ("applying", "slicing", "exporting", "importing", "waiting_import")
            if not changing and not self.analytics_mode and not self.chats_mode and time.monotonic() - self.last_assessment > 10:
                routes.append((self.path(sid) + "/assessment", lambda value, sid=sid: self.receive_assessment(sid, value)))
        if self.analytics_mode and time.monotonic() - self.last_report > 10:
            self.loadAnalytics(self.days)
        for path, callback in routes:
            if path in self.pending:
                continue
            self.pending.add(path)
            def success(value, path=path, callback=callback):
                self.pending.discard(path)
                callback(value)
            def failure(error, path=path):
                self.pending.discard(path)
                self.notice(error)
            self.client.call(path, success, failure=failure)

    def receive_context(self, value):
        self.context = value
        self.update_buttons()

    def receive_listing(self, value):
        self.listing = value["sessions"]
        self.update_buttons()

    def receive_assessment(self, sid, value):
        if self.current == sid:
            if self.session and value.get("session_revision", 0) < self.session["revision"]:
                self.last_assessment = 0
                return
            self.assessment = value
            self.last_assessment = time.monotonic()
            self.update_buttons()

    def update_buttons(self):
        session = self.session or {}
        task = session.get("task_map") or {}
        messages = []
        for old in session.get("timeline", session.get("messages", [])):
            row = dict(old)
            row["expanded"] = (session.get("id"), old.get("id")) in self.expanded_activity
            row["requirementsExpanded"] = (session.get("id"), old.get("id")) in self.expanded_requirements
            row["files"] = [session["files"][fid] for fid in old.get("file_ids", []) if fid in session["files"]]
            messages.append(row)
        document_key = (session.get("id"), session.get("revision"), self.history)
        if self._document_key != document_key:
            self._document = session_document(session, self.history, False) if session else ("", {})
            self._document_key = document_key
        workflow, self.actions = self._document
        plans = sorted(session.get("plans", {}).values(), key=lambda p: p.get("created_at", ""))
        plan = next((p for p in reversed(plans) if p["status"] in ("validated", "approved", "applying", "applied")), None)
        actionable_plan = plan if plan and plan["status"] in ("validated", "approved") else None
        params = []
        if plan:
            choice = plan["selected"]
            material = next((row["material"].get("name") or choice["material_id"] for row in self.context.get("extruders", []) if (row.get("material") or {}).get("id") == choice["material_id"]), choice["material_id"])
            profile_context = self.context.get("profile") or {}
            profile = profile_context.get("name") if choice["profile_id"] in [(profile_context.get(kind) or {}).get("id") for kind in ("quality", "custom")] else choice["profile_id"]
            params = [{"name": "Режим", "value": {"classic": "3D", "cylindrical": "5D Spiral", "cylindrical_full": "5D"}.get(choice["mode"], choice["mode"])},
                      {"name": "Материал", "value": material}, {"name": "Профиль", "value": profile or choice["profile_id"]}]
            for action in plan["actions"]:
                if action["tool"] == "set_settings":
                    params.extend({"name": PARAMETERS.get(c["key"], field_label(c["key"])), "value": human(c["value"]) + (" " + SETTINGS[c["key"]][1] if c["key"] in SETTINGS and SETTINGS[c["key"]][1] else "")} for c in action["arguments"]["changes"])
        runs = session.get("runs", {})
        state = session.get("workflow_state", "idle")
        step = 0 if not messages else 1 if state in ("idle", "analyzing", "waiting_user", "queued", "failed") else 2 if state == "plan_ready" else 3 if state in ("applying", "slicing", "result_ready") else 4
        report, self.analytics_actions = analytics_document(self.report, self.report_search, self.report_filter) if self.report else ("", {})
        view = {"sessionId": self.current or "", "title": session.get("title", "AI Print Assistant"), "sessions": self.listing,
                      "messages": messages, "workflow": workflow, "status": FLOW.get(state, state), "state": state, "step": step,
                      "busy": self.current in self.busy, "active": any(r["status"] in ("queued", "running") for r in runs.values()),
                      "pending": [session["files"][fid] for fid in self.draft_for(self.current)["files"] if fid in session.get("files", {})] if self.current else [],
                      "notice": self.message, "context": {k: v for k, v in self.context.items() if k != "updated_at"}, "params": params,
                      "planId": actionable_plan["id"] if actionable_plan else "", "planVersion": actionable_plan["version"] if actionable_plan else 0,
                      "assessment": self.assessment, "analytics": self.analytics_mode, "chats": self.chats_mode, "report": report,
                      "analyticsData": analytics_projection(self.report, self.report_search, self.report_filter, self.report_issue),
                      "analyticsFilter": self.report_filter, "analyticsSearch": self.report_search, "analyticsDays": self.days, "analyticsIssue": self.report_issue,
                      "calibrationFor": session.get("calibration_for", {}), "config": self.config, "configVersion": self.config_version, "prompt": self.prompt or {}}
        if self._view != view:
            self._view = view
            self.changed.emit()

    @pyqtSlot()
    def newChat(self):
        self.client.call("/api/sessions", lambda value: self.select(value["id"]), "POST", {})

    @pyqtSlot(str)
    def rename(self, title):
        self.client.call(self.path() + "/workspace", self.receive_session, "PATCH", {"title": title})

    @pyqtSlot()
    def showChats(self):
        self.chats_return_id = self.current
        self.chats_mode, self.analytics_mode = True, False
        self.client.call("/api/sessions", self.receive_listing)
        self.update_buttons()

    @pyqtSlot()
    def closeChats(self):
        ids = [row["id"] for row in self.listing]
        sid = self.chats_return_id if self.chats_return_id in ids else self.current if self.current in ids else ids[0] if ids else None
        if sid and sid != self.current:
            self.select(sid)
        elif sid:
            self.chats_mode = False
            self.update_buttons()
        else:
            self.newChat()

    @pyqtSlot(str, str)
    def renameChat(self, sid, title):
        def saved(value):
            if sid == self.current:
                self.receive_session(value)
            self.client.call("/api/sessions", self.receive_listing)
        self.client.call(self.path(sid) + "/workspace", saved, "PATCH", {"title": title})

    @pyqtSlot(str)
    def deleteChat(self, sid):
        if sid in self.busy:
            return
        self.busy.add(sid)
        def failed(error):
            self.busy.discard(sid)
            self.notice(error)
        def deleted(value):
            self.busy.discard(sid)
            self.drafts.pop(sid, None)
            self.expanded_activity = {key for key in self.expanded_activity if key[0] != sid}
            self.expanded_requirements = {key for key in self.expanded_requirements if key[0] != sid}
            self.listing = [row for row in self.listing if row["id"] != sid]
            if self.current == sid:
                self.current, self.session = None, None
                self.assessment, self.prompt = {}, None
                self.receive_progress({})
                self.draftChanged.emit()
            self.chats_mode, self.analytics_mode = True, False
            self.notice(value.get("warning", ""))
            self.client.call("/api/sessions", self.receive_listing)
        self.client.call(self.path(sid), deleted, "DELETE", failure=failed)

    @pyqtSlot('QVariantList', str)
    def mergeChats(self, ids, title):
        self.client.call("/api/sessions/merge", lambda value: self.select(value["id"]), "POST",
                         {"session_ids": list(ids), "title": title, "request_id": request_id()})

    @pyqtSlot()
    def send(self):
        self.send_files(None)

    def send_files(self, files):
        sid = self.current
        if not sid or sid in self.busy:
            return
        d = self.draft_for(sid)
        payload = {"text": d["text"] if files is None else "", "file_ids": list(d["files"] if files is None else files),
                   "request_id": (d["request"] or request_id()) if files is None else request_id()}
        if not payload["text"].strip() and not payload["file_ids"]:
            self.notice("Опишите задачу или прикрепите модель.")
            return
        if files is None:
            d["request"] = payload["request_id"]
        self.busy.add(sid)
        self.update_buttons()
        def done(value):
            self.busy.discard(sid)
            if files is None:
                d.update(text="", files=[], request=None)
                if sid == self.current:
                    self.draftChanged.emit()
            self.last_assessment = 0
            self.notice("" if value.get("run_id") else "Сообщение сохранено. Подключите LLM и запустите анализ.")
            self.poll()
        def failed(error):
            self.busy.discard(sid)
            self.notice(error)
        self.client.call(self.path(sid) + "/messages", done, "POST", payload, failed)

    @pyqtSlot()
    def attachFiles(self):
        paths, _ = QFileDialog.getOpenFileNames(None, "Прикрепить модель, изображение или ТЗ", "", "Модели, изображения и ТЗ (*.stl *.obj *.3mf *.jpg *.jpeg *.png *.docx)")
        self.upload(paths)

    @pyqtSlot('QVariantList')
    def uploadUrls(self, urls):
        self.upload([url.toLocalFile() if isinstance(url, QUrl) else QUrl(str(url)).toLocalFile() for url in urls])

    def upload(self, paths):
        sid = self.current
        if not sid or sid in self.busy or not paths:
            return
        if len(paths) + len(self.draft_for(sid)["files"]) > 20 or any(not os.path.isfile(p) for p in paths):
            self.notice("Выберите до 20 отдельных файлов.")
            return
        self.busy.add(sid)
        self.update_buttons()
        queue, errors = list(paths), []
        def next_file():
            if not queue:
                self.busy.discard(sid)
                self.notice("\n".join(errors))
                self.poll()
                return
            path = queue.pop(0)
            self.notice("Прикрепляем «" + os.path.basename(path) + "»…")
            def saved(value):
                self.draft_for(sid)["files"].append(value["id"])
                self.draft_for(sid)["request"] = None
                next_file()
            def failed(error):
                errors.append(error)
                next_file()
            self.client.call(self.path(sid) + "/files", saved, "POST", failure=failed, upload=path)
        next_file()

    @pyqtSlot(str)
    def removeFile(self, fid):
        if self.current not in self.busy and fid in self.draft_for(self.current)["files"]:
            self.draft_for(self.current)["files"].remove(fid)
            self.draft_for(self.current)["request"] = None
            self.update_buttons()

    @pyqtSlot(str)
    def importFile(self, fid):
        self.send_files([fid])

    @pyqtSlot()
    def analyze(self):
        route = "/calibration/plan" if (self.session or {}).get("calibration_for") else "/runs"
        self.client.call(self.path() + route, lambda value: self.poll(), "POST", {"request_id": request_id()})

    @pyqtSlot()
    def cancel(self):
        for run in (self.session or {}).get("runs", {}).values():
            if run["status"] in ("queued", "running"):
                self.client.call(self.path() + "/runs/" + run["id"] + "/cancel", lambda value: self.poll(), "POST", {})

    def answer(self, sid, payload):
        if sid in self.busy:
            return
        self.busy.add(sid)
        self.update_buttons()
        def finish(error=""):
            self.busy.discard(sid)
            self.notice(error)
            self.poll()
        self.client.call(self.path(sid) + "/answers", lambda value: finish(), "POST", dict(payload, request_id=request_id()), finish)

    @pyqtSlot(str)
    def invokeLink(self, link):
        url = QUrl(link)
        if url.scheme() != "assistant" or self.current in self.busy:
            return
        a = self.actions.get(url.path())
        if not a:
            return
        kind = a["kind"]
        if kind == "answer":
            self.answer(a["session_id"], a["payload"])
        elif kind in ("input", "risk"):
            self.prompt = copy.deepcopy(a)
            self.prompt.setdefault("title", "Цель прототипа и принятие инженерного риска")
            self.update_buttons()
        elif kind == "history":
            self.history = not self.history
            self.update_buttons()
        elif kind == "approve":
            self.approve(self.current, a)
        elif kind == "export":
            self.prompt = dict(a, title="Сохранить project.3mf и result.gcode")
            self.update_buttons()
        elif kind == "image":
            self.preview(a["file_id"])
        elif kind == "document":
            self.previewDocument(a["file_id"])
        elif kind == "action":
            self.showAction(a["start_id"], a["finish_id"])
        elif kind == "requirements":
            self.showRequirements(a["message_id"])
        elif kind == "import":
            self.importFile(a["file_id"])
        elif kind == "event":
            def show(event):
                dialog = QDialog()
                dialog.setWindowTitle("Подробности действия — AI Assistant")
                dialog.resize(800, 600)
                layout = QVBoxLayout(dialog)
                details = QPlainTextEdit()
                details.setReadOnly(True)
                details.setPlainText(json.dumps(event, ensure_ascii=False, indent=2))
                layout.addWidget(details)
                close = QPushButton("Закрыть")
                close.clicked.connect(dialog.accept)
                layout.addWidget(close)
                dialog.exec_()
            self.client.call(self.path() + "/events/" + quote(a["event_id"], safe=""), show)

    @pyqtSlot(str)
    def submitPrompt(self, value):
        a, self.prompt = self.prompt, None
        if a and value.strip():
            self.answer(a["session_id"], {"accept_risk": {"reason": value}} if a["kind"] == "risk" else {a["field"]: [dict(a["row"], **{a["value_key"]: value})]})
        self.update_buttons()

    @pyqtSlot()
    def dismissPrompt(self):
        self.prompt = None
        self.update_buttons()

    approve = AssistantWindow.approve
    watch = AssistantWindow.watch

    @pyqtSlot()
    def applyPlan(self):
        if self._view.get("planId"):
            self.approve(self.current, {"plan_id": self._view["planId"], "version": self._view["planVersion"]})

    @pyqtSlot(bool)
    def exportResult(self, overwrite):
        a, self.prompt = self.prompt, None
        if not a or a.get("kind") != "export":
            return
        sid, oid = a["session_id"], a["operation_id"]
        self.busy.add(sid)
        self.update_buttons()
        def finish(message):
            self.busy.discard(sid)
            self.notice(message)
            self.poll()
        def target(value):
            if value.get("cancelled"):
                finish("")
                return
            self.client.call(self.path(sid) + "/exports", lambda value: self.watch(sid, value["operation_id"], lambda result: finish("Проект и G-code сохранены."), finish), "POST",
                             {"request_id": request_id(), "operation_id": oid, "export_target_id": value["export_target_id"]}, finish)
        self.client.call(self.path(sid) + "/export-target", lambda value: self.watch(sid, value["operation_id"], target, finish), "POST",
                         {"request_id": request_id(), "overwrite": overwrite}, finish)

    @pyqtSlot(str)
    def preview(self, fid):
        def show(raw):
            image = QPixmap()
            if not image.loadFromData(raw):
                self.notice("Изображение недоступно")
                return
            dialog = QDialog()
            dialog.setWindowTitle("Вложение — AI Assistant")
            layout = QVBoxLayout(dialog)
            view = QLabel()
            view.setPixmap(image.scaledToWidth(min(850, image.width())))
            layout.addWidget(view)
            close = QPushButton("Закрыть")
            close.clicked.connect(dialog.accept)
            layout.addWidget(close)
            dialog.exec_()
        self.client.call(self.path() + "/files/" + quote(fid, safe=""), show, binary=True)

    @pyqtSlot(int)
    def loadAnalytics(self, days=30):
        self.chats_mode = False
        self.analytics_mode, self.days = True, days
        def loaded(report):
            if self.days != days:
                return
            self.report = report
            self.last_report = time.monotonic()
            self.update_buttons()
        self.client.call("/api/analytics?days=" + str(days), loaded)
        self.update_buttons()

    @pyqtSlot()
    def chat(self):
        self.analytics_mode = False
        self.chats_mode = False
        self.update_buttons()

    @pyqtSlot(str)
    def searchAnalytics(self, value):
        self.report_search = value
        self.update_buttons()

    @pyqtSlot(str)
    def filterAnalytics(self, value):
        if value in ("all", "attention", "ready", "saved"):
            self.report_filter = value
            self.report_issue = ""
            self.update_buttons()

    @pyqtSlot(str)
    def filterAnalyticsIssue(self, code):
        self.report_filter, self.report_issue, self.report_search = "attention", code, ""
        self.update_buttons()

    @pyqtSlot()
    def exportCsv(self):
        path, _ = QFileDialog.getSaveFileName(None, "Сохранить аналитику", "assistant-analytics.csv", "CSV (*.csv)")
        if path:
            def saved(raw):
                try:
                    with open(path, "wb") as target:
                        target.write(raw)
                    self.notice("CSV сохранён")
                except OSError:
                    self.notice("Не удалось сохранить CSV")
            self.client.call("/api/analytics/export?days=" + str(self.days), saved, binary=True)

    @pyqtSlot()
    def loadSettings(self):
        def loaded(value):
            self.config = value
            self.config_version += 1
            self.update_buttons()
        self.client.call("/api/settings/llm", loaded)

    @pyqtSlot(str, str, str, str, bool)
    def saveSettings(self, url, model, key, pipeline, test):
        def saved(value):
            self.config = value
            self.config_version += 1
            self.notice("Настройки сохранены. " + ("Ключ сохранён в STE Slicer." if value["key_configured"] else "Ключ ещё не задан."))
            if test:
                self.client.call("/api/settings/llm/test", lambda result: self.watch(self.current, result["operation_id"], lambda r: self.notice("Подключение работает: " + r.get("text", "") + ". Вызовы инструментов не проверялись.")), "POST",
                                 {"session_id": self.current, "request_id": request_id()})
        self.client.call("/api/settings/llm", saved, "PATCH", {"base_url": url, "model": model, "api_key": key, "pipeline": pipeline})

    @pyqtSlot()
    def clearKey(self):
        self.client.call("/api/settings/llm/clear-key", lambda value: (self.loadSettings(), self.notice("Ключ удалён")), "POST", {})

    @pyqtSlot(str)
    def invokeReportLink(self, link):
        url = QUrl(link)
        action = self.analytics_actions.get(url.path()) if url.scheme() == "assistant" else None
        if action and action["kind"] == "session":
            self.select(action["session_id"])

    @pyqtSlot(str, str)
    def answerAssessment(self, field, value):
        sid = self.current
        def saved(result):
            self.receive_assessment(sid, result)
            self.poll()
            if self.config.get("key_configured") and not (self.session or {}).get("calibration_for"):
                self.client.call(self.path(sid) + "/runs", lambda result: self.poll(), "POST", {"request_id": request_id()})
        self.client.call(self.path() + "/assessment", saved, "PATCH", {"answers": {field: value}})

    @pyqtSlot(str)
    def toggleActivity(self, group_id):
        key = (self.current, group_id)
        if key in self.expanded_activity:
            self.expanded_activity.remove(key)
        else:
            self.expanded_activity.add(key)
        self.update_buttons()

    @pyqtSlot(str)
    def toggleRequirements(self, message_id):
        self.expanded_requirements.symmetric_difference_update([(self.current, message_id)])
        self.update_buttons()

    @pyqtSlot(str)
    def showRequirements(self, message_id):
        message = next((row for row in (self.session or {}).get("timeline", []) if row.get("id") == message_id), None)
        if message and message.get("requirements_card"):
            self.showText("Исходные поля и источники требований", json.dumps(message["requirements_card"], ensure_ascii=False, indent=2))

    def showText(self, title, text):
        dialog = QDialog()
        dialog.setWindowTitle(title)
        dialog.resize(800, 600)
        layout = QVBoxLayout(dialog)
        details = QPlainTextEdit()
        details.setReadOnly(True)
        details.setPlainText(text)
        layout.addWidget(details)
        close = QPushButton("Закрыть")
        close.clicked.connect(dialog.accept)
        layout.addWidget(close)
        dialog.exec_()

    @pyqtSlot(str)
    def previewDocument(self, fid):
        self.client.call(self.path() + "/files/" + quote(fid, safe="") + "/text",
                         lambda value: self.showText(value["name"] + " — извлечённый текст", value["text"]))

    @pyqtSlot(str, str)
    def showAction(self, start_id, finish_id):
        ids = list(dict.fromkeys(eid for eid in (start_id, finish_id) if eid))
        sid, result = self.current, []
        def next_event():
            if not ids:
                self.showText("Подробности действия — AI Assistant", json.dumps(result, ensure_ascii=False, indent=2))
                return
            self.client.call(self.path(sid) + "/events/" + quote(ids.pop(0), safe=""), received)
        def received(event):
            result.append(event)
            next_event()
        next_event()

    @pyqtSlot()
    def createCalibration(self):
        self.client.call(self.path() + "/calibration", lambda value: self.select(value["id"]), "POST", {"request_id": request_id()})

    @pyqtSlot(str, str, str, bool, bool, bool, bool)
    def saveCalibration(self, x, y, z, adhesion, layers, surface, printed):
        try:
            values = [float(v.replace(",", ".")) for v in (x, y, z)]
        except ValueError:
            self.notice("Укажите три размера в миллиметрах")
            return
        sid = self.current
        self.client.call(self.path() + "/calibration/result", lambda value: (self.notice("Измерения сохранены. Вернитесь к исходной детали с теми же настройками."), self.poll()), "POST",
                         {"x_mm": values[0], "y_mm": values[1], "z_mm": values[2], "adhesion_ok": adhesion, "layers_ok": layers,
                          "surface_ok": surface, "printed": printed, "process_signature": (self.session or {}).get("calibration_process")})
