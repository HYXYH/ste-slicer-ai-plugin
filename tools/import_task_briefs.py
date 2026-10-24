"""Extract provided DOCX briefs as observations, never reviewed print recipes."""
import hashlib
import json
import os
import sys
import zipfile
from xml.etree import ElementTree

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


def main():
    records = []
    for directory, subdirs, names in os.walk(os.path.join(ROOT, "data")):
        for name in sorted(names):
            if not name.lower().endswith(".docx"):
                continue
            path = os.path.join(directory, name)
            with zipfile.ZipFile(path) as archive:
                info = archive.getinfo("word/document.xml")
                if info.file_size > 4 * 1024 * 1024:
                    raise ValueError("Document XML too large")
                xml = archive.read(info)
            if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
                raise ValueError("Document entities are not supported")
            tree = ElementTree.fromstring(xml)
            text = "\n".join("".join(p.itertext()) for p in tree.findall(".//w:p", NS))
            source = os.path.relpath(path, ROOT).replace("\\", "/")
            records.append({"id": "brief-" + hashlib.sha256(source.encode("utf-8")).hexdigest()[:16], "version": 1,
                            "title": os.path.basename(directory), "status": "observation", "source": {"document": source,
                            "sha256_xml": hashlib.sha256(xml).hexdigest()}, "reviewed_by": None, "reviewed_at": None,
                            "applies_when": [{"field": "task_brief", "op": "eq", "value": os.path.basename(directory)}],
                            "excluded_when": [], "required_facts": ["task_brief"], "affected_decisions": ["requirements", "mode", "material", "orientation"],
                            "recommendations": {}, "limits": {}, "question_templates": [], "calibration_hint": None,
                            "notes": "Предоставленное описание задачи. Не подтверждает пригодность профиля или прочность материала.",
                            "provided_requirements": text})
    destination = os.path.join(ROOT, "plugin", "AIAssistant", "knowledge", "task_briefs.json")
    with open(destination, "w", encoding="utf-8") as target:
        json.dump(records, target, ensure_ascii=False, indent=2)
        target.write("\n")
    print("Imported {} supplied briefs as observations".format(len(records)))
    if "--show" in sys.argv:
        for row in records:
            sys.stdout.buffer.write((row["title"] + "\n" + row["provided_requirements"] + "\n\n").encode("utf-8"))


if __name__ == "__main__":
    main()
