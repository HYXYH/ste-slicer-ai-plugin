import QtQuick 2.7
import QtQuick.Controls 2.2

Column {
    id: card
    property var requirements: ({})
    property bool expanded: false
    signal toggleRequested()
    signal detailsRequested()
    spacing: 8
    Text { width:parent.width; text:"Распознаны требования · " + (card.requirements.count || 0); color:"#234862"; font.pixelSize:14; font.bold:true }
    Repeater {
        model:(card.requirements.primary || []).concat(card.expanded ? (card.requirements.secondary || []) : [])
        Text {
            width:card.width
            text:(modelData.warning ? "⚠ " : "• ") + modelData.label + ": " + modelData.value
            textFormat:Text.PlainText; wrapMode:Text.Wrap; maximumLineCount:2; elide:Text.ElideRight
            color:modelData.warning ? "#b67724" : "#36556e"; font.pixelSize:12
            ToolTip.visible:rowHover.containsMouse
            ToolTip.delay:500; ToolTip.text:modelData.label + ": " + modelData.value + (modelData.source ? "\n" + modelData.source : "")
            MouseArea { id:rowHover; anchors.fill:parent; hoverEnabled:true; acceptedButtons:Qt.NoButton }
        }
    }
    Repeater {
        model:card.requirements.warnings || []
        Text { width:card.width; text:"⚠ " + modelData; textFormat:Text.PlainText; wrapMode:Text.Wrap; color:"#b67724"; font.pixelSize:12 }
    }
    AssistantButton { visible:(card.requirements.secondary || []).length > 0; text:card.expanded ? "Свернуть требования ▴" : "Ещё " + (card.requirements.secondary || []).length + " требований ▾"; compact:true; onClicked:card.toggleRequested() }
    Text {
        text:"Исходные поля и источники"; font.pixelSize:10; color:"#2382be"; font.underline:true
        MouseArea { anchors.fill:parent; cursorShape:Qt.PointingHandCursor; onClicked:card.detailsRequested() }
    }
}
