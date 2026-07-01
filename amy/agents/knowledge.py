from .base import SubAgent
class KnowledgeAgent(SubAgent):
    name = "knowledge"
    can_write = False   # read-only: evergreen knowledge edited by hand
    persona = ("You are the Knowledge agent for evergreen notes and system design. "
               "Answer only from context.")
