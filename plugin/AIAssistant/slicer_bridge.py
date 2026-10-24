"""All slicer reads and mutations run on the Qt thread; HTTP sees copies only."""
import copy
import logging
import os
import threading
import time
from collections import deque

from .session import now, ApiError

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
            "enabled": bool(extruder.isEnabled) if hasattr(extruder, "isEnabled") else None,
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
        from .slicer_api import SlicerAPI
        self.commands = deque()
        self.callbacks = deque()
        self.qt_thread = threading.current_thread().ident
        self.api = SlicerAPI(self)
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

    def call(self, name, arguments, expected_ref=None):
        if threading.current_thread().ident == self.qt_thread:
            return self.api.dispatch(name, arguments, expected_ref)
        command = {"name": name, "arguments": copy.deepcopy(arguments), "expected": expected_ref,
                   "done": threading.Event(), "abandoned": False}
        with self.lock:
            if self.closed:
                raise ApiError("Слайсер закрывается", 409, "INTERRUPTED")
            self.commands.append(command)
        if not command["done"].wait(300 if name == "choose_export_target" else 30):
            with self.lock:
                command["abandoned"] = True
            raise ApiError("Слайсер не подтвердил операцию. Проверьте фактическую сцену.", 504, "TOOL_TIMEOUT")
        if "error" in command:
            raise command["error"]
        return command["result"]

    def post(self, callback):
        with self.lock:
            self.callbacks.append(callback)

    def _commands(self):
        with self.lock:
            command = self.commands.popleft() if self.commands else None
        deadline = time.monotonic() + .008
        for unused in range(4):
            with self.lock:
                callback = self.callbacks.popleft() if self.callbacks else None
            if callback is None:
                break
            try:
                callback()
            except Exception:
                log.exception("Assistant backend callback failed")
            if time.monotonic() >= deadline:
                break
        if command:
            try:
                if command["abandoned"]:
                    return
                command["result"] = self.api.dispatch(command["name"], command["arguments"], command["expected"])
            except ApiError as error:
                command["error"] = error
            except Exception:
                log.exception("Assistant Qt command failed")
                command["error"] = ApiError("Ошибка API STE Slicer. Проверьте журнал приложения.", 409, "SLICER_API_ERROR")
            finally:
                command["done"].set()

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
            # Completion always takes a fresh snapshot. Passive polling must not compete with slicing.
            if self.app.getGlobalContainerStack() is not None and not self.api.slice_active and self.ticks % 40 == 0:
                self.store.invalidate_snapshot(self.api.get_scene_snapshot()["snapshot_ref"])
        except Exception:
            log.exception("Cannot reconcile scene")

    def _tick(self):
        if self.closed:
            return
        self._commands()
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
            for command in self.commands:
                command["error"] = ApiError("Приложение закрыто", 409, "INTERRUPTED")
                command["done"].set()
            self.commands.clear()
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
        self.preferences_path = os.path.join(store.root, "demo-preferences.json")

    def call(self, name, arguments, expected_ref=None):
        if name == "preferences":
            import json
            config = {"base_url": "", "api_key": "", "model": "", "pipeline": ""}
            if os.path.exists(self.preferences_path):
                with open(self.preferences_path, encoding="utf-8") as source:
                    config.update(json.load(source))
            if arguments.get("values") is not None:
                config.update(arguments["values"])
                with open(self.preferences_path, "w", encoding="utf-8") as target:
                    json.dump(config, target)
            return config
        raise ApiError("Для этого действия откройте плагин в STE Slicer", 409, "NO_PRINTER")

    def close(self):
        pass
