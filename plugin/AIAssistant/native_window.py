"""A slicer-owned Qt Widgets window, compatible with the bundled Qt 5.10."""
import json
import logging
import os
import time
import uuid
from urllib.parse import quote

from PyQt5.QtCore import Qt, QUrl, QTimer, QSettings, QBuffer, QByteArray, QIODevice, QFile, QSize, QObject, pyqtSignal
from PyQt5.QtGui import QPixmap
from PyQt5.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkProxy
from PyQt5.QtWidgets import (QWidget, QFrame, QHBoxLayout, QVBoxLayout, QLabel, QPushButton, QListWidget,
                            QListWidgetItem, QPlainTextEdit, QTextBrowser, QStackedWidget, QFileDialog,
                            QInputDialog, QLineEdit, QDialog, QFormLayout, QComboBox, QCheckBox, QShortcut)
from .native_view import FLOW, session_document, analytics_document

log = logging.getLogger(__name__)
STYLE = """
QWidget#assistant { background:#f3f6fb; color:#263b57; font-family:'Segoe UI'; font-size:13px; }
QFrame#sidebar { background:#142b45; } QFrame#sidebar QLabel { color:#91a9c5; }
QLabel#brand { color:#f4f7ff; font-size:23px; font-weight:600; }
QLabel#eyebrow { color:#8fadd1; font-size:10px; }
QListWidget { background:transparent; border:0; color:#c6d6e9; outline:0; padding:0; }
QListWidget::item { padding:13px 12px; margin:2px 0; border-radius:7px; }
QListWidget::item:selected { background:#e9f1ff; color:#1b4786; }
QListWidget::item:hover:!selected { background:#254360; }
QPushButton { background:white; color:#36516f; border:1px solid #d7e1ee; border-radius:7px; padding:9px 14px; }
QPushButton:hover { background:#edf3fd; border-color:#a9bfe0; }
QPushButton:disabled { color:#95a5bb; background:#edf1f6; }
QPushButton#primary { background:#265dcb; border-color:#265dcb; color:white; font-weight:600; }
QPushButton#primary:hover { background:#194cae; }
QFrame#sidebar QPushButton { background:#213f5e; border:1px solid #3b5876; color:#e6effe; text-align:left; padding:11px 13px; }
QFrame#sidebar QPushButton:hover { background:#2b5077; }
QFrame#topbar { background:white; border-bottom:1px solid #dde5f0; }
QLabel#title { font-size:20px; font-weight:600; color:#203957; }
QLabel#status { color:#4d719d; font-size:12px; }
QFrame#context { background:white; border:1px solid #dde5f0; border-radius:10px; }
QLabel#muted { color:#7a8da5; font-size:11px; }
QLabel#notice { background:#fff0e5; color:#914b2e; padding:10px; border-radius:7px; }
QTextBrowser { background:transparent; border:0; padding:8px; selection-background-color:#c3d7fc; }
QFrame#composer { background:white; border:1px solid #cbd9ed; border-radius:11px; }
QPlainTextEdit { background:white; border:0; color:#263b57; padding:8px; font-size:13px; }
QLineEdit, QComboBox { border:1px solid #cbd8e9; border-radius:6px; background:white; color:#263b57; padding:8px; }
QLineEdit:focus, QPlainTextEdit:focus { border-color:#6c99df; }
QDialog { background:#f6f8fc; color:#263b57; font-family:'Segoe UI'; font-size:13px; }
QScrollBar:vertical { background:transparent; width:10px; margin:2px; }
QScrollBar::handle:vertical { background:#c2cfdf; border-radius:4px; min-height:30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
"""


def request_id():
    return uuid.uuid4().hex


def label(value, name=None):
    widget = QLabel(value)
    widget.setTextFormat(Qt.PlainText)
    widget.setWordWrap(True)
    if name:
        widget.setObjectName(name)
    return widget


def button(value, callback, primary=False):
    widget = QPushButton(value)
    if primary:
        widget.setObjectName("primary")
    widget.clicked.connect(callback)
    return widget


class LocalClient(QObject):
    """Asynchronous local requests: the GUI thread must never wait on the Qt bridge."""
    def __init__(self, server, parent):
        super().__init__(parent)
        self.origin, self.token = server.origin, server.token
        self.network = QNetworkAccessManager(self)
        self.network.setProxy(QNetworkProxy(QNetworkProxy.NoProxy))
        self.pending = set()
        self.stopped = False

    def call(self, path, callback, method="GET", body=None, failure=None, upload=None, binary=False):
        if self.stopped:
            return
        if not path.startswith("/api/") or "#" in path:
            raise ValueError("Only local assistant API paths are allowed")
        request = QNetworkRequest(QUrl(self.origin + path))
        request.setRawHeader(b"X-Assistant-Token", self.token.encode("ascii"))
        request.setAttribute(QNetworkRequest.RedirectPolicyAttribute, QNetworkRequest.ManualRedirectPolicy)
        device = None
        if upload:
            device = QFile(upload)
            if not device.open(QIODevice.ReadOnly):
                (failure or self.parent().notice)("Не удалось прочитать файл.")
                return
            if device.size() > 100 * 1024 * 1024:
                device.close()
                (failure or self.parent().notice)("Максимальный размер файла — 100 МиБ.")
                return
            request.setRawHeader(b"Content-Type", b"application/octet-stream")
            request.setRawHeader(b"X-File-Name", quote(os.path.basename(upload), safe="").encode("ascii"))
        elif method != "GET":
            device = QBuffer()
            device.setData(QByteArray(json.dumps(body or {}, ensure_ascii=False).encode("utf-8")))
            device.open(QIODevice.ReadOnly)
            request.setRawHeader(b"Content-Type", b"application/json")
        if device:
            request.setHeader(QNetworkRequest.ContentLengthHeader, device.size())
            reply = self.network.sendCustomRequest(request, method.encode("ascii"), device)
            device.setParent(reply)
        else:
            reply = self.network.get(request)
        self.pending.add(reply)
        timeout = QTimer(reply)
        timeout.setSingleShot(True)
        timeout.timeout.connect(reply.abort)
        timeout.start(120000 if upload else 60000)
        def finished():
            self.pending.discard(reply)
            timeout.stop()
            if self.stopped:
                reply.deleteLater()
                return
            try:
                status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute) or 0
                raw = bytes(reply.readAll())
                value = None
                if not binary or status >= 300:
                    try:
                        value = json.loads(raw.decode("utf-8"))
                    except (ValueError, UnicodeError):
                        pass
                if not 200 <= status < 300:
                    message = value.get("error") if isinstance(value, dict) else None
                    (failure or self.parent().notice)(message or "Нет ответа от локального сервера. Повторите действие через меню слайсера.")
                elif not binary and value is None:
                    (failure or self.parent().notice)("Локальный сервер вернул неверный ответ.")
                else:
                    callback(raw if binary else value)
            except Exception:
                log.exception("Native assistant response handler failed")
                (failure or self.parent().notice)("Не удалось обновить окно. Подробности в журнале слайсера.")
            finally:
                reply.deleteLater()
        reply.finished.connect(finished)
        return reply

    def stop(self):
        self.stopped = True
        for reply in list(self.pending):
            reply.abort()


class Transcript(QTextBrowser):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.setAcceptDrops(False)
        self.document().setDocumentMargin(12)
        self.actions = {}

    def loadResource(self, resource_type, url):
        # Task text must never load remote images, files or execute document links.
        return None

    def set_document(self, document, follow_tail=True):
        markup, self.actions = document
        bar = self.verticalScrollBar()
        old, bottom = bar.value(), bar.maximum() - bar.value() < 80
        self.setHtml(markup)
        bar.setValue(bar.maximum() if follow_tail and bottom else old)


class Composer(QPlainTextEdit):
    submitted = pyqtSignal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and event.modifiers() & Qt.ControlModifier:
            self.submitted.emit()
        else:
            super().keyPressEvent(event)


class AssistantWindow(QWidget):
    def __init__(self, app, server, root):
        super().__init__(None, Qt.Window)
        self.app, self.server = app, server
        self.setAttribute(Qt.WA_QuitOnClose, False)
        self.setObjectName("assistant")
        self.setWindowTitle("AI Assistant — STE Slicer")
        self.setStyleSheet(STYLE)
        self.resize(1180, 820)
        self.setMinimumSize(840, 620)
        self.settings = QSettings(os.path.join(root, "window.ini"), QSettings.IniFormat)
        self.current, self.session, self.report = None, None, None
        self.drafts, self.busy, self.polling = {}, set(), set()
        self.history, self.disposed = False, False
        self.last_report, self.last_listing = 0, None
        self.client = LocalClient(server, self)
        self._build()
        geometry = self.settings.value("geometry")
        if isinstance(geometry, QByteArray):
            self.restoreGeometry(geometry)
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.poll)
        QShortcut("Ctrl+Return", self, activated=self.send)
        self.setAcceptDrops(True)
        QTimer.singleShot(0, self.boot)

    def _build(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(220)
        nav = QVBoxLayout(side)
        nav.setContentsMargins(18, 28, 18, 20)
        nav.setSpacing(13)
        nav.addWidget(label("STE  /  AI", "brand"))
        nav.addWidget(label("ПОМОЩНИК ПОДГОТОВКИ ПЕЧАТИ", "eyebrow"))
        nav.addSpacing(18)
        self.new_button = button("+  Новая задача", self.new_session)
        nav.addWidget(self.new_button)
        nav.addWidget(button("Обзор и аналитика", self.analytics))
        nav.addSpacing(7)
        nav.addWidget(label("ВАШИ ЗАДАЧИ", "eyebrow"))
        self.sessions = QListWidget()
        self.sessions.itemClicked.connect(lambda item: self.select(item.data(Qt.UserRole)))
        nav.addWidget(self.sessions, 1)
        nav.addWidget(button("Подключение LLM", self.connection))
        nav.addWidget(label("Сцена общая для всех задач.\nИзменения — после согласования.", "muted"))
        layout.addWidget(side)
        main = QVBoxLayout()
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)
        layout.addLayout(main, 1)
        top = QFrame()
        top.setObjectName("topbar")
        row = QHBoxLayout(top)
        row.setContentsMargins(26, 18, 26, 18)
        self.title = label("AI Assistant", "title")
        row.addWidget(self.title, 1)
        self.rename_button = button("Переименовать", self.rename)
        row.addWidget(self.rename_button)
        main.addWidget(top)
        self.notice_label = label("", "notice")
        self.notice_label.hide()
        main.addWidget(self.notice_label)
        self.stack = QStackedWidget()
        main.addWidget(self.stack, 1)
        chat = QWidget()
        content = QVBoxLayout(chat)
        content.setContentsMargins(20, 18, 20, 16)
        content.setSpacing(12)
        context = QFrame()
        context.setObjectName("context")
        tiles = QHBoxLayout(context)
        tiles.setContentsMargins(18, 13, 18, 13)
        self.context = []
        for caption in ("ПРИНТЕР", "РЕЖИМ", "МАТЕРИАЛ", "ПРОФИЛЬ"):
            tile = QVBoxLayout()
            tile.addWidget(label(caption, "muted"))
            value = label("—")
            tile.addWidget(value)
            tiles.addLayout(tile, 2 if caption == "ПРИНТЕР" else 1)
            self.context.append(value)
        content.addWidget(context)
        state = QHBoxLayout()
        self.status = label("Подключение…", "status")
        state.addWidget(self.status, 1)
        self.stop_button = button("Остановить", self.cancel)
        state.addWidget(self.stop_button)
        self.analyze_button = button("Повторить анализ", self.analyze)
        state.addWidget(self.analyze_button)
        content.addLayout(state)
        self.chat = Transcript()
        self.chat.anchorClicked.connect(lambda url: self.action(self.chat, url))
        content.addWidget(self.chat, 1)
        composer = QFrame()
        composer.setObjectName("composer")
        compose = QVBoxLayout(composer)
        compose.setContentsMargins(10, 8, 10, 8)
        self.pending = QListWidget()
        self.pending.setMaximumHeight(65)
        self.pending.setStyleSheet("QListWidget{color:#4c6380;}QListWidget::item{padding:3px;}")
        self.pending.setToolTip("Дважды нажмите на вложение, чтобы убрать его из сообщения")
        self.pending.itemDoubleClicked.connect(self.remove_file)
        compose.addWidget(self.pending)
        self.editor = Composer()
        self.editor.setAcceptDrops(False)
        self.editor.setPlaceholderText("Что будем печатать? Опишите назначение детали и требования…")
        self.editor.setFixedHeight(78)
        self.editor.textChanged.connect(self.edited)
        self.editor.submitted.connect(self.send)
        compose.addWidget(self.editor)
        toolbar = QHBoxLayout()
        self.attach = button("+  Прикрепить", self.attach_files)
        toolbar.addWidget(self.attach)
        toolbar.addWidget(label("STL · OBJ · 3MF · JPG · PNG", "muted"), 1)
        self.send_button = button("Отправить  ↑", self.send, True)
        toolbar.addWidget(self.send_button)
        compose.addLayout(toolbar)
        content.addWidget(composer)
        self.connection_status = label("Локальное подключение к STE Slicer  ·  Ctrl+Enter — отправить", "muted")
        content.addWidget(self.connection_status)
        self.stack.addWidget(chat)
        page = QWidget()
        analytics_layout = QVBoxLayout(page)
        analytics_layout.setContentsMargins(22, 20, 22, 16)
        controls = QHBoxLayout()
        controls.addWidget(button("←  К задаче", lambda: self.select(self.current)))
        self.period = QComboBox()
        for name, value in (("7 дней", 7), ("30 дней", 30), ("Всё время", 0)):
            self.period.addItem(name, value)
        self.period.setCurrentIndex(1)
        self.period.currentIndexChanged.connect(lambda: self.fetch_report(True))
        controls.addWidget(self.period)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Найти задачу…")
        self.search.textChanged.connect(self.render_report)
        controls.addWidget(self.search, 1)
        controls.addWidget(button("Обновить", lambda: self.fetch_report(True)))
        controls.addWidget(button("CSV ↓", self.export_csv))
        analytics_layout.addLayout(controls)
        self.analytics_view = Transcript()
        self.analytics_view.anchorClicked.connect(lambda url: self.action(self.analytics_view, url))
        analytics_layout.addWidget(self.analytics_view, 1)
        self.stack.addWidget(page)

    def notice(self, value):
        self.notice_label.setText(value)
        self.notice_label.setVisible(bool(value))

    def draft(self, sid):
        return self.drafts.setdefault(sid, {"text": "", "files": [], "request": None, "initialized": False})

    def path(self, sid=None):
        return "/api/sessions/" + quote(sid or self.current, safe="")

    def boot(self):
        def loaded(listing):
            self.render_listing(listing)
            self.select(listing["active_session_id"])
        self.client.call("/api/sessions", loaded)

    def present(self):
        self.show()
        self.winId()
        if hasattr(self.app, "getMainWindow"):
            main = self.app.getMainWindow()
            if main is not None:
                self.windowHandle().setTransientParent(main.windowHandle() if isinstance(main, QWidget) else main)
                self.setWindowIcon(main.windowIcon() if isinstance(main, QWidget) else main.icon())
        self.showNormal() if self.isMinimized() else None
        self.raise_()
        self.activateWindow()
        self.timer.start()
        self.poll()

    def closeEvent(self, event):
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.sync()
        self.timer.stop()
        event.accept()

    def shutdown(self):
        self.disposed = True
        self.timer.stop()
        self.client.stop()
        self.close()

    def select(self, sid):
        if not sid:
            return
        if self.current:
            self.draft(self.current)["text"] = self.editor.toPlainText()
        self.current, self.session = sid, None
        self.history = False
        self.stack.setCurrentIndex(0)
        self.rename_button.show()
        self.editor.blockSignals(True)
        self.editor.setPlainText(self.draft(sid)["text"])
        self.editor.blockSignals(False)
        self.chat.clear()
        self.title.setText("Загрузка…")
        self.client.call(self.path(sid) + "/select", lambda value: self.render_session(value) if self.current == sid else None, "POST", {})
        self.poll()

    def render_listing(self, listing):
        signature = [(s["id"], s["title"]) for s in listing["sessions"]]
        if signature != self.last_listing:
            self.sessions.clear()
            for session in listing["sessions"]:
                item = QListWidgetItem(session["title"])
                item.setData(Qt.UserRole, session["id"])
                item.setToolTip(session["title"])
                self.sessions.addItem(item)
            self.last_listing = signature
        for index in range(self.sessions.count()):
            self.sessions.item(index).setSelected(self.sessions.item(index).data(Qt.UserRole) == self.current)

    def poll(self):
        if self.disposed:
            return
        routes = [("/api/context", self.render_context), ("/api/sessions", self.render_listing)]
        if self.stack.currentIndex() == 1:
            self.fetch_report()
        elif self.current:
            sid = self.current
            routes.append((self.path(sid), lambda value: self.render_session(value) if self.current == sid else None))
        for path, callback in routes:
            if path in self.polling:
                continue
            self.polling.add(path)
            def done(value, path=path, callback=callback):
                self.polling.discard(path)
                callback(value)
            def failed(error, path=path):
                self.polling.discard(path)
                self.connection_status.setText("Нет подключения к STE Slicer")
                self.notice(error)
            self.client.call(path, done, failure=failed)

    def render_context(self, context):
        values = [(context.get("printer") or {}).get("name"), (context.get("mode") or {}).get("name"),
                  ", ".join((e.get("material") or {}).get("name") or (e.get("material") or {}).get("type") or "—" for e in context.get("extruders", [])),
                  (context.get("profile") or {}).get("name")]
        for widget, value in zip(self.context, values):
            widget.setText(value or "—")
        self.connection_status.setText(context.get("error") or "Локальное подключение к STE Slicer  ·  Ctrl+Enter — отправить")

    def render_session(self, value):
        if value["id"] != self.current:
            return
        if self.session and value["revision"] < self.session["revision"]:
            return
        changed = self.session is None or self.session["revision"] != value["revision"]
        self.session = value
        self.title.setText(value["title"] if self.stack.currentIndex() == 0 else "Обзор подготовки")
        self.status.setText(FLOW.get(value["workflow_state"], value["workflow_state"]))
        active = any(r["status"] in ("queued", "running") for r in value["runs"].values())
        self.stop_button.setVisible(active)
        self.analyze_button.setEnabled(not active)
        d = self.draft(self.current)
        if not d["initialized"]:
            used = set(fid for m in value["messages"] for fid in m.get("file_ids", []))
            d["files"] = [fid for fid in value["files"] if fid not in used]
            d["initialized"] = True
        self.render_pending()
        self.update_buttons()
        if changed:
            self.chat.set_document(session_document(value, self.history))

    def update_buttons(self):
        enabled = bool(self.current) and self.current not in self.busy
        for widget in (self.editor, self.attach, self.send_button, self.pending):
            widget.setEnabled(enabled)

    def edited(self):
        if self.current:
            self.draft(self.current).update(text=self.editor.toPlainText(), request=None)

    def render_pending(self):
        self.pending.clear()
        if self.session:
            for fid in self.draft(self.current)["files"]:
                f = self.session["files"].get(fid)
                if f:
                    item = QListWidgetItem(f["name"] + "  ·  " + str(round(f["size"] / 1048576, 2)) + " МиБ  ×")
                    item.setData(Qt.UserRole, fid)
                    self.pending.addItem(item)
        self.pending.setVisible(self.pending.count() > 0)

    def remove_file(self, item):
        d = self.draft(self.current)
        d["files"].remove(item.data(Qt.UserRole))
        d["request"] = None
        self.render_pending()

    def send(self, checked=False, sid=None, files=None):
        sid = sid or self.current
        if not sid or sid in self.busy:
            return
        d = self.draft(sid)
        body = {"text": d["text"] if files is None else "", "file_ids": list(d["files"] if files is None else files),
                "request_id": d["request"] or request_id() if files is None else request_id()}
        if not body["text"].strip() and not body["file_ids"]:
            self.notice("Добавьте описание или прикрепите модель.")
            return
        if files is None:
            d["request"] = body["request_id"]
        self.busy.add(sid)
        self.update_buttons()
        def success(value):
            self.busy.discard(sid)
            if files is None:
                d.update(text="", files=[], request=None)
                if self.current == sid:
                    self.editor.setPlainText("")
            self.notice("" if value.get("run_id") else "Сообщение сохранено. Подключите LLM и повторите анализ.")
            self.update_buttons()
            self.poll()
        def failed(error):
            self.busy.discard(sid)
            self.update_buttons()
            self.notice(error + " Повторная отправка сохранит тот же идентификатор сообщения.")
        self.client.call(self.path(sid) + "/messages", success, "POST", body, failed)

    def attach_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Прикрепить модели и изображения", "", "Модели и изображения (*.stl *.obj *.3mf *.jpg *.jpeg *.png)")
        self.upload(paths)

    def upload(self, paths):
        sid = self.current
        if not sid or sid in self.busy or not paths:
            return
        if len(paths) + len(self.draft(sid)["files"]) > 20 or any(not os.path.isfile(p) for p in paths):
            self.notice("Прикрепляйте до 20 отдельных файлов.")
            return
        self.busy.add(sid)
        self.update_buttons()
        remaining, errors = list(paths), []
        def next_file():
            if not remaining:
                self.busy.discard(sid)
                self.notice("\n".join(errors))
                self.update_buttons()
                self.poll()
                return
            path = remaining.pop(0)
            self.notice("Прикрепляем «" + os.path.basename(path) + "»…")
            def success(value):
                self.draft(sid)["files"].append(value["id"])
                self.draft(sid)["request"] = None
                next_file()
            def failed(error):
                errors.append(os.path.basename(path) + ": " + error)
                next_file()
            self.client.call(self.path(sid) + "/files", success, "POST", failure=failed, upload=path)
        next_file()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and self.stack.currentIndex() == 0:
            event.acceptProposedAction()

    def dropEvent(self, event):
        self.upload([u.toLocalFile() for u in event.mimeData().urls()])
        event.acceptProposedAction()

    def new_session(self):
        self.client.call("/api/sessions", lambda value: self.select(value["id"]), "POST", {})

    def rename(self):
        if self.session:
            value, ok = QInputDialog.getText(self, "Название задачи", "Название:", text=self.session["title"])
            if ok:
                sid = self.current
                self.client.call(self.path(sid), lambda result: self.poll(), "PATCH", {"title": value})

    def analyze(self):
        if self.current:
            self.client.call(self.path() + "/runs", lambda value: self.poll(), "POST", {"request_id": request_id()})

    def cancel(self):
        if self.session:
            for run in self.session["runs"].values():
                if run["status"] in ("queued", "running"):
                    self.client.call(self.path() + "/runs/" + run["id"] + "/cancel", lambda value: self.poll(), "POST", {})

    def answer(self, sid, payload):
        if sid in self.busy:
            return
        self.busy.add(sid)
        self.update_buttons()
        def finish(error=""):
            self.busy.discard(sid)
            self.update_buttons()
            self.notice(error)
            self.poll()
        self.client.call(self.path(sid) + "/answers", lambda value: finish(), "POST", dict(payload, request_id=request_id()), finish)

    def action(self, view, url):
        if url.scheme() != "assistant" or self.current in self.busy:
            return
        action = view.actions.get(url.path())
        if not action:
            return
        kind, sid = action["kind"], action.get("session_id", self.current)
        if kind == "session":
            self.select(sid)
        elif kind == "answer":
            self.answer(sid, action["payload"])
        elif kind == "input":
            value, ok = QInputDialog.getMultiLineText(self, action["title"], "Ваш ответ:")
            if ok and value.strip():
                self.answer(sid, {action["field"]: [dict(action["row"], **{action["value_key"]: value})]})
        elif kind == "risk":
            value, ok = QInputDialog.getText(self, "Принять инженерный риск", "Укажите цель прототипа. Эксплуатационные требования останутся непроверенными:")
            if ok and value.strip():
                self.answer(sid, {"accept_risk": {"reason": value}})
        elif kind == "history":
            self.history = not self.history
            self.chat.set_document(session_document(self.session, self.history))
        elif kind == "approve":
            self.approve(sid, action)
        elif kind == "export":
            self.export(sid, action["operation_id"])
        elif kind == "import":
            self.send(sid=sid, files=[action["file_id"]])
        elif kind == "image":
            self.client.call(self.path(sid) + "/files/" + action["file_id"], self.show_image, binary=True)

    def approve(self, sid, action):
        path = self.path(sid) + "/plans/" + action["plan_id"]
        self.busy.add(sid)
        self.update_buttons()
        def failed(error):
            self.busy.discard(sid)
            self.update_buttons()
            self.notice(error)
        def approved(value):
            def applied(value):
                self.busy.discard(sid)
                self.update_buttons()
                self.poll()
            self.client.call(path + "/apply", applied, "POST", {"request_id": action["plan_id"] + "-apply"}, failed)
        self.client.call(path + "/approve", approved, "POST", {"version": action["version"]}, failed)

    def watch(self, sid, oid, callback, failure=None):
        def loaded(op):
            if op["status"] in ("completed", "imported"):
                callback(op.get("result") or {})
            elif op["status"] in ("error", "failed", "interrupted", "cancelled"):
                (failure or self.notice)(op.get("error") or "Операция прервана")
            elif not self.disposed:
                QTimer.singleShot(1000, lambda: self.watch(sid, oid, callback, failure))
        if not self.disposed:
            self.client.call(self.path(sid) + "/operations/" + oid, loaded, failure=failure)

    def export(self, sid, oid):
        dialog = QDialog(self)
        dialog.setWindowTitle("Сохранение проекта и G-code")
        layout = QVBoxLayout(dialog)
        layout.addWidget(label("Выберите папку для project.3mf и result.gcode.\nОба файла относятся к этому расчёту."))
        overwrite = QCheckBox("Разрешаю замену существующих project.3mf и result.gcode")
        layout.addWidget(overwrite)
        layout.addWidget(button("Выбрать папку и сохранить", dialog.accept, True))
        layout.addWidget(button("Отмена", dialog.reject))
        if dialog.exec_() != QDialog.Accepted:
            return
        base = self.path(sid)
        self.busy.add(sid)
        self.update_buttons()
        def finish(message):
            self.busy.discard(sid)
            self.update_buttons()
            self.notice(message)
            self.poll()
        def target(value):
            if value.get("cancelled"):
                finish("")
                return
            self.client.call(base + "/exports", lambda result: self.watch(sid, result["operation_id"], lambda done: finish("Проект и G-code сохранены."), finish), "POST",
                             {"request_id": request_id(), "operation_id": oid, "export_target_id": value["export_target_id"]}, finish)
        self.client.call(base + "/export-target", lambda value: self.watch(sid, value["operation_id"], target, finish), "POST",
                         {"request_id": request_id(), "overwrite": overwrite.isChecked()}, finish)

    def show_image(self, raw):
        image = QPixmap()
        if not image.loadFromData(raw):
            self.notice("Не удалось открыть изображение.")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Вложение")
        layout = QVBoxLayout(dialog)
        view = QLabel()
        view.setPixmap(image.scaled(QSize(850, 650), Qt.KeepAspectRatio, Qt.SmoothTransformation))
        layout.addWidget(view)
        layout.addWidget(button("Закрыть", dialog.accept))
        dialog.exec_()

    def analytics(self):
        self.notice("")
        self.stack.setCurrentIndex(1)
        self.rename_button.hide()
        self.title.setText("Обзор подготовки")
        self.fetch_report(True)

    def fetch_report(self, force=False):
        if self.stack.currentIndex() != 1 or "report" in self.polling or not force and time.monotonic() - self.last_report < 10:
            return
        period = self.period.currentData()
        self.polling.add("report")
        def done(value):
            self.polling.discard("report")
            if self.period.currentData() != period:
                self.fetch_report(True)
                return
            self.report = value
            self.last_report = time.monotonic()
            self.render_report()
        def failed(error):
            self.polling.discard("report")
            self.notice(error)
        self.client.call("/api/analytics?days=" + str(period), done, failure=failed)

    def render_report(self):
        if self.report:
            self.analytics_view.set_document(analytics_document(self.report, self.search.text()), follow_tail=False)

    def export_csv(self):
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить аналитику", "assistant-analytics.csv", "CSV (*.csv)")
        if not path:
            return
        def saved(raw):
            try:
                with open(path, "wb") as target:
                    target.write(raw)
                self.notice("CSV сохранён.")
            except OSError:
                self.notice("Не удалось сохранить CSV в выбранную папку.")
        self.client.call("/api/analytics/export?days=" + str(self.period.currentData()), saved, binary=True)

    def connection(self):
        self.client.call("/api/settings/llm", self.connection_dialog)

    def connection_dialog(self, config):
        dialog = QDialog(self)
        dialog.setWindowTitle("Подключение LLM — STE Slicer")
        dialog.setMinimumWidth(520)
        layout = QVBoxLayout(dialog)
        layout.addWidget(label("Подключение LLM", "title"))
        form = QFormLayout()
        url, model, key = QLineEdit(config["base_url"]), QLineEdit(config["model"]), QLineEdit()
        key.setEchoMode(QLineEdit.Password)
        key.setPlaceholderText("Новый ключ — или оставьте пустым")
        pipeline = QComboBox()
        pipeline.addItem("Пошаговый анализ", "stepwise")
        pipeline.addItem("Полная карта одним запросом", "single")
        pipeline.setCurrentIndex(1 if config["pipeline"] == "single" else 0)
        for caption, field in (("Base URL", url), ("Модель", model), ("Режим анализа", pipeline), ("API key", key)):
            form.addRow(caption, field)
        layout.addLayout(form)
        state = label("Ключ сохранён в STE Slicer" if config["key_configured"] else "Ключ ещё не задан", "status")
        layout.addWidget(state)
        layout.addWidget(label("Пустое поле сохраняет прежний ключ. После сохранения поле очищается,\nа статус подтверждает сохранение. Удаление — отдельной кнопкой.", "muted"))
        result = label("")
        layout.addWidget(result)
        controls = QHBoxLayout()
        layout.addLayout(controls)
        widgets = [url, model, key, pipeline]
        def enabled(value):
            for w in widgets:
                w.setEnabled(value)
        def failed(error):
            enabled(True)
            result.setText(error)
        def saved(value, test):
            key.clear()
            state.setText("Ключ сохранён в STE Slicer" if value["key_configured"] else "Ключ ещё не задан")
            if not test:
                enabled(True)
                result.setText("Настройки сохранены.")
                return
            result.setText("Проверяем подключение…")
            sid = self.current
            def tested(value):
                enabled(True)
                result.setText("Подключение работает: " + value.get("text", "") + "\nВызовы инструментов этим запросом не проверяются.")
            self.client.call("/api/settings/llm/test", lambda value: self.watch(sid, value["operation_id"], tested, failed), "POST",
                             {"session_id": sid, "request_id": request_id()}, failed)
        def save(test=False):
            enabled(False)
            self.client.call("/api/settings/llm", lambda value: saved(value, test), "PATCH",
                             {"base_url": url.text().strip(), "model": model.text().strip(), "api_key": key.text(), "pipeline": pipeline.currentData()}, failed)
        def clear_key():
            enabled(False)
            self.client.call("/api/settings/llm/clear-key", lambda value: saved(value, False), "POST", {}, failed)
        widgets += [button("Удалить ключ", clear_key), button("Сохранить и проверить", lambda: save(True)), button("Сохранить", save, True)]
        for w in widgets[4:]:
            controls.addWidget(w)
        controls.addWidget(button("Закрыть", dialog.accept))
        dialog.exec_()
        key.clear()
