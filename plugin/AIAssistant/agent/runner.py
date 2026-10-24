"""One global analysis worker. Session history remains readable during network waits."""
import copy
import json
import queue
import threading
import time

from ..session import ApiError, uid, now
from ..documents import task_input
from ..task_data import field_for, set_answer, reconcile_answers
from ..user_view import readable_text
from .contracts import strict_json, fail, validate, obj, ID, TEXT, array
from .llm_client import LLMClient, image_content, endpoint, HTTP_TIMEOUT
from .mcp_client import MCPClient
from .prompts import SYSTEM, STRUCTURE, DECISION
from .context import analysis_context, compact_result
from .recommend import recommend, describe
from .steps import Steps
from .output_contract import proposal_schema, validation_reason
from .task_state import merge_proposal, readiness, resolve_tool_unknowns, select_default_candidate, COLLECTIONS

MAX_LLM_REQUESTS = 12
MAX_TOOL_CALLS = 24
MAX_IDENTICAL_CALLS = 3
ANALYSIS_SECONDS = 600
# current_data already holds scene, catalogs and knowledge; extra reads rarely help and cost time.
MAX_TOOL_ROUNDS = 3
READINESS_NAMES = {"needs_data": "нужны данные", "needs_input": "нужны уточнения", "blocked": "нужна инженерная проверка",
                   "ready_for_plan": "данных достаточно для согласования плана", "stale": "данные устарели"}
STEP_NAMES = {"requirements": "Подтвердите, что требования поняты верно", "material": "Подтвердите выбор материала",
              "select_material": "Выберите подтверждённый материал в STE Slicer и повторите анализ"}


def task_text(session):
    return "\n".join([session["task"].get("description") or "", task_input(session)])


class Runner:
    def __init__(self, server):
        self.server, self.store = server, server.store
        self.queue = queue.Queue()
        self.closed = threading.Event()
        self.cancelled_ids = set()
        self.lock = threading.RLock()
        self.worker = threading.Thread(target=self._work, name="STE analysis")
        self.worker.daemon = True
        self.worker.start()

    def start(self, sid, request_id):
        validate(request_id, ID)
        def create(session):
            key = "run:" + request_id
            prior = session["workflow_requests"].get(key)
            if prior:
                return prior["result"]
            for run in session["runs"].values():
                if run["status"] in ("queued", "running") and run["input_revision"] == session["input_revision"]:
                    result = {"run_id": run["id"], "state": run["status"]}
                    session["workflow_requests"][key] = {"input_revision": session["input_revision"], "result": result}
                    return result
            run = {"id": uid(), "status": "queued", "input_revision": session["input_revision"], "created_at": now()}
            session["runs"][run["id"]] = run
            session["workflow_state"] = "queued"
            result = {"run_id": run["id"], "state": "queued"}
            session["workflow_requests"][key] = {"input_revision": session["input_revision"], "result": result}
            return result
        with self.lock:
            result = self.store.edit(sid, create)
            self.queue.put((sid, result["run_id"]))
        return result

    def cancel(self, sid, rid):
        with self.lock:
            session = self.store.internal(sid)
            if rid not in session["runs"]:
                fail("Запуск не найден", "OBJECT_NOT_FOUND", 404)
            self.cancelled_ids.add(rid)
        def update(s):
            if s["runs"][rid]["status"] in ("queued", "running"):
                s["runs"][rid]["status"] = "cancelled"
                s["workflow_state"] = "cancelled"
        self.store.edit(sid, update)
        return {"run_id": rid, "state": "cancelled"}

    def _work(self):
        while not self.closed.is_set():
            try:
                sid, rid = self.queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                run = self.store.internal(sid)["runs"][rid]
                if run["status"] == "queued":
                    self._analyze(sid, rid)
            except Exception:
                # The actual run saves a safe error; the worker must survive corrupt input.
                self.store.event(sid, rid, "error", code="RUNNER_ERROR", message="Ошибка обработки задачи. Повторите анализ.")
            finally:
                self.queue.task_done()

    def _analyze(self, sid, rid):
        revision = self.store.internal(sid)["runs"][rid]["input_revision"]
        deadline = time.monotonic() + ANALYSIS_SECONDS
        token = uid() + uid()
        configuration = None
        client = MCPClient(self.server, token)
        def llm_metric(metric):
            self.store.event(sid, rid, "llm_request", purpose="analysis", **self.server.settings.redact(metric))
        llm = LLMClient(self.server.settings, observer=llm_metric)
        counts, calls, repeated, cached = {"llm": 0}, [], {}, {}
        visible_sources = set()
        def cancelled():
            return (self.closed.is_set() or rid in self.cancelled_ids or
                    self.store.internal(sid)["input_revision"] != revision)
        def check():
            if cancelled():
                fail("Запуск отменён или исходные данные изменились", "CANCELLED", 409)
            if time.monotonic() > deadline:
                fail("Истёк срок анализа", "LLM_TIMEOUT", 504)
        def complete(messages, tools=None, structured=False):
            check()
            counts["llm"] += 1
            if counts["llm"] > MAX_LLM_REQUESTS:
                fail("Достигнут предел запросов LLM", "RUN_LIMIT", 409)
            self.store.event(sid, rid, "progress", phase="llm", request_number=counts["llm"],
                             message="Запрос к LLM №{}. Ожидаем ответ (до {} секунд).".format(
                                 counts["llm"], max(1, int(min(HTTP_TIMEOUT, deadline - time.monotonic())))))
            schema = False
            if isinstance(structured, dict):
                # A stepwise request brings its own small schema.
                schema = structured
            elif structured:
                sources = self.store.internal(sid)["sources"]
                schema = proposal_schema({key: sources[key] for key in visible_sources if key in sources})
            response = llm.complete(messages, tools, cancelled, deadline, structured=schema)
            check()
            return self.server.settings.redact(response)
        def tool(name, args, call_id=None, fresh=False):
            check()
            key = json.dumps([name, args], sort_keys=True, allow_nan=False)
            cacheable = name not in ("get_operation_status", "get_slice_result")
            if cacheable and not fresh and key in cached:
                return copy.deepcopy(cached[key])
            repeated[key] = repeated.get(key, 0) + 1
            calls.append(key)
            if len(calls) > MAX_TOOL_CALLS or repeated[key] > MAX_IDENTICAL_CALLS:
                fail("Достигнут предел вызовов инструментов", "RUN_LIMIT", 409)
            operation_id = uid()
            tool_started = time.monotonic()
            self.store.event(sid, rid, "tool_started", operation_id=operation_id, tool_call_id=call_id, tool=name, arguments=args)
            try:
                result = client.call(name, args)
                check()
                visible_sources.update(ref["id"] for ref in result.get("source_refs", []))
                visible_sources.update(row["id"] for row in result.get("data", {}).get("items", []) if row.get("usable_as_evidence"))
                self.store.event(sid, rid, "tool_finished", operation_id=operation_id, tool_call_id=call_id, tool=name, result=result,
                                 duration_ms=round((time.monotonic() - tool_started) * 1000, 2))
                if cacheable:
                    cached[key] = copy.deepcopy(result)
                return result
            except ApiError as exc:
                self.store.event(sid, rid, "tool_failed", operation_id=operation_id, tool_call_id=call_id, tool=name, code=exc.code, message=str(exc),
                                 duration_ms=round((time.monotonic() - tool_started) * 1000, 2))
                raise
        try:
            check()
            self.store.edit(sid, lambda s: (s["runs"][rid].update(status="running"), s.update(workflow_state="waiting_import")))
            self.store.event(sid, rid, "run_started")
            while True:
                check()
                session = self.store.internal(sid)
                used = set(fid for m in session["messages"] for fid in m.get("file_ids", []))
                attachments = [f for f in session["files"].values() if f["id"] in used and f["kind"] == "model"]
                if any(f["status"] in ("error", "interrupted") for f in attachments):
                    fail("Не все прикреплённые модели импортированы", "IMPORT_FAILED", 409)
                if not any(f["status"] in ("queued", "importing") for f in attachments):
                    break
                if self.closed.wait(0.25):
                    check()
            self.store.edit(sid, lambda s: s.update(workflow_state="analyzing"))
            self.server.mcp.grant(token, {"session_id": sid, "run_id": rid, "phase": "analyzing"})
            client.initialize()
            scene_result = tool("get_scene_snapshot", {})
            scene = scene_result["data"]
            capabilities = tool("get_printer_capabilities", {})
            configuration = tool("get_current_configuration", {})
            if scene["snapshot_ref"] != configuration["snapshot_ref"]:
                fail("Сцена изменилась между чтениями", "STALE_INPUT", 409)
            knowledge = tool("search_knowledge", {"query": task_text(session)[:20000] or "подготовка печати", "limit": 50})
            materials = tool("list_materials", {"limit": 50})
            profiles = tool("find_compatible_profiles", {"limit": 50})
            def observed(s):
                if s["input_revision"] != revision or cancelled():
                    fail("Исходные данные изменились", "CANCELLED", 409)
                targets = set(oid for f in attachments for oid in f.get("object_ids", []))
                targets.update(s["task_map"]["target_object_ids"])
                s["task_map"]["target_object_ids"] = sorted(targets)
                s["task_map"]["snapshot_ref"] = scene["snapshot_ref"]
                s["task_map"]["facts"] = [{"id": scene_result["source_refs"][0]["id"], "key": "scene", "value": scene,
                                           "unit": "mm", "status": "known", "source_refs": scene_result["source_refs"], "object_ids": sorted(targets), "updated_at": now(), "supersedes": None}]
                s["task_map"]["resources"] = {"catalog": materials["data"], "physical_availability": "unknown"}
                for m in s["messages"]:
                    if m["role"] == "user":
                        s["sources"][m["id"]] = {"id": m["id"], "version": 1, "kind": "user", "text": m["text"]}
                        for fid in m.get("file_ids", []):
                            f = s["files"].get(fid, {})
                            if f.get("kind") == "document":
                                s["sources"][fid] = {"id": fid, "version": 1, "kind": "user", "file_id": fid,
                                                      "message_id": m["id"], "text": f["document_text"]}
            self.store.edit(sid, observed)
            session = self.store.internal(sid)
            def rejected(error, phase):
                reason = self.server.settings.redact(validation_reason(error))
                self.store.event(sid, rid, "model_output_rejected", phase=phase,
                                 request_number=counts["llm"], message=reason)
                return reason
            stepwise = self.server.settings.private().get("pipeline") != "single"
            if stepwise:
                def progress(message):
                    self.store.event(sid, rid, "progress", phase="step", message=message)
                steps = Steps(complete, rejected, progress)
                catalog = list(materials["data"]["items"])
                cursor = materials["data"].get("next_cursor")
                while cursor and len(catalog) < 500:
                    page = tool("list_materials", {"limit": 50, "cursor": cursor})["data"]
                    catalog += page["items"]
                    cursor = page.get("next_cursor")
                updated, proposal = steps.advance(session, configuration, catalog)
                switch = proposal.get("switch_material")
                if switch:
                    # The user agreed to this material on the previous step; that consent covers selecting it.
                    progress("Выбираю материал в слайсере: " + switch["material_id"])
                    operation_id = uid()
                    self.store.event(sid, rid, "tool_started", operation_id=operation_id, tool="select_material", arguments=switch)
                    try:
                        before = self.server.bridge.call("get_scene_snapshot", {})["snapshot_ref"]
                        result = self.server.bridge.call("select_material", switch, expected_ref=before)
                        self.store.event(sid, rid, "tool_finished", operation_id=operation_id, tool="select_material", result=result)
                        configuration = tool("get_current_configuration", {}, fresh=True)
                        scene = tool("get_scene_snapshot", {}, fresh=True)["data"]
                        def switched(s):
                            if s["input_revision"] != revision or cancelled():
                                fail("Исходные данные изменились", "CANCELLED", 409)
                            s["task_map"]["snapshot_ref"] = scene["snapshot_ref"]
                        self.store.edit(sid, switched)
                        updated, proposal = steps.advance(self.store.internal(sid), configuration, catalog)
                    except ApiError as exc:
                        if exc.code == "CANCELLED":
                            raise
                        self.store.event(sid, rid, "tool_failed", operation_id=operation_id, tool="select_material", code=exc.code, message=str(exc))
                        proposal["summary"] = "Не удалось выбрать материал автоматически: {}. {}".format(exc, proposal["summary"])
                for decision in updated["decision_proposals"]:
                    for action in [a for a in decision["actions"] if a["tool"] == "set_settings"]:
                        accepted = []
                        for change in action["arguments"]["changes"]:
                            try:
                                # One unavailable setting must not block the G-code: drop it and say so.
                                self.server.bridge.call("validate_settings", {"changes": [change]})
                                accepted.append(change)
                            except ApiError as exc:
                                decision["limitations"].append("Настройка {} не применена: {}".format(change["key"], exc))
                        action["arguments"]["changes"] = accepted
                    decision["actions"] = [a for a in decision["actions"] if a["tool"] != "set_settings" or a["arguments"]["changes"]]
                latest = self.store.internal(sid)
            else:
                context = analysis_context(session, {"scene": scene_result, "capabilities": capabilities, "configuration": configuration,
                                                     "knowledge": knowledge, "materials": materials, "profiles": profiles})
                visible_sources.update(context["sources"])
                contract_text = "\nТочная JSON Schema ответа (кроме структуры, сервер проверяет источники и инженерные основания):\n" + json.dumps(proposal_schema(), ensure_ascii=False, separators=(",", ":"))
                messages = [{"role": "system", "content": SYSTEM + "\n" + DECISION + contract_text}, {"role": "user", "content": json.dumps(context, ensure_ascii=False, separators=(",", ":"))}]
                # History reduction is deterministic: last 20 user messages; the task map retains earlier requirements.
                for message in [m for m in session["messages"] if m["role"] == "user"][-20:]:
                    content = [{"type": "text", "text": "message_id=" + message["id"] + "\n" + (message["text"] or "Вложения к задаче")}]
                    for fid in message.get("file_ids", []):
                        attachment = self.store.file(sid, fid)
                        if attachment["kind"] == "image":
                            content.append(image_content(attachment))
                    messages.append({"role": "user", "content": content})
                schemas = client.request("tools/list")["tools"]
                read_names = set(tool["name"] for tool in self.server.tools.listing(read_only=True))
                available = [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["inputSchema"]}} for t in schemas if t["name"] in read_names and t["name"] not in ("get_operation_status", "get_slice_result")]
                proposal, initial_draft, last_validation = None, None, None
                rounds = 0
                while counts["llm"] < MAX_LLM_REQUESTS - 2 and rounds < MAX_TOOL_ROUNDS:
                    rounds += 1
                    response = complete(messages, available)
                    tool_calls = response.get("tool_calls", [])
                    if not tool_calls:
                        try:
                            proposed = strict_json(response["content"])
                            initial_draft = response
                            latest = self.store.internal(sid)
                            updated = merge_proposal(latest["task_map"], proposed, latest["sources"])
                            proposal = proposed
                        except (ApiError, ValueError, TypeError, KeyError) as exc:
                            # Prose or an invalid map still gets the bounded structured repair.
                            last_validation = rejected(exc, "initial")
                        break
                    if not isinstance(tool_calls, list) or not tool_calls or len(tool_calls) > MAX_TOOL_CALLS:
                        fail("Неверный список tool_calls", "INVALID_MODEL_OUTPUT", 502)
                    identifiers = [call.get("id") for call in tool_calls if isinstance(call, dict)]
                    if len(identifiers) != len(tool_calls) or any(not isinstance(i, str) or not i for i in identifiers) or len(set(identifiers)) != len(identifiers):
                        fail("Неверные ID tool_calls", "INVALID_MODEL_OUTPUT", 502)
                    messages.append({"role": "assistant", "content": response.get("content"), "tool_calls": tool_calls})
                    previous_calls = len(calls)
                    for call in tool_calls:
                        name = None
                        try:
                            function = call["function"]
                            name = function["name"]
                            if name not in read_names:
                                fail("Инструмент изменения недоступен при анализе", "APPROVAL_REQUIRED", 403)
                            args = strict_json(function["arguments"])
                            result = tool(name, args, call["id"])
                        except (ApiError, ValueError, KeyError, TypeError) as exc:
                            result = {"error": {"code": getattr(exc, "code", "INVALID_ARGUMENTS"), "message": str(exc) if isinstance(exc, ApiError) else "Неверные JSON-аргументы инструмента"}}
                        messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(compact_result(name, result), ensure_ascii=False, separators=(",", ":"))})
                    if len(calls) == previous_calls:
                        # Every requested read was already supplied. Finish the complete
                        # assistant/tool pair, then move to the structured decision.
                        break
                if proposal is None:
                    attempts = 1 if initial_draft is not None else 2
                    if initial_draft is not None:
                        messages.append({"role": "assistant", "content": initial_draft.get("content") or ""})
                    messages.append({"role": "user", "content": STRUCTURE +
                                     ("\nИсправь предыдущий ответ: " + last_validation if last_validation else "")})
                    for attempt in range(attempts):
                        response = complete(messages, structured=True)
                        try:
                            if response.get("tool_calls"):
                                raise ValueError()
                            proposal = strict_json(response["content"])
                            latest = self.store.internal(sid)
                            updated = merge_proposal(latest["task_map"], proposal, latest["sources"])
                            break
                        except (ApiError, ValueError, TypeError, KeyError) as exc:
                            last_validation = rejected(exc, "structured")
                            if attempt == attempts - 1:
                                fail("LLM дважды вернула неверную карту задачи", "INVALID_MODEL_OUTPUT", 502)
                            messages.append({"role": "assistant", "content": response.get("content") or ""})
                            messages.append({"role": "user", "content": "Исправь JSON по контракту. Ошибка: " + last_validation})
            check()
            current_scene = tool("get_scene_snapshot", {}, fresh=True)["data"]
            reconcile_answers(updated, self.store.internal(sid)["sources"])
            resolve_tool_unknowns(updated, self.store.internal(sid)["sources"], current_scene["snapshot_ref"])
            select_default_candidate(updated, ((configuration["data"].get("context") or {}).get("mode") or {}).get("id"))
            updated["readiness"] = readiness(updated, current_scene, latest["sources"])
            waiting = proposal.get("waiting_step")
            if waiting:
                # The user confirms this step before the next small request is sent.
                updated["readiness"].update(status="needs_input", reasons=[STEP_NAMES[waiting]])
            if not stepwise:
                selected = next((c for c in updated["candidates"] if c["status"] == "selected"), None)
                updated["recommended_settings"] = recommend(task_text(self.store.internal(sid)), configuration["data"].get("context"),
                                                            selected["material_id"] if selected else None)
            def save(s):
                if s["input_revision"] != revision or rid in self.cancelled_ids:
                    fail("Запуск устарел", "CANCELLED", 409)
                s["task_map"] = updated
            self.store.edit(sid, save)
            self.store.event(sid, rid, "map_updated", readiness=updated["readiness"])
            state = "waiting_user"
            if updated["readiness"]["status"] == "ready_for_plan":
                plan = self.server.plans.build(sid, proposal)
                self.store.event(sid, rid, "plan_ready", plan_id=plan["id"])
                state = "plan_ready"
            else:
                questions = [q for q in updated["questions"] if not q.get("answer")][:3]
                self.store.event(sid, rid, "questions_ready", questions=questions)
            check()
            def finish(s):
                if s["input_revision"] != revision or rid in self.cancelled_ids:
                    fail("Запуск устарел", "CANCELLED", 409)
                s["workflow_state"] = state
                s["runs"][rid].update(status="completed", finished_at=now())
                s["messages"].append({"id": uid(), "role": "assistant", "created_at": now(), "run_id": rid,
                                      "text": proposal["summary"] if waiting else proposal["summary"] + "\n\nПроверка: " + READINESS_NAMES[updated["readiness"]["status"]] +
                                      (". " + "; ".join(updated["readiness"]["reasons"]) if updated["readiness"]["reasons"] else "") +
                                      ("\n\nПрототип: не подтверждено требований — " + str(len(updated["readiness"]["accepted_risk_ids"])) +
                                       ", риск принят пользователем." if updated["readiness"].get("accepted_risk_ids") else "") +
                                      ("" if state == "plan_ready" or not updated.get("recommended_settings") else "\n\nНастройки слайсера под задачу:\n" + describe(updated["recommended_settings"])),
                                      "file_ids": []})
                if updated["requirements"] and (waiting == "requirements" or not stepwise):
                    s["messages"][-1]["requirements_card"] = {"rows": copy.deepcopy(updated["requirements"] + updated["constraints"]),
                                                               "conflicts": copy.deepcopy(updated["conflicts"])}
            self.store.edit(sid, finish)
            self.store.event(sid, rid, "run_finished", state=state)
        except Exception as exc:
            code = getattr(exc, "code", "ANALYSIS_FAILED")
            status = "cancelled" if code == "CANCELLED" else "failed"
            def finish_error(s):
                s["runs"][rid].update(status=status, error=code, finished_at=now())
                if s["input_revision"] == revision:
                    s["workflow_state"] = status
            message = self.server.settings.redact(str(exc)) if isinstance(exc, ApiError) else "Не удалось обработать ответ или данные API. Повторите анализ."
            fallback = None
            if status == "failed":
                try:
                    # The user still gets settings: the reference base answers without the LLM.
                    fallback = recommend(task_text(self.store.internal(sid)), configuration["data"].get("context") if configuration else None)
                except Exception:
                    fallback = None
            # Terminal state and its diagnostic become visible together to readers.
            with self.store.lock:
                self.store.edit(sid, finish_error)
                self.store.event(sid, rid, "error", code=code, message=message)
                if fallback:
                    def fallback_message(s):
                        s["task_map"]["recommended_settings"] = fallback
                        s["messages"].append({"id": uid(), "role": "assistant", "created_at": now(), "run_id": rid, "file_ids": [],
                                              "text": message + "\n\nНастройки слайсера под задачу (по справочнику, без LLM):\n" + describe(fallback)})
                    self.store.edit(sid, fallback_message)
        finally:
            client.close()
            self.server.mcp.revoke(token)

    def answers(self, sid, body):
        validate(body, obj({"request_id": ID, "text": {"type": "string", "maxLength": 20000},
                            "answers": array(obj({"question_id": ID, "value": TEXT}, ["question_id", "value"]), 3),
                            "assumptions": array(obj({"id": ID, "accepted": {"type": "boolean"}}, ["id", "accepted"])),
                            "include_object_ids": array(ID),
                            "manual_checks": array(obj({"requirement_id": ID, "reason": TEXT}, ["requirement_id", "reason"])),
                            "conflict_resolutions": array(obj({"id": ID, "reason": TEXT}, ["id", "reason"])),
                            "accept_risk": obj({"reason": TEXT}, ["reason"])}, ["request_id"]))
        def save(session):
            key = "answers:" + body["request_id"]
            previous = session["workflow_requests"].get(key)
            if previous:
                if previous["payload"] != body:
                    fail("request_id уже использован", status=409)
                return previous["message_id"]
            message_id = uid()
            task = session["task_map"]
            texts = [body.get("text", "")]
            for answer in body.get("answers", []):
                question = next((q for q in task["questions"] if q["id"] == answer["question_id"]), None)
                if not question:
                    fail("Вопрос не найден", "OBJECT_NOT_FOUND", 404)
                question["answer"] = dict(answer, message_id=message_id)
                field = field_for(question["field"])
                if field:
                    set_answer(session, field, answer["value"], message_id)
                texts.append(readable_text(question["text"]) + " " + answer["value"])
                if answer["value"].strip().lower() not in ("не знаю", "unknown"):
                    for unknown in task["unknowns"]:
                        if unknown["field"] == question["field"]:
                            unknown.update(resolved=True, resolution_ref=message_id)
            for consent in body.get("assumptions", []):
                assumption = next((a for a in task["assumptions"] if a["id"] == consent["id"]), None)
                if not assumption:
                    fail("Допущение не найдено", "OBJECT_NOT_FOUND", 404)
                assumption.update(acceptance="accepted" if consent["accepted"] else "rejected", consent_ref=message_id)
                texts.append("Допущение " + consent["id"] + (": принимаю" if consent["accepted"] else ": отклоняю"))
            task["target_object_ids"] = sorted(set(task["target_object_ids"]) | set(body.get("include_object_ids", [])))
            for resolution in body.get("conflict_resolutions", []):
                conflict = next((c for c in task["conflicts"] if c["id"] == resolution["id"]), None)
                if conflict is None:
                    fail("Противоречие не найдено", "OBJECT_NOT_FOUND", 404)
                conflict.update(resolved=True, resolution_ref=message_id, resolution_reason=resolution["reason"])
                texts.append("Разрешение противоречия " + resolution["id"] + ": " + resolution["reason"])
            requirements = {r["id"] for r in task["requirements"] + task["constraints"]}
            for check in body.get("manual_checks", []):
                if check["requirement_id"] not in requirements:
                    fail("Требование не найдено")
                ref = uid()
                session["sources"][ref] = {"id": ref, "version": 1, "kind": "manual", "message_id": message_id,
                                            "requirement_id": check["requirement_id"], "reason": check["reason"]}
                texts.append("Ручная инженерная проверка " + check["requirement_id"] + ": " + check["reason"] + "; источник " + ref)
            if "accept_risk" in body:
                task["risk_acceptance"] = {"message_id": message_id, "reason": body["accept_risk"]["reason"]}
                texts.append("Принимаю инженерный риск, режим прототипа: " + body["accept_risk"]["reason"])
            text = "\n".join(t for t in texts if t)
            if not text and not body.get("include_object_ids"):
                fail("Добавьте ответ или явное согласие")
            session["messages"].append({"id": message_id, "role": "user", "created_at": now(), "text": text, "file_ids": []})
            session["input_revision"] += 1
            task["input_revision"] = session["input_revision"]
            task["revision"] += 1
            task["readiness"] = {"status": "needs_data", "reasons": ["Получены новые ответы"]}
            for plan in session["plans"].values():
                plan["status"] = "stale"
            session["workflow_requests"][key] = {"payload": body, "message_id": message_id}
            return message_id
        self.store.edit(sid, save)
        return self.start(sid, "answers-" + body["request_id"])

    def connection_test(self, sid, request_id):
        validate(request_id, ID)
        oid = uid()
        result = {"operation_id": oid, "session_id": sid, "state": "queued"}
        def reserve(s):
            key = "llm_test:" + request_id
            if key in s["workflow_requests"]:
                return s["workflow_requests"][key], False
            s["operations"][oid] = {"id": oid, "kind": "llm_test", "status": "queued"}
            s["workflow_requests"][key] = result
            return result, True
        saved, fresh = self.store.edit(sid, reserve)
        if not fresh:
            return saved
        def run():
            try:
                def metric(data):
                    self.store.event(sid, None, "llm_request", purpose="connection_test", **self.server.settings.redact(data))
                response = LLMClient(self.server.settings, observer=metric).complete([{"role": "user", "content": "Ответь одним коротким словом: подключение работает?"}], cancelled=self.closed.is_set)
                result = {"status": "completed", "endpoint": endpoint(self.server.settings.public()["base_url"]), "text": self.server.settings.redact(response.get("content") or "Ответ получен"), "tool_calling_verified": False}
            except ApiError as error:
                result = {"status": "error", "error": str(error), "code": error.code}
            self.store.edit(sid, lambda s: s["operations"][oid].update(result))
        thread = threading.Thread(target=run, name="STE LLM test")
        thread.daemon = True
        thread.start()
        return result

    def close(self):
        self.closed.set()
        # Never join a network worker while Qt is shutting down.
