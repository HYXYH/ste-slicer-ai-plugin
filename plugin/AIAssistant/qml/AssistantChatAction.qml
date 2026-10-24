import QtQuick 2.7
import QtQuick.Controls 2.2

Button {
    id: control
    property url iconSource
    property bool danger: false
    property bool iconOnly: false
    implicitHeight: 44
    implicitWidth: iconOnly ? 44 : actionLabel.implicitWidth + 58
    hoverEnabled: true
    padding: 10
    contentItem: Item {
        Row {
            anchors.centerIn: parent
            spacing: 9
            Image { width: 20; height: 20; source: control.iconSource; anchors.verticalCenter: parent.verticalCenter }
            Text { id: actionLabel; visible: !control.iconOnly; text: control.text; textFormat: Text.PlainText; color: control.danger ? "#ed4048" : "#087fee"; font.family: "Segoe UI"; font.pixelSize: 12; anchors.verticalCenter: parent.verticalCenter }
        }
    }
    background: Rectangle {
        radius: 8
        color: control.danger ? (control.down ? "#ffe2e4" : control.hovered ? "#ffedef" : "#fff7f7") : (control.down ? "#dceeff" : control.hovered ? "#e7f3ff" : "#f4faff")
        border.color: control.danger ? "#ffc5c9" : "#b4d8ff"
    }
    ToolTip.visible: iconOnly && hovered
    ToolTip.delay: 500
    ToolTip.text: text
}
