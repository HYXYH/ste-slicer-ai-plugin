"""Conversation merge and deterministic evidence coverage, never an LLM confidence claim."""
import copy
import hashlib
import io
import json
import math
import os
import shutil
import threading

from .session import ApiError, uid, now
from .uploads import receive

VERSION = 1
THRESHOLD = 80
TOLERANCE_MM = .2
FIELDS = {"load": "Какая нагрузка ожидается? Укажите величину и единицы или «не применяется».",
          "temperature": "Какова рабочая температура детали? Укажите °C или «не применяется».",
          "environment": "В какой среде будет работать деталь: помещение, УФ, влага, химия?"}
OBSERVATIONS = {"dimensions_check": "Чем и в каких направлениях измерен куб? Повторите измерение отклонившихся осей. Автоматическую коррекцию масштаба по одному кубу не применяем.",
                "adhesion_context": "Где произошёл отрыв: по углам или по всему основанию? Опишите состояние поверхности стола и первого слоя.",
                "layer_context": "Где видны пропуски или расслоение? Были ли щелчки экструдера, обрыв подачи или отрыв детали?",
                "surface_context": "Какие дефекты поверхности видны: наплывы, нити, поры или повторяющиеся волны?"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def known_answer(value):
    return bool(value.strip()) and value.strip().casefold().strip(".! ") not in ("не знаю", "unknown", "неизвестно", "нет данных", "не определено", "?")


class Workspace:
    def __init__(self, store, bridge):
        self.store, self.bridge = store, bridge
        self.lock = threading.RLock()

    def merge(self, ids, title, request_id):
        if len(ids) < 2 or len(ids) > 10 or len(set(ids)) != len(ids):
            raise ApiError("Выберите от 2 до 10 разных чатов")
        title = self.store._title(title)
        with self.lock:
            with self.store.lock:
                existing = list(self.store.data["sessions"].values())
            for row in existing:
                if row.get("merge_request", {}).get("id") == request_id:
                    if row["merge_request"]["sessions"] != ids or row["merge_request"]["title"] != title:
                        raise ApiError("request_id уже использован", 409)
                    return self.store.get(row["id"])
            originals = [self.store.internal(sid) for sid in ids]
            if any(any(r["status"] in ("queued", "running") for r in s["runs"].values()) or
                   s["workflow_state"] in ("applying", "slicing", "exporting", "importing", "waiting_import") or
                   any(o["status"] in ("queued", "importing", "running", "applying", "slicing", "exporting") for o in s["operations"].values()) for s in originals):
                raise ApiError("Дождитесь завершения операций в выбранных чатах", 409)
            sid, created_sid, previous_active = uid(), None, None
            temporary_sid = sid
            try:
                files, messages, descriptions = {}, [], []
                for original in originals:
                    mapping = {}
                    for old_id, attachment in original["files"].items():
                        new_id = uid()
                        folder = os.path.join(self.store.root, "uploads", sid)
                        os.makedirs(folder, exist_ok=True)
                        destination = os.path.join(folder, new_id + attachment["extension"])
                        shutil.copyfile(attachment["path"], destination)
                        files[new_id] = dict(copy.deepcopy(attachment), id=new_id, path=destination,
                                             origin={"session_id": original["id"], "file_id": old_id})
                        if files[new_id]["status"] in ("queued", "importing"):
                            files[new_id].update(status="saved", scene_present=False, object_ids=[])
                        mapping[old_id] = new_id
                    messages.append({"id": uid(), "role": "system", "text": "Объединён чат: " + original["title"], "created_at": now(), "file_ids": []})
                    for old in original["messages"]:
                        # Keep the conversation, but old source IDs and approvals never become new plan authority.
                        message = {k: copy.deepcopy(old[k]) for k in ("role", "text", "created_at", "context") if k in old}
                        message.update(id=uid(), file_ids=[mapping[f] for f in old.get("file_ids", []) if f in mapping],
                                       origin={"session_id": original["id"], "message_id": old.get("id")})
                        messages.append(message)
                    if original["task"]["description"]:
                        descriptions.append(original["title"] + ":\n" + original["task"]["description"])
                def save(session):
                    session.update(files=files, messages=messages, merge_request={"id": request_id, "sessions": ids, "title": title},
                                   input_revision=1, workflow_state="idle")
                    session["task"]["description"] = "\n\n".join(descriptions)[:20000]
                    session["task_map"]["target_object_ids"] = sorted(set(oid for f in files.values() if f.get("scene_present") for oid in f.get("object_ids", [])))
                    session["task_map"]["readiness"] = {"status": "needs_data", "reasons": ["Объединённые требования требуют нового анализа и согласования"]}
                    self.store._task_status(session)
                # Large attachments are copied before taking the store lock or publishing a new chat.
                with self.store.lock:
                    previous_active = self.store.data["active_session_id"]
                    merged = self.store.create(title)
                    created_sid = sid = merged["id"]
                    if files:
                        source_folder = os.path.realpath(os.path.join(self.store.root, "uploads", temporary_sid))
                        destination_folder = os.path.realpath(os.path.join(self.store.root, "uploads", sid))
                        prefix = os.path.realpath(os.path.join(self.store.root, "uploads")) + os.sep
                        if not source_folder.startswith(prefix) or not destination_folder.startswith(prefix) or os.path.exists(destination_folder):
                            raise ApiError("Не удалось создать папку объединённого чата", 409)
                        os.rename(source_folder, destination_folder)
                        for f in files.values():
                            f["path"] = os.path.join(destination_folder, os.path.basename(f["path"]))
                    self.store.edit(sid, save)
                    return self.store.get(sid)
            except Exception:
                if created_sid:
                    with self.store.lock:
                        self.store.data["sessions"].pop(created_sid, None)
                        self.store.data["active_session_id"] = previous_active
                        self.store._save()
                # Only files created under this new session can be removed on rollback.
                parent = os.path.realpath(os.path.join(self.store.root, "uploads")) + os.sep
                for cleanup_sid in set([temporary_sid, sid]):
                    folder = os.path.realpath(os.path.join(self.store.root, "uploads", cleanup_sid))
                    if folder.startswith(parent) and os.path.isdir(folder):
                        shutil.rmtree(folder)
                raise

    def process(self):
        try:
            return self.bridge.call("get_calibration_context", {})
        except ApiError:
            return {"signature": None, "context": self.bridge.snapshot(), "profile_id": None}

    def assessment(self, sid):
        session = self.store.internal(sid)
        process = self.process()
        context = process["context"]
        answers = session.get("assessment_answers", {})
        models = [f for f in session["files"].values() if f["kind"] == "model"]
        try:
            scene = self.bridge.call("get_scene_snapshot", {})
        except ApiError:
            scene = {"objects": [], "snapshot_ref": None}
        targets = set(session["task_map"].get("target_object_ids", [])) or set(oid for f in models for oid in f.get("object_ids", []))
        objects = [o for o in scene["objects"] if o["id"] in targets]
        dimensions = bool(objects) and all(isinstance(o.get("dimensions_mm"), list) and len(o["dimensions_mm"]) == 3 and
                                         all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0 for v in o["dimensions_mm"]) for o in objects)
        geometry = 25 if dimensions and targets.issubset(set(o["id"] for o in objects)) else 0
        compatible = False
        if process.get("profile_id"):
            try:
                page = self.bridge.call("find_compatible_profiles", {"limit": 50})
                while True:
                    compatible = any(p["id"] == process["profile_id"] and p["compatibility"] == "pass" for p in page["items"])
                    if compatible or not page.get("next_cursor"):
                        break
                    page = self.bridge.call("find_compatible_profiles", {"limit": 50, "cursor": page["next_cursor"]})
            except ApiError:
                pass
        configuration = (5 if context.get("printer") else 0) + (5 if context.get("mode") else 0) + (
            5 if any((e.get("material") or {}).get("id") for e in context.get("extruders", [])) else 0) + (10 if compatible else 0)
        requirements = (5 if session["task"]["description"].strip() else 0) + sum(5 for key in FIELDS if known_answer(answers.get(key, "")))
        if session.get("calibration_for"):
            requirements = 20
        records = []
        if process.get("signature"):
            for row in self.store.listing()["sessions"]:
                records += [r for r in self.store.internal(row["id"]).get("calibration_results", []) if r["process_signature"] == process["signature"]]
        record = max(records, key=lambda r: r["created_at"]) if records else None
        calibration = 0
        if record:
            calibration = sum(weight for axis, weight in zip(("x_mm", "y_mm", "z_mm"), (7, 7, 6)) if abs(record[axis] - 20) <= TOLERANCE_MM)
            calibration += (4 if record["adhesion_ok"] else 0) + (3 if record["layers_ok"] else 0) + (3 if record["surface_ok"] else 0)
        components = [{"id": "geometry", "title": "Модель и размеры", "score": geometry, "maximum": 25,
                       "reason": "Габариты целевых моделей прочитаны из сцены" if geometry else "Загрузите целевую модель в сцену"},
                      {"id": "configuration", "title": "Материал и профиль", "score": configuration, "maximum": 25,
                       "reason": "Совместимость текущего профиля подтверждена" if compatible else "Нужен совместимый профиль текущей конфигурации"},
                      {"id": "requirements", "title": "Условия эксплуатации", "score": requirements, "maximum": 20,
                       "reason": "Калибровочный образец: эксплуатационные требования не применяются" if session.get("calibration_for") else "Назначение и условия указаны пользователем" if requirements == 20 else "Уточните нагрузку, температуру и среду"},
                      {"id": "calibration", "title": "Результат калибровки", "score": calibration, "maximum": 30,
                       "reason": "Измерения куба для текущих настроек" if record else "Для текущих настроек нет измеренного куба"}]
        questions = [{"field": k, "text": FIELDS[k]} for k in sorted(FIELDS) if not known_answer(answers.get(k, ""))]
        if session.get("calibration_for"):
            questions = []
        if record:
            failed = {"dimensions_check": any(abs(record[a] - 20) > TOLERANCE_MM for a in ("x_mm", "y_mm", "z_mm")),
                      "adhesion_context": not record["adhesion_ok"], "layer_context": not record["layers_ok"], "surface_context": not record["surface_ok"]}
            questions += [{"field": k, "text": OBSERVATIONS[k], "label": "Уточнить: " + {"dimensions_check": "размеры куба", "adhesion_context": "адгезия", "layer_context": "слои", "surface_context": "поверхность"}[k]}
                          for k in sorted(failed) if failed[k] and not known_answer(answers.get(k, ""))]
        weakest = sorted(components, key=lambda c: (c["score"] / c["maximum"], c["id"]))
        score = sum(c["score"] for c in components)
        failed_checks = []
        if record:
            failed_checks = [axis for axis in ("x_mm", "y_mm", "z_mm") if abs(record[axis] - 20) > TOLERANCE_MM]
            failed_checks += [k for k in ("adhesion_ok", "layers_ok", "surface_ok") if not record[k]]
        return {"version": VERSION, "score": score, "threshold": session.get("assessment_threshold", THRESHOLD), "components": components,
                "weakest": [c["id"] for c in weakest if c["score"] < c["maximum"]], "questions": questions,
                "failed_checks": failed_checks,
                "recommend_calibration": (score < session.get("assessment_threshold", THRESHOLD) or bool(failed_checks)) and calibration < 30,
                "process_signature": process.get("signature"), "calibration": record, "answers": answers,
                "meaning": "Эвристика полноты проверок, не вероятность качества G-code и не подтверждение прочности.",
                "gcode_check": "Соответствие результата текущей сцене проверяется отдельно при расчёте и экспорте."}

    def save_answers(self, sid, answers, threshold=None):
        if set(answers) - (set(FIELDS) | set(OBSERVATIONS)) or any(not isinstance(v, str) or not v.strip() or len(v) > 2000 for v in answers.values()):
            raise ApiError("Укажите условия эксплуатации текстом")
        if threshold is not None and (isinstance(threshold, bool) or not isinstance(threshold, int) or not 1 <= threshold <= 100):
            raise ApiError("Порог должен быть целым числом от 1 до 100")
        current = self.store.internal(sid)
        changed = {k: v for k, v in answers.items() if current.get("assessment_answers", {}).get(k) != v}
        if changed:
            self.store.message(sid, "assessment-" + uid(), "Условия эксплуатации:\n" + "\n".join(k + ": " + changed[k] for k in sorted(changed)), [], self.bridge.snapshot())
        def save(session):
            session.setdefault("assessment_answers", {}).update(answers)
            if threshold is not None:
                session["assessment_threshold"] = threshold
        self.store.edit(sid, save)
        return self.assessment(sid)

    def create_calibration(self, sid, request_id):
        with self.lock:
            original = self.store.internal(sid)
            for row in self.store.listing()["sessions"]:
                candidate = self.store.internal(row["id"])
                if candidate.get("calibration_for") == {"session_id": sid, "request_id": request_id}:
                    return self.store.get(candidate["id"])
            process = self.process()
            if not process.get("signature"):
                raise ApiError("Сначала выберите принтер, материал и профиль", 409)
            child = self.store.create("Калибровочный куб · " + original["title"][:80])
            path = os.path.join(os.path.dirname(__file__), "models", "cube-20mm.stl")
            with open(path, "rb") as source:
                raw = source.read()
            attachment = receive(self.store, child["id"], "Куб 20 мм.stl", io.BytesIO(raw), len(raw))
            def save(session):
                session.update(calibration_for={"session_id": sid, "request_id": request_id}, calibration_process=process["signature"])
                session["task"]["description"] = "Калибровочный куб 20×20×20 мм. Сохранить текущие материал, профиль, масштаб и ориентацию. Геометрический образец, не для эксплуатации. Подготовить план расчёта и экспорта; физическую печать пользователь выполняет вручную."
                session["messages"].append({"id": uid(), "role": "system", "text": "Куб сохранён как вложение. Сохраните основной проект и очистите сцену в Prepare, затем загрузите куб в сцену. Исходный чат сохранён.", "file_ids": [attachment["id"]], "created_at": now()})
            self.store.edit(child["id"], save)
            return self.store.get(child["id"])

    def record_calibration(self, sid, body):
        session = self.store.internal(sid)
        if not session.get("calibration_for"):
            raise ApiError("Результат можно записать только в задаче калибровочного куба")
        if not any(o.get("kind") == "slice" and o["status"] == "completed" and
                   session["plans"].get(o.get("plan_id"), {}).get("status") == "applied" for o in session["operations"].values()):
            raise ApiError("Сначала согласуйте и рассчитайте куб в этой задаче", 409)
        fields = {"x_mm", "y_mm", "z_mm", "adhesion_ok", "layers_ok", "surface_ok", "printed", "process_signature"}
        if set(body) != fields or body["printed"] is not True:
            raise ApiError("Подтвердите фактическую печать и заполните измерения и проверки")
        for key in ("x_mm", "y_mm", "z_mm"):
            value = body[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 1 <= value <= 100:
                raise ApiError("Размеры нужно указать в миллиметрах от 1 до 100")
        if any(not isinstance(body[k], bool) for k in ("adhesion_ok", "layers_ok", "surface_ok")):
            raise ApiError("Укажите результат каждой проверки")
        process = self.process()
        if not body["process_signature"] or body["process_signature"] != session["calibration_process"] or body["process_signature"] != process.get("signature"):
            raise ApiError("Настройки изменились после подготовки куба. Создайте новую калибровку", 409, "STALE_INPUT")
        record = dict(body, id=uid(), created_at=now(), version=VERSION)
        self.store.edit(sid, lambda s: s.setdefault("calibration_results", []).append(record))
        return record

    def cube_plan(self, sid, plans, request_id):
        with self.lock:
            session = self.store.internal(sid)
            if not session.get("calibration_for"):
                raise ApiError("Это не задача калибровочного куба")
            previous = session.get("calibration_plan_request")
            if previous and previous["request_id"] == request_id:
                return session["plans"][previous["plan_id"]]
            process = self.process()
            if not process.get("profile_id"):
                raise ApiError("Выберите совместимый профиль в STE Slicer", 409)
            scene = self.bridge.call("get_scene_snapshot", {})
            targets = sorted(set(oid for f in session["files"].values() for oid in f.get("object_ids", [])))
            objects = [o for o in scene["objects"] if o["id"] in targets]
            if len(objects) != 1 or len(targets) != 1 or not objects[0].get("dimensions_mm") or any(abs(v - 20) > .01 for v in objects[0]["dimensions_mm"]):
                raise ApiError("Загрузите куб в сцену с исходным размером 20×20×20 мм", 409)
            context = process["context"]
            extruder = next((e for e in context.get("extruders", []) if e.get("active")), (context.get("extruders") or [None])[0])
            if not extruder or not (extruder.get("material") or {}).get("id"):
                raise ApiError("Выберите материал", 409)
            source_id = uid()
            refs = [{"id": source_id, "version": 1}]
            candidate = {"id": "cube-candidate", "status": "selected", "mode": context["mode"]["id"],
                         "material_id": extruder["material"]["id"], "extruder_id": extruder["id"],
                         "profile_id": process["profile_id"], "requirement_checks": [], "missing_fields": []}
            def prepared(s):
                s["sources"][source_id] = {"id": source_id, "version": 1, "kind": "tool", "tool": "get_scene_snapshot", "data": scene}
                s["task_map"].update(target_object_ids=targets, snapshot_ref=scene["snapshot_ref"], candidates=[candidate],
                                     requirements=[], constraints=[], conflicts=[], assumptions=[], unknowns=[], questions=[],
                                     readiness={"status": "ready_for_plan", "reasons": []})
                s["task_map"]["revision"] += 1
                s["calibration_process"] = process["signature"]
                for old in s["plans"].values():
                    old["status"] = "stale"
            self.store.edit(sid, prepared)
            try:
                plan = plans.build(sid, {"decision_proposals": [{"candidate_id": candidate["id"], "actions": [],
                                    "source_refs": refs, "limitations": ["Куб проверяет только размеры и видимые дефекты; прочность основной детали не подтверждена"]}]})
            except ApiError as exc:
                def blocked(s):
                    s["task_map"]["readiness"] = {"status": "needs_input", "reasons": [str(exc)]}
                    s["workflow_state"] = "waiting_user"
                self.store.edit(sid, blocked)
                raise
            self.store.edit(sid, lambda s: s.update(workflow_state="plan_ready", calibration_plan_request={"request_id": request_id, "plan_id": plan["id"]}))
            return plan
