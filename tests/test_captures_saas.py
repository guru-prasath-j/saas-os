"""Per-user captures test (SaaS Phase 3).

Verifies a capture is written into the correct user's vault and never leaks into
another user's vault. Caption/OCR is skipped (api_key="") so the test is offline.

Run:  pytest tests/test_captures_saas.py -v
"""
import os
import tempfile

os.environ["AMY_SAAS_DATA"] = tempfile.mkdtemp(prefix="amy_cap_test_")

from amy import captures  # noqa: E402
from amy.saas import paths, tenancy  # noqa: E402

USER_A = "capaaaa000000001"
USER_B = "capbbbb000000002"

# 1x1 PNG (bytes content is irrelevant since captioning is skipped)
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
       b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00"
       b"\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


def setup_module(_):
    tenancy.ensure_dirs(USER_A)
    tenancy.ensure_dirs(USER_B)


def test_capture_lands_in_correct_user_vault():
    res = captures.ingest(
        PNG, filename="shot.png", taken_at="2026-06-22T10:00:00+05:30",
        source="test", vault=paths.vault_dir(USER_A), openai_api_key="",
    )
    assert res.note_path.startswith("08_Captures/")
    assert not res.duplicate

    a_note = paths.vault_dir(USER_A) / res.note_path
    a_img = paths.vault_dir(USER_A) / res.image_path
    assert a_note.exists()
    assert a_img.exists()

    # user B must have nothing
    b_caps = paths.vault_dir(USER_B) / "08_Captures"
    assert not b_caps.exists() or not any(b_caps.rglob("*.md"))


def test_capture_dedup_by_hash():
    first = captures.ingest(PNG, filename="dup.png", taken_at="2026-06-22T11:00:00+05:30",
                            source="test", vault=paths.vault_dir(USER_A), openai_api_key="")
    second = captures.ingest(PNG, filename="dup.png", taken_at="2026-06-22T11:00:00+05:30",
                             source="test", vault=paths.vault_dir(USER_A), openai_api_key="")
    assert second.duplicate is True
    assert first.hash == second.hash
