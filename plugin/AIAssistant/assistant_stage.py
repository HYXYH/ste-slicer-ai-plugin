"""Register an ordinary STE Slicer stage; no application QML files are patched."""
import os
from PyQt5.QtCore import QObject, QUrl, pyqtProperty
from UM.Stage import Stage
from UM.Resources import Resources


class AssistantStage(Stage):
    def __init__(self, extension):
        super().__init__()
        self.extension = extension
        self.addDisplayComponent("main", os.path.join(os.path.dirname(__file__), "qml", "AssistantStage.qml"))
        self.extension.app.engineCreatedSignal.connect(self.engine_created)

    def engine_created(self):
        self.addDisplayComponent("sidebar", os.path.join(Resources.getPath(self.extension.app.ResourceTypes.QmlFiles), "PrepareSidebar.qml"))

    @pyqtProperty(str, constant=True)
    def stageId(self):
        return self.getPluginId()

    @pyqtProperty(QUrl, constant=True)
    def mainComponent(self):
        return self.getDisplayComponent("main")

    @pyqtProperty(QUrl, constant=True)
    def sidebarComponent(self):
        return self.getDisplayComponent("sidebar")

    @pyqtProperty(QObject, constant=True)
    def assistant(self):
        return self.extension.ensure_started()

    def onStageSelected(self):
        self.extension.ensure_started().set_active(True)

    def onStageDeselected(self):
        if self.extension.controller:
            self.extension.controller.set_active(False)
