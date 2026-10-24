"""Thread-safe, persistent task conversations. No Qt or slicer imports."""
import copy
import datetime
import json
import os
import shutil
import threading
import uuid


def uid():
    return uuid.uuid4().hex


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


class ApiError(Exception):
    def __init__(self, message, status=400, code="INVALID_ARGUMENTS", details=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = details or {}


def workspace_view(session):
    """A display projection; complete source evidence remains in the normal API and history."""
    result = {key: session[key] for key in ("id", "title", "revision", "updated_at", "workflow_state", "task", "plans", "operations", "exports", "calibration_for", "calibration_results", "calibration_process") if key in session}
    result["files"] = {fid: {key: value for key, value in row.items() if key != "path"} for fid, row in session["files"].items()}
    result["messages"] = [{key: row[key] for key in ("id", "role", "text", "created_at", "file_ids", "run_id", "origin") if key in row} for row in session["messages"]]
    result["runs"] = {rid: {key: row[key] for key in ("id", "status", "created_at", "finished_at", "error") if key in row} for rid, row in session["runs"].items()}
    task = {key: value for key, value in session["task_map"].items() if key != "resources"}
    task["facts"] = [dict(fact, value={"objects": [{key: row[key] for key in ("id", "name", "dimensions_mm", "printable", "file_refs") if key in row} for row in fact["value"].get("objects", [])]})
                     if fact.get("key") == "scene" else fact for fact in task.get("facts", [])]
    result["task_map"] = task
    result["events"] = [dict({key: row[key] for key in ("id", "seq", "session_id", "run_id", "created_at", "type", "operation_id", "tool", "code", "message", "phase", "request_number", "duration_ms") if key in row},
                             has_details=bool(row.get("arguments") or row.get("result"))) for row in session["events"]]
    return copy.deepcopy(result)


class SessionStore:
    def __init__(self, root):
        self.root = root
        self.lock = threading.RLock()
        os.makedirs(root, exist_ok=True)
        self.path = os.path.join(root, "sessions.json")
        self.run_id = uid()
        self.data = {"version": 2, "active_session_id": None, "sessions": {}}
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as source:
                loaded = json.load(source)
            if loaded.get("version") not in (1, 2) or not isinstance(loaded.get("sessions"), dict):
                raise RuntimeError("Неподдерживаемый формат sessions.json")
            if loaded["version"] == 1:
                backup = self.path + ".v1.bak"
                if not os.path.exists(backup):
                    shutil.copy2(self.path, backup)
                loaded["version"] = 2
            self.data = loaded
        # Scene object identifiers only have meaning in the running application.
        for session in self.data["sessions"].values():
            self._extend(session)
            for run in session["runs"].values():
                if run["status"] in ("queued", "running"):
                    run["status"] = "interrupted"
                    session["workflow_state"] = "interrupted"
            for plan in session["plans"].values():
                plan["status"] = "stale"
            session["task_map"]["snapshot_ref"] = None
            session["task_map"]["target_object_ids"] = []
            session["task_map"]["readiness"] = {"status": "stale", "reasons": ["Приложение перезапущено"]}
            if session["plans"]:
                session["workflow_state"] = "stale"
            for attachment in session["files"].values():
                attachment["scene_present"] = False
                if attachment["status"] in ("queued", "importing"):
                    attachment["status"] = "interrupted"
                    attachment["error"] = "Импорт прерван закрытием приложения."
                elif attachment["status"] == "imported":
                    attachment["status"] = "saved"
                attachment["object_ids"] = []
            for operation in session["operations"].values():
                if operation["status"] in ("queued", "importing", "running", "slicing", "applying", "exporting"):
                    operation["status"] = "interrupted"
                    operation["error"] = "Приложение было закрыто во время операции."
            self._task_status(session)
        if not self.data["sessions"]:
            self.create("Новая задача")
        elif self.data["active_session_id"] not in self.data["sessions"]:
            self.data["active_session_id"] = next(iter(self.data["sessions"]))
        self._save()

    def _save(self):
        temp = self.path + ".tmp"
        with open(temp, "w", encoding="utf-8") as target:
            json.dump(self.data, target, ensure_ascii=False, indent=2)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temp, self.path)

    def _session(self, session_id):
        if session_id not in self.data["sessions"]:
            raise ApiError("Сессия не найдена", 404)
        return self.data["sessions"][session_id]

    def _touch(self, session):
        session["updated_at"] = now()
        session["revision"] += 1
        self._save()

    def create(self, title="Новая задача"):
        with self.lock:
            session_id = uid()
            session = {"id": session_id, "title": self._title(title),
                       "created_at": now(), "updated_at": now(), "revision": 0,
                       "messages": [], "files": {}, "operations": {},
                       "task": {"description": "", "status": "draft"},
                       "requests": {}}
            self._extend(session)
            self.data["sessions"][session_id] = session
            self.data["active_session_id"] = session_id
            self._save()
            return self.get(session_id)

    @staticmethod
    def _title(title):
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 120:
            raise ApiError("Название должно содержать от 1 до 120 символов")
        return title.strip()

    def listing(self):
        with self.lock:
            rows = [{key: s[key] for key in ("id", "title", "updated_at")}
                    for s in self.data["sessions"].values()]
            return {"sessions": sorted(rows, key=lambda s: s["updated_at"], reverse=True),
                    "active_session_id": self.data["active_session_id"]}

    def select(self, session_id, compact=False):
        with self.lock:
            self._session(session_id)
            self.data["active_session_id"] = session_id
            self._save()
            return self.get(session_id, compact=compact)

    def rename(self, session_id, title, compact=False):
        with self.lock:
            s = self._session(session_id)
            s["title"] = self._title(title)
            self._touch(s)
            return self.get(session_id, compact=compact)

    def get(self, session_id, compact=False):
        with self.lock:
            original = self._session(session_id)
            if compact:
                return workspace_view(original)
            result = copy.deepcopy(original)
            result.pop("requests")
            result.pop("workflow_requests", None)
            result.pop("action_results", None)
            for attachment in result["files"].values():
                attachment.pop("path", None)
            return result

    def progress(self, session_id, operation_id, amount):
        """Progress is transient. The terminal operation persists it with the result."""
        with self.lock:
            operation = self._session(session_id)["operations"].get(operation_id)
            if operation and operation["status"] == "slicing":
                operation["progress"] = amount

    def attachments_for_object(self, object_id):
        with self.lock:
            return [{"session_id": session["id"], "file_id": attachment["id"]}
                    for session in self.data["sessions"].values() for attachment in session["files"].values()
                    if object_id in attachment.get("object_ids", [])]

    def event_detail(self, session_id, event_id):
        with self.lock:
            event = next((row for row in self._session(session_id)["events"] if row["id"] == event_id), None)
            if event is None:
                raise ApiError("Действие не найдено", 404)
            return copy.deepcopy(event)

    def add_file(self, session_id, attachment):
        with self.lock:
            s = self._session(session_id)
            s["files"][attachment["id"]] = attachment
            self._touch(s)
            result = dict(attachment)
            result.pop("path")
            return result

    def file(self, session_id, file_id):
        with self.lock:
            s = self._session(session_id)
            if file_id not in s["files"]:
                raise ApiError("Файл не найден в этой сессии", 404)
            return copy.deepcopy(s["files"][file_id])

    def message(self, session_id, request_id, text, file_ids, context):
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
            raise ApiError("Нужен request_id длиной до 100 символов")
        if not isinstance(text, str) or len(text) > 20000:
            raise ApiError("Описание должно быть строкой до 20 000 символов")
        if (not isinstance(file_ids, list) or len(file_ids) > 20
                or any(not isinstance(f, str) for f in file_ids)
                or len(set(file_ids)) != len(file_ids)):
            raise ApiError("Нужен список до 20 уникальных ID файлов")
        if not text.strip() and not file_ids:
            raise ApiError("Добавьте описание или файл")
        with self.lock:
            s = self._session(session_id)
            payload = {"text": text.strip(), "file_ids": file_ids}
            if request_id in s["requests"]:
                previous = s["requests"][request_id]
                if previous["payload"] != payload:
                    raise ApiError("request_id уже использован для другого сообщения", 409)
                return dict(previous["result"]), []
            for file_id in file_ids:
                self.file(session_id, file_id)
            message_id = uid()
            s["input_revision"] += 1
            s["task_map"]["input_revision"] = s["input_revision"]
            for plan in s["plans"].values():
                if plan["status"] not in ("failed", "stale"):
                    plan["status"] = "stale"
            if s["plans"]:
                s["workflow_state"] = "stale"
            s["messages"].append({"id": message_id, "role": "user", "created_at": now(),
                                  "text": text.strip(), "file_ids": file_ids,
                                  "context": copy.deepcopy(context)})
            if text.strip():
                s["task"]["description"] = "\n\n".join(
                    m["text"] for m in s["messages"] if m["role"] == "user" and m["text"])
            new_operations = []
            for file_id in file_ids:
                attachment = s["files"][file_id]
                if attachment["kind"] != "model" or attachment["status"] in ("queued", "importing", "imported"):
                    continue
                operation = {"id": uid(), "session_id": session_id, "file_id": file_id,
                             "status": "queued", "created_at": now(), "error": None}
                s["operations"][operation["id"]] = operation
                attachment.update(status="queued", error=None)
                new_operations.append(copy.deepcopy(operation))
            s["messages"].append({"id": uid(), "role": "system", "created_at": now(),
                                  "text": "Задача сохранена. Модели переданы на импорт." if new_operations
                                  else "Сообщение и вложения сохранены.", "file_ids": []})
            self._task_status(s)
            result = {"message_id": message_id, "operation_ids": [o["id"] for o in new_operations]}
            s["requests"][request_id] = {"payload": payload, "result": result}
            self._touch(s)
            return result, new_operations

    @staticmethod
    def _extend(session):
        from .agent.task_state import new_map
        for key, value in {"input_revision": 0, "workflow_state": "idle", "runs": {},
                           "events": [], "plans": {}, "workflow_requests": {},
                           "action_results": {}, "sources": {}, "exports": []}.items():
            session.setdefault(key, value)
        session.setdefault("task_map", new_map())

    def edit(self, session_id, change):
        """Never call Qt, MCP or the provider from a change callback."""
        with self.lock:
            session = self._session(session_id)
            backup = copy.deepcopy(session)
            try:
                result = change(session)
                self._touch(session)
                return copy.deepcopy(result)
            except Exception:
                self.data["sessions"][session_id] = backup
                raise

    def internal(self, session_id):
        with self.lock:
            return copy.deepcopy(self._session(session_id))

    def event(self, session_id, run_id, kind, **data):
        def append(session):
            event = {"id": uid(), "seq": len(session["events"]) + 1, "session_id": session_id,
                     "run_id": run_id, "created_at": now(), "type": kind}
            event.update(data)
            session["events"].append(event)
            return event
        return self.edit(session_id, append)

    @staticmethod
    def _task_status(session):
        used = set(f for m in session["messages"] for f in m.get("file_ids", []))
        states = [f["status"] for key, f in session["files"].items()
                  if key in used and f["kind"] == "model"]
        if any(s in ("queued", "importing") for s in states):
            status = "importing"
        elif any(s in ("error", "interrupted") for s in states):
            status = "needs_attention"
        elif states and all(s == "imported" for s in states):
            status = "ready" if session["task"]["description"] else "needs_description"
        else:
            status = "needs_model" if session["messages"] else "draft"
        session["task"]["status"] = status

    def operation(self, session_id, operation_id):
        with self.lock:
            operations = self._session(session_id)["operations"]
            if operation_id not in operations:
                raise ApiError("Операция не найдена", 404)
            return copy.deepcopy(operations[operation_id])

    def finish(self, session_id, operation_id, status, error=None, object_ids=None):
        with self.lock:
            s = self._session(session_id)
            op = s["operations"][operation_id]
            op.update(status=status, error=error, updated_at=now())
            attachment = s["files"][op["file_id"]]
            attachment.update(status=status, error=error)
            if object_ids is not None:
                previous = set(attachment.get("object_ids", []))
                targets = set(s["task_map"]["target_object_ids"])
                if previous & targets:
                    # Reloading the same attachment replaces its obsolete scene IDs.
                    s["task_map"]["target_object_ids"] = sorted((targets - previous) | set(object_ids))
                attachment.update(object_ids=object_ids, scene_present=bool(object_ids), run_id=self.run_id)
            if status in ("imported", "error"):
                message = ("Модель «{}» добавлена в сцену.".format(attachment["name"])
                           if status == "imported" else "Ошибка импорта «{}»: {}".format(attachment["name"], error))
                s["messages"].append({"id": uid(), "role": "system", "created_at": now(),
                                      "text": message, "file_ids": [], "operation_id": operation_id})
            self._task_status(s)
            self._touch(s)

    def reconcile_scene(self, object_ids):
        with self.lock:
            changed = False
            for s in self.data["sessions"].values():
                local_change = False
                for attachment in s["files"].values():
                    present = bool(attachment.get("run_id") == self.run_id
                                   and set(attachment.get("object_ids", [])) & object_ids)
                    if present != attachment.get("scene_present", False):
                        attachment["scene_present"] = present
                        local_change = True
                    if attachment["status"] == "imported" and not present:
                        attachment["status"] = "saved"
                        local_change = True
                if local_change:
                    self._task_status(s)
                    s["revision"] += 1
                    changed = True
            if changed:
                self._save()

    def invalidate_snapshot(self, snapshot_ref):
        with self.lock:
            changed = False
            for session in self.data["sessions"].values():
                local = False
                for plan in session["plans"].values():
                    expected = plan.get("applied_snapshot_ref", plan["snapshot_ref"])
                    if plan["status"] not in ("stale", "failed", "applying") and expected != snapshot_ref:
                        plan["status"] = "stale"
                        session["task_map"]["readiness"] = {"status": "stale", "reasons": ["Сцена или настройки изменились"]}
                        session["workflow_state"] = "stale"
                        local = True
                for operation in session["operations"].values():
                    if operation.get("kind") == "slice" and operation["status"] == "completed" and operation["snapshot_ref"] != snapshot_ref:
                        operation["status"] = "stale"
                        session["workflow_state"] = "stale"
                        local = True
                if local:
                    session["revision"] += 1
                    changed = True
            if changed:
                self._save()
