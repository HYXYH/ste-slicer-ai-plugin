"""Print prices use captured slice estimates and user tariffs, never bounding-box guesses."""
import copy
import math

from .experience import conditions
from .session import ApiError


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def indexed(values, position):
    if isinstance(values, list) and position < len(values):
        return values[position]
    if isinstance(values, dict):
        return values.get(str(position), values.get(position))
    return None


def slice_mass(operation):
    result = operation.get("result") or {}
    direct = result.get("material_mass") or {}
    lengths = result.get("material") or {}
    snapshot = operation.get("print_snapshot") or {}
    extruders = snapshot.get("context", {}).get("extruders", [])
    rows = []
    for extruder in extruders:
        try:
            position = int(extruder.get("position", "0" if len(extruders) == 1 else "-1"))
        except (TypeError, ValueError):
            return None
        if position < 0:
            return None
        weight = indexed(direct.get("values"), position) if direct.get("unit") == "g" else None
        source = "slicer"
        if not number(weight):
            length = indexed(lengths.get("values"), position) if lengths.get("unit") == "m" else None
            values = next((s["values"] for s in snapshot.get("settings", []) if s["target"] == {"scope": "extruder", "extruder_id": extruder["id"]}), {})
            diameter, density = values.get("material_diameter"), values.get("material_density")
            if number(length) and length == 0:
                weight = 0.
            elif number(length) and number(diameter) and diameter > 0 and number(density) and density > 0:
                # metres * circular area in mm² = cm³; density is captured in g/cm³.
                weight = length * math.pi * (diameter / 2.) ** 2 * density
                source = "captured_length_diameter_density"
            else:
                return None
        material = extruder.get("material") or {}
        rows.append({"extruder_id": extruder["id"], "material_id": material.get("id"), "grams": weight, "position": position, "source": source})
    if not rows:
        return None
    # Do not silently omit a consumed spool that is absent from the captured configuration.
    provided = direct.get("values") if direct.get("unit") == "g" else lengths.get("values") if lengths.get("unit") == "m" else None
    used = {r["position"] for r in rows}
    if isinstance(provided, list) and any(number(v) and v > 0 and i not in used for i, v in enumerate(provided)):
        return None
    return rows


def estimate(operation, tariffs):
    if not operation:
        return {"available": False, "cost": None, "grams": None, "hours": None, "reason": "Появится после нарезки"}
    snapshot = operation.get("print_snapshot") or {}
    context = snapshot.get("context", {})
    timing = (operation.get("result") or {}).get("time") or {}
    seconds = timing.get("value") if timing.get("unit") == "s" and number(timing.get("value")) else None
    masses = slice_mass(operation)
    printer_id = (context.get("printer") or {}).get("id")
    hour_price = tariffs.get("printers", {}).get(printer_id)
    hours = seconds / 3600. if seconds is not None else None
    grams = sum(r["grams"] for r in masses) if masses is not None else None
    material_cost = 0.
    if masses is not None:
        for row in masses:
            price = tariffs.get("materials", {}).get(row["material_id"])
            if row["grams"] and not number(price):
                material_cost = None
                break
            material_cost += row["grams"] * (price or 0.)
    else:
        material_cost = None
    time_cost = hours * hour_price if hours is not None and number(hour_price) else None
    total = material_cost + time_cost if material_cost is not None and time_cost is not None else None
    reason = "По прогнозу слайсера" if total is not None else "Масса пластика не сохранена; нужна новая нарезка" if grams is None else "Время печати не определено" if hours is None else "Укажите тарифы в настройках"
    return {"available": True, "operation_id": operation["id"], "cost": round(total, 2) if total is not None else None,
            "grams": round(grams, 2) if grams is not None else None, "hours": round(hours, 4) if hours is not None else None,
            "material_cost": round(material_cost, 2) if material_cost is not None else None,
            "time_cost": round(time_cost, 2) if time_cost is not None else None, "reason": reason}


class PrintPricing:
    def __init__(self, store):
        self.store = store

    def tariffs(self):
        with self.store.lock:
            return copy.deepcopy(self.store.data.get("print_tariffs", {"materials": {}, "printers": {}}))

    def save(self, snapshot, body):
        context = snapshot.get("context", {})
        printer = (context.get("printer") or {}).get("id")
        materials = {(e.get("material") or {}).get("id") for e in context.get("extruders", [])}
        if (not printer or body.get("printer_id") != printer or set(body) - {"printer_id", "printer_hour", "material_prices"}
                or "printer_hour" not in body or not isinstance(body.get("material_prices"), dict) or set(body["material_prices"]) - materials):
            raise ApiError("Материал или принтер изменились. Откройте настройки заново", 409)
        prices = list(body["material_prices"].values()) + [body["printer_hour"]]
        if any(v is not None and (not number(v) or v > 1000000000) for v in prices):
            raise ApiError("Тарифы: неотрицательные числа в рублях; неизвестные оставьте пустыми")
        with self.store.lock:
            tariffs = self.store.data.setdefault("print_tariffs", {"materials": {}, "printers": {}})
            for category, key, value in [("printers", printer, body["printer_hour"])] + [("materials", k, v) for k, v in body["material_prices"].items()]:
                if value is None:
                    tariffs[category].pop(key, None)
                else:
                    tariffs[category][key] = value
            self.store._save()

    def view(self, session, snapshot, test=None):
        tariffs = self.tariffs()
        context = snapshot.get("context", {})
        printer = context.get("printer") or {}
        materials = {}
        for extruder in context.get("extruders", []):
            material = extruder.get("material") or {}
            if material.get("id"):
                materials[material["id"]] = {"id": material["id"], "title": material.get("name") or material["id"], "price": tariffs["materials"].get(material["id"])}
        run = session.get("intelligent_calibration", {})
        decision = session.get("calibration_decision", {})
        main_id = run.get("main_plan_id") if run.get("state") in ("preparing", "test_ready", "awaiting_print", "review") else decision.get("plan_id")
        slices = sorted([o for o in session["operations"].values() if o.get("kind") == "slice" and o.get("status") == "completed" and o.get("result") and o.get("print_snapshot")], key=lambda o: o.get("created_at", ""), reverse=True)
        main = next((o for o in slices if o.get("plan_id") == main_id and conditions(o["print_snapshot"]) == conditions(snapshot) and conditions(snapshot) is not None), None)
        def compatible_test(operation):
            a, b = conditions(operation["print_snapshot"]), conditions(snapshot)
            if not a or not b:
                return False
            excluded = set((test or {}).get("required_settings", {}))
            for value in (a, b):
                value.pop("objects", None)
                for row in value["settings"]:
                    row["values"] = {k: v for k, v in row["values"].items() if k not in excluded}
            return a == b
        trial = next((o for o in slices if test and run.get("test", {}).get("id") == test["id"] and o.get("plan_id") in run.get("plan_ids", []) and compatible_test(o)), None)
        main_estimate, test_estimate = estimate(main, tariffs), estimate(trial, tariffs)
        costs = {}
        if main_estimate["cost"] is not None:
            costs["main_loss"] = main_estimate["cost"]
        if test_estimate["cost"] is not None:
            costs["test_cost"] = test_estimate["cost"]
        return {"printer_id": printer.get("id"), "printer_title": printer.get("name") or printer.get("id") or "Выберите принтер",
                "printer_hour": tariffs["printers"].get(printer.get("id")), "materials": sorted(materials.values(), key=lambda m: m["title"]),
                "main": main_estimate, "test": test_estimate, "costs": costs, "currency": "RUB"}
