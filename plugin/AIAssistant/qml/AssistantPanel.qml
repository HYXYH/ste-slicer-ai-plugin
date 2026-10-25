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
    property string deleteChatId: ""
    property string deleteChatTitle: ""
    property string editingTitleSession: ""
    property string titleDraft: ""
    property bool details: false
    property int settingsVersion: -1
    property bool followTail: true
    readonly property real navigationWidth: 94
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
        if (v.prompt && v.prompt.kind && !v.chats && !v.analytics && !v.knowledge) { promptInput.text = ""; modal = "prompt"; }
        tailTimer.restart();
    }
    Connections {
        target: root.backend || null
        onDraftChanged: if (root.backend) composer.text = root.backend.draft
    }
    Timer { id: tailTimer; interval: 20; onTriggered: if (root.followTail) conversation.contentY = Math.max(0, conversation.contentHeight - conversation.height); }
    Timer { id:calibrationFocusTimer;interval:30;onTriggered:conversation.contentY=Math.max(0,calibrationChat.y) }
    DropArea {
        anchors.fill: parent
        enabled: !root.v.analytics && !root.v.chats && !root.v.knowledge && !root.v.busy
        onDropped: if (drop.hasUrls && root.backend) root.backend.uploadUrls(drop.urls)
    }
    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 14
        spacing: 12
        Item {
            Layout.fillWidth: true
            implicitHeight: root.v.chats || root.v.analytics || root.v.knowledge ? 48 : Math.max(64, chatHeading.height + 8)
            Image { id:headerRobot; source:"Robot.svg"; width:45; height:45; anchors.left:parent.left; anchors.leftMargin:(root.navigationWidth - width) / 2; anchors.verticalCenter:parent.verticalCenter }
            Text { visible:!!root.v.chats || !!root.v.analytics || !!root.v.knowledge; anchors.left:parent.left; anchors.leftMargin:root.navigationWidth + 12; anchors.verticalCenter:parent.verticalCenter; text:root.v.chats ? "Все чаты" : root.v.knowledge ? "База знаний" : "Аналитика подготовки"; color:"#123858"; font.family:"Segoe UI"; font.pixelSize:root.v.chats ? 30 : 20; font.bold:true }
            Column {
                id:chatHeading
                visible:!root.v.chats && !root.v.analytics && !root.v.knowledge
                anchors.centerIn:parent; width:Math.max(120,parent.width - (root.width < 850 ? 230 : 120)); spacing:6
                Item {
                    width:parent.width; height:root.editingTitleSession && root.editingTitleSession === root.v.sessionId ? 36 : titleRow.height
                    Row {
                        id:titleRow
                        visible:!(root.editingTitleSession && root.editingTitleSession === root.v.sessionId)
                        anchors.horizontalCenter:parent.horizontalCenter
                        width:Math.min(chatTitle.implicitWidth + 36,parent.width); spacing:8
                        height:Math.max(30,chatTitle.implicitHeight)
                        Text { id:chatTitle; width:parent.width - 36; anchors.verticalCenter:parent.verticalCenter; text:root.v.title || "Новая задача"; textFormat:Text.PlainText; horizontalAlignment:Text.AlignHCenter; wrapMode:Text.Wrap; maximumLineCount:2; elide:Text.ElideRight; font.family:"Segoe UI"; font.pixelSize:18; font.bold:true; color:"#123858" }
                        Button {
                            width:28; height:28; anchors.verticalCenter:parent.verticalCenter
                            enabled:!!root.v.sessionId; hoverEnabled:true
                            contentItem:Image { source:"icons/Edit.svg"; fillMode:Image.PreserveAspectFit; sourceSize.width:16; sourceSize.height:16 }
                            padding:6; background:Rectangle { radius:5; color:parent.hovered ? "#e3f1ff" : "transparent" }
                            ToolTip.visible:hovered; ToolTip.text:"Редактировать название"
                            onClicked: { root.titleDraft=root.v.title || ""; root.editingTitleSession=root.v.sessionId; titleInput.forceActiveFocus(); titleInput.selectAll(); }
                        }
                    }
                    TextField {
                        id:titleInput
                        visible:!!root.editingTitleSession && root.editingTitleSession === root.v.sessionId
                        anchors.centerIn:parent; width:Math.min(parent.width,720); height:36
                        text:root.titleDraft; maximumLength:120; selectByMouse:true; horizontalAlignment:TextInput.AlignHCenter; font.pixelSize:16
                        onTextChanged:if(visible && text !== root.titleDraft) root.titleDraft=text
                        onAccepted:if(text.trim().length && root.backend) { root.backend.rename(text.trim()); root.editingTitleSession=""; }
                        Keys.onEscapePressed:root.editingTitleSession=""
                        ToolTip.visible:activeFocus; ToolTip.delay:700; ToolTip.text:"Enter — сохранить, Escape — отменить"
                    }
                }
                Text {
                    width:parent.width; horizontalAlignment:Text.AlignHCenter; elide:Text.ElideRight; font.pixelSize:10; color:"#7d8e9f"
                    textFormat:Text.RichText
                    text:'<font color="' + (root.v.state === "failed" ? "#dd715b" : root.v.active ? "#168bd1" : root.v.state === "waiting_user" ? "#e8aa32" : "#40b987") + '">●</font> ' + (root.v.status || "Подключение…") + (root.v.updatedAt ? " · Последнее изменение: " + Qt.formatDateTime(new Date(root.v.updatedAt),"dd.MM.yyyy, HH:mm") : "")
                }
            }
            AssistantButton { anchors.right:parent.right; anchors.verticalCenter:parent.verticalCenter; visible: root.width < 850 && !root.v.analytics && !root.v.chats && !root.v.knowledge; text: root.details ? "Чат" : "Параметры"; onClicked: root.details = !root.details }
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
                Layout.preferredWidth: root.navigationWidth; Layout.minimumWidth: root.navigationWidth; Layout.maximumWidth: root.navigationWidth; Layout.fillHeight: true
                color: "#fafcfe"; radius: 9
                Column {
                    anchors.top: parent.top; anchors.left: parent.left; anchors.right: parent.right; anchors.margins: 12
                    spacing: 12
                    Repeater {
                        model: [{title:"Новый чат",icon:"NewChat.svg",action:"new"}, {title:root.v.chats ? "Назад" : "Все чаты",icon:root.v.chats ? "Back.svg" : "Chats.svg",action:"chats"}, {title:"LLM",icon:"Settings.svg",action:"settings"}, {title:root.v.knowledge ? "Назад" : "База знаний",icon:root.v.knowledge ? "Back.svg" : "Book.svg",action:"experience"}, {title:root.v.analytics ? "Назад" : "Аналитика",icon:root.v.analytics ? "Back.svg" : "Analytics.svg",action:"analytics"}]
                        AssistantNavButton {
                            width: parent.width
                            text: modelData.title
                            iconSource: "icons/" + modelData.icon
                            onClicked: {
                                if (modelData.action === "new") { root.modal=""; root.act("newChat"); }
                                else if (modelData.action === "chats") { root.modal=""; root.v.chats ? root.act("closeChats") : root.act("showChats"); }
                                else if (modelData.action === "settings") { root.modal="settings"; root.fillSettings(); root.act("loadSettings"); }
                                else if (modelData.action === "experience") { root.modal=""; root.v.knowledge ? root.act("chat") : root.act("showPrintExperience"); }
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
                    anchors.fill:parent; visible:!root.v.analytics && !root.v.chats && !root.v.knowledge; spacing:12
            ColumnLayout {
                visible: root.width >= 850 || !root.details
                Layout.fillWidth: true; Layout.fillHeight: true
                spacing: 8
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
                        AssistantButton { visible:!!root.v.active; text:"Остановить"; compact:true; enabled:!root.v.busy; onClicked:root.act("cancel") }
                        AssistantButton { text:"Сообщить о результате печати"; enabled:!!root.v.sessionId && !root.v.busy && !root.v.active; onClicked:root.act("reportPrintResult") }
                        AssistantCalibrationChat {
                            id:calibrationChat
                            width:parent.width;backend:root.backend;viewState:root.v
                            visible:!!((root.v.intelligentCalibration || {}).decision || {}).offer || ["preparing","test_ready","awaiting_print","review"].indexOf(((root.v.intelligentCalibration || {}).run || {}).state)>=0
                            onExplain:root.modal="calibrationInfo"
                            onActiveChanged:if(active){root.followTail=false;calibrationFocusTimer.restart();}
                        }
                        AssistantCard {
                            property var recovery:root.v.analysisAction || ({})
                            visible:!!recovery.kind
                            width:parent.width; title:recovery.title || ""
                            Text { Layout.fillWidth:true; text:root.v.analysisAction ? root.v.analysisAction.text : ""; textFormat:Text.PlainText; wrapMode:Text.Wrap; color:"#75899c"; font.pixelSize:12 }
                            Row {
                                spacing:8
                                AssistantButton { text:(root.v.analysisAction || {}).label || ""; primary:true; enabled:!!root.v.sessionId && !root.v.busy; onClicked: { if((root.v.analysisAction || {}).kind === "settings") { root.modal="settings"; root.fillSettings(); root.act("loadSettings"); } else root.act("analyze"); } }
                                AssistantButton { visible:!!(root.v.analysisAction || {}).settings; text:"Настройки LLM"; onClicked: { root.modal="settings"; root.fillSettings(); root.act("loadSettings"); } }
                            }
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
                        Flickable {
                            Layout.fillWidth:true; Layout.preferredHeight:70
                            clip:true; boundsBehavior:Flickable.StopAtBounds
                            TextArea.flickable: TextArea {
                                id:composer; objectName:"composer"
                                placeholderText:"Напишите сообщение для AI ассистента…"
                                wrapMode:TextEdit.Wrap; selectByMouse:true; rightPadding:16
                                enabled:!!root.v.sessionId && !root.v.busy
                                font.family:"Segoe UI"; font.pixelSize:12; color:"#294864"
                                background:Rectangle { color:"transparent" }
                                onTextChanged:if(root.backend && text !== root.backend.draft) root.backend.setDraft(text)
                                Keys.onReturnPressed: { if(event.modifiers & Qt.ControlModifier) { root.act("send"); event.accepted = true; } else event.accepted = false; }
                            }
                            ScrollBar.vertical: ScrollBar { policy:ScrollBar.AsNeeded }
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
                            Text { text:root.assessment.score === undefined ? "—" : root.assessment.score + " / 100"; font.pixelSize:25; font.bold:true; color:root.assessment.status === "conflict" ? "#d68a24" : root.assessment.status === "sufficient" ? "#13a974" : "#36a2dc" }
                            Item { Layout.fillWidth:true }
                        }
                        Text { Layout.fillWidth:true; text:root.assessment.status_text || "Собираем данные…"; textFormat:Text.PlainText; wrapMode:Text.Wrap; color:"#54748d"; font.pixelSize:11 }
                        Repeater {
                            model:root.assessment.components || []
                            RowLayout {
                                id:componentRow
                                property var component: modelData
                                property color statusColor: component.has_warning ? "#d68a24" : component.complete ? "#13a974" : "#36a2dc"
                                objectName:"readiness-" + component.id
                                Layout.fillWidth:true; spacing:8
                                Rectangle {
                                    Layout.alignment:Qt.AlignTop
                                    Layout.preferredWidth:28; Layout.preferredHeight:28; radius:8
                                    color:componentRow.component.has_warning ? "#fff7e8" : componentRow.component.complete ? "#eaf9f4" : "#edf7ff"
                                    Text { objectName:componentRow.objectName + "-icon"; anchors.centerIn:parent; text:componentRow.component.has_warning ? "⚠" : componentRow.component.complete ? "✓" : componentRow.component.id === "configuration" ? "◆" : "▣"; color:componentRow.statusColor; font.pixelSize:18; font.bold:true }
                                }
                                ColumnLayout {
                                    Layout.fillWidth:true; spacing:5
                                    RowLayout {
                                        Layout.fillWidth:true
                                        Text { Layout.fillWidth:true; text:componentRow.component.title; wrapMode:Text.Wrap; color:"#36556e"; font.pixelSize:11; font.bold:true }
                                        Text { text:componentRow.component.score + " / " + componentRow.component.maximum; color:"#536d82"; font.pixelSize:10; font.bold:true }
                                    }
                                    Rectangle { Layout.fillWidth:true; height:6; radius:3; color:"#edf2f6"; Rectangle { objectName:componentRow.objectName + "-bar"; width:parent.width * componentRow.component.score / componentRow.component.maximum; height:parent.height; color:componentRow.statusColor; radius:3 } }
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
                                AssistantButton {
                                    objectName:"explain-" + modelData.id
                                    Layout.preferredWidth:26; Layout.preferredHeight:26; implicitWidth:26; padding:0; compact:true; text:"ⓘ"
                                    enabled:!!root.backend && !root.v.explainingParameter
                                    ToolTip.visible:hovered; ToolTip.text:"Объяснить значение с помощью подключённой LLM"
                                    Accessible.name:"Объяснить: " + modelData.name
                                    onClicked: { root.followTail=true; root.backend.explainParameter(root.v.parameterPlanId, root.v.parameterPlanVersion, modelData.id); }
                                }
                            }
                        }
                        Text { Layout.fillWidth:true; text:"Изменения параметров задавайте в чате. Применение — после согласования плана."; color:"#8b9dab"; wrapMode:Text.Wrap; font.pixelSize:10 }
                        AssistantButton { visible:!!root.v.cubePlanNeeded; Layout.fillWidth:true; text:"Подготовить план куба"; primary:true; enabled:!!root.v.cubeModelReady && !!root.assessment.process_signature && !root.v.busy; onClicked:root.act("analyze") }
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
        AssistantChatsView {
            anchors.fill:parent; visible:!!root.v.chats
            backend:root.backend; sessions:root.v.sessions || []; currentId:root.v.sessionId || ""
            onDeleteRequested: { root.deleteChatId=sessionId; root.deleteChatTitle=title; root.modal="deleteChat"; }
        }
        AssistantKnowledgeView {
            anchors.fill:parent; visible:!!root.v.knowledge
            backend:root.backend; viewState:root.v
        }
        AssistantAnalyticsView {
            anchors.fill:parent; visible:!!root.v.analytics
            backend:root.backend; viewState:root.v
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
                    RowLayout { Layout.fillWidth:true; Text { Layout.fillWidth:true; text:({deleteChat:"Удалить чат?",settings:"Подключение LLM",prompt:(root.v.prompt || {}).title,assessment:"Условия эксплуатации",calibration:"Калибровочный куб",calibrationInfo:"Как работает калибровка",measurements:"Измерения после ручной печати"})[root.modal] || ""; wrapMode:Text.Wrap; color:"#1b4566"; font.pixelSize:18; font.bold:true } AssistantButton { text:"Закрыть"; compact:true; onClicked: { root.modal=""; secret.text=""; root.act("dismissPrompt"); } } }
                    ColumnLayout {
                        visible:root.modal === "deleteChat"; Layout.fillWidth:true; spacing:12
                        Text { Layout.fillWidth:true; text:root.deleteChatTitle; textFormat:Text.PlainText; wrapMode:Text.Wrap; font.bold:true; color:"#36556e" }
                        Text { Layout.fillWidth:true; text:"Переписка и вложения этого чата будут удалены без возможности восстановления. Модели в сцене и экспортированные файлы сохранятся."; wrapMode:Text.Wrap; color:"#75899c"; font.pixelSize:12 }
                        RowLayout {
                            AssistantButton { text:"Отмена"; onClicked:root.modal="" }
                            AssistantButton { text:"Удалить чат"; primary:true; onClicked: { if(root.backend) root.backend.deleteChat(root.deleteChatId); root.modal=""; } }
                        }
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
                    Column {
                        visible:root.modal === "calibrationInfo"; spacing:12
                        Text { width:parent.width;text:(((root.v.intelligentCalibration || {}).decision || {}).reason || "");textFormat:Text.PlainText;wrapMode:Text.Wrap;color:"#426f91";font.pixelSize:12 }
                        Text { width:parent.width;text:"Недостаточно проверено: " + ((((root.v.intelligentCalibration || {}).decision || {}).unchecked_labels || []).join("; "));wrapMode:Text.Wrap;color:"#987947";font.pixelSize:11 }
                        Text { width:parent.width;text:"Учтены печати: " + ((((root.v.intelligentCalibration || {}).decision || {}).print_ids || []).join(", ") || "сопоставимых проверок нет");textFormat:Text.PlainText;wrapMode:Text.Wrap;color:"#6988a2";font.pixelSize:10 }
                        Text { width:parent.width;text:"Тест: " + (((((root.v.intelligentCalibration || {}).decision || {}).test || {}).title) || "не выбран") + " · " + (((((root.v.intelligentCalibration || {}).decision || {}).test || {}).limitations) || "");wrapMode:Text.Wrap;color:"#6988a2";font.pixelSize:11 }
                        Repeater {
                            model:(((root.v.intelligentCalibration || {}).decision || {}).features || []).filter(function(f){return f.required;})
                            Text { width:parent.width;text:modelData.title + ": учтено проверок с весами " + modelData.effective_trials + ", успешных " + modelData.success_weight.toFixed(1) + ", неудачных " + modelData.failure_weight.toFixed(1) + (modelData.mean === null || modelData.mean === undefined ? "; данных недостаточно для вероятностной оценки" : "; оценка по истории, не гарантия");wrapMode:Text.Wrap;color:"#6988a2";font.pixelSize:11 }
                        }
                        Text { width:parent.width;text:"Для расчёта U можно указать потери от неудачной основной печати и стоимость выбранного теста в одинаковых условных единицах. Без данных об обнаружении и исправлении останутся правила холодного старта.";wrapMode:Text.Wrap;color:"#6988a2";font.pixelSize:11 }
                        RowLayout { width:parent.width;TextField { id:mainLoss;Layout.fillWidth:true;placeholderText:"Потери при неудаче" } TextField { id:testCost;Layout.fillWidth:true;placeholderText:"Стоимость теста" } }
                        AssistantButton { text:"Обновить оценку с затратами";compact:true;onClicked:if(root.backend)root.backend.calibrationCommand("refresh",{costs:{main_loss:Number(mainLoss.text.replace(",",".")),test_cost:Number(testCost.text.replace(",","."))}}) }
                        Repeater {
                            model:root.v.calibrationInfo || []
                            Column {
                                width:forms.width; spacing:6
                                Text { width:parent.width; text:modelData.title + " · " + modelData.state; textFormat:Text.PlainText; wrapMode:Text.Wrap; font.bold:true; font.pixelSize:13; color:"#315f84" }
                                Text { width:parent.width; text:modelData.text; textFormat:Text.PlainText; wrapMode:Text.Wrap; font.pixelSize:12; color:"#6d88a1" }
                            }
                        }
                        AssistantButton { text:"Открыть раздел «Калибровка»"; onClicked: { root.modal=""; root.act("showCalibrationInfo"); } }
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
