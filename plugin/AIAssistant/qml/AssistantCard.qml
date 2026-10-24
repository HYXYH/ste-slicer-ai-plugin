import QtQuick 2.7
import QtQuick.Layouts 1.3

Rectangle {
    id: card
    default property alias content: body.data
    property string title: ""
    property color headingColor: "#153b5a"
    implicitHeight: body.height + 30
    color: "white"
    radius: 9
    border.color: "#e2eef5"
    Column {
        id: body
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: 15
        spacing: 10
        function fitWidths() { for (var i=0; i<children.length; ++i) children[i].width = width; }
        onWidthChanged: fitWidths()
        onChildrenChanged: fitWidths()
        Component.onCompleted: fitWidths()
        Text {
            visible: card.title !== ""
            text: card.title
            Layout.fillWidth: true
            wrapMode: Text.Wrap
            font.family: "Segoe UI"
            font.pixelSize: 14
            font.bold: true
            color: card.headingColor
        }
    }
}
