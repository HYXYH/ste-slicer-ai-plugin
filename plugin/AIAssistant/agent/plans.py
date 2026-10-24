"""Validated plans, explicit approvals and exact, idempotent MCP mutations."""
import copy
import threading
import time

from ..session import uid, now
from .contracts import fail, validate, source_ids
from .mcp_client import MCPClient
from .task_state import readiness

ACTION_ORDER = {"set_print_mode": 0, "select_material": 1, "select_quality_profile": 2,
                "create_quality_profile": 3, "set_settings": 4, "set_object_transform": 5,
                "arrange_objects": 6, "remove_objects": 7, "save_quality_profile": 8}


class Plans:
    def __init__(self, server):
        self.server, self.store, self.bridge = server, server.store, server.bridge
        self.lock = threading.Lock()
        self.request_lock = threading.Lock()
        self.tokens = {}
        self.expected = {}
        self.finished = threading.Event()

    def validate(self, session, plan, scene):
        if (plan["snapshot_ref"] != scene["snapshot_ref"] or plan["input_revision"] != session["input_revision"]
                or plan["map_revision"] != session["task_map"]["revision"]):
            fail("План устарел. Повторите анализ.", "STALE_INPUT", 409)
        if session["task_map"]["readiness"]["status"] != "ready_for_plan":
            fail("Карта задачи ещё не готова", "APPROVAL_REQUIRED", 409)
        targets = set(plan["target_object_ids"])
        objects = set(o["id"] for o in scene["objects"] if o["printable"])
        removed = set(oid for a in plan["actions"] if a["tool"] == "remove_objects" for oid in a["arguments"]["object_ids"])
        if not targets or not targets.issubset(objects) or objects - targets - removed:
            fail("В сцене есть другие модели. Включите их в задачу или явно согласуйте удаление.", "FOREIGN_OBJECTS", 409)
        configuration = scene["configuration"]
        choice = plan["selected"]
        if not isinstance(choice.get("profile_id"), str) or not choice["profile_id"]:
            fail("Сначала подтвердите профиль выбранных режима и материала", "NO_COMPATIBLE_PROFILE", 409)
        extruder = next((e for e in configuration["extruders"] if e["id"] == choice["extruder_id"]), None)
        if extruder is None:
            fail("Выбранный экструдер отсутствует", "OBJECT_NOT_FOUND", 409)
        current_profile = configuration["profile"]["custom"]["id"]
        if current_profile == "empty_quality_changes":
            current_profile = configuration["profile"]["quality"]["id"]
        required_changes = {"select_quality_profile": choice["profile_id"] != current_profile}
        if choice["mode"] != configuration["mode"]["id"]:
            fail("Нужна проверка профилей целевого режима. Выберите режим и повторите анализ.", "TARGET_CONFIGURATION_UNKNOWN", 409)
        profiles = self.bridge.call("find_compatible_profiles", {"mode": choice["mode"], "material_id": choice["material_id"], "extruder_id": choice["extruder_id"], "limit": 50})
        while not any(p["id"] == choice["profile_id"] and p["compatibility"] == "pass" for p in profiles["items"]):
            if not profiles["next_cursor"]:
                fail("Выбранный профиль не подтверждён", "NO_COMPATIBLE_PROFILE", 409)
            profiles = self.bridge.call("find_compatible_profiles", {"limit": 50, "cursor": profiles["next_cursor"]})
        capabilities = self.bridge.call("get_printer_capabilities", {})
        if choice["mode"] not in capabilities["modes"]:
            fail("Режим недоступен", "NO_COMPATIBLE_PROFILE", 409)
        seen, previous_order = set(), -1
        for action in plan["actions"]:
            if set(action) != {"id", "tool", "arguments", "before", "after", "reason", "source_refs"} or not action["reason"] or action["id"] in seen:
                fail("Неверная структура действия", "INVALID_MODEL_OUTPUT")
            seen.add(action["id"])
            name, args = action["tool"], action["arguments"]
            if name not in ACTION_ORDER or ACTION_ORDER[name] < previous_order:
                fail("Неверный порядок действий", "INVALID_MODEL_OUTPUT")
            previous_order = ACTION_ORDER[name]
            source_ids(action["source_refs"], session["sources"])
            validate(dict(args, plan_id=plan["id"], action_id=action["id"], request_id="validation"), self.server.tools.registry[name]["inputSchema"])
            affected = args.get("object_ids", [args["object_id"]] if "object_id" in args else [])
            if set(affected) - objects:
                fail("План ссылается на отсутствующий объект", "OBJECT_NOT_FOUND", 409)
            if name == "set_settings":
                self.bridge.call("validate_settings", {"changes": args["changes"]})
            if name == "set_print_mode" and args["mode"] != choice["mode"]:
                fail("Режим действия отличается от карточки плана")
            if name == "select_material" and (args["material_id"] != choice["material_id"] or args["extruder_id"] != choice["extruder_id"]):
                fail("Материал действия отличается от карточки плана")
            if name == "select_quality_profile" and args["profile_id"] != choice["profile_id"]:
                fail("Профиль действия отличается от карточки плана")
            if name == "set_object_transform" and "rotation_quaternion" in args:
                if abs(sum(n * n for n in args["rotation_quaternion"]) - 1) > 0.000001:
                    fail("Quaternion должен быть нормирован")
        for name, required in required_changes.items():
            if required and not any(a["tool"] == name for a in plan["actions"]):
                fail("Выбор профиля отсутствует в списке действий")
        return {"valid": True, "limitations": list(plan["limitations"])}

    def build(self, sid, proposal):
        session = self.store.internal(sid)
        selected = next(c for c in session["task_map"]["candidates"] if c["status"] == "selected")
        decision = next((d for d in proposal["decision_proposals"] if d.get("candidate_id") == selected["id"]), None)
        if decision is None:
            fail("Нет предложения по выбранному варианту", "INVALID_MODEL_OUTPUT")
        source_ids(decision["source_refs"], session["sources"])
        plan = {"id": uid(), "version": 1, "session_id": sid, "map_revision": session["task_map"]["revision"],
                "input_revision": session["input_revision"], "snapshot_ref": session["task_map"]["snapshot_ref"],
                "target_object_ids": session["task_map"]["target_object_ids"],
                "selected": {k: selected[k] for k in ("mode", "material_id", "profile_id", "extruder_id")},
                "actions": copy.deepcopy(decision["actions"]),
                "limitations": decision.get("limitations", []) + (
                    ["Режим прототипа: инженерные требования не подтверждены, риск принят пользователем"]
                    if session["task_map"]["readiness"].get("accepted_risk_ids") else []),
                "assumptions": copy.deepcopy(session["task_map"]["assumptions"]), "status": "draft", "approval": None,
                "completed_actions": [], "created_at": now()}
        scene = self.bridge.call("get_scene_snapshot", {})
        for action in plan["actions"]:
            name, args = action["tool"], action["arguments"]
            if name == "set_settings":
                before = []
                for change in args["changes"]:
                    values = self.bridge.call("get_current_configuration", {"keys": [change["key"]], "target": change["target"]})
                    before.append(dict(change, value=values["settings"].get(change["key"])))
                action["before"], action["after"] = before, copy.deepcopy(args["changes"])
            elif name == "set_object_transform":
                current = next((o for o in scene["objects"] if o["id"] == args["object_id"]), {})
                action["before"] = {k: current.get(k) for k in args if k != "object_id"}
                action["after"] = {k: v for k, v in args.items() if k != "object_id"}
            elif name == "set_print_mode":
                action["before"], action["after"] = scene["configuration"]["mode"]["id"], args["mode"]
            elif name == "remove_objects":
                action["before"], action["after"] = args["object_ids"], []
            else:
                action["before"] = {"profile": scene["configuration"]["profile"], "extruders": scene["configuration"]["extruders"]} if name.startswith("select_") else "Текущее состояние сцены"
                action["after"] = copy.deepcopy(args)
        plan["validation"] = self.validate(session, plan, scene)
        plan["status"] = "validated"
        self.store.edit(sid, lambda s: s["plans"].update({plan["id"]: plan}))
        return plan

    def approve(self, sid, pid, version):
        session = self.store.internal(sid)
        plan = session["plans"].get(pid)
        if not plan or plan["version"] != version or plan["status"] not in ("validated", "approved"):
            fail("Нужен актуальный проверенный план", "APPROVAL_REQUIRED", 409)
        scene = self.bridge.call("get_scene_snapshot", {})
        self.validate(session, plan, scene)
        def update(s):
            if s["input_revision"] != plan["input_revision"]:
                fail("Требования изменились", "STALE_INPUT", 409)
            stored = s["plans"][pid]
            stored["approval"] = {"plan_id": pid, "version": version, "snapshot_ref": plan["snapshot_ref"], "created_at": now()}
            stored["status"] = "approved"
            return stored
        return self.store.edit(sid, update)

    def _grant(self, sid, plan, phase):
        token = uid() + uid()
        context = {"session_id": sid, "run_id": uid(), "plan_id": plan["id"], "phase": phase,
                   "input_revision": plan["input_revision"]}
        self.tokens[token] = context
        self.server.mcp.grant(token, context)
        return token, context

    def authorize(self, name, arguments, context):
        if context.get("phase") not in ("applying", "slicing", "exporting") or self.finished.is_set():
            fail("Нет разрешения на изменение", "APPROVAL_REQUIRED", 403)
        sid, pid = context["session_id"], context["plan_id"]
        session = self.store.internal(sid)
        plan = session["plans"].get(pid)
        if not plan or not plan.get("approval") or plan["input_revision"] != session["input_revision"]:
            fail("Согласованный план устарел", "STALE_INPUT", 409)
        if arguments.get("plan_id", pid) != pid:
            fail("Чужой план", "APPROVAL_REQUIRED", 403)
        request_key = name + ":" + arguments["request_id"]
        if context["phase"] == "applying":
            # A fresh request_id does not authorize repeating the same action.
            action_key = "action:" + pid + ":" + arguments.get("action_id", "")
            previous_action = session["action_results"].get(action_key)
            if previous_action:
                original = {k: v for k, v in previous_action["arguments"].items() if k != "request_id"}
                if original != {k: v for k, v in arguments.items() if k != "request_id"}:
                    fail("Аргументы уже выполненного действия изменены", "APPROVAL_REQUIRED", 403)
                if "result" not in previous_action:
                    fail("Исход действия не подтверждён", "TOOL_TIMEOUT", 409)
                return previous_action["result"]
        previous = session["action_results"].get(request_key)
        if previous:
            if previous["arguments"] != arguments:
                fail("request_id использован с другими аргументами", "INVALID_ARGUMENTS", 409)
            if "result" not in previous:
                fail("Исход операции неизвестен. Проверьте фактическое состояние.", "TOOL_TIMEOUT", 409)
            return previous["result"]
        action_args = {k: v for k, v in arguments.items() if k not in ("plan_id", "action_id", "request_id")}
        if context["phase"] == "applying":
            action = next((a for a in plan["actions"] if a["id"] == arguments.get("action_id")), None)
            if (not action or action["tool"] != name or action["arguments"] != action_args
                    or plan["status"] != "applying" or context.get("action_id") != action["id"]):
                fail("Действие не совпадает с согласованным планом", "APPROVAL_REQUIRED", 403)
        elif context["phase"] == "slicing":
            if name != "start_slicing" or plan["status"] != "applied":
                fail("Слайсинг разрешён только после применения", "APPROVAL_REQUIRED", 403)
        else:
            if name not in ("save_project", "export_gcode"):
                fail("Инструмент недоступен при экспорте", "APPROVAL_REQUIRED", 403)
            operation = self.store.operation(sid, context["operation_id"])
            if operation.get("plan_id") != pid or operation["status"] != "completed" or arguments.get("operation_id", operation["id"]) != operation["id"]:
                fail("Нет завершённого расчёта этого плана", "STALE_INPUT", 409)
        expected = self.expected.get(pid)
        if not expected:
            fail("Нет актуального снимка выполнения", "STALE_INPUT", 409)
        self.store.edit(sid, lambda s: s["action_results"].update({request_key: {"arguments": arguments}}))
        if context["phase"] == "applying":
            self.store.edit(sid, lambda s: s["action_results"].update({action_key: {"arguments": arguments}}))
        if name in ("save_project", "export_gcode"):
            bridge_args = {"session_id": sid, "export_target_id": arguments["export_target_id"]}
        elif name == "start_slicing":
            bridge_args = {"session_id": sid, "plan_id": pid}
        elif name == "arrange_objects":
            bridge_args = dict(action_args, session_id=sid)
        elif name == "load_model":
            fail("Дополнительный импорт выполняется загрузкой пользователя и требует нового анализа", "STALE_INPUT", 409)
        else:
            bridge_args = action_args
        observed = self.bridge.call(name, bridge_args, expected_ref=expected)
        result, actual = observed["result"], observed["snapshot_ref"]
        if context["phase"] == "exporting":
            result["stale"] = actual != operation["snapshot_ref"]
        if name == "arrange_objects":
            deadline = time.monotonic() + 120
            while self.store.operation(sid, result["operation_id"])["status"] == "running":
                if time.monotonic() > deadline or self.finished.wait(0.25):
                    fail("Расстановка ещё не подтверждена", "TOOL_TIMEOUT", 504)
            result = self.store.operation(sid, result["operation_id"])
            if result["status"] != "completed":
                fail("Расстановка не завершилась успешно", "PARTIAL_APPLICATION", 409)
            actual = result["result"]["snapshot_ref"]
        self.expected[pid] = actual
        self.store.edit(sid, lambda s: s["action_results"][request_key].update(result=result, snapshot_ref=actual))
        if context["phase"] == "applying":
            self.store.edit(sid, lambda s: s["action_results"][action_key].update(result=result, snapshot_ref=actual))
        return result

    def apply(self, sid, pid, request_id=None):
        if request_id is not None:
            with self.request_lock:
                key = "apply:" + request_id
                saved = self.store.internal(sid)["workflow_requests"].get(key)
                if saved:
                    if saved["plan_id"] != pid:
                        fail("request_id уже использован", status=409)
                    return saved["result"]
                result = self.apply(sid, pid)
                self.store.edit(sid, lambda s: s["workflow_requests"].update({key: {"plan_id": pid, "result": result}}))
                return result
        if not self.lock.acquire(False):
            fail("Другая операция изменения уже выполняется", "BUSY", 409)
        try:
            session = self.store.internal(sid)
            plan = session["plans"].get(pid)
            if not plan or plan["status"] != "approved":
                fail("Сначала согласуйте план", "APPROVAL_REQUIRED", 409)
            self.validate(session, plan, self.bridge.call("get_scene_snapshot", {}))
            self.expected[pid] = plan["snapshot_ref"]
            self.store.edit(sid, lambda s: (s["plans"][pid].update(status="applying"), s.update(workflow_state="applying")))
            thread = threading.Thread(target=self._apply_worker, args=(sid, plan), name="STE apply")
            thread.daemon = True
            thread.start()
            return {"plan_id": pid, "status": "applying"}
        except Exception:
            self.lock.release()
            raise

    def _measured_call(self, client, sid, rid, name, arguments):
        operation_id, started = uid(), time.monotonic()
        self.store.event(sid, rid, "tool_started", tool=name, operation_id=operation_id, arguments=arguments)
        try:
            result = client.call(name, arguments)
        except Exception as error:
            self.store.event(sid, rid, "tool_failed", tool=name, operation_id=operation_id,
                             code=getattr(error, "code", "APPLICATION_FAILED"),
                             duration_ms=round((time.monotonic() - started) * 1000, 2))
            raise
        self.store.event(sid, rid, "tool_finished", tool=name, operation_id=operation_id, result=result,
                         duration_ms=round((time.monotonic() - started) * 1000, 2))
        return result

    def _apply_worker(self, sid, plan):
        pid = plan["id"]
        token, context = self._grant(sid, plan, "applying")
        client = MCPClient(self.server, token)
        try:
            client.initialize()
            for action in plan["actions"]:
                context["action_id"] = action["id"]
                args = dict(action["arguments"], plan_id=pid, action_id=action["id"], request_id=pid + ":" + action["id"])
                result = self._measured_call(client, sid, context["run_id"], action["tool"], args)
                self.store.edit(sid, lambda s: s["plans"][pid]["completed_actions"].append(action["id"]))
            self.store.edit(sid, lambda s: s["plans"][pid].update(status="applied", applied_snapshot_ref=self.expected[pid]))
            context["phase"] = "slicing"
            self.store.edit(sid, lambda s: s.update(workflow_state="slicing"))
            result = self._measured_call(client, sid, context["run_id"], "start_slicing", {"plan_id": pid, "request_id": pid + ":slice"})
            self.store.event(sid, context["run_id"], "progress", message="Штатный слайсинг запущен", result=result)
        except Exception as error:
            self.store.edit(sid, lambda s: (s["plans"][pid].update(status="failed"), s.update(workflow_state="failed")))
            self.store.event(sid, context["run_id"], "error", code=getattr(error, "code", "APPLICATION_FAILED"), message="Применение остановлено: " + self.server.settings.redact(str(error)))
        finally:
            client.close()
            self.server.mcp.revoke(token)
            self.tokens.pop(token, None)
            self.lock.release()

    def export(self, sid, operation_id, target_id, request_id):
        if not self.lock.acquire(timeout=5):
            fail("Другой экспорт или применение ещё выполняется", "BUSY", 409)
        try:
            operation = self.store.operation(sid, operation_id)
            session = self.store.internal(sid)
            plan = session["plans"].get(operation.get("plan_id"))
            if not plan or operation["status"] != "completed":
                fail("Нет актуального результата", "STALE_INPUT", 409)
            self.expected[plan["id"]] = operation["snapshot_ref"]
            token, context = self._grant(sid, plan, "exporting")
            context["operation_id"] = operation_id
            client = MCPClient(self.server, token)
            results = []
            try:
                client.initialize()
                self.store.edit(sid, lambda s: s.update(workflow_state="exporting"))
                for name in ("save_project", "export_gcode"):
                    args = {"export_target_id": target_id, "request_id": request_id + ":" + name}
                    args["plan_id" if name == "save_project" else "operation_id"] = plan["id"] if name == "save_project" else operation_id
                    result = self._measured_call(client, sid, context["run_id"], name, args)
                    results.append(result)
                    if result.get("stale"):
                        fail("Сцена изменилась при записи. Файл сохранён, но не считается актуальным.", "STALE_INPUT", 409)
                self.store.edit(sid, lambda s: (s["exports"].append({"operation_id": operation_id, "files": results}), s.update(workflow_state="completed")))
                return {"files": results, "operation_id": operation_id}
            except Exception:
                self.store.edit(sid, lambda s: (s["exports"].append({"operation_id": operation_id, "files": results, "partial": True}), s.update(workflow_state="result_ready")))
                raise
            finally:
                client.close()
                self.server.mcp.revoke(token)
                self.tokens.pop(token, None)
        finally:
            self.lock.release()

    def close(self):
        self.finished.set()
        for token in list(self.tokens):
            self.server.mcp.revoke(token)
