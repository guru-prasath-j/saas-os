"""Job discovery agent: searches and crawls job portals for relevant postings."""
from __future__ import annotations
import json
from ...llm import LLMRouter

def discover_jobs(llm: LLMRouter, query: str) -> list[dict]:
    """Generates discovered job listings based on search query.
    In production, this would integrate with job search APIs (LinkedIn, Indeed, etc.) or web scraping.
    For this agentic implementation, we leverage the LLM to simulate structured job search results.
    """
    sys_prompt = (
        "You are an AI Job Discovery Agent. Generate a list of 3 highly realistic and relevant job postings "
        "matching the user's search query. "
        "Return ONLY a valid JSON array of objects, where each object has these exact keys: "
        '"title" (string), "company" (string), "url" (string), '
        '"salary" (string), "requirements" (array of strings), '
        '"benefits" (array of strings), "description" (string). '
        "Do not include any other markdown formatting or text outside the JSON."
    )
    
    prompt = f"Search query: {query}"
    
    try:
        content, _ = llm.generate(sys_prompt, prompt, sensitive=False)
        content_clean = content.strip()
        if content_clean.startswith("```"):
            lines = content_clean.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            content_clean = "\n".join(lines).strip()
            
        start = content_clean.find("[")
        end = content_clean.rfind("]")
        if start != -1 and end != -1:
            content_clean = content_clean[start:end+1]
            
        jobs = json.loads(content_clean)
        if isinstance(jobs, list):
            return jobs
    except Exception:
        pass
        
    return []
