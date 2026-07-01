"""Job normalizer: extracts structured fields from raw job descriptions and handles duplicate checks."""
from __future__ import annotations
import json
import re
from ...vault import Note
from ...llm import LLMRouter

def clean_string(s: str) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip()

def normalize_job_description(llm: LLMRouter, raw_text: str) -> dict:
    """Uses the LLM to parse raw job text/HTML and return structured metadata.
    
    Returns a dict with:
      title: str
      company: str
      url: str
      salary: str
      requirements: list[str]
      benefits: list[str]
      description: str
    """
    sys_prompt = (
        "You are a precise data extractor. Extract job posting details from the raw text/HTML. "
        "Return ONLY a valid JSON object with the keys: "
        '"title" (string), "company" (string), "url" (string, default ""), '
        '"salary" (string, default "Not specified"), "requirements" (array of strings), '
        '"benefits" (array of strings), "description" (string, summary of role). '
        "Do not include any other markdown formatting or text outside the JSON."
    )
    
    # We clip the raw text to fit max tokens / context comfortably
    prompt = f"Raw Job Post:\n\n{raw_text[:4000]}"
    
    try:
        content, _ = llm.generate(sys_prompt, prompt, sensitive=False)
        # Parse JSON from content. Strip backticks/markdown if the LLM wraps it.
        content_clean = content.strip()
        if content_clean.startswith("```"):
            # strip markdown fence
            lines = content_clean.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            content_clean = "\n".join(lines).strip()
            
        # Find first '{' and last '}'
        start = content_clean.find("{")
        end = content_clean.rfind("}")
        if start != -1 and end != -1:
            content_clean = content_clean[start:end+1]
            
        data = json.loads(content_clean)
    except Exception:
        # Graceful fallback in case of LLM parsing failures
        data = {}
        
    return {
        "title": clean_string(data.get("title") or "Unknown Role"),
        "company": clean_string(data.get("company") or "Unknown Company"),
        "url": clean_string(data.get("url") or ""),
        "salary": clean_string(data.get("salary") or "Not specified"),
        "requirements": [clean_string(r) for r in data.get("requirements") or [] if r],
        "benefits": [clean_string(b) for b in data.get("benefits") or [] if b],
        "description": clean_string(data.get("description") or raw_text[:500]),
    }

def generate_job_markdown(job_info: dict) -> str:
    """Generate standardized Obsidian note markdown content with YAML frontmatter."""
    reqs_str = "\n".join(f"  - {r}" for r in job_info["requirements"])
    bens_str = "\n".join(f"  - {b}" for b in job_info["benefits"])
    
    return f"""---
category: job-application
title: "{job_info['title']}"
company: "{job_info['company']}"
url: "{job_info['url']}"
salary: "{job_info['salary']}"
status: "draft"
tags:
  - job-application
  - career
---

# {job_info['title']} at {job_info['company']}

**URL:** {job_info['url'] or 'N/A'}
**Salary:** {job_info['salary']}
**Status:** draft

## Description
{job_info['description']}

## Requirements
{reqs_str if reqs_str else '  - None specified'}

## Benefits
{bens_str if bens_str else '  - None specified'}
"""

def check_duplicate(existing_notes: list[Note], title: str, company: str, url: str | None = None) -> Note | None:
    """Check if the job posting already exists in the user's notes."""
    url_clean = clean_string(url or "").lower()
    title_clean = clean_string(title or "").lower()
    company_clean = clean_string(company or "").lower()
    
    for note in existing_notes:
        if note.category != "job-application":
            # Only compare with other job applications
            if "job-application" not in note.tags and not note.path.startswith("06_Job_Search/"):
                continue
                
        # 1. Exact URL Match
        n_url = clean_string(note.meta.get("url") or "").lower()
        if url_clean and n_url and url_clean == n_url:
            return note
            
        # 2. Company + Title Match
        n_title = clean_string(note.meta.get("title") or note.title or "").lower()
        n_company = clean_string(note.meta.get("company") or "").lower()
        if title_clean and company_clean and n_title == title_clean and n_company == company_clean:
            return note
            
    return None
