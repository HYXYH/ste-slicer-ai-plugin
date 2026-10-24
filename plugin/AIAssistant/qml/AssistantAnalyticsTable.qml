import QtQuick 2.7
import QtQuick.Controls 2.2

Rectangle {
    id: table
    property var headers: []
    property var rows: []
    property var weights: []
    property var rightColumns: []
    property var tips: ({})
    property int barColumn: -1
    signal sessionRequested(string sessionId)
    function columnWidth(index) {
        var total = 0;
        for (var i=0; i<headers.length; ++i) total += weights[i] || 1;
        return width * (weights[index] || 1) / total;
    }
    function escaped(value) { return String(value).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }
    implicitHeight: header.height + body.height + 2
    color: "white"; border.color: "#e1ecf6"; radius: 6; clip: true
    Rectangle {
        id: header; width: parent.width; height: 38; color: "#eaf2f9"
        Row {
            width: parent.width
            Repeater {
                model: table.headers
                Text {
                    width: table.columnWidth(index); height: header.height
                    leftPadding: 12; rightPadding: 12
                    text: modelData; textFormat: Text.PlainText; elide: Text.ElideRight
                    verticalAlignment: Text.AlignVCenter
                    horizontalAlignment: table.rightColumns.indexOf(index) >= 0 ? Text.AlignRight : Text.AlignLeft
                    color: "#456887"; font.pixelSize: 11; font.bold: true
                    ToolTip.visible: headHover.containsMouse && !!table.tips[index]
                    ToolTip.delay: 400; ToolTip.text: table.tips[index] || ""
                    MouseArea { id: headHover; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                }
            }
        }
    }
    Column {
        id: body; anchors.top: header.bottom; width: parent.width
        Repeater {
            model: table.rows
            Rectangle {
                id: row
                property var record: modelData
                width: table.width; height: cells.height + 20
                color: index % 2 ? "#f5f9fc" : "white"
                Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: "#e8f0f7" }
                Row {
                    id: cells; y: 10; width: parent.width
                    Repeater {
                        model: row.record.cells
                        Item {
                            width: table.columnWidth(index); height: Math.max(20, label.implicitHeight)
                            Rectangle {
                                visible: table.barColumn === index
                                x: 12; anchors.verticalCenter: parent.verticalCenter
                                width: Math.max(2, (parent.width - 24) * (row.record.fraction || 0)); height: 7; radius: 3; color: "#0988ff"
                            }
                            Text {
                                id: label
                                visible: table.barColumn !== index
                                x: 12; width: parent.width - 24
                                text: row.record.id && index === 0 ? '<a href="open">' + table.escaped(modelData) + '</a>' : modelData
                                textFormat: row.record.id && index === 0 ? Text.RichText : Text.PlainText
                                wrapMode: Text.Wrap; maximumLineCount: 2; elide: Text.ElideRight
                                horizontalAlignment: table.rightColumns.indexOf(index) >= 0 ? Text.AlignRight : Text.AlignLeft
                                color: row.record.warning ? "#b5782e" : "#36556e"; linkColor: "#087fee"; font.pixelSize: 12
                                onLinkActivated: table.sessionRequested(row.record.id)
                            }
                            ToolTip.visible: cellHover.containsMouse && (!!table.tips[index] || !!row.record.tip || label.truncated)
                            ToolTip.delay: 500; ToolTip.text: table.tips[index] || row.record.tip || String(modelData)
                            MouseArea { id: cellHover; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                        }
                    }
                }
            }
        }
    }
}
