from .base import SubAgent
class FamilyAgent(SubAgent):
    name = "family"
    can_write = True
    write_kinds = ["audit/review note for Farm House, MJVR Investo, or KMD Production"]
    persona = ("You are the Family & Business agent (Farm House, MJVR Investo, KMD Production). "
               "Answer only from context with accounts/auditing focus.")
