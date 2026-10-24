"""Bounded Chat Completions transport using the Python 3.5 standard library."""
import base64
import json
import socket
import time
from http.client import IncompleteRead
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener
from urllib.error import HTTPError, URLError

from .contracts import fail, strict_json
from .output_contract import provider_schema
from ..analytics import usage_counts, token_count
from ..session import ApiError

HTTP_TIMEOUT = 120
MAX_REQUEST_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_RETRIES = 2


def generation_options(url, model, structured=False):
    # Provider extensions are scoped to the configured Timeweb/Qwen pair.
    # Other OpenAI-compatible services must not receive unknown parameters.
    if urlsplit(url).hostname != "api.timeweb.ai" or model != "dashscope/qwen3.8-flash":
        return {}
    options = {"enable_thinking": False, "max_completion_tokens": 8192}
    if structured:
        options["response_format"] = ({"type": "json_schema", "json_schema": {
            "name": "ste_task_map", "strict": True, "schema": provider_schema(structured)}}
            if isinstance(structured, dict) else {"type": "json_object"})
    return options


def schema_unsupported(raw):
    try:
        error = strict_json(raw.decode("utf-8"))
        message = error.get("error", {}).get("message", "").lower()
        return (isinstance(message, str) and "json_schema" in message and
                any(word in message for word in ("not supported", "unsupported", "not allowed", "unknown")))
    except (ValueError, TypeError, AttributeError, UnicodeError):
        return False


def option_metrics(options):
    result = {k: v for k, v in options.items() if k != "response_format"}
    if "response_format" in options:
        result["response_format"] = {"type": options["response_format"]["type"]}
    return result


def endpoint(url):
    if not isinstance(url, str):
        fail("Укажите Base URL")
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        fail("Нужен URL http/https без ключа, query и fragment")
    path = parts.path.rstrip("/")
    if not path.endswith("/chat/completions"):
        path += "/chat/completions"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Provider redirects never receive Authorization, even on the same host.
        return None


class LLMClient:
    def __init__(self, settings, observer=None):
        self.settings = settings
        self.observer = observer
        self.opener = build_opener(NoRedirect())

    def complete(self, messages, tools=None, cancelled=None, deadline=None, structured=False):
        config = self.settings.private()
        url = endpoint(config.get("base_url", ""))
        if not config.get("api_key") or not config.get("model"):
            fail("Сохраните ключ и модель в настройках подключения", "LLM_AUTH_ERROR", 409)
        payload = {"model": config["model"], "messages": messages, "stream": False}
        options = generation_options(url, config["model"], structured)
        if any(isinstance(m.get("content"), list) and any(p.get("type") == "image_url" for p in m["content"] if isinstance(p, dict)) for m in messages):
            if options.get("response_format", {}).get("type") == "json_schema":
                # Qwen's schema mode does not constrain multimodal input.
                options["response_format"] = {"type": "json_object"}
        payload.update(options)
        if tools:
            payload["tools"] = tools
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            fail("Запрос превышает 8 МиБ. Уменьшите изображение или историю.", "LLM_REQUEST_TOO_LARGE", 413)
        for attempt in range(MAX_RETRIES + 1):
            if cancelled and cancelled():
                fail("Анализ остановлен", "CANCELLED", 409)
            remaining = deadline - time.monotonic() if deadline else HTTP_TIMEOUT
            if remaining <= 0:
                fail("Истёк срок анализа", "LLM_TIMEOUT", 504)
            request = Request(url, body, {"Content-Type": "application/json", "Authorization": "Bearer " + config["api_key"]})
            started = time.monotonic()
            metric = {"model": config["model"], "attempt": attempt + 1, "http_status": None, "request_bytes": len(body),
                      "outcome": "error", "code": None, "duration_ms": None, "usage": usage_counts(None),
                      "generation_options": option_metrics(options), "reasoning_tokens": None, "reasoning_present": None}
            try:
                with self.opener.open(request, timeout=min(HTTP_TIMEOUT, remaining)) as response:
                    metric["http_status"] = response.getcode()
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                if cancelled and cancelled():
                    fail("Поздний ответ отменённого запуска отброшен", "CANCELLED", 409)
                if len(raw) > MAX_RESPONSE_BYTES:
                    fail("Ответ LLM превышает 2 МиБ", "INVALID_MODEL_OUTPUT", 502)
                result = strict_json(raw.decode("utf-8"))
                if not isinstance(result, dict):
                    raise ValueError()
                metric["usage"] = usage_counts(result.get("usage"))
                usage = result.get("usage")
                details = usage.get("completion_tokens_details") if isinstance(usage, dict) else None
                if isinstance(details, dict):
                    metric["reasoning_tokens"] = token_count(details.get("reasoning_tokens"))
                choice = result["choices"][0]
                if not isinstance(choice, dict):
                    raise ValueError()
                if choice.get("finish_reason") == "length":
                    fail("Ответ LLM обрезан ограничением контекста", "INVALID_MODEL_OUTPUT", 502)
                message = choice["message"]
                if not isinstance(message, dict) or message.get("role") != "assistant":
                    raise ValueError()
                metric["reasoning_present"] = bool(message.get("reasoning_content"))
                metric["outcome"] = "success"
                return message
            except HTTPError as error:
                code = error.code
                unsupported = False
                if code == 400 and options.get("response_format", {}).get("type") == "json_schema":
                    unsupported = schema_unsupported(error.read(16384))
                metric.update(http_status=code, code="LLM_AUTH_ERROR" if code in (401, 403) else "LLM_HTTP_ERROR",
                              duration_ms=round((time.monotonic() - started) * 1000, 2))
                error.close()
                if unsupported and attempt < MAX_RETRIES:
                    # Only an explicit rejection of json_schema permits this
                    # fallback. Validation and the schema in the prompt remain.
                    metric["code"] = "LLM_FORMAT_UNSUPPORTED"
                    options = dict(options, response_format={"type": "json_object"})
                    payload["response_format"] = options["response_format"]
                    body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
                    continue
                if code in (401, 403):
                    fail("API отклонил ключ доступа", "LLM_AUTH_ERROR", 502)
                if code == 429 or 500 <= code <= 599:
                    if attempt < MAX_RETRIES:
                        if cancelled and cancelled():
                            fail("Анализ остановлен", "CANCELLED", 409)
                        time.sleep(min(2 ** attempt, max(0, remaining - 1)))
                        continue
                fail("API вернул HTTP {}. Проверьте адрес и модель.".format(code), "LLM_HTTP_ERROR", 502)
            except (socket.timeout, TimeoutError):
                metric["code"] = "LLM_TIMEOUT"
                fail("LLM не ответила за отведённое время", "LLM_TIMEOUT", 504)
            except URLError as error:
                metric["code"] = "LLM_TIMEOUT" if isinstance(error.reason, (socket.timeout, TimeoutError)) else "LLM_CONNECTION_ERROR"
                if isinstance(error.reason, (socket.timeout, TimeoutError)):
                    fail("LLM не ответила за отведённое время", "LLM_TIMEOUT", 504)
                fail("Не удалось подключиться к API LLM. Проверьте адрес и сеть.", "LLM_CONNECTION_ERROR", 502)
            except OSError:
                metric["code"] = "LLM_CONNECTION_ERROR"
                fail("Не удалось подключиться к API LLM. Проверьте адрес и сеть.", "LLM_CONNECTION_ERROR", 502)
            except (ValueError, KeyError, IndexError, TypeError, UnicodeError, IncompleteRead):
                metric["code"] = "INVALID_MODEL_OUTPUT"
                fail("API вернул неполный или неверный JSON", "INVALID_MODEL_OUTPUT", 502)
            except ApiError as error:
                metric["code"] = error.code
                if error.code == "CANCELLED":
                    metric["outcome"] = "cancelled"
                raise
            finally:
                if metric["duration_ms"] is None:
                    metric["duration_ms"] = round((time.monotonic() - started) * 1000, 2)
                if self.observer:
                    try:
                        self.observer(metric)
                    except Exception:
                        # Analytics must never change printing decisions or retry a request.
                        pass


def image_content(attachment):
    with open(attachment["path"], "rb") as source:
        content = source.read(MAX_REQUEST_BYTES + 1)
    if len(content) > MAX_REQUEST_BYTES * 3 // 4:
        fail("Уменьшите изображение перед отправкой в LLM", "LLM_REQUEST_TOO_LARGE", 413)
    mime = "image/png" if attachment["name"].lower().endswith(".png") else "image/jpeg"
    return {"type": "image_url", "image_url": {"url": "data:" + mime + ";base64," + base64.b64encode(content).decode("ascii")}}
