"""Deterministic slicer settings from the reference knowledge base. Works without the LLM."""
import os
import re

from .contracts import strict_json
from ..user_view import field_label

# Order matters: the first material named in the task wins over later mentions.
MATERIAL_WORDS = (
    ("mat-petg", r"petg|петг|пэтг"), ("mat-pla", r"\bpla\b|\bпла\b"), ("mat-pc", r"\bpc\b|поликарбонат"),
    ("mat-abs", r"\babs\b|\bабс\b"), ("mat-pa", r"\bpa ?6\b|\bpa ?12\b|нейлон|полиамид|\bnylon\b"),
    ("mat-tpu", r"\btpu\b|\bтпу\b|термополиуретан"), ("mat-pp", r"\bpp\b|полипропилен"))
LOAD_WORDS = r"нагруз|прочн|кронштейн|шестерн|зубч|н·м|н\*м|нм\b|момент|усили|силов|крепеж|крепёж"
PRECISION_WORDS = r"шестерн|зуб|эвольвент|резьб|точност|посадк"
NO_SUPPORT_WORDS = r"без поддерж"
UV_WORDS = r"\bуф\b|ультрафиолет|солн|\buv\b"
GLASS_TRANSITION = r"(?:стеклован\w*|tg)[^0-9\n]{0,40}?(?:\d{2,3}\s*[–-]\s*)?(\d{2,3})\s*(?:°|º|град)"
SERVICE_TEMPERATURE = r"(?:рабоч\w* температур\w*|температур\w* эксплуатац\w*|до)\s*[≥>=:]*\s*\+?(\d{2,3})\s*(?:°|º|град|c\b|с\b)"


def load_records(directory=None):
    directory = directory or os.path.join(os.path.dirname(os.path.dirname(__file__)), "knowledge")
    records = {}
    for name in ("materials.json", "process_rules.json"):
        path = os.path.join(directory, name)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as source:
                for record in strict_json(source.read()):
                    records[record["id"]] = record
    return records


def middle(value, step=5):
    if isinstance(value, list):
        return int(round((value[0] + value[-1]) / 2.0 / step) * step)
    return value


def known_id(record, container_id):
    """Container ids may carry machine and nozzle suffixes, e.g. enduse_petg_stereotech_ste320_0.4."""
    return next((mid for mid in sorted(record.get("material_ids", []), key=len, reverse=True)
                 if container_id and (container_id == mid or container_id.startswith(mid + "_"))), None)


def material_record(records, material_id):
    for record in records.values():
        if known_id(record, material_id):
            return record
    return None


def choose_material(records, text, preferred_id=None, current_ids=()):
    record = material_record(records, preferred_id)
    if record:
        return record, known_id(record, preferred_id), "выбранный вариант анализа"
    lowered = text.lower()
    hits = [(match.start(), rid) for rid, pattern in MATERIAL_WORDS for match in [re.search(pattern, lowered)] if match]
    if hits:
        rid = min(hits)[1]
        installed = next((known_id(records[rid], mid) for mid in current_ids if known_id(records[rid], mid)), None)
        return records[rid], installed or records[rid]["material_ids"][0], "материал указан в задаче"
    for mid in current_ids:
        record = material_record(records, mid)
        if record:
            return record, known_id(record, mid), "материал, выбранный в слайсере"
    return None, None, None


def required_temperature(text, pattern=SERVICE_TEMPERATURE):
    values = [int(v) for v in re.findall(pattern, text.lower()) if 30 <= int(v) <= 300]
    return max(values) if values else None


def glass_transition(record):
    limits = record.get("limits", {})
    value = limits.get("glass_transition_c", limits.get("glass_transition_dry_c"))
    return value[0] if isinstance(value, list) else value


def fits(record, need_service, need_tg):
    """Unknown properties never pass a stated requirement."""
    if need_service and (service_limit(record) or 0) < need_service:
        return False
    return not need_tg or (glass_transition(record) or 0) >= need_tg


def service_limit(record):
    limits = record.get("limits", {})
    value = limits.get("max_service_temperature_under_load_c")
    if value is None and isinstance(limits.get("hdt_0_45mpa_c"), (int, float)):
        value = limits["hdt_0_45mpa_c"] - 15
    return value


def recommend(text, configuration=None, preferred_material_id=None, directory=None, confirmed=False, experience=None):
    records = load_records(directory)
    configuration = configuration or {}
    extruders = configuration.get("extruders") or []
    current_ids = [(e.get("material") or {}).get("id") for e in extruders if (e.get("material") or {}).get("id")]
    mode = (configuration.get("mode") or {}).get("id")
    record, material_id, basis = choose_material(records, text, preferred_material_id, current_ids)
    if confirmed and record:
        basis = "подтверждён пользователем"
    lowered = text.lower()
    settings, warnings, sources = [], [], []
    need_service, need_tg = required_temperature(text), required_temperature(text, GLASS_TRANSITION)
    if record and not fits(record, need_service, need_tg):
        fitting = sorted((service_limit(r), r["id"]) for r in records.values()
                         if r["id"].startswith("mat-") and r["id"] != "mat-contifiber" and fits(r, need_service, need_tg))
        demands = (["работа при {} °C".format(need_service)] if need_service else []) + (
            ["Tg не ниже {} °C".format(need_tg)] if need_tg else [])
        message = "Требуется {}, а у {} предел под нагрузкой около {} °C, Tg около {} °C.".format(
            " и ".join(demands), record["title"].split(":")[0], service_limit(record), glass_transition(record))
        if fitting and basis not in ("материал указан в задаче", "подтверждён пользователем"):
            # The material was not the user's explicit choice, so switch to the coolest-running one that fits.
            warnings.append(message + " Материал заменён на подходящий по температуре.")
            record = records[fitting[0][1]]
            material_id = next((known_id(record, mid) for mid in current_ids if known_id(record, mid)), record["material_ids"][0])
            basis = "подобран по рабочей температуре"
        else:
            warnings.append(message + (" Подходят по температуре: " + ", ".join(records[rid]["title"].split(":")[0] for _, rid in fitting[:3]) + "."
                                       if fitting else ""))

    def add(key, value, unit, reason, source):
        settings.append({"key": key, "value": value, "unit": unit, "reason": reason, "source": source})
        if source not in sources:
            sources.append(source)

    if record:
        limits = record["limits"]
        nozzle = (limits.get(material_id + "_print_temperature_c") or limits.get(material_id + "_profile_print_temperature_c")
                  or middle(limits.get("print_temperature_c")))
        bed = limits.get(material_id + "_bed_temperature_c")
        if bed is None:
            bed = middle(limits.get("bed_temperature_c"))
        if nozzle:
            add("material_print_temperature", nozzle, "°C", "Температура сопла для " + record["title"].split(":")[0], record["id"])
        if bed is not None:
            add("material_bed_temperature", bed, "°C", "Температура стола по профилю материала", record["id"])
        cooling = limits.get("cooling_percent")
        if cooling is not None:
            # The low end of the range favours layer bonding, which matters more than surface finish here.
            add("cool_fan_speed", cooling[0] if isinstance(cooling, list) else cooling, "%", "Обдув по профилю материала", record["id"])
        if limits.get("uv_resistance") == "low" and re.search(UV_WORDS, lowered):
            warnings.append("Материал слабо стоек к УФ: окрасить деталь или защитить от прямого солнца.")
        if "drying" in limits:
            warnings.append("Перед печатью высушить пруток: " + limits["drying"] + ".")
    else:
        warnings.append("Материал не определён: укажите его в задаче или выберите в слайсере.")

    load_bearing = re.search(LOAD_WORDS, lowered) is not None
    precise = re.search(PRECISION_WORDS, lowered) is not None
    gear = re.search(r"шестерн|зубч|зуб", lowered) is not None
    if load_bearing:
        add("wall_line_count", 6 if gear else 5, "шт", "Силовая деталь: прочность дают периметры", "proc-load-bearing")
        add("top_layers", 6, "шт", "Плотные верх и низ для силовой детали", "proc-load-bearing")
        add("bottom_layers", 6, "шт", "Плотные верх и низ для силовой детали", "proc-load-bearing")
        add("infill_sparse_density", 60 if gear else 50, "%", "Заполнение силовой детали", "proc-load-bearing")
        add("infill_pattern", "gyroid", None, "Равнопрочное заполнение во всех направлениях", "proc-load-bearing")
    else:
        add("wall_line_count", 3, "шт", "Ненагруженная деталь", "proc-load-bearing")
        add("infill_sparse_density", 20, "%", "Ненагруженная деталь", "proc-load-bearing")
    add("layer_height", 0.15 if precise else 0.2, "мм",
        "Точный профиль (зубья, посадки)" if precise else "Баланс скорости и прочности для сопла 0,4 мм", "proc-layer-quality")
    if re.search(NO_SUPPORT_WORDS, lowered):
        add("support_enable", False, None, "По условию задачи — без поддержек; нависания не более 45°", "proc-no-supports")
    orientation = None
    if load_bearing:
        orientation = ("Ось шестерни вертикально (по Z), зубья в плоскости слоя" if gear else
                       "Основную нагрузку направить вдоль слоёв: плечом кронштейна на стол")
        sources.append("proc-orientation-anisotropy")
    if mode in ("cylindrical", "cylindrical_full"):
        warnings.append("Режим 5D: часть настроек имеет отдельные ключи *_cylindrical, проверьте их в слайсере.")
    if load_bearing:
        warnings.append("Допустимая нагрузка готовой детали неизвестна: свойства пластика из справочника не определяют прочность конкретной печати.")
    result = {"material": {"id": material_id, "title": record["title"] if record else None, "basis": basis},
            "mode": mode, "orientation": orientation, "settings": settings, "warnings": warnings, "source_ids": sources}
    return experience(result) if experience else result


def describe(result):
    lines = []
    material = result["material"]
    if material["id"]:
        lines.append("Материал: {} ({}; {}).".format(material["id"], material["title"], material["basis"]))
    if result["orientation"]:
        lines.append("Ориентация: " + result["orientation"] + ".")
    for row in result["settings"]:
        value = {True: "вкл", False: "выкл"}.get(row["value"], row["value"]) if isinstance(row["value"], bool) else row["value"]
        lines.append("• {}: {}{} — {}".format(field_label(row["key"]), value, " " + row["unit"] if row["unit"] else "", row["reason"]))
    lines.extend("⚠ " + w for w in result["warnings"])
    lines.append("Источники: " + ", ".join(result["source_ids"]))
    return "\n".join(lines)
