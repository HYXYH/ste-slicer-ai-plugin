import QtQuick 2.7
import QtQuick.Layouts 1.3

Rectangle {
    id:card
    property var entry:({})
    property bool expanded:false
    implicitHeight:body.height+30
    radius:12;color:"#ffffff";border.color:"#d4e7fa"
    Column {
        id:body;x:16;y:15;width:parent.width-32;spacing:12
        RowLayout {
            width:parent.width;spacing:14
            Rectangle {
                Layout.preferredWidth:52;Layout.preferredHeight:52;radius:26;color:card.entry.tint || "#e7f4ff"
                Image { anchors.centerIn:parent;width:27;height:27;source:card.entry.icon ? "icons/"+card.entry.icon+".svg" : "icons/Info.svg" }
            }
            Column {
                Layout.fillWidth:true;spacing:5
                Text { width:parent.width;text:card.entry.title || "";textFormat:Text.PlainText;font.pixelSize:16;font.bold:true;color:"#103b67";wrapMode:Text.Wrap }
                Text { width:parent.width;text:card.entry.summary || card.entry.text || "";textFormat:Text.PlainText;font.pixelSize:13;color:"#6c89a9";wrapMode:Text.Wrap }
            }
            Canvas {
                id:chevron
                Layout.preferredWidth:20;Layout.preferredHeight:18
                onPaint:{var c=getContext("2d");c.clearRect(0,0,width,height);c.strokeStyle="#6994bf";c.lineWidth=2;c.beginPath();c.moveTo(4,card.expanded?12:6);c.lineTo(10,card.expanded?6:12);c.lineTo(16,card.expanded?12:6);c.stroke();}
                Connections { target:card;onExpandedChanged:chevron.requestPaint() }
            }
        }
        Rectangle { visible:card.expanded;width:parent.width;height:1;color:"#e6f0f9" }
        Text { visible:card.expanded;width:parent.width;text:card.entry.text || "";textFormat:Text.PlainText;font.pixelSize:13;color:"#587895";wrapMode:Text.Wrap }
    }
    MouseArea { anchors.fill:parent;cursorShape:Qt.PointingHandCursor;onClicked:card.expanded=!card.expanded }
}
