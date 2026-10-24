"""Safe rich text for the native Qt window. No JavaScript or web engine."""
import html
import json


FLOW = {"idle": "Готов к работе", "queued": "Анализ в очереди", "waiting_import": "Ожидание импорта",
        "analyzing": "Анализ задачи…", "waiting_user": "Нужны уточнения", "plan_ready": "План готов к согласованию",
        "applying": "Применение плана…", "slicing": "Расчёт G-code…", "result_ready": "Результат готов",
        "exporting": "Сохранение файлов…", "completed": "Файлы сохранены", "failed": "Операция остановлена с ошибкой",
        "cancelled": "Анализ остановлен", "interrupted": "Работа прервана перезапуском", "stale": "Данные устарели"}
TOOLS = {"get_scene_snapshot": "Сцена и размеры", "get_printer_capabilities": "Возможности принтера",
         "get_current_configuration": "Текущие настройки", "list_materials": "Каталог материалов",
         "find_compatible_profiles": "Совместимые профили", "search_knowledge": "Экспертная база",
         "select_material": "Выбор материала", "set_settings": "Изменение настроек", "start_slicing": "Расчёт G-code",
         "save_project": "Сохранение 3MF", "export_gcode": "Сохранение G-code"}
FILE_STATUS = {"saved": "Сохранён", "queued": "В очереди", "importing": "Импортируется…", "imported": "В сцене",
               "error": "Ошибка", "interrupted": "Импорт прерван"}
MODES = {"classic": "3D", "cylindrical": "5D Spiral", "cylindrical_full": "5D"}
SETTINGS = {"material_print_temperature": ("Температура сопла", "°C"), "material_bed_temperature": ("Температура стола", "°C"),
            "cool_fan_speed": ("Обдув", "%"), "wall_line_count": ("Стенки", "линий"), "top_layers": ("Верх", "слоёв"),
            "bottom_layers": ("Низ", "слоёв"), "infill_sparse_density": ("Заполнение", "%"), "infill_pattern": ("Рисунок заполнения", ""),
            "layer_height": ("Высота слоя", "мм"), "speed_print": ("Скорость", "мм/с"), "support_enable": ("Поддержки", "")}


def setting_changes(action):
    def identity(row):
        target = row.get("target") or {}
        return row["key"], target.get("scope"), target.get("extruder_id")
    previous = action.get("before") or []
    before = {identity(row): row.get("value") for row in previous if isinstance(row, dict) and "key" in row}
    rows = []
    for row in action.get("arguments", {}).get("changes", []):
        label, unit = SETTINGS.get(row["key"], (row["key"], ""))
        old, new = before.get(identity(row)), row["value"]
        def display(value):
            if value is None:
                return "—"
            value = round(value, 4) if isinstance(value, float) else value
            rendered = {"gyroid": "Гироид", "grid": "Сетка", "lines": "Линии"}.get(str(value), human(value))
            return rendered + (" " + unit if unit else "")
        rows.append([text(label), text(display(old)), text(display(new))])
    return table(["Параметр", "Сейчас", "В плане"], rows)


def text(value):
    return html.escape(str(value), quote=True).replace("\n", "<br>")


def human(value):
    if value is None:
        return "не определено"
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, list):
        return ", ".join(human(v) for v in value)
    if isinstance(value, dict):
        return "; ".join(str(k) + ": " + human(v) for k, v in sorted(value.items()))
    return str(value)


def paragraph(value, muted=False):
    return '<p style="color:{};margin-top:8px;margin-bottom:8px">{}</p>'.format("#73839a" if muted else "#263b57", text(value))


def card(title, body, color="#ffffff"):
    return ('<table width="100%" cellspacing="0" cellpadding="18" bgcolor="{}"><tr><td>'
            '<b style="font-size:15px;color:#192f4d">{}</b>{}</td></tr></table><p style="margin:6px"></p>').format(color, text(title), body)


def table(headers, rows):
    return '<table width="100%" cellpadding="9" cellspacing="1" bgcolor="#e7edf5">' + (
        '<tr>' + ''.join('<th bgcolor="#edf3fa" align="left">' + text(h) + '</th>' for h in headers) + '</tr>' if headers else '') + ''.join(
        '<tr>' + ''.join('<td bgcolor="#ffffff">' + cell + '</td>' for cell in row) + '</tr>' for row in rows) + '</table>'


class Document:
    def __init__(self):
        self.actions = {}
        self.parts = []

    def link(self, label, kind, **values):
        key = str(len(self.actions))
        self.actions[key] = dict(values, kind=kind)
        return '<p><a href="assistant:{}" style="color:#245fca;text-decoration:none"><b>{} →</b></a></p>'.format(key, text(label))

    def finish(self):
        return '<html><body style="font-family:Segoe UI;color:#263b57;font-size:13px">' + ''.join(self.parts) + '</body></html>', self.actions


def session_document(session, history=False, include_messages=True):
    doc = Document()
    sid = session["id"]
    if include_messages and not session["messages"]:
        doc.parts.append(card("Начните с детали и её задачи", paragraph("Прикрепите 3D-модель и опишите, для чего нужна деталь.") +
                              paragraph("Ассистент уточнит требования, подготовит план и поможет сохранить проект и G-code.", True), "#eaf1ff"))
    for message in session["messages"] if include_messages else []:
        role = {"user": "Вы", "assistant": "Ассистент", "system": "Система"}.get(message["role"], "Сообщение")
        body = paragraph(message.get("text") or "")
        for fid in message.get("file_ids", []):
            f = session["files"].get(fid)
            if not f:
                continue
            body += paragraph("Вложение: " + f["name"] + " · " + str(round(f["size"] / 1048576, 2)) + " МиБ · " + FILE_STATUS.get(f["status"], f["status"]))
            if f.get("error"):
                body += paragraph(f["error"])
            if f.get("kind") == "image":
                body += doc.link("Посмотреть изображение", "image", file_id=fid)
            elif f["status"] in ("saved", "error", "interrupted"):
                body += doc.link("Загрузить в сцену", "import", file_id=fid)
        doc.parts.append(card(role, body, "#edf3ff" if message["role"] == "user" else "#ffffff"))
    task = session.get("task_map") or {}
    runs = session.get("runs") or {}
    latest = max(runs.values(), key=lambda r: r.get("created_at", "")) if runs else None
    events = session.get("events", [])
    current_events = [e for e in events if not latest or e["run_id"] == latest["id"] or
                      e["run_id"] not in runs and e["created_at"] >= latest["created_at"]]
    if latest and latest["status"] in ("queued", "running"):
        progress = next((e for e in reversed(current_events) if e["type"] in ("progress", "tool_started")), {})
        doc.parts.append(card("Ассистент работает", paragraph(progress.get("message") or TOOLS.get(progress.get("tool"), "Ожидание ответа LLM…")), "#eaf1ff"))
    if session.get("workflow_state") in ("failed", "cancelled", "interrupted"):
        error = next((e for e in reversed(current_events) if e["type"] == "error"), None)
        if error:
            doc.parts.append(card("Работа остановлена", paragraph(error["message"]), "#fff1e9"))
    rec = task.get("recommended_settings") or {}
    if rec.get("settings") and (include_messages or history):
        rows = [[text(s["key"]), text(human(s["value"]) + (" " + s["unit"] if s.get("unit") else "")), text(s.get("reason", ""))] for s in rec["settings"]]
        doc.parts.append(card("Настройки слайсера под задачу", paragraph("Материал: " + human(rec.get("material"))) +
                              table(["Параметр", "Значение", "Основание"], rows) + ''.join(paragraph(w) for w in rec.get("warnings", [])) +
                              paragraph("Источники: " + ", ".join(rec.get("source_ids", [])), True)))
    known = [paragraph(r["key"] + ": " + human(r["value"]) + (" " + r["unit"] if r.get("unit") else "")) for r in task.get("requirements", []) + task.get("constraints", [])]
    for fact in task.get("facts", []):
        if fact.get("key") == "scene":
            objects = fact["value"].get("objects", [])
            known += [paragraph(o["name"] + " · " + human(o.get("dimensions_mm")) + " мм") for o in objects]
            foreign = [o for o in objects if o.get("printable") and o["id"] not in task["target_object_ids"]]
            if foreign:
                doc.parts.append(card("Другие модели в общей сцене", paragraph(", ".join(o["name"] for o in foreign)) +
                                      doc.link("Включить в задачу", "answer", payload={"include_object_ids": [o["id"] for o in foreign]})))
    if known and (include_messages or history):
        doc.parts.append(card("Известно", ''.join(known)))
    for q in [q for q in task.get("questions", []) if not q.get("answer")][:3]:
        body = paragraph(q.get("reason", ""), True)
        for option in q.get("options", []):
            body += doc.link(option["label"], "answer", payload={"answers": [{"question_id": q["id"], "value": option["label"]}]})
        if q.get("allow_unknown"):
            body += doc.link("Не знаю", "answer", payload={"answers": [{"question_id": q["id"], "value": "Не знаю"}]})
        if q.get("allow_free_text"):
            body += doc.link("Написать ответ…", "input", title=q["text"], field="answers", row={"question_id": q["id"]}, value_key="value")
        doc.parts.append(card(q["text"], body, "#f0f5ff"))
    for a in task.get("assumptions", []):
        if a.get("acceptance") == "pending":
            doc.parts.append(card("Допущение: " + a["key"], paragraph(human(a["value"]) + ". " + a.get("reason", "")) +
                                  doc.link("Принять", "answer", payload={"assumptions": [{"id": a["id"], "accepted": True}]}) +
                                  doc.link("Отклонить", "answer", payload={"assumptions": [{"id": a["id"], "accepted": False}]})))
    ready = task.get("readiness") or {}
    limits = ready.get("reasons", []) + [u["reason"] for u in task.get("unknowns", []) if u.get("blocking") and not u.get("resolved")]
    if limits:
        doc.parts.append(card("Ограничения", ''.join(paragraph(v) for v in limits), "#fff8e9"))
    for c in task.get("conflicts", []):
        if not c.get("resolved"):
            doc.parts.append(card("Противоречие", paragraph(c["reason"]) + doc.link("Уточнить действующее требование…", "input", title="Разрешение противоречия",
                                  field="conflict_resolutions", row={"id": c["id"]}, value_key="reason")))
    failed = ready.get("failed_check_ids", [])
    if failed:
        body = paragraph("Укажите метод, источник и результат фактической проверки. Принятие риска прототипа не подтверждает эксплуатационные свойства.")
        for r in task.get("requirements", []) + task.get("constraints", []):
            if r["id"] in failed:
                body += doc.link("Подтвердить проверку: " + r["key"], "input", title="Метод, источник и результат проверки",
                                 field="manual_checks", row={"requirement_id": r["id"]}, value_key="reason")
        body += doc.link("Печатать как прототип — принять риск…", "risk")
        doc.parts.append(card("Ручная инженерная проверка", body, "#fff8e9"))
    if ready.get("accepted_risk_ids"):
        doc.parts.append(card("Режим прототипа", paragraph("Риск принят. Непроверенные требования остаются ограничениями; эксплуатация требует испытаний."), "#fff8e9"))
    for plan in session.get("plans", {}).values():
        choice = plan["selected"]
        body = paragraph("Режим: " + MODES.get(choice["mode"], choice["mode"]) + " · материал: " + choice["material_id"] + " · профиль: " + human(choice["profile_id"]))
        if plan["status"] == "stale":
            body += paragraph("План устарел. Повторите анализ.")
        for action in plan["actions"]:
            body += paragraph(TOOLS.get(action["tool"], action["tool"]))
            body += setting_changes(action) if action["tool"] == "set_settings" else paragraph(human(action.get("before")) + " → " + human(action.get("after")))
            body += paragraph(action["reason"], True)
        if not plan["actions"]:
            body += paragraph("Сохранить текущие настройки профиля и рассчитать G-code.")
        body += ''.join(paragraph(v) for v in plan.get("limitations", []))
        if plan["status"] in ("validated", "approved"):
            body += doc.link("Согласовать, применить и рассчитать", "approve", plan_id=plan["id"], version=plan["version"])
        doc.parts.append(card("План печати · версия " + str(plan["version"]), body, "#edf7f3"))
    for op in session.get("operations", {}).values():
        if op.get("kind") != "slice":
            continue
        body = paragraph("Состояние: " + op["status"]) + paragraph(human(op.get("result")))
        if op.get("error"):
            body += paragraph(op["error"])
        if op["status"] == "completed" and session.get("workflow_state") != "stale":
            body += doc.link("Выбрать папку и сохранить 3MF + G-code", "export", operation_id=op["id"])
        doc.parts.append(card("Слайсинг", body))
    for result in session.get("exports", []):
        doc.parts.append(card("Сохранено частично" if result.get("partial") else "Сохранённые файлы", ''.join(paragraph(f["filename"] + " · " + str(f["size"]) + " байт") for f in result["files"])))
    doc.parts.append(doc.link("Скрыть историю действий" if history else "Показать историю действий", "history"))
    if history:
        for title, group in (("Текущий запуск", current_events), ("Предыдущие запуски", [e for e in events if e not in current_events])):
            body = ''
            for e in group:
                if e["type"] in ("tool_started", "tool_finished", "tool_failed", "error", "model_output_rejected"):
                    label = TOOLS.get(e.get("tool"), e.get("tool")) or e["type"]
                    body += paragraph(label + " · " + e["type"] + (": " + e["message"] if e.get("message") else ""))
                    if e.get("result") or e.get("arguments"):
                        body += '<p style="font-family:Consolas;font-size:11px">' + text(json.dumps({k: e[k] for k in ("arguments", "result") if k in e}, ensure_ascii=False, indent=2)) + '</p>'
                    elif e.get("has_details"):
                        body += doc.link("Подробности действия", "event", event_id=e["id"])
            if body:
                doc.parts.append(card(title, body))
    for action in doc.actions.values():
        action["session_id"] = sid
    return doc.finish()


def analytics_document(report, search="", filter_value="all"):
    doc = Document()
    s, llm = report["summary"], report["llm"]
    metrics = [("Задачи", s["tasks"]), ("Анализы", s["runs"]), ("Актуальный G-code", s["ready_results"]), ("Комплекты файлов", s["saved_bundles"])]
    doc.parts.append(table([], [['<span style="color:#73839a">' + text(label) + '</span><br><span style="font-size:30px;color:#1b4786"><b>' + text(value) + '</b></span>' for label, value in metrics]]))
    timeline = [d for d in report.get("timeline", []) if d["runs"]][-7:]
    maximum = max([d["runs"] for d in timeline] or [1]) or 1
    bars = []
    for d in timeline:
        width = max(1, int(260 * d["runs"] / maximum))
        bars.append([text(d["date"]), '<table cellpadding="0" cellspacing="0"><tr><td width="{}" bgcolor="#557fcd">&nbsp;</td></tr></table>'.format(width), text(str(d["runs"]) + " / ошибок " + str(d["failed_runs"]))])
    doc.parts.append(card("Активность по дням", (table(["Дата", "Запуски", "Всего / ошибки"], bars) +
                          paragraph("Последние 7 активных дней. В сводке и CSV учтён весь выбранный период.", True)) if bars else paragraph("Анализы пока не запускались.")))
    for issue in report.get("attention", []):
        doc.parts.append(card(issue["title"] + " · задач: " + str(issue["count"]), paragraph(issue["action"]), "#fff8e9"))
    def latency(value):
        return "нет данных" if value is None else str(round(value / 1000, 1)) + " с"
    rows = [("HTTP-попытки / повторные", str(llm["requests"]) + " / " + str(llm["retries"])),
            ("Учтённые токены", human(llm["total_tokens"])), ("Полнота usage", str(llm["usage_reported_requests"]) + " из " + str(llm["requests"])),
            ("LLM: медиана / p95", latency(llm["p50_ms"]) + " / " + latency(llm["p95_ms"])),
            ("Анализ: медиана / p95", latency(report["runs"]["p50_ms"]) + " / " + latency(report["runs"]["p95_ms"])),
            ("Оценка печати", human(s["estimated_print_seconds"]) + " с"), ("Материал", human(s["estimated_material"]))]
    doc.parts.append(card("LLM и оценки актуальных результатов", table(["Показатель", "Значение"], [[text(k), text(v)] for k, v in rows])))
    if report.get("tools"):
        doc.parts.append(card("Работа инструментов", table(["Инструмент", "Вызовы", "Ошибки", "p95"], [[text(TOOLS.get(t["name"], t["name"])), text(t["calls"]), text(t["failures"]), text(latency(t["p95_ms"]))] for t in report["tools"]])))
    tasks = []
    for row in report["sessions"]:
        if search.casefold() not in row["title"].casefold():
            continue
        if filter_value == "attention" and not (row.get("issues") or row.get("blocking_unknowns")) or filter_value == "ready" and not row["ready_result"] or filter_value == "saved" and not row["saved_bundles"]:
            continue
        tasks.append([doc.link(row["title"], "session", session_id=row["id"]), text(FLOW.get(row["workflow_state"], row["workflow_state"])), text(str(row["runs"]) + " / " + str(row["failed_runs"])), text(human(row["total_tokens"])), text(row["saved_bundles"])])
    doc.parts.append(card("Задачи", table(["Задача", "Состояние", "Анализы / ошибки", "Токены", "Комплекты"], tasks) if tasks else paragraph("Нет задач по выбранным условиям.")))
    doc.parts.append(''.join(paragraph(n, True) for n in report.get("notes", [])))
    return doc.finish()
