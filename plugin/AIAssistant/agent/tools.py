"""One authoritative registry for MCP and Chat Completions schemas."""
import copy
import json

from ..session import uid
from .contracts import ID, TEXT, MODE, NUMBER, VALUE, VECTOR, TARGET, array, obj, validate, fail
from .knowledge import Knowledge


PAGE = {"limit": {"type": "integer", "minimum": 1, "maximum": 50}, "cursor": ID}
CONFIG = {"mode": MODE, "material_id": ID, "extruder_id": ID, "profile_id": ID}


class Tools:
    def __init__(self, store, bridge):
        self.store, self.bridge = store, bridge
        self.knowledge = Knowledge()
        self.registry = {}
        self.authorize_change = None
        self.add("get_scene_snapshot", "Прочитать всю сцену, размеры и преобразования в мм", {"object_ids": array(ID)})
        self.add("get_printer_capabilities", "Прочитать реальный принтер, сопла и режимы")
        self.add("get_current_configuration", "Прочитать эффективные настройки", {"keys": array(ID), "target": TARGET})
        self.add("get_setting_definitions", "Типы, единицы, диапазоны и зависимости", dict(CONFIG, keys=array(ID, 100, 1), target=TARGET), ["keys"])
        self.add("list_materials", "Совместимый каталог; не подтверждает наличие катушки", dict(PAGE, **{k: CONFIG[k] for k in ("extruder_id", "mode")}))
        self.add("find_compatible_profiles", "Каталог реальных профилей", dict(PAGE, **CONFIG))
        self.add("search_knowledge", "Найти экспертные правила и пробелы знаний", dict(PAGE, query=TEXT, conditions={"type": "object"}, decision=ID), ["query"])
        self.add("get_operation_status", "Состояние длительной операции", {"operation_id": ID}, ["operation_id"])
        self.add("get_slice_result", "Время и расход актуального слайсинга", {"operation_id": ID}, ["operation_id"])
        changes = {
            "load_model": ({"file_id": ID}, ["file_id"]),
            "set_print_mode": ({"mode": MODE}, ["mode"]),
            "select_material": ({"material_id": ID, "extruder_id": ID}, ["material_id", "extruder_id"]),
            "select_quality_profile": ({"profile_id": ID}, ["profile_id"]),
            "set_settings": ({"changes": array(obj({"key": ID, "value": VALUE, "target": TARGET}, ["key", "value", "target"]), 100, 1)}, ["changes"]),
            "create_quality_profile": ({"name": {"type": "string", "minLength": 1, "maxLength": 120}, "source_profile_id": ID}, ["name", "source_profile_id"]),
            "save_quality_profile": ({"profile_id": ID}, ["profile_id"]),
            "set_object_transform": ({"object_id": ID, "position_mm": VECTOR, "scale": {"type": "array", "minItems": 3, "maxItems": 3, "items": {"type": "number", "minimum": 0.000001}},
                                      "rotation_quaternion": {"type": "array", "items": NUMBER, "minItems": 4, "maxItems": 4}}, ["object_id"]),
            "arrange_objects": ({"object_ids": array(ID, 100, 1)}, ["object_ids"]),
            "remove_objects": ({"object_ids": array(ID, 100, 1)}, ["object_ids"])}
        for name, (properties, required) in changes.items():
            properties.update(plan_id=ID, action_id=ID, request_id=ID)
            required = required + (["request_id"] if name == "load_model" else ["plan_id", "action_id", "request_id"])
            self.add(name, "Согласованное действие: " + name, properties, required, True)
        self.add("start_slicing", "Запустить штатный backend согласованного плана", {"plan_id": ID, "request_id": ID}, ["plan_id", "request_id"], True)
        self.add("save_project", "Сохранить проект штатным 3MFWorkspaceWriter", {"plan_id": ID, "export_target_id": ID, "request_id": ID}, ["plan_id", "export_target_id", "request_id"], True)
        self.add("export_gcode", "Экспорт актуального результата штатным GCodeWriter", {"operation_id": ID, "export_target_id": ID, "request_id": ID}, ["operation_id", "export_target_id", "request_id"], True)

    def add(self, name, description, properties=None, required=None, mutation=False):
        self.registry[name] = {"name": name, "description": description, "inputSchema": obj(properties, required), "mutation": mutation}

    def listing(self, read_only=False):
        return [{k: copy.deepcopy(v) for k, v in tool.items() if k != "mutation"}
                for tool in self.registry.values() if not read_only or not tool["mutation"]]

    def call(self, name, arguments, context):
        if name not in self.registry:
            fail("Неизвестный инструмент", "UNKNOWN_TOOL")
        tool = self.registry[name]
        validate(arguments, tool["inputSchema"])
        if name == "load_model" and context.get("phase") == "importing":
            sid, oid = context.get("session_id"), context.get("import_operation_id")
            operation = self.store.operation(sid, oid)
            if operation.get("file_id") != arguments["file_id"]:
                fail("Файл не разрешён операцией импорта", "APPROVAL_REQUIRED", 403)
            self.store.file(sid, arguments["file_id"])
            def reserve(session):
                key = "import:" + oid
                previous = session["action_results"].get(key)
                if previous:
                    return False
                session["action_results"][key] = {"operation_id": oid}
                return True
            if self.store.edit(sid, reserve):
                self.bridge.submit([operation])
            current = self.store.operation(sid, oid)
            return {"operation_id": oid, "status": current["status"],
                    "object_ids": self.store.file(sid, arguments["file_id"]).get("object_ids", [])}
        if tool["mutation"]:
            if self.authorize_change is None:
                fail("Нужно согласие на план", "APPROVAL_REQUIRED", 403)
            return self.authorize_change(name, arguments, context)
        if name == "search_knowledge":
            data = self.knowledge.search(**arguments)
        elif name in ("get_operation_status", "get_slice_result"):
            sid = context.get("session_id")
            if not sid:
                fail("Нужен контекст запуска", "APPROVAL_REQUIRED", 403)
            data = self.store.operation(sid, arguments["operation_id"])
            if name == "get_slice_result" and data["status"] != "completed":
                fail("Расчёт не завершён", "SLICE_FAILED", 409)
            if name == "get_slice_result":
                current = self.bridge.call("get_scene_snapshot", {})
                if data["snapshot_ref"] != current["snapshot_ref"]:
                    fail("Результат слайсинга устарел", "STALE_INPUT", 409)
        else:
            data = self.bridge.call(name, arguments)
        ref = {"id": uid(), "version": 1}
        if context.get("session_id"):
            def save(session):
                session["sources"][ref["id"]] = {"id": ref["id"], "version": 1, "kind": "tool", "tool": name, "data": data}
                if name == "search_knowledge":
                    for row in data["items"]:
                        if row["usable_as_evidence"]:
                            session["sources"][row["id"]] = {"id": row["id"], "version": row["version"], "kind": "knowledge", "data": row}
            self.store.edit(context["session_id"], save)
        return {"data": data, "source_refs": [ref], "snapshot_ref": data.get("snapshot_ref"), "warnings": data.get("warnings", [])}
