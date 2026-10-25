"""Weighted Beta-Binomial evidence; unknown utility terms stay unknown, never invented."""
import copy
import json
import math
import os
import re

from .experience import conditions, encode
from .knowledge_base import effective_report, knowledge_conditions
from .session import ApiError

FEATURES = {
    "adhesion": {"title": "Адгезия и первый слой", "group": "adhesion", "defects": ["adhesion", "warping", "bottom_gaps", "elephant_foot"], "keys": ["material_bed_temperature", "speed_layer_0", "adhesion_type", "cool_fan_speed_0"]},
    "extrusion": {"title": "Равномерность экструзии", "group": "extrusion", "defects": ["underextrusion", "blobs", "top_gaps", "top_holes", "top_ridges"], "keys": ["material_flow", "material_print_temperature", "speed_print", "layer_height"]},
    "dimensions": {"title": "Геометрическая точность", "group": "geometry", "defects": ["dimensions", "closed_holes", "layer_shift", "ringing"], "keys": ["layer_height", "material_flow", "horizontal_expansion", "speed_print"]},
    "stringing": {"title": "Нити при переходах / ретракт", "group": "retraction", "defects": ["stringing"], "keys": ["retraction_enable", "retraction_amount", "retraction_speed", "material_print_temperature", "speed_travel"]},
    "bridges": {"title": "Провисание мостов", "group": "cooling", "defects": ["bridges"], "keys": ["cool_fan_speed", "speed_print", "material_print_temperature", "bridge_settings_enabled"]},
    "overhangs": {"title": "Нависания", "group": "cooling", "defects": ["overhangs"], "keys": ["cool_fan_speed", "speed_print", "material_print_temperature", "support_enable"]},
    "small_features": {"title": "Охлаждение мелких элементов", "group": "cooling", "defects": ["small_features"], "keys": ["cool_fan_speed", "cool_min_layer_time", "material_print_temperature"]},
    "bonding": {"title": "Межслойное сцепление", "group": "bonding", "defects": ["delamination"], "keys": ["material_print_temperature", "cool_fan_speed", "speed_print", "layer_height"]}}
GROUPS = {"adhesion": "Адгезия", "extrusion": "Экструзия", "geometry": "Геометрическая точность", "retraction": "Ретракт", "cooling": "Охлаждение", "bonding": "Межслойное сцепление"}
CHECKS = ("pass", "fail", "not_checked", "unknown")


def load_policy(root=None):
    with open(os.path.join(os.path.dirname(__file__), "calibration-policy.json"), encoding="utf-8") as source:
        result = json.load(source)
    local = os.path.join(root, "calibration-policy.json") if root else None
    if local and os.path.exists(local):
        with open(local, encoding="utf-8") as source:
            override = json.load(source)
        if not isinstance(override, dict) or set(override) - set(result):
            raise ApiError("Неизвестные параметры calibration-policy.json")
        result.update(override)
    for key, value in result.items():
        values = value.values() if isinstance(value, dict) else [value]
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in values):
            raise ApiError("Коэффициенты калибровки должны быть конечными неотрицательными числами")
    if result["prior_alpha"] <= 0 or result["prior_beta"] <= 0 or result["minimum_effective_trials"] <= 0:
        raise ApiError("Параметры prior и minimum_effective_trials должны быть положительными")
    for key in ("minimum_similarity", "geometry_transfer_weight", "setting_mismatch_weight", "adequate_success_mean"):
        if not 0 < result[key] <= 1:
            raise ApiError(key + " должен лежать в (0,1]")
    return result


def catalog():
    with open(os.path.join(os.path.dirname(__file__), "models", "calibration-catalog.json"), encoding="utf-8") as source:
        return json.load(source)


def hardware(snapshot):
    value = conditions(snapshot)
    if not value or not value["mode"]:
        return None
    return {k: value[k] for k in ("printer", "definition", "mode", "extruders")}


def similarity(first, second, feature, policy):
    if hardware(first) is None or hardware(first) != hardware(second) or not first.get("settings") or not second.get("settings"):
        return 0.
    def values(snapshot):
        return {encode(row["target"]): {k: row["values"].get(k) for k in FEATURES[feature]["keys"]} for row in snapshot["settings"]}
    a, b = values(first), values(second)
    if set(a) != set(b):
        return 0.
    common = [(a[t][k], b[t][k]) for t in a for k in a[t] if a[t][k] is not None or b[t][k] is not None]
    if not common:
        return 0.
    match = sum(x == y for x, y in common) / len(common)
    weight = match + (1 - match) * policy["setting_mismatch_weight"]
    ca, cb = knowledge_conditions(first), knowledge_conditions(second)
    if not ca or not cb:
        return 0.
    # Geometry transfer is deliberately discounted, even within one material and process.
    if ca["objects"] != cb["objects"]:
        weight *= policy["geometry_transfer_weight"]
    return weight if weight >= policy["minimum_similarity"] else 0.


def checked(data):
    result = {k: v for k, v in data.get("checks", {}).items() if k in FEATURES and v in CHECKS}
    # A generic 'good' result does not certify bridges, retraction or strength.
    for feature, meta in FEATURES.items():
        if set(meta["defects"]) & set(data.get("reported_defects", data.get("defects", []))):
            result[feature] = "fail"
    if data.get("legacy") and "calibration_checks" in data:
        result["adhesion"] = "pass" if data["calibration_checks"]["adhesion_ok"] else "fail"
    return result


def needed(snapshot, text):
    result = ["adhesion", "extrusion"]
    for feature, pattern in [("dimensions", r"точн|посад|размер|зуб|шестер|допуск"), ("stringing", r"ретракт|нит[ьи]|паутин|переход|несколько"),
                             ("bridges", r"мост|перемыч"), ("overhangs", r"навис|поддерж"), ("small_features", r"мелк|сложн|тонк|миниатюр"),
                             ("bonding", r"прочн|нагруз|силов|рассло")]:
        if re.search(pattern, text.lower()):
            result.append(feature)
    if len(snapshot.get("objects", [])) > 1 and "stringing" not in result:
        result.append("stringing")
    return result


def posterior(samples, policy):
    # One unit of evidence per dependency cluster, regardless of duplicate reports.
    clusters = {}
    for cluster, success, weight, pid in samples:
        clusters.setdefault(cluster, []).append((success, weight, pid))
    positive = negative = 0.
    refs = []
    for members in clusters.values():
        total = sum(w for _, w, _ in members)
        scale = min(1., total) / total if total else 0.
        positive += sum(w * scale for good, w, _ in members if good)
        negative += sum(w * scale for good, w, _ in members if not good)
        refs.extend(p for _, _, p in members)
    a, b = policy["prior_alpha"] + positive, policy["prior_beta"] + negative
    effective = positive + negative
    return {"alpha": a, "beta": b, "effective_trials": round(effective, 4), "success_weight": positive, "failure_weight": negative,
            "mean": a / (a + b) if effective >= policy["minimum_effective_trials"] else None,
            "print_ids": sorted(set(refs)), "method": "weighted Beta-Binomial; Beta prior; dependency-cluster cap=1"}


def assess(snapshot, library, text, policy, costs=None):
    costs = costs or {}
    required = needed(snapshot, text)
    prints = {p["id"]: p for p in library["prints"]}
    parents = {p: p for p in prints}
    def root(pid):
        while parents[pid] != pid:
            pid = parents[pid]
        return pid
    for lesson in library["lessons"]:
        a, b = lesson["data"].get("print_id"), lesson["data"].get("repeat_print_id")
        if a in parents and b in parents:
            parents[root(b)] = root(a)
    operations = {}
    for item in prints.values():
        key = (item["session_id"], item["operation_id"])
        if key in operations:
            parents[root(item["id"])] = root(operations[key])
        operations[key] = item["id"]
    groups = {}
    for feature in FEATURES:
        samples = []
        for item in prints.values():
            report = effective_report(item)
            status = checked(report["data"]).get(feature) if report else None
            weight = similarity(snapshot, item["snapshot"], feature, policy)
            if weight and status in ("pass", "fail"):
                # Duplicate reports/sessions of the same slice never count as fresh independent evidence.
                cluster = item["session_id"] + ":" + item["operation_id"]
                linked = root(item["id"])
                if linked != item["id"] or any(root(p) == linked and p != item["id"] for p in prints):
                    cluster = "trial:" + linked
                samples.append((cluster, status == "pass", weight, item["id"]))
        groups[feature] = dict(posterior(samples, policy), id=feature, title=FEATURES[feature]["title"], group=FEATURES[feature]["group"], required=feature in required)
    unchecked = [k for k in required if groups[k]["mean"] is None or groups[k]["mean"] < policy["adequate_success_mean"]]
    available = [m for m in catalog() if (snapshot.get("context", {}).get("mode") or {}).get("id") in m["modes"]]
    def score(model):
        coverage = sum(1 + min(1., groups[k]["failure_weight"]) for k in unchecked if k in model["checks"])
        rank = policy["test_cost_ranks"].get(model["id"], model["cost_rank"])
        return coverage / max(1., rank ** .5)
    test = max(available, key=score) if available else None
    if test and len(unchecked) >= 5 and any(m["id"] == "universal" for m in available):
        test = next(m for m in available if m["id"] == "universal")
    dimensions = [v for o in snapshot.get("objects", []) for v in o.get("dimensions_mm", [])]
    volume = sum(math.prod(o.get("dimensions_mm", [0])) if hasattr(math, "prod") else _product(o.get("dimensions_mm", [0])) for o in snapshot.get("objects", []))
    costly = (max(dimensions or [0]) >= policy["cold_start_longest_mm"] or volume >= policy["cold_start_box_volume_mm3"] or costs.get("main_loss", 0) > costs.get("test_cost", float("inf")))
    complex_model = len(required) >= 4
    failed = any(groups[k]["failure_weight"] for k in unchecked)
    utility = None
    utility_terms = {}
    if test:
        for feature in unchecked:
            if feature not in test["checks"] or groups[feature]["mean"] is None:
                continue
            detection, repair = [], []
            for item in prints.values():
                report = effective_report(item)
                meta = report["data"].get("calibration", {}) if report else {}
                status = checked(report["data"]).get(feature) if report else None
                if meta.get("test_id") != test["id"] or status not in ("pass", "fail"):
                    continue
                for pid in meta.get("control_print_ids", []):
                    control = prints.get(pid)
                    original = effective_report(control) if control else None
                    if not original or checked(original["data"]).get(feature) != "fail":
                        continue
                    weight = similarity(snapshot, control["snapshot"], feature, policy)
                    # Detection evidence requires unchanged process values, before corrective experiments.
                    if weight and conditions(control["snapshot"]) and conditions(control["snapshot"])["settings"] == (conditions(item["snapshot"]) or {}).get("settings"):
                        detection.append((pid, status == "fail", weight, item["id"]))
            for lesson in library["lessons"]:
                data = lesson["data"]
                original, repeated = prints.get(data.get("print_id")), prints.get(data.get("repeat_print_id"))
                before, after = effective_report(original) if original else None, effective_report(repeated) if repeated else None
                if (data.get("defect") not in FEATURES[feature]["defects"] or data.get("outcome") not in ("resolved", "better", "worse", "unchanged") or not before or not after
                        or data.get("defect") not in before["data"].get("defects", []) or after["data"]["result"] in ("unknown", "not_checked")
                        or conditions(repeated["snapshot"]) != conditions(data.get("applied_snapshot") or {})):
                    continue
                if data["outcome"] == "resolved" and data["defect"] in after["data"].get("defects", []):
                    continue
                weight = similarity(snapshot, original["snapshot"], feature, policy)
                if weight:
                    repair.append((root(original["id"]), data["outcome"] in ("resolved", "better"), weight, repeated["id"]))
            detect, fix = posterior(detection, policy), posterior(repair, policy)
            utility_terms[feature] = {"defect": groups[feature], "detection": detect, "repair": fix}
            if detect["mean"] is not None and fix["mean"] is not None and "main_loss" in costs and "test_cost" in costs:
                value = (1 - groups[feature]["mean"]) * detect["mean"] * fix["mean"] * costs["main_loss"] - costs["test_cost"] * policy["test_cost_multiplier"]
                utility = max(utility, value) if utility is not None else value
    covered = [k for k in unchecked if test and k in test["checks"]]
    if utility is not None:
        offer, method = utility > policy["minimum_utility"], "utility"
        reason = "Ожидаемая польза одного проверяемого риска: {:.2f} в указанных вами единицах затрат. Вероятности — оценки по истории, не гарантия.".format(utility)
    else:
        offer = bool(covered) and (failed or (len(unchecked) >= policy["cold_start_min_unchecked"] and (costly or complex_model)))
        method = "cold_start"
        reason = ("Есть неудачные проверки в сопоставимых условиях." if failed else "Характеристики ещё недостаточно проверены; основная модель сложная или затраты на неудачу потенциально велики." if offer else "Мало опыта, но польза отдельного теста пока не обоснована." if unchecked else "Характеристики проверены в сопоставимых условиях.")
        reason += " Для численного U не хватает независимых наблюдений обнаружения/исправления или ваших оценок затрат."
    if not test:
        offer = False
        reason = "Нет проверенного теста для текущего режима. Планарные FDM-тесты не переносятся автоматически на 5D."
    elif "main_loss" in costs and "test_cost" in costs and costs["main_loss"] - costs["test_cost"] * policy["test_cost_multiplier"] <= policy["minimum_utility"]:
        offer = False
        reason = "Даже верхняя граница пользы не превышает стоимость теста по указанным вами затратам. Продолжайте обычную печать и сообщите результат."
    return {"version": 1, "offer": offer, "method": method, "reason": reason, "features": list(groups.values()),
            "groups": [{"id": k, "title": title, "features": [f for f in groups.values() if f["group"] == k]} for k, title in GROUPS.items()],
            "unchecked": unchecked, "unchecked_labels": [FEATURES[k]["title"] for k in unchecked], "test": test,
            "utility": utility, "utility_terms": utility_terms, "costs": costs, "policy": copy.deepcopy(policy),
            "print_ids": sorted(set(pid for f in groups.values() for pid in f["print_ids"])),
            "scope_note": "Положительная визуальная проверка не подтверждает прочность. Схожесть — фиксированное правило; Beta-Binomial с дробными весами является приближённой оценкой, а не откалиброванным прогнозом."}


def _product(values):
    result = 1
    for value in values:
        result *= value
    return result
