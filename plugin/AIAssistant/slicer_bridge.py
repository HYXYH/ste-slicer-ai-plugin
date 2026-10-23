"""All slicer reads and mutations run on the Qt thread; HTTP sees copies only."""
import copy
import logging
import os
import threading
import time
from collections import deque

from .session import now

log = logging.getLogger(__name__)
MODE_NAMES = {"classic": "3D", "cylindrical": "5D Spiral", "cylindrical_full": "5D"}


def container_info(container):
    if container is None:
        return None
    return {"id": container.getId(), "name": container.getName()}


def read_context(app):
    """Called exclusively on the main thread. Keep this shape stable for the agent."""
    stack = app.getGlobalContainerStack()
    result = {"available": stack is not None, "updated_at": now(), "printer": None,
              "mode": None, "extruders": [], "profile": None, "error": None}
    if stack is None:
        return result
    manager = app.getMachineManager()
    result["printer"] = container_info(stack)
    result["printer"]["definition_id"] = stack.definition.getId()
    mode = stack.getProperty("printing_mode", "value")
    result["mode"] = {"id": mode, "name": MODE_NAMES.get(mode, mode or "Не задан")}
    quality = container_info(stack.quality)
    changes = container_info(stack.qualityChanges)
    result["profile"] = {
        "quality": quality, "custom": changes,
        "name": manager.activeQualityOrQualityChangesName,
        "quality_type": manager.activeQualityType,
        "has_user_changes": bool(stack.userChanges.getAllKeys())}
    extruders = sorted(stack.extruders.values(), key=lambda e: int(e.getMetaDataEntry("position", "0")))
    for extruder in extruders:
        material = container_info(extruder.material)
        if material:
            material.update(type=extruder.material.getMetaDataEntry("material", ""),
                            brand=extruder.material.getMetaDataEntry("brand", ""))
        result["extruders"].append({
            "id": extruder.getId(), "position": str(extruder.getMetaDataEntry("position", "0")),
            "active": manager.activeStack == extruder, "material": material})
        result["profile"]["has_user_changes"] = (
            result["profile"]["has_user_changes"] or bool(extruder.userChanges.getAllKeys()))
    return result


class SlicerBridge:
    IMPORT_TIMEOUT = 120

    def __init__(self, app, store):
        from PyQt5.QtCore import QTimer
        self.app = app
        self.store = store
        self.lock = threading.RLock()
        self.context = {"available": False, "error": "Ожидание данных слайсера"}
        self.queue = deque()
        self.active = None
        self.closed = False
        self.ticks = 0
        self.timer = QTimer()
        self.timer.setInterval(250)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self._refresh()

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.context)

    def submit(self, operations):
        with self.lock:
            if self.closed:
                raise RuntimeError("Слайсер завершает работу")
            self.queue.extend(operations)
        # QTimer drains the queue on the main Qt thread.

    def _nodes(self):
        from UM.Scene.Iterator.DepthFirstIterator import DepthFirstIterator
        root = self.app.getController().getScene().getRoot()
        return [n for n in DepthFirstIterator(root) if n.getMeshData() is not None]

    def _refresh(self):
        try:
            context = read_context(self.app)
        except Exception:
            log.exception("Cannot read slicer context")
            context = {"available": False, "error": "Не удалось прочитать настройки. См. журнал слайсера.",
                       "updated_at": now()}
        with self.lock:
            self.context = context
        try:
            self.store.reconcile_scene(set(str(id(n)) for n in self._nodes()))
        except Exception:
            log.exception("Cannot reconcile scene")

    def _tick(self):
        if self.closed:
            return
        try:
            self.ticks += 1
            if self.ticks % 8 == 0:
                self._refresh()
            if self.active:
                self._check_import()
            if self.active is None:
                with self.lock:
                    operation = self.queue.popleft() if self.queue else None
                if operation:
                    self._start_import(operation)
        except Exception:
            log.exception("Import operation failed")
            if self.active:
                self._finish("error", "Ошибка импорта. Подробности в журнале слайсера.")

    def _start_import(self, operation):
        from PyQt5.QtCore import QUrl
        sid, oid = operation["session_id"], operation["id"]
        self.active = {"operation": operation, "started": time.monotonic(), "path": None}
        if self.app.getGlobalContainerStack() is None:
            self._finish("error", "Сначала выберите принтер в STE Slicer, затем повторите импорт.")
            return
        attachment = self.store.file(sid, operation["file_id"])
        self.active["path"] = os.path.normcase(os.path.abspath(attachment["path"]))
        self.active["before"] = set(id(n) for n in self._nodes())
        self.store.finish(sid, oid, "importing")
        # Same API as the bundled 'Open as models' dialog. True bypasses workspace restore.
        accepted = self.app.readLocalFile(QUrl.fromLocalFile(attachment["path"]), True)
        if accepted is False:
            self._finish("error", "Слайсер отклонил файл. Проверьте формат и журнал слайсера.")

    def _check_import(self):
        matches = []
        for node in self._nodes():
            if id(node) in self.active["before"]:
                continue
            name = node.getMeshData().getFileName()
            if name and os.path.normcase(os.path.abspath(name)) == self.active["path"]:
                matches.append(str(id(node)))
        if matches:
            self._finish("imported", object_ids=matches)
        elif time.monotonic() - self.active["started"] > self.IMPORT_TIMEOUT:
            self._finish("error", "За 120 секунд модель не появилась в сцене. Проверьте сообщения STE Slicer. "
                         "Загрузчик может ещё работать; перед повтором проверьте сцену.")

    def _finish(self, status, error=None, object_ids=None):
        operation = self.active["operation"]
        self.store.finish(operation["session_id"], operation["id"], status, error, object_ids)
        self.active = None
        self._refresh()

    def close(self):
        with self.lock:
            self.closed = True
        self.timer.stop()


class DemoBridge:
    """Explicit browser development mode; never claims a model was imported."""
    def snapshot(self):
        return {"available": False, "demo": True, "error": "Режим разработки: STE Slicer не подключён"}

    def submit(self, operations):
        for operation in operations:
            self.store.finish(operation["session_id"], operation["id"], "error",
                              "Для импорта откройте плагин внутри STE Slicer.")

    def __init__(self, store):
        self.store = store

    def close(self):
        pass
