"""Company Discovery + ATS Fast-Track Posting Detection (extends CAREER
AUTOPILOT Phase E) — free-sources-only ATS polling, weekly fan-out, and
free-only LinkedIn slug lookup.

All ATS/GitHub/Himalayas/TheirStack responses constructed here are
SYNTHETIC test fixtures, not real API data. See amy/company_discovery.py's
module docstring for the free-sources-only framing and the excluded-
sources list.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from amy.automation import build_ctx
from amy.collab import CollabDB
from amy.company_discovery import (
    _merge_hits, _persist_hits, _tier2_leg, ats_fast_poll,
    company_discovery_scan, detect_ats_platform, enrich_linkedin_slug,
)


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    from amy.saas import paths
    monkeypatch.setattr(paths, "SAAS_DATA", tmp_path / "saas_data")
    (tmp_path / "connectors").mkdir(parents=True)
    cdb = CollabDB(str(tmp_path / "collab.db"))
    c = build_ctx("u-discover", "discover@example.com", cdb, tmp_path, llm_router=None)
    yield c
    cdb.close()


def _seed_ats_company(ctx, company, platform, slug, is_target=True):
    ctx.store._upsert_ats_platform_for_company(ctx.user_id, company, platform, slug)
    row = ctx.store.get_company_intel(ctx.user_id, company)
    ctx.store.set_company_is_target(ctx.user_id, row["id"], is_target)
    return row["id"]


# ---------------------------------------------------------------------------
# detect_ats_platform — real URL patterns, never a guess
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://boards.greenhouse.io/acme/jobs/12345", ("greenhouse", "acme")),
    ("https://job-boards.greenhouse.io/acme/jobs/12345", ("greenhouse", "acme")),
    ("https://jobs.lever.co/acme/abcd-1234", ("lever", "acme")),
    ("https://jobs.ashbyhq.com/acme/xyz-999", ("ashby", "acme")),
    ("https://example.com/careers/acme", None),
    ("", None),
])
def test_detect_ats_platform(url, expected):
    assert detect_ats_platform(url) == expected


# ---------------------------------------------------------------------------
# add_posting_if_new's opportunistic ATS hook
# ---------------------------------------------------------------------------

def test_add_posting_if_new_populates_ats_platform_on_match(ctx):
    posting = {"title": "Flutter Dev", "company": "Acme", "source": "jobspy",
              "url": "https://boards.greenhouse.io/acme/jobs/1", "keywords": []}
    ctx.store.add_posting_if_new(ctx.user_id, posting)
    row = ctx.store.get_company_intel(ctx.user_id, "Acme")
    assert row["ats_platform"] == "greenhouse"
    assert row["ats_company_slug"] == "acme"


def test_add_posting_if_new_leaves_ats_platform_none_on_unknown_url(ctx):
    posting = {"title": "Flutter Dev", "company": "Beta", "source": "jobspy",
              "url": "https://example.com/careers/beta/1", "keywords": []}
    ctx.store.add_posting_if_new(ctx.user_id, posting)
    row = ctx.store.get_company_intel(ctx.user_id, "Beta")
    assert row is None or row.get("ats_platform") is None


# ---------------------------------------------------------------------------
# ats_fast_poll — target-only scope, real diffing
# ---------------------------------------------------------------------------

def test_ats_fast_poll_skipped_without_target_companies(ctx):
    assert ats_fast_poll(ctx) == {"skipped": "no ATS target companies on file"}


def test_ats_fast_poll_ignores_non_target_companies(ctx, monkeypatch):
    _seed_ats_company(ctx, "Acme", "greenhouse", "acme", is_target=False)

    def fake_fetch(url):
        raise AssertionError("should never poll a non-target company")

    monkeypatch.setattr("amy.company_discovery._fetch_json", fake_fetch)
    assert ats_fast_poll(ctx) == {"skipped": "no ATS target companies on file"}


def test_ats_fast_poll_discovers_and_dedups(ctx, monkeypatch):
    _seed_ats_company(ctx, "Acme", "greenhouse", "acme", is_target=True)

    def fake_fetch(url):
        assert url == "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
        return {"jobs": [{"title": "Flutter Developer",
                          "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                          "location": {"name": "Remote"}}]}

    monkeypatch.setattr("amy.company_discovery._fetch_json", fake_fetch)

    out = ats_fast_poll(ctx)
    assert out == {"companies_polled": 1, "discovered": 1}

    postings = [p for p in ctx.store.list_postings(ctx.user_id) if p["source"] == "ats_greenhouse"]
    assert len(postings) == 1

    # second poll, same postings — nothing new
    out2 = ats_fast_poll(ctx)
    assert out2 == {"companies_polled": 1, "discovered": 0}


# ---------------------------------------------------------------------------
# company_discovery_scan — honest skip, graceful TheirStack degradation
# ---------------------------------------------------------------------------

def test_company_discovery_scan_skipped_without_target_role(ctx):
    assert company_discovery_scan(ctx) == {"skipped": "no target_role on file"}


def test_tier2_leg_degrades_theirstack_gracefully_without_connector(ctx, monkeypatch):
    monkeypatch.setattr("amy.connectors.mcp_call.find_connector_row",
                        lambda uid, name: None)
    hits, availability = _tier2_leg(ctx, "Flutter Developer", "Bengaluru")
    assert availability == {"himalayas": False, "theirstack": False}
    assert hits == {}


def test_company_discovery_scan_completes_despite_no_tier2_connectors(ctx, monkeypatch):
    monkeypatch.setattr("amy.connectors.mcp_call.find_connector_row",
                        lambda uid, name: None)
    ctx.store.set_career_profile(ctx.user_id, target_role="Flutter Developer",
                                 target_location="Bengaluru")
    out = company_discovery_scan(ctx)
    assert out["tier2_availability"] == {"himalayas": False, "theirstack": False}
    assert "companies_found" in out


# ---------------------------------------------------------------------------
# Confidence scoring — the prompt's exact rule
# ---------------------------------------------------------------------------

def test_confidence_high_with_two_sources(ctx):
    hits_a = {"Acme": {"sources": {"himalayas"}, "matched_via": {"keyword"}, "city": ""}}
    hits_b = {"Acme": {"sources": {"github"}, "matched_via": {"keyword"}, "city": "Bengaluru"}}
    merged = _merge_hits(hits_a, hits_b)
    _persist_hits(ctx, merged)
    row = ctx.store.get_company_intel(ctx.user_id, "Acme")
    assert row["confidence"] == "high"
    assert set(row["relevance_tags"]) == {"himalayas", "github"}


def test_confidence_high_with_role_title_and_keyword_match_on_one_source(ctx):
    hits = {"Beta": {"sources": {"himalayas"}, "matched_via": {"role_title", "keyword"}, "city": ""}}
    merged = _merge_hits(hits)
    _persist_hits(ctx, merged)
    row = ctx.store.get_company_intel(ctx.user_id, "Beta")
    assert row["confidence"] == "high"
    assert row["matched_via"] == "both"


def test_confidence_verify_with_single_source_single_match(ctx):
    hits = {"Gamma": {"sources": {"himalayas"}, "matched_via": {"keyword"}, "city": ""}}
    merged = _merge_hits(hits)
    _persist_hits(ctx, merged)
    row = ctx.store.get_company_intel(ctx.user_id, "Gamma")
    assert row["confidence"] == "verify"


# ---------------------------------------------------------------------------
# LinkedIn — free search-lookup only, never a guess
# ---------------------------------------------------------------------------

def test_enrich_linkedin_slug_none_without_connector(ctx, monkeypatch):
    monkeypatch.setattr("amy.connectors.mcp_call.find_connector_row",
                        lambda uid, name: None)
    out = enrich_linkedin_slug(ctx, "Acme")
    assert out == {"linkedin_slug": None}
    row = ctx.store.get_company_intel(ctx.user_id, "Acme")
    assert row is None or row.get("linkedin_slug") is None


def test_enrich_linkedin_slug_from_real_search_result(ctx, monkeypatch):
    def fake_call(uid, store, source, candidates, args, target_style="owner_repo"):
        assert source == "web_search"
        return {"result": {"structured": [
            {"title": "Acme Corp | LinkedIn", "url": "https://www.linkedin.com/company/acme-corp/"}]}}

    monkeypatch.setattr("amy.connectors.mcp_call.call_mcp_tool", fake_call)
    out = enrich_linkedin_slug(ctx, "Acme Corp")
    assert out == {"linkedin_slug": "acme-corp", "linkedin_source": "search_lookup"}
    row = ctx.store.get_company_intel(ctx.user_id, "Acme Corp")
    assert row["linkedin_slug"] == "acme-corp"


# ---------------------------------------------------------------------------
# Excluded sources — never appear anywhere in output
# ---------------------------------------------------------------------------

def test_excluded_sources_never_appear_in_scan_output(ctx, monkeypatch):
    monkeypatch.setattr("amy.connectors.mcp_call.find_connector_row",
                        lambda uid, name: None)
    ctx.store.set_career_profile(ctx.user_id, target_role="Flutter Developer",
                                 target_location="Bengaluru")
    out = company_discovery_scan(ctx)
    blob = str(out).lower()
    for banned in ("naukri", "bayt", "gulftalent", "fantastic.jobs", "crunchbase",
                  "linkedin", "clutch", "techbehemoths", "goodfirms", "tracxn",
                  "yourstory", "magnitt", "cutshort", "wellfound", "instahyre"):
        assert banned not in blob
