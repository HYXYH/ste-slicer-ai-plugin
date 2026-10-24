"""Prompt projections only. The complete history and evidence stay in SessionStore."""
import copy


def compact_result(name, result):
    value = copy.deepcopy(result)
    if name == "search_knowledge":
        for row in value.get("data", {}).get("items", []):
            if row.get("status") == "observation" and row.get("applicability") != "pass":
                if "provided_requirements" in row:
                    row.pop("provided_requirements")
                    row["omitted_fields"] = ["provided_requirements"]
                    row["details_available"] = "search_knowledge: укажите известные conditions из applies_when"
    return value


def analysis_context(session, current_data):
    task = copy.deepcopy(session["task_map"])
    history = task.pop("history", [])
    task["history_count"] = len(history)
    for fact in task.get("facts", []):
        if fact.get("key") == "scene":
            fact["value"] = {"details_in": "current_data.scene", "snapshot_ref": task.get("snapshot_ref")}
    if "catalog" in task.get("resources", {}):
        task["resources"]["catalog"] = {"details_in": "current_data.materials"}
    data = {key: compact_result("search_knowledge" if key == "knowledge" else key, value)
            for key, value in current_data.items()}
    sources = session["sources"]
    selected, current_refs = set(), set()
    def references(value):
        if isinstance(value, str) and value in sources:
            selected.add(value)
        elif isinstance(value, dict):
            for item in value.values():
                references(item)
        elif isinstance(value, list):
            for item in value:
                references(item)
    references(task)
    for result in data.values():
        for ref in result.get("source_refs", []):
            selected.add(ref["id"])
            current_refs.add(ref["id"])
        for row in result.get("data", {}).get("items", []):
            if row.get("usable_as_evidence") and row.get("id") in sources:
                selected.add(row["id"])
                current_refs.add(row["id"])
    messages = [m for m in session["messages"] if m["role"] == "user"][-20:]
    documents = [{"id": fid, "name": session["files"][fid]["name"], "text": session["files"][fid]["document_text"]}
                 for fid in dict.fromkeys(fid for m in session["messages"] if m["role"] == "user" for fid in m.get("file_ids", []))
                 if session["files"].get(fid, {}).get("kind") == "document"]
    message_ids = set(m["id"] for m in messages)
    selected.update(message_ids & set(sources))
    selected.update(d["id"] for d in documents if d["id"] in sources)
    index = {}
    for sid in sorted(selected):
        source = sources.get(sid)
        if source is None:
            continue
        row = {key: copy.deepcopy(value) for key, value in source.items() if key not in ("data", "text")}
        if source.get("kind") == "tool":
            row["historical"] = sid not in current_refs
            row["snapshot_ref"] = source.get("data", {}).get("snapshot_ref")
        elif source.get("kind") == "knowledge" and sid not in current_refs:
            row["data"] = copy.deepcopy(source.get("data"))
        if source.get("kind") == "user":
            if source.get("file_id"):
                row["details_in"] = "documents"
            elif sid in message_ids:
                row["details_in"] = "user_messages"
            else:
                # Keep older user evidence when active requirements still cite it.
                row["text"] = source.get("text", "")
        index[sid] = row
    return {"task_map": task, "sources": index, "current_data": data, "documents": documents,
            "context_notes": ["Полная история и ответы инструментов сохранены на сервере.",
                              "sources — индекс источников; актуальные измерения находятся в current_data.",
                              "Исторические измерения не подтверждают текущую сцену. Запросите недостающие данные инструментами.",
                              "Описания с applicability=unknown показаны без требований: сначала уточните условия применимости."]}
