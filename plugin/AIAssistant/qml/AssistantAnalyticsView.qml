import QtQuick 2.7
import QtQuick.Controls 2.2
import QtQuick.Layouts 1.3

ColumnLayout {
    id: view
    property var backend: null
    property var viewState: ({})
    property var reportData: viewState.analyticsData || ({})
    property int periodDays: viewState.analyticsDays === undefined ? 30 : viewState.analyticsDays
    property bool compact: width < 900
    property string percentileTip: "p95 — время, не превышенное в 95% измеренных операций. Медиана — время среднего по порядку измерения."
    spacing: 16
    RowLayout {
        Layout.fillWidth: true; spacing: 12
        ComboBox {
            id: period
            Layout.preferredWidth: view.compact ? 125 : 165; implicitHeight: 44
            model: ["7 дней", "30 дней", "Всё время"]
            currentIndex: [7,30,0].indexOf(view.periodDays)
            leftPadding: 38; font.pixelSize: 12
            background: Rectangle { color: "white"; radius: 7; border.color: "#cbddec" }
            Image { source: "icons/Calendar.svg"; width: 20; height: 20; anchors.left: parent.left; anchors.leftMargin: 12; anchors.verticalCenter: parent.verticalCenter }
            onActivated: if (view.backend) view.backend.loadAnalytics([7,30,0][currentIndex])
        }
        TextField {
            id: search
            Layout.fillWidth: true; Layout.minimumWidth: 70; implicitHeight: 44
            placeholderText: "Найти задачу…"; leftPadding: 40; rightPadding: 12; selectByMouse: true; font.pixelSize: 12
            text: view.viewState.analyticsSearch || ""
            background: Rectangle { color: "white"; radius: 7; border.color: search.activeFocus ? "#8fc4ff" : "#cbddec" }
            Image { source: "icons/Search.svg"; width: 20; height: 20; anchors.left: parent.left; anchors.leftMargin: 12; anchors.verticalCenter: parent.verticalCenter }
            onTextChanged: if (view.backend && text !== (view.viewState.analyticsSearch || "")) view.backend.searchAnalytics(text)
        }
        ComboBox {
            Layout.preferredWidth: view.compact ? 160 : 185; implicitHeight: 44
            model: ["Все задачи", "Нужны действия", "Актуальный G-code", "Есть файлы"]
            currentIndex: ["all","attention","ready","saved"].indexOf(view.viewState.analyticsFilter || "all")
            leftPadding: 38; font.pixelSize: 12
            background: Rectangle { color: "white"; radius: 7; border.color: "#cbddec" }
            Image { source: "icons/Filter.svg"; width: 20; height: 20; anchors.left: parent.left; anchors.leftMargin: 12; anchors.verticalCenter: parent.verticalCenter }
            onActivated: if (view.backend) view.backend.filterAnalytics(["all","attention","ready","saved"][currentIndex])
        }
        AssistantChatAction { text: "Обновить"; iconSource: "icons/Refresh.svg"; iconOnly: view.compact; onClicked: if (view.backend) view.backend.loadAnalytics(view.periodDays) }
        Button {
            id: exportButton
            implicitWidth: view.compact ? 44 : 154; implicitHeight: 44; hoverEnabled: true
            contentItem: Item {
                Row {
                    anchors.centerIn: parent; spacing: 9
                    Image { source: "icons/Download.svg"; width: 20; height: 20; anchors.verticalCenter: parent.verticalCenter }
                    Text { visible: !view.compact; text: "Сохранить CSV"; color: "white"; font.pixelSize: 12; anchors.verticalCenter: parent.verticalCenter }
                }
            }
            background: Rectangle { radius: 7; color: exportButton.down ? "#0065cf" : exportButton.hovered ? "#0877e9" : "#087fee" }
            ToolTip.visible: hovered && view.compact; ToolTip.text: "Сохранить CSV"
            onClicked: if (view.backend) view.backend.exportCsv()
        }
    }
    Flickable {
        id: scroll
        Layout.fillWidth: true; Layout.fillHeight: true; clip: true
        contentWidth: width; contentHeight: content.height
        boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: ScrollBar {}
        Column {
            id: content; width: scroll.width - 12; spacing: 14
            Text { visible: !view.reportData.metrics; text: "Загружаем аналитику…"; color: "#7c96b6"; font.pixelSize: 13 }
            GridLayout {
                width: parent.width; columns: width >= 850 ? 4 : 2; columnSpacing: 14; rowSpacing: 14
                Repeater {
                    model: view.reportData.metrics || []
                    Rectangle {
                        Layout.fillWidth: true; Layout.preferredHeight: 102
                        color: "white"; radius: 12; border.color: "#e3eef8"
                        RowLayout {
                            anchors.fill: parent; anchors.margins: 18; spacing: 15
                            Rectangle {
                                Layout.preferredWidth: 54; Layout.preferredHeight: 54; radius: 27; color: modelData.background
                                Image { anchors.centerIn: parent; width: 28; height: 28; source: "icons/" + modelData.icon }
                            }
                            ColumnLayout {
                                Layout.fillWidth: true; spacing: 3
                                Text { Layout.fillWidth: true; text: modelData.title; wrapMode: Text.Wrap; color: "#8092ad"; font.pixelSize: 11 }
                                Text { text: modelData.value; color: modelData.color; font.pixelSize: 32; font.bold: true }
                            }
                        }
                    }
                }
            }
            AssistantAnalyticsCard {
                width: parent.width; visible: !!view.reportData.metrics
                title: "Активность по дням"; iconSource: "icons/Analytics.svg"
                subtitle: "Последние 7 активных дней. В сводке и CSV учтён весь выбранный период."
                AssistantAnalyticsTable { visible: (view.reportData.activity || []).length > 0; headers: ["Дата","Запуски","Всего / ошибки"]; weights: [1,1,1]; rightColumns: [2]; rows: view.reportData.activity || []; barColumn: 1 }
                Text { visible: !(view.reportData.activity || []).length; text: "Анализы пока не запускались."; color: "#8096b2"; font.pixelSize: 12 }
            }
            Repeater {
                model: view.reportData.attention || []
                Rectangle {
                    width: content.width; height: Math.max(78, attentionText.height + 30)
                    color: attentionHover.containsMouse ? "#fff2d8" : "#fff8e9"; radius: 10; border.color: "#f4d18a"
                    Rectangle {
                        x: 18; anchors.verticalCenter: parent.verticalCenter; width: 42; height: 42; radius: 21; color: "#ffedc5"
                        Image { anchors.centerIn: parent; width: 26; height: 26; source: "icons/Alert.svg" }
                    }
                    Column {
                        id: attentionText; x: 78; width: parent.width - 100; anchors.verticalCenter: parent.verticalCenter; spacing: 6
                        Text { width: parent.width; text: modelData.title + " · задач: " + modelData.count; textFormat: Text.PlainText; wrapMode: Text.Wrap; color: "#234862"; font.pixelSize: 14; font.bold: true }
                        Text { width: parent.width; text: modelData.action + " Нажмите, чтобы показать задачи."; textFormat: Text.PlainText; wrapMode: Text.Wrap; color: "#6482a6"; font.pixelSize: 12 }
                    }
                    MouseArea {
                        id: attentionHover; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                        onClicked: if (view.backend) { view.backend.filterAnalyticsIssue(modelData.code); scroll.contentY = Math.max(0, Math.min(tasksCard.y, scroll.contentHeight - scroll.height)); }
                    }
                }
            }
            AssistantAnalyticsCard {
                width: parent.width; visible: !!view.reportData.metrics
                title: "Использование LLM и оценки печати"; iconSource: "icons/Chip.svg"
                AssistantAnalyticsTable { headers: ["Показатель","Значение"]; weights: [3,2]; rightColumns: [1]; rows: view.reportData.llmRows || [] }
            }
            AssistantAnalyticsCard {
                width: parent.width; visible: !!view.reportData.metrics
                title: "Работа инструментов"; iconSource: "icons/Tools.svg"
                AssistantAnalyticsTable { visible: (view.reportData.toolRows || []).length > 0; headers: ["Инструмент","Вызовы","Ошибки","p95"]; weights: [3,1,1,1]; rightColumns: [1,2,3]; tips: ({3:view.percentileTip}); rows: view.reportData.toolRows || [] }
                Text { visible: !(view.reportData.toolRows || []).length; text: "Нет вызовов инструментов за выбранный период."; color: "#8096b2"; font.pixelSize: 12 }
            }
            AssistantAnalyticsCard {
                id: tasksCard; width: parent.width; visible: !!view.reportData.metrics
                title: "Задачи"; iconSource: "icons/Tasks.svg"
                subtitle: view.viewState.analyticsIssue ? "Показаны задачи выбранной проблемы" : "По текущему поиску и фильтру"
                AssistantButton { visible: !!view.viewState.analyticsIssue; text: "Сбросить выбор проблемы"; compact: true; onClicked: if (view.backend) view.backend.filterAnalytics("all") }
                AssistantAnalyticsTable { visible: (view.reportData.taskRows || []).length > 0; headers: ["Задача","Состояние","Анализы / ошибки","Токены","Комплекты"]; weights: [3,2,1.5,1,1]; rightColumns: [2,3,4]; rows: view.reportData.taskRows || []; onSessionRequested: if (view.backend) view.backend.select(sessionId) }
                Text { visible: !(view.reportData.taskRows || []).length; text: "Нет задач по выбранным условиям."; color: "#8096b2"; font.pixelSize: 12 }
            }
            Repeater {
                model: view.reportData.notes || []
                Text { width: content.width; text: modelData; textFormat: Text.PlainText; wrapMode: Text.Wrap; color: "#8096b2"; font.pixelSize: 10 }
            }
        }
    }
}
