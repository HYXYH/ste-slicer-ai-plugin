"""STE Slicer API adapter. Every method is called only by the Qt queue."""
import hashlib
import json
import math
import os
import threading
import time

from .session import ApiError, uid, now
from .agent.contracts import fail

MODES = ("classic", "cylindrical", "cylindrical_full")


def plain(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if hasattr(value, "tolist"):
        return plain(value.tolist())
    return None


def vector(value):
    return [float(value.x), float(value.y), float(value.z)]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":")).encode("utf-8")).hexdigest()


class SlicerAPI:
    def __init__(self, bridge):
        self.bridge, self.app, self.store = bridge, bridge.app, bridge.store
        self.exports, self.jobs, self.slice_active = {}, {}, None
        self.export_active = None
        self.progress_lock, self.pending_progress = threading.Lock(), {}

    def stack(self, target=None):
        stack = self.app.getGlobalContainerStack()
        if stack is None:
            fail("Выберите принтер в STE Slicer", "NO_PRINTER", 409)
        if target and target.get("scope") == "extruder":
            for extruder in stack.extruders.values():
                if extruder.getId() == target["extruder_id"]:
                    return extruder
            fail("Экструдер не найден", "OBJECT_NOT_FOUND", 404)
        return stack

    def nodes(self):
        return {str(id(node)): node for node in self.bridge._nodes() if node.callDecoration("isSliceable")}

    def get_scene_snapshot(self, object_ids=None):
        from .slicer_bridge import read_context
        context = read_context(self.app)
        stack = self.stack()
        settings = {}
        for scope in [stack] + list(stack.extruders.values()):
            settings[scope.getId()] = {k: plain(scope.getProperty(k, "value")) for k in sorted(scope.getAllKeys())}
        objects = []
        for oid, node in sorted(self.nodes().items()):
            mesh, bounds = node.getMeshData(), node.getBoundingBox()
            geometry = hashlib.sha256(mesh.getVertices().tobytes())
            indices = mesh.getIndices()
            if indices is not None:
                geometry.update(indices.tobytes())
            rotation = node.getOrientation()
            per_mesh = node.callDecoration("getStack")
            attachments = self.store.attachments_for_object(oid)
            objects.append({"id": oid, "name": node.getName(), "geometry_revision": geometry.hexdigest(),
                            "printable": True, "build_plate": node.callDecoration("getBuildPlateNumber"),
                            "position_mm": vector(node.getPosition()), "scale": vector(node.getScale()),
                            "rotation_quaternion": [float(rotation.x), float(rotation.y), float(rotation.z), float(rotation.w)],
                            "world_transform": plain(node.getWorldTransformation().getData()),
                            "extruder_id": node.callDecoration("getActiveExtruder"),
                            "per_mesh_settings": {k: plain(per_mesh.getProperty(k, "value")) for k in sorted(per_mesh.getAllKeys())} if per_mesh else {},
                            "dimensions_mm": [float(bounds.width), float(bounds.height), float(bounds.depth)] if bounds else None,
                            "file_refs": sorted(attachments, key=lambda ref: (ref["session_id"], ref["file_id"]))})
        # Time, sources and chat events do not enter the fingerprint.
        stable_context = {k: v for k, v in context.items() if k != "updated_at"}
        scene = {"process_id": self.store.run_id, "objects": objects, "configuration": stable_context,
                 "effective_settings": settings, "active_build_plate": self.app.getMultiBuildPlateModel().activeBuildPlate}
        physical_scene = dict(scene, objects=[{k: v for k, v in row.items() if k != "file_refs"} for row in objects])
        reference = {"process_id": self.store.run_id, "fingerprint": digest(physical_scene)}
        scene.pop("effective_settings")
        if object_ids and not set(object_ids).issubset(set(o["id"] for o in objects)):
            fail("Целевой объект удалён", "OBJECT_NOT_FOUND", 409)
        scene.update(snapshot_ref=reference, captured_at=now(), warnings=["Пригодность для нагрузок и геометрия поддержек требуют инженерной проверки"])
        return scene

    def get_printer_capabilities(self):
        stack = self.stack()
        options = stack.getProperty("printing_mode", "options") or {}
        manager = self.app.getBackendManager()
        return {"printer_id": stack.getId(), "definition_id": stack.definition.getId(),
                "modes": [mode for mode in MODES if mode in options and manager.getBackendByType(mode) is not None],
                "workspace": {key: {"value": plain(stack.getProperty(key, "value")), "unit": "mm"}
                              for key in ("machine_width", "machine_height", "machine_depth")},
                "extruders": [{"id": e.getId(), "position": str(e.getMetaDataEntry("position")),
                               "nozzle_mm": plain(e.getProperty("machine_nozzle_size", "value"))} for e in stack.extruders.values()],
                "five_axis_fit": "unknown", "snapshot_ref": self.get_scene_snapshot()["snapshot_ref"],
                "warnings": ["Наличие backend не подтверждает лицензию 5D и пригодность геометрии"]}

    def get_current_configuration(self, keys=None, target=None):
        from .slicer_bridge import read_context
        stack = self.stack(target)
        return {"context": read_context(self.app), "target": target or {"scope": "global"},
                "settings": {k: plain(stack.getProperty(k, "value")) for k in keys or []},
                "snapshot_ref": self.get_scene_snapshot()["snapshot_ref"]}

    def get_calibration_context(self):
        from .slicer_bridge import read_context
        stack = self.stack()
        context = read_context(self.app)
        settings = {scope.getId(): {k: plain(scope.getProperty(k, "value")) for k in sorted(scope.getAllKeys())}
                    for scope in [stack] + list(stack.extruders.values())}
        stable = {k: v for k, v in context.items() if k != "updated_at"}
        return {"context": context, "profile_id": self.profile_id(),
                "signature": digest({"version": 1, "configuration": stable, "settings": settings})}

    def _current_only(self, arguments):
        stack = self.stack()
        if arguments.get("mode", stack.getProperty("printing_mode", "value")) != stack.getProperty("printing_mode", "value"):
            fail("Каталог другого режима нельзя подтвердить без переключения. Выберите режим вручную и повторите анализ.", "TARGET_CONFIGURATION_UNKNOWN", 409)
        if arguments.get("material_id"):
            extruder = self.stack({"scope": "extruder", "extruder_id": arguments["extruder_id"]}) if arguments.get("extruder_id") else next(iter(stack.extruders.values()))
            if arguments["material_id"] not in (extruder.material.getId(), extruder.material.getMetaDataEntry("base_file")):
                fail("Совместимость профилей с другим материалом не подтверждена без выбора материала", "TARGET_CONFIGURATION_UNKNOWN", 409)
        if arguments.get("profile_id") and arguments["profile_id"] != self.profile_id():
            fail("Зависимости настроек другого профиля требуют его выбора и повторного анализа", "TARGET_CONFIGURATION_UNKNOWN", 409)

    def get_setting_definitions(self, keys, target=None, **configuration):
        self._current_only(configuration)
        stack = self.stack(target)
        result = []
        for key in keys:
            if key not in stack.getAllKeys():
                fail("Неизвестная настройка: " + key)
            result.append(dict({"key": key}, **{name: plain(stack.getProperty(key, name)) for name in
                           ("type", "unit", "options", "minimum_value", "maximum_value", "enabled", "settable_per_extruder", "settable_per_mesh", "value")}))
        return {"items": result, "target": target or {"scope": "global"}, "configuration": self.get_current_configuration()["context"],
                "snapshot_ref": self.get_scene_snapshot()["snapshot_ref"],
                "warnings": ["Границы относятся к текущей конфигурации; после изменений нужна повторная проверка"]}

    def page(self, rows, limit=20, cursor=None):
        try:
            offset = int(cursor or 0)
            if offset < 0:
                raise ValueError()
        except ValueError:
            fail("Неверный cursor")
        return {"items": rows[offset:offset + limit], "next_cursor": str(offset + limit) if offset + limit < len(rows) else None}

    def _materials(self, extruder):
        return self.app.getMaterialManager().getAvailableMaterialsForMachineExtruder(self.stack(), extruder) or {}

    def list_materials(self, extruder_id=None, mode=None, limit=20, cursor=None):
        self._current_only({"mode": mode} if mode else {})
        extruder = self.stack({"scope": "extruder", "extruder_id": extruder_id}) if extruder_id else next(iter(self.stack().extruders.values()))
        rows = []
        for material_id, node in sorted(self._materials(extruder).items()):
            rows.append({"id": material_id, "container_id": node.getMetaDataEntry("id"), "name": node.getMetaDataEntry("name"),
                         "type": node.getMetaDataEntry("material"), "brand": node.getMetaDataEntry("brand"),
                         "extruder_id": extruder.getId(), "compatibility": "pass", "physically_available": "unknown"})
        result = self.page(rows, limit, cursor)
        result["snapshot_ref"] = self.get_scene_snapshot()["snapshot_ref"]
        return result

    def _profiles(self):
        manager, stack = self.app.getQualityManager(), self.stack()
        rows = {}
        for custom, groups in ((False, manager.getQualityGroups(stack)), (True, manager.getQualityChangesGroups(stack))):
            for key, group in groups.items():
                profile_id = group.node_for_global.getMetaDataEntry("id") if group.node_for_global else None
                if profile_id:
                    rows[profile_id] = (group, custom)
        return rows

    def profile_id(self):
        stack = self.stack()
        return stack.qualityChanges.getId() if stack.qualityChanges.getId() != "empty_quality_changes" else stack.quality.getId()

    def find_compatible_profiles(self, limit=20, cursor=None, **configuration):
        self._current_only(configuration)
        rows = [{"id": pid, "name": group.name, "custom": custom, "quality_type": group.quality_type,
                 "compatibility": "pass" if group.is_available else "fail",
                 "limitations": [] if group.is_available else ["Профиль недоступен для выбранной конфигурации"]}
                for pid, (group, custom) in sorted(self._profiles().items())]
        result = self.page(rows, limit, cursor)
        result["snapshot_ref"] = self.get_scene_snapshot()["snapshot_ref"]
        return result

    def validate_settings(self, changes):
        seen = set()
        for change in changes:
            identity = (change["key"], change["target"]["scope"], change["target"].get("extruder_id"))
            if identity in seen:
                fail("Настройка повторяется в одном действии")
            seen.add(identity)
            row = self.get_setting_definitions([change["key"]], target=change["target"])["items"][0]
            value, kind = change["value"], row["type"]
            if row["enabled"] is not True:
                fail("Настройка недоступна: " + change["key"])
            if kind in ("float", "int"):
                if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or kind == "int" and int(value) != value:
                    fail("Неверный тип настройки")
                if row["minimum_value"] is not None and value < row["minimum_value"] or row["maximum_value"] is not None and value > row["maximum_value"]:
                    fail("Значение вне границ настройки")
            elif kind == "bool" and not isinstance(value, bool):
                fail("Ожидается логическое значение")
            elif kind == "enum" and value not in (row["options"] or {}):
                fail("Недопустимый вариант настройки")
            elif kind not in ("float", "int", "bool", "enum"):
                fail("Изменение этого типа настройки не поддерживается")
            if change["target"]["scope"] == "extruder" and row["settable_per_extruder"] is not True:
                fail("Настройка не допускает область экструдера")
        return {"valid": True}

    def set_settings(self, changes):
        self.validate_settings(changes)
        for change in changes:
            self.stack(change["target"]).userChanges.setProperty(change["key"], "value", change["value"])
        actual = [{"key": c["key"], "value": plain(self.stack(c["target"]).getProperty(c["key"], "value")), "target": c["target"]} for c in changes]
        if actual != changes:
            fail("Фактические настройки отличаются от согласованных", "PARTIAL_APPLICATION", 409)
        return {"changes": actual}

    def set_print_mode(self, mode):
        if mode not in self.get_printer_capabilities()["modes"]:
            fail("Режим недоступен у выбранного принтера")
        self.stack().userChanges.setProperty("printing_mode", "value", mode)
        actual = self.stack().getProperty("printing_mode", "value")
        if actual != mode:
            fail("Режим не изменился", "PARTIAL_APPLICATION", 409)
        return {"mode": actual}

    def select_material(self, material_id, extruder_id):
        extruder = self.stack({"scope": "extruder", "extruder_id": extruder_id})
        material = self._materials(extruder).get(material_id)
        if material is None:
            fail("Материал несовместим или неизвестен", "NO_COMPATIBLE_PROFILE", 409)
        self.app.getMachineManager().setMaterial(str(extruder.getMetaDataEntry("position")), material)
        if extruder.material.getId() != material.getMetaDataEntry("id"):
            fail("Материал не выбран", "PARTIAL_APPLICATION", 409)
        return {"material_id": material_id, "container_id": extruder.material.getId(), "extruder_id": extruder_id}

    def select_quality_profile(self, profile_id):
        entry = self._profiles().get(profile_id)
        if not entry or not entry[0].is_available:
            fail("Профиль несовместим или неизвестен", "NO_COMPATIBLE_PROFILE", 409)
        group, custom = entry
        manager = self.app.getMachineManager()
        if custom:
            manager.setQualityChangesGroup(group, no_dialog=True)
        else:
            manager.setQualityGroup(group, no_dialog=True)
        if self.profile_id() != profile_id:
            fail("Профиль не выбран", "PARTIAL_APPLICATION", 409)
        return {"profile_id": self.profile_id()}

    def create_quality_profile(self, name, source_profile_id):
        if source_profile_id != self.profile_id():
            fail("Исходный профиль должен быть текущим")
        before = set(self._profiles())
        self.app.getQualityManager().createQualityChanges(name)
        new = set(self._profiles()) - before
        if len(new) != 1:
            fail("Не удалось подтвердить создание одного профиля", "PARTIAL_APPLICATION", 409)
        return {"profile_id": next(iter(new))}

    def save_quality_profile(self, profile_id):
        from steslicer.Settings.ContainerManager import ContainerManager
        from UM.Settings.ContainerRegistry import ContainerRegistry
        entry = self._profiles().get(profile_id)
        if not entry or not entry[1] or profile_id != self.profile_id():
            fail("Сохранять можно только выбранную пользовательскую копию")
        registry = ContainerRegistry.getInstance()
        containers = [self.stack()] + list(self.stack().extruders.values())
        if any(registry.isReadOnly(stack.qualityChanges.getId()) for stack in containers):
            fail("Профиль доступен только для чтения")
        if not ContainerManager.getInstance().updateQualityChanges():
            fail("Не удалось объединить настройки профиля", "PARTIAL_APPLICATION", 409)
        for stack in containers:
            registry.saveContainer(stack.qualityChanges)
            if stack.qualityChanges.isDirty():
                fail("Профиль остался несохранённым", "PARTIAL_APPLICATION", 409)
        return {"profile_id": self.profile_id(), "saved": True}

    def set_object_transform(self, object_id, position_mm=None, rotation_quaternion=None, scale=None):
        from UM.Math.Vector import Vector
        from UM.Math.Quaternion import Quaternion
        from UM.Operations.SetTransformOperation import SetTransformOperation
        node = self.nodes().get(object_id)
        if node is None:
            fail("Объект удалён", "OBJECT_NOT_FOUND", 409)
        if position_mm is None and rotation_quaternion is None and scale is None:
            fail("Нужно хотя бы одно преобразование")
        rotation = node.getOrientation()
        if rotation_quaternion is not None:
            norm = math.sqrt(sum(n * n for n in rotation_quaternion))
            if abs(norm - 1) > 0.000001:
                fail("Quaternion x,y,z,w должен быть нормирован")
            rotation = Quaternion(*rotation_quaternion)
        SetTransformOperation(node, Vector(*position_mm) if position_mm is not None else node.getPosition(),
                              rotation, Vector(*scale) if scale is not None else node.getScale()).push()
        return next(o for o in self.get_scene_snapshot()["objects"] if o["id"] == object_id)

    def remove_objects(self, object_ids):
        from UM.Operations.GroupedOperation import GroupedOperation
        from UM.Operations.RemoveSceneNodeOperation import RemoveSceneNodeOperation
        nodes = self.nodes()
        if not set(object_ids).issubset(set(nodes)):
            fail("Объект удалён", "OBJECT_NOT_FOUND", 409)
        operation = GroupedOperation()
        for oid in object_ids:
            operation.addOperation(RemoveSceneNodeOperation(nodes[oid]))
        operation.push()
        if set(object_ids) & set(self.nodes()):
            fail("Не все объекты удалены", "PARTIAL_APPLICATION", 409)
        return {"removed_object_ids": object_ids}

    def arrange_objects(self, object_ids, session_id):
        from steslicer.Arranging.ArrangeObjectsJob import ArrangeObjectsJob
        nodes = self.nodes()
        if not set(object_ids).issubset(set(nodes)):
            fail("Объект удалён", "OBJECT_NOT_FOUND", 409)
        if any(nodes[oid].getParent().callDecoration("isGroup") for oid in object_ids):
            fail("Расстановка сгруппированных дочерних объектов требует ручной подготовки")
        operation_id = uid()
        job = ArrangeObjectsJob([nodes[oid] for oid in object_ids], [n for oid, n in nodes.items() if oid not in object_ids], min_offset=8)
        self.jobs[operation_id] = job
        self._operation(session_id, operation_id, {"kind": "arrange", "status": "running"})
        def done(finished):
            self.bridge.post(lambda: self._arranged(session_id, operation_id))
        job.finished.connect(done)
        job.start()
        return {"operation_id": operation_id, "status": "running"}

    def _arranged(self, session_id, operation_id):
        job = self.jobs[operation_id]
        scene = self.get_scene_snapshot()
        # The bundled arranger can finish after placing a model outside the bed.
        outside = any(node.isOutsideBuildArea() for node in self.nodes().values() if hasattr(node, "isOutsideBuildArea"))
        self._operation(session_id, operation_id, {"status": "error" if job.hasError() or outside else "completed",
                                                 "error": "Расстановка не подтверждена внутри рабочей области" if job.hasError() or outside else None,
                                                 "result": scene})
        self.jobs.pop(operation_id, None)

    def _operation(self, sid, oid, values):
        def update(session):
            operation = session["operations"].setdefault(oid, {"id": oid, "session_id": sid, "created_at": now()})
            operation.update(values)
        self.store.edit(sid, update)

    def start_slicing(self, session_id, plan_id):
        from UM.Backend.Backend import BackendState
        if self.slice_active:
            fail("Другой расчёт ещё выполняется", "SLICE_FAILED", 409)
        backend = self.app.getBackendManager().getCurrentBackend()
        if backend is None:
            fail("Backend выбранного режима недоступен", "SLICE_FAILED", 409)
        operation_id = uid()
        snapshot = self.get_scene_snapshot()["snapshot_ref"]
        self._operation(session_id, operation_id, {"kind": "slice", "plan_id": plan_id, "status": "slicing", "progress": None, "snapshot_ref": snapshot})
        self.slice_active = (session_id, operation_id, backend)
        def progress(amount):
            if not self.slice_active or self.slice_active[1] != operation_id:
                return
            with self.progress_lock:
                queued = operation_id in self.pending_progress
                self.pending_progress[operation_id] = plain(amount)
            if not queued:
                self.bridge.post(lambda: self._flush_slice_progress(operation_id))
        phase = {"launched": False, "processing": False}
        def state(value):
            if not phase["launched"]:
                return
            if value == BackendState.Processing:
                phase["processing"] = True
            # forceSlice first emits NotStarted itself; only a later reset is an interruption.
            if value in (BackendState.Done, BackendState.Error, BackendState.Disabled) or (value == BackendState.NotStarted and phase["processing"]):
                self.bridge.post(lambda: self._slice_finished(operation_id, value == BackendState.Done))
        self.slice_signals = (progress, state)
        backend.processingProgress.connect(progress)
        backend.backendStateChange.connect(state)
        try:
            self._launch_slice(operation_id, phase, time.monotonic() + 30)
        except Exception:
            backend.processingProgress.disconnect(progress)
            backend.backendStateChange.disconnect(state)
            self.slice_active = None
            self._operation(session_id, operation_id, {"status": "error",
                            "error": "Не удалось запустить слайсинг. Проверьте журнал STE Slicer."})
            self.store.edit(session_id, lambda session: session.update(workflow_state="failed"))
            raise
        return {"operation_id": operation_id, "status": "slicing", "snapshot_ref": snapshot}

    def _launch_slice(self, operation_id, phase, deadline):
        if not self.slice_active or self.slice_active[1] != operation_id:
            return
        sid, oid, backend = self.slice_active
        checker = self.app.getMachineErrorChecker() if hasattr(self.app, "getMachineErrorChecker") else None
        # 7.10 reschedules validation after setting writes. Starting early can cancel the engine.
        if checker is not None and getattr(checker, "needToWaitForResult", False) is True:
            if time.monotonic() >= deadline:
                self._slice_finished(oid, False, "Проверка настроек STE Slicer не завершилась за 30 секунд. Проверьте сообщения приложения.")
                return
            from PyQt5.QtCore import QTimer
            QTimer.singleShot(250, lambda: self._resume_slice(oid, phase, deadline))
            return
        if self.get_scene_snapshot()["snapshot_ref"] != self.store.operation(sid, oid)["snapshot_ref"]:
            self._slice_finished(oid, False)
            return
        phase["launched"] = True
        backend.forceSlice()

    def _resume_slice(self, operation_id, phase, deadline):
        try:
            self._launch_slice(operation_id, phase, deadline)
        except Exception:
            self._slice_finished(operation_id, False, "Не удалось запустить слайсинг после проверки настроек. Проверьте журнал STE Slicer.")

    def _slice_progress(self, operation_id, amount):
        if self.slice_active and self.slice_active[1] == operation_id:
            self.store.progress(self.slice_active[0], operation_id, plain(amount))

    def _flush_slice_progress(self, operation_id):
        with self.progress_lock:
            amount = self.pending_progress.pop(operation_id, None)
        self._slice_progress(operation_id, amount)

    def _slice_finished(self, operation_id, success, error=None):
        if not self.slice_active or self.slice_active[1] != operation_id:
            return
        sid, oid, backend = self.slice_active
        with self.progress_lock:
            self.pending_progress.pop(oid, None)
        backend.processingProgress.disconnect(self.slice_signals[0])
        backend.backendStateChange.disconnect(self.slice_signals[1])
        self.slice_active = None
        information = self.app.getPrintInformation()
        scene = self.app.getController().getScene()
        plate = self.app.getMultiBuildPlateModel().activeBuildPlate
        operation = self.store.operation(sid, oid)
        current = self.get_scene_snapshot()["snapshot_ref"]
        success = success and bool(getattr(scene, "gcode_dict", {}).get(plate))
        stale = current != operation["snapshot_ref"]
        self._operation(sid, oid, {"status": "stale" if stale else "completed" if success else "error",
                           "error": "Сцена изменилась" if stale else None if success else error or "Backend не подготовил G-code. Проверьте сообщения STE Slicer.",
                         "result": {"time": {"value": sum(int(v) for v in information.printTimes().values()) if information.printTimes() and all(v.valid for v in information.printTimes().values()) else None, "unit": "s"},
                                    "material": {"values": plain(information.materialLengths), "unit": "m"}} if success else None})
        def update(session):
            session["workflow_state"] = "stale" if stale else "result_ready" if success else "failed"
            session["events"].append({"id": uid(), "seq": len(session["events"]) + 1, "session_id": sid,
                                      "run_id": None, "created_at": now(), "type": "progress", "operation_id": oid,
                                      "message": "Расчёт завершён" if success and not stale else "Расчёт не дал актуального результата"})
        self.store.edit(sid, update)
        if not success and not stale and hasattr(self.bridge, "on_slice_failure"):
            self.store.edit(sid, lambda session: session["sources"].update({oid: {"id": oid, "version": 1, "kind": "tool", "tool": "start_slicing",
                            "data": {"code": "SLICE_FAILED", "message": "Backend не подготовил G-code. Нужен новый план с повторным согласием."}}}))
            self.bridge.on_slice_failure(sid, oid)

    def choose_export_target(self, session_id, overwrite=False):
        from PyQt5.QtWidgets import QFileDialog
        path = QFileDialog.getExistingDirectory(None, "Сохранить проект и G-code")
        if not path:
            return {"cancelled": True}
        target_id = uid()
        self.exports[target_id] = {"session_id": session_id, "path": os.path.realpath(path), "overwrite": overwrite}
        return {"export_target_id": target_id, "folder_name": os.path.basename(path), "overwrite": overwrite}

    def start_export(self, command):
        from .export_job import NativeExport
        if self.export_active is not None or self.slice_active is not None or self.bridge.active is not None:
            fail("Дождитесь завершения текущей операции слайсера", "BUSY", 409)
        self.export_active = NativeExport(self, command)
        try:
            self.export_active.start()
        except Exception:
            self.export_active = None
            raise

    def preferences(self, values=None):
        preferences = self.app.getPreferences()
        for key in ("base_url", "api_key", "model", "pipeline"):
            preferences.addPreference("ai_assistant/" + key, "")
        if values is not None:
            for key, value in values.items():
                preferences.setValue("ai_assistant/" + key, value)
            self.app.savePreferences()
        return {k: preferences.getValue("ai_assistant/" + k) for k in ("base_url", "api_key", "model", "pipeline")}

    def dispatch(self, name, arguments, expected_ref=None):
        if expected_ref and self.get_scene_snapshot()["snapshot_ref"] != expected_ref:
            fail("Сцена изменилась перед выполнением действия", "STALE_INPUT", 409)
        if name == "save_project" or name == "export_gcode":
            fail("Сохранение должно выполняться через штатный WriteFileJob", "EXPORT_FAILED", 409)
        else:
            method = getattr(self, name, None)
            if method is None or name.startswith("_"):
                fail("API не поддерживает инструмент", "UNSUPPORTED_TOOL", 409)
            result = method(**arguments)
        if expected_ref:
            # The mutation and its observed result share a single Qt queue turn.
            return {"result": result, "snapshot_ref": self.get_scene_snapshot()["snapshot_ref"]}
        return result
