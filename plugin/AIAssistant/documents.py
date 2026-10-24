"""Bounded DOCX text extraction and shared, source-labelled task input."""
import zipfile
import xml.etree.ElementTree as ET

from .session import ApiError

MAX_XML = 4 * 1024 * 1024
MAX_TEXT = 30000
MAX_TASK_TEXT = 120000
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def extract_docx(path):
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            names = [e.filename for e in entries]
            if len(entries) > 2000 or len(names) != len(set(names)) or sum(e.file_size for e in entries) > 100 * 1024 * 1024:
                raise ApiError("Документ слишком велик или имеет некорректную структуру")
            entry = archive.getinfo("word/document.xml")
            if entry.file_size > MAX_XML:
                raise ApiError("Текстовая часть DOCX превышает 4 МиБ")
            raw = archive.read(entry)
        scan = raw.replace(b"\x00", b"").upper()
        if b"<!DOCTYPE" in scan or b"<!ENTITY" in scan:
            raise ApiError("Объявления DTD и сущностей в DOCX не поддерживаются")
        body = ET.fromstring(raw).find(W + "body")
        if body is None:
            raise ApiError("В DOCX отсутствует текст документа")

        def paragraph(element):
            return "".join((node.text or "") if node.tag == W + "t" else "\t" if node.tag == W + "tab" else "\n"
                           for node in element.iter() if node.tag in (W + "t", W + "tab", W + "br", W + "cr"))

        blocks = []
        for element in body:
            if element.tag == W + "tbl":
                for row in element.findall(W + "tr"):
                    blocks.append("\t".join(" / ".join(paragraph(p) for p in cell.iter(W + "p")) for cell in row.findall(W + "tc")))
            else:
                blocks.extend(paragraph(p) for p in ([element] if element.tag == W + "p" else element.iter(W + "p")))
        result = "\n".join(block for block in blocks if block.strip()).strip()
        if not result:
            raise ApiError("DOCX не содержит доступного текста. Документы из сканов требуют распознавания")
        if len(result) > MAX_TEXT:
            raise ApiError("Текст DOCX превышает 30 000 символов. Разделите ТЗ на документы меньшего размера", 413)
        return result
    except (zipfile.BadZipFile, ET.ParseError, KeyError, RuntimeError, OSError, NotImplementedError) as exc:
        raise ApiError("Повреждённый или неподдерживаемый DOCX") from exc


def message_text(session, message):
    parts = [message.get("text") or ""]
    for fid in message.get("file_ids", []):
        attachment = session["files"].get(fid, {})
        if attachment.get("kind") == "document":
            parts.append("Вложение: {} (source_id={})\n{}".format(attachment["name"], fid, attachment["document_text"]))
    return "\n\n".join(p for p in parts if p)


def task_input(session):
    return "\n\n".join(message_text(session, m) for m in session["messages"] if m["role"] == "user")
