"""Vault import test (SaaS Phase 2).

Imports a zip into a user's vault via the background worker (run synchronously)
and verifies notes load AND that tenants stay isolated after import.

Run from _Amy/:   pytest tests/test_vault_import.py -v
"""
import io
import os
import tempfile
import zipfile

os.environ["AMY_SAAS_DATA"] = tempfile.mkdtemp(prefix="amy_import_test_")

from amy.saas import imports, paths, tenancy  # noqa: E402
from amy.saas.db import ImportJob, SessionLocal, init_db  # noqa: E402

USER_A = "imptaaaa00000001"
USER_B = "imptbbbb00000002"


def _make_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _run(user_id: str, zip_bytes: bytes) -> ImportJob:
    up = paths.uploads_dir(user_id)
    up.mkdir(parents=True, exist_ok=True)
    zpath = up / "test.zip"
    zpath.write_bytes(zip_bytes)

    db = SessionLocal()
    job = ImportJob(user_id=user_id, status="pending")
    db.add(job)
    db.commit()
    job_id = job.id
    db.close()

    imports.run_import(job_id, user_id, str(zpath), replace=True)

    db = SessionLocal()
    job = db.get(ImportJob, job_id)
    db.refresh(job)
    db.close()
    return job


def setup_module(_):
    init_db()


def test_import_loads_notes():
    zb = _make_zip({
        "Work/Project.md": "# Project\n\nA work note. WORKWORD.\n",
        "Health/Run.md": "# Run\n\nHealth note. HEALTHWORD.\n",
        "README.txt": "ignored, not markdown",
    })
    job = _run(USER_A, zb)
    assert job.status == "done", job.error
    assert job.markdown_notes == 2
    assert job.notes_loaded == 2


def test_import_keeps_tenants_isolated():
    _run(USER_A, _make_zip({"Work/A.md": "# A\n\nALPHAONLY for user A.\n"}))
    _run(USER_B, _make_zip({"Notes/B.md": "# B\n\nBETAONLY for user B.\n"}))

    eng_a = tenancy.get_engine(USER_A)
    eng_b = tenancy.get_engine(USER_B)

    a_paths = {n.path for n in eng_a.notes}
    b_paths = {n.path for n in eng_b.notes}

    assert "Work/A.md" in a_paths and "Notes/B.md" not in a_paths
    assert "Notes/B.md" in b_paths and "Work/A.md" not in b_paths

    # retrieval must not cross tenants even when searching the other's codeword
    assert all(n.path != "Notes/B.md" for n in eng_a.retriever.search("BETAONLY", k=10))


def test_reimport_replaces():
    _run(USER_A, _make_zip({"Old/one.md": "# One\n\nold content\n"}))
    job = _run(USER_A, _make_zip({"New/two.md": "# Two\n\nnew content\n"}))
    eng_a = tenancy.get_engine(USER_A)
    paths_now = {n.path for n in eng_a.notes}
    assert "New/two.md" in paths_now
    assert "Old/one.md" not in paths_now   # replace wiped the old vault
    assert job.notes_loaded == 1
