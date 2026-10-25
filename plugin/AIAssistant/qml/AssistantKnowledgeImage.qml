import QtQuick 2.7

Rectangle {
    id: frame
    property url imageSource
    property bool schematic: false
    color: "#eff6fc"; radius: 10; clip: true
    Image {
        id: picture; anchors.fill: parent; anchors.margins: 4
        source: frame.imageSource; fillMode: Image.PreserveAspectFit; asynchronous: true
    }
    Image {
        anchors.fill: parent; anchors.margins: 4; fillMode: Image.PreserveAspectFit
        visible: picture.status === Image.Error || picture.status === Image.Null
        source: "defects/placeholder.svg"
    }
    Text { anchors.centerIn: parent; visible: picture.status === Image.Loading; text: "Загрузка изображения…"; color: "#7993aa"; font.pixelSize: 11 }
}
