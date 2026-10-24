# HTTP, MCP и карта задачи

Все запросы API и MCP требуют `X-Assistant-Token` процесса. Сервер слушает
только 127.0.0.1 на выбранном ОС порту. Проверяются Host и переданный Origin.
JSON не допускает повторных ключей, NaN и Infinity. Ошибка HTTP сохраняет
`error` и добавляет `code`, `details`. Неизвестный ID сессии или операции даёт 404.
Ключи и внутренние пути вложений не выдаются клиенту.

## Сохранённые маршруты

`GET /api/context`; `GET/POST /api/sessions`; `GET/PATCH /api/sessions/{sid}`;
`POST /api/sessions/{sid}/select`; `POST /api/sessions/{sid}/files`;
`GET /api/sessions/{sid}/files/{fid}`; `POST /api/sessions/{sid}/messages`;
`GET /api/sessions/{sid}/operations/{oid}`.

Загрузка принимает байты и имя в `X-File-Name`. Просмотр доступен только для
изображений. Файл другой сессии нельзя прикрепить по ID. 3MF импортируется
как геометрия. Сообщение: `{request_id,text,file_ids}`. Ответ содержит
`message_id`, `operation_ids`; при настроенной LLM также `run_id`, `state`.
Повтор сообщения возвращает сохранённый ответ. Изменение содержимого при том же
request_id даёт 409. Старое `task.status=ready` означает только готовность импорта.

## Новые маршруты и тела

Поля тела, не перечисленные ниже, запрещены. Обязательные поля отмечены `*`.
ID — непустая строка до 160 символов. POST/PATCH принимает application/json.

| Метод и путь | Тело | Ответ |
| --- | --- | --- |
| GET `/api/settings/llm` | Нет | base_url, model, key_configured |
| PATCH `/api/settings/llm` | base_url?, model?, api_key? | Безопасная часть настроек |
| POST `/api/settings/llm/test` | session_id*, request_id* | 202: operation_id, session_id, state |
| POST `/api/settings/llm/clear-key` | `{}` | Безопасная часть настроек |
| GET `/api/sessions/{sid}/events?after=N` | Нет | events (до 100), last_seq |
| POST `/api/sessions/{sid}/runs` | request_id* | 202: run_id, state |
| POST `/api/sessions/{sid}/runs/{rid}/cancel` | `{}` | run_id, state=cancelled |
| POST `/api/sessions/{sid}/answers` | request_id*, text?, answers?, assumptions?, include_object_ids?, manual_checks?, conflict_resolutions? | 202: run_id, state |
| GET `/api/sessions/{sid}/plans/{pid}` | Нет | Полный объект плана |
| POST `/api/sessions/{sid}/plans/{pid}/approve` | version* (целое >=1) | План с серверным approval |
| POST `/api/sessions/{sid}/plans/{pid}/apply` | request_id* | 202: plan_id, status=applying |
| POST `/api/sessions/{sid}/export-target` | request_id*, overwrite? (bool) | 202: operation_id, state |
| POST `/api/sessions/{sid}/exports` | request_id*, operation_id*, export_target_id* | 202: operation_id, state |

`answers`: до трёх `{question_id,value}`. Вариант ответа отправляется его текстом.
«Не знаю» не закрывает неизвестное. `assumptions`: `{id,accepted}`. Согласие
привязано к сообщению пользователя. `manual_checks`: `{requirement_id,reason}`.
В reason укажите метод, источник и результат фактической инженерной проверки.
`conflict_resolutions`: `{id,reason}`. Противоречие сохраняется с причиной разрешения.
`include_object_ids`: явное включение дополнительных моделей в задачу.

Результат выбора папки читается через operations: `result.export_target_id`,
`folder_name`, `overwrite` либо `cancelled`. Путь хранится только на сервере.
Результат exports: `result.files[]` с filename, size, written. При частичной ошибке
сохранённые файлы остаются в `session.exports` с partial=true.
Тест LLM возвращает endpoint, text и tool_calling_verified=false: короткий ответ
не подтверждает инструментальный режим.

Один request_id не запускает повторный анализ, применение, тест или экспорт.
Файлы другого расчёта нельзя включить в повтор того же экспорта. Различаются
request_id (повтор запроса), run_id (обработка сообщения), operation_id (инструмент).
Конфликт параметров повторного запроса даёт 409. Незавершённое изменение с
неизвестным исходом не выполняется повторно вслепую.

## Карта задачи

`session.task_map` содержит schema_version=1, revision, input_revision,
target_object_ids, snapshot_ref, requirements, constraints, facts, resources,
candidates, unknowns, conflicts, assumptions, decisions, decision_proposals,
questions, history, readiness. Версия всей переписки не совпадает с версиями карты
и исходных требований. Источники находятся в session.sources.

Например, факт сцены имеет key=scene, value с objects и configuration, unit=mm,
status=known и source_refs `[{"id":"server-issued-source","version":1}]`.
Требование дополнительно содержит strength=hard|preference и supersedes.
Заменённое требование переносится в history. Измерения и readiness записывает код.

Схемы строк карты объявлены в `agent/task_state.py: ROW_SCHEMAS`.
Общие типы и строгий валидатор объявлены в `agent/contracts.py`.
Сервер отвергает неизвестные поля, выдуманные источники и подмену согласия.
Программная проверка достаточности не является расчётом прочности.

Снимок учитывает процесс, вершины/индексы mesh, мировую матрицу, локальное
преобразование, размеры, настройки каждого объекта, состав сцены, платформу,
эффективные глобальные настройки и настройки экструдеров. Время и seq событий
не входят в отпечаток. При смене процесса прежние планы устаревают.

Система координат — внутренняя система UM: X/Z в плоскости платформы, Y вверх;
линейные величины в мм. Преобразования абсолютные в локальных координатах узла.
Поворот — нормированный quaternion x,y,z,w. Scale строго положительный.
3MFWriter самостоятельно переводит координаты в систему формата 3MF.

## Аналитика

`GET /api/analytics?days=30` возвращает read-only отчёт. `days` — целое 0…365,
по умолчанию 30; 0 означает всё время. Неизвестные, пустые или повторяющиеся
параметры дают HTTP 400. Применяется та же проверка Host, Origin и токена.

Поля: `generated_at`, `period_days`, `timezone=UTC`, `summary`, `llm`, `runs`,
`tools`, `errors`, `attention`, `timeline`, `workflow_states`, `milestones`,
`sessions`, `notes`. `milestones` — независимые признаки задач, не воронка конверсии.
`analysis_completion_rate` = completed/(completed+failed), без отмен/прерываний;
это доля завершённых анализов, не успешной печати. Неизвестные значения — null.
Сводка, состояния и результаты относятся к задачам с активностью в периоде;
события и запуски ограничены периодом. Сохранённые комплекты — за всю историю
этих задач. Семантика и правила актуальности: [analytics.md](analytics.md).

`GET /api/analytics/export?days=30` возвращает `text/csv; charset=utf-8`,
Content-Disposition attachment, UTF-8 BOM, разделитель `;`. Только строки задач,
без полного журнала. Названия экранируются против формул Excel; секреты
редактируются до формирования CSV. Выгрузка не учитывает фильтры таблицы SPA.

Новые события `llm_request` содержат purpose, model, attempt, http_status,
outcome, code, duration_ms и usage (prompt_tokens/completion_tokens/total_tokens).
Одно событие на отправленную HTTP-попытку; ожидание retry не входит в duration.
Ошибка наблюдателя метрик не меняет ответ LLM и не запускает повторный запрос.
tool_finished/tool_failed дополнены duration_ms для чтений, применения и экспорта.
События не являются доказательством качества печати.

## MCP

Версия: **2025-06-18**. Транспорт: POST `/mcp`, JSON-ответы; GET даёт 405.
SSE и универсальное подключение внешних MCP-серверов не реализованы.
Основание: [transport](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports),
[lifecycle](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle).

1. Отправить initialize с protocolVersion, capabilities={}, clientInfo.
2. Сохранить полученный Mcp-Session-Id.
3. Отправить notifications/initialized без id. Ответ: HTTP 202 без тела.
4. Передавать MCP-Protocol-Version и Mcp-Session-Id в следующих запросах.
5. Получить tools/list; выполнить tools/call; закрыть сессию через DELETE `/mcp`.

Клиент передаёт Accept: application/json, text/event-stream. Нет обязательного
ID — HTTP 400; неизвестный или отозванный ID — 404. id JSON-RPC сохраняется.
Ошибки: -32700, -32600, -32601, -32602, -32603. Ошибка исполнения инструмента
передаётся в content с isError=true. Списки инструментов и схемы Chat Completions
получаются из одного реестра `agent/tools.py`. Скрытого второго списка нет.

Внешняя MCP-сессия имеет только чтение. Внутренний клиент получает случайный
X-Agent-Run-Token, связанный с сессией, запуском и фазой. Токен не передаётся LLM
или SPA. Его отзыв закрывает связанные MCP-сессии. Изменение требует совпадения
инструмента и аргументов с конкретным согласованным действием. Для load_model
допускается отдельное серверное разрешение существующей операции явного импорта
пользователя; выдуманный file_id или путь не принимается.

Полный набор чтения: get_scene_snapshot, get_printer_capabilities,
get_current_configuration, get_setting_definitions, search_knowledge,
list_materials, find_compatible_profiles, get_operation_status, get_slice_result.
Изменения: load_model, set_print_mode, select_material, select_quality_profile,
set_settings, create_quality_profile, save_quality_profile, set_object_transform,
arrange_objects, remove_objects, start_slicing, save_project, export_gcode.

Сетевой worker не удерживает блокировку SessionStore при ожидании Qt или MCP.
Очередь QTimer выполняет чтение и изменение в главном потоке. Команда, которая
не начала выполнение до тайм-аута, не выполнится позже. После тайм-аута уже
начавшейся команды исход считается неизвестным; автоматического повтора нет.

## LLM и пределы

Chat Completions, stream=false, stdlib urllib. Base URL нормализуется добавлением
/chat/completions один раз. Используется введённая модель. Нет обязательных
response_format для неизвестных провайдеров, strict или современного OpenAI SDK. Полные пары assistant/tool
сохраняются в контексте одного запуска, включая несколько tool_calls.
Контракт карты передаётся сразу. Корректная первая карта проходит обычную проверку
без дополнительного запроса. Иначе для финальной карты tools отключены.
Одна попытка исправления неверного JSON.
Изображения JPG/PNG идут как image_url с data URL. История не сохраняет base64.
Основание: [Chat Completions](https://developers.openai.com/api/reference/resources/chat).

| Предел | Значение |
| --- | --- |
| HTTP LLM | 120 секунд, не больше остатка срока анализа |
| Анализ без ожидания человека | 360 секунд |
| Запросы LLM / инструменты за запуск | 12 / 24 |
| Одинаковые вызовы за запуск | 3 |
| Повторы 429 или 5xx | До 2 |
| Запрос / ответ LLM | 8 / 2 МиБ |
| История сообщений в контексте | Последние 20 пользовательских сообщений и текущая карта |
| Файл / HTTP JSON / описание | 100 МиБ / 1 МиБ / 20 000 символов |
| Каталог / ключи настроек | До 50 строк на страницу / 100 ключей |
| Ожидание очереди Qt / выбора папки | 30 / 300 секунд |
| Ожидание расстановки | 120 секунд; по тайм-ауту проверить сцену |

Контекст LLM содержит актуальные данные и индекс необходимых источников без
полных payload прошлых чтений и заменённых записей карты. Активные требования
и их пользовательские/ручные основания сохраняются; полная история остаётся
на сервере. Observation с неизвестной применимостью показаны с условиями и
missing_fields; полное описание доступно через search_knowledge с conditions.
Применимые и reviewed-записи не обрезаются. Одинаковые чтения кешируются только
в одном анализе; финальное чтение сцены всегда свежее. get_operation_status и
get_slice_result не кешируются. Повтор только уже известных чтений завершает
цикл после полных пар assistant/tool и переводит к структурированному ответу.
llm_request дополнен request_bytes (размер отправленного JSON без его тела).
Событие progress перед запросом показывает фазу llm и номер запроса.

Для точного сочетания `api.timeweb.ai` + `dashscope/qwen3.8-flash` передаются
`enable_thinking=false`, `max_completion_tokens=8192`; в отдельном финальном
запросе также `response_format` с JSON Schema карты. Другим сочетаниям эти
расширения не передаются. Автоматического повтора после тайм-аута нет.
Параметры и агрегаты reasoning_tokens/reasoning_present входят в llm_request;
из response_format записывается только type, без полной схемы и ID источников.
Текст reasoning_content в журнал не записывается. Отключение thinking и JSON
Object уже вернули HTTP 200 в настоящем шлюзе. JSON Schema требует ручного прогона.
Основание:
[Qwen3.8-Flash](https://docs.qwencloud.com/developer-guides/getting-started/latest-model),
[Thinking](https://docs.qwencloud.com/developer-guides/text-generation/thinking).

`agent/output_contract.py` строит схему из ROW_SCHEMAS сервера. Полная схема
добавлена в системный запрос для всех провайдеров. Для Timeweb/Qwen финальный
запрос получает strict JSON Schema с допустимыми парами ID/версии только тех
источников, которые модель видела в текущем контексте и чтениях. Старые полные
источники не добавляются. Неподдерживаемый Qwen keyword uniqueItems удаляется
из передаваемой схемы; сервер проверяет повторы и все прежние ограничения.
При изображениях используется JSON Object с полной схемой в инструкции.
Только явный HTTP 400 «json_schema не поддерживается» допускает один переход
к JSON Object в общем бюджете повторов. Остальные HTTP 400 не повторяются.
Основание: [Qwen Structured output](https://docs.qwencloud.com/developer-guides/text-generation/structured-output).

Невалидная первая JSON-карта считается первой попыткой; затем разрешено одно
исправление. Промежуточный текст без JSON не считается картой. Отклонения
сохраняются как model_output_rejected: phase, request_number, message с путём
поля и причиной, без полного ответа модели. История интерфейса показывает их
отдельно от ошибок инструментов. Схема не подменяет проверку источников,
инженерной достаточности, свежести сцены и пользовательского согласия.

Неизвестный прогресс остаётся null. Нет обещания отменить штатный backend.
Доступность режима в каталоге не подтверждает лицензию 5D.
