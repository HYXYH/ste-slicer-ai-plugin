import QtQuick 2.7
import QtQuick.Controls 2.2

Button {
    id: control
    property url iconSource
    implicitWidth: 70
    implicitHeight: 78
    padding: 7
    hoverEnabled: true
    contentItem: Column {
        spacing: 8
        Image { anchors.horizontalCenter: parent.horizontalCenter; source: control.iconSource; width: 26; height: 26; fillMode: Image.PreserveAspectFit }
        Text { width: parent.width; text: control.text; textFormat: Text.PlainText; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.WordWrap; font.family: "Segoe UI"; font.pixelSize: 11; color: "#405b75" }
    }
    background: Rectangle {
        radius: 7
        color: control.down ? "#deedf8" : control.hovered ? "#eef6ff" : "transparent"
        border.width: 1
        border.color: control.hovered ? "#bddced" : "transparent"
    }
}
