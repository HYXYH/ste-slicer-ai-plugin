def getMetaData():
    return {}


def register(app):
    from .AIAssistant import AIAssistant
    return {"extension": AIAssistant(app)}
