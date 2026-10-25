"""Local print evidence, isolated from chat JSON. Connections never cross threads."""
import copy
import json
import os
try:
    import sqlite3
except ImportError:
    # STE Slicer omits sqlite3; Windows provides the same database engine.
    from . import sqlite_native as sqlite3
import threading
from contextlib import contextmanager

from .session import ApiError, uid, now


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def conditions(snapshot, excluded_key=None):
    """Conservative applicability: even unknown nozzle sizes cannot establish a match."""
    context = snapshot.get("context", {})
    printer = context.get("printer") or {}
    extruders = context.get("extruders") or []
    if not printer.get("id") or not extruders or any(not e.get("nozzle_mm") or not (e.get("material") or {}).get("id") for e in extruders):
        return None
    return {"printer": printer.get("id"), "definition": printer.get("definition_id"),
            "mode": (context.get("mode") or {}).get("id"), "profile": {k: v for k, v in (context.get("profile") or {}).items() if k != "has_user_changes"},
            "extruders": [{k: e.get(k) for k in ("id", "position", "nozzle_mm", "material", "enabled")} for e in extruders],
            "objects": [dict({k: o.get(k) for k in ("geometry_revision", "dimensions_mm", "scale", "rotation_quaternion", "extruder_id")},
                             per_mesh_settings={k: v for k, v in (o.get("per_mesh_settings") or {}).items() if k != excluded_key})
                        for o in snapshot.get("objects", [])],
            "settings": [{"target": row["target"], "values": {k: v for k, v in row["values"].items() if k != excluded_key}}
                         for row in snapshot.get("settings", [])]}


def setting(snapshot, key, target):
    return next((row["values"].get(key) for row in snapshot.get("settings", []) if row["target"] == target), None)


class ExperienceStore:
    def __init__(self, root):
        self.root = os.path.join(root, "print-experience")
        os.makedirs(self.root, exist_ok=True)
        self.path = os.path.join(self.root, "experience.sqlite3")
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS prints (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL, created_at TEXT NOT NULL, snapshot TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS observations (id TEXT PRIMARY KEY, print_id TEXT NOT NULL REFERENCES prints(id),
                    request_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS adjustments (id TEXT PRIMARY KEY, print_id TEXT NOT NULL REFERENCES prints(id),
                    observation_id TEXT NOT NULL REFERENCES observations(id), created_at TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS lessons (id TEXT PRIMARY KEY, adjustment_id TEXT NOT NULL UNIQUE REFERENCES adjustments(id),
                    created_at TEXT NOT NULL, degree TEXT NOT NULL, conditions TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS print_sessions ON prints(session_id, created_at);
                CREATE TABLE IF NOT EXISTS observation_revisions (id TEXT PRIMARY KEY,
                    observation_id TEXT NOT NULL REFERENCES observations(id), created_at TEXT NOT NULL,
                    data TEXT NOT NULL, excluded INTEGER NOT NULL, reason TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS observation_revision_lookup ON observation_revisions(observation_id, created_at);
                PRAGMA user_version=2;
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _read(self, db, pid):
        row = db.execute("SELECT * FROM prints WHERE id=?", (pid,)).fetchone()
        if not row:
            raise ApiError("Печать не найдена", 404)
        item = dict(row)
        item["snapshot"] = json.loads(item["snapshot"])
        for table in ("observations", "adjustments"):
            item[table] = [dict(dict(r), data=json.loads(r["data"])) for r in db.execute(
                "SELECT * FROM " + table + " WHERE print_id=? ORDER BY created_at, rowid", (pid,))]
        for observation in item["observations"]:
            revisions = [dict(dict(r), data=json.loads(r["data"])) for r in db.execute(
                "SELECT * FROM observation_revisions WHERE observation_id=? ORDER BY created_at, rowid", (observation["id"],))]
            observation["original_data"] = copy.deepcopy(observation["data"])
            observation["revisions"], observation["excluded"] = revisions, False
            if revisions:
                observation.update(data=revisions[-1]["data"], excluded=bool(revisions[-1]["excluded"]))
        return item

    def revise_observation(self, observation_id, data, excluded, reason):
        with self.lock, self.connect() as db:
            row = db.execute("SELECT print_id FROM observations WHERE id=?", (observation_id,)).fetchone()
            if not row:
                raise ApiError("Наблюдение не найдено", 404)
            db.execute("INSERT INTO observation_revisions VALUES (?,?,?,?,?,?)",
                       (uid(), observation_id, now(), encode(data), int(excluded), reason))
            return self._read(db, row["print_id"])

    def get(self, pid):
        with self.lock, self.connect() as db:
            return self._read(db, pid)

    def record(self, sid, operation, data, request_id, print_id=None):
        with self.lock, self.connect() as db:
            previous = db.execute("SELECT * FROM observations WHERE request_id=?", (request_id,)).fetchone()
            if previous:
                item = self._read(db, previous["print_id"])
                if item["session_id"] != sid or item["operation_id"] != operation["id"] or json.loads(previous["data"]) != data:
                    raise ApiError("request_id уже использован с другими данными", 409)
                return item
            row = db.execute("SELECT id FROM prints WHERE id=? AND session_id=? AND operation_id=?", (print_id, sid, operation["id"])).fetchone() if print_id else None
            if print_id and not row:
                raise ApiError("Печать для дополнения не найдена", 404)
            pid = row["id"] if row else uid()
            if not row:
                db.execute("INSERT INTO prints VALUES (?,?,?,?,?)", (pid, sid, operation["id"], operation.get("printed_at") or now(), encode(operation["print_snapshot"])))
            db.execute("INSERT INTO observations VALUES (?,?,?,?,?)", (uid(), pid, request_id, now(), encode(data)))
            return self._read(db, pid)

    def propose(self, pid, observation_id, data):
        with self.lock, self.connect() as db:
            item = self._read(db, pid)
            for old in item["adjustments"]:
                if (old["observation_id"] == observation_id and old["data"].get("defect") == data["defect"]
                        and old["data"].get("choice") == data["choice"] and old["data"]["state"] == "proposed"
                        and old["data"]["snapshot_ref"] == data["snapshot_ref"]):
                    return old
            aid = uid()
            result = dict(id=aid, print_id=pid, observation_id=observation_id, created_at=now(), data=data)
            db.execute("INSERT INTO adjustments VALUES (?,?,?,?,?)", (aid, pid, observation_id, result["created_at"], encode(data)))
            db.execute("INSERT INTO lessons VALUES (?,?,?,?,?,?)", (uid(), aid, now(), "hypothesis",
                       encode(conditions(item["snapshot"], data["key"])), encode(dict(data, print_id=pid))))
            return result

    def adjustment(self, aid):
        with self.lock, self.connect() as db:
            row = db.execute("SELECT * FROM adjustments WHERE id=?", (aid,)).fetchone()
            if not row:
                raise ApiError("Исправление не найдено", 404)
            return dict(dict(row), data=json.loads(row["data"]))

    def update_adjustment(self, aid, changes, degree=None):
        with self.lock, self.connect() as db:
            row = self.adjustment(aid)
            row["data"].update(changes)
            db.execute("UPDATE adjustments SET data=? WHERE id=?", (encode(row["data"]), aid))
            db.execute("UPDATE lessons SET data=? WHERE adjustment_id=?", (encode(dict(row["data"], print_id=row["print_id"])), aid))
            if degree:
                db.execute("UPDATE lessons SET degree=? WHERE adjustment_id=?", (degree, aid))
            return row

    def library(self, sid=None, limit=100):
        with self.lock, self.connect() as db:
            params = (sid,) if sid else ()
            if limit is not None:
                params += (limit,)
            rows = db.execute("SELECT id FROM prints" + (" WHERE session_id=?" if sid else "") +
                              " ORDER BY created_at DESC, rowid DESC" + (" LIMIT ?" if limit is not None else ""), params).fetchall()
            prints = [self._read(db, row["id"]) for row in rows]
            lessons = [dict(dict(r), conditions=json.loads(r["conditions"]), data=json.loads(r["data"]))
                       for r in db.execute("SELECT * FROM lessons ORDER BY created_at DESC, rowid DESC" +
                                           (" LIMIT ?" if limit is not None else ""), (limit,) if limit is not None else ())]
            return {"prints": prints, "lessons": lessons}

    def find(self, snapshot):
        """Only verified corrections may become settings; observations stay explicitly factual."""
        current = conditions(snapshot)
        if current is None or not current["mode"] or not current["objects"]:
            return {"observations": [], "lessons": []}
        from .knowledge_base import derive_conclusions, knowledge_conditions, effective_report
        scope = knowledge_conditions(snapshot)
        if scope is None:
            return {"observations": [], "lessons": []}
        library = self.library(limit=None)
        conclusions = derive_conclusions(library)
        eligible = {member for conclusion in conclusions if conclusion["type"] == "correction" and
                    conclusion["status"] == "confirmed" for member in conclusion["verified_lesson_ids"]}
        observations = [{"print_id": p["id"], "observations": [effective_report(p)]} for p in library["prints"]
                        if effective_report(p) is not None and knowledge_conditions(p["snapshot"]) == scope][:10]
        prints = {p["id"]: p for p in library["prints"]}
        lessons = []
        for row in library["lessons"]:
            data = row["data"]
            original = prints.get(data.get("print_id"), {})
            candidate = knowledge_conditions(snapshot, data["key"], data.get("defect"))
            verified = knowledge_conditions(data.get("applied_snapshot") or {}, data["key"], data.get("defect"))
            baseline = knowledge_conditions(original.get("snapshot", {}), data["key"], data.get("defect"))
            if row["id"] not in eligible or baseline is None or candidate not in (baseline, verified):
                continue
            if setting(snapshot, data["key"], data["target"]) not in (data["before"], data["after"]):
                continue
            lessons.append(dict(row, degree="confirmed"))
        # Disagreeing verified trials are evidence of uncertainty, never last-write-wins recipes.
        conflicts = set((r["data"]["key"], encode(r["data"]["target"])) for r in lessons
                        if len(set(encode(x["data"]["after"]) for x in lessons if x["data"]["key"] == r["data"]["key"] and x["data"]["target"] == r["data"]["target"])) > 1)
        return {"observations": observations, "lessons": [r for r in lessons if (r["data"]["key"], encode(r["data"]["target"])) not in conflicts],
                "conflicts": [k for k, target in conflicts]}


def reuse(result, evidence):
    """Overlay compatible verified corrections without losing the base reference sources."""
    result = copy.deepcopy(result)
    for lesson in evidence.get("lessons", []):
        data = lesson["data"]
        result["settings"] = [s for s in result["settings"] if s["key"] != data["key"]]
        result["settings"].append({"key": data["key"], "value": data["after"], "unit": data.get("unit"),
                                   "target": data["target"], "reason": "Подтверждено независимыми проверками: " + data["reason"],
                                   "source": lesson["id"]})
        result["source_ids"].append(lesson["id"])
    for observation in evidence.get("observations", []):
        if any(o["data"]["result"] in ("defects", "unknown") for o in observation["observations"]):
            result["warnings"].append("В похожих условиях были замечания к печати; источник " + observation["print_id"])
    if evidence.get("conflicts"):
        result["warnings"].append("Результаты повторных печатей противоречат друг другу: " + ", ".join(evidence["conflicts"]))
    return result


def proposed_snapshot(snapshot, result):
    snapshot = copy.deepcopy(snapshot)
    extruders = snapshot.get("context", {}).get("extruders") or []
    if not extruders:
        return {}
    material_id = result["material"]["id"]
    if not material_id or not any((e.get("material") or {}).get("id") == material_id or
            ((e.get("material") or {}).get("id") or "").startswith(material_id + "_") for e in extruders):
        return {}
    if result.get("mode") != (snapshot.get("context", {}).get("mode") or {}).get("id"):
        return {}
    selected = next((e for e in extruders if e.get("active")), extruders[0])
    for change in result["settings"]:
        target = change.get("target") or ({"scope": "global"} if change["key"] in ("layer_height", "support_enable", "material_bed_temperature")
                                          else {"scope": "extruder", "extruder_id": selected["id"]})
        for row in snapshot.get("settings", []):
            if row["target"] == target:
                row["values"][change["key"]] = change["value"]
    return snapshot
