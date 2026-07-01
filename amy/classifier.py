"""Intent classifier across all folder agents.

Keyword-first: if the query clearly matches a domain's keywords, route there
deterministically (so 'projects/flutter' never gets mis-sent). Only when keywords
are ambiguous do we ask the LLM, and even then we constrain it to the valid set.
"""
from __future__ import annotations
import json
from .llm import LLMRouter

INTENTS = ["home","profile","projects","family","finances","career","resources","jobsearch","knowledge","captures"]

KEYWORDS = {
    "projects": ["project","projects","built","build","building","made","developed","app","apps",
                 "repo","repository","github","portfolio of","flutter app","ai app","which projects",
                 "what projects","my apps"],
    "profile":  ["about me","who am i","my skill","skills","my experience","work experience",
                 "education","my background","my resume profile","bio","contact info"],
    "family":   ["farm","farmhouse","mjvr","kmd","import","export","property","shipment","daddy",
                 "sathish","appa","business","audit","sbi","pay","payout","payouts","eswari","sumathi",
                 "vjpn","rent","member","ledger"],
    "finances": ["budget","savings","investment","investments","net worth","expense","expenses",
                 "yearly summary","personal finance"],
    "career":   ["learning roadmap","certification","certifications","career goal","upskill","roadmap"],
    "jobsearch":["job","jobs","interview","company","companies","recruiter","application","applications",
                 "offer","offers","linkedin","cover letter","salary","resume","hiring","apply"],
    "knowledge":["architecture","agentic","rag system","second brain","system design","how does",
                 "explain","amy","personalos","knowledge base"],
    "home":     ["dashboard","my goals","goal","quick link","quick links"],
    "resources":["contacts","contact list","document","documents","reference","references","bookmark"],
    "captures": ["photo","photos","picture","pictures","pic","pics","image","images","captured",
                 "capture","i took","snapshot","screenshot","whiteboard","receipt","photographed",
                 "what did i see","show me the photo","my photos","scanned"],
}

_SYS = ("Classify the request into ONE intent from this list: "
        + ", ".join(INTENTS) + ". "
        "projects=apps/repos the user built. profile=identity/skills/experience/education. "
        "family=father's farm/MJVR/KMD businesses and the SBI payouts. finances=personal budget/savings/investments. "
        "career=learning roadmap/certifications/career goals. jobsearch=resumes/jobs/interviews/companies. "
        "knowledge=system design/how things work. home=dashboard/goals. resources=contacts/documents. "
        "captures=photos/pictures the user took with their phone (whiteboards, receipts, places, OCR text). "
        'Reply ONLY JSON: {"intent":"<one>"}.')


def keyword_scores(query: str) -> dict:
    q = query.lower()
    return {intent: sum(1 for kw in kws if kw in q) for intent, kws in KEYWORDS.items()}


def keyword_intent(query: str):
    s = keyword_scores(query)
    best = max(s, key=s.get)
    return (best, s[best])


class IntentClassifier:
    def __init__(self, llm: LLMRouter):
        self.llm = llm

    def classify(self, query: str) -> tuple[str, str]:
        intent, score = keyword_intent(query)
        # confident keyword match -> use it (deterministic, no misroute)
        if score >= 1:
            return intent, f"keyword:{score}"
        # ambiguous -> ask the LLM, constrained to the valid set
        try:
            out, name = self.llm.generate(_SYS, query, "", sensitive=False)
            if name != "template":
                data = json.loads(out[out.find("{"): out.rfind("}")+1])
                got = str(data.get("intent","")).lower()
                if got in INTENTS:
                    return got, f"llm:{name}"
        except Exception:
            pass
        return "profile", "default"   # safe default: most queries are about the user
