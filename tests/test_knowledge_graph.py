import os
import tempfile

from amy.vault import Note
from amy.collab import CollabDB
from amy.knowledge_graph import build_graph, GraphStore


def _setup():
    db = CollabDB(os.path.join(tempfile.mkdtemp(prefix="amy_kg_"), "collab.db"))
    db.conn.execute("INSERT INTO goals (id,title,domain,status,progress,created_at,target_date) "
                    "VALUES ('g1','Launch app','projects','active',0,'t',NULL)")
    db.conn.execute("INSERT INTO goals (id,title,domain,status,progress,created_at,target_date) "
                    "VALUES ('g2','Finish portfolio','projects','active',0,'t',NULL)")
    db.conn.execute("INSERT INTO goal_deps (goal_id,depends_on) VALUES ('g1','g2')")
    db.conn.execute("INSERT INTO tasks (id,goal_id,title,done,created_at) VALUES ('t1','g1','write code',0,'t')")
    db.conn.commit()
    notes = [Note(path="Projects/app.md", title="Launch app plan", meta={"tags": []},
                  body="building the launch app with flutter")]
    gp = os.path.join(tempfile.mkdtemp(prefix="amy_kg_g_"), "graph.db")
    return gp, notes, db


def test_build_creates_nodes_and_edges():
    gp, notes, db = _setup()
    stats = build_graph(gp, notes, db)
    assert stats["nodes"]["goal"] >= 2
    assert stats["nodes"].get("task", 0) >= 1
    assert stats["nodes"].get("note", 0) >= 1
    assert "belongs_to" in stats["edges"] and "depends_on" in stats["edges"]
    assert "supports" in stats["edges"]


def test_blocks_and_traverse():
    gp, notes, db = _setup()
    build_graph(gp, notes, db)
    g = GraphStore(gp)
    assert any(x["rel"] == "blocks" for x in g.neighbors("goal:g2"))     # unmet dep blocks dependent
    reached = {r["id"] for r in g.traverse("goal:g1", depth=2)}
    assert "goal:g2" in reached and "task:t1" in reached
    g.close()
