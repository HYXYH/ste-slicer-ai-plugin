"""Conversation-local action groups, with bounded summaries and lazy full details."""
import json
from .user_view import display_message

LABELS = {"get_scene_snapshot": "Прочитана сцена и размеры", "get_printer_capabilities": "Проверены возможности принтера",
          "get_current_configuration": "Прочитаны текущие настройки", "list_materials": "Прочитан каталог материалов",
          "find_compatible_profiles": "Найдены совместимые профили", "search_knowledge": "Выполнен поиск в базе знаний",
          "select_material": "Выбран материал", "set_settings": "Изменены настройки", "start_slicing": "Выполнен расчёт G-code",
          "save_project": "Сохранён проект 3MF", "export_gcode": "Сохранён G-code"}
RUNNING = {"get_scene_snapshot": "Читаю сцену и размеры", "get_printer_capabilities": "Проверяю возможности принтера",
           "get_current_configuration": "Читаю текущие настройки", "list_materials": "Читаю каталог материалов",
           "find_compatible_profiles": "Ищу совместимые профили", "search_knowledge": "Ищу данные в базе знаний",
           "select_material": "Выбираю материал", "set_settings": "Применяю настройки", "start_slicing": "Рассчитываю G-code",
           "save_project": "Сохраняю проект 3MF", "export_gcode": "Сохраняю G-code"}


def brief(value):
    if value is None:
        return ""
    if isinstance(value, dict):
        value = {k: v for k, v in value.items() if not any(word in k.lower() for word in ("token", "secret", "password", "api_key"))}
    rendered = json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
    return rendered if len(rendered) <= 240 else rendered[:237] + "…"


def command_label(tool, arguments):
    arguments = arguments or {}
    rendered = ", ".join(key + "=" + json.dumps(value, ensure_ascii=False) for key, value in arguments.items()
                         if not any(word in key.lower() for word in ("token", "secret", "password", "api_key")))
    if len(rendered) > 240:
        rendered = rendered[:237] + "…"
    return tool + "(" + rendered + ")"


def result_brief(event):
    if event["type"] == "tool_failed":
        return event.get("message") or "Не удалось выполнить действие"
    result = event.get("result") or {}
    data = result.get("data", result.get("result", result)) if isinstance(result, dict) else result
    if isinstance(data, dict):
        if "objects" in data:
            return brief({"моделей": len(data["objects"]), "размеры, мм": [o.get("dimensions_mm") for o in data["objects"][:3]]})
        if "items" in data:
            return "Найдено записей: " + str(len(data["items"])) + (" (есть продолжение)" if data.get("next_cursor") else "")
        if "context" in data:
            context = data["context"]
            return brief({"принтер": (context.get("printer") or {}).get("name"), "режим": (context.get("mode") or {}).get("name"),
                          "профиль": context.get("profile")})
    return brief(data)


def conversation_timeline(session):
    operations = {}
    for index, event in enumerate(session.get("events", [])):
        if event["type"] not in ("tool_started", "tool_finished", "tool_failed") or not event.get("tool"):
            continue
        key = (event.get("run_id"), event.get("operation_id") or event.get("tool_call_id") or event["id"])
        row = operations.setdefault(key, {"id": str(key), "run_id": event.get("run_id"), "created_at": event.get("created_at", ""),
                                          "order": index, "name": LABELS.get(event["tool"], event["tool"]), "tool": event["tool"],
                                          "command": command_label(event["tool"], event.get("arguments")),
                                          "arguments": "", "result": "", "start_event_id": "", "finish_event_id": ""})
        row["status"] = event["type"]
        if event["type"] == "tool_started":
            row.update(arguments=brief(event.get("arguments")), command=command_label(event["tool"], event.get("arguments")), start_event_id=event["id"])
        else:
            row.update(result=result_brief(event), finish_event_id=event["id"], duration_ms=event.get("duration_ms"))
        run = session.get("runs", {}).get(event.get("run_id"), {})
        if row["status"] == "tool_started" and run.get("status") in ("cancelled", "failed", "interrupted", "completed"):
            row["status"] = "interrupted"
    entries = [(m.get("created_at", ""), 1, i, dict(display_message(session, m), type="message")) for i, m in enumerate(session.get("messages", []))]
    entries += [(row["created_at"], 0, row["order"], row) for row in operations.values()]
    entries.sort(key=lambda item: item[:3])
    timeline = []
    for _, _, _, row in entries:
        if row.get("type") == "message":
            timeline.append(row)
        else:
            if not timeline or timeline[-1].get("type") != "activity" or timeline[-1]["run_id"] != row["run_id"]:
                timeline.append({"type": "activity", "id": "activity-" + row["id"], "run_id": row["run_id"], "operations": []})
            timeline[-1]["operations"].append(row)
    for group in timeline:
        if group["type"] != "activity":
            continue
        rows = group["operations"]
        finished = sum(r["status"] == "tool_finished" for r in rows)
        failed = sum(r["status"] == "tool_failed" for r in rows)
        running = next((r for r in rows if r["status"] == "tool_started"), None)
        group["summary"] = RUNNING.get(running["tool"], "Выполняется действие") + "…" if running else "Выполнено действий: " + str(finished)
        if failed:
            group["summary"] += " · ошибок: " + str(failed)
        interrupted = sum(r["status"] == "interrupted" for r in rows)
        if interrupted:
            group["summary"] += " · прервано: " + str(interrupted)
    return timeline
