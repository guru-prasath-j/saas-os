"""COURSES SOURCE — free-courses discovery through the Learning Feed.
Server logic tested against canned upstream JSON (no live HTTP); feed
fan-out + the course->goal-task proposal tested with mocked MCP/LLM
(quirk 24: _get_llm forced to None)."""
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.automation import build_ctx
from amy.collab import CollabDB

_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def courses_mod():
    spec = importlib.util.spec_from_file_location(
        "courses_server_under_test", _ROOT / "mcp_servers" / "courses_server.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _search(mod, *a, **kw):
    fn = mod.search_courses.fn if hasattr(mod.search_courses, "fn") else mod.search_courses
    return fn(*a, **kw)


_FCC = {"superblocks": ["machine-learning-with-python", "responsive-web-design"]}
_MSL = {"learningPaths": [
    {"title": "Develop generative AI apps in Azure",
     "summary": "<p>Build RAG apps with vector search.</p>",
     "url": "https://learn.microsoft.com/x", "duration_in_minutes": 300,
     "levels": ["intermediate"], "products": ["azure-openai"]},
    {"title": "Manage Windows Server storage",
     "summary": "File servers.", "url": "https://learn.microsoft.com/y",
     "products": []},
], "courses": []}


def _mock_upstreams(monkeypatch, mod, fcc=_FCC, msl=_MSL, counter=None):
    def fake(key, url):
        if counter is not None:
            counter[key] = counter.get(key, 0) + 1
        if key == "fcc":
            if isinstance(fcc, Exception):
                raise fcc
            return fcc
        if isinstance(msl, Exception):
            raise msl
        return msl
    monkeypatch.setattr(mod, "_cached_get_json", fake)


def test_query_matches_both_sources(courses_mod, monkeypatch):
    _mock_upstreams(monkeypatch, courses_mod)
    out = _search(courses_mod, "machine learning python", 10)
    assert any("freeCodeCamp" in c["title"] for c in out)
    out2 = _search(courses_mod, "generative ai", 10)
    assert any("generative AI apps" in c["title"] for c in out2)
    # whole-word matching: 'rag' must not hit 'storage'
    out3 = _search(courses_mod, "rag", 10)
    assert all("storage" not in c["title"].lower() for c in out3)


def test_one_source_failing_shrinks_not_fails(courses_mod, monkeypatch):
    _mock_upstreams(monkeypatch, courses_mod, fcc=RuntimeError("fcc down"))
    out = _search(courses_mod, "generative ai", 10)
    assert out and all("Microsoft Learn" in c["title"] for c in out)


def test_upstream_cache_hit_does_not_refetch(courses_mod, monkeypatch):
    calls = {"n": 0}

    def fake_get(url, timeout=30, headers=None):
        calls["n"] += 1
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: _FCC)
    monkeypatch.setattr(courses_mod.requests, "get", fake_get)
    courses_mod._cache.clear()
    courses_mod._cached_get_json("fcc", "http://u")
    courses_mod._cached_get_json("fcc", "http://u")
    assert calls["n"] == 1


# --- feed fan-out ----------------------------------------------------------------

def test_aggregator_knows_courses_source():
    from amy.learning_feed.aggregator import SOURCE_TOOLS
    assert ("courses", ("search_courses",)) in SOURCE_TOOLS


def test_connectors_status_descriptor_present():
    from amy.saas.routers.connectors import _LOCAL_MCP_DESCRIPTORS
    assert ("Courses", "courses", 8005) in _LOCAL_MCP_DESCRIPTORS
    from amy.saas.app import _LOCAL_MCP_SERVERS
    assert any(n == "courses" and p == 8005 for n, _f, p in _LOCAL_MCP_SERVERS)


# --- course -> goal-task proposal (Part C) ------------------------------------------

@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-courses", "t@example.com", cdb, tmp_path, llm_router=None)
    monkeypatch.setattr("amy.agents.reactive._get_llm", lambda ctx: None)
    yield c
    cdb.close()


def _seed_focus_with_course(ctx, goal_id, relevance=9.0, topic="rag"):
    import uuid
    from amy.learning_feed.sensor import add_focus
    fid = add_focus(ctx.collab.conn, ctx.user_id, topic, goal_id=goal_id)
    ctx.collab.conn.execute(
        "INSERT INTO learning_feed_items(id,uid,source,title,url,summary,score,"
        " relevance,why,focus_tag,focus_id,saved,fetched_at)"
        " VALUES(?,?,'courses','RAG Fundamentals (freeCodeCamp)',"
        " ?,'', 0, ?, '', ?, ?, 0, datetime('now'))",
        (uuid.uuid4().hex[:16], ctx.user_id, f"https://fcc/{uuid.uuid4().hex[:6]}",
         relevance, topic, fid))
    ctx.collab.conn.commit()
    return fid


def _emit_refresh(ctx, fid, topic="rag"):
    from amy.events.factory import get_events
    es = get_events(ctx.user_id, ctx.collab, ctx=ctx)
    es.emit("learning.feed_refreshed",
            {"focus": topic, "focus_id": fid, "new_items": 1}, source="test")


def _course_proposals(ctx):
    rows = ctx.collab.conn.execute(
        "SELECT * FROM approvals WHERE dedup_key LIKE 'course_%'").fetchall()
    return [dict(r) for r in rows]


def test_high_relevance_course_on_goal_linked_focus_proposes_once(ctx):
    from amy.autonomous import GoalEngine
    gid = GoalEngine(ctx.collab).create_goal("Become a GenAI Engineer", domain="career")
    fid = _seed_focus_with_course(ctx, gid)
    _emit_refresh(ctx, fid)
    props = _course_proposals(ctx)
    assert len(props) == 1
    payload = json.loads(props[0]["payload"])
    assert payload["tool"] == "add_goal_task"
    assert "Take course: RAG Fundamentals" in payload["args"]["title"]
    assert props[0]["tier"] == 2 and props[0]["status"] == "pending"
    # second refresh: dedup key -> no double proposal
    _emit_refresh(ctx, fid)
    assert len(_course_proposals(ctx)) == 1


def test_low_relevance_or_unlinked_focus_proposes_nothing(ctx):
    from amy.autonomous import GoalEngine
    gid = GoalEngine(ctx.collab).create_goal("g2", domain="career")
    fid = _seed_focus_with_course(ctx, gid, relevance=5.0)
    _emit_refresh(ctx, fid)
    assert _course_proposals(ctx) == []

    fid2 = _seed_focus_with_course(ctx, None, relevance=9.0, topic="rag pipelines")
    _emit_refresh(ctx, fid2, topic="rag pipelines")
    # unlinked focus takes the trending-goal path, never the course path
    assert all(json.loads(p["payload"]).get("tool") != "add_goal_task"
               for p in _course_proposals(ctx))


def test_kill_switch_disables_course_proposals(ctx, monkeypatch):
    monkeypatch.setenv("AMY_AGENT_LEARNING", "0")
    from amy.autonomous import GoalEngine
    gid = GoalEngine(ctx.collab).create_goal("g3", domain="career")
    fid = _seed_focus_with_course(ctx, gid)
    _emit_refresh(ctx, fid)
    assert _course_proposals(ctx) == []


def test_source_fairness_floor_keeps_small_source_in_the_cut(ctx, monkeypatch):
    """HN/Dev.to volume must not crowd a smaller source's items out of the
    save cap entirely (found live: 12 course items fetched, 0 saved)."""
    from amy.learning_feed import sensor as sensor_mod
    from amy.learning_feed.sensor import LearningFeedSensor, add_focus

    fid = add_focus(ctx.collab.conn, ctx.user_id, "genai floor test")
    big = [{"id": f"h{i}", "source": "hackernews", "title": f"story {i}",
            "url": f"https://hn/{i}", "summary": "", "score": 0,
            "published_at": None} for i in range(40)]
    small = [{"id": f"c{i}", "source": "courses", "title": f"course {i}",
              "url": f"https://c/{i}", "summary": "", "score": 0,
              "published_at": None} for i in range(5)]
    monkeypatch.setattr(sensor_mod.aggregator, "fetch_all",
                        lambda *a, **k: _fake_coro(big + small))
    monkeypatch.setattr(sensor_mod.ranker, "rank", lambda items, t, llm: items)
    monkeypatch.setattr(LearningFeedSensor, "_write_note", lambda self, *a: None)

    from amy.events.store import EventStore
    s = LearningFeedSensor(EventStore(ctx.collab), ctx.collab, ctx.user_id,
                           llm=None, connector_rows=[object()])
    s.poll_one({"id": fid, "topic": "genai floor test"})
    rows = ctx.collab.conn.execute(
        "SELECT source, COUNT(*) n FROM learning_feed_items WHERE focus_id=?"
        " GROUP BY source", (fid,)).fetchall()
    by_src = {r["source"]: r["n"] for r in rows}
    assert by_src.get("courses", 0) >= 3          # floor held
    assert sum(by_src.values()) <= sensor_mod.TOP_SAVE


async def _fake_coro_inner(v):
    return v


def _fake_coro(v):
    return _fake_coro_inner(v)


def test_fetch_all_interleaves_sources_for_ranking_window():
    """Sources must be round-robin interleaved: the ranker only scores the
    first 40 items, so a last-registered source concatenated at the tail
    would never be scored (found live with courses)."""
    import asyncio
    from types import SimpleNamespace
    from amy.learning_feed import aggregator

    async def run():
        rows = [SimpleNamespace(name="hackernews"), SimpleNamespace(name="courses")]

        async def fake_fetch(row, tool, topic):
            n = 45 if "hacker" in row.name else 5
            return [{"title": f"{row.name} {i}", "url": f"https://{row.name}/{i}"}
                    for i in range(n)]
        orig = aggregator._fetch_one
        aggregator._fetch_one = fake_fetch
        try:
            return await aggregator.fetch_all("topic", rows)
        finally:
            aggregator._fetch_one = orig

    items = asyncio.run(run())
    first_40 = {it["source"] for it in items[:40]}
    assert "courses" in first_40 and "hackernews" in first_40
