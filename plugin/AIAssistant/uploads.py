"""Bounded upload storage and basic format validation; final validation is in the slicer."""
import os
import struct
import zipfile
import xml.etree.ElementTree as ET

from .session import ApiError, uid, now

MAX_FILE_SIZE = 100 * 1024 * 1024
MODEL_EXTENSIONS = (".stl", ".obj", ".3mf")
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png")
DOCUMENT_EXTENSIONS = (".docx",)


def validate(path, extension):
    size = os.path.getsize(path)
    if not size:
        raise ApiError("Пустой файл")
    with open(path, "rb") as source:
        head = source.read(4096)
        if extension == ".png":
            if not head.startswith(b"\x89PNG\r\n\x1a\n") or size < 33 or head[12:16] != b"IHDR":
                raise ApiError("Некорректный PNG")
            width, height = struct.unpack(">II", head[16:24])
            source.seek(max(0, size - 12))
            if not width or not height or width * height > 100000000 or source.read(12)[4:8] != b"IEND":
                raise ApiError("Некорректный PNG или слишком большое разрешение")
        elif extension in (".jpg", ".jpeg"):
            if not head.startswith(b"\xff\xd8\xff"):
                raise ApiError("Некорректный JPEG")
            source.seek(max(0, size - 2))
            if source.read(2) != b"\xff\xd9":
                raise ApiError("JPEG не завершён")
        elif extension == ".stl":
            binary = size >= 84 and 84 + 50 * struct.unpack("<I", head[80:84])[0] == size
            if binary:
                if size == 84:
                    raise ApiError("STL не содержит треугольников")
            elif not head.lstrip().lower().startswith(b"solid"):
                raise ApiError("Файл не похож на ASCII или бинарный STL")
            else:
                # Inspect the complete ASCII file, not only its header.
                source.seek(0)
                facet = vertex = end = False
                for line in source:
                    clean = line.strip().lower()
                    facet = facet or clean.startswith(b"facet ")
                    vertex = vertex or clean.startswith(b"vertex ")
                    end = end or clean.startswith(b"endsolid")
                if not (facet and vertex and end):
                    raise ApiError("ASCII STL не содержит геометрии или не завершён")
        elif extension == ".obj":
            source.seek(0)
            vertices = faces = False
            for line in source:
                clean = line.lstrip()
                vertices = vertices or clean.startswith(b"v ")
                faces = faces or clean.startswith(b"f ")
                if clean.startswith((b"mtllib ", b"call ", b"csh ")):
                    raise ApiError("OBJ должен содержать геометрию без внешних файлов и команд")
            if not (vertices and faces):
                raise ApiError("OBJ должен содержать вершины и грани")
    if extension == ".3mf":
        try:
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
                if len(names) != len(set(names)) or len(names) > 2000:
                    raise ApiError("Некорректная структура 3MF")
                if "3D/3dmodel.model" not in names or "[Content_Types].xml" not in names:
                    raise ApiError("В 3MF отсутствует описание модели")
                total = 0
                for entry in archive.infolist():
                    name = entry.filename.replace("\\", "/")
                    total += entry.file_size
                    if name.startswith("/") or ".." in name.split("/") or ":" in name:
                        raise ApiError("Недопустимый путь внутри 3MF")
                if total > 400 * 1024 * 1024:
                    raise ApiError("Распакованный 3MF слишком велик")
                # XML is passed to the slicer, never extracted into a user-supplied path.
                with archive.open("3D/3dmodel.model") as source:
                    overlap = b""
                    while True:
                        chunk = source.read(65536)
                        if not chunk:
                            break
                        # Remove NULs to recognize declarations in UTF-16/32 as well.
                        scan = (overlap + chunk).replace(b"\x00", b"").upper()
                        if b"<!DOCTYPE" in scan or b"<!ENTITY" in scan:
                            raise ApiError("Объявления DTD в 3MF не поддерживаются")
                        overlap = chunk[-64:]
                # Parse incrementally to avoid a second in-memory copy of large meshes.
                has_mesh = False
                with archive.open("3D/3dmodel.model") as source:
                    for event, element in ET.iterparse(source, events=("end",)):
                        if element.tag.split("}")[-1] == "triangle":
                            has_mesh = True
                        element.clear()
                if not has_mesh:
                    raise ApiError("3MF не содержит треугольников")
        except (zipfile.BadZipFile, ET.ParseError, RuntimeError, OSError, KeyError, NotImplementedError):
            raise ApiError("Повреждённый или неподдерживаемый 3MF")


def receive(store, session_id, name, stream, length):
    store.get(session_id)
    if not isinstance(name, str) or not name or len(name) > 255 or any(c in name for c in ("/", "\\", "\x00")):
        raise ApiError("Недопустимое имя файла")
    extension = os.path.splitext(name)[1].lower()
    if extension not in MODEL_EXTENSIONS + IMAGE_EXTENSIONS + DOCUMENT_EXTENSIONS:
        raise ApiError("Поддерживаются STL, OBJ, 3MF, JPG, PNG и DOCX. Сохраните старый DOC в формате DOCX", 415)
    if length <= 0 or length > MAX_FILE_SIZE:
        raise ApiError("Размер файла должен быть от 1 байта до 100 МиБ", 413)
    file_id = uid()
    folder = os.path.join(store.root, "uploads", session_id)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, file_id + extension)
    try:
        with open(path, "wb") as target:
            remaining = length
            while remaining:
                chunk = stream.read(min(65536, remaining))
                if not chunk:
                    raise ApiError("Загрузка оборвалась: файл получен не полностью")
                target.write(chunk)
                remaining -= len(chunk)
        validate(path, extension)
        document = {}
        if extension in DOCUMENT_EXTENSIONS:
            from .documents import extract_docx
            document["document_text"] = extract_docx(path)
            document["text_characters"] = len(document["document_text"])
        return store.add_file(session_id, {
            "id": file_id, "name": name, "extension": extension, "size": length,
            "kind": "model" if extension in MODEL_EXTENSIONS else "document" if extension in DOCUMENT_EXTENSIONS else "image",
            "path": path, "created_at": now(), "status": "saved", "error": None,
            "scene_present": False, "object_ids": [], **document})
    except Exception:
        if os.path.exists(path):
            os.remove(path)
        raise
