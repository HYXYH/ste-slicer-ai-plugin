"""Deterministic, three-valued JSON rule search. No executable expressions."""
import json
import os

from .contracts import fail, strict_json


def condition(test, facts):
    if not isinstance(test, dict) or set(test) != {"field", "op", "value"}:
        fail("Неверное условие базы знаний")
    value = facts.get(test["field"])
    op, expected = test["op"], test["value"]
    if op == "exists":
        return (value is not None) == expected
    if op not in ("eq", "in", "gte", "lte"):
        fail("Неизвестный оператор базы знаний")
    if value is None:
        return None
    if op == "eq":
        return value == expected
    if op == "in":
        if not isinstance(expected, list):
            fail("Оператор in требует список")
        return value in expected
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not isinstance(expected, (float, int)):
        return None
    return value >= expected if op == "gte" else value <= expected


class Knowledge:
    def __init__(self, directory=None):
        self.directory = directory or os.path.join(os.path.dirname(os.path.dirname(__file__)), "knowledge")

    def search(self, query, conditions=None, decision=None, limit=20, cursor=None):
        records = []
        for name in sorted(os.listdir(self.directory)):
            if not name.endswith(".json") or name.startswith("template"):
                continue
            with open(os.path.join(self.directory, name), encoding="utf-8") as source:
                batch = strict_json(source.read())
            records.extend(batch)
        found = []
        facts = conditions or {}
        for record in records:
            applies = [condition(c, facts) for c in record["applies_when"]]
            excluded = [condition(c, facts) for c in record["excluded_when"]]
            if False in applies or True in excluded:
                continue
            if decision and decision not in record["affected_decisions"]:
                continue
            row = dict(record)
            missing = [c["field"] for c in record["applies_when"] + record["excluded_when"] if condition(c, facts) is None]
            missing.extend(f for f in record["required_facts"] if facts.get(f) is None)
            row["missing_fields"] = sorted(set(missing))
            row["applicability"] = "unknown" if missing else "pass"
            # Reference records hold cited datasheet values: they may back a setting, never certify a part.
            row["usable_as_evidence"] = not missing and (
                (row["status"] == "reviewed" and bool(row.get("reviewed_by") and row.get("reviewed_at")))
                or (row["status"] == "reference" and bool(row.get("source", {}).get("document"))))
            row["relevance"] = sum(word in json.dumps(record, ensure_ascii=False).lower() for word in query.lower().split())
            found.append(row)
        found.sort(key=lambda row: (not row["usable_as_evidence"], -len(row["applies_when"]), -row["relevance"], row["id"]))
        conflicts = []
        for index, left in enumerate(found):
            for right in found[index + 1:]:
                for key in set(left["recommendations"]) & set(right["recommendations"]):
                    if left["recommendations"][key] != right["recommendations"][key]:
                        conflicts.append({"rule_ids": [left["id"], right["id"]], "key": key})
        try:
            offset = int(cursor or "0")
            if offset < 0:
                raise ValueError()
        except ValueError:
            fail("Неверный cursor")
        warnings = [] if any(r["usable_as_evidence"] and r["recommendations"] for r in found) else [
            "KNOWLEDGE_GAP: подтверждённых экспертных рекомендаций нет; описания задач не являются рецептами печати"]
        if any(r["usable_as_evidence"] and r["status"] == "reference" for r in found):
            warnings.append("Справочные данные (status=reference) не доказывают прочность конкретной детали")
        return {"items": found[offset:offset + limit], "next_cursor": str(offset + limit) if offset + limit < len(found) else None,
                "conflicts": conflicts, "warnings": warnings}
