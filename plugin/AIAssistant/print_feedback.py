"""Human print reports and single-setting trials, with explicit consent and freshness checks."""
import copy
import math
import os
import shutil
import threading

from .session import ApiError, uid, now
from .experience import ExperienceStore, conditions, setting, encode
from .print_diagnostics import DEFECTS, catalog, detect, EXTERNAL
from .agent.recommend import load_records, material_record


def text(value, maximum=4000):
    if not isinstance(value, str) or len(value) > maximum:
        raise ApiError("Неверный текст результата печати")
    return value.strip()


class PrintFeedback:
    def __init__(self, server):
        self.server, self.store, self.bridge = server, server.store, server.bridge
        self.db = ExperienceStore(self.store.root)
        self.lock = threading.RLock()

    def state(self, sid):
        session = self.store.internal(sid)
        operations = [{"id": o["id"], "created_at": o.get("created_at", ""),
                       "models": [m.get("name", "Модель") for m in o["print_snapshot"]["objects"]],
                       "context": o["print_snapshot"]["context"]}
                      for o in session["operations"].values() if o.get("kind") == "slice" and o["status"] == "completed" and o.get("print_snapshot")]
        return {"operations": sorted(operations, key=lambda o: o["created_at"]), "catalog": catalog(), "prints": self.db.library(sid)["prints"], "external": EXTERNAL}

    def note(self, sid, message):
        def save(session):
            session["messages"].append({"id": uid(), "role": "assistant", "text": message, "file_ids": [], "created_at": now()})
        self.store.edit(sid, save)

    def owned(self, sid, pid):
        item = self.db.get(pid)
        if item["session_id"] != sid:
            raise ApiError("Печать относится к другому чату", 409)
        return item

    def report(self, sid, body):
        from .agent.contracts import validate, obj, ID, TEXT, array
        validate(body, obj({"operation_id": ID, "request_id": ID, "print_id": ID, "printed": {"type": "boolean", "enum": [True]},
                           "result": {"enum": ["good", "defects", "not_checked", "unknown"]},
                           "defects": array({"enum": list(DEFECTS)}, 18), "text": {"type": "string", "maxLength": 4000},
                           "answers": {"type": "object"}, "measurements": {"type": "object"},
                           "photo_ids": array(ID, 12)}, ["operation_id", "request_id", "printed", "result", "defects", "text", "answers", "measurements", "photo_ids"]))
        session = self.store.internal(sid)
        operation = session["operations"].get(body["operation_id"])
        if not operation or operation.get("kind") != "slice" or operation["status"] != "completed" or not operation.get("print_snapshot"):
            raise ApiError("Нужен завершённый расчёт со снимком параметров. Для старой печати пересчитайте модель.", 409)
        defects = list(dict.fromkeys(body["defects"] + (detect(body["text"]) if body["result"] == "defects" else [])))
        if body["result"] != "defects" and defects:
            raise ApiError("Выберите результат «Есть дефекты»")
        answers = body["answers"]
        if set(answers) - (set(DEFECTS) | {"external"}) or any(not isinstance(v, str) or len(v) > 1000 for v in answers.values()):
            raise ApiError("Неверные ответы диагностики")
        measures = body["measurements"]
        if set(measures) - {"expected_mm", "actual_mm"}:
            raise ApiError("Измерения указываются в миллиметрах")
        for values in measures.values():
            if not isinstance(values, list) or len(values) != 3 or any(v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0) for v in values):
                raise ApiError("Укажите три положительных размера в мм или оставьте неизвестные пустыми")
        photos = []
        for fid in dict.fromkeys(body["photo_ids"]):
            attachment = session["files"].get(fid)
            if not attachment or attachment["kind"] != "image":
                raise ApiError("Фотография должна быть вложением этого чата")
            # Keep a durable copy: removing a chat must not destroy the print evidence.
            photo_id = fid + attachment["extension"]
            destination = os.path.join(self.db.root, photo_id)
            if not os.path.exists(destination):
                shutil.copyfile(attachment["path"], destination)
            photos.append({"id": photo_id, "name": attachment["name"]})
        data = {"kind": "user_observation", "result": body["result"], "text": text(body["text"]), "defects": defects,
                "print_kind": "calibration" if session.get("calibration_for") else "ordinary",
                "reported_defects": body["defects"], "inferred_defects": [k for k in defects if k not in body["defects"]],
                "answers": answers, "measurements": measures, "photos": photos, "printed": True}
        with self.lock:
            item = self.db.record(sid, operation, data, body["request_id"], body.get("print_id"))
            marker = "print-report-" + body["request_id"]
            if not any(m.get("experience_request") == marker for m in self.store.internal(sid)["messages"]):
                def saved(s):
                    s["messages"].append({"id": uid(), "role": "user", "created_at": now(), "file_ids": body["photo_ids"], "experience_request": marker,
                        "text": "Сохранён результат фактической печати: " + {"good": "дефектов нет", "defects": "; ".join(DEFECTS[k]["label"] for k in defects) or "описаны пользователем",
                        "not_checked": "не проверялось", "unknown": "не могу определить, нужны фотографии"}[body["result"]] + ". " + data["text"] + "\nЗапись опыта: " + item["id"] + "."})
                self.store.edit(sid, saved)
        return {"print": item, "questions": self.questions(item), "external": EXTERNAL}

    def questions(self, item):
        report = item["observations"][-1]["data"]
        if report["result"] == "unknown":
            return [{"id": "photos", "text": "Приложите фотографии: общий вид, сбоку и снизу. Что видно до зачистки?", "choices": []}]
        if report["result"] != "defects":
            return []
        if not report["defects"]:
            return [{"id": "location", "text": "Где виден дефект: первый слой, стенки, верх, мелкие элементы, нависания или размеры?", "choices": []}]
        return [{"id": key, "text": DEFECTS[key]["label"] + ": какой признак совпадает?",
                 "choices": DEFECTS[key]["choices"]} for key in report["defects"] if key not in report["answers"]][:2]

    def propose(self, sid, pid, body):
        from .agent.contracts import validate, obj
        validate(body, obj({"defect": {"enum": list(DEFECTS)}, "choice": {"enum": ["0", "1", "2"]},
                           "external_checked": {"type": "boolean"}}, ["defect", "choice", "external_checked"]))
        item = self.owned(sid, pid)
        observation = item["observations"][-1]
        if observation.get("excluded"):
            raise ApiError("Наблюдение исключено. Уточните или восстановите его в базе знаний", 409)
        if body["defect"] not in observation["data"]["defects"]:
            raise ApiError("Сначала сохраните наблюдение об этом дефекте")
        choice = DEFECTS[body["defect"]]["choices"][int(body["choice"])]
        if not body["external_checked"]:
            return {"manual": EXTERNAL}
        if not choice["key"]:
            return {"manual": choice["reason"] + ". " + EXTERNAL}
        if len(item["snapshot"]["context"].get("extruders", [])) != 1:
            return {"manual": "Укажите затронутый экструдер и проверьте его отдельно. Автоматический эксперимент первого этапа доступен для одного экструдера."}
        current = self.bridge.call("get_print_context", {})
        if conditions(current) is None or conditions(current) != conditions(item["snapshot"]):
            raise ApiError("Восстановите модель и параметры этой печати перед подбором исправления", 409, "STALE_INPUT")
        key = choice["key"]
        targets = [r["target"] for r in current["settings"] if r["target"]["scope"] == "extruder"] + [{"scope": "global"}]
        for target in targets:
            try:
                definition = self.bridge.call("get_setting_definitions", {"keys": [key], "target": target})["items"][0]
                if definition["enabled"] is not True or target["scope"] == "extruder" and definition.get("settable_per_extruder") is not True:
                    continue
                before = setting(current, key, target)
                after = self.next_value(choice, before, current, definition)
                change = {"key": key, "value": after, "target": target}
                self.bridge.call("validate_settings", {"changes": [change]})
                break
            except ApiError:
                continue
        else:
            return {"manual": "Для этой гипотезы нет доступного безопасного изменения. Проверьте ограничения профиля или другой признак. " + EXTERNAL}
        data = dict(body, observed_symptom=choice["symptom"], key=key, before=before, after=after, target=target, unit=definition.get("unit"), reason=choice["reason"],
                    state="proposed", snapshot_ref=current["snapshot_ref"], approved_at=None, outcome=None)
        return {"adjustment": self.db.propose(pid, observation["id"], data)}

    def next_value(self, choice, before, snapshot, definition):
        key, direction = choice["key"], choice["direction"]
        if direction == "on":
            value = True
        elif direction == "brim":
            value = "brim"
        else:
            if isinstance(before, bool) or not isinstance(before, (int, float)):
                raise ApiError("Нет числового исходного значения")
            delta = 1 if definition["type"] == "int" else max(abs(before) * .1, .01)
            if key.startswith("material_flow") or key == "skin_material_flow":
                delta = 2
            if "temperature" in key:
                delta = 5
            value = before + (delta if direction == "up" else -delta)
            if definition["type"] == "int":
                value = int(value)
            else:
                value = round(value, 3)
        if value == before:
            raise ApiError("Настройка уже имеет предложенное значение")
        # Manufacturer/material reference bounds supplement actual machine setting validation.
        if "temperature" in key or "fan_speed" in key:
            materials = [material_record(load_records(), (e.get("material") or {}).get("id")) for e in snapshot["context"]["extruders"]]
            limit_key = "bed_temperature_c" if key == "material_bed_temperature" else "print_temperature_c" if "temperature" in key else "cooling_percent"
            limits = [r.get("limits", {}).get(limit_key) for r in materials if r]
            if not limits or any(v is None for v in limits):
                raise ApiError("Неизвестен диапазон материала; требуется ручная проверка")
            for bound in limits:
                low, high = (bound[0], bound[-1]) if isinstance(bound, list) else (bound, bound)
                if not low <= value <= high:
                    raise ApiError("Пробное значение выходит за диапазон материала")
        if key == "layer_height":
            nozzle = min(e["nozzle_mm"] for e in snapshot["context"]["extruders"])
            if not .1 * nozzle <= value <= .8 * nozzle:
                raise ApiError("Высота слоя не соответствует соплу")
        return value

    def apply(self, sid, aid, consent, rollback=False):
        if consent is not True:
            raise ApiError("Требуется явное согласие на изменение одного параметра", 403, "APPROVAL_REQUIRED")
        with self.lock:
            if not self.server.plans.lock.acquire(False):
                raise ApiError("Дождитесь завершения применения или экспорта", 409)
            try:
                for row in self.store.listing()["sessions"]:
                    session = self.store.internal(row["id"])
                    if session["workflow_state"] in ("applying", "slicing", "exporting", "importing", "waiting_import") or any(r["status"] in ("queued", "running") for r in session["runs"].values()):
                        raise ApiError("Дождитесь завершения анализа или операции слайсера", 409, "BUSY")
                adjustment = self.db.adjustment(aid)
                item = self.owned(sid, adjustment["print_id"])
                data = adjustment["data"]
                expected_state = ("applied", "evaluated") if rollback else ("proposed",)
                if data["state"] == ("rolled_back" if rollback else "applied"):
                    return adjustment
                if data["state"] not in expected_state:
                    raise ApiError("Этот эксперимент уже завершён", 409)
                current = self.bridge.call("get_print_context", {})
                baseline = data.get("applied_snapshot") if rollback else item["snapshot"]
                if conditions(current) is None or conditions(current) != conditions(baseline):
                    raise ApiError("Настройки или модель изменились; восстановите условия эксперимента", 409, "STALE_INPUT")
                if not rollback and current["snapshot_ref"] != data["snapshot_ref"]:
                    raise ApiError("Предложение устарело; заново выберите гипотезу", 409, "STALE_INPUT")
                change = {"key": data["key"], "value": data["before"] if rollback else data["after"], "target": data["target"]}
                self.bridge.call("validate_settings", {"changes": [change]})
                # Reserve before mutation: an interrupted call must never silently run twice.
                self.db.update_adjustment(aid, {"state": "rolling_back" if rollback else "applying", "approved_at": now()})
                try:
                    observed = self.bridge.call("set_settings", {"changes": [change]}, expected_ref=current["snapshot_ref"])
                    applied = self.bridge.call("get_print_context", {})
                    if observed["snapshot_ref"] != applied["snapshot_ref"] or setting(applied, data["key"], data["target"]) != change["value"]:
                        raise ApiError("Слайсер не подтвердил изменение", 409)
                except Exception:
                    self.db.update_adjustment(aid, {"state": "unknown", "error": "Проверьте фактические настройки: исход операции не подтверждён"})
                    raise
                result = self.db.update_adjustment(aid, {"state": "rolled_back" if rollback else "applied", "rollback_snapshot" if rollback else "applied_snapshot": applied,
                                                        "applied_at": now()}, "rejected" if rollback else None)
                def stale(session):
                    for plan in session["plans"].values():
                        plan["status"] = "stale"
                    session["workflow_state"] = "stale"
                self.store.edit(sid, stale)
                self.note(sid, ("Выполнен откат: " if rollback else "С согласия пользователя изменён один параметр: ") + data["key"] +
                          " = " + str(change["value"]) + ". " + ("Выберите другую причину в результате исходной печати." if rollback else "Подготовьте повторную печать в форме результата: параметры будут сохранены без нового подбора."))
                return result
            finally:
                self.server.plans.lock.release()

    def repeat_plan(self, sid, aid):
        adjustment = self.db.adjustment(aid)
        item = self.owned(sid, adjustment["print_id"])
        data = adjustment["data"]
        if data["state"] != "applied":
            raise ApiError("Сначала примените согласованное изменение", 409)
        current = self.bridge.call("get_print_context", {})
        if conditions(current) != conditions(data["applied_snapshot"]):
            raise ApiError("Условия повторной печати изменились", 409, "STALE_INPUT")
        session = self.store.internal(sid)
        original = session["operations"].get(item["operation_id"], {})
        original_plan = session["plans"].get(original.get("plan_id"))
        if not original_plan:
            raise ApiError("Исходный согласованный план не найден", 409)
        from .agent.task_state import readiness
        source_id = uid()
        refs = [{"id": source_id, "version": 1}]
        def prepare(s):
            s["sources"][source_id] = {"id": source_id, "version": 1, "kind": "tool", "tool": "get_print_context", "data": current}
            previous = next((c for c in s["task_map"]["candidates"] if c.get("status") == "selected"), {})
            s["task_map"].update(snapshot_ref=current["snapshot_ref"], candidates=[dict(previous, **dict(original_plan["selected"], id="print-trial", status="selected"))],
                                  target_object_ids=[o["id"] for o in current["objects"]])
            s["task_map"]["revision"] += 1
            s["task_map"]["readiness"] = readiness(s["task_map"], {"objects": current["objects"], "snapshot_ref": current["snapshot_ref"]}, s["sources"])
        self.store.edit(sid, prepare)
        plan = self.server.plans.build(sid, {"decision_proposals": [{"candidate_id": "print-trial", "actions": [], "source_refs": refs,
                                           "limitations": ["Повторная печать проверяет одну гипотезу; исходные инженерные ограничения сохраняются"] + original_plan["limitations"]}]})
        self.store.edit(sid, lambda s: s.update(workflow_state="plan_ready"))
        return plan

    def evaluate(self, sid, aid, body):
        from .agent.contracts import validate, obj, ID
        validate(body, obj({"repeat_print_id": ID, "outcome": {"enum": ["resolved", "better", "worse", "unchanged", "not_checked", "unknown"]}}, ["repeat_print_id", "outcome"]))
        with self.lock:
            adjustment = self.db.adjustment(aid)
            original = self.owned(sid, adjustment["print_id"])
            repeated = self.owned(sid, body["repeat_print_id"])
            data = adjustment["data"]
            if data.get("outcome") and data["outcome"] not in ("unknown", "not_checked"):
                if data["outcome"] == body["outcome"] and data.get("repeat_print_id") == repeated["id"]:
                    return adjustment
                raise ApiError("Результат эксперимента уже записан", 409)
            if data["state"] != "applied" or repeated["id"] == original["id"] or repeated["created_at"] <= data["applied_at"]:
                raise ApiError("Нужна новая фактическая печать после применения изменения", 409)
            if conditions(repeated["snapshot"]) is None or conditions(repeated["snapshot"]) != conditions(data["applied_snapshot"]):
                raise ApiError("Повторная печать выполнена в других условиях; эффект одного изменения не подтверждён", 409)
            report = repeated["observations"][-1]["data"]
            if repeated["observations"][-1].get("excluded"):
                raise ApiError("Наблюдение повторной печати исключено из базы знаний", 409)
            if body["outcome"] in ("resolved", "better", "worse", "unchanged") and report["result"] in ("unknown", "not_checked"):
                raise ApiError("Сначала осмотрите повторную печать и уточните её результат")
            if body["outcome"] == "resolved" and (report["result"] in ("unknown", "not_checked") or data["defect"] in report["defects"]):
                raise ApiError("Наблюдения не подтверждают исчезновение дефекта")
            degree = {"resolved": "observation", "better": "supported", "worse": "rejected", "unchanged": "rejected"}.get(body["outcome"], "hypothesis")
            result = self.db.update_adjustment(aid, dict(body, state="applied" if body["outcome"] in ("unknown", "not_checked") else "evaluated", evaluated_at=now()), degree)
            self.note(sid, "Результат проверки: " + {"resolved": "дефект исчез", "better": "стало лучше", "worse": "стало хуже", "unchanged": "без изменений", "unknown": "не могу определить", "not_checked": "не проверялось"}[body["outcome"]] +
                      (". Предлагаю вернуть исходное значение и проверить другую причину; откат требует согласия." if degree == "rejected" else ". Наблюдение сохранено вместе с условиями печати."))
            return result
