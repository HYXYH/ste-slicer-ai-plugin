import QtQuick 2.7
import QtQuick.Controls 2.2
import QtQuick.Layouts 1.3

ColumnLayout {
    id: view
    objectName: "knowledgePage"
    property var backend: null
    property var viewState: ({})
    property var libraryData: viewState.knowledgeData || ({})
    property var options: viewState.knowledgeOptions || ({})
    property var detail: libraryData.detail || ({})
    property string tab: options.tab || "conclusions"
    property bool compact: width < 900
    spacing: 12
    function filter(key,value) { if(backend) backend.filterKnowledge(key,value); }
    function select(tab,id) { if(backend) backend.selectKnowledgeItem(tab,id); }
    function statusValues() { return tab === "prints" ? ["all","good","defects","not_checked","unknown","excluded"] : ["all","hypothesis","observation","confirmed","conflicting"]; }
    function typeValues() { return tab === "prints" ? ["all","ordinary","calibration"] : ["all","configuration","problem","correction"]; }
    RowLayout {
        Layout.fillWidth:true; spacing:8
        Repeater {
            model:[{id:"conclusions",title:"Выводы"},{id:"prints",title:"История печатей"},{id:"defects",title:"Справочник дефектов"},{id:"calibration",title:"Калибровка"}]
            AssistantButton { Layout.fillWidth:true; text:modelData.title; primary:view.tab === modelData.id; onClicked:if(view.backend) view.backend.knowledgeTab(modelData.id) }
        }
        AssistantChatAction { text:"Обновить"; iconSource:"icons/Refresh.svg"; iconOnly:true; onClicked:if(view.backend) view.backend.loadKnowledge() }
    }
    Text { Layout.fillWidth:true; visible:!!view.viewState.knowledgeIssue; text:view.viewState.knowledgeIssue || ""; textFormat:Text.PlainText; wrapMode:Text.Wrap; color:"#b86a39"; font.pixelSize:12 }
    BusyIndicator { Layout.alignment:Qt.AlignHCenter; visible:!!view.viewState.knowledgeLoading; running:visible; implicitHeight:32 }
    Flow {
        Layout.fillWidth:true; Layout.preferredHeight:childrenRect.height; visible:view.tab !== "calibration"; spacing:8
        TextField {
            width:Math.max(180,Math.min(320,parent.width * .34)); height:40; placeholderText:view.tab === "defects" ? "Найти по симптому…" : "Поиск по базе знаний…"
            text:view.options.search || ""; selectByMouse:true; font.pixelSize:12
            background:Rectangle { radius:7; color:"white"; border.color:"#cbddec" }
            onTextChanged:if(text !== (view.options.search || "")) view.filter("search",text)
        }
        ComboBox {
            width:170; height:40; visible:view.tab !== "defects"
            model:["Все принтеры"].concat(view.libraryData.printers || []); property var values:["all"].concat(view.libraryData.printers || [])
            currentIndex:Math.max(0,values.indexOf(view.options.printer || "all")); font.pixelSize:11
            onActivated:view.filter("printer",values[currentIndex])
        }
        ComboBox {
            width:170; height:40; visible:view.tab !== "defects"
            model:["Все материалы"].concat(view.libraryData.materials || []); property var values:["all"].concat(view.libraryData.materials || [])
            currentIndex:Math.max(0,values.indexOf(view.options.material || "all")); font.pixelSize:11
            onActivated:view.filter("material",values[currentIndex])
        }
        ComboBox {
            width:166; height:40; visible:view.tab !== "defects"; font.pixelSize:11
            model:view.tab === "prints" ? ["Любой результат","Дефектов нет","Есть дефекты","Не проверялось","Не могу определить","Исключено"] : ["Все статусы","Гипотеза","Наблюдение","Подтверждено","Противоречиво"]
            currentIndex:Math.max(0,view.statusValues().indexOf(view.options.status || "all")); onActivated:view.filter("status",view.statusValues()[currentIndex])
        }
        ComboBox {
            width:190; height:40; visible:view.tab !== "defects"; font.pixelSize:11
            model:view.tab === "prints" ? ["Все печати","Обычные","Калибровочные"] : ["Все типы выводов","Рабочая конфигурация","Выявленная проблема","Эффективное исправление"]
            currentIndex:Math.max(0,view.typeValues().indexOf(view.options.type || "all")); onActivated:view.filter("type",view.typeValues()[currentIndex])
        }
        ComboBox {
            width:195; height:40; visible:view.tab === "defects"; font.pixelSize:11
            model:["Все категории"].concat(view.libraryData.categories || []); property var values:["all"].concat(view.libraryData.categories || [])
            currentIndex:Math.max(0,values.indexOf(view.options.category || "all")); onActivated:view.filter("category",values[currentIndex])
        }
        ComboBox {
            width:180; height:40; font.pixelSize:11
            model:view.tab === "defects" ? ["Без группировки","По категории"] : ["Без группировки","По принтеру","По материалу","По типу","По статусу"]
            property var values:view.tab === "defects" ? ["none","group"] : ["none","printer","material","type","status"]
            currentIndex:Math.max(0,values.indexOf(view.options.group_by || "none")); onActivated:view.filter("group_by",values[currentIndex])
        }
    }
    RowLayout {
        Layout.fillWidth:true; visible:view.tab !== "calibration"
        Text { Layout.fillWidth:true; text:"Показано " + (view.libraryData.shown || 0) + " из " + (view.libraryData.total || 0); color:"#859ab1"; font.pixelSize:11 }
        AssistantButton { visible:!!view.detail.kind; compact:true; text:"К списку"; onClicked:view.select(view.tab,"") }
    }
    RowLayout {
        Layout.fillWidth:true; Layout.fillHeight:true; visible:view.tab !== "calibration"; spacing:12
        Item {
            Layout.fillWidth:true; Layout.fillHeight:true; visible:!view.compact || !view.detail.kind
            Rectangle {
                anchors.fill:parent; visible:!(view.libraryData.rows || []).length; color:"white"; radius:12; border.color:"#e2edf8"
                Column {
                    anchors.centerIn:parent; width:Math.max(150,parent.width-60); spacing:14
                    Image { source:"icons/Cube.svg"; anchors.horizontalCenter:parent.horizontalCenter; width:48; height:48; opacity:.45 }
                    Text { width:parent.width; text:(view.libraryData.total || 0) > 0 ? "Нет совпадений" : view.tab === "conclusions" ? "Пока нет выводов из опыта" : view.tab === "prints" ? "История ещё не заполнена" : "Каталог загружается"; color:"#2f5578"; font.pixelSize:18; font.bold:true; horizontalAlignment:Text.AlignHCenter; wrapMode:Text.Wrap }
                    Text { width:parent.width; text:(view.libraryData.total || 0) > 0 ? "Измените поиск или фильтры." : "После печати сообщите о результате в чате. Реальные наблюдения появятся здесь; несколько независимых проверок дадут подтверждённый вывод."; color:"#8a9fb4"; font.pixelSize:12; horizontalAlignment:Text.AlignHCenter; wrapMode:Text.Wrap }
                }
            }
            ListView {
                id:list; anchors.fill:parent; clip:true; spacing:10; visible:view.tab !== "defects"
                model:view.libraryData.rows || []; boundsBehavior:Flickable.StopAtBounds
                ScrollBar.vertical:ScrollBar {}
                section.property:"group_label"; section.criteria:ViewSection.FullString
                section.delegate:Text { width:list.width; height:section ? 30 : 0; text:section; color:"#7c96b1"; font.pixelSize:12; font.bold:true; verticalAlignment:Text.AlignVCenter }
                delegate:Rectangle {
                    width:list.width-10; height:summary.height+32; radius:12; color:selectHover.containsMouse ? "#f5faff" : "white"; border.color:view.options.selection === modelData.id ? "#78b8f3" : "#e1edf8"
                    Column {
                        id:summary; x:16; y:16; width:parent.width-32; spacing:9
                        RowLayout {
                            width:parent.width
                            Text { Layout.fillWidth:true; text:modelData.title; textFormat:Text.PlainText; color:"#234766"; font.bold:true; font.pixelSize:14; wrapMode:Text.Wrap }
                            Rectangle { width:badge.implicitWidth+16; height:24; radius:12; color:Qt.rgba(.2,.5,.8,.08); Text { id:badge; anchors.centerIn:parent; text:modelData.status_label; textFormat:Text.PlainText; color:modelData.color; font.pixelSize:10 } }
                        }
                        Text { width:parent.width; text:modelData.description || (modelData.defects || []).join("; ") || "Результат сохранён"; textFormat:Text.PlainText; color:"#6b839c"; font.pixelSize:12; wrapMode:Text.Wrap; maximumLineCount:3; elide:Text.ElideRight }
                        Text { width:parent.width; text:modelData.printer + " · " + modelData.material + " · " + modelData.mode + " · " + modelData.nozzle; textFormat:Text.PlainText; color:"#8a9fb3"; font.pixelSize:10; wrapMode:Text.Wrap }
                        Text { width:parent.width; text:view.tab === "conclusions" ? "Независимых проверок: " + modelData.confirmations + (modelData.counterexamples ? " · противоречащих: " + modelData.counterexamples : "") : modelData.type_label + " · " + modelData.date.substring(0,16).replace("T"," ") + " · фото: " + modelData.photo_count; color:"#7194b3"; font.pixelSize:10; wrapMode:Text.Wrap }
                    }
                    MouseArea { id:selectHover; anchors.fill:parent; hoverEnabled:true; cursorShape:Qt.PointingHandCursor; onClicked:view.select(view.tab,modelData.id) }
                }
            }
            GridView {
                id:gallery; anchors.fill:parent; clip:true; visible:view.tab === "defects"; model:view.libraryData.rows || []
                cellWidth:width/Math.max(1,Math.floor(width/245)); cellHeight:330; boundsBehavior:Flickable.StopAtBounds
                ScrollBar.vertical:ScrollBar {}
                delegate:Rectangle {
                    width:gallery.cellWidth-10; height:318; radius:12; color:"white"; border.color:view.options.selection === modelData.id ? "#78b8f3" : "#dfedf8"
                    Column {
                        x:12; y:12; width:parent.width-24; spacing:8
                        AssistantKnowledgeImage { width:parent.width; height:145; imageSource:modelData.image }
                        Text { width:parent.width; text:modelData.group; textFormat:Text.PlainText; color:"#3a96cb"; font.pixelSize:10; wrapMode:Text.Wrap }
                        Text { width:parent.width; text:modelData.title; textFormat:Text.PlainText; color:"#224765"; font.bold:true; font.pixelSize:13; wrapMode:Text.Wrap; maximumLineCount:3; elide:Text.ElideRight }
                        Text { width:parent.width; text:modelData.status_label; textFormat:Text.PlainText; color:"#8ea4b9"; font.pixelSize:10; elide:Text.ElideRight }
                        Text { width:parent.width; text:"Схема · печатей: " + modelData.related_count; color:"#8ea4b9"; font.pixelSize:10 }
                    }
                    MouseArea { anchors.fill:parent; cursorShape:Qt.PointingHandCursor; onClicked:view.select("defects",modelData.id) }
                }
            }
        }
        Rectangle {
            visible:!!view.detail.kind; Layout.fillHeight:true; Layout.fillWidth:view.compact; Layout.preferredWidth:view.compact ? view.width : Math.min(510,view.width*.48)
            radius:12; color:"white"; border.color:"#dfedf8"
            Flickable {
                id:detailScroll; anchors.fill:parent; anchors.margins:16; clip:true; contentWidth:width; contentHeight:detailBody.height
                boundsBehavior:Flickable.StopAtBounds; ScrollBar.vertical:ScrollBar {}
                Column {
                    id:detailBody; width:detailScroll.width-10; spacing:14
                    Text { width:parent.width; text:view.detail.title || ""; textFormat:Text.PlainText; wrapMode:Text.Wrap; color:"#1c4261"; font.pixelSize:19; font.bold:true }
                    Text { width:parent.width; text:view.detail.status_label || ""; textFormat:Text.PlainText; wrapMode:Text.Wrap; color:view.detail.color || "#2586c6"; font.pixelSize:12; font.bold:true }
                    AssistantKnowledgeImage { visible:view.detail.kind === "defect"; width:parent.width; height:250; imageSource:view.detail.image || "" }
                    Text { visible:view.detail.kind === "defect"; width:parent.width; text:"Схематическая иллюстрация · не фото\nАвтор: проект STE Slicer AI Assistant · CC0-1.0"; color:"#8b9fb5"; font.pixelSize:10; wrapMode:Text.Wrap }
                    Text { width:parent.width; text:view.detail.description || ""; textFormat:Text.PlainText; color:"#67809a"; font.pixelSize:12; wrapMode:Text.Wrap }
                    Text { visible:!!view.detail.signs; width:parent.width; text:"Как распознать: " + (view.detail.signs || ""); textFormat:Text.PlainText; color:"#3f6d90"; font.pixelSize:12; wrapMode:Text.Wrap }
                    Text { visible:view.detail.kind === "conclusion"; width:parent.width; text:"Независимых подтверждений: " + (view.detail.confirmations || 0) + " · противоречащих проверок: " + (view.detail.counterexamples || 0) + "\n" + (view.detail.result || ""); color:"#5988a6"; font.pixelSize:12; wrapMode:Text.Wrap }
                    Text { visible:!!view.detail.scope_note; width:parent.width; text:view.detail.scope_note || ""; color:"#96713f"; font.pixelSize:11; wrapMode:Text.Wrap }
                    Text { visible:!!view.detail.legacy; width:parent.width; text:"Старая калибровка: фактические параметры не сохранились. Неизвестные условия не восстановлены предположениями."; color:"#aa7a3f"; font.pixelSize:11; wrapMode:Text.Wrap }
                    Repeater { model:view.detail.conditions || []; Text { width:detailBody.width; text:modelData.label + ": " + modelData.value; textFormat:Text.PlainText; color:"#7b91a9"; font.pixelSize:11; wrapMode:Text.Wrap } }
                    Text { visible:(view.detail.causes || []).length > 0; text:"Возможные причины"; color:"#315a7d"; font.bold:true; font.pixelSize:13 }
                    Repeater { model:view.detail.causes || []; Text { width:detailBody.width; text:"• " + modelData; textFormat:Text.PlainText; color:"#728ba3"; font.pixelSize:12; wrapMode:Text.Wrap } }
                    Text { visible:(view.detail.choices || []).length > 0; width:parent.width; text:"Альтернативные направления проверки"; color:"#315a7d"; font.bold:true; font.pixelSize:13; wrapMode:Text.Wrap }
                    Repeater { model:view.detail.choices || []; Text { width:detailBody.width; text:(index+1) + ". " + modelData.reason; textFormat:Text.PlainText; color:"#507b9b"; font.pixelSize:12; wrapMode:Text.Wrap } }
                    Repeater { model:view.detail.warnings || []; Text { width:detailBody.width; text:"! " + modelData; textFormat:Text.PlainText; color:"#b17c42"; font.pixelSize:11; wrapMode:Text.Wrap } }
                    Repeater { model:view.detail.evidence || []; Rectangle { width:detailBody.width; height:evidenceText.height+18; radius:8; color:modelData.supporting ? "#edf8f3" : "#fff4e8"; Text { id:evidenceText; x:9; y:9; width:parent.width-18; text:modelData.date.substring(0,16).replace("T"," ") + " · " + modelData.text; color:modelData.supporting ? "#36886d" : "#b17c42"; font.pixelSize:11; wrapMode:Text.Wrap } MouseArea { anchors.fill:parent; cursorShape:Qt.PointingHandCursor; onClicked:view.select("prints",modelData.print_id) } } }
                    Repeater { model:view.detail.observations || []; AssistantKnowledgeObservation { width:detailBody.width; entry:modelData; catalog:view.libraryData.catalog || []; backend:view.backend } }
                    Repeater { model:view.detail.adjustments || []; Column { width:detailBody.width; spacing:5; Text { width:parent.width; text:modelData.title; textFormat:Text.PlainText; color:"#426b8a"; font.pixelSize:12; wrapMode:Text.Wrap } Text { width:parent.width; text:modelData.change + " · " + modelData.state + " · " + modelData.outcome; textFormat:Text.PlainText; color:"#8a9fb5"; font.pixelSize:11; wrapMode:Text.Wrap } } }
                    Text { visible:(view.detail.related || []).length > 0; text:"Связанные записи"; color:"#315a7d"; font.bold:true; font.pixelSize:13 }
                    Repeater { model:view.detail.related || []; AssistantButton { width:detailBody.width; text:modelData.title; compact:true; onClicked:view.select(modelData.tab,modelData.id) } }
                    Button {
                        id:settingsToggle; visible:(view.detail.parameters || []).length > 0; width:parent.width; checkable:true; text:checked ? "Скрыть параметры" : "Показать фактические параметры"
                    }
                    Column { width:parent.width; visible:settingsToggle.checked; spacing:6; Repeater { model:view.detail.parameters || []; Text { width:detailBody.width; text:(modelData.scope ? modelData.scope + " · " : "") + modelData.label + ": " + modelData.value; textFormat:Text.PlainText; color:"#7a91a8"; font.pixelSize:11; wrapMode:Text.Wrap } } }
                }
            }
        }
    }
    Flickable {
        Layout.fillWidth:true; Layout.fillHeight:true; visible:view.tab === "calibration"; clip:true; contentWidth:width; contentHeight:calibrationContent.height
        boundsBehavior:Flickable.StopAtBounds; ScrollBar.vertical:ScrollBar {}
        Column {
            id:calibrationContent; width:parent.width-12; spacing:14
            AssistantAnalyticsCard {
                width:parent.width; title:"От первой печати к подтверждённому опыту"; iconSource:"icons/Cube.svg"
                Text { width:parent.width; text:"Калибровка необязательна. Обычная деталь тоже помогает ассистенту узнавать ваш принтер."; color:"#7291ad"; font.pixelSize:13; wrapMode:Text.Wrap }
                Flow {
                    width:parent.width; spacing:8
                    Repeater {
                        model:["Модель теста","Подготовка и печать","Опрос и измерения","Диагностика","Один параметр","Повторная проверка","Сохранение опыта"]
                        Rectangle { width:Math.max(130,(calibrationContent.width-90)/4); height:76; radius:9; color:index%2 ? "#edf7fc" : "#edf4ff"; Column { anchors.fill:parent; anchors.margins:11; spacing:6; Text { text:(index+1) + " →"; color:"#218ace"; font.pixelSize:17; font.bold:true } Text { width:parent.width; text:modelData; color:"#557c9d"; font.pixelSize:11; wrapMode:Text.Wrap } } }
                    }
                }
            }
            Repeater {
                model:view.libraryData.calibration || []
                AssistantAnalyticsCard { width:calibrationContent.width; title:modelData.title; subtitle:modelData.state; iconSource:"icons/Chip.svg"; Text { width:parent.width; text:modelData.text; textFormat:Text.PlainText; color:"#6d8aa5"; font.pixelSize:13; wrapMode:Text.Wrap } }
            }
        }
    }
    onDetailChanged:detailScroll.contentY=0
}
