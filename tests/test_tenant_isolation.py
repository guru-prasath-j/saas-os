"""Cross-tenant isolation test (SaaS Phase 1).

Proves that one user's engine can NEVER see or retrieve another user's notes.
This is the single most important guarantee of the multi-tenant design.

Run from _Amy/:   pytest tests/test_tenant_isolation.py -v
"""
import os
import tempfile

# point SaaS data at a throwaway dir BEFORE importing the saas modules
os.environ["AMY_SAAS_DATA"] = tempfile.mkdtemp(prefix="amy_saas_test_")

from amy.saas import paths, tenancy  # noqa: E402

USER_A = "aaaaaaaaaaaa1111"
USER_B = "bbbbbbbbbbbb2222"

NOTE_A = "# Alpha\n\nThis note belongs to user A. Codeword ALPHAUNIQUE.\n"
NOTE_B = "# Beta\n\nThis note belongs to user B. Codeword BETAUNIQUE.\n"


def _seed(user_id: str, filename: str, content: str):
    tenancy.ensure_dirs(user_id)
    (paths.vault_dir(user_id) / filename).write_text(content, encoding="utf-8")


def setup_module(_):
    tenancy.invalidate(USER_A)
    tenancy.invalidate(USER_B)
    _seed(USER_A, "alpha.md", NOTE_A)
    _seed(USER_B, "beta.md", NOTE_B)


def test_each_engine_loads_only_its_own_notes():
    eng_a = tenancy.get_engine(USER_A)
    eng_b = tenancy.get_engine(USER_B)

    a_paths = {n.path for n in eng_a.notes}
    b_paths = {n.path for n in eng_b.notes}

    assert "alpha.md" in a_paths
    assert "beta.md" not in a_paths          # A must NOT see B's note
    assert "beta.md" in b_paths
    assert "alpha.md" not in b_paths          # B must NOT see A's note


def test_retrieval_never_crosses_tenants():
    eng_a = tenancy.get_engine(USER_A)
    eng_b = tenancy.get_engine(USER_B)

    # even when A searches for B's exact codeword, B's note must never surface
    a_hits = eng_a.retriever.search("BETAUNIQUE", k=10)
    assert all(n.path != "beta.md" for n in a_hits)

    b_hits = eng_b.retriever.search("ALPHAUNIQUE", k=10)
    assert all(n.path != "alpha.md" for n in b_hits)


def test_separate_collections_and_dirs():
    assert paths.vault_dir(USER_A) != paths.vault_dir(USER_B)
    assert paths.index_dir(USER_A) != paths.index_dir(USER_B)
    assert paths.collection_name(USER_A) != paths.collection_name(USER_B)
