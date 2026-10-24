"use strict";
const $ = id => document.getElementById(id);
const fragment = new URLSearchParams(location.hash.slice(1));
if (fragment.get("token")) {
  sessionStorage.setItem("assistant-token", fragment.get("token"));
  history.replaceState(null, "", location.pathname);
}
const token = sessionStorage.getItem("assistant-token") || "";
let current = null, currentSession = null, lastRevision = -1, polling = false, renameTarget = null;
const drafts = new Map(), busy = new Set();
const STATUS = {saved:"Сохранён", queued:"В очереди", importing:"Импортируется…", imported:"В сцене", error:"Ошибка", interrupted:"Импорт прерван"};
const TASK = {draft:"Новая задача", importing:"Загрузка моделей в сцену…", ready:"Модель и описание готовы", needs_description:"Добавьте описание задачи", needs_model:"Прикрепите модель", needs_attention:"Проверьте результат импорта"};

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}
function notice(message) { $("notice").textContent = message || ""; $("notice").hidden = !message; }
async function api(path, method = "GET", body, headers = {}) {
  headers["X-Assistant-Token"] = token;
  if (body !== undefined && !(body instanceof File)) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(body);
  }
  const response = await fetch(path, {method, body, headers, cache:"no-store"});
  if (!response.ok) {
    let message = "Ошибка HTTP " + response.status;
    try { message = (await response.json()).error || message; } catch (_) { /* no JSON body */ }
    throw new Error(message);
  }
  return response.json();
}
function draft(sid) {
  if (!drafts.has(sid)) drafts.set(sid, {text:"", files:[], request:null, initialized:false});
  return drafts.get(sid);
}
function sessionPath(sid) { return "/api/sessions/" + encodeURIComponent(sid); }
function updateButtons() {
  $("send").disabled = !current || busy.has(current);
  $("attach").disabled = !current || busy.has(current);
  $("description").disabled = !current || busy.has(current);
  $("rename-session").disabled = !current;
}
function renderList(listing) {
  $("sessions").replaceChildren();
  listing.sessions.forEach(s => {
    const button = node("button", "session-item" + (s.id === current ? " selected" : ""), s.title);
    button.type = "button";
    button.title = s.title;
    button.setAttribute("aria-current", s.id === current ? "true" : "false");
    button.onclick = () => selectSession(s.id).catch(e => notice(e.message));
    $("sessions").append(button);
  });
}
async function refreshList() { renderList(await api("/api/sessions")); }
async function selectSession(sid, select = true) {
  if (window.assistantAnalytics) window.assistantAnalytics.close();
  if (current) draft(current).text = $("description").value;
  current = sid;
  currentSession = null;
  lastRevision = -1;
  $("description").value = draft(sid).text;
  $("session-title").textContent = "Загрузка…";
  $("messages").replaceChildren();
  $("pending-files").replaceChildren();
  notice(""); updateButtons();
  const session = await api(sessionPath(sid) + (select ? "/select" : ""), select ? "POST" : "GET");
  if (current !== sid) return;
  renderSession(session);
  await refreshList();
}
function renderContext(context) {
  $("printer").textContent = context.printer ? context.printer.name : "Не выбран";
  $("mode").textContent = context.mode ? context.mode.name : "—";
  $("material").textContent = (context.extruders || []).map(e =>
    (e.active ? "● " : "") + (e.material ? e.material.name : "Не задан") +
    ((context.extruders || []).length > 1 ? " (E" + e.position + ")" : "")).join(", ") || "—";
  $("profile").textContent = context.profile ?
    (context.profile.name || (context.profile.quality || {}).name || "Не задан") +
    (context.profile.has_user_changes ? " · изменён" : "") : "—";
  $("context-error").hidden = !context.error && context.available;
  $("context-error").textContent = context.error || "Выберите принтер в STE Slicer, чтобы загрузить модель.";
}
function fileCard(file, sid, removable) {
  const card = node("div", "file-card");
  card.append(node("div", "file-name", file.name));
  const state = STATUS[file.status] || file.status;
  card.append(node("div", "file-meta", (file.size / 1024 / 1024).toFixed(2) + " МиБ · " + state));
  if (file.error) card.append(node("div", "file-error", file.error));
  const actions = node("div", "file-actions");
  if (file.kind === "image") {
    const preview = node("button", "", "Открыть изображение");
    preview.type = "button";
    preview.onclick = () => showImage(sid, file.id).catch(e => notice(e.message));
    actions.append(preview);
  }
  if (removable) {
    const remove = node("button", "", "Убрать из сообщения"); remove.type = "button";
    remove.onclick = () => {
      if (busy.has(sid)) return;
      const d = draft(sid); d.files = d.files.filter(id => id !== file.id); d.request = null;
      renderPending();
    };
    actions.append(remove);
  } else if (file.kind === "model" && ["saved", "error", "interrupted"].includes(file.status)) {
    const retry = node("button", "", "Загрузить в сцену"); retry.type = "button";
    retry.onclick = () => sendMessage(sid, "", [file.id]).catch(e => notice(e.message));
    actions.append(retry);
  }
  if (actions.childElementCount) card.append(actions);
  return card;
}
function renderPending() {
  const area = $("pending-files"); area.replaceChildren();
  if (!currentSession) return;
  draft(current).files.forEach(id => {
    if (currentSession.files[id]) area.append(fileCard(currentSession.files[id], current, true));
  });
}
function renderSession(session) {
  if (session.id !== current) return;
  if (session.revision < lastRevision) return;
  currentSession = session;
  if (!window.assistantAnalytics || !window.assistantAnalytics.visible()) {
    $("session-title").textContent = session.title;
    $("session-title").title = session.title;
  }
  $("task-state").textContent = FLOW[session.workflow_state] || TASK[session.task.status] || session.task.status;
  const busy = Object.values(session.runs || {}).some(r => ["queued", "running"].includes(r.status)) || BUSY_STATES.includes(session.workflow_state);
  const indicator = busy ? "busy" : ["failed", "interrupted"].includes(session.workflow_state) ? "error" : session.workflow_state === "idle" ? "idle" : "done";
  $("run-indicator").className = "run-indicator " + indicator;
  $("run-indicator").title = {busy:"Ассистент думает", error:"Ошибка", done:"Готово", idle:"Ожидание задачи"}[indicator];
  $("run-indicator").setAttribute("aria-label", $("run-indicator").title);
  $("stop-run").hidden = !Object.values(session.runs || {}).some(r => ["queued", "running"].includes(r.status));
  $("analyze").disabled = !$("stop-run").hidden;
  const d = draft(session.id);
  if (!d.initialized) {
    const used = new Set(session.messages.flatMap(m => m.file_ids || []));
    d.files = Object.keys(session.files).filter(id => !used.has(id));
    d.initialized = true;
  }
  renderPending();
  if (lastRevision === session.revision) return;
  lastRevision = session.revision;
  const area = $("messages");
  const atBottom = area.scrollHeight - area.scrollTop - area.clientHeight < 90;
  const oldScroll = area.scrollTop;
  area.replaceChildren();
  if (!session.messages.length) {
    const empty = node("div", "empty");
    empty.append(node("div", "empty-symbol", "↗"), node("h2", "", "Начните с детали и её задачи"),
      node("p", "", "Прикрепите 3D-модель кнопкой или перетащите её в окно чата и опишите назначение детали. Модель появится в сцене STE Slicer, а описание и файлы сохранятся в этом чате."));
    area.append(empty);
  }
  session.messages.forEach(message => {
    const article = node("article", "message " + message.role);
    const heading = node("div", "message-heading");
    heading.append(node("span", "", message.role === "user" ? "Вы" : message.role === "assistant" ? "Ассистент" : "Система"),
      node("time", "", new Date(message.created_at).toLocaleTimeString("ru", {hour:"2-digit", minute:"2-digit"})));
    article.append(heading);
    if (message.text) article.append(node("div", "message-body", message.text));
    if (message.file_ids.length) {
      const attachments = node("div", "attachments");
      message.file_ids.forEach(id => {
        if (session.files[id]) attachments.append(fileCard(session.files[id], session.id, false));
      });
      article.append(attachments);
    }
    area.append(article);
  });
  renderWorkflow(session, area);
  area.scrollTop = atBottom ? area.scrollHeight : oldScroll;
}
async function loadSession(sid) {
  const session = await api(sessionPath(sid));
  if (sid === current) renderSession(session);
}
function requestId() {
  const bytes = new Uint8Array(16); crypto.getRandomValues(bytes);
  return Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("");
}
async function sendMessage(sid, text, fileIds, retryId) {
  if (busy.has(sid)) return;
  busy.add(sid); updateButtons(); notice("");
  try {
    const sent = await api(sessionPath(sid) + "/messages", "POST", {
      request_id:retryId || requestId(), text, file_ids:fileIds
    });
    const d = draft(sid);
    if (retryId) { d.text = ""; d.files = []; d.request = null; }
    if (sid === current) { if (retryId) $("description").value = ""; await loadSession(sid); }
    await refreshList();
    if (sid === current && !sent.run_id) notice("Сообщение сохранено. Подключите LLM через кнопку слева, затем нажмите «Повторить анализ».");
  } finally { busy.delete(sid); updateButtons(); }
}
$("composer").onsubmit = async event => {
  event.preventDefault();
  if (!current || busy.has(current)) return;
  const sid = current, d = draft(sid);
  d.text = $("description").value;
  if (!d.text.trim() && !d.files.length) { notice("Добавьте описание или прикрепите файл."); return; }
  if (!d.request) d.request = requestId();
  try { await sendMessage(sid, d.text, d.files.slice(), d.request); }
  catch (e) { notice(e.message + " Повторная отправка этого сообщения не создаст дубликат."); }
};
$("description").oninput = () => {
  if (current) { const d = draft(current); d.text = $("description").value; d.request = null; }
};
$("description").onkeydown = event => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); $("composer").requestSubmit(); }
};
$("attach").onclick = () => $("file-input").click();
async function uploadFiles(files, sid = current) {
  if (!files.length) return;
  if (!sid || !currentSession) { notice("Дождитесь загрузки задачи, затем прикрепите файлы."); return; }
  if (busy.has(sid)) { notice("Дождитесь завершения текущей загрузки или отправки сообщения."); return; }
  busy.add(sid); updateButtons();
  const errors = [];
  try {
    for (const file of files) {
      if (sid === current) notice("Сохраняем «" + file.name + "»…");
      try {
        if (file.size > 100 * 1024 * 1024) throw new Error("Максимальный размер — 100 МиБ");
        const saved = await api(sessionPath(sid) + "/files", "POST", file, {
          "Content-Type":"application/octet-stream", "X-File-Name":encodeURIComponent(file.name)
        });
        const d = draft(sid); d.files.push(saved.id); d.request = null;
      } catch (e) { errors.push(file.name + ": " + e.message); }
    }
    if (sid === current) await loadSession(sid);
  } catch (e) { errors.push(e.message); }
  finally { busy.delete(sid); updateButtons(); if (sid === current) notice(errors.join("\n")); }
}
$("file-input").onchange = async event => {
  const files = Array.from(event.target.files); event.target.value = "";
  await uploadFiles(files);
};
let fileDragDepth = 0;
function isFileDrag(event) {
  return event.dataTransfer && Array.from(event.dataTransfer.types).includes("Files");
}
function resetFileDrag() {
  fileDragDepth = 0;
  $("file-drop-overlay").hidden = true;
}
document.addEventListener("dragenter", event => {
  if (!isFileDrag(event)) return;
  event.preventDefault();
  if (window.assistantAnalytics && window.assistantAnalytics.visible()) return;
  fileDragDepth += 1;
  $("file-drop-title").textContent = current && currentSession && !busy.has(current)
    ? "Отпустите файлы, чтобы прикрепить" : "Дождитесь завершения текущей операции";
  $("file-drop-overlay").hidden = false;
});
document.addEventListener("dragover", event => {
  if (!isFileDrag(event)) return;
  // Prevent the browser from navigating to the dropped file, including outside the composer.
  event.preventDefault();
  event.dataTransfer.dropEffect = current && currentSession && !busy.has(current) && !(window.assistantAnalytics && window.assistantAnalytics.visible()) ? "copy" : "none";
});
document.addEventListener("dragleave", () => {
  if (fileDragDepth > 0 && --fileDragDepth === 0) resetFileDrag();
});
document.addEventListener("drop", async event => {
  const files = Array.from(event.dataTransfer ? event.dataTransfer.files : []);
  const fileDrop = isFileDrag(event) || files.length > 0;
  resetFileDrag();
  if (!fileDrop) return;
  event.preventDefault();
  if (window.assistantAnalytics && window.assistantAnalytics.visible()) return;
  const items = Array.from(event.dataTransfer.items || []);
  if (items.some(item => {
    const entry = item.webkitGetAsEntry ? item.webkitGetAsEntry() : null;
    return entry && entry.isDirectory;
  })) { notice("Перетаскивайте отдельные файлы, а не папки."); return; }
  await uploadFiles(files);
});
document.addEventListener("dragend", resetFileDrag);
window.addEventListener("blur", resetFileDrag);
$("new-session").onclick = async () => {
  $("new-session").disabled = true;
  try { const created = await api("/api/sessions", "POST", {}); await selectSession(created.id, false); }
  catch (e) { notice(e.message); }
  finally { $("new-session").disabled = false; }
};
$("rename-session").onclick = () => {
  if (!currentSession) return;
  renameTarget = current; $("rename-input").value = currentSession.title;
  $("rename-dialog").showModal(); $("rename-input").select();
};
$("cancel-rename").onclick = () => $("rename-dialog").close();
$("rename-form").onsubmit = async event => {
  event.preventDefault();
  try {
    const session = await api(sessionPath(renameTarget), "PATCH", {title:$("rename-input").value});
    $("rename-dialog").close(); if (session.id === current) renderSession(session); await refreshList();
  } catch (e) { $("rename-dialog").close(); notice(e.message); }
};
let imageUrl = null;
async function showImage(sid, id) {
  const response = await fetch(sessionPath(sid) + "/files/" + encodeURIComponent(id), {headers:{"X-Assistant-Token":token}});
  if (!response.ok) throw new Error("Не удалось открыть изображение");
  if (imageUrl) URL.revokeObjectURL(imageUrl);
  imageUrl = URL.createObjectURL(await response.blob());
  $("image-preview").src = imageUrl; $("image-dialog").showModal();
}
$("close-image").onclick = () => $("image-dialog").close();
$("image-dialog").onclose = () => { if (imageUrl) URL.revokeObjectURL(imageUrl); imageUrl = null; $("image-preview").removeAttribute("src"); };
async function poll() {
  if (polling) return;
  polling = true;
  try {
    if (window.assistantAnalytics && window.assistantAnalytics.visible()) {
      await window.assistantAnalytics.refresh();
      return;
    }
    const sid = current;
    const [context, listing] = await Promise.all([api("/api/context"), api("/api/sessions")]);
    renderContext(context); renderList(listing);
    if (window.assistantAnalytics && window.assistantAnalytics.visible()) return;
    if (sid && sid === current) {
      await loadSession(sid);
      const latest = await api(sessionPath(sid) + "/events?after=" + (eventSeq.get(sid) || 0));
      if (latest.events.length) eventSeq.set(sid, latest.events[latest.events.length - 1].seq);
    }
    $("connection").textContent = "Локальное подключение";
    $("connection").classList.remove("offline");
  } catch (e) {
    $("connection").textContent = "Нет подключения"; $("connection").classList.add("offline");
    notice(e.message + ". Если слайсер был перезапущен, откройте чат через его меню.");
  } finally { polling = false; }
}
async function boot() {
  updateButtons();
  try {
    const listing = await api("/api/sessions");
    await selectSession(listing.active_session_id, false);
    await poll();
    setInterval(poll, 2000);
  } catch (e) { notice(e.message); $("connection").textContent = "Нет подключения"; }
}
const FLOW = {idle:"Готов к работе", queued:"Анализ в очереди", waiting_import:"Ожидание импорта", analyzing:"Анализ задачи…", waiting_user:"Нужны уточнения", plan_ready:"План готов к согласованию", applying:"Применение плана…", slicing:"Расчёт G-code…", result_ready:"Результат готов", exporting:"Сохранение файлов…", completed:"Файлы сохранены", failed:"Операция остановлена с ошибкой", cancelled:"Анализ остановлен", interrupted:"Работа прервана перезапуском", stale:"Данные устарели — повторите анализ"};
const BUSY_STATES = ["queued", "waiting_import", "analyzing", "applying", "slicing", "exporting"];
const eventSeq = new Map();
const TOOL_LABEL = {get_scene_snapshot:"Сцена и размеры", get_printer_capabilities:"Возможности принтера", get_current_configuration:"Текущие настройки", get_setting_definitions:"Ограничения настроек", list_materials:"Каталог материалов", find_compatible_profiles:"Совместимые профили", search_knowledge:"Экспертная база", set_print_mode:"Режим печати", select_material:"Выбор материала", select_quality_profile:"Выбор профиля", set_settings:"Изменение настроек", set_object_transform:"Положение, поворот и масштаб", arrange_objects:"Расстановка", remove_objects:"Удаление моделей", create_quality_profile:"Создание профиля", save_quality_profile:"Сохранение профиля"};
function button(label, action, primary = false) {
  const b = node("button", "button" + (primary ? " primary" : ""), label); b.type = "button";
  b.onclick = async () => { b.disabled = true; try { await action(); } catch (e) { notice(e.message); } finally { b.disabled = false; } };
  return b;
}
function panel(title, parent) { const p = node("section", "workflow-card"); p.append(node("h3", "", title)); parent.append(p); return p; }
function human(value) {
  if (value === null || value === undefined) return "не определено";
  if (Array.isArray(value)) return value.map(human).join(", ");
  if (typeof value === "object") return Object.entries(value).map(([key, v]) => key + ": " + human(v)).join("; ");
  return value === true ? "да" : value === false ? "нет" : String(value);
}
async function answer(sid, payload) {
  await api(sessionPath(sid) + "/answers", "POST", {request_id:requestId(), ...payload});
  if (sid === current) await loadSession(sid);
}
function renderWorkflow(session, area) {
  const sid = session.id, map = session.task_map;
  if (!map) return;
  const latestRun = Object.values(session.runs || {}).sort((a,b) => (a.created_at || "").localeCompare(b.created_at || "")).slice(-1)[0];
  const currentEvents = latestRun ? session.events.filter(e => e.run_id === latestRun.id || Date.parse(e.created_at) >= Date.parse(latestRun.created_at)) : session.events;
  const currentEventIds = new Set(currentEvents.map(e => e.id));
  const active = latestRun && ["queued", "running"].includes(latestRun.status);
  if (active) {
    const box = panel("Анализ выполняется", area);
    const progress = currentEvents.filter(e => e.type === "progress" || e.type === "tool_started").slice(-1)[0];
    box.append(node("p", "", progress ? progress.message || (TOOL_LABEL[progress.tool] || progress.tool) : "Задача в очереди на анализ."),
      node("p", "muted", "Готовый план появится отдельной карточкой с кнопкой согласования. Ошибки предыдущих запусков находятся в отдельной истории ниже."));
  }
  if (["failed", "cancelled", "interrupted"].includes(session.workflow_state)) {
    const latest = currentEvents.filter(e => e.type === "error").slice(-1)[0];
    if (latest) panel("Работа остановлена", area).append(node("p", "warning", latest.message));
  }
  const rec = map.recommended_settings;
  if (rec && rec.settings && rec.settings.length) {
    const box = panel(active ? "Настройки слайсера под задачу · обновляются" : "Настройки слайсера под задачу", area);
    if (rec.material && rec.material.id) box.append(node("p", "", "Материал: " + rec.material.id + " · " + (rec.material.basis || "")));
    if (rec.orientation) box.append(node("p", "", "Ориентация: " + rec.orientation));
    const table = node("table", "settings-table");
    rec.settings.forEach(s => {
      const row = node("tr");
      const value = s.value === true ? "вкл" : s.value === false ? "выкл" : String(s.value) + (s.unit ? " " + s.unit : "");
      row.append(node("td", "settings-key", s.key), node("td", "settings-value", value), node("td", "muted", s.reason));
      table.append(row);
    });
    box.append(table);
    (rec.warnings || []).forEach(w => box.append(node("p", "warning", w)));
    box.append(node("p", "muted", "Источники: " + (rec.source_ids || []).join(", ")));
  }
  if (map.facts.length || map.requirements.length) {
    const known = panel("Известно", area);
    map.requirements.concat(map.constraints).forEach(r => known.append(node("p", "", r.key + ": " + human(r.value) + (r.unit ? " " + r.unit : "") + (r.strength === "hard" ? " · обязательное" : ""))));
    map.facts.filter(f => f.key === "scene").forEach(f => (f.value.objects || []).forEach(o => known.append(node("p", "", o.name + " · " + human(o.dimensions_mm) + " мм"))));
  }
  const questions = map.questions.filter(q => !q.answer).slice(0, 3);
  const sceneFact = map.facts.find(f => f.key === "scene");
  const foreign = sceneFact ? sceneFact.value.objects.filter(o => o.printable && !map.target_object_ids.includes(o.id)) : [];
  if (foreign.length) {
    const box = panel("Другие модели в общей сцене", area);
    box.append(node("p", "", foreign.map(o => o.name).join(", ")),
      button("Включить эти модели в задачу", () => answer(sid, {include_object_ids:foreign.map(o => o.id)})));
    box.append(node("p", "muted", "Можно также убрать лишние модели в STE Slicer и повторить анализ."));
  }
  if (questions.length) {
    const box = panel("Нужно уточнить", area);
    questions.forEach(q => {
      const question = node("div", "question"); question.append(node("strong", "", q.text), node("p", "muted", q.reason));
      const options = node("div", "workflow-actions");
      (q.options || []).forEach(option => options.append(button(option.label, () => answer(sid, {answers:[{question_id:q.id,value:option.label}]}))));
      if (q.allow_unknown) options.append(button("Не знаю", () => answer(sid, {answers:[{question_id:q.id,value:"Не знаю"}]})));
      question.append(options);
      if (q.allow_free_text) {
        const input = node("input"); input.placeholder = "Ваш ответ"; input.maxLength = 20000;
        input.setAttribute("aria-label", q.text);
        question.append(input, button("Ответить", () => answer(sid, {answers:[{question_id:q.id,value:input.value}]})));
      }
      box.append(question);
    });
  }
  const assumptions = map.assumptions.filter(a => a.acceptance === "pending");
  if (assumptions.length) {
    const box = panel("Допущения требуют вашего согласия", area);
    assumptions.forEach(a => {
      const p = node("div", "question"); p.append(node("p", "", a.key + ": " + human(a.value) + ". " + (a.reason || "")),
        button("Принять", () => answer(sid, {assumptions:[{id:a.id,accepted:true}]})), button("Отклонить", () => answer(sid, {assumptions:[{id:a.id,accepted:false}]})));
      box.append(p);
    });
  }
  const limitations = (map.readiness.reasons || []).concat(map.unknowns.filter(u => u.blocking && !u.resolved).map(u => u.reason));
  if (limitations.length || map.conflicts.some(c => !c.resolved)) {
    const box = panel(active ? "Ограничения предыдущего анализа · обновляются" : "Ограничения", area);
    limitations.forEach(reason => box.append(node("p", "", reason)));
    map.conflicts.filter(c => !c.resolved).forEach(c => {
      box.append(node("p", "", c.reason || "Противоречие: " + c.id));
      const input = node("input"); input.placeholder = "Укажите, какое требование считать действующим"; input.setAttribute("aria-label", input.placeholder);
      box.append(input, button("Разрешить противоречие", () => answer(sid, {conflict_resolutions:[{id:c.id,reason:input.value}]})));
    });
  }
  if ((map.readiness.failed_check_ids || []).length) {
    const box = panel("Ручная инженерная проверка", area);
    box.append(node("p", "muted", "Подтверждайте требование только после фактической проверки. Укажите метод, источник и результат."));
    map.requirements.concat(map.constraints).filter(r => map.readiness.failed_check_ids.includes(r.id)).forEach(r => {
      const input = node("input"); input.placeholder = "Основание проверки: " + r.key; input.setAttribute("aria-label", input.placeholder);
      box.append(input, button("Подтвердить проверку", () => answer(sid, {manual_checks:[{requirement_id:r.id,reason:input.value}]})));
    });
    const risk = node("div", "question");
    risk.append(node("p", "muted", "Или напечатайте как прототип: ассистент предложит настройки, а непроверенные требования останутся в ограничениях плана."));
    const purpose = node("input"); purpose.placeholder = "Цель прототипа, например: проверка геометрии и посадки"; purpose.setAttribute("aria-label", purpose.placeholder);
    risk.append(purpose, button("Печатать как прототип (принять риск)", () => answer(sid, {accept_risk:{reason:purpose.value || "Прототип"}})));
    box.append(risk);
  }
  if ((map.readiness.accepted_risk_ids || []).length) {
    const box = panel("Режим прототипа", area);
    box.append(node("p", "warning", "Риск принят. Не подтверждено требований и неизвестных: " + map.readiness.accepted_risk_ids.length + ". Деталь не годится для эксплуатации без испытаний."));
  }
  Object.values(session.plans).forEach(plan => {
    const box = panel("План печати · версия " + plan.version, area);
    box.append(node("p", "", "Режим: " + ({classic:"3D", cylindrical:"5D Spiral", cylindrical_full:"5D"}[plan.selected.mode] || plan.selected.mode)));
    box.append(node("p", "", "Материал: " + plan.selected.material_id + " · профиль: " + plan.selected.profile_id));
    if (plan.status === "stale") box.append(node("p", "warning", "План устарел. Повторите анализ."));
    plan.actions.forEach(a => {
      const row = node("div", "plan-action"); row.append(node("strong", "", TOOL_LABEL[a.tool] || a.tool), node("p", "", human(a.before) + " → " + human(a.after)), node("p", "muted", a.reason)); box.append(row);
    });
    if (!plan.actions.length) box.append(node("p", "", "Сохранить текущие настройки профиля и рассчитать G-code."));
    plan.limitations.forEach(l => box.append(node("p", "warning", l)));
    if (["validated", "approved"].includes(plan.status)) box.append(button("Согласовать, применить и рассчитать", async () => {
      await api(sessionPath(sid) + "/plans/" + plan.id + "/approve", "POST", {version:plan.version});
      await api(sessionPath(sid) + "/plans/" + plan.id + "/apply", "POST", {request_id:plan.id + "-apply"});
      if (sid === current) await loadSession(sid);
    }, true));
  });
  const slices = Object.values(session.operations).filter(o => o.kind === "slice");
  slices.forEach(op => {
    const box = panel("Слайсинг", area);
    box.append(node("p", "", op.status === "slicing" ? "Расчёт…" + (op.progress === null ? "" : " " + Math.round(op.progress * 100) + "%") : op.status === "completed" ? "Расчёт завершён" : "Результат требует проверки: " + op.status));
    if (op.error) box.append(node("p", "warning", op.error));
    if (op.result) box.append(node("p", "", "Время: " + human(op.result.time) + " · материал: " + human(op.result.material)));
    if (op.status === "completed" && session.workflow_state !== "stale") {
      const overwrite = node("input"); overwrite.type = "checkbox";
      const label = node("label", "overwrite"); label.append(overwrite, document.createTextNode(" Разрешаю замену project.3mf и result.gcode в выбранной папке"));
      box.append(label, button("Выбрать папку и сохранить", async () => {
        const target = await api(sessionPath(sid) + "/export-target", "POST", {request_id:requestId(),overwrite:overwrite.checked});
        const selected = await waitOperation(sid, target.operation_id);
        if (selected.result.cancelled) return;
        const exported = await api(sessionPath(sid) + "/exports", "POST", {request_id:requestId(), operation_id:op.id, export_target_id:selected.result.export_target_id});
        await waitOperation(sid, exported.operation_id);
        if (sid === current) { notice("Проект и G-code сохранены."); await loadSession(sid); }
      }, true));
    }
  });
  session.exports.forEach(result => {
    const box = panel(result.partial ? "Сохранено частично" : "Сохранённые файлы", area);
    result.files.forEach(f => box.append(node("p", "", f.filename + " · " + f.size + " байт")));
  });
  function eventHistory(events, title) {
    const details = node("details", "workflow-card");
    const operations = new Map();
    events.filter(e => e.operation_id && e.tool).forEach(e => {
      const key = e.operation_id, record = operations.get(key) || {}; operations.set(key, {...record,...e});
    });
    const errors = events.filter(e => e.type === "error").slice(-5);
    const rejected = events.filter(e => e.type === "model_output_rejected").slice(-3);
    if (!operations.size && !errors.length && !rejected.length) return;
    details.append(node("summary", "", title + " · инструментов: " + operations.size));
    operations.forEach(event => {
      const item = node("details", "tool-event");
      item.append(node("summary", "", (TOOL_LABEL[event.tool] || event.tool) + " · " + ({tool_started:"выполняется",tool_finished:"готово",tool_failed:"ошибка"}[event.type] || event.type)), node("pre", "", JSON.stringify({arguments:event.arguments,result:event.result,error:event.message}, null, 2))); details.append(item);
    });
    errors.forEach(e => details.append(node("p", "warning", new Date(e.created_at).toLocaleTimeString("ru-RU") + " · " + e.message)));
    rejected.forEach(e => {
      const item = node("details", "tool-event");
      item.append(node("summary", "", "Проверка ответа LLM №" + e.request_number + " · формат не принят"), node("p", "warning", e.message));
      details.append(item);
    });
    area.append(details);
  }
  eventHistory(currentEvents, "Действия текущего запуска");
  eventHistory(session.events.filter(e => !currentEventIds.has(e.id)), "Предыдущие запуски");
}
async function waitOperation(sid, oid) {
  for (;;) {
    const result = await api(sessionPath(sid) + "/operations/" + oid);
    if (result.status === "completed") return result;
    if (["error", "interrupted", "stale"].includes(result.status)) throw new Error(result.error || "Операция прервана");
    await new Promise(resolve => setTimeout(resolve, 750));
  }
}
$("analyze").onclick = async () => {
  if (!current) return;
  try { const sid = current; await api(sessionPath(sid) + "/runs", "POST", {request_id:requestId()}); await loadSession(sid); } catch(e) { notice(e.message); }
};
$("stop-run").onclick = async () => {
  if (!currentSession) return;
  const sid = current;
  try { for (const run of Object.values(currentSession.runs).filter(r => ["queued", "running"].includes(r.status))) await api(sessionPath(sid) + "/runs/" + run.id + "/cancel", "POST", {}); await loadSession(sid); } catch(e) { notice(e.message); }
};
async function settingsState() {
  const config = await api("/api/settings/llm"); $("llm-url").value = config.base_url; $("llm-model").value = config.model; $("llm-pipeline").value = config.pipeline || "stepwise"; $("llm-key").value = "";
  $("key-state").textContent = config.key_configured ? "✓ Ключ сохранён в STE Slicer" : "Ключ пока не задан";
  $("key-state").classList.toggle("configured", config.key_configured);
  $("llm-key").placeholder = config.key_configured ? "Ключ уже сохранён. Введите новый только для замены" : "Введите API key";
  $("clear-key").disabled = !config.key_configured;
  return config;
}
$("llm-settings").onclick = async () => { try { await settingsState(); $("llm-result").textContent = ""; $("llm-dialog").showModal(); } catch(e) { notice(e.message); } };
$("close-llm").onclick = () => { $("llm-key").value = ""; $("llm-dialog").close(); };
async function saveSettings() {
  await api("/api/settings/llm", "PATCH", {base_url:$("llm-url").value,model:$("llm-model").value,pipeline:$("llm-pipeline").value,api_key:$("llm-key").value}); return settingsState();
}
$("llm-form").onsubmit = async event => { event.preventDefault(); try { const config = await saveSettings(); $("llm-result").textContent = config.key_configured ? "Настройки сохранены. Ключ сохранён в STE Slicer; поле очищено, чтобы не показывать секрет." : "Настройки сохранены. API key пока не задан."; } catch(e) { $("llm-result").textContent = e.message; } };
$("clear-key").onclick = async () => { try { await api("/api/settings/llm/clear-key", "POST", {}); await settingsState(); $("llm-result").textContent = "Ключ удалён"; } catch(e) { $("llm-result").textContent = e.message; } };
$("test-llm").onclick = async () => {
  const sid = current; $("test-llm").disabled = true; $("llm-result").textContent = "Проверяем подключение…";
  try { await saveSettings(); const op = await api("/api/settings/llm/test", "POST", {session_id:sid,request_id:requestId()}); const result = await waitOperation(sid, op.operation_id); $("llm-result").textContent = result.endpoint + "\n" + result.text + "\nВызовы инструментов этим запросом не проверяются."; }
  catch(e) { $("llm-result").textContent = e.message; } finally { $("test-llm").disabled = false; }
};
boot();
