"""Read-only analytics over a minimal snapshot. No Qt, provider calls or raw prompts."""
import csv
import copy
import datetime
import io
import math
from collections import Counter

from .session import now, ApiError

UTC = datetime.timezone.utc
TERMINAL_RUNS = ("completed", "failed", "cancelled", "interrupted")
DIAGNOSTICS = {
    "questions": ("Нужны ответы", "Ответьте на вопросы в задаче."),
    "assumptions": ("Не согласованы допущения", "Примите или отклоните допущения в задаче."),
    "checks": ("Нужна инженерная проверка", "Проверьте обязательные требования и укажите основание."),
    "conflicts": ("Противоречия", "Уточните действующие требования."),
    "unknowns": ("Есть блокирующие неизвестные", "Откройте задачу и уточните недостающие условия."),
    "stale": ("Данные устарели", "Повторите анализ актуальной сцены."),
    "approval": ("План ждёт согласования", "Откройте задачу, проверьте план и согласуйте применение."),
    "failed": ("Операция завершилась ошибкой", "Откройте задачу и проверьте сообщения слайсера и историю действий."),
    "knowledge": ("Пробел базы знаний", "Добавьте проверенные экспертные сведения для этой задачи."),
    "LLM_AUTH_ERROR": ("Ошибка подключения LLM", "Проверьте ключ и модель в настройках подключения."),
    "LLM_TIMEOUT": ("LLM не ответила вовремя", "Проверьте соединение и повторите анализ."),
    "NO_PRINTER": ("Нет выбранного принтера", "Выберите принтер в STE Slicer."),
    "TARGET_CONFIGURATION_UNKNOWN": ("Не подтверждена конфигурация", "Выберите режим и материал в STE Slicer и повторите анализ."),
    "IMPORT_FAILED": ("Импорт не завершён", "Проверьте файл и сообщения STE Slicer."),
    "SLICE_FAILED": ("Ошибка расчёта", "Проверьте сообщения backend и подготовьте новый план."),
    "EXPORT_FAILED": ("Ошибка сохранения", "Проверьте выбранную папку, права и свободное место.")}


def timestamp(value):
    if not isinstance(value, str):
        return None
    value = value.replace("+00:00", "Z")
    for pattern in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.datetime.strptime(value, pattern).replace(tzinfo=UTC).timestamp()
        except ValueError:
            pass
    return None


def number(value):
    try:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0)
    except OverflowError:
        return False


def token_count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10 ** 12 else None


def usage_counts(usage):
    usage = usage if isinstance(usage, dict) else {}
    result = {key: token_count(usage.get(key)) for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
    if result["total_tokens"] is None and result["prompt_tokens"] is not None and result["completion_tokens"] is not None:
        result["total_tokens"] = result["prompt_tokens"] + result["completion_tokens"]
    return result


def percentile(values, fraction):
    """Nearest-rank percentile. Empty samples stay unknown."""
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, int(math.ceil(len(ordered) * fraction)) - 1)], 2)


def csv_cell(value):
    text = str(value) if value is not None else ""
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


def within(value, cutoff, end):
    parsed = timestamp(value)
    return cutoff is None if parsed is None else parsed <= end and (cutoff is None or parsed >= cutoff)


def project_session(session):
    """Project while the store lock is held; skip messages, paths and tool payloads."""
    task = session["task_map"]
    result = {key: session.get(key) for key in ("id", "title", "created_at", "updated_at", "workflow_state", "input_revision")}
    result.update(has_description=bool(session["task"].get("description")),
                  file_count=len(session["files"]), model_count=sum(f.get("kind") == "model" for f in session["files"].values()),
                  imported_models=sum(f.get("kind") == "model" and f.get("scene_present", False) for f in session["files"].values()),
                  unanswered_questions=sum(not q.get("answer") for q in task["questions"]),
                  pending_assumptions=len(set(task["readiness"].get("pending_assumption_ids", [a["id"] for a in task["assumptions"] if a.get("acceptance") == "pending"]))),
                  blocking_unknowns=sum(u.get("blocking", False) and not u.get("resolved") for u in task["unknowns"]),
                  conflicts=sum(not c.get("resolved") for c in task["conflicts"]),
                  readiness=task["readiness"].get("status"),
                  pending_checks=len(set(task["readiness"].get("failed_check_ids", []))))
    result["runs"] = [{key: run.get(key) for key in ("id", "status", "created_at", "finished_at", "input_revision", "error")} for run in session["runs"].values()]
    result["plans"] = {pid: {key: p.get(key) for key in ("id", "status", "input_revision", "snapshot_ref", "applied_snapshot_ref", "approval", "created_at")}
                       for pid, p in session["plans"].items()}
    result["operations"] = [{key: op.get(key) for key in ("id", "kind", "file_id", "status", "plan_id", "created_at", "updated_at", "finished_at", "snapshot_ref", "result")}
                            for op in session["operations"].values() if op.get("kind") == "slice" or op.get("file_id")]
    result["exports"] = [{key: entry.get(key) for key in ("operation_id", "files", "partial", "created_at")} for entry in session["exports"]]
    result["events"] = []
    for event in session["events"]:
        if event["type"] not in ("llm_request", "tool_started", "tool_finished", "tool_failed", "error", "progress"):
            continue
        compact = {key: event.get(key) for key in ("id", "seq", "type", "created_at", "run_id", "operation_id", "tool", "code", "duration_ms", "purpose", "model", "attempt", "http_status", "outcome", "usage")}
        if event.get("tool") == "search_knowledge" and event["type"] == "tool_finished":
            payload = event.get("result", {})
            payload = payload.get("data", payload)
            compact["knowledge_gap"] = any("KNOWLEDGE_GAP" in warning for warning in payload.get("warnings", []))
        result["events"].append(compact)
    return result


class Analytics:
    def __init__(self, store):
        self.store = store

    def snapshot(self, days=30, at=None):
        if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= 365:
            raise ApiError("Период должен быть от 0 до 365 дней")
        generated = at or now()
        end = timestamp(generated)
        if end is None:
            raise ApiError("Неверное время отчёта")
        cutoff = end - days * 86400 if days else None
        with self.store.lock:
            sessions = [copy.deepcopy(project_session(s)) for s in self.store.data["sessions"].values()]
            process_id = self.store.run_id
        report = {"generated_at": generated, "period_days": days, "timezone": "UTC", "sessions": [],
                  "summary": {"tasks": 0, "active_runs": 0, "runs": 0, "completed_runs": 0, "failed_runs": 0,
                              "cancelled_runs": 0, "interrupted_runs": 0, "analysis_completion_rate": None,
                              "ready_results": 0, "saved_bundles": 0, "tasks_needing_attention": 0,
                              "estimated_print_seconds": None, "estimated_material": {}},
                  "llm": {"requests": 0, "retries": 0, "failed_requests": 0, "usage_reported_requests": 0,
                          "prompt_tokens": None, "completion_tokens": None, "total_tokens": None, "p50_ms": None, "p95_ms": None},
                  "runs": {"p50_ms": None, "p95_ms": None, "duration_samples": 0},
                  "tools": [], "errors": [], "attention": [], "timeline": [], "workflow_states": [],
                  "milestones": {"model": 0, "description": 0, "plan": 0, "approval": 0, "slice": 0, "files": 0},
                  "notes": ["Метрики LLM собираются после обновления плагина. В старой истории они могут отсутствовать.",
                            "Время и материал — оценки актуального слайсинга по последнему снимку, а не измерения физической печати.",
                            "Завершённый анализ может ожидать ответа пользователя. Он не означает готовность G-code."]}
        tool_rows, errors, attention, states = {}, Counter(), {}, Counter()
        llm_durations, run_durations, print_times, materials = [], [], [], {}
        timeline_start = datetime.datetime.fromtimestamp(max(cutoff or end - 29 * 86400, end - 30 * 86400), UTC).date()
        today = datetime.datetime.fromtimestamp(end, UTC).date()
        timeline = {}
        date = timeline_start
        while date <= today:
            timeline[str(date)] = {"date": str(date), "runs": 0, "failed_runs": 0, "llm_requests": 0}
            date += datetime.timedelta(days=1)
        def day_bucket(value):
            parsed = timestamp(value)
            return timeline.get(str(datetime.datetime.fromtimestamp(parsed, UTC).date())) if parsed is not None else None
        def issue(key, sid):
            attention.setdefault(key, set()).add(sid)
        for session in sessions:
            runs = [r for r in session["runs"] if within(r.get("finished_at") or r.get("created_at"), cutoff, end)]
            events = [e for e in session["events"] if within(e.get("created_at"), cutoff, end)]
            if not runs and not events and not within(session["updated_at"], cutoff, end):
                continue
            sid = session["id"]
            row = {key: session[key] for key in ("id", "title", "updated_at", "workflow_state", "model_count", "imported_models", "file_count", "unanswered_questions", "pending_assumptions", "blocking_unknowns", "pending_checks", "conflicts", "readiness")}
            row.update(runs=len(runs), failed_runs=0, llm_requests=0, total_tokens=None, ready_result=False,
                       estimated_print_seconds=None, estimated_material={}, saved_bundles=0, issues=[])
            for key, count in (("questions", row["unanswered_questions"]), ("assumptions", row["pending_assumptions"]), ("checks", row["pending_checks"]), ("conflicts", row["conflicts"]), ("unknowns", row["blocking_unknowns"])):
                if count:
                    row["issues"].append(key)
                    issue(key, sid)
            if session["workflow_state"] == "stale":
                row["issues"].append("stale")
                issue("stale", sid)
            if session["workflow_state"] == "plan_ready":
                row["issues"].append("approval")
                issue("approval", sid)
            for run in runs:
                status = run["status"]
                report["summary"]["runs"] += 1
                if status in ("queued", "running"):
                    report["summary"]["active_runs"] += 1
                for state in TERMINAL_RUNS:
                    if status == state:
                        report["summary"][state + "_runs"] += 1
                if status == "failed":
                    row["failed_runs"] += 1
                started, finished = timestamp(run["created_at"]), timestamp(run["finished_at"])
                if status == "completed" and started is not None and finished is not None and finished >= started:
                    run_durations.append((finished - started) * 1000)
                bucket = day_bucket(run["finished_at"] or run["created_at"])
                if bucket:
                    bucket["runs"] += 1
                    bucket["failed_runs"] += status == "failed"
            starts = {e["operation_id"]: e for e in session["events"] if e["type"] == "tool_started" and e.get("operation_id")}
            # Deduplicate attempt events by their event ID, never by identical contents.
            seen_events = set()
            failed_slices = {op["id"] for op in session["operations"] if op["kind"] == "slice" and op["status"] in ("error", "failed")}
            for event in events:
                if event["id"] in seen_events:
                    continue
                seen_events.add(event["id"])
                kind = event["type"]
                if kind == "llm_request":
                    llm = report["llm"]
                    llm["requests"] += 1
                    row["llm_requests"] += 1
                    llm["retries"] += isinstance(event["attempt"], int) and event["attempt"] > 1
                    llm["failed_requests"] += event["outcome"] != "success"
                    usage = usage_counts(event["usage"])
                    for key, value in usage.items():
                        if value is not None:
                            llm[key] = (llm[key] or 0) + value
                    if usage["total_tokens"] is not None:
                        llm["usage_reported_requests"] += 1
                        row["total_tokens"] = (row["total_tokens"] or 0) + usage["total_tokens"]
                    if number(event["duration_ms"]):
                        llm_durations.append(event["duration_ms"])
                    bucket = day_bucket(event["created_at"])
                    if bucket:
                        bucket["llm_requests"] += 1
                if kind in ("tool_finished", "tool_failed") and event["tool"]:
                    tool = tool_rows.setdefault(event["tool"], {"name": event["tool"], "calls": 0, "failures": 0, "durations": []})
                    tool["calls"] += 1
                    tool["failures"] += kind == "tool_failed"
                    start = starts.get(event["operation_id"], {})
                    first, last = timestamp(start.get("created_at")), timestamp(event["created_at"])
                    duration = event["duration_ms"]
                    if not number(duration) and first is not None and last is not None and last >= first:
                        duration = (last - first) * 1000
                    if number(duration):
                        tool["durations"].append(duration)
                if kind == "error" and event.get("code"):
                    errors[event["code"]] += 1
                if kind == "progress" and event["operation_id"] in failed_slices:
                    errors["SLICE_FAILED"] += 1
            latest_error = next((e for e in reversed(session["events"]) if e["type"] == "error" and e.get("code")
                                 or e["type"] == "progress" and e["operation_id"] in failed_slices), None)
            latest_knowledge = next((e for e in reversed(session["events"]) if e["type"] == "tool_finished" and e["tool"] == "search_knowledge"), None)
            if latest_knowledge and latest_knowledge.get("knowledge_gap"):
                issue("knowledge", sid)
                row["issues"].append("knowledge")
            if session["workflow_state"] == "failed":
                code = (latest_error["code"] or "SLICE_FAILED") if latest_error else "failed"
                issue(code, sid)
                row["issues"].append(code)
            valid_slices = []
            for op in session["operations"]:
                if op["kind"] != "slice" or op["status"] != "completed":
                    continue
                plan = session["plans"].get(op["plan_id"], {})
                ref = op["snapshot_ref"] or {}
                if (plan.get("status") == "applied" and plan.get("input_revision") == session["input_revision"]
                        and ref.get("process_id") == process_id and plan.get("approval") and ref == plan.get("applied_snapshot_ref")):
                    valid_slices.append(op)
            if valid_slices:
                op = max(valid_slices, key=lambda o: timestamp(o["created_at"]) or 0)
                row["ready_result"] = True
                result = op["result"] or {}
                duration = result.get("time", {})
                if duration.get("unit") == "s" and number(duration.get("value")):
                    row["estimated_print_seconds"] = duration["value"]
                    print_times.append(duration["value"])
                material = result.get("material", {})
                values, unit = material.get("values"), material.get("unit")
                if isinstance(values, list) and values and all(number(v) for v in values) and unit in ("m", "mm", "g", "kg", "mm3"):
                    row["estimated_material"][unit] = sum(values)
                    materials[unit] = materials.get(unit, 0) + sum(values)
            seen_bundles = set()
            for entry in session["exports"]:
                files = entry["files"] or []
                names = {f.get("filename") for f in files if f.get("written") is True and number(f.get("size")) and f["size"] > 0 and not f.get("stale")}
                if not entry["partial"] and {"project.3mf", "result.gcode"}.issubset(names):
                    seen_bundles.add(entry["operation_id"])
            row["saved_bundles"] = len(seen_bundles)
            report["summary"]["ready_results"] += row["ready_result"]
            report["summary"]["saved_bundles"] += row["saved_bundles"]
            report["summary"]["tasks_needing_attention"] += bool(row["issues"] or row["blocking_unknowns"])
            states[row["workflow_state"]] += 1
            milestones = report["milestones"]
            milestones["model"] += row["model_count"] > 0
            milestones["description"] += session["has_description"]
            milestones["plan"] += bool(session["plans"])
            milestones["approval"] += any(p.get("approval") for p in session["plans"].values())
            milestones["slice"] += any(o["kind"] == "slice" and o["status"] in ("completed", "stale") for o in session["operations"])
            milestones["files"] += bool(seen_bundles)
            report["sessions"].append(row)
        summary = report["summary"]
        summary["tasks"] = len(report["sessions"])
        finished = summary["completed_runs"] + summary["failed_runs"]
        summary["analysis_completion_rate"] = round(100.0 * summary["completed_runs"] / finished, 1) if finished else None
        summary["estimated_print_seconds"] = sum(print_times) if print_times else None
        summary["estimated_material"] = materials
        report["runs"].update(p50_ms=percentile(run_durations, .5), p95_ms=percentile(run_durations, .95), duration_samples=len(run_durations))
        report["llm"].update(p50_ms=percentile(llm_durations, .5), p95_ms=percentile(llm_durations, .95))
        for tool in tool_rows.values():
            samples = tool.pop("durations")
            tool.update(p50_ms=percentile(samples, .5), p95_ms=percentile(samples, .95), duration_samples=len(samples))
        report["tools"] = sorted(tool_rows.values(), key=lambda t: (-(t["p95_ms"] or 0), t["name"]))
        report["errors"] = [{"code": code, "count": count} for code, count in errors.most_common()]
        report["workflow_states"] = [{"state": state, "count": count} for state, count in states.most_common()]
        report["attention"] = [{"code": key, "title": DIAGNOSTICS.get(key, ("Ошибка задачи", "Откройте задачу и проверьте историю действий."))[0],
                                "action": DIAGNOSTICS.get(key, ("", "Откройте задачу и проверьте историю действий."))[1],
                                "session_ids": sorted(ids), "count": len(ids)} for key, ids in sorted(attention.items(), key=lambda item: (-len(item[1]), item[0]))]
        report["timeline"] = list(sorted(timeline.values(), key=lambda d: d["date"]))
        report["sessions"].sort(key=lambda s: (-(timestamp(s["updated_at"]) or 0), s["id"]))
        return report

    def csv(self, report):
        output = io.StringIO(newline="")
        writer = csv.writer(output, delimiter=";")
        writer.writerow(["ID", "Задача", "Состояние", "Обновлено UTC", "Запусков анализа", "Ошибок анализа", "Вопросов без ответа",
                         "Блокирующих неизвестных", "Ожидают проверки", "Попыток LLM", "Токены, сообщённые API", "Актуальный G-code",
                         "Оценка времени, с", "Оценка материала (значение и единица)", "Сохранённых комплектов"])
        for row in report["sessions"]:
            writer.writerow([csv_cell(value) for value in (row["id"], row["title"], row["workflow_state"], row["updated_at"], row["runs"], row["failed_runs"],
                            row["unanswered_questions"], row["blocking_unknowns"], row["pending_checks"], row["llm_requests"], row["total_tokens"],
                            "да" if row["ready_result"] else "нет", row["estimated_print_seconds"],
                            ", ".join("{} {}".format(value, unit) for unit, value in sorted(row["estimated_material"].items())), row["saved_bundles"])])
        return ("\ufeff" + output.getvalue()).encode("utf-8")
