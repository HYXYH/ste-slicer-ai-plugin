import logging
import os
import webbrowser

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
        self.setMenuName("AI Assistant")
        self.addMenuItem("Открыть чат", self.showChat)
        self.log_handler = SlicerLogHandler()
        self.logger = logging.getLogger(__package__)
        self.logger.setLevel(logging.INFO)
        self.logger.addHandler(self.log_handler)
        QApplication.instance().aboutToQuit.connect(self.close)

    def showChat(self):
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
            if not webbrowser.open(self.server.browser_url()):
                from UM.Message import Message
                Message("Не удалось открыть браузер. Проверьте системный браузер по умолчанию.",
                        title="AI Assistant").show()
        except Exception:
            self.logger.exception("Cannot open AI Assistant")
            from UM.Message import Message
            Message("Не удалось запустить AI Assistant. Подробности в журнале слайсера.",
                    title="AI Assistant").show()

    def close(self):
        if self.bridge:
            self.bridge.close()
            self.bridge = None
        if self.server:
            self.server.stop()
            self.server = None
        self.logger.removeHandler(self.log_handler)
