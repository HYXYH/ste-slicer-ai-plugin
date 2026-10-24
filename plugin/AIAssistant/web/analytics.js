"use strict";
(() => {
  let visible = false, report = null, page = 0, lastFetch = 0, generation = 0, pending = false, signature = "";
  const PAGE_SIZE = 25, fmt = new Intl.NumberFormat("ru-RU", {maximumFractionDigits:1});
  const chatParts = [document.querySelector(".context"), document.querySelector(".task-line"), $("messages"), document.querySelector(".compose-area")];
  const display = value => value === null || value === undefined ? "Нет данных" : fmt.format(value);
  const latency = value => value === null ? "—" : display(value / 1000) + " с";
  const time = seconds => seconds === null ? "Нет данных" : display(seconds / 3600) + " ч";
  const material = values => Object.entries(values).map(([unit, value]) => display(value) + " " + ({m:"м", mm:"мм", g:"г", kg:"кг", mm3:"мм³"}[unit] || unit)).join(" · ") || "Нет данных";
  function error(message) { $("analytics-error").textContent = message || ""; $("analytics-error").hidden = !message; }
  function localButton(label, action, className = "button subtle") {
    const b = node("button", className, label); b.type = "button";
    b.onclick = async () => { b.disabled = true; try { await action(); } catch (e) { error(e.message); } finally { b.disabled = false; } };
    return b;
  }
  function panel(title, caption) {
    const box = node("section", "analytics-panel"), heading = node("div", "analytics-heading");
    heading.append(node("h2", "", title)); box.append(heading);
    if (caption) box.append(node("p", "analytics-caption", caption));
    return box;
  }
  function table(headers, rows) {
    const wrap = node("div", "analytics-table-wrap"), t = node("table", "analytics-table"), head = node("thead"), tr = node("tr");
    headers.forEach(h => { const th = node("th", "", h); th.scope = "col"; tr.append(th); });
    head.append(tr); t.append(head);
    const body = node("tbody");
    rows.forEach(cells => { const row = node("tr"); cells.forEach(cell => { const td = node("td"); td.append(cell instanceof Node ? cell : document.createTextNode(String(cell))); row.append(td); }); body.append(row); });
    t.append(body); wrap.append(t); return wrap;
  }
  function metric(label, value, detail, accent) {
    const box = node("div", "analytics-metric" + (accent ? " " + accent : ""));
    box.append(node("span", "analytics-metric-label", label), node("strong", "", value), node("span", "muted", detail)); return box;
  }
  function render(data) {
    const content = $("analytics-content"), s = data.summary, llm = data.llm;
    content.replaceChildren();
    if (!s.tasks) content.append(node("p", "analytics-empty", "Пока нет задач с активностью за этот период. Создайте задачу или выберите другой период — показатели появятся по мере работы."));
    const metrics = node("div", "analytics-metrics");
    metrics.append(metric("Задачи", display(s.tasks), "Анализов в работе: " + display(s.active_runs)),
      metric("Требуют внимания", display(s.tasks_needing_attention), "Уточнения, проверки или ошибки", s.tasks_needing_attention ? "amber" : ""),
      metric("Актуальный G-code", display(s.ready_results), "Задачи с завершённым слайсингом", "teal"),
      metric("Сохранённые комплекты", display(s.saved_bundles), "3MF + G-code, без повторов"));
    content.append(metrics);
    const grid = node("div", "analytics-grid");
    const activity = panel("Активность подготовки", "Запуски анализа по дням UTC. Для длинных периодов график показывает последние 31 день.");
    const legend = node("div", "analytics-legend"); legend.append(node("span", "legend-runs", "Все запуски"), node("span", "legend-errors", "Из них с ошибкой")); activity.append(legend);
    if (data.timeline.some(d => d.runs)) {
      const chart = node("div", "analytics-chart"); chart.setAttribute("role", "img"); chart.setAttribute("aria-label", "Запуски анализа по дням UTC. Подробные значения доступны в подсказках столбцов.");
      const max = Math.max(1, ...data.timeline.map(d => d.runs));
      data.timeline.forEach(d => {
        const bar = node("div", "analytics-day"); bar.title = d.date + " UTC: запусков " + d.runs + ", с ошибкой " + d.failed_runs + ", запросов LLM " + d.llm_requests;
        const column = node("div", "analytics-bar"); column.style.height = (d.runs / max * 100) + "%";
        const failed = node("div", "analytics-bar-error"); failed.style.height = (d.runs ? d.failed_runs / d.runs * 100 : 0) + "%";
        column.append(failed); bar.append(column); chart.append(bar);
      });
      activity.append(chart);
      const axis = node("div", "analytics-axis"); axis.append(node("span", "", data.timeline[0].date), node("span", "", data.timeline[data.timeline.length - 1].date)); activity.append(axis);
    } else activity.append(node("p", "analytics-empty", "В эти дни анализ ещё не запускался."));
    activity.append(node("p", "analytics-caption", "Завершено: " + s.completed_runs + " · Ошибок: " + s.failed_runs + " · Отменено: " + s.cancelled_runs + " · Прервано перезапуском: " + s.interrupted_runs));
    const attention = panel("Что требует внимания", "Одна задача может иметь несколько препятствий.");
    if (!data.attention.length) attention.append(node("p", "analytics-empty", "Открытых препятствий в выбранных задачах нет."));
    data.attention.slice(0, 6).forEach(item => {
      const row = node("div", "analytics-issue"), text = node("div"); text.append(node("strong", "", item.title), node("p", "muted", item.action));
      const link = localButton("Задач: " + display(item.count) + " →", () => {
        $("analytics-filter").value = "attention"; $("analytics-search").value = ""; page = 0; renderTasks(item.session_ids);
        $("analytics-tasks").scrollIntoView({behavior:"smooth", block:"nearest"});
      }); row.append(text, link); attention.append(row);
    });
    if (data.attention.length > 6) attention.append(node("p", "analytics-caption", "Другие препятствия доступны в таблице задач ниже."));
    grid.append(activity, attention);
    const stages = panel("Состояние задач", "Текущее состояние задач, активных в периоде.");
    if (!data.workflow_states.length) stages.append(node("p", "analytics-empty", "Нет данных."));
    data.workflow_states.forEach(item => {
      const row = node("div", "analytics-state"), track = node("span", "analytics-track"), fill = node("span"); fill.style.width = (item.count / Math.max(1, s.tasks) * 100) + "%"; track.append(fill);
      row.append(node("span", "", FLOW[item.state] || item.state), track, node("strong", "", display(item.count))); stages.append(row);
    });
    const usage = panel("LLM и время анализа", "Включая проверку подключения. Токены учитываются только при наличии usage в ответе API. Стоимость не рассчитывается.");
    usage.append(table(["Показатель", "Значение"], [
      ["HTTP-попытки / повторные попытки", display(llm.requests) + " / " + display(llm.retries)],
      ["Попытки без принятого ответа", display(llm.failed_requests)],
      ["Учтённые токены", display(llm.total_tokens)],
      ["Покрытие статистикой токенов", llm.usage_reported_requests + " из " + llm.requests + " попыток"],
      ["Входные / выходные токены", display(llm.prompt_tokens) + " / " + display(llm.completion_tokens)],
      ["LLM: медиана / p95", latency(llm.p50_ms) + " / " + latency(llm.p95_ms)],
      ["Анализ целиком: медиана / p95", latency(data.runs.p50_ms) + " / " + latency(data.runs.p95_ms)]
    ]));
    usage.append(node("p", "analytics-caption", "Анализ целиком включает очередь и ожидание импорта; выборка: " + data.runs.duration_samples + " завершённых запусков. p95 — 95-й процентиль длительности. Завершённый анализ не означает готовность к печати."));
    grid.append(stages, usage); content.append(grid);
    const estimates = panel("Оценки актуальных результатов", "По одному последнему актуальному результату на задачу. Это прогноз слайсера, а не измерения физической печати.");
    const estimateRows = node("div", "analytics-estimates"); estimateRows.append(metric("Время печати", time(s.estimated_print_seconds), "Сумма известных оценок"), metric("Материал", material(s.estimated_material), "Единицы учитываются раздельно")); estimates.append(estimateRows); content.append(estimates);
    const tools = panel("Работа инструментов", "Сортировка по p95. Измеряется вызов инструмента; start_slicing учитывает запуск backend, а не весь расчёт. Старые задержки восстановлены по событиям, когда это возможно.");
    if (data.tools.length) tools.append(table(["Инструмент", "Вызовы", "Ошибки", "Медиана", "p95", "Замеров"], data.tools.map(t => [TOOL_LABEL[t.name] || t.name, display(t.calls), display(t.failures), latency(t.p50_ms), latency(t.p95_ms), display(t.duration_samples)])));
    else tools.append(node("p", "analytics-empty", "Инструменты ещё не вызывались за этот период."));
    content.append(tools);
    if (data.errors.length) {
      const errors = panel("Ошибки за период", "Исторические ошибки могут быть уже устранены. Текущие препятствия показаны выше.");
      errors.append(table(["Код", "Событий"], data.errors.map(e => [e.code, display(e.count)]))); content.append(errors);
    }
    const notes = node("div", "analytics-notes"); data.notes.forEach(n => notes.append(node("p", "", n))); content.append(notes);
    renderTasks();
  }
  let issueIds = null;
  function renderTasks(ids) {
    if (ids !== undefined) issueIds = ids;
    if (!report) return;
    const search = $("analytics-search").value.trim().toLocaleLowerCase("ru"), filter = $("analytics-filter").value;
    const rows = report.sessions.filter(s => s.title.toLocaleLowerCase("ru").includes(search) &&
      (!issueIds || issueIds.includes(s.id)) && (filter === "all" || filter === "attention" && s.issues.length || filter === "ready" && s.ready_result || filter === "saved" && s.saved_bundles));
    const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE)); page = Math.min(page, pages - 1);
    $("analytics-task-count").textContent = "Показано " + rows.length + " из " + report.sessions.length + (issueIds ? " · по выбранному препятствию" : "");
    const box = $("analytics-tasks"); box.replaceChildren();
    if (!rows.length) box.append(node("p", "analytics-empty", "Нет задач по выбранным условиям."));
    else box.append(table(["Задача", "Состояние / препятствия", "Анализы / ошибки", "Токены API", "Оценка печати", "Комплекты"], rows.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE).map(s => {
      const state = node("div"); state.append(node("span", "", FLOW[s.workflow_state] || s.workflow_state));
      if (s.issues.length) {
        const labels = s.issues.map(code => (report.attention.find(a => a.code === code) || {}).title || code);
        state.append(node("div", "analytics-task-issues", labels.join(" · ")));
      }
      const estimate = node("div", "", s.ready_result ? time(s.estimated_print_seconds) : "Нет актуального G-code");
      if (Object.keys(s.estimated_material).length) estimate.append(node("div", "muted", material(s.estimated_material)));
      return [localButton(s.title, () => selectSession(s.id), "analytics-task-link"), state, s.runs + " / " + s.failed_runs, display(s.total_tokens), estimate, display(s.saved_bundles)];
    })));
    $("analytics-page").textContent = (page + 1) + " / " + pages;
    $("analytics-prev").disabled = page === 0; $("analytics-next").disabled = page + 1 >= pages;
  }
  async function refresh(force = false) {
    if (!visible || pending || !force && Date.now() - lastFetch < 10000) return;
    const request = generation, period = $("analytics-period").value;
    pending = true; $("analytics-content").setAttribute("aria-busy", "true");
    try {
      const data = await api("/api/analytics?days=" + period);
      if (request !== generation || !visible) return;
      const nextSignature = JSON.stringify({...data, generated_at:null}); report = data;
      if (signature !== nextSignature) { signature = nextSignature; render(data); }
      lastFetch = Date.now(); error("");
      $("analytics-updated").textContent = "Обновлено " + new Date(data.generated_at).toLocaleTimeString("ru-RU");
    } catch (e) { if (request === generation && visible) error(e.message + ". Последние данные могли устареть; повторите обновление."); }
    finally {
      pending = false; $("analytics-content").setAttribute("aria-busy", "false");
      if (visible && request !== generation) refresh(true);
    }
  }
  function close() {
    if (!visible) return;
    visible = false; generation += 1; $("analytics").hidden = true;
    chatParts.forEach(p => p.hidden = false); $("rename-session").hidden = false;
    $("open-analytics").setAttribute("aria-pressed", "false");
    if (currentSession) renderSession(currentSession);
  }
  $("open-analytics").onclick = () => {
    visible = true; generation += 1; lastFetch = 0;
    chatParts.forEach(p => p.hidden = true); $("rename-session").hidden = true;
    $("analytics").hidden = false; $("session-title").textContent = "Обзор подготовки"; $("session-title").title = "Обзор подготовки";
    $("open-analytics").setAttribute("aria-pressed", "true"); refresh(true);
  };
  $("analytics-back").onclick = () => { close(); poll(); };
  $("analytics-period").onchange = () => { generation += 1; page = 0; issueIds = null; signature = ""; refresh(true); };
  $("analytics-refresh").onclick = () => refresh(true);
  [$("analytics-search"), $("analytics-filter")].forEach(input => input.addEventListener("input", () => { issueIds = null; page = 0; renderTasks(); }));
  $("analytics-prev").onclick = () => { page -= 1; renderTasks(); };
  $("analytics-next").onclick = () => { page += 1; renderTasks(); };
  $("analytics-export").onclick = async () => {
    const b = $("analytics-export"); b.disabled = true;
    try {
      const response = await fetch("/api/analytics/export?days=" + $("analytics-period").value, {headers:{"X-Assistant-Token":token}, cache:"no-store"});
      if (!response.ok) throw new Error("Не удалось выгрузить CSV: HTTP " + response.status);
      const url = URL.createObjectURL(await response.blob()), link = node("a"); link.href = url; link.download = "assistant-analytics.csv";
      document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000); error("");
    } catch (e) { error(e.message); } finally { b.disabled = false; }
  };
  window.assistantAnalytics = {close, refresh, visible:() => visible};
})();
