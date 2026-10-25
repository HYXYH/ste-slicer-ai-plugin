"""Author original SVG teaching diagrams and the shared static defect catalog (project venv)."""
import json
import os
import runpy
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, "plugin", "AIAssistant")
ROWS = runpy.run_path(os.path.join(PLUGIN, "print_diagnostics.py"))["ROWS"]
META = {
    "adhesion": ("Bed adhesion", "Деталь полностью или частично отделяется от стола ещё во время печати.", "Основание приподнято; деталь двигается или тянется за соплом."),
    "warping": ("Warping", "Края поднимаются при остывании, хотя центральная часть может держаться.", "Углы загнуты вверх; между углом и столом заметен зазор."),
    "bottom_gaps": ("First-layer gaps", "Линии первого слоя не соединяются в сплошное основание.", "Снизу видны отдельные дорожки с промежутками."),
    "elephant_foot": ("Elephant’s foot", "Нижние слои выступают за контур остальной детали.", "Расширение расположено у стола, верхние стенки уже."),
    "underextrusion": ("Under-extrusion", "Экструзия местами не заполняет рассчитанную траекторию.", "В стенках есть поры, разрывы и пропуски дорожек."),
    "blobs": ("Blobs / zits", "На поверхности остаются локальные избытки пластика.", "Капли, бугорки или наплывы на стенках и шве."),
    "ringing": ("Ringing / banding", "После резких изменений движения или по высоте повторяются волны.", "Эхо угла отличается от равномерных горизонтальных полос; причины могут быть разными."),
    "layer_shift": ("Layer shifting", "Начиная с некоторой высоты слой печатается со смещением.", "Внешний контур резко образует ступеньку в сторону."),
    "delamination": ("Layer delamination", "Соседние слои плохо соединены и разделяются.", "Трещина проходит вдоль слоёв; деталь может раскалываться по ней."),
    "top_holes": ("Pillowing / top holes", "Верхняя оболочка не закрывает заполнение сплошной поверхностью.", "В верхней плоскости остаются открытые провалы или отверстия."),
    "top_gaps": ("Top-surface gaps", "Соседние верхние дорожки не стыкуются.", "Тонкие щели между линиями; отличайте их от провалов над заполнением."),
    "top_ridges": ("Top-surface ridges", "На верхней плоскости поднимаются гребни или остаются царапины.", "Борозды вдоль линий либо следы сопла при холостых перемещениях."),
    "stringing": ("Stringing", "При переходах между участками вытягиваются тонкие нити пластика.", "Паутинка между стойками или отдельными деталями до зачистки."),
    "small_features": ("Overheated details", "Небольшие элементы не сохраняют исходную форму.", "Вершины расплываются, стойки слипаются или наклоняются."),
    "closed_holes": ("Closed holes / negative features", "Отверстия и узкие зазоры оказываются меньше нужного или перекрыты.", "Различайте сплошное сужение отверстия и отдельные нити поперёк него."),
    "overhangs": ("Poor overhangs", "Нижняя поверхность наклонного участка не удерживает форму.", "У нависания свисают петли, край неровный или рваный."),
    "bridges": ("Sagging bridges", "Горизонтальный участок между опорами провисает.", "Дорожки моста образуют дуги вниз или касаются поверхности под ними."),
    "dimensions": ("Dimensional error", "Измеренные габариты или посадки отличаются от требуемых.", "Сравните X/Y/Z с моделью, повторите измерения и учитывайте инструмент."),
}


def diagram(key):
    lines = ''.join('<path d="M90 {0} H310"/>'.format(y) for y in range(86, 190, 12))
    cube = '<rect x="90" y="70" width="220" height="132" rx="4" fill="#d8effc"/>' + lines
    if key == "adhesion":
        return '<g transform="rotate(-9 200 135)">' + cube + '</g><path class="bad" d="M92 211 Q200 177 312 199"/><path class="bad" d="M180 229 V209 M172 217 L180 209 188 217"/>'
    if key == "warping":
        return ''.join('<path d="M75 {0} Q85 {1} 110 {2} H290 Q315 {1} 325 {0}"/>'.format(165+i, 195+i, 195+i) for i in range(0, 31, 10)) + '<path class="bad" d="M74 166 Q84 197 110 196 H290 Q314 197 326 166"/>'
    if key == "bottom_gaps":
        return '<rect x="70" y="65" width="260" height="145" rx="8" fill="#e4f2fa"/>' + ''.join('<path d="M{0} 78 L{1} 198"/>'.format(x, x+45) for x in range(80, 290, 20)) + '<path class="bad" d="M160 80 L205 196"/>'
    if key == "elephant_foot":
        return cube + '<path class="bad" d="M90 188 L66 210 H334 L310 188"/><path d="M70 202 H330"/>'
    if key == "underextrusion":
        return cube + '<g fill="#fff" stroke="#f08b43"><ellipse cx="142" cy="123" rx="22" ry="8"/><ellipse cx="248" cy="159" rx="29" ry="9"/><ellipse cx="268" cy="98" rx="18" ry="7"/></g>'
    if key == "blobs":
        return cube + '<g class="bad" fill="#ffcc9e"><circle cx="307" cy="103" r="12"/><circle cx="301" cy="156" r="15"/><circle cx="151" cy="135" r="11"/><circle cx="206" cy="91" r="8"/></g>'
    if key == "ringing":
        return cube + ''.join('<path class="bad" d="M{0} 73 q-15 15 0 30 t0 30 t0 30 t0 30"/>'.format(x) for x in (118, 143, 168))
    if key == "layer_shift":
        return '<rect x="80" y="140" width="200" height="63" fill="#d8effc"/><rect x="120" y="65" width="200" height="75" fill="#d8effc"/>' + ''.join('<path d="M80 {0} H280"/>'.format(y) for y in (151, 165, 179, 193)) + ''.join('<path d="M120 {0} H320"/>'.format(y) for y in (77, 91, 105, 119)) + '<path class="bad" d="M80 140 H320"/><path class="bad" d="M80 130 H119 M110 122 L119 130 110 138"/>'
    if key == "delamination":
        return cube + '<path d="M90 130 H310" stroke="#f8fbff" stroke-width="16"/><path class="bad" d="M90 126 L132 129 172 122 209 130 250 124 310 129"/>'
    if key in ("top_holes", "top_gaps", "top_ridges"):
        top = '<path d="M70 108 L135 65 H328 L270 111 V196 H70 Z" fill="#d8effc"/><path d="M70 108 H270 L328 65 M270 108 V195"/>'
        hatch = ''.join('<path d="M{0} 106 l57 -38"/>'.format(x) for x in range(85, 260, 14))
        if key == "top_holes":
            return top + hatch + '<path class="bad" d="M154 84 L178 72 206 81 186 98 Z M232 86 L251 76 269 88 248 101 Z" fill="#fff"/>'
        if key == "top_gaps":
            return top + hatch + '<path class="bad" d="M133 108 l57 -38 M189 108 l57 -38 M245 108 l57 -38"/>'
        return top + hatch + '<path class="bad" stroke-width="8" d="M130 104 l57 -38 M202 104 l57 -38"/>'
    if key == "stringing":
        return '<rect x="78" y="65" width="55" height="143" rx="4" fill="#d8effc"/><rect x="267" y="65" width="55" height="143" rx="4" fill="#d8effc"/>' + ''.join('<path class="bad" stroke-width="2" d="M133 {0} Q194 {1} 267 {2}"/>'.format(y, y+30, y+6) for y in (77, 98, 120, 148, 175))
    if key == "small_features":
        return '<path d="M78 207 V154 L102 82 126 154 V207 M178 207 V143 L199 65 222 143 V207" fill="#d8effc"/><path class="bad" d="M259 207 V137 Q239 78 277 87 Q310 80 306 121 Q330 159 306 170 V207" fill="#ffcc9e"/><path class="bad" d="M104 81 Q147 63 157 113"/>'
    if key == "closed_holes":
        return '<rect x="65" y="65" width="270" height="143" rx="13" fill="#d8effc"/><circle cx="130" cy="135" r="35" fill="#fff"/><circle cx="247" cy="135" r="35" fill="#ffcc9e" class="bad"/><circle cx="247" cy="135" r="13" fill="#fff"/><path class="bad" d="M110 112 L153 160 M104 142 L159 121"/>'
    if key == "overhangs":
        return '<path d="M80 207 V166 L213 72 H300 L168 166 V207 Z" fill="#d8effc"/>' + ''.join('<path d="M{0} {1} H{2}"/>'.format(90+i*15, 160-i*10, 173+i*15) for i in range(8)) + '<path class="bad" d="M170 166 q18 32 28 -4 q14 30 24 -13 q15 28 22 -14 q17 27 25 -14"/>'
    if key == "bridges":
        return '<path d="M70 207 V108 H122 V207 M278 207 V108 H330 V207" fill="#d8effc"/><path d="M72 100 H329 M72 90 H329"/>' + ''.join('<path class="bad" d="M122 {0} Q200 {1} 278 {0}"/>'.format(111+i, 180+i) for i in (0, 8, 16))
    return cube + '<path class="bad" d="M90 220 H310 M90 214 V226 M310 214 V226 M325 70 V202 M319 70 H331 M319 202 H331"/><text x="200" y="247" text-anchor="middle" stroke="none" fill="#d87832" font-size="17">20 мм → 20,8 мм</text>'


def main():
    folder = os.path.join(PLUGIN, "qml", "defects")
    os.makedirs(folder, exist_ok=True)
    records = []
    for key, group, title, pattern, choices in ROWS:
        english, description, signs = META[key]
        records.append({"id": key, "group": group, "label": title, "pattern": pattern, "english": english,
                        "description": description, "signs": signs, "causes": [a[0] for a in choices],
                        "choices": [{"id": str(i), "symptom": a[0], "key": a[1], "direction": a[2], "reason": a[3]} for i, a in enumerate(choices)],
                        "warnings": ["Направления исправления — альтернативы. Меняйте один параметр и повторяйте проверку.",
                                     "Соблюдайте ограничения принтера и материала. Проверьте сухость пластика, стол, подачу, засор и механику."] +
                                    (["Знак Z-offset и XY-компенсаций зависит от принтера и слайсера. Не допускайте контакта сопла со столом."] if key in ("bottom_gaps", "elephant_foot", "closed_holes", "dimensions") else []),
                        "illustration": {"path": "defects/" + key + ".svg", "type": "schematic", "author": "Проект STE Slicer AI Assistant", "license": "CC0-1.0"},
                        "source": "kalibrovka_nastroiki_printera.md"})
        svg = '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="280" viewBox="0 0 400 280"><rect width="400" height="280" rx="16" fill="#f0f7fd"/><defs><style>path,rect,circle,ellipse{stroke:#3689bb;stroke-width:3;stroke-linecap:round;stroke-linejoin:round} .bad{stroke:#ef8a43;stroke-width:4}</style></defs><path d="M45 228 H355" stroke="#9bb7ce"/><g fill="none">' + diagram(key) + '</g><circle cx="74" cy="260" r="4" fill="#3689bb" stroke="none"/><text x="86" y="265" fill="#718ca2" font-family="Segoe UI,sans-serif" font-size="11">слои</text><circle cx="168" cy="260" r="4" fill="#ef8a43" stroke="none"/><text x="180" y="265" fill="#718ca2" font-family="Segoe UI,sans-serif" font-size="11">признак дефекта · схема</text></svg>'
        # Qt's SVG renderer differs in CSS support across bundled versions. Use explicit attributes.
        ET.register_namespace("", "http://www.w3.org/2000/svg")
        tree = ET.fromstring(svg)
        for child in list(tree):
            if child.tag.endswith("defs"):
                tree.remove(child)
        def explicit(element, stroke="#3689bb", thickness="3"):
            if element.get("class") == "bad":
                stroke, thickness = "#ef8a43", "4"
                del element.attrib["class"]
            stroke, thickness = element.get("stroke", stroke), element.get("stroke-width", thickness)
            if element.tag.rsplit("}", 1)[-1] in ("path", "rect", "circle", "ellipse"):
                element.set("stroke", stroke)
                element.set("stroke-width", thickness)
                element.set("stroke-linecap", "round")
                element.set("stroke-linejoin", "round")
            for child in element:
                explicit(child, stroke, thickness)
        explicit(tree)
        list(tree)[0].set("stroke", "none")
        with open(os.path.join(folder, key + ".svg"), "w", encoding="utf-8") as target:
            target.write(ET.tostring(tree, encoding="unicode"))
    with open(os.path.join(folder, "placeholder.svg"), "w", encoding="utf-8") as target:
        target.write('<svg xmlns="http://www.w3.org/2000/svg" width="400" height="280"><rect width="400" height="280" rx="16" fill="#edf5fc"/><path d="M130 175 L175 115 215 150 245 110 280 175Z" fill="#cadfeb"/><text x="200" y="220" text-anchor="middle" font-family="sans-serif" font-size="14" fill="#7c96ac">Схема недоступна</text></svg>')
    os.makedirs(os.path.join(PLUGIN, "defects"), exist_ok=True)
    with open(os.path.join(PLUGIN, "defects", "catalog.json"), "w", encoding="utf-8") as target:
        json.dump(records, target, ensure_ascii=False, indent=2)
    with open(os.path.join(folder, "LICENSE.txt"), "w", encoding="utf-8") as target:
        target.write("Original schematic illustrations by STE Slicer AI Assistant project. CC0-1.0.\nNot photographs; diagrams are illustrative and not measurements.\n")


if __name__ == "__main__":
    main()
