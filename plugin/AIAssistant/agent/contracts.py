"""Small, explicit JSON Schema subset used by every public tool."""
import json
import math

from ..session import ApiError


def fail(message, code="INVALID_ARGUMENTS", status=400):
    raise ApiError(message, status, code)


def strict_json(text):
    def reject(value):
        raise ValueError("Non-finite JSON number: " + value)
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key: " + key)
            result[key] = value
        return result
    return json.loads(text, parse_constant=reject, object_pairs_hook=pairs)


def validate(value, schema, path="arguments"):
    types = {"object": dict, "array": list, "string": str, "boolean": bool,
             "integer": int, "number": (int, float), "null": type(None)}
    if "oneOf" in schema:
        matches = 0
        for branch in schema["oneOf"]:
            try:
                validate(value, branch, path)
                matches += 1
            except ApiError:
                pass
        if matches != 1:
            fail(path + ": неверный тип или область настройки")
        return
    kind = schema.get("type")
    if kind and (not isinstance(value, types[kind]) or
                 kind in ("number", "integer") and isinstance(value, bool)):
        fail(path + ": ожидается " + kind)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            fail(path + ": нужно конечное число")
        for key, compare in (("minimum", lambda a, b: a < b), ("maximum", lambda a, b: a > b)):
            if key in schema and compare(value, schema[key]):
                fail(path + ": нарушена граница " + key)
    if "enum" in schema and value not in schema["enum"]:
        fail(path + ": недопустимое значение")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 100000):
            fail(path + ": недопустимая длина строки")
    if isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 100):
            fail(path + ": недопустимое число элементов")
        if schema.get("uniqueItems") and len(set(json.dumps(v, sort_keys=True) for v in value)) != len(value):
            fail(path + ": повторяющиеся элементы")
        for index, item in enumerate(value):
            validate(item, schema.get("items", {}), path + "[" + str(index) + "]")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        missing = [key for key in schema.get("required", []) if key not in value]
        if missing:
            fail(path + ": отсутствуют обязательные поля " + ", ".join(missing))
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            fail(path + ": неизвестные поля " + ", ".join(sorted(set(value) - set(properties))))
        for key, item in value.items():
            if key in properties:
                validate(item, properties[key], path + "." + key)


def obj(properties=None, required=None):
    return {"type": "object", "properties": properties or {},
            "required": list(required or []), "additionalProperties": False}


ID = {"type": "string", "minLength": 1, "maxLength": 160}
TEXT = {"type": "string", "minLength": 1, "maxLength": 20000}
MODE = {"type": "string", "enum": ["classic", "cylindrical", "cylindrical_full"]}
NUMBER = {"type": "number"}
VALUE = {"oneOf": [{"type": t} for t in ("string", "number", "boolean")]}
VECTOR = {"type": "array", "items": NUMBER, "minItems": 3, "maxItems": 3}
TARGET = {"oneOf": [obj({"scope": {"enum": ["global"]}}, ["scope"]),
                        obj({"scope": {"enum": ["extruder"]}, "extruder_id": ID}, ["scope", "extruder_id"])]}


def array(items, maximum=100, minimum=0):
    return {"type": "array", "items": items, "minItems": minimum, "maxItems": maximum, "uniqueItems": True}


def source_ids(refs, sources, path="source_refs"):
    if not isinstance(refs, list) or not refs:
        fail(path + ": нужны источники решения", "INVALID_MODEL_OUTPUT")
    for index, ref in enumerate(refs):
        validate(ref, obj({"id": ID, "version": {"type": "integer", "minimum": 1}}, ["id", "version"]), path + "[" + str(index) + "]")
        if ref["id"] not in sources or sources[ref["id"]]["version"] != ref["version"]:
            fail(path + "[" + str(index) + "]: неизвестный или устаревший источник " + ref["id"], "INVALID_MODEL_OUTPUT")
