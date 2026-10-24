import QtQuick 2.7
import QtQuick.Controls 2.0

Button {
    id: control
    property bool primary: false
    property bool compact: false
    implicitHeight: compact ? 32 : 38
    implicitWidth: Math.max(72, contentItem.implicitWidth + 24)
    padding: 10
    font.family: "Segoe UI"
    font.pixelSize: 12
    contentItem: Text {
        text: control.text
        font: control.font
        color: !control.enabled ? "#9aabba" : control.primary ? "white" : "#2382be"
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }
    background: Rectangle {
        radius: 6
        color: !control.enabled ? "#edf2f6" : control.primary ? (control.down ? "#0067a8" : "#008bda") : (control.hovered ? "#eaf6ff" : "#ffffff")
        border.width: 1
        border.color: control.primary ? "#008bda" : "#bddced"
    }
}
