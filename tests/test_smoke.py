"""Offline smoke test for Amy."""
from amy.engine import get_engine

def run():
    eng = get_engine()
    s = eng.stats(); print("STATS:", {"notes": s["notes"], "backend": s["index_backend"]})
    assert s["notes"] > 0
    print("AGENTS:", list(eng.master.agents.keys()))
    for q, exp in [("what projects did I build with flutter", "projects"),
                   ("what are my skills", "profile"),
                   ("explain the agentic architecture", "knowledge")]:
        r = eng.ask(q); print(q, "->", r.intent); assert r.intent == exp
    print("ALL SMOKE CHECKS PASSED")

if __name__ == "__main__":
    run()
