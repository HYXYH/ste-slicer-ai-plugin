"""Server-owned task state, provenance and conservative readiness calculation."""
import copy
import json

from ..session import now
from .contracts import fail, source_ids, validate, obj, ID, TEXT, array, MODE


COLLECTIONS = ("requirements", "constraints", "candidates", "unknowns", "conflicts",
               "assumptions", "questions", "decision_proposals")

REF = obj({"id": ID, "version": {"type": "integer", "minimum": 1}}, ["id", "version"])
REFS = array(REF)
ANY_JSON = {"oneOf": [{"type": t} for t in ("string", "number", "boolean", "null", "object", "array")]}
RECORD = {"id": ID, "key": ID, "value": ANY_JSON, "unit": {"oneOf": [{"type": "string"}, {"type": "null"}]},
          "source_refs": REFS, "object_ids": array(ID), "supersedes": {"oneOf": [ID, {"type": "null"}]},
          "status": {"enum": ["known", "unknown", "assumed", "conflict", "not_applicable"]}, "strength": {"enum": ["hard", "preference"]}}
ROW_SCHEMAS = {
    "requirements": obj(RECORD, RECORD), "constraints": obj(RECORD, RECORD),
    "unknowns": obj({"id": ID, "field": ID, "reason": TEXT, "affected_decisions": array(ID), "blocking": {"type": "boolean"},
                     "resolution": {"enum": ["user", "tool", "knowledge", "assumption"]}, "rule_refs": array(ID), "source_refs": REFS},
                    ["id", "field", "reason", "affected_decisions", "blocking", "resolution", "rule_refs", "source_refs"]),
    "questions": obj({"id": ID, "field": ID, "text": TEXT, "reason": TEXT, "affected_decisions": array(ID), "source_refs": REFS,
                      "options": array(obj({"id": ID, "label": TEXT}, ["id", "label"]), 20),
                      "allow_free_text": {"type": "boolean"}, "allow_unknown": {"type": "boolean"}, "required": {"type": "boolean"}},
                     ["id", "field", "text", "reason", "affected_decisions", "source_refs", "options", "allow_free_text", "allow_unknown", "required"]),
    "assumptions": obj({"id": ID, "key": ID, "value": ANY_JSON, "unit": {"oneOf": [{"type": "string"}, {"type": "null"}]}, "reason": TEXT, "source_refs": REFS},
                       ["id", "key", "value", "unit", "reason", "source_refs"]),
    "conflicts": obj({"id": ID, "reason": TEXT, "record_ids": array(ID), "source_refs": REFS}, ["id", "reason", "record_ids", "source_refs"]),
    "candidates": obj({"id": ID, "mode": MODE, "material_id": ID, "profile_id": ID, "extruder_id": ID, "orientation_proposal": ANY_JSON,
                       "requirement_checks": array(obj({"requirement_id": ID, "status": {"enum": ["pass", "fail", "unknown"]}, "source_refs": REFS}, ["requirement_id", "status", "source_refs"])),
                       "missing_fields": array(ID), "evidence_refs": REFS, "rejection_reasons": array(TEXT),
                       "assumption_ids": array(ID), "status": {"enum": ["possible", "rejected", "selected"]}, "source_refs": REFS},
                      ["id", "mode", "material_id", "profile_id", "extruder_id", "orientation_proposal", "requirement_checks", "missing_fields", "evidence_refs", "rejection_reasons", "status", "source_refs"]),
    "decision_proposals": obj({"id": ID, "candidate_id": ID, "actions": array(obj({"id": ID, "tool": ID, "arguments": {"type": "object"}, "before": ANY_JSON,
                               "after": ANY_JSON, "reason": TEXT, "source_refs": REFS}, ["id", "tool", "arguments", "before", "after", "reason", "source_refs"])),
                               "limitations": array(TEXT), "source_refs": REFS}, ["id", "candidate_id", "actions", "limitations", "source_refs"])}


def new_map():
    result = {"schema_version": 1, "revision": 0, "input_revision": 0,
              "target_object_ids": [], "snapshot_ref": None, "facts": [], "resources": {},
              "decisions": [], "history": [], "readiness": {"status": "needs_data", "reasons": []}}
    result.update({key: [] for key in COLLECTIONS})
    return result


def merge_proposal(task, proposal, sources):
    if not isinstance(proposal, dict) or set(proposal) != set(COLLECTIONS + ("summary",)):
        fail("Неверные поля структурированного ответа", "INVALID_MODEL_OUTPUT")
    if not isinstance(proposal["summary"], str) or len(proposal["summary"]) > 20000:
        fail("Неверное объяснение", "INVALID_MODEL_OUTPUT")
    updated = copy.deepcopy(task)
    for collection in COLLECTIONS:
        rows = proposal[collection]
        if not isinstance(rows, list) or len(rows) > 100:
            fail("Неверный список " + collection, "INVALID_MODEL_OUTPUT")
        existing = {row["id"]: row for row in updated[collection]}
        incoming_ids = set()
        for index, row in enumerate(rows):
            path = collection + "[" + str(index) + "]"
            validate(row, ROW_SCHEMAS[collection], path)
            if row["id"] in incoming_ids:
                fail("Повторяющийся ID записи", "INVALID_MODEL_OUTPUT")
            incoming_ids.add(row["id"])
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
                fail("Записи нужен ID", "INVALID_MODEL_OUTPUT")
            if "acceptance" in row or "answer" in row or "resolved" in row:
                fail("Согласие и ответы сохраняет только сервер", "INVALID_MODEL_OUTPUT")
            source_ids(row.get("source_refs"), sources, path + ".source_refs")
            if collection in ("requirements", "constraints"):
                required = {"id", "key", "value", "unit", "status", "strength", "source_refs", "object_ids", "supersedes"}
                if set(row) != required or row["strength"] not in ("hard", "preference") or row["status"] not in ("known", "unknown", "assumed", "conflict", "not_applicable"):
                    fail("Неверная структура требования", "INVALID_MODEL_OUTPUT")
                if isinstance(row["value"], (int, float)) and not isinstance(row["value"], bool) and not row["unit"]:
                    fail("Для числа нужна единица", "INVALID_MODEL_OUTPUT")
                if row["supersedes"]:
                    old = existing.get(row["supersedes"])
                    if old is None:
                        fail("Заменяемое требование не найдено", "INVALID_MODEL_OUTPUT")
                    if not any(ref not in old["source_refs"] and sources[ref["id"]].get("kind") == "user" for ref in row["source_refs"]):
                        fail("Для замены требования нужен новый ответ пользователя", "INVALID_MODEL_OUTPUT")
                    updated["history"].append(copy.deepcopy(old))
                    existing.pop(old["id"])
            if collection == "unknowns" and (not isinstance(row.get("blocking"), bool) or not row.get("field") or row.get("resolution") not in ("user", "tool", "knowledge", "assumption")):
                fail("Неверное неизвестное", "INVALID_MODEL_OUTPUT")
            if collection == "questions":
                if not row.get("field") or not row.get("text") or not row.get("reason") or not row.get("affected_decisions"):
                    fail("Вопрос должен объяснять влияние на решение", "INVALID_MODEL_OUTPUT")
            if collection == "candidates":
                source_ids(row["evidence_refs"], sources, path + ".evidence_refs")
                if row.get("status") not in ("possible", "rejected", "selected"):
                    fail("Неверный статус варианта", "INVALID_MODEL_OUTPUT")
                for check_index, check in enumerate(row.get("requirement_checks", [])):
                    if check.get("status") not in ("pass", "fail", "unknown"):
                        fail("Неверный результат проверки", "INVALID_MODEL_OUTPUT")
                    if check.get("source_refs"):
                        source_ids(check["source_refs"], sources, path + ".requirement_checks[" + str(check_index) + "].source_refs")
            if row["id"] in existing:
                old = existing[row["id"]]
                if collection in ("requirements", "constraints") and any(row[k] != old[k] for k in RECORD):
                    fail("Замена требования требует нового ID и supersedes", "INVALID_MODEL_OUTPUT")
                if collection == "unknowns" and old.get("blocking") and not row["blocking"] and not old.get("resolved"):
                    fail("Блокирующее неизвестное сначала требует ответа или измерения", "INVALID_MODEL_OUTPUT")
                if collection == "unknowns" and old.get("resolved"):
                    row = dict(row, resolved=True, resolution_ref=old.get("resolution_ref"))
                if collection in ("assumptions", "questions", "conflicts"):
                    if any(row.get(k) != v for k, v in old.items() if k not in ("updated_at", "acceptance", "answer", "resolved", "consent_ref")):
                        fail("Для изменённого вопроса, допущения или конфликта нужен новый ID", "INVALID_MODEL_OUTPUT")
                    row = copy.deepcopy(old)
            row = copy.deepcopy(row)
            if collection == "assumptions":
                row.setdefault("acceptance", "pending")
            row["updated_at"] = now()
            existing[row["id"]] = row
        if collection == "candidates":
            incoming_selected = [r["id"] for r in rows if r["status"] == "selected"]
            if len(incoming_selected) > 1:
                fail("Можно выбрать только один вариант", "INVALID_MODEL_OUTPUT")
            if incoming_selected:
                for candidate in existing.values():
                    if candidate["id"] != incoming_selected[0] and candidate["status"] == "selected":
                        candidate["status"] = "possible"
        updated[collection] = list(existing.values())
    updated["revision"] += 1
    return updated


def resolve_tool_unknowns(task, sources, snapshot_ref):
    for unknown in task["unknowns"]:
        if unknown.get("resolution") != "tool" or unknown.get("resolved"):
            continue
        measurements = []
        for source in sources.values():
            data = source.get("data", {})
            if source.get("kind") != "tool" or not isinstance(data, dict) or data.get("snapshot_ref") != snapshot_ref:
                continue
            value = data.get("settings", {}).get(unknown["field"])
            if source.get("tool") == "get_printer_capabilities" and unknown["field"] == "printer_id":
                value = data.get("printer_id")
            if source.get("tool") == "get_scene_snapshot" and unknown["field"] == "dimensions_mm":
                targets = [o for o in data.get("objects", []) if o["id"] in task["target_object_ids"]]
                value = targets[0].get("dimensions_mm") if len(targets) == 1 else None
            if value is not None:
                measurements.append((value, source["id"]))
        if measurements and len(set(json.dumps(v, sort_keys=True) for v, ref in measurements)) == 1:
            unknown.update(resolved=True, resolution_ref=measurements[0][1])


def readiness(task, scene, sources):
    result = {"status": "needs_data", "blocking_unknown_ids": [], "conflict_ids": [],
              "pending_assumption_ids": [], "failed_check_ids": [], "reasons": []}
    if not task["target_object_ids"] or not set(task["target_object_ids"]).issubset(set(o["id"] for o in scene["objects"])):
        result["reasons"].append("Целевые модели отсутствуют в сцене")
        return result
    if task["snapshot_ref"] != scene["snapshot_ref"]:
        result.update(status="stale", reasons=["Сцена или настройки изменились"])
        return result
    result["conflict_ids"] = [r["id"] for r in task["conflicts"] if not r.get("resolved")]
    selected = [r for r in task["candidates"] if r.get("status") == "selected"]
    used_assumptions = set(selected[0].get("assumption_ids", [a["id"] for a in task["assumptions"]])) if len(selected) == 1 else set()
    result["pending_assumption_ids"] = [r["id"] for r in task["assumptions"] if r["id"] in used_assumptions and r.get("acceptance") != "accepted"]
    result["blocking_unknown_ids"] = [r["id"] for r in task["unknowns"] if r.get("blocking") and not r.get("resolved")]
    if len(selected) != 1:
        result["reasons"].append("Нужен один выбранный вариант")
    else:
        checks = {c["requirement_id"]: c for c in selected[0].get("requirement_checks", [])}
        for requirement in task["requirements"] + task["constraints"]:
            if requirement.get("strength") != "hard" or requirement.get("status") == "not_applicable":
                continue
            check = checks.get(requirement["id"], {})
            # LLM assertions alone cannot certify engineering requirements.
            proven = any(sources.get(ref.get("id"), {}).get("kind") == "manual"
                         and sources[ref["id"]].get("requirement_id") == requirement["id"]
                         for ref in check.get("source_refs", []))
            if check.get("status") != "pass" or not proven:
                result["failed_check_ids"].append(requirement["id"])
        if selected[0].get("missing_fields"):
            result["reasons"].append("Не заполнены существенные условия варианта")
    if result["conflict_ids"] or result["failed_check_ids"]:
        result["status"] = "blocked"
        result["reasons"].append("Жёсткие требования или противоречия требуют проверки специалистом")
    elif result["blocking_unknown_ids"] or result["pending_assumption_ids"]:
        result["status"] = "needs_input"
    elif not result["reasons"]:
        result["status"] = "ready_for_plan"
    return result
