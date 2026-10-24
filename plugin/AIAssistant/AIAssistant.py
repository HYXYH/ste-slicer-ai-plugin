import logging
import os

from PyQt5.QtWidgets import QApplication
from UM.Extension import Extension
from UM.Logger import Logger
from UM.Resources import Resources

from .server import LocalServer
from .session import SessionStore
from .slicer_bridge import SlicerBridge


class SlicerLogHandler(logging.Handler):
    def emit(self, record):
        Logger.log("e" if record.levelno >= logging.ERROR else "d", self.format(record))


class AIAssistant(Extension):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.server = None
        self.bridge = None
        self.window = None
        self.controller = None
        self.setMenuName("AI Assistant")
        self.addMenuItem("Открыть AI Assistant", self.showChat)
        self.log_handler = SlicerLogHandler()
        self.logger = logging.getLogger(__package__)
        self.logger.setLevel(logging.INFO)
        self.logger.addHandler(self.log_handler)
        QApplication.instance().aboutToQuit.connect(self.close)

    def showChat(self):
        try:
            self.app.getController().setActiveStage(self.getPluginId())
        except Exception:
            self.logger.exception("Cannot select AI Assistant tab")

    def ensure_started(self):
        try:
            if self.server is None:
                root = os.path.join(Resources.getDataStoragePath(), "ai_assistant")
                store = SessionStore(root)
                self.bridge = SlicerBridge(self.app, store)
                try:
                    self.server = LocalServer(store, self.bridge)
                    self.server.start()
                except Exception:
                    self.bridge.close()
                    self.bridge = None
                    self.server = None
                    raise
            if self.controller is None:
                from .assistant_controller import AssistantController
                self.controller = AssistantController(self.server)
            return self.controller
        except Exception:
            self.logger.exception("Cannot open AI Assistant")
            from UM.Message import Message
            Message("Не удалось запустить AI Assistant. Подробности в журнале слайсера.",
                    title="AI Assistant").show()
            raise

    def close(self):
        if self.controller:
            self.controller.shutdown()
            self.controller = None
        if self.window:
            self.window.shutdown()
            self.window = None
        if self.bridge:
            self.bridge.close()
            self.bridge = None
        if self.server:
            self.server.stop()
            self.server = None
        self.logger.removeHandler(self.log_handler)
