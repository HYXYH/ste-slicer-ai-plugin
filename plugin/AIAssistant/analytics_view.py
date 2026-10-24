"""Display-only analytics projection; all counts and estimates come from Analytics."""
from .native_view import FLOW, TOOLS


def value_text(value, unit=""):
    if value is None:
        return "Нет данных"
    if isinstance(value, dict):
        units = {"m": "м", "mm": "мм", "g": "г", "kg": "кг", "mm3": "мм³"}
        return "; ".join(value_text(amount, units.get(key, key)) for key, amount in value.items()) or "Нет данных"
    text = "{:,}".format(value).replace(",", " ") if isinstance(value, int) else str(round(value, 2)) if isinstance(value, float) else str(value)
    return text + (" " + unit if unit else "")


def latency(value):
    return value_text(round(value / 1000, 1), "с") if value is not None else "Нет данных"


def analytics_projection(report, search="", filter_value="all", issue_code=""):
    if not report:
        return {}
    summary, llm = report["summary"], report["llm"]
    metrics = [{"title": title, "value": value_text(summary[key]), "icon": icon, "color": color, "background": background}
               for title, key, icon, color, background in (
                   ("Задачи", "tasks", "Tasks.svg", "#123e79", "#e0f0ff"),
                   ("Анализы", "runs", "Runs.svg", "#123e79", "#dcf9ed"),
                   ("Актуальный G-code", "ready_results", "Cube.svg", "#6034ed", "#ece4ff"),
                   ("Комплекты файлов", "saved_bundles", "Files.svg", "#e67b13", "#fff0d7"))]
    days = [day for day in report.get("timeline", []) if day["runs"]][-7:]
    maximum = max([day["runs"] for day in days] or [1])
    activity = [{"cells": [day["date"], "", "{} / ошибок {}".format(day["runs"], day["failed_runs"])],
                 "fraction": day["runs"] / maximum} for day in days]
    llm_rows = [{"cells": [label, value], "tip": "Медиана — время среднего по порядку измерения; p95 — время, не превышенное в 95% измеренных операций." if "p95" in label else ""} for label, value in (
        ("Запросы к модели / повторные", "{} / {}".format(llm["requests"], llm["retries"])),
        ("Учтённые токены", value_text(llm["total_tokens"])),
        ("Запросы с данными о токенах", "{} из {}".format(llm["usage_reported_requests"], llm["requests"])),
        ("Ответ модели: медиана / p95", latency(llm["p50_ms"]) + " / " + latency(llm["p95_ms"])),
        ("Анализ задачи: медиана / p95", latency(report["runs"]["p50_ms"]) + " / " + latency(report["runs"]["p95_ms"])),
        ("Расчётное время печати", value_text(summary["estimated_print_seconds"], "с")),
        ("Расчётный расход материала", value_text(summary["estimated_material"]))) ]
    tools = [{"cells": [TOOLS.get(row["name"], row["name"]), str(row["calls"]), str(row["failures"]), latency(row["p95_ms"])],
              "warning": bool(row["failures"])} for row in report.get("tools", [])]
    tasks = []
    for row in report["sessions"]:
        if search.casefold().strip() not in row["title"].casefold():
            continue
        if (filter_value == "attention" and not (row.get("issues") or row.get("blocking_unknowns"))
                or filter_value == "ready" and not row["ready_result"]
                or filter_value == "saved" and not row["saved_bundles"]
                or issue_code and issue_code not in row.get("issues", [])):
            continue
        tasks.append({"id": row["id"], "cells": [row["title"], FLOW.get(row["workflow_state"], row["workflow_state"]),
                                                "{} / {}".format(row["runs"], row["failed_runs"]), value_text(row["total_tokens"]), str(row["saved_bundles"])]})
    return {"metrics": metrics, "activity": activity, "attention": report.get("attention", []),
            "llmRows": llm_rows, "toolRows": tools, "taskRows": tasks, "notes": report.get("notes", [])}
