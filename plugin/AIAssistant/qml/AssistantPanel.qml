import QtQuick 2.7
import QtQuick.Controls 2.2
import QtQuick.Layouts 1.3

Rectangle {
    id: root
    objectName: "assistantPanel"
    property var backend: null
    property var v: backend ? backend.view : ({})
    property var assessment: v.assessment || ({})
    property string modal: ""
    property var selectedChats: []
    property bool details: false
    property int settingsVersion: -1
    property bool followTail: true
    color: "#f1f7fb"
    implicitWidth: 1000
    implicitHeight: 720
    function act(name) { if (backend) backend[name](); }
    function fillSettings() {
        var c = v.config || {};
        baseUrl.text = c.base_url || ""; modelName.text = c.model || ""; secret.text = "";
        pipeline.currentIndex = c.pipeline === "single" ? 1 : 0;
        settingsVersion = v.configVersion;
    }
    function toggleChat(id, checked) {
        var next = selectedChats.slice(0), at = next.indexOf(id);
        if (checked && at < 0) next.push(id);
        if (!checked && at >= 0) next.splice(at, 1);
        selectedChats = next;
    }
    onVChanged: {
        if (modal === "settings" && settingsVersion !== v.configVersion) fillSettings();
        if (v.prompt && v.prompt.kind) { promptInput.text = ""; modal = "prompt"; }
        tailTimer.restart();
    }
    Connections {
        target: root.backend || null
        onDraftChanged: if (root.backend) composer.text = root.backend.draft
    }
    Timer { id: tailTimer; interval: 20; onTriggered: if (root.followTail) conversation.contentY = Math.max(0, conversation.contentHeight - conversation.height); }
    DropArea {
        anchors.fill: parent
        enabled: !root.v.analytics && !root.v.busy
        onDropped: if (drop.hasUrls && root.backend) root.backend.uploadUrls(drop.urls)
    }
    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 14
        spacing: 12
        RowLayout {
            Layout.fillWidth: true
            spacing: 10
            Image { source: "Robot.svg"; Layout.preferredWidth: 45; Layout.preferredHeight: 45 }
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 3
                Text { text: root.v.analytics ? "Аналитика подготовки" : "AI Print Assistant"; color: "#123858"; font.family: "Segoe UI"; font.pixelSize: 20; font.bold: true }
                Text { Layout.fillWidth: true; text: root.v.title || "Опишите задачу — ассистент подберёт параметры печати"; color: "#7d8e9f"; font.pixelSize: 11; elide: Text.ElideRight }
            }
            AssistantButton { visible: root.width < 850 && !root.v.analytics; text: root.details ? "Чат" : "Параметры"; onClicked: root.details = !root.details }
        }
        Rectangle {
            visible: (root.v.notice || "") !== ""
            Layout.fillWidth: true
            implicitHeight: noticeText.implicitHeight + 20
            color: "#fff3e4"; radius: 7
            Text { id: noticeText; anchors.fill: parent; anchors.margins: 10; text: root.v.notice || ""; textFormat: Text.PlainText; wrapMode: Text.Wrap; color: "#96532c"; font.pixelSize: 12 }
        }
        RowLayout {
            Layout.fillWidth: true; Layout.fillHeight: true
            spacing: 12
            Rectangle {
                Layout.preferredWidth: 94; Layout.minimumWidth: 94; Layout.maximumWidth: 94; Layout.fillHeight: true
                color: "#fafcfe"; radius: 9
                Column {
                    anchors.top: parent.top; anchors.left: parent.left; anchors.right: parent.right; anchors.margins: 12
                    spacing: 12
                    Repeater {
                        model: [{title:"Новый чат",icon:"NewChat.svg",action:"new"}, {title:"Все чаты",icon:"Chats.svg",action:"chats"}, {title:"LLM",icon:"Settings.svg",action:"settings"}, {title:root.v.analytics ? "Назад" : "Аналитика",icon:root.v.analytics ? "Back.svg" : "Analytics.svg",action:"analytics"}]
                        AssistantNavButton {
                            width: parent.width
                            text: modelData.title
                            iconSource: "icons/" + modelData.icon
                            onClicked: {
                                if (modelData.action === "new") { root.modal=""; root.act("newChat"); }
                                else if (modelData.action === "chats") { root.selectedChats=[]; root.modal="chats"; }
                                else if (modelData.action === "settings") { root.modal="settings"; root.fillSettings(); root.act("loadSettings"); }
                                else if (root.v.analytics) root.act("chat");
                                else if (root.backend) root.backend.loadAnalytics(30);
                            }
                        }
                    }
                }
            }
            Item {
                Layout.fillWidth:true; Layout.fillHeight:true
                RowLayout {
                    anchors.fill:parent; visible:!root.v.analytics; spacing:12
            ColumnLayout {
                visible: root.width >= 850 || !root.details
                Layout.fillWidth: true; Layout.fillHeight: true
                spacing: 8
                RowLayout {
                    Layout.fillWidth:true
                    Rectangle { width:7; height:7; radius:4; color:root.v.active ? "#f0b442" : "#40b987" }
                    Text { Layout.fillWidth:true; text:root.v.status || "Подключение…"; color:"#748a9c"; font.pixelSize:11; elide:Text.ElideRight }
                    Text { visible:root.v.state === "slicing"; text:root.backend && root.backend.sliceProgress >= 0 ? Math.round(root.backend.sliceProgress * 100) + "%" : "Проверка настроек…"; color:"#1683c7"; font.pixelSize:11 }
                    AssistantButton { compact:true; text:root.assessment.score === undefined ? "Готовность данных…" : "Готовность данных " + root.assessment.score + "/100"; onClicked: { root.details=true; rightPane.contentY=assessmentCard.y; } }
                    AssistantButton { compact:true; text:root.v.active ? "Остановить" : (root.v.calibrationFor || {}).session_id ? "Подготовить план куба" : "Повторить анализ"; enabled:!!root.v.sessionId && !root.v.busy; onClicked:root.v.active ? root.act("cancel") : root.act("analyze") }
                }
                ProgressBar { visible:root.v.state === "slicing"; Layout.fillWidth:true; indeterminate:!root.backend || root.backend.sliceProgress < 0; value:root.backend && root.backend.sliceProgress >= 0 ? root.backend.sliceProgress : 0 }
                Flickable {
                    id:conversation
                    objectName:"conversation"
                    Layout.fillWidth:true; Layout.fillHeight:true
                    contentWidth:width; contentHeight:messagesColumn.height
                    clip:true
                    boundsBehavior:Flickable.StopAtBounds
                    onMovementEnded:root.followTail = contentY + height >= contentHeight - 80
                    ScrollBar.vertical:ScrollBar {}
                    Column {
                        id:messagesColumn; width:conversation.width - 6; spacing:12
                        AssistantCard {
                            visible:!(root.v.messages || []).length
                            width:parent.width; title:"Начните с детали и её задачи"
                            Text { Layout.fillWidth:true; text:"Прикрепите модель и опишите назначение. Здесь появятся анализ, вопросы и план печати."; color:"#75899c"; font.pixelSize:13; wrapMode:Text.Wrap }
                        }
                        Repeater {
                            model:root.v.messages || []
                            Item {
                                width:messagesColumn.width
                                height:modelData.type === "activity" ? actionContent.height : bubble.height
                                Column {
                                    id:actionContent; width:parent.width; spacing:6
                                    visible:modelData.type === "activity"
                                    AssistantButton { text:(modelData.expanded ? "▾ " : "▸ ") + (modelData.summary || ""); compact:true; onClicked:root.backend.toggleActivity(modelData.id) }
                                    Column {
                                        width:parent.width; spacing:10; visible:!!modelData.expanded
                                        Repeater {
                                            model:modelData.operations || []
                                            Column {
                                                width:actionContent.width; spacing:4
                                                Text {
                                                    id:commandLine; width:parent.width
                                                    property string statusPrefix:modelData.status === "tool_finished" ? "✓ " : modelData.status === "tool_failed" ? "! " : modelData.status === "interrupted" ? "■ " : "• "
                                                    property string durationText:modelData.duration_ms === undefined || modelData.duration_ms === null ? "" : " · " + (modelData.duration_ms / 1000).toFixed(1) + " с"
                                                    property string argumentsText:modelData.command.slice(modelData.tool.length + 1, -1)
                                                    function escaped(value) { return String(value).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }
                                                    text:escaped(statusPrefix) + '<a href="details" style="text-decoration:underline">' + escaped(modelData.tool) + '</a>(' + escaped(commandArguments.elidedText) + ')' + escaped(durationText)
                                                    textFormat:Text.RichText; wrapMode:Text.NoWrap; maximumLineCount:1; clip:true
                                                    color:modelData.status === "tool_failed" ? "#af6235" : "#54748d"; linkColor:"#167db4"; font.pixelSize:11
                                                    onLinkActivated:if(link === "details" && root.backend) root.backend.showAction(modelData.start_event_id || "", modelData.finish_event_id || "")
                                                    TextMetrics { id:commandFixed; font:commandLine.font; text:commandLine.statusPrefix + modelData.tool + "()" + commandLine.durationText }
                                                    TextMetrics { id:commandArguments; font:commandLine.font; text:commandLine.argumentsText; elide:Qt.ElideRight; elideWidth:Math.max(0,commandLine.width - commandFixed.width - 4) }
                                                }
                                            }
                                        }
                                    }
                                }
                                Rectangle {
                                    id:bubble
                                    visible:modelData.type !== "activity"
                                    width:modelData.role === "user" ? parent.width * .87 : parent.width
                                    anchors.right:parent.right
                                    color:modelData.role === "user" ? "#dff0ff" : modelData.role === "system" ? "#eef3f7" : "white"
                                    radius:9
                                    height:bubbleContent.height + 24
                                    Column {
                                        id:bubbleContent; anchors.left:parent.left; anchors.right:parent.right; anchors.top:parent.top; anchors.margins:12; spacing:8
                                        function fitWidths() { for(var i=0;i<children.length;++i) children[i].width=width; }
                                        onWidthChanged:fitWidths()
                                        onChildrenChanged:fitWidths()
                                        Component.onCompleted:fitWidths()
                                        Text { text:modelData.role === "user" ? "Вы" : modelData.role === "assistant" ? "AI Assistant" : "Система"; color:"#587e9f"; font.pixelSize:10; font.bold:true }
                                        Text { Layout.fillWidth:true; text:modelData.text || ""; textFormat:Text.PlainText; wrapMode:Text.Wrap; color:"#2f4a62"; font.pixelSize:12; lineHeight:1.25 }
                                        AssistantRequirementsCard {
                                            visible:!!modelData.requirements
                                            requirements:modelData.requirements || ({})
                                            expanded:!!modelData.requirementsExpanded
                                            onToggleRequested:if(root.backend) root.backend.toggleRequirements(modelData.id)
                                            onDetailsRequested:if(root.backend) root.backend.showRequirements(modelData.id)
                                        }
                                        Repeater {
                                            model:modelData.files || []
                                            ColumnLayout {
                                                Layout.fillWidth:true; spacing:4
                                                Text { Layout.fillWidth:true; text:modelData.name; textFormat:Text.PlainText; elide:Text.ElideRight; color:"#167db4"; font.pixelSize:12 }
                                                RowLayout {
                                                    AssistantButton { compact:true; text:modelData.kind === "document" ? "Текст извлечён · Посмотреть" : modelData.kind === "image" ? "Посмотреть" : modelData.status === "imported" ? "В сцене" : "Загрузить в сцену"; enabled:modelData.kind !== "model" || ["saved","error","interrupted"].indexOf(modelData.status) >= 0; onClicked: modelData.kind === "document" ? root.backend.previewDocument(modelData.id) : modelData.kind === "image" ? root.backend.preview(modelData.id) : root.backend.importFile(modelData.id) }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                        TextEdit {
                            id:workflow
                            objectName:"workflow"
                            width:parent.width
                            height:contentHeight
                            text:root.v.workflow || ""
                            textFormat:TextEdit.RichText; readOnly:true; selectByMouse:true; wrapMode:TextEdit.Wrap
                            color:"#2d4862"; font.family:"Segoe UI"; font.pixelSize:12
                            onLinkActivated:root.backend.invokeLink(link)
                        }
                    }
                }
                Rectangle {
                    Layout.fillWidth:true
                    implicitHeight:composerLayout.implicitHeight + 18
                    color:"white"; radius:9; border.color:"#9ccdea"
                    ColumnLayout {
                        id:composerLayout; anchors.left:parent.left; anchors.right:parent.right; anchors.top:parent.top; anchors.margins:9; spacing:6
                        Repeater {
                            model:root.v.pending || []
                            RowLayout {
                                Layout.fillWidth:true
                                Text { Layout.fillWidth:true; text:modelData.name; textFormat:Text.PlainText; color:"#2585b5"; elide:Text.ElideRight; font.pixelSize:11 }
                                AssistantButton { compact:true; text:"Убрать"; enabled:!root.v.busy; onClicked:root.backend.removeFile(modelData.id) }
                            }
                        }
                        TextArea {
                            id:composer; objectName:"composer"
                            Layout.fillWidth:true; Layout.preferredHeight:70
                            placeholderText:"Напишите сообщение для AI ассистента…"
                            wrapMode:TextEdit.Wrap; selectByMouse:true
                            enabled:!!root.v.sessionId && !root.v.busy
                            font.family:"Segoe UI"; font.pixelSize:12; color:"#294864"
                            background:Rectangle { color:"transparent" }
                            onTextChanged:if(root.backend && text !== root.backend.draft) root.backend.setDraft(text)
                            Keys.onReturnPressed: { if(event.modifiers & Qt.ControlModifier) { root.act("send"); event.accepted = true; } else event.accepted = false; }
                        }
                        RowLayout {
                            Layout.fillWidth:true
                            AssistantButton { text:"Прикрепить"; compact:true; enabled:!root.v.busy; onClicked:root.act("attachFiles") }
                            Text { Layout.fillWidth:true; text:"STL / OBJ / 3MF · Изображения · DOCX"; color:"#9aabb8"; font.pixelSize:10; elide:Text.ElideRight }
                            AssistantButton { text:"Отправить ↑"; primary:true; compact:true; enabled:!!root.v.sessionId && !root.v.busy; onClicked:root.act("send") }
                        }
                    }
                }
            }
            Flickable {
                id:rightPane
                visible:root.width >= 850 || root.details
                Layout.preferredWidth:285
                Layout.fillWidth:root.width < 850
                Layout.fillHeight:true
                clip:true
                contentWidth:width
                contentHeight:rightColumn.height
                boundsBehavior:Flickable.StopAtBounds
                ScrollBar.vertical:ScrollBar {}
                Column {
                    id:rightColumn
                    width:rightPane.width - 8; spacing:12
                    AssistantCard {
                        id:assessmentCard
                        width:parent.width; title:"Готовность данных"
                        RowLayout {
                            Layout.fillWidth:true
                            Text { text:root.assessment.score === undefined ? "—" : root.assessment.score + " / 100"; font.pixelSize:25; font.bold:true; color:"#13a974" }
                            Item { Layout.fillWidth:true }
                        }
                        Text { Layout.fillWidth:true; text:root.assessment.status_text || "Собираем данные…"; textFormat:Text.PlainText; wrapMode:Text.Wrap; color:"#54748d"; font.pixelSize:11 }
                        Repeater {
                            model:root.assessment.components || []
                            RowLayout {
                                id:componentRow
                                property var component: modelData
                                Layout.fillWidth:true; spacing:8
                                Rectangle {
                                    Layout.alignment:Qt.AlignTop
                                    Layout.preferredWidth:28; Layout.preferredHeight:28; radius:8
                                    color:componentRow.component.id === "geometry" ? "#eaf9f4" : "#edf7ff"
                                    Text { anchors.centerIn:parent; text:componentRow.component.id === "geometry" ? "✓" : componentRow.component.id === "configuration" ? "◆" : "▣"; color:componentRow.component.id === "geometry" ? "#13a974" : "#008bd6"; font.pixelSize:18; font.bold:true }
                                }
                                ColumnLayout {
                                    Layout.fillWidth:true; spacing:5
                                    RowLayout {
                                        Layout.fillWidth:true
                                        Text { Layout.fillWidth:true; text:componentRow.component.title; wrapMode:Text.Wrap; color:"#36556e"; font.pixelSize:11; font.bold:true }
                                        Text { text:componentRow.component.score + " / " + componentRow.component.maximum; color:"#536d82"; font.pixelSize:10; font.bold:true }
                                    }
                                    Rectangle { Layout.fillWidth:true; height:6; radius:3; color:"#edf2f6"; Rectangle { width:parent.width * componentRow.component.score / componentRow.component.maximum; height:parent.height; color:componentRow.component.id === "geometry" ? "#13a974" : "#36a2dc"; radius:3 } }
                                    Repeater {
                                        model:componentRow.component.details || []
                                        ColumnLayout {
                                            Layout.fillWidth:true; spacing:3
                                            Text {
                                                Layout.fillWidth:true
                                                text:(modelData.warning ? "⚠ " : "• ") + modelData.text
                                                textFormat:Text.PlainText; wrapMode:Text.Wrap
                                                color:modelData.warning ? "#d68a24" : "#6b8193"; font.pixelSize:10
                                                ToolTip.visible:sourceHover.containsMouse && !!modelData.source
                                                ToolTip.delay:500; ToolTip.text:modelData.source || ""
                                                MouseArea { id:sourceHover; anchors.fill:parent; hoverEnabled:true; acceptedButtons:Qt.NoButton }
                                            }
                                            AssistantButton { visible:!!modelData.field; text:"Уточнить"; compact:true; onClicked: { root.modal="assessment"; assessmentField.text=modelData.field; assessmentQuestion.text=modelData.question; assessmentValue.text=""; } }
                                        }
                                    }
                                }
                            }
                        }
                    }
                    AssistantCard {
                        width:parent.width; title:"Предлагаемые параметры печати"
                        Text { visible:!(root.v.params || []).length; Layout.fillWidth:true; text:"Параметры появятся после анализа и проверки плана."; wrapMode:Text.Wrap; color:"#8b9dab"; font.pixelSize:11 }
                        Text { visible:root.assessment.status !== "sufficient"; Layout.fillWidth:true; text:root.assessment.status === "conflict" ? "Требования противоречат друг другу. Уточните данные перед применением параметров." : "Недостаточно данных для подбора параметров. Ответьте на вопросы в чате или в карточке «Готовность данных»."; wrapMode:Text.Wrap; color:"#af6235"; font.pixelSize:11 }
                        Text { visible:(root.v.params || []).length > 0 && root.assessment.status !== "sufficient"; Layout.fillWidth:true; text:"Предварительные параметры"; color:"#af6235"; font.pixelSize:11; font.bold:true }
                        Repeater {
                            model:root.v.params || []
                            RowLayout {
                                Layout.fillWidth:true
                                Text { Layout.preferredWidth:75; text:modelData.name; color:"#536a7d"; font.pixelSize:11; wrapMode:Text.Wrap }
                                Rectangle {
                                    Layout.fillWidth:true; implicitHeight:32; color:"#f9fcfe"; radius:5; border.color:"#d3e1ea"
                                    Text { anchors.fill:parent; anchors.margins:7; text:modelData.value || "—"; textFormat:Text.PlainText; elide:Text.ElideRight; color:"#36556e"; font.pixelSize:11; verticalAlignment:Text.AlignVCenter }
                                }
                            }
                        }
                        Text { Layout.fillWidth:true; text:"Изменения параметров задавайте в чате. Применение — после согласования плана."; color:"#8b9dab"; wrapMode:Text.Wrap; font.pixelSize:10 }
                        AssistantButton { Layout.fillWidth:true; text:"✓  Применить и рассчитать"; primary:true; enabled:!!root.v.planId && !root.v.busy && root.assessment.status !== "conflict"; onClicked:root.act("applyPlan") }
                        AssistantButton { visible:root.assessment.recommend_calibration === true; Layout.fillWidth:true; text:"Провести калибровку"; primary:true; enabled:!!root.assessment.process_signature; onClicked:root.modal = "calibration" }
                        AssistantButton { visible:!!(root.v.calibrationFor || {}).session_id; Layout.fillWidth:true; text:"Ввести измерения куба"; onClicked:root.modal = "measurements" }
                        AssistantButton { visible:!!(root.v.calibrationFor || {}).session_id; Layout.fillWidth:true; text:"Вернуться к детали"; compact:true; onClicked:root.backend.select(root.v.calibrationFor.session_id) }
                        Text { visible:root.assessment.recommend_calibration === true; Layout.fillWidth:true; text:root.assessment.process_signature ? "Куб 20 мм проверяет размеры и качество печати. Условия эксплуатации и противоречия ТЗ нужно уточнить отдельно." : "Для калибровки выберите принтер, материал и совместимый профиль."; wrapMode:Text.Wrap; color:"#8b9dab"; font.pixelSize:10 }
                        Text { visible:(root.assessment.failed_checks || []).length > 0; Layout.fillWidth:true; text:"Куб не прошёл часть проверок. Проверьте измерения и дефекты перед повторной печатью."; wrapMode:Text.Wrap; color:"#af6235"; font.pixelSize:11 }
                        Repeater {
                            model:root.assessment.calibration_questions || []
                            AssistantButton { Layout.fillWidth:true; text:modelData.label; compact:true; onClicked: { root.modal="assessment"; assessmentField.text=modelData.field; assessmentQuestion.text=modelData.text; assessmentValue.text=""; } }
                        }
                    }
                }
            }
        }
        ColumnLayout {
            visible:!!root.v.analytics
            anchors.fill:parent; spacing:10
            RowLayout {
                Layout.fillWidth:true
                ComboBox { id:reportDays; model:["7 дней","30 дней","Всё время"]; currentIndex:1; onActivated:root.backend.loadAnalytics([7,30,0][currentIndex]) }
                TextField { Layout.fillWidth:true; placeholderText:"Найти задачу…"; onTextChanged:if(root.backend) root.backend.searchAnalytics(text) }
                ComboBox { model:["Все задачи","Нужны действия","Актуальный G-code","Есть файлы"]; onActivated:root.backend.filterAnalytics(["all","attention","ready","saved"][currentIndex]) }
                AssistantButton { text:"Обновить"; onClicked:root.backend.loadAnalytics([7,30,0][reportDays.currentIndex]) }
                AssistantButton { text:"Сохранить CSV"; onClicked:root.act("exportCsv") }
            }
            Flickable {
                id:analyticsScroll
                Layout.fillWidth:true; Layout.fillHeight:true; clip:true
                contentWidth:width; contentHeight:analyticsDoc.contentHeight
                boundsBehavior:Flickable.StopAtBounds
                ScrollBar.vertical:ScrollBar {}
                TextEdit { id:analyticsDoc; width:analyticsScroll.width - 12; height:contentHeight; text:root.v.report || "Загружаем аналитику…"; readOnly:true; selectByMouse:true; textFormat:TextEdit.RichText; wrapMode:TextEdit.Wrap; onLinkActivated:root.backend.invokeReportLink(link) }
            }
        }
            }
        }
    }
    // Forms stay inside the stage; they never replace the application's main window.
    Rectangle {
        visible:root.modal !== ""
        anchors.fill:parent; color:"#661c3e56"; z:100
        MouseArea { anchors.fill:parent; acceptedButtons:Qt.AllButtons; onWheel:wheel.accepted = true }
        Rectangle {
            anchors.centerIn:parent; width:Math.min(600,root.width-32); height:Math.min(forms.height+40,root.height-28); color:"#f8fcff"; radius:12; border.color:"#d4e5f0"
            Flickable {
                id:formScroll
                anchors.fill:parent; anchors.margins:20; clip:true
                contentWidth:width; contentHeight:forms.height
                boundsBehavior:Flickable.StopAtBounds
                ScrollBar.vertical:ScrollBar {}
                Column {
                    id:forms; width:formScroll.width-10; spacing:12
                    function fitWidths() { for(var i=0;i<children.length;++i) children[i].width=width; }
                    onWidthChanged:fitWidths()
                    onChildrenChanged:fitWidths()
                    Component.onCompleted:fitWidths()
                    RowLayout { Layout.fillWidth:true; Text { Layout.fillWidth:true; text:({chats:"Все чаты",settings:"Подключение LLM",prompt:(root.v.prompt || {}).title,assessment:"Условия эксплуатации",calibration:"Калибровочный куб",measurements:"Измерения после ручной печати"})[root.modal] || ""; wrapMode:Text.Wrap; color:"#1b4566"; font.pixelSize:18; font.bold:true } AssistantButton { text:"Закрыть"; compact:true; onClicked: { root.modal=""; secret.text=""; root.act("dismissPrompt"); } } }
                    ColumnLayout {
                        visible:root.modal === "chats"; Layout.fillWidth:true; spacing:8
                        RowLayout { AssistantButton { text:"Новый чат"; primary:true; onClicked: { root.modal=""; root.act("newChat"); } } Text { text:"Выберите несколько для объединения"; color:"#879dac"; font.pixelSize:11 } }
                        Repeater {
                            model:root.v.sessions || []
                            RowLayout {
                                Layout.fillWidth:true
                                CheckBox { checked:root.selectedChats.indexOf(modelData.id) >= 0; onToggled:root.toggleChat(modelData.id,checked) }
                                AssistantButton { Layout.fillWidth:true; text:modelData.title; onClicked: { root.modal=""; root.backend.select(modelData.id); } }
                            }
                        }
                        TextField { id:mergeTitle; Layout.fillWidth:true; text:"Объединённая задача"; placeholderText:"Название общего чата" }
                        Text { Layout.fillWidth:true; text:"Создастся новый чат с копией переписки и вложений. Исходные чаты сохранятся. Старые планы и согласия не переносятся."; wrapMode:Text.Wrap; color:"#879dac"; font.pixelSize:11 }
                        AssistantButton { text:"Объединить выбранные (" + root.selectedChats.length + ")"; enabled:root.selectedChats.length >= 2; primary:true; onClicked: { root.backend.mergeChats(root.selectedChats,mergeTitle.text); root.modal=""; } }
                        TextField { id:renameTitle; Layout.fillWidth:true; placeholderText:"Новое название текущего чата" }
                        AssistantButton { text:"Переименовать текущий чат"; enabled:renameTitle.text.length>0; onClicked: { root.backend.rename(renameTitle.text); root.modal=""; } }
                    }
                    ColumnLayout {
                        visible:root.modal === "settings"; Layout.fillWidth:true
                        Text { text:"Base URL"; color:"#658198" }
                        TextField { id:baseUrl; Layout.fillWidth:true; placeholderText:"https://…/v1" }
                        Text { text:"Модель"; color:"#658198" }
                        TextField { id:modelName; Layout.fillWidth:true }
                        ComboBox { id:pipeline; Layout.fillWidth:true; model:["Пошаговый анализ","Полная карта одним запросом"] }
                        TextField { id:secret; Layout.fillWidth:true; placeholderText:"Новый API key — или оставьте пустым"; echoMode:TextInput.Password }
                        Text { Layout.fillWidth:true; text:(root.v.config || {}).key_configured ? "Ключ сохранён в STE Slicer" : "Ключ ещё не задан"; color:(root.v.config || {}).key_configured ? "#1aa274" : "#b17a2c" }
                        Text { Layout.fillWidth:true; text:"После сохранения поле очищается. Пустое поле сохраняет прежний ключ."; wrapMode:Text.Wrap; color:"#879dac"; font.pixelSize:11 }
                        RowLayout {
                            AssistantButton { text:"Сохранить"; primary:true; onClicked:root.backend.saveSettings(baseUrl.text,modelName.text,secret.text,pipeline.currentIndex === 0 ? "stepwise" : "single",false) }
                            AssistantButton { text:"Сохранить и проверить"; onClicked:root.backend.saveSettings(baseUrl.text,modelName.text,secret.text,pipeline.currentIndex === 0 ? "stepwise" : "single",true) }
                            AssistantButton { text:"Удалить ключ"; onClicked:root.act("clearKey") }
                        }
                    }
                    ColumnLayout {
                        visible:root.modal === "prompt"; Layout.fillWidth:true
                        TextArea { id:promptInput; Layout.fillWidth:true; Layout.preferredHeight:100; placeholderText:"Ваш ответ и основание"; wrapMode:TextEdit.Wrap; visible:(root.v.prompt || {}).kind !== "export" }
                        CheckBox { id:overwrite; visible:(root.v.prompt || {}).kind === "export"; text:"Разрешаю замену существующих project.3mf и result.gcode" }
                        AssistantButton { text:(root.v.prompt || {}).kind === "export" ? "Выбрать папку и сохранить" : "Ответить"; primary:true; onClicked: { if((root.v.prompt || {}).kind === "export") root.backend.exportResult(overwrite.checked); else root.backend.submitPrompt(promptInput.text); root.modal=""; } }
                    }
                    ColumnLayout {
                        visible:root.modal === "assessment"; Layout.fillWidth:true
                        Text { id:assessmentField; visible:false }
                        Text { id:assessmentQuestion; Layout.fillWidth:true; wrapMode:Text.Wrap; color:"#54748d" }
                        TextField { id:assessmentValue; Layout.fillWidth:true; placeholderText:"Ответ, единицы или «не применяется»" }
                        AssistantButton { text:"Сохранить ответ"; primary:true; onClicked: { root.backend.answerAssessment(assessmentField.text,assessmentValue.text); root.modal=""; } }
                    }
                    ColumnLayout {
                        visible:root.modal === "calibration"; Layout.fillWidth:true
                        Text { Layout.fillWidth:true; text:"Будет создан отдельный чат со встроенным кубом 20×20×20 мм. Сохраните основной проект и очистите сцену в Prepare перед загрузкой куба. Материал и настройки должны соответствовать будущей детали. Печать выполняется вручную."; wrapMode:Text.Wrap; color:"#54748d" }
                        Text { Layout.fillWidth:true; text:"Куб проверяет размеры, адгезию и видимые дефекты. Он не подтверждает прочность детали и правильность G-code для другой геометрии."; wrapMode:Text.Wrap; color:"#9b7549" }
                        AssistantButton { text:"Создать задачу с кубом"; primary:true; onClicked: { root.modal=""; root.act("createCalibration"); } }
                    }
                    ColumnLayout {
                        visible:root.modal === "measurements"; Layout.fillWidth:true
                        Text { Layout.fillWidth:true; text:"Измерьте куб штангенциркулем. Номинал каждой оси — 20 мм; текущий допуск оценки ±0,2 мм. Z — высота; отметьте направления X/Y до снятия со стола. Результат относится к настройкам, с которыми подготовлен куб."; wrapMode:Text.Wrap; color:"#54748d" }
                        RowLayout { TextField { id:mx; Layout.fillWidth:true; placeholderText:"X, мм" } TextField { id:my; Layout.fillWidth:true; placeholderText:"Y, мм" } TextField { id:mz; Layout.fillWidth:true; placeholderText:"Z, мм" } }
                        CheckBox { id:adhesion; text:"Нет отрыва и заметного коробления" }
                        CheckBox { id:layers; text:"Нет пропусков и расслоения" }
                        CheckBox { id:surface; text:"Поверхность без заметных дефектов" }
                        CheckBox { id:printed; text:"Куб действительно напечатан; это мои измерения" }
                        AssistantButton { text:"Сохранить измерения"; primary:true; enabled:printed.checked; onClicked: { root.backend.saveCalibration(mx.text,my.text,mz.text,adhesion.checked,layers.checked,surface.checked,printed.checked); root.modal=""; } }
                    }
                }
            }
        }
    }
}
