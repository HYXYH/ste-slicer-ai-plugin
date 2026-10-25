"""Derived user knowledge and page projections. SQLite remains the only experience store."""
import copy
import hashlib
import json
import math

from .experience import conditions, encode
from .print_diagnostics import DEFECTS, catalog, EXTERNAL
from .session import ApiError
from .user_view import field_label

STATUS = {"hypothesis": ("Гипотеза", "#9471c6"), "observation": ("Наблюдение", "#2586c6"),
          "confirmed": ("Подтверждено", "#159a78"), "conflicting": ("Противоречиво", "#d77b38")}
TYPES = {"configuration": "Рабочая конфигурация", "problem": "Выявленная проблема", "correction": "Эффективное исправление"}
RESULTS = {"good": "Дефектов нет", "defects": "Есть дефекты", "not_checked": "Не проверялось", "unknown": "Не могу определить"}
OUTCOMES = {"resolved": "Дефект исчез", "better": "Стало лучше", "worse": "Стало хуже", "unchanged": "Без изменений",
            "not_checked": "Не проверялось", "unknown": "Не могу определить"}
CALIBRATION = [
    {"title": "Что такое калибровка?", "text": "Справочник даёт отправную точку. Результат зависит от конкретного принтера, сопла, материала и геометрии. Проверка на реальной печати помогает уточнить параметры.", "state": "Принцип"},
    {"title": "Что работает сейчас", "text": "После обычной или калибровочной печати можно сохранить наблюдения, фотографии и измерения, проверить одну гипотезу и сравнить результат. Повторённые независимые проверки формируют подтверждённые выводы. Калибровка необязательна.", "state": "Реализовано"},
    {"title": "Когда предложить тест — планируемый механизм", "text": "После подбора параметров ассистент будет искать сопоставимые успехи и неудачи. Планируется модель Beta-Binomial с весами схожести условий, затем оценка полезности теста. Сейчас эти расчёты не выполняются; численных вероятностей нет.", "state": "Планируется"},
    {"title": "Польза теста", "text": "U = P(дефекта) × P(обнаружения) × P(исправления) × потери от неудачи − стоимость теста. Это планируемый критерий: стоимость, потери и вероятности ещё не оцениваются автоматически.", "state": "Планируется"},
    {"title": "Два разных показателя", "text": "Готовность данных показывает, известны ли модель, конфигурация и требования. Подтверждённость параметров относится к реальным результатам в сопоставимых условиях. Готовность 100/100 не обещает печать без дефектов.", "state": "Важно"},
    {"title": "Как проходит проверка", "text": "Подбор тестовой модели → подготовка и печать → опрос и измерения → диагностика → уточнение одного параметра → повторная проверка → сохранение опыта. Автоматический выбор специальной тестовой модели пока планируется; встроенный куб доступен отдельно.", "state": "Процесс"},
    {"title": "Как ассистент учится", "text": "Результат обычной детали — тоже источник опыта. Сообщение пользователя сохраняется как наблюдение, причина — как гипотеза. Исправление становится подтверждённым после нескольких независимых сопоставимых проверок. Противоречия, исправления и исключения наблюдений пересчитывают знания.", "state": "Реализовано"}]


def effective_report(item):
    if not item.get("observations") or item["observations"][-1].get("excluded"):
        return None
    return item["observations"][-1]


def knowledge_conditions(snapshot, excluded_key=None, defect=None):
    result = conditions(snapshot, excluded_key)
    if result is None or not snapshot.get("settings"):
        return None
    features = []
    for model in snapshot.get("objects", []):
        dims = model.get("dimensions_mm")
        if not isinstance(dims, list) or len(dims) != 3 or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in dims):
            return None
        longest, shortest = max(dims), min(dims)
        features.append({"size_class": "small" if longest < 30 else "medium" if longest <= 100 else "large",
                         "shape": "flat" if shortest / longest < .15 else "slender" if shortest / longest < .35 else "compact",
                         "axis_ratios": [round(v / longest, 1) for v in dims],
                         "dimensions_mm": dims if defect == "dimensions" else None,
                         "orientation": model.get("rotation_quaternion"),
                         "per_mesh_settings": {k: v for k, v in (model.get("per_mesh_settings") or {}).items() if k != excluded_key},
                         "extruder_id": model.get("extruder_id")})
    if not features:
        return None
    result["objects"] = sorted(features, key=encode)
    def normalized(value):
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, dict):
            return {k: normalized(v) for k, v in value.items()}
        if isinstance(value, list):
            return [normalized(v) for v in value]
        return value
    return normalized(result)


def stable_id(kind, scope, extra):
    return kind + "-" + hashlib.sha256(encode([scope, extra]).encode("utf-8")).hexdigest()[:24]


def derive_conclusions(library):
    prints = {p["id"]: p for p in library["prints"]}
    groups = {}
    def parameters(snapshot, defect=None):
        keys = {a["key"] for a in DEFECTS.get(defect, {}).get("choices", []) if a["key"]} if defect else {
            "layer_height", "material_print_temperature", "material_bed_temperature", "speed_print", "material_flow", "cool_fan_speed", "wall_line_count", "infill_sparse_density"}
        return [{"key": k, "before": value, "after": value, "target": scope["target"], "unit": None}
                for scope in snapshot.get("settings", []) for k, value in sorted(scope["values"].items()) if k in keys]
    def group(kind, scope, extra, description):
        key = stable_id(kind, scope, extra)
        if key not in groups:
            groups[key] = {"id": key, "type": kind, "conditions": scope, "description": description,
                           "parameters": [], "print_ids": [], "lesson_ids": [], "verified_lesson_ids": [], "defects": [], "evidence": [], "created_at": ""}
        return groups[key]
    def evidence(row, item, outcome, supporting, pair=None):
        row["print_ids"] = sorted(set(row["print_ids"] + [item["id"]] + (pair or [])))
        row["created_at"] = max(row["created_at"], item["created_at"])
        row["evidence"].append({"print_id": item["id"], "outcome": outcome, "supporting": supporting, "pair": pair or [item["id"]], "date": item["created_at"]})
    for item in library["prints"]:
        report = effective_report(item)
        scope = knowledge_conditions(item["snapshot"])
        if report is None:
            continue
        data = report["data"]
        if data["result"] not in ("good", "defects"):
            continue
        # Unknown legacy settings may be displayed, but never merged or reused as known conditions.
        grouping_scope = scope if scope is not None else {"unknown_conditions_for_print": item["id"]}
        row = group("configuration", grouping_scope, "visual", "Конфигурация проверена на отсутствие видимых дефектов")
        row["parameters"] = parameters(item["snapshot"])
        evidence(row, item, data["result"], data["result"] == "good")
        for defect in data.get("defects", []):
            if defect not in DEFECTS or defect in data.get("inferred_defects", []):
                continue
            row = group("problem", grouping_scope, defect, DEFECTS[defect]["label"])
            row["defects"] = [defect]
            row["parameters"] = parameters(item["snapshot"], defect)
            evidence(row, item, "defects", True)
        if data["result"] == "defects" and not data.get("defects"):
            row = group("problem", grouping_scope, "unspecified", "Есть дефекты: характер проблемы пока не уточнён")
            evidence(row, item, "defects", True)
    # A clean checked print opposes claims of a persistent problem under the same conditions.
    for row in list(groups.values()):
        if row["type"] == "problem":
            for item in library["prints"]:
                report = effective_report(item)
                if report and report["data"]["result"] == "good" and knowledge_conditions(item["snapshot"]) == row["conditions"]:
                    evidence(row, item, "good", False)
    for lesson in library["lessons"]:
        data = lesson["data"]
        original = prints.get(data.get("print_id"))
        if not original:
            continue
        observation = next((o for o in original["observations"] if o["id"] == next((a["observation_id"] for a in original["adjustments"] if a["id"] == lesson["adjustment_id"]), None)), None)
        latest_original = effective_report(original)
        if (not observation or observation.get("excluded") or not latest_original or latest_original["data"]["result"] != "defects"
                or data.get("defect") not in observation["data"].get("defects", []) or data.get("defect") not in latest_original["data"].get("defects", [])):
            continue
        scope = knowledge_conditions(original["snapshot"], data["key"], data.get("defect"))
        row = group("correction", scope or {"unknown_conditions_for_print": original["id"]},
                    [data.get("defect"), data["key"], data["target"], data["before"], data["after"]], data["reason"])
        row["parameters"] = [{"key": data["key"], "before": data["before"], "after": data["after"], "target": data["target"], "unit": data.get("unit")}]
        row["defects"] = [data["defect"]]
        row["lesson_ids"].append(lesson["id"])
        row["print_ids"] = sorted(set(row["print_ids"] + [original["id"]]))
        row["created_at"] = max(row["created_at"], original["created_at"])
        repeated = prints.get(data.get("repeat_print_id"))
        report = effective_report(repeated) if repeated else None
        if not report or data.get("outcome") not in ("resolved", "better", "worse", "unchanged"):
            continue
        if conditions(repeated["snapshot"]) != conditions(data.get("applied_snapshot") or {}) or report["data"]["result"] in ("unknown", "not_checked"):
            continue
        resolved = data["outcome"] == "resolved"
        if resolved and data["defect"] in report["data"].get("defects", []):
            continue
        if data["outcome"] in ("resolved", "better"):
            row["verified_lesson_ids"].append(lesson["id"])
        evidence(row, repeated, data["outcome"], data["outcome"] in ("resolved", "better"), [original["id"], repeated["id"]])
    result = []
    for row in groups.values():
        proofs = [p for p in row["evidence"] if p["supporting"]]
        negatives = {p["print_id"] for p in row["evidence"] if not p["supporting"]}
        if row["type"] != "correction":
            confirmations = len({p["print_id"] for p in proofs})
        else:
            # Maximum bipartite matching: one baseline and one repeat per independent experiment.
            edges, matched = {}, {}
            for proof in proofs:
                edges.setdefault(proof["pair"][0], set()).add(proof["print_id"])
            def augment(source, seen):
                for repeated in sorted(edges[source]):
                    if repeated in seen:
                        continue
                    seen.add(repeated)
                    if repeated not in matched or augment(matched[repeated], seen):
                        matched[repeated] = source
                        return True
                return False
            for source in sorted(edges):
                augment(source, set())
            confirmations = len(matched)
        counter = len(negatives)
        row["confirmations"], row["counterexamples"] = confirmations, counter
        row["status"] = "conflicting" if confirmations and counter else "confirmed" if confirmations >= 2 else "observation" if row["evidence"] else "hypothesis"
        row["result"] = "; ".join(sorted(set(OUTCOMES.get(e["outcome"], RESULTS.get(e["outcome"], e["outcome"])) for e in row["evidence"]))) or "Ещё не проверялось"
        # Failed configurations are problems, not misleading cards headed 'working configuration'.
        if row["type"] == "configuration" and not confirmations:
            continue
        result.append(row)
    # Different effective values for the same parameter/defect/scope are explicit conflicting recipes.
    for row in result:
        if row["type"] != "correction" or not row["confirmations"]:
            continue
        for other in result:
            if (other["id"] != row["id"] and other["type"] == "correction" and other["confirmations"] and
                    other["conditions"] == row["conditions"] and other["defects"] == row["defects"] and
                    other["parameters"][0]["key"] == row["parameters"][0]["key"] and
                    other["parameters"][0]["after"] != row["parameters"][0]["after"]):
                row["status"] = other["status"] = "conflicting"
    return sorted(result, key=lambda row: (row["created_at"], row["id"]), reverse=True)


def context_labels(snapshot):
    context = snapshot.get("context", {})
    printer = context.get("printer") or {}
    extruders = context.get("extruders") or []
    materials = [(e.get("material") or {}).get("name") or (e.get("material") or {}).get("id") or "Материал неизвестен" for e in extruders]
    return {"printer": printer.get("name") or printer.get("id") or "Принтер неизвестен",
            "material": ", ".join(materials) or "Материал неизвестен", "nozzle": ", ".join(str(e.get("nozzle_mm") or "?") for e in extruders) + " мм" if extruders else "Сопло неизвестно",
            "mode": {"classic": "3D", "cylindrical": "5D Spiral", "cylindrical_full": "5D"}.get((context.get("mode") or {}).get("id"), "Режим неизвестен")}


class KnowledgeBase:
    def __init__(self, store, experience):
        self.store, self.db = store, experience

    def import_calibrations(self):
        for brief in self.store.listing()["sessions"]:
            session = self.store.internal(brief["id"])
            for record in session.get("calibration_results", []):
                defects = [] if record["adhesion_ok"] else ["adhesion"]
                if any(abs(record[k] - 20) > .2 for k in ("x_mm", "y_mm", "z_mm")):
                    defects.append("dimensions")
                # Old records do not contain actual effective settings. Do not reconstruct them from today's scene.
                snapshot = copy.deepcopy(record.get("print_snapshot")) if record.get("print_snapshot") else {
                    "context": {}, "settings": [], "objects": [{"name": "Калибровочный куб 20 мм", "dimensions_mm": [20, 20, 20]}],
                    "source_kind": "calibration", "process_signature": record.get("process_signature"), "legacy": True}
                snapshot["source_kind"] = "calibration"
                operation = {"id": record.get("operation_id") or "calibration-" + record["id"], "printed_at": record["created_at"], "print_snapshot": snapshot}
                failed = bool(defects) or not record["layers_ok"] or not record["surface_ok"]
                data = {"kind": "user_observation", "result": "defects" if failed else "good", "defects": defects,
                        "text": "Проверки калибровочного куба." + (" Параметры расчёта не сохранены; запись не используется как рецепт." if snapshot.get("legacy") else ""),
                        "calibration_checks": {k: record[k] for k in ("adhesion_ok", "layers_ok", "surface_ok")},
                        "answers": {}, "photos": [], "measurements": {"expected_mm": [20, 20, 20], "actual_mm": [record[k] for k in ("x_mm", "y_mm", "z_mm")]},
                        "print_kind": "calibration", "printed": True, "legacy": bool(snapshot.get("legacy"))}
                self.db.record(session["id"], operation, data, "legacy-calibration-" + record["id"])

    def snapshot(self):
        self.import_calibrations()
        library = self.db.library(limit=None)
        return dict(library, conclusions=derive_conclusions(library), defects=catalog(), calibration=CALIBRATION)

    def revise(self, observation_id, body):
        from .agent.contracts import validate, obj
        validate(body, obj({"result": {"enum": list(RESULTS)}, "text": {"type": "string", "maxLength": 4000},
                           "defects": {"type": "array", "items": {"enum": list(DEFECTS)}, "maxItems": 18, "uniqueItems": True},
                           "excluded": {"type": "boolean"}, "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
                           "measurements": {"type": "object"}}, ["result", "text", "defects", "excluded", "reason", "measurements"]))
        found = next((o for p in self.db.library(limit=None)["prints"] for o in p["observations"] if o["id"] == observation_id), None)
        if not found:
            raise ApiError("Наблюдение не найдено", 404)
        if not body["reason"].strip():
            raise ApiError("Укажите причину исправления или исключения")
        if body["result"] != "defects" and body["defects"]:
            raise ApiError("Для отмеченных дефектов выберите результат «Есть дефекты»")
        measures = body["measurements"]
        if set(measures) - {"expected_mm", "actual_mm"}:
            raise ApiError("Неизвестное измерение")
        for values in measures.values():
            if not isinstance(values, list) or len(values) != 3 or any(v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0) for v in values):
                raise ApiError("Размеры: три положительных числа в мм; неизвестные оставьте пустыми")
        data = copy.deepcopy(found["data"])
        data.update({k: body[k] for k in ("result", "text", "defects", "measurements")})
        data.update(reported_defects=body["defects"], inferred_defects=[])
        self.db.revise_observation(observation_id, data, body["excluded"], body["reason"].strip())
        return self.snapshot()
