import QtQuick 2.7
import QtQuick.Layouts 1.3

Rectangle {
    id: card
    default property alias content: entries.data
    property string title: ""
    property string subtitle: ""
    property url iconSource
    implicitHeight: body.height + 36
    color: "white"; radius: 12; border.color: "#e3eef8"
    Column {
        id: body
        anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top; anchors.margins: 18
        spacing: 14
        RowLayout {
            width: parent.width; spacing: 10
            Image { source: card.iconSource; Layout.preferredWidth: 23; Layout.preferredHeight: 23 }
            Text { Layout.fillWidth: true; text: card.title; color: "#173959"; font.pixelSize: 16; font.bold: true; wrapMode: Text.Wrap }
            Text { visible: card.subtitle !== ""; Layout.maximumWidth: card.width * .6; text: card.subtitle; color: "#8096b2"; font.pixelSize: 10; wrapMode: Text.Wrap }
        }
        Column {
            id: entries; width: parent.width; spacing: 8
            function fitWidths() { for (var i=0; i<children.length; ++i) children[i].width = width; }
            onWidthChanged: fitWidths()
            onChildrenChanged: fitWidths()
            Component.onCompleted: fitWidths()
        }
    }
}
