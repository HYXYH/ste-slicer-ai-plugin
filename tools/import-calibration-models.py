"""Import only supported fixed-temperature STL tests from the supplied archive."""
import hashlib
import json
import os
import re
import struct
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEST = os.path.join(ROOT, "plugin", "AIAssistant", "models")
MODELS = [
    ("universal", "ksr_fdmtest_v4.stl", "Универсальный FDM-тест", ["adhesion", "extrusion", "dimensions", "stringing", "bridges", "overhangs", "small_features"], 8),
    ("stringing", "string_test_fast_pyramid.stl", "Ретракт: две пирамиды", ["adhesion", "stringing", "small_features"], 1),
    ("bridges", "bridging_test.stl", "Мосты", ["adhesion", "extrusion", "bridges"], 2),
    ("overhangs", "overhang_test.stl", "Нависания", ["adhesion", "overhangs"], 2),
    ("dimensions", "dimensional_accuracy_test.stl", "Размерная точность", ["adhesion", "dimensions", "extrusion"], 2),
    ("fine", "fine_positive_features.stl", "Мелкие элементы", ["adhesion", "small_features"], 2),
    ("cube", "cube-20mm.stl", "Куб 20 мм", ["adhesion", "dimensions", "extrusion"], 1)]


def geometry(raw):
    count = struct.unpack_from("<I", raw, 80)[0] if len(raw) >= 84 else 0
    if 84 + count * 50 == len(raw):
        vertices = [struct.unpack_from("<3f", raw, 84 + i * 50 + 12 + j * 12) for i in range(count) for j in range(3)]
    else:
        vertices = [tuple(float(v) for v in match) for match in re.findall(rb"vertex\s+([-+\d.eE]+)\s+([-+\d.eE]+)\s+([-+\d.eE]+)", raw)]
    if not vertices:
        raise ValueError("STL has no vertices")
    return [round(max(v[i] for v in vertices) - min(v[i] for v in vertices), 4) for i in range(3)]


def main(archive):
    rows = []
    with zipfile.ZipFile(archive) as source:
        for identifier, filename, title, checks, rank in MODELS:
            member = next((n for n in source.namelist() if n.endswith("/" + filename)), None)
            if identifier == "cube":
                with open(os.path.join(DEST, filename), "rb") as handle:
                    raw = handle.read()
            else:
                if member is None:
                    raise ValueError(filename)
                raw = source.read(member)
                with open(os.path.join(DEST, filename), "wb") as handle:
                    handle.write(raw)
            rows.append({"id": identifier, "file": filename, "title": title, "checks": checks,
                         "not_checked": [k for k in ["adhesion", "extrusion", "dimensions", "stringing", "bridges", "overhangs", "small_features", "bonding"] if k not in checks],
                         "dimensions_mm": geometry(raw), "cost_rank": rank, "modes": ["classic"],
                         "sha256": hashlib.sha256(raw).hexdigest(), "source": member or "Встроенная модель STE Slicer AI Assistant",
                         "measurements": ["expected_mm", "actual_mm"] if "dimensions" in checks else [],
                         "required_settings": {"support_enable": False} if identifier in ("universal", "bridges", "overhangs", "stringing", "fine") else {},
                         "limitations": "Проверяет только отмеченные элементы при постоянной температуре. Не подтверждает прочность или межслойное сцепление под нагрузкой. Для 5D применимость не проверена."})
    with open(os.path.join(DEST, "calibration-catalog.json"), "w", encoding="utf-8") as target:
        json.dump(rows, target, ensure_ascii=False, indent=2)
    print("\n".join(r["id"] + ": " + str(r["dimensions_mm"]) for r in rows))


if __name__ == "__main__":
    main(sys.argv[1])
