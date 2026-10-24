"""One projection of user requirements for the conversation and data coverage card."""
import re

FIELDS = {"purpose": "Для чего предназначена деталь?", "load": "Какая нагрузка ожидается? Укажите величину и единицы или «не применяется».",
          "temperature": "Какова рабочая температура детали? Укажите °C или «не применяется».",
          "environment": "В какой среде будет работать деталь: помещение, УФ, влага, химия?",
          "accuracy": "Какие нужны точность размеров и посадки?", "surface": "Какое качество поверхности требуется?"}
LABELS = {"purpose": "назначение", "load": "нагрузку", "temperature": "температуру", "environment": "среду",
          "accuracy": "точность", "surface": "качество поверхности"}
ALIASES = {"purpose": r"purpose|назнач|part_function|application", "load": r"load|torque|force|нагруз|момент|усили",
           "temperature": r"service_temperature|operating_temperature|working_temperature|^temperature$|рабоч.*температ",
           "environment": r"environment|humidity|water|moisture|^uv$|uv_|_uv|chemical|сред|влаг|уф|хими",
           "accuracy": r"accuracy|precision|tolerance|fit_|точност|посад|допуск",
           "surface": r"surface|roughness|поверхност|шероховат"}


def field_for(key):
    return next((field for field, pattern in ALIASES.items() if re.search(pattern, key, re.I)), None)


def known_answer(value):
    return value is not None and str(value).strip().casefold().strip(".! ") not in (
        "", "не знаю", "unknown", "неизвестно", "нет данных", "не определено", "?", "none")


def set_answer(session, field, value, message_id):
    """An explicit field answer replaces older values and retains their provenance in history."""
    task = session["task_map"]
    previous = [r for r in task["requirements"] if field_for(r["key"]) == field]
    task["history"].extend(previous)
    task["requirements"] = [r for r in task["requirements"] if r not in previous]
    task["requirements"].append({"id": "data-" + message_id + "-" + field, "key": field, "value": value, "unit": None,
                                 "status": "known" if known_answer(value) else "unknown", "strength": "hard" if any(r.get("strength") == "hard" for r in previous) else "preference",
                                 "source_refs": [{"id": message_id, "version": 1}], "object_ids": task["target_object_ids"], "supersedes": None})
    session["sources"][message_id] = {"id": message_id, "version": 1, "kind": "user", "text": value}
    old_ids = {r["id"] for r in previous}
    for conflict in task["conflicts"]:
        if conflict.get("record_ids") and set(conflict["record_ids"]).issubset(old_ids):
            conflict.update(resolved=True, resolution_ref=message_id)
    for unknown in task["unknowns"]:
        if field_for(unknown["field"]) == field:
            unknown.update(resolved=known_answer(value), resolution_ref=message_id)


def reconcile_answers(task, sources):
    """Free chat answers extracted as requirements also resolve matching user-data gaps."""
    for unknown in task.get("unknowns", []):
        field = field_for(unknown["field"])
        if unknown.get("resolved") or unknown.get("resolution") != "user" or field is None:
            continue
        for row in task["requirements"]:
            if row["key"] != unknown["field"] and not (unknown["field"] == field and field_for(row["key"]) == field):
                continue
            if row.get("status") not in ("known", "not_applicable") or not known_answer(row.get("value")):
                continue
            refs = [ref for ref in row.get("source_refs", []) if sources.get(ref["id"], {}).get("kind") == "user"]
            if refs:
                unknown.update(resolved=True, resolution_ref=refs[-1]["id"])
                break


def coverage(session):
    task = session["task_map"]
    rows = task.get("requirements", []) + task.get("constraints", [])
    conflicts = [c for c in task.get("conflicts", []) if not c.get("resolved")]
    conflicted_ids = {rid for c in conflicts for rid in c.get("record_ids", [])}
    result = {}
    for field in FIELDS:
        matches = [r for r in rows if field_for(r["key"]) == field]
        by_key = {}
        for row in matches:
            if row.get("status") in ("known", "not_applicable") and known_answer(row.get("value")):
                by_key.setdefault(row["key"], set()).add(str(row["value"]).strip().casefold() + " " + str(row.get("unit") or ""))
        conflict = any(r.get("status") == "conflict" or r["id"] in conflicted_ids for r in matches) or any(len(v) > 1 for v in by_key.values())
        valid = [r for r in matches if (r.get("status") == "not_applicable" or r.get("status") == "known" and known_answer(r.get("value")))
                 and any(session.get("sources", {}).get(ref["id"], {}).get("kind") == "user" for ref in r.get("source_refs", []))]
        unknown = any(field_for(u["field"]) == field and not u.get("resolved") and u.get("resolution") == "user" for u in task.get("unknowns", []))
        required = field in ("purpose", "load", "temperature", "environment") or bool(matches) or any(
            field_for(q.get("field", "")) == field and q.get("required") for q in task.get("questions", [])) or unknown
        # Calibration tasks describe a geometric sample; operating conditions do not apply.
        sample = bool(session.get("calibration_for")) and field in ("load", "temperature", "environment")
        result[field] = {"field": field, "required": required, "known": sample or bool(valid) and not conflict and not unknown,
                         "conflict": conflict, "values": [str(r["value"]) + (" " + r["unit"] if r.get("unit") else "") for r in matches],
                         "source_refs": [ref for r in valid for ref in r.get("source_refs", [])]}
    return result
