"""github_projects_sync — GitHub repos → vault project notes, additions
only. GitHub is mocked at tools.invoke; the vault is a tmp dir.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.automation import build_ctx
from amy.collab import CollabDB
from amy.projects_sync import github_projects_sync


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-psync", "t@example.com", cdb, tmp_path, llm_router=None)
    monkeypatch.setattr("amy.saas.tenancy.resolve_vault_dir",
                        lambda uid: tmp_path / "vault")
    yield c
    cdb.close()


def _repo(name, url=None, **kw):
    return {"name": name, "html_url": url or f"https://github.com/me/{name}",
            "description": kw.get("description", f"{name} does things"),
            "language": kw.get("language", "Python"),
            "private": kw.get("private", False),
            "fork": kw.get("fork", False),
            "owner": {"login": "me"}}


def _mock_repos(monkeypatch, repos):
    monkeypatch.setattr(
        "amy.tools.invoke",
        lambda c, name, args, actor="human": {"repos": repos, "count": len(repos)})


def _projects_dir(tmp_path):
    return tmp_path / "vault" / "01_Profile" / "Projects"


def test_creates_notes_for_new_repos(ctx, tmp_path, monkeypatch):
    _mock_repos(monkeypatch, [_repo("new-shiny-agent"), _repo("rag-eval")])
    out = github_projects_sync(ctx)
    assert out["notes_created"] == 2
    note = (_projects_dir(tmp_path) / "new-shiny-agent.md").read_text(encoding="utf-8")
    assert "type: project" in note
    assert "repo: https://github.com/me/new-shiny-agent" in note
    assert "new-shiny-agent does things" in note
    # notification fired
    types = [n["type"] for n in ctx.notify_store().list(limit=10)]
    assert "projects_sync" in types


def test_second_run_is_idempotent(ctx, tmp_path, monkeypatch):
    _mock_repos(monkeypatch, [_repo("only-once")])
    assert github_projects_sync(ctx)["notes_created"] == 1
    assert github_projects_sync(ctx)["notes_created"] == 0
    assert len(list(_projects_dir(tmp_path).glob("*.md"))) == 1


def test_never_overwrites_an_edited_note(ctx, tmp_path, monkeypatch):
    d = _projects_dir(tmp_path)
    d.mkdir(parents=True)
    # user-authored note covering the repo by URL, custom content
    (d / "my-custom-name.md").write_text(
        "---\nrepo: https://github.com/me/covered-repo\n---\nMY CAREFUL WORDS",
        encoding="utf-8")
    _mock_repos(monkeypatch, [_repo("covered-repo")])
    out = github_projects_sync(ctx)
    assert out["notes_created"] == 0
    assert "MY CAREFUL WORDS" in (d / "my-custom-name.md").read_text(encoding="utf-8")
    assert not (d / "covered-repo.md").exists()


def test_matches_existing_note_by_filename_stem(ctx, tmp_path, monkeypatch):
    d = _projects_dir(tmp_path)
    d.mkdir(parents=True)
    (d / "brainsync-ai-app.md").write_text("no frontmatter at all", encoding="utf-8")
    _mock_repos(monkeypatch, [_repo("brainsync-ai-app")])
    assert github_projects_sync(ctx)["notes_created"] == 0


def test_skips_forks(ctx, tmp_path, monkeypatch):
    _mock_repos(monkeypatch, [_repo("someone-elses-work", fork=True)])
    assert github_projects_sync(ctx)["notes_created"] == 0


def test_no_connector_skips_cleanly(ctx, monkeypatch):
    def _boom(c, name, args, actor="human"):
        raise RuntimeError("no github connector registered")
    monkeypatch.setattr("amy.tools.invoke", _boom)
    out = github_projects_sync(ctx)
    assert "skipped" in out


def test_job_wired():
    from amy.automation.jobs import DEFAULT_JOBS, HANDLERS
    assert "github_projects_sync" in HANDLERS
    assert any(n == "github_projects_sync" for n, _s in DEFAULT_JOBS)
