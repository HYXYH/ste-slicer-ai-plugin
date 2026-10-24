"""Credentials live in application preferences, never in session history."""
import copy
import threading

from .contracts import obj, validate, fail
from .llm_client import endpoint


class Settings:
    def __init__(self, bridge):
        self.bridge = bridge
        self.lock = threading.RLock()
        self.config = {"base_url": "", "api_key": "", "model": "", "pipeline": "stepwise"}
        if hasattr(bridge, "call"):
            self.config.update(bridge.call("preferences", {}))
        # An empty preference means the default stepwise analysis.
        self.config["pipeline"] = self.config.get("pipeline") or "stepwise"

    def private(self):
        with self.lock:
            return copy.deepcopy(self.config)

    def public(self):
        config = self.private()
        return {"base_url": config["base_url"], "model": config["model"], "pipeline": config["pipeline"], "key_configured": bool(config["api_key"])}

    def update(self, body, clear=False):
        schema = obj({k: {"type": "string", "maxLength": 8192} for k in ("base_url", "model", "api_key")})
        schema["properties"]["pipeline"] = {"type": "string", "enum": ["stepwise", "single"]}
        validate(body, schema)
        values = dict(body)
        if values.get("base_url"):
            endpoint(values["base_url"])
        if not values.get("api_key"):
            values.pop("api_key", None)
        if clear:
            values["api_key"] = ""
        self.bridge.call("preferences", {"values": values})
        with self.lock:
            self.config.update(values)
        return self.public()

    def redact(self, value):
        secret = self.private().get("api_key")
        if isinstance(value, str):
            return value.replace(secret, "[ключ скрыт]") if secret else value
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, dict):
            return {key: self.redact(item) for key, item in value.items() if key.lower() not in
                    ("api_key", "authorization", "x-agent-run-token", "x-assistant-token")}
        return value
