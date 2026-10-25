"""Render the real QML knowledge page with the static catalog and no invented print history."""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "plugin"))
from PyQt5.QtCore import QUrl, QTimer
from PyQt5.QtWidgets import QApplication
from PyQt5.QtQuickWidgets import QQuickWidget
from PyQt5.QtGui import QFontDatabase, QFont
from AIAssistant.knowledge_view import projection
from AIAssistant.knowledge_base import CALIBRATION
from AIAssistant.print_diagnostics import catalog

app = QApplication([])
if not QFontDatabase().families() and os.name == "nt":
    for name in ("segoeui.ttf", "segoeuib.ttf", "seguisb.ttf"):
        QFontDatabase.addApplicationFont(os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts", name))
app.setFont(QFont("Segoe UI", 10))
widget = QQuickWidget()
widget.setResizeMode(QQuickWidget.SizeRootObjectToView)
widget.resize(1180, 820)
widget.setSource(QUrl.fromLocalFile(os.path.join(ROOT, "plugin", "AIAssistant", "qml", "AssistantKnowledgeView.qml")))
if widget.status() != QQuickWidget.Ready:
    raise RuntimeError("\n".join(e.toString() for e in widget.errors()))
pages = [("defects", "stringing", "knowledge-defect-preview.png"), ("calibration", "", "knowledge-calibration-preview.png"),
         ("conclusions", "", "knowledge-empty-preview.png"), ("panel", "defects", "knowledge-panel-preview.png"),
         ("modal", "", "knowledge-info-preview.png")]
data = {"prints": [], "lessons": [], "conclusions": [], "defects": catalog(), "calibration": CALIBRATION}
folder = os.path.join(ROOT, ".local-assistant-data")
os.makedirs(folder, exist_ok=True)


def show_next():
    if not pages:
        app.quit()
        return
    tab, selection, filename = pages.pop(0)
    options = {"tab": "defects" if tab == "panel" else tab, "selection": selection if tab != "panel" else ""}
    state = {"knowledgeData": projection(data, options), "knowledgeOptions": options, "calibrationInfo": CALIBRATION}
    if tab in ("panel", "modal"):
        if tab == "panel":
            widget.setSource(QUrl.fromLocalFile(os.path.join(ROOT, "plugin", "AIAssistant", "qml", "AssistantPanel.qml")))
            if widget.status() != QQuickWidget.Ready:
                raise RuntimeError("\n".join(e.toString() for e in widget.errors()))
        state["knowledge"] = tab == "panel"
        widget.rootObject().setProperty("v", state)
        widget.rootObject().setProperty("modal", "calibrationInfo" if tab == "modal" else "")
    else:
        widget.rootObject().setProperty("viewState", state)
    widget.show()
    def save():
        widget.grab().save(os.path.join(folder, filename))
        show_next()
    QTimer.singleShot(400, save)

show_next()
app.exec_()
