"""Render calibration UI states without user history, files, database or network access."""
import copy
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "plugin"))
from PyQt5.QtCore import QObject, QTimer, QUrl
from PyQt5.QtGui import QFontDatabase, QFont
from PyQt5.QtWidgets import QApplication
from PyQt5.QtQuickWidgets import QQuickWidget
from AIAssistant.calibration_assessment import assess, load_policy
from AIAssistant.knowledge_base import CALIBRATION
from AIAssistant.print_diagnostics import catalog

app = QApplication([])
if not QFontDatabase().families() and os.name == "nt":
    for filename in ("segoeui.ttf", "segoeuib.ttf", "seguisb.ttf"):
        QFontDatabase.addApplicationFont(os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts", filename))
app.setFont(QFont("Segoe UI", 10))
widget = QQuickWidget()
widget.setResizeMode(QQuickWidget.SizeRootObjectToView)
widget.resize(1180, 900)
widget.setSource(QUrl.fromLocalFile(os.path.join(ROOT, "plugin", "AIAssistant", "qml", "AssistantPanel.qml")))
if widget.status() != QQuickWidget.Ready:
    raise RuntimeError("\n".join(e.toString() for e in widget.errors()))
snapshot = {"context": {"printer": {"id": "preview"}, "mode": {"id": "classic"}, "extruders": [{"id": "preview", "nozzle_mm": .4, "material": {"id": "preview"}}]},
            "objects": [{"dimensions_mm": [150, 80, 70]}], "settings": [{"target": {"scope": "global"}, "values": {"speed_layer_0": 20}}]}
decision = assess(snapshot, {"prints": [], "lessons": []}, "Сложная деталь с точными посадками, мостами, нависаниями и ретрактом", load_policy())
state = {"title": "Предпросмотр калибровки", "sessionId": "", "calibrationInfo": CALIBRATION, "llmRecipient": "Получатель из настроек LLM",
         "intelligentCalibration": {"decision": decision, "run": {}, "pricing": {"printer_id": "preview", "printer_title": "Принтер · предпросмотр", "materials": [{"id": "preview", "title": "Материал · предпросмотр", "price": None}]}}}
pages = ["offer", "active", "info", "info-settings"]
folder = os.path.join(ROOT, ".local-assistant-data")
os.makedirs(folder, exist_ok=True)


def next_page():
    if not pages:
        app.quit()
        return
    page = pages.pop(0)
    value = copy.deepcopy(state)
    if page == "active":
        value["intelligentCalibration"]["run"] = {"state": "preparing", "test": decision["test"], "prints": [], "operations": [], "catalog": catalog()}
    widget.rootObject().setProperty("v", value)
    widget.rootObject().setProperty("modal", "calibrationInfo" if page.startswith("info") else "")
    if page == "info-settings":
        widget.rootObject().findChild(QObject, "calibrationExplanation").setProperty("settingsExpanded", True)
    widget.show()
    def capture():
        widget.rootObject().findChild(QObject, "conversation").setProperty("contentY", 0)
        if page == "info-settings":
            scroll = widget.rootObject().findChild(QObject, "assistantModalScroll")
            scroll.setProperty("contentY", max(0, scroll.property("contentHeight")-scroll.property("height")))
        def save():
            widget.grab().save(os.path.join(folder, "calibration-" + page + ".png"))
            next_page()
        QTimer.singleShot(80, save)
    QTimer.singleShot(450, capture)


next_page()
app.exec_()
