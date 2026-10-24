import QtQuick 2.7
import QtQuick.Controls 2.2
import QtQuick.Layouts 1.3

ColumnLayout {
    id: view
    property var backend: null
    property var sessions: []
    property string currentId: ""
    property var selectedIds: []
    property string editingId: ""
    property string editingText: ""
    signal deleteRequested(string sessionId, string title)
    property bool compactActions: width < 850
    spacing: 16
    function toggle(id, checked) {
        var next = selectedIds.slice(0), index = next.indexOf(id);
        if (checked && index < 0) next.push(id);
        else if (!checked && index >= 0) next.splice(index, 1);
        selectedIds = next;
    }
    onSessionsChanged: {
        var ids = (sessions || []).map(function(row) { return row.id; });
        selectedIds = selectedIds.filter(function(id) { return ids.indexOf(id) >= 0; });
        if (editingId && ids.indexOf(editingId) < 0) editingId = "";
    }
    RowLayout {
        Layout.fillWidth: true
        spacing: 16
        TextField {
            id: search
            Layout.fillWidth: true; implicitHeight: 48
            placeholderText: "Найти чат по названию…"; selectByMouse: true
            leftPadding: 46; rightPadding: 14; font.pixelSize: 14; color: "#36556e"
            background: Rectangle { radius: 10; color: "white"; border.color: search.activeFocus ? "#8fc4ff" : "#d2e4f6" }
            Image { anchors.left: parent.left; anchors.leftMargin: 16; anchors.verticalCenter: parent.verticalCenter; width: 21; height: 21; source: "icons/Search.svg" }
        }
        Rectangle {
            implicitWidth: countLabel.implicitWidth + 32; implicitHeight: 44; radius: 10; color: "#e5f0fa"
            Text { id: countLabel; anchors.centerIn: parent; text: search.text.trim() ? "Найдено: " + list.count + " из " + (view.sessions || []).length : "Чатов: " + (view.sessions || []).length; color: "#6482a6"; font.pixelSize: 13 }
        }
    }
    RowLayout {
        Layout.fillWidth: true; spacing: 10
        Image { source: "icons/Info.svg"; Layout.preferredWidth: 20; Layout.preferredHeight: 20 }
        Text { Layout.fillWidth: true; text: "Выберите несколько чатов, чтобы объединить переписку и вложения."; wrapMode: Text.Wrap; color: "#7c96b6"; font.pixelSize: 12 }
    }
    ListView {
        id: list
        Layout.fillWidth: true; Layout.fillHeight: true
        clip: true; spacing: 12; boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: ScrollBar {}
        model: (view.sessions || []).filter(function(row) { return row.title.toLowerCase().indexOf(search.text.trim().toLowerCase()) >= 0; })
        delegate: Rectangle {
            width: list.width - 12; height: Math.max(96, chatRow.implicitHeight + 32)
            color: view.selectedIds.indexOf(modelData.id) >= 0 ? "#edf7ff" : "white"; radius: 12
            border.color: view.selectedIds.indexOf(modelData.id) >= 0 ? "#9ccfff" : "#e0ecf7"
            RowLayout {
                id: chatRow
                anchors.left: parent.left; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter; anchors.margins: 18
                spacing: 18
                CheckBox {
                    id: selection
                    implicitWidth: 28; implicitHeight: 30; padding: 0
                    checked: view.selectedIds.indexOf(modelData.id) >= 0; onToggled: view.toggle(modelData.id, checked)
                    indicator: Rectangle {
                        x: 3; y: (selection.height - height) / 2; width: 22; height: 22; radius: 4
                        color: selection.checked ? "#087fee" : "white"; border.color: selection.checked ? "#087fee" : "#aab7c7"
                        Text { anchors.centerIn: parent; visible: selection.checked; text: "✓"; color: "white"; font.pixelSize: 16 }
                    }
                }
                Rectangle { Layout.preferredWidth: 1; Layout.preferredHeight: 48; color: "#edf2f7" }
                ColumnLayout {
                    Layout.fillWidth: true; Layout.minimumWidth: 0; spacing: 5
                        Button {
                            id: openChatButton
                            visible: view.editingId !== modelData.id
                            Layout.fillWidth: true
                            text: modelData.title
                            padding: 0; implicitHeight: 28; hoverEnabled: true
                            contentItem: Text { text: openChatButton.text; textFormat: Text.PlainText; elide: Text.ElideRight; verticalAlignment: Text.AlignVCenter; color: openChatButton.hovered ? "#0059ba" : "#006fda"; font.pixelSize: 14; font.bold: true }
                            background: Rectangle { color: "transparent" }
                            ToolTip.visible: hovered && contentItem.truncated; ToolTip.delay: 500; ToolTip.text: text
                            onClicked: if (view.backend) view.backend.select(modelData.id)
                        }
                        TextField {
                            visible: view.editingId === modelData.id
                            Layout.fillWidth: true; maximumLength: 120; selectByMouse: true; font.pixelSize: 14
                            text: view.editingText
                            onTextChanged: if (view.editingId === modelData.id && text !== view.editingText) view.editingText = text
                            onAccepted: if (text.trim().length && view.backend) { view.backend.renameChat(modelData.id, text.trim()); view.editingId = ""; }
                            Keys.onEscapePressed: view.editingId = ""
                        }
                    RowLayout {
                        Layout.fillWidth: true; spacing: 7
                        Image { source: "icons/Clock.svg"; Layout.preferredWidth: 16; Layout.preferredHeight: 16 }
                        Text { Layout.fillWidth: true; text: "Последнее изменение: " + Qt.formatDateTime(new Date(modelData.updated_at), "dd.MM.yyyy HH:mm"); elide: Text.ElideRight; color: "#7c96b6"; font.pixelSize: 11 }
                    }
                }
                RowLayout {
                    spacing: 10
                    AssistantChatAction { visible: view.editingId !== modelData.id; text: "Редактировать"; iconSource: "icons/Edit.svg"; iconOnly: view.compactActions; onClicked: { view.editingText = modelData.title; view.editingId = modelData.id; } }
                    AssistantChatAction { visible: view.editingId !== modelData.id; text: "Удалить"; iconSource: "icons/Delete.svg"; iconOnly: view.compactActions; danger: true; onClicked: view.deleteRequested(modelData.id, modelData.title) }
                    AssistantChatAction { visible: view.editingId === modelData.id; text: "Сохранить"; iconSource: "icons/Save.svg"; iconOnly: view.compactActions; enabled: view.editingText.trim().length > 0; onClicked: { if (view.backend) view.backend.renameChat(modelData.id, view.editingText.trim()); view.editingId = ""; } }
                    AssistantChatAction { visible: view.editingId === modelData.id; text: "Отмена"; iconSource: "icons/Cancel.svg"; iconOnly: view.compactActions; onClicked: view.editingId = "" }
                }
            }
        }
        Text { anchors.centerIn: parent; visible: list.count === 0; text: search.text ? "Чаты не найдены" : "Пока нет чатов. Создайте новый чат в левом меню."; color: "#75899c"; font.pixelSize: 13 }
    }
    Rectangle {
        visible: view.selectedIds.length >= 2
        Layout.fillWidth: true; implicitHeight: mergeContent.implicitHeight + 28
        color: "#f7fbff"; border.color: "#cee2f6"; radius: 10
        ColumnLayout {
            id: mergeContent
            anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top; anchors.margins: 14; spacing: 9
            Text { text: "Выбрано чатов: " + view.selectedIds.length; color: "#36556e"; font.pixelSize: 12; font.bold: true }
            RowLayout {
                Layout.fillWidth: true; spacing: 12
                TextField { id: mergeTitle; Layout.fillWidth: true; maximumLength: 120; text: "Объединённая задача"; placeholderText: "Название общего чата" }
                AssistantButton { text: "Объединить"; primary: true; enabled: mergeTitle.text.trim().length > 0; onClicked: if (view.backend) view.backend.mergeChats(view.selectedIds, mergeTitle.text.trim()) }
            }
            Text { Layout.fillWidth: true; text: "Создастся новый чат. Исходные чаты сохранятся; планы и согласия не переносятся."; wrapMode: Text.Wrap; color: "#879dac"; font.pixelSize: 11 }
        }
    }
}
