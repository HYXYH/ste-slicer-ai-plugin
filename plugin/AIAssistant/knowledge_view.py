"""Plain-data presentation for the embedded QML knowledge page."""
import copy
import json

from .knowledge_base import STATUS, TYPES, RESULTS, OUTCOMES, context_labels, effective_report
from .print_diagnostics import DEFECTS
from .user_view import field_label


def printable(value):
    if value is None:
        return "Не проверялось"
    if isinstance(value, bool):
        return "Да" if value else "Нет"
    return str(value)


def print_card(item):
    snapshot = item["snapshot"]
    latest = item["observations"][-1] if item["observations"] else None
    report = latest["data"] if latest else {}
    names = [o.get("name") or "Модель" for o in snapshot.get("objects", [])]
    excluded = latest and latest.get("excluded")
    result = "excluded" if excluded else report.get("result", "not_checked")
    labels = context_labels(snapshot)
    return dict(labels, id=item["id"], title=" / ".join(names) or "Печать", date=item["created_at"],
                print_kind="calibration" if report.get("print_kind") == "calibration" or snapshot.get("source_kind") == "calibration" else "ordinary",
                type_label="Калибровочная" if report.get("print_kind") == "calibration" or snapshot.get("source_kind") == "calibration" else "Обычная",
                status=result, status_label="Исключено" if excluded else RESULTS.get(result, result),
                color="#99a6b8" if excluded else "#159a78" if result == "good" else "#d77b38" if result == "defects" else "#2586c6",
                description=report.get("text", ""), defects=[DEFECTS[k]["label"] for k in report.get("defects", []) if k in DEFECTS],
                defect_ids=report.get("defects", []), photo_count=sum(len(o["data"].get("photos", [])) for o in item["observations"]))


def snapshot_sections(snapshot):
    labels = context_labels(snapshot)
    rows = [{"label": name, "value": labels[key]} for key, name in (("printer", "Принтер"), ("material", "Материал"), ("nozzle", "Сопло"), ("mode", "Режим"))]
    profile = snapshot.get("context", {}).get("profile") or {}
    rows.append({"label": "Профиль", "value": profile.get("name") or (profile.get("custom") or {}).get("id") or (profile.get("quality") or {}).get("id") or "Не сохранён"})
    for model in snapshot.get("objects", []):
        rows.append({"label": model.get("name") or "Модель", "value": " × ".join(printable(v) for v in model.get("dimensions_mm") or []) + " мм"})
    return rows


def observation_card(observation):
    data = observation["data"]
    from .calibration_assessment import FEATURES
    return dict(copy.deepcopy(data), id=observation["id"], date=observation["created_at"], excluded=observation.get("excluded", False),
                status_label="Исключено из рекомендаций" if observation.get("excluded") else RESULTS.get(data["result"], data["result"]),
                defect_labels=[DEFECTS[k]["label"] for k in data.get("defects", []) if k in DEFECTS],
                checked_labels=[FEATURES[k]["title"] + ": " + {"pass": "дефектов нет", "fail": "есть дефект", "unknown": "не могу определить", "not_checked": "не проверялось"}.get(v, v) for k, v in data.get("checks", {}).items() if k in FEATURES],
                revisions=[{"date": r["created_at"], "reason": r["reason"], "excluded": bool(r["excluded"]),
                            "result": RESULTS.get(r["data"]["result"], r["data"]["result"]), "text": r["data"].get("text", ""),
                            "defects": [DEFECTS[k]["label"] for k in r["data"].get("defects", []) if k in DEFECTS],
                            "measurements": r["data"].get("measurements", {})} for r in observation.get("revisions", [])],
                original_text=observation.get("original_data", data).get("text", ""))


def projection(data, options):
    prints = {p["id"]: p for p in data.get("prints", [])}
    conclusions = {c["id"]: c for c in data.get("conclusions", [])}
    all_print_cards = {key: print_card(value) for key, value in prints.items()}
    rows = []
    tab = options.get("tab", "conclusions")
    if tab == "conclusions":
        for c in conclusions.values():
            representative = next((prints[p] for p in c["print_ids"] if p in prints), None)
            labels = context_labels(representative["snapshot"]) if representative else context_labels({})
            name, color = STATUS[c["status"]]
            rows.append(dict(labels, id=c["id"], title=TYPES[c["type"]], description=c["description"], type=c["type"],
                             type_label=TYPES[c["type"]], date=c["created_at"], status=c["status"], status_label=name, color=color,
                             confirmations=c["confirmations"], counterexamples=c["counterexamples"], defects=c["defects"], result=c["result"]))
    elif tab == "prints":
        rows = list(all_print_cards.values())
    elif tab == "defects":
        for entry in data.get("defects", []):
            related = [p["id"] for p in prints.values() if any(entry["id"] in o["data"].get("defects", []) for o in p["observations"])]
            rows.append(dict(entry, title=entry["label"], description=entry.get("description", entry["label"]),
                             image="defects/" + entry["id"] + ".svg", illustration_label="Собственная схема · не фотография",
                             related_print_ids=related, related_count=len(related), printer="", material="", date="",
                             type_label=entry["group"], status="", status_label=entry.get("english", ""), color="#2586c6"))
    printers = sorted(set(row.get("printer", "") for row in rows if row.get("printer")))
    materials = sorted(set(row.get("material", "") for row in rows if row.get("material")))
    categories = sorted(set(row.get("group", "") for row in rows if row.get("group")))
    query = options.get("search", "").lower().strip()
    filtered = []
    for row in rows:
        if query and query not in json.dumps(row, ensure_ascii=False).lower():
            continue
        if any(options.get(k, "all") not in ("all", "", row.get(k)) for k in ("printer", "material", "status")):
            continue
        if options.get("type", "all") not in ("all", "", row.get("type", row.get("print_kind"))):
            continue
        if options.get("category", "all") not in ("all", "", row.get("group")):
            continue
        grouped_by = options.get("group_by", "none")
        row["group_label"] = row.get({"type": "type_label", "status": "status_label"}.get(grouped_by, grouped_by), "") if grouped_by != "none" else ""
        filtered.append(row)
    filtered.sort(key=lambda r: (r.get("date", ""), r["id"]), reverse=True)
    if options.get("group_by", "none") != "none":
        filtered.sort(key=lambda r: r["group_label"])
    detail = {}
    selection = options.get("selection")
    if tab == "prints" and selection in prints:
        item = prints[selection]
        detail = dict(all_print_cards[selection], kind="print", conditions=snapshot_sections(item["snapshot"]),
                      observations=[observation_card(o) for o in item["observations"]],
                      parameters=[{"label": field_label(k), "value": printable(v), "scope": "Общие" if scope["target"]["scope"] == "global" else "Экструдер"}
                                  for scope in item["snapshot"].get("settings", []) for k, v in sorted(scope["values"].items())],
                      adjustments=[{"id": a["id"], "title": a["data"]["reason"], "change": field_label(a["data"]["key"]) + ": " +
                                    printable(a["data"]["before"]) + " → " + printable(a["data"]["after"]),
                                    "state": {"proposed": "Предложено", "applied": "Применено", "evaluated": "Проверено", "rolled_back": "Выполнен откат", "unknown": "Исход не подтверждён"}.get(a["data"]["state"], "Выполняется"),
                                    "outcome": OUTCOMES.get(a["data"].get("outcome"), "Ещё не проверялось")} for a in item["adjustments"]],
                      related=[{"id": c["id"], "title": TYPES[c["type"]] + ": " + c["description"], "tab": "conclusions"}
                               for c in conclusions.values() if selection in c["print_ids"]],
                      legacy=bool(item["snapshot"].get("legacy")))
    elif tab == "conclusions" and selection in conclusions:
        c = conclusions[selection]
        card = next(row for row in rows if row["id"] == selection)
        representative = next((prints[p] for p in c["print_ids"] if p in prints), None)
        shapes = (c["conditions"] or {}).get("objects", [])
        features = [{"label": "Геометрия", "value": {"small": "до 30 мм", "medium": "30–100 мм", "large": "более 100 мм"}.get(f.get("size_class"), "неизвестно") + "; " +
                     {"flat": "плоская", "slender": "вытянутая", "compact": "компактная"}.get(f.get("shape"), "") + "; пропорции " + str(f.get("axis_ratios", []))} for f in shapes]
        detail = dict(card, kind="conclusion", conditions=(snapshot_sections(representative["snapshot"])[:5] if representative else []) + features,
                      parameters=[{"label": field_label(p["key"]), "value": (printable(p["before"]) + " → " if p["before"] != p["after"] else "") + printable(p["after"]) + " " + (p.get("unit") or "")} for p in c["parameters"]],
                      related=[{"id": p, "title": all_print_cards[p]["title"] + " · " + all_print_cards[p]["date"], "tab": "prints"} for p in c["print_ids"] if p in prints],
                      evidence=[{"date": e["date"], "text": OUTCOMES.get(e["outcome"], RESULTS.get(e["outcome"], e["outcome"])),
                                 "supporting": e["supporting"], "print_id": e["print_id"]} for e in c["evidence"]],
                      observations=[observation_card(o) for pid in c["print_ids"] if pid in prints for o in prints[pid]["observations"]],
                      scope_note="Действует только для указанных оборудования, материала, профиля, остальных параметров и сопоставимой геометрии. Подтверждение — минимум две независимые проверки; это не гарантия качества или прочности.")
    elif tab == "defects":
        row = next((r for r in rows if r["id"] == selection), None)
        if row:
            related = [{"id": p, "title": all_print_cards[p]["title"], "tab": "prints"} for p in row["related_print_ids"]]
            related += [{"id": c["id"], "title": c["description"], "tab": "conclusions"} for c in conclusions.values() if selection in c["defects"]]
            detail = dict(row, kind="defect", related=related)
    return {"rows": filtered, "detail": detail, "printers": printers, "materials": materials, "categories": categories,
            "catalog": data.get("defects", []),
            "total": len(rows), "shown": len(filtered), "calibration": data.get("calibration", []),
            "counts": {"prints": len(prints), "conclusions": len(conclusions), "defects": len(data.get("defects", []))}}
