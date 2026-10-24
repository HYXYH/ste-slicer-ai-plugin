"""Readable labels and compact requirement cards; original keys remain in stored evidence."""
import copy
import re

from .task_data import field_for, known_answer

FIELD_LABELS = {
    "purpose": "Назначение", "part_purpose": "Назначение детали", "part_function": "Назначение детали",
    "application": "Применение", "method": "Способ печати", "printing_method": "Способ печати",
    "material": "Материал", "material_type": "Тип материала", "material_properties": "Свойства материала",
    "material_availability": "Доступные материалы", "available_materials": "Доступные материалы",
    "load": "Нагрузка", "load_type": "Тип нагрузки", "force": "Усилие", "load_force": "Усилие",
    "peak_torque": "Максимальный крутящий момент", "load_torque_peak": "Максимальный крутящий момент",
    "nominal_torque": "Рабочий крутящий момент", "load_torque_nominal": "Рабочий крутящий момент",
    "load_torque": "Крутящий момент", "torque": "Крутящий момент", "fatigue_resistance": "Усталостная прочность",
    "strength": "Прочность", "tensile_strength": "Прочность при растяжении", "stiffness": "Жёсткость",
    "elastic_modulus": "Модуль упругости", "impact_resistance": "Ударная прочность", "wear_resistance": "Износостойкость",
    "temperature": "Рабочая температура", "service_temperature": "Рабочая температура",
    "operating_temperature": "Рабочая температура", "working_temperature": "Рабочая температура",
    "glass_transition": "Температура стеклования", "glass_transition_temperature": "Температура стеклования",
    "environment": "Условия эксплуатации", "humidity": "Влажность", "water_resistance": "Влагостойкость",
    "uv_resistance": "Стойкость к ультрафиолету", "chemical_resistance": "Химическая стойкость",
    "chemical_uv_resistance": "Стойкость к химии и ультрафиолету", "accuracy": "Точность размеров",
    "precision": "Точность размеров", "tolerance": "Допуск размеров", "dimensional_accuracy": "Точность размеров",
    "surface": "Качество поверхности", "surface_quality": "Качество поверхности", "surface_finish": "Обработка поверхности",
    "roughness": "Шероховатость", "supports": "Поддержки", "quantity": "Количество деталей",
    "dimensions": "Размеры", "dimensions_mm": "Размеры", "weight": "Масса", "density": "Плотность",
    "layer_height": "Высота слоя", "wall_line_count": "Количество стенок", "infill_sparse_density": "Заполнение",
    "infill_pattern": "Рисунок заполнения", "speed_print": "Скорость печати", "support_enable": "Поддержки",
    "material_print_temperature": "Температура сопла", "material_bed_temperature": "Температура стола",
    "cool_fan_speed": "Обдув", "top_layers": "Верхние слои", "bottom_layers": "Нижние слои",
}


def field_label(key):
    if key in FIELD_LABELS:
        return FIELD_LABELS[key]
    if re.search("[А-Яа-яЁё]", key):
        return key.replace("_", " ")
    category = field_for(key)
    return FIELD_LABELS.get(category, "Дополнительная характеристика")


def readable_text(value):
    return re.sub(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", lambda match: field_label(match.group()), str(value))


def labels_for_task(task):
    labels = dict(FIELD_LABELS)
    for collection in ("requirements", "constraints", "assumptions", "questions"):
        for row in task.get(collection, []):
            key = row.get("key") or row.get("field")
            if key:
                labels[key] = field_label(key)
    return labels


def display_value(value):
    if value is None:
        return "не указано"
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, list):
        return ", ".join(display_value(item) for item in value)
    if isinstance(value, dict):
        return "; ".join(field_label(key) + ": " + display_value(item) for key, item in value.items())
    return readable_text(value)


def requirement_card(session, snapshot):
    conflicts = [c for c in snapshot.get("conflicts", []) if not c.get("resolved")]
    conflict_ids = {rid for c in conflicts for rid in c.get("record_ids", [])}
    rows = []
    priorities = {"purpose": 0, "material": 1, "load": 2, "temperature": 3, "environment": 4}
    for index, row in enumerate(snapshot.get("rows", [])):
        key = row["key"]
        category = "material" if key in ("material", "material_type") else field_for(key)
        warning = row.get("status") in ("unknown", "conflict", "assumed") or row.get("id") in conflict_ids or not known_answer(row.get("value"))
        value = display_value(row.get("value")) + (" " + row["unit"] if row.get("unit") else "")
        if row.get("status") == "conflict" or row.get("id") in conflict_ids:
            value += " · есть противоречие"
        elif row.get("status") == "assumed":
            value += " · допущение"
        sources = []
        for ref in row.get("source_refs", []):
            source = session.get("sources", {}).get(ref["id"], {})
            file_id = source.get("file_id") or ref["id"]
            attachment = session.get("files", {}).get(file_id, {})
            sources.append("Из ТЗ: " + attachment["name"] if attachment.get("kind") == "document" else "Из сообщения" if source.get("kind") == "user" else "Из карты требований")
        rows.append({"key": key, "label": field_label(key), "value": value, "warning": warning,
                     "source": ", ".join(dict.fromkeys(sources)), "priority": priorities.get(category, 5), "order": index})
    ranked = sorted(rows, key=lambda row: (row["priority"], row["order"]))
    primary = ranked[:5]
    primary += [row for row in ranked[5:] if row["warning"]]
    return {"count": len(rows), "primary": primary, "secondary": [row for row in rows if row not in primary],
            "warnings": [readable_text(c["reason"]) for c in conflicts]}


def display_message(session, message):
    result = copy.deepcopy(message)
    if result.get("role") != "assistant":
        return result
    # Existing chats can acquire the new card without rewriting their stored transcript.
    legacy = re.findall(r"^•\s+([a-z][a-z0-9_]*)\s*=\s*(.+)$", result.get("text", ""), re.M) if result.get("text", "").startswith("Я понял задачу так:") else []
    if legacy and "requirements_card" not in result:
        result["requirements_card"] = {"rows": [{"key": key, "value": value.replace(" (обязательно)", ""), "status": "known"} for key, value in legacy]}
        result["text"] = re.sub(r"^•\s+[a-z][a-z0-9_]*\s*=.*\n?", "", result["text"], flags=re.M).strip()
    result["text"] = readable_text(result.get("text", ""))
    if result.get("requirements_card"):
        result["requirements"] = requirement_card(session, result["requirements_card"])
    return result
