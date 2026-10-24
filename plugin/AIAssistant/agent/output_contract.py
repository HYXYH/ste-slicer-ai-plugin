"""Publish the server's task-map contract without accepting model-owned state."""
import copy
import json

from .contracts import obj, array
from .task_state import COLLECTIONS, ROW_SCHEMAS, REF
from ..session import ApiError


def proposal_schema(sources=None):
    properties = {name: array(copy.deepcopy(ROW_SCHEMAS[name])) for name in COLLECTIONS}
    properties["summary"] = {"type": "string", "maxLength": 20000}
    for name in COLLECTIONS:
        row = properties[name]["items"]["properties"]
        # These references are already mandatory in merge_proposal; an unknown
        # requirement check may still have no evidence, unlike its parent row.
        row["source_refs"]["minItems"] = 1
        if name == "candidates":
            row["evidence_refs"]["minItems"] = 1
    schema = obj(properties, list(COLLECTIONS) + ["summary"])
    if sources:
        def replace_refs(value):
            if value == REF:
                return {"$ref": "#/$defs/source_ref"}
            if isinstance(value, dict):
                return {k: replace_refs(v) for k, v in value.items()}
            if isinstance(value, list):
                return [replace_refs(v) for v in value]
            return value
        schema = replace_refs(schema)
        schema["$defs"] = {"source_ref": {"oneOf": [obj({
            "id": {"type": "string", "enum": [sid]},
            "version": {"type": "integer", "enum": [source["version"]]}}, ["id", "version"])
            for sid, source in sorted(sources.items())]}}
    return schema


def provider_schema(schema):
    """Qwen rejects uniqueItems. The server still enforces duplicates locally."""
    if isinstance(schema, dict):
        return {k: provider_schema(v) for k, v in schema.items() if k != "uniqueItems"}
    if isinstance(schema, list):
        return [provider_schema(v) for v in schema]
    return schema


def validation_reason(error):
    if isinstance(error, ApiError):
        return str(error)[:600]
    if isinstance(error, json.JSONDecodeError):
        return "Некорректный JSON: строка {}, столбец {}".format(error.lineno, error.colno)
    if isinstance(error, ValueError):
        return "Ответ должен содержать JSON без повторяющихся ключей и нечисловых значений"
    return "Ответ должен содержать текст JSON с обязательными полями карты задачи"
