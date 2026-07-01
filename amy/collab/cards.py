"""Agent Memory Cards — each agent keeps: known topics, frequently asked
questions, last accessed files, and an importance ranking.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from collections import Counter

_W = re.compile(r"[a-z0-9]+")
_STOP = set("the a an and or but if then of to in on for with at by from is are was "
            "were be this that it my your our notes note about how what".split())
_MAX_FAQ = 20


def _now():
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


class AgentCards:
    def __init__(self, db):
        self.db = db.conn

    def build(self, registry: dict, domain_map: dict):
        """registry: {domain: DomainAgent}; domain_map: {domain: [paths]}."""
        for domain, agent in registry.items():
            toks = []
            for n in agent.notes:
                toks += [t for t in _W.findall((n.title + " " + (n.body or "")).lower())
                         if t not in _STOP and len(t) > 2]
            topics = [w for w, _ in Counter(toks).most_common(12)]
            last_files = [n.path for n in agent.notes[:8]]
            importance = float(len(agent.notes))
            # on rebuild, keep existing faqs + last_files; only refresh topics/importance
            self.db.execute(
                "INSERT INTO agent_cards (agent, topics, faqs, last_files, importance, updated_at) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(agent) DO UPDATE SET topics=excluded.topics, "
                "importance=excluded.importance, updated_at=excluded.updated_at",
                (f"{domain}_agent", json.dumps(topics), "[]", json.dumps(last_files),
                 importance, _now()))
        self.db.commit()

    def _faqs_json(self, agent: str) -> str:
        r = self.db.execute("SELECT faqs FROM agent_cards WHERE agent=?", (agent,)).fetchone()
        return r["faqs"] if r and r["faqs"] else "[]"

    def record_question(self, agent: str, question: str):
        faqs = json.loads(self._faqs_json(agent))
        faqs.append(question)
        faqs = faqs[-_MAX_FAQ:]
        self.db.execute(
            "INSERT INTO agent_cards (agent, topics, faqs, last_files, importance, updated_at) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(agent) DO UPDATE SET faqs=excluded.faqs, updated_at=excluded.updated_at",
            (agent, "[]", json.dumps(faqs), "[]", 0.0, _now()))
        self.db.commit()

    def record_access(self, agent: str, path: str):
        r = self.db.execute("SELECT last_files FROM agent_cards WHERE agent=?", (agent,)).fetchone()
        files = json.loads(r["last_files"]) if r and r["last_files"] else []
        files = [path] + [f for f in files if f != path]
        files = files[:8]
        self.db.execute(
            "INSERT INTO agent_cards (agent, topics, faqs, last_files, importance, updated_at) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(agent) DO UPDATE SET last_files=excluded.last_files, updated_at=excluded.updated_at",
            (agent, "[]", "[]", json.dumps(files), 0.0, _now()))
        self.db.commit()

    def get(self, agent: str) -> dict | None:
        r = self.db.execute("SELECT * FROM agent_cards WHERE agent=?", (agent,)).fetchone()
        if not r:
            return None
        return {"agent": r["agent"], "topics": json.loads(r["topics"] or "[]"),
                "faqs": json.loads(r["faqs"] or "[]"),
                "last_files": json.loads(r["last_files"] or "[]"),
                "importance": r["importance"], "updated_at": r["updated_at"]}

    def all(self) -> list[dict]:
        rs = self.db.execute("SELECT agent FROM agent_cards ORDER BY importance DESC").fetchall()
        return [self.get(r["agent"]) for r in rs]
