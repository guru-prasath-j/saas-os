"""Per-user private folders test (SaaS Phase 4).

A folder the user marks private should make its notes 'sensitive', which the LLM
router keeps on the local model (never a cloud key).

Run:  pytest tests/test_private_folders.py -v
"""
import os
import tempfile

os.environ["AMY_SAAS_DATA"] = tempfile.mkdtemp(prefix="amy_priv_test_")

from amy.saas import paths, tenancy  # noqa: E402

USER = "privuser00000001"


def _seed(rel, content):
    p = paths.vault_dir(USER) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def setup_module(_):
    tenancy.invalidate(USER)
    tenancy.ensure_dirs(USER)
    _seed("Finance/budget.md", "# Budget\n\nmy private money stuff\n")
    _seed("Notes/idea.md", "# Idea\n\na public idea\n")


def test_private_folder_marks_notes_sensitive():
    tenancy.invalidate(USER)
    eng = tenancy.get_engine(USER, openai_key=None, sensitive_prefixes=["Finance"])
    by_path = {n.path: n for n in eng.notes}
    assert by_path["Finance/budget.md"].sensitive is True
    assert by_path["Notes/idea.md"].sensitive is False


def test_no_private_folders_means_not_sensitive():
    tenancy.invalidate(USER)
    eng = tenancy.get_engine(USER, openai_key=None, sensitive_prefixes=[])
    by_path = {n.path: n for n in eng.notes}
    assert by_path["Finance/budget.md"].sensitive is False
