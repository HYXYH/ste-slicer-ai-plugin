"""Stepwise analysis for light models: the server splits the task into small LLM requests
and asks the user between them. Every step has a narrow schema and a reference fallback."""
import copy
import hashlib
import json

from ..session import ApiError
from .contracts import fail, strict_json, validate, obj, array
from .output_contract import validation_reason
from .prompts import STEP_SYSTEM, STEP_REQUIREMENTS, STEP_CLARIFY, STEP_MATERIAL, STEP_SUMMARY
from .recommend import (recommend, load_records, material_record, known_id, fits, service_limit,
                        glass_transition, required_temperature, GLASS_TRANSITION)

CONFIRM = "Верно"
AGREE = "Согласен"
UNKNOWN = "Не знаю"
# Facts that change the material or the settings. The model only asks about missing ones.
CHECKLIST = ["назначение детали и тип нагрузки", "рабочая температура", "среда: УФ, влага, масла, химия",
             "точность и посадки", "качество поверхности", "какой материал есть в наличии"]
# Settings that live on the global stack; the rest are resolved per extruder.
GLOBAL_KEYS = ("layer_height", "support_enable", "material_bed_temperature")

SHORT = {"type": "string", "minLength": 1, "maxLength": 200}
UNIT = {"type": "string", "maxLength": 20}
REQUIREMENTS_SCHEMA = obj({"requirements": array(obj({"key": SHORT, "value": SHORT, "unit": UNIT,
                                                      "strength": {"type": "string", "enum": ["hard", "preference"]}},
                                                     ["key", "value", "unit", "strength"]), 12, 1),
                           "summary": {"type": "string", "minLength": 1, "maxLength": 400}}, ["requirements", "summary"])
CLARIFY_SCHEMA = obj({"questions": array(obj({"field": SHORT, "text": SHORT, "reason": SHORT, "options": array(SHORT, 4)},
                                             ["field", "text", "reason", "options"]), 2)}, ["questions"])
MATERIAL_SCHEMA = obj({"reason": {"type": "string", "minLength": 1, "maxLength": 600}, "risks": array(SHORT, 3)}, ["reason", "risks"])
SUMMARY_SCHEMA = obj({"summary": {"type": "string", "minLength": 1, "maxLength": 800}, "limitations": array(SHORT, 3)}, ["summary", "limitations"])


def digest(value):
    return hashlib.sha1(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:10]


def short_title(record):
    return record["title"].split(":")[0]


def is_step(question):
    return question["id"].startswith("step-")


class Steps:
    def __init__(self, complete, rejected, progress):
        self.complete, self.rejected, self.progress = complete, rejected, progress

    def ask(self, prompt, payload, schema, phase):
        """Two bounded attempts; None lets the caller use its reference fallback."""
        messages = [{"role": "system", "content": STEP_SYSTEM + "\n" + prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}]
        for attempt in range(2):
            response = self.complete(messages, structured=schema)
            try:
                if response.get("tool_calls"):
                    raise ValueError()
                result = strict_json(response["content"])
                validate(result, schema, phase)
                return result
            except (ApiError, ValueError, TypeError, KeyError) as exc:
                reason = self.rejected(exc, phase)
                messages.append({"role": "assistant", "content": response.get("content") or ""})
                messages.append({"role": "user", "content": "Исправь JSON по схеме. Ошибка: " + reason})
        return None

    def optional(self, prompt, payload, schema, phase):
        """Explanations are not decisions: a provider failure here keeps the reference answer."""
        try:
            return self.ask(prompt, payload, schema, phase)
        except ApiError as exc:
            if exc.code == "CANCELLED":
                raise
            self.rejected(exc, phase)
            return None

    def brief(self, session, questions):
        """Task text without service answers, so confirmations do not restart earlier steps."""
        payloads = {v["message_id"]: v["payload"] for v in session["workflow_requests"].values()
                    if isinstance(v, dict) and "payload" in v and "message_id" in v}
        parts, refs = [session["task"].get("description") or ""], []
        for message in [m for m in session["messages"] if m["role"] == "user"]:
            payload = payloads.get(message["id"])
            if payload is None:
                text = message.get("text") or ""
            else:
                text = [payload.get("text", "")]
                for answer in payload.get("answers", []):
                    question = questions.get(answer["question_id"])
                    if question is None or question["field"] == "confirm_material" or question["id"].startswith("step-clar-"):
                        continue
                    if question["field"] == "confirm_requirements":
                        if answer["value"] != CONFIRM:
                            text.append("Уточнение: " + answer["value"])
                    else:
                        text.append(question["text"] + " " + answer["value"])
                text = "\n".join(t for t in text if t)
            if text:
                parts.append(text)
                if message["id"] in session["sources"]:
                    refs.append(message["id"])
        return "\n".join(p for p in parts if p), refs

    def advance(self, session, configuration, catalog=()):
        """Returns (task_map, proposal). proposal["waiting_step"] marks a stop for the user."""
        task = copy.deepcopy(session["task_map"])
        state = task.setdefault("steps", {})
        questions = {q["id"]: q for q in task["questions"]}
        context = configuration["data"].get("context") or {}
        config_refs = configuration["source_refs"]
        brief, refs = self.brief(session, questions)
        refs = refs or [m["id"] for m in session["messages"] if m["role"] == "user" and m["id"] in session["sources"]]
        user_refs = [{"id": ref, "version": session["sources"][ref]["version"]} for ref in refs] or config_refs
        key = digest(brief)
        # Stepwise owns the map: earlier single-request records would block readiness.
        for name in ("constraints", "unknowns", "conflicts", "assumptions", "candidates", "decision_proposals"):
            task[name] = []
        task["questions"] = [q for q in task["questions"] if q.get("answer")]

        # Step 1-2: requirements and clarifying questions, confirmed by the user.
        requirements = state.get("requirements")
        if not requirements or requirements["key"] != key:
            self.progress("Шаг 1 из 4: выделяю требования из задачи")
            result = self.ask(STEP_REQUIREMENTS, {"task": brief}, REQUIREMENTS_SCHEMA, "step_requirements")
            if result is None:
                fail("LLM не смогла выделить требования из задачи", "INVALID_MODEL_OUTPUT", 502)
            rows = [dict(row, id="req-" + key + "-" + str(index + 1)) for index, row in enumerate(result["requirements"])]
            self.progress("Шаг 2 из 4: проверяю, каких сведений не хватает")
            clarify = self.optional(STEP_CLARIFY, {"requirements": [{k: r[k] for k in ("key", "value", "unit")} for r in rows],
                                                   "checklist": CHECKLIST}, CLARIFY_SCHEMA, "step_clarify") or {"questions": []}
            requirements = state["requirements"] = {"key": key, "rows": rows, "summary": result["summary"],
                                                    "clarify": clarify["questions"]}
        confirm_id = "step-req-" + key
        clarify_ids = ["step-clar-" + key + "-" + str(index + 1) for index in range(len(requirements["clarify"]))]
        answered = {qid: questions[qid]["answer"] for qid in [confirm_id] + clarify_ids if questions.get(qid, {}).get("answer")}
        answer_rows, answer_lines = [], []
        for qid, question in zip(clarify_ids, requirements["clarify"]):
            answer = answered.get(qid)
            if answer and answer["value"].strip().lower() not in (UNKNOWN.lower(), "unknown") and answer["message_id"] in session["sources"]:
                answer_rows.append({"id": "req-" + qid, "key": question["field"], "value": answer["value"], "unit": "", "strength": "preference",
                                    "source": [{"id": answer["message_id"], "version": session["sources"][answer["message_id"]]["version"]}]})
                answer_lines.append(question["text"] + " " + answer["value"])
        targets = task["target_object_ids"]
        task["requirements"] = [{"id": r["id"], "key": r["key"], "value": r["value"], "unit": r["unit"] or None, "status": "known",
                                 "strength": r["strength"], "source_refs": r.get("source") or user_refs, "object_ids": targets, "supersedes": None}
                                for r in requirements["rows"] + answer_rows]
        if answered.get(confirm_id, {}).get("value") != CONFIRM:
            pending = [self.question(confirm_id, "confirm_requirements", "Я правильно понял задачу?",
                                     "От требований зависят материал и настройки", [CONFIRM], True)]
            for qid, question in zip(clarify_ids, requirements["clarify"]):
                if qid not in answered:
                    pending.append(self.question(qid, question["field"], question["text"], question["reason"], question["options"], True, True))
            task["questions"] += pending
            lines = ["Я понял задачу так: " + requirements["summary"]]
            lines += ["• {} = {}{}{}".format(r["key"], r["value"], " " + r["unit"] if r["unit"] else "",
                                            " (обязательно)" if r["strength"] == "hard" else "") for r in requirements["rows"]]
            lines.append("\nЕсли всё верно, нажмите «{}». Если нет, напишите, что исправить.".format(CONFIRM) +
                         (" Ответьте также на уточняющие вопросы." if len(pending) > 1 else ""))
            return task, {"summary": "\n".join(lines), "decision_proposals": [], "waiting_step": "requirements"}

        # Step 3: the reference base chooses the material, the model explains, the user agrees.
        records = load_records()
        text = "\n".join([brief] + answer_lines)
        proposed = recommend(text, context)
        record = material_record(records, proposed["material"]["id"])
        need_service, need_tg = required_temperature(text), required_temperature(text, GLASS_TRANSITION)
        alternatives = sorted((r for r in records.values() if r["id"].startswith("mat-") and r["id"] != "mat-contifiber"
                               and (record is None or r["id"] != record["id"]) and fits(r, need_service, need_tg)),
                              key=lambda r: (service_limit(r) or 0))[:2 if record else 3]
        material_key = digest([key, answer_lines, record["id"] if record else None])
        material_id = "step-mat-" + material_key
        options = ([AGREE] if record else []) + [short_title(r) for r in alternatives]
        material = state.get("material")
        if not material or material["key"] != material_key:
            reason = None
            if record:
                self.progress("Шаг 3 из 4: обосновываю выбор материала")
                table = [{"id": r["id"], "title": r["title"], "glass_transition_c": glass_transition(r), "service_limit_c": service_limit(r),
                          "uv_resistance": r.get("limits", {}).get("uv_resistance"), "notes": (r.get("notes") or "")[:200]}
                         for r in [record] + alternatives]
                explained = self.optional(STEP_MATERIAL, {"requirements": [{k: r[k] for k in ("key", "value", "unit")} for r in task["requirements"]],
                                                          "chosen_id": record["id"], "materials": table}, MATERIAL_SCHEMA, "step_material")
                reason = explained["reason"] + "".join("\n⚠ " + risk for risk in explained["risks"]) if explained else None
            material = state["material"] = {"key": material_key, "record_id": record["id"] if record else None, "reason": reason,
                                            "options": {short_title(r): r["id"] for r in alternatives}}
        answer = questions.get(material_id, {}).get("answer")
        chosen = None
        if answer:
            chosen = material["record_id"] if answer["value"] == AGREE else material["options"].get(answer["value"])
        if chosen is None:
            hard = any(r["strength"] == "hard" for r in task["requirements"])
            task["questions"].append(self.question(material_id, "confirm_material", "Согласны с выбором материала?" + (
                " Непроверенные обязательные требования останутся рисками прототипа." if hard else "") +
                " Агент сам выберет этот материал в слайсере.",
                "Материал определяет температуры и прочность детали", options, False))
            if record:
                lines = ["Предлагаю материал {} ({}).".format(short_title(record), proposed["material"]["basis"]),
                         material["reason"] or "\n".join(proposed["warnings"][:2])]
            else:
                lines = ["Материал по задаче определить не удалось. Выберите подходящий по требованиям:"]
            if alternatives:
                lines.append("Также подходят по температуре: " + ", ".join(short_title(r) for r in alternatives) + ".")
            return task, {"summary": "\n".join(l for l in lines if l), "decision_proposals": [], "waiting_step": "material"}

        # Step 4: settings come from the reference base for the confirmed material; the model summarises.
        record = records[chosen]
        if not task.get("risk_acceptance") and any(r["strength"] == "hard" for r in task["requirements"]):
            # The material question names this consequence, so agreeing is the explicit prototype consent.
            task["risk_acceptance"] = {"message_id": answer["message_id"],
                                       "reason": "Материал {} согласован с рисками на шаге выбора материала".format(short_title(record))}
        final = recommend(text, context, record["material_ids"][0], confirmed=True)
        task["recommended_settings"] = final
        extruders = context.get("extruders") or []
        extruder = next((e for e in extruders if known_id(record, (e.get("material") or {}).get("id"))), None)
        if extruder is None:
            message = "Выберите в STE Slicer материал {} (например, {}) и повторите анализ.".format(short_title(record), record["material_ids"][0])
            # Prefer the catalog entry of the record's first-listed material, e.g. Enduse before Generic.
            matches = sorted((record["material_ids"].index(known_id(record, row["id"])), row["id"], row) for row in catalog
                             if row.get("compatibility") == "pass" and known_id(record, row.get("id")))
            if not matches:
                message = "В каталоге принтера нет материала {}. ".format(short_title(record)) + message
            switch = {"material_id": matches[0][2]["id"], "extruder_id": matches[0][2]["extruder_id"]} if matches else None
            return task, {"summary": message, "decision_proposals": [], "waiting_step": "select_material", "switch_material": switch}
        self.progress("Шаг 4 из 4: собираю план настроек")
        summary = self.optional(STEP_SUMMARY, {"requirements": [{k: r[k] for k in ("key", "value", "unit")} for r in task["requirements"]],
                                               "material": record["title"], "orientation": final["orientation"],
                                               "settings": [{k: s[k] for k in ("key", "value", "unit")} for s in final["settings"]],
                                               "warnings": final["warnings"]}, SUMMARY_SCHEMA, "step_summary")
        profile = context["profile"]["custom"]["id"]
        if profile == "empty_quality_changes":
            profile = context["profile"]["quality"]["id"]
        manual = {}
        for source in session["sources"].values():
            if source.get("kind") == "manual":
                manual.setdefault(source["requirement_id"], []).append({"id": source["id"], "version": source["version"]})
        checks = [{"requirement_id": r["id"], "status": "pass" if r["id"] in manual else "unknown", "source_refs": manual.get(r["id"], [])}
                  for r in task["requirements"] if r["strength"] == "hard"]
        candidate = {"id": "step-candidate", "mode": context["mode"]["id"], "material_id": extruder["material"]["id"], "profile_id": profile,
                     "extruder_id": extruder["id"], "orientation_proposal": final["orientation"], "requirement_checks": checks,
                     "missing_fields": [], "evidence_refs": config_refs, "rejection_reasons": [], "assumption_ids": [],
                     "status": "selected", "source_refs": config_refs}
        changes = [{"key": s["key"], "value": s["value"],
                    "target": {"scope": "global"} if s["key"] in GLOBAL_KEYS else {"scope": "extruder", "extruder_id": extruder["id"]}}
                   for s in final["settings"]]
        actions = [{"id": "step-settings", "tool": "set_settings", "arguments": {"changes": changes}, "before": None, "after": None,
                    "reason": "Настройки по справочнику материалов и правилам процесса: " + ", ".join(final["source_ids"]),
                    "source_refs": config_refs}] if changes else []
        limitations = (summary["limitations"] if summary else []) + final["warnings"]
        if final["orientation"]:
            limitations.append("Ориентацию задайте вручную: " + final["orientation"])
        decision = {"id": "step-decision", "candidate_id": candidate["id"], "actions": actions,
                    "limitations": limitations, "source_refs": config_refs}
        task["candidates"], task["decision_proposals"] = [candidate], [decision]
        text = summary["summary"] if summary else "Материал {}. Настройки рассчитаны по справочнику под требования задачи.".format(short_title(record))
        text += ("\n\nДальше: нажмите «Согласовать, применить и рассчитать» в плане печати — настройки применятся "
                 "и слайсер нарежет модель. Затем «Выбрать папку и сохранить» запишет result.gcode и project.3mf.")
        return task, {"summary": text, "decision_proposals": [decision]}

    @staticmethod
    def question(qid, field, text, reason, options, free_text, allow_unknown=False):
        return {"id": qid, "field": field, "text": text, "reason": reason, "affected_decisions": ["material", "settings"],
                "source_refs": [], "options": [{"id": "option-" + str(index + 1), "label": label} for index, label in enumerate(options)],
                "allow_free_text": free_text, "allow_unknown": allow_unknown, "required": True}
