"""Tests for the Career Specialist Agent (Phase 1)."""
from __future__ import annotations
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock
from amy.vault import Note
from amy.llm import LLMRouter
from amy.retrieval import Retriever
from amy.agents.career import CareerAgent
from amy.intelligence.career import normalizer, discovery
from amy import tools

class MockLLM:
    def __init__(self, responses: dict[str, str]):
        self.responses = responses
        self.name = "mock-llm"

    def generate(self, system: str, prompt: str, context: str = "") -> str:
        for keyword, resp in self.responses.items():
            if keyword in prompt.lower() or keyword in system.lower():
                return resp
        return "{}"

def test_job_normalizer():
    # 1. Test clean_string
    assert normalizer.clean_string("   foo   bar   ") == "foo bar"
    
    # 2. Test LLM extraction
    mock_response = """
    {
        "title": "Staff AI Engineer",
        "company": "DeepMind",
        "url": "https://deepmind.google/careers/123",
        "salary": "$250k - $300k",
        "requirements": ["Python", "TensorFlow", "Jax"],
        "benefits": ["Free lunch", "Remote work"],
        "description": "Develop advanced agentic models."
    }
    """
    router = MagicMock(spec=LLMRouter)
    router.generate.return_value = (mock_response, "mock-openai")
    
    data = normalizer.normalize_job_description(router, "Raw text spec goes here")
    assert data["title"] == "Staff AI Engineer"
    assert data["company"] == "DeepMind"
    assert data["salary"] == "$250k - $300k"
    assert "Jax" in data["requirements"]
    
    # 3. Test generating markdown content
    md = normalizer.generate_job_markdown(data)
    assert 'title: "Staff AI Engineer"' in md
    assert 'company: "DeepMind"' in md
    assert 'status: "draft"' in md
    assert "- TensorFlow" in md

def test_check_duplicate():
    notes = [
        Note(path="06_Job_Search/Company A - Job 1.md", title="Job 1", meta={"category": "job-application", "company": "Company A", "url": "http://comp.a/j1"}),
        Note(path="06_Job_Search/Company B - Job 2.md", title="Job 2", meta={"category": "job-application", "company": "Company B", "url": ""})
    ]
    
    # URL match
    dup1 = normalizer.check_duplicate(notes, "Different Title", "Different Company", url="http://comp.a/j1")
    assert dup1 is not None
    assert dup1.title == "Job 1"
    
    # Company + Title match
    dup2 = normalizer.check_duplicate(notes, "Job 2", "Company B")
    assert dup2 is not None
    assert dup2.path == "06_Job_Search/Company B - Job 2.md"
    
    # No match
    dup3 = normalizer.check_duplicate(notes, "Job 3", "Company B")
    assert dup3 is None

def test_discovery_agent():
    """CAREER AUTOPILOT (docs/AGENT_PLAN.md): discover_jobs() used to ask the
    LLM to fabricate postings — disabled by design now, regardless of what
    the LLM would return. Real discovery lives behind the jobspy MCP
    connector (amy/tools/career_tools.py's job_search tool)."""
    mock_response = """
    [
        {
            "title": "MLE 1",
            "company": "DeepMind",
            "url": "https://deepmind.google/1",
            "salary": "$200k",
            "requirements": ["Jax"],
            "benefits": ["Gym"],
            "description": "Research models"
        }
    ]
    """
    router = MagicMock(spec=LLMRouter)
    router.generate.return_value = (mock_response, "mock-openai")

    jobs = discovery.discover_jobs(router, "Deeplearning jobs")
    assert jobs == []

def test_career_agent_answers():
    # Setup mock retriever and router
    retriever = MagicMock(spec=Retriever)
    retriever.search.return_value = [
        Note(path="01_Profile/Skills.md", title="Skills", meta={"category": "profile"}, body="Python, TensorFlow, ML"),
        Note(path="06_Job_Search/Google - AI.md", title="Google AI", meta={"category": "job-application", "status": "interviewing"})
    ]
    
    router = MagicMock(spec=LLMRouter)
    # Mock search answer
    router.generate.return_value = ('[{"title": "MLE", "company": "DeepMind", "url": "http://dm.co", "salary": "$200k", "requirements": ["Jax"], "benefits": ["Food"], "description": "AI"}]', "mock-openai")
    
    agent = CareerAgent(retriever, router)
    
    # Test Discovery routing — disabled (CAREER AUTOPILOT): no longer
    # fabricates listings, points at the real job_search tool instead.
    res1 = agent.answer("discover jobs for ML Engineer")
    assert "job_search" in res1.answer
    
    # Test Matching routing
    router.generate.return_value = ("Fit Score: 85%", "mock-openai")
    res2 = agent.answer("Calculate my match score for Google AI")
    assert "Fit Score: 85%" in res2.answer
    
    # Test Tailoring routing
    router.generate.return_value = ("Tailored Resume Summary", "mock-openai")
    res3 = agent.answer("tailor resume for Google AI")
    assert "Tailored Resume" in res3.answer
    
    # Test Analytics routing
    res4 = agent.answer("Show me my job search pipeline stats")
    assert "Interviewing:** 1" in res4.answer

def test_career_agent_propose_write_and_apply():
    # Setup temp directory for vault testing
    tmpdir = tempfile.mkdtemp()
    try:
        retriever = MagicMock(spec=Retriever)
        retriever.search.return_value = []
        
        # Mock LLM returning job extraction JSON
        mock_extract = '{"title": "Staff Researcher", "company": "OpenAI", "url": "http://openai.com/1"}'
        router = MagicMock(spec=LLMRouter)
        router.generate.return_value = (mock_extract, "mock-openai")
        
        agent = CareerAgent(retriever, router)
        
        # Test propose_write
        prop = agent.propose_write("log job Staff Researcher at OpenAI url http://openai.com/1")
        assert prop is not None
        assert prop.tool == "create_file"
        assert "OpenAI - Staff Researcher.md" in prop.target
        assert "category: job-application" in prop.payload
        
        # Test applying the proposal
        res = tools.apply(prop, vault=Path(tmpdir))
        assert "applied" in res
        
        file_path = Path(tmpdir) / prop.target
        assert file_path.exists()
        content = file_path.read_text(encoding="utf-8")
        assert 'company: "OpenAI"' in content
        assert 'title: "Staff Researcher"' in content
        
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_clip_endpoint_personal():
    from unittest.mock import patch
    tmpdir = tempfile.mkdtemp()
    try:
        from amy.app import clip_job, JobClipReq
        from amy.engine import Engine
        
        mock_extract = '{"title": "Staff Engineer", "company": "Google", "url": "http://google.com"}'
        router = MagicMock(spec=LLMRouter)
        router.generate.return_value = (mock_extract, "mock-openai")
        
        mock_engine = Engine(vault_path=tmpdir)
        mock_engine.master.classifier.llm = router
        mock_engine.notes = []
        
        with patch("amy.app.get_engine", return_value=mock_engine), \
             patch("amy.app.config.PUBLIC", False):
            req = JobClipReq(raw_text="Staff Engineer at Google", url="http://google.com")
            res = clip_job(req)
            assert res["ok"] is True
            assert res["duplicate"] is False
            assert "Google - Staff Engineer.md" in res["note_path"]
            
            # Check file was written
            file_path = Path(tmpdir) / res["note_path"]
            assert file_path.exists()
            content = file_path.read_text(encoding="utf-8")
            assert 'company: "Google"' in content
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_clip_endpoint_saas():
    from unittest.mock import patch
    tmp_saas_data = tempfile.mkdtemp()
    # Temporarily override saas data root
    from amy.saas import paths as saas_paths
    old_saas_data = saas_paths.SAAS_DATA
    saas_paths.SAAS_DATA = Path(tmp_saas_data)
    try:
        from amy.saas.routers.captures import saas_clip_job, JobClipReq as SaaSJobClipReq
        from amy.saas.db import User
        from amy.saas import tenancy

        user = User(id="user123", email="user@test.com", openai_key_enc=None)
        tenancy.ensure_dirs(user.id)

        mock_extract = '{"title": "Staff Researcher", "company": "OpenAI", "url": "http://openai.com"}'

        with patch("amy.saas.routers.captures._engine_for") as mock_eng_for, \
             patch("amy.saas.routers.captures._user_key", return_value=""), \
             patch("amy.llm.LLMRouter.generate", return_value=(mock_extract, "mock-openai")):
             
            # mock engine for user
            from amy.engine import Engine
            mock_engine = Engine(vault_path=saas_paths.vault_dir(user.id))
            mock_engine.notes = []
            mock_eng_for.return_value = mock_engine
            
            req = SaaSJobClipReq(raw_text="Staff Researcher at OpenAI", url="http://openai.com")
            res = saas_clip_job(req, user=user)
            
            assert res["ok"] is True
            assert res["duplicate"] is False
            assert "OpenAI - Staff Researcher.md" in res["note_path"]
            
            # Verify file exists in user's vault
            file_path = saas_paths.vault_dir(user.id) / res["note_path"]
            assert file_path.exists()
    finally:
        saas_paths.SAAS_DATA = old_saas_data
        shutil.rmtree(tmp_saas_data, ignore_errors=True)

