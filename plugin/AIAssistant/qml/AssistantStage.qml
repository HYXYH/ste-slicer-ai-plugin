import QtQuick 2.7
import UM 1.4 as UM

AssistantPanel {
    backend: UM.Controller.activeStage && UM.Controller.activeStage.stageId === "AIAssistant" ? UM.Controller.activeStage.assistant : null
    property real previousLoaderZ: 0
    Component.onCompleted: {
        if (parent) {
            previousLoaderZ = parent.z;
            parent.z = 1; // Keep the stage above the Prepare-only JobSpecs overlay.
        }
    }
    Component.onDestruction: if (parent) parent.z = previousLoaderZ
}
