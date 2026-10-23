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
  $("session-title").textContent = session.title;
  $("session-title").title = session.title;
  $("task-state").textContent = TASK[session.task.status] || session.task.status;
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
    heading.append(node("span", "", message.role === "user" ? "Вы" : "Система"),
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
    await api(sessionPath(sid) + "/messages", "POST", {
      request_id:retryId || requestId(), text, file_ids:fileIds
    });
    const d = draft(sid);
    if (retryId) { d.text = ""; d.files = []; d.request = null; }
    if (sid === current) { if (retryId) $("description").value = ""; await loadSession(sid); }
    await refreshList();
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
  fileDragDepth += 1;
  $("file-drop-title").textContent = current && currentSession && !busy.has(current)
    ? "Отпустите файлы, чтобы прикрепить" : "Дождитесь завершения текущей операции";
  $("file-drop-overlay").hidden = false;
});
document.addEventListener("dragover", event => {
  if (!isFileDrag(event)) return;
  // Prevent the browser from navigating to the dropped file, including outside the composer.
  event.preventDefault();
  event.dataTransfer.dropEffect = current && currentSession && !busy.has(current) ? "copy" : "none";
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
    const sid = current;
    const [context, listing] = await Promise.all([api("/api/context"), api("/api/sessions")]);
    renderContext(context); renderList(listing);
    if (sid && sid === current) await loadSession(sid);
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
boot();
