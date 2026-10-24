def getMetaData():
    return {"stage": {"name": "AI ASSISTANT", "weight": 10}}


def register(app):
    from .AIAssistant import AIAssistant
    from .assistant_stage import AssistantStage
    extension = AIAssistant(app)
    return {"extension": extension, "stage": AssistantStage(extension)}
