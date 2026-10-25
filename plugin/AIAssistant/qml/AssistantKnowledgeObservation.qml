import QtQuick 2.7
import QtQuick.Controls 2.2
import QtQuick.Layouts 1.3

Rectangle {
    id: card
    property var entry: ({})
    property var catalog: []
    property var backend: null
    property bool editing: false
    property bool editDefects: false
    property var selected: []
    property string error: ""
    property string largePhoto: ""
    implicitHeight: body.height + 28
    color: entry.excluded ? "#f3f5f8" : "#f5faff"; radius: 10; border.color: "#dceaf5"
    function dimension(name, index) {
        var values = (entry.measurements || {})[name] || [];
        return values[index] === null || values[index] === undefined ? "" : String(values[index]);
    }
    function parseDimension(text) {
        if (!text.trim()) return null;
        var value=Number(text.trim().replace(",", "."));
        if (!isFinite(value) || value <= 0) throw "Размеры должны быть положительными числами в мм.";
        return value;
    }
    function startEdit() {
        selected=(entry.defects || []).slice(0); note.text=entry.text || "";
        result.currentIndex=["good","defects","not_checked","unknown"].indexOf(entry.result);
        ex.text=dimension("expected_mm",0); ey.text=dimension("expected_mm",1); ez.text=dimension("expected_mm",2);
        ax.text=dimension("actual_mm",0); ay.text=dimension("actual_mm",1); az.text=dimension("actual_mm",2);
        reason.text=""; error=""; editing=true;
    }
    function toggle(id, checked) {
        var next=selected.slice(0), index=next.indexOf(id);
        if (checked && index < 0) next.push(id);
        if (!checked && index >= 0) next.splice(index,1);
        selected=next;
    }
    function save(excluded) {
        if (!reason.text.trim()) { error="Укажите причину изменения. История правок сохранится."; return; }
        try {
            var outcome=["good","defects","not_checked","unknown"][result.currentIndex];
            backend.reviseKnowledgeObservation(entry.id,{result:outcome,text:note.text,defects:outcome === "defects" ? selected : [],
                excluded:excluded,reason:reason.text.trim(),measurements:{expected_mm:[parseDimension(ex.text),parseDimension(ey.text),parseDimension(ez.text)],
                    actual_mm:[parseDimension(ax.text),parseDimension(ay.text),parseDimension(az.text)]}});
        } catch(e) { error=String(e); }
    }
    Column {
        id:body; anchors.left:parent.left; anchors.right:parent.right; anchors.top:parent.top; anchors.margins:14; spacing:10
        Text { width:parent.width; text:card.entry.status_label || "Наблюдение"; textFormat:Text.PlainText; color:card.entry.excluded ? "#8392a6" : "#236e9b"; font.bold:true; font.pixelSize:13; wrapMode:Text.Wrap }
        Text { width:parent.width; text:card.entry.date || ""; color:"#8b9eb4"; font.pixelSize:10 }
        Text { width:parent.width; text:card.entry.text || "Описание не добавлено"; textFormat:Text.PlainText; color:"#46617c"; wrapMode:Text.Wrap; font.pixelSize:12 }
        Repeater { model:card.entry.defect_labels || []; Text { width:body.width; text:"• " + modelData; textFormat:Text.PlainText; wrapMode:Text.Wrap; color:"#a66e36"; font.pixelSize:12 } }
        Text { width:parent.width; text:"По модели: " + [card.dimension("expected_mm",0)||"?",card.dimension("expected_mm",1)||"?",card.dimension("expected_mm",2)||"?"].join(" × ") + " мм\nИзмерено: " + [card.dimension("actual_mm",0)||"?",card.dimension("actual_mm",1)||"?",card.dimension("actual_mm",2)||"?"].join(" × ") + " мм"; color:"#7890a8"; wrapMode:Text.Wrap; font.pixelSize:11 }
        Flow {
            width:parent.width; spacing:8
            Repeater {
                model:card.entry.photos || []
                Column {
                    width:108; spacing:4
                    AssistantKnowledgeImage { width:108; height:82; imageSource:modelData.url || ""; MouseArea { anchors.fill:parent; cursorShape:Qt.PointingHandCursor; onClicked:card.largePhoto=card.largePhoto === modelData.url ? "" : (modelData.url || "") } }
                    Text { width:parent.width; text:modelData.name || "Фото"; textFormat:Text.PlainText; elide:Text.ElideRight; font.pixelSize:10; color:"#8095ac" }
                }
            }
        }
        AssistantKnowledgeImage { width:parent.width; height:280; visible:card.largePhoto !== ""; imageSource:card.largePhoto }
        AssistantButton { text:card.editing ? "Отменить редактирование" : "Исправить / исключить наблюдение"; compact:true; onClicked:card.editing ? card.editing=false : card.startEdit() }
        Column {
            visible:card.editing; width:parent.width; spacing:8
            ComboBox { id:result; width:parent.width; model:["Дефектов нет","Есть дефекты","Не проверялось","Не могу определить"] }
            TextArea { id:note; width:parent.width; height:90; placeholderText:"Уточните наблюдение"; wrapMode:TextEdit.Wrap }
            AssistantButton { text:card.editDefects ? "Скрыть список дефектов" : "Изменить отмеченные дефекты"; compact:true; onClicked:card.editDefects=!card.editDefects }
            Column {
                width:parent.width; visible:card.editDefects && result.currentIndex === 1
                Repeater { model:card.catalog; CheckBox { width:parent.width; text:modelData.label; checked:card.selected.indexOf(modelData.id) >= 0; font.pixelSize:11; onClicked:card.toggle(modelData.id,checked) } }
            }
            Text { text:"Размеры по модели / измерено, X · Y · Z (мм)"; color:"#7890a8"; font.pixelSize:10 }
            RowLayout { width:parent.width; TextField { id:ex; Layout.fillWidth:true; placeholderText:"X" } TextField { id:ey; Layout.fillWidth:true; placeholderText:"Y" } TextField { id:ez; Layout.fillWidth:true; placeholderText:"Z" } }
            RowLayout { width:parent.width; TextField { id:ax; Layout.fillWidth:true; placeholderText:"X" } TextField { id:ay; Layout.fillWidth:true; placeholderText:"Y" } TextField { id:az; Layout.fillWidth:true; placeholderText:"Z" } }
            TextField { id:reason; width:parent.width; placeholderText:"Причина исправления или исключения"; maximumLength:1000 }
            Text { width:parent.width; visible:card.error !== ""; text:card.error; wrapMode:Text.Wrap; color:"#ba6643"; font.pixelSize:11 }
            Flow {
                width:parent.width; spacing:8
                AssistantButton { text:"Сохранить исправление"; primary:true; onClicked:card.save(!!card.entry.excluded) }
                AssistantButton { text:card.entry.excluded ? "Вернуть в рекомендации" : "Исключить из рекомендаций"; onClicked:card.save(!card.entry.excluded) }
            }
        }
        Text { width:parent.width; visible:(card.entry.revisions || []).length > 0; text:"История правок · " + (card.entry.revisions || []).length; font.bold:true; color:"#6d87a1"; font.pixelSize:11 }
        Repeater { model:card.entry.revisions || []; Text { width:body.width; text:modelData.date + " · " + modelData.reason + "\n" + modelData.result + (modelData.excluded ? " · исключено" : "") + (modelData.text ? "\n" + modelData.text : "") + ((modelData.defects || []).length ? "\n" + modelData.defects.join("; ") : ""); textFormat:Text.PlainText; wrapMode:Text.Wrap; color:"#8598ac"; font.pixelSize:10 } }
    }
}
