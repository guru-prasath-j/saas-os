import json
import os
import tempfile

from amy.vault import Note
from amy.collab import CollabDB
from amy.search import UniversalSearch


def _setup():
    db = CollabDB(os.path.join(tempfile.mkdtemp(prefix="amy_us_"), "collab.db"))
    cdir = tempfile.mkdtemp(prefix="amy_us_conn_")
    json.dump([{"id": "e1", "subject": "Budget review", "snippet": "your monthly budget"}],
              open(os.path.join(cdir, "email.json"), "w"))
    db.conn.execute("INSERT INTO goals (id,title,domain,status,progress,created_at,target_date) "
                    "VALUES ('g1','Save budget money','finance','active',0,'t',NULL)")
    db.conn.commit()
    notes = [Note(path="Finance/budget.md", title="Budget", meta={"tags": []}, body="monthly budget savings money")]
    return notes, db, cdir


def test_search_across_sources():
    notes, db, cdir = _setup()
    res = UniversalSearch(notes, db, connector_dir=cdir).search("budget")
    srcs = {r["source"] for r in res["results"]}
    assert {"vault", "email", "goals"} <= srcs
    assert res["total"] >= 3 and 0 <= res["confidence"] <= 100


def test_search_filter_and_pagination():
    notes, db, cdir = _setup()
    res = UniversalSearch(notes, db, connector_dir=cdir).search("budget", sources=["vault"], limit=1, offset=0)
    assert all(r["source"] == "vault" for r in res["results"])
    assert res["limit"] == 1 and res["offset"] == 0
