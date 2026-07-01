"""AI Matching & Skill Gap Analysis Engine."""
from __future__ import annotations
import json
from ...llm import LLMRouter

def match_job(llm: LLMRouter, profile: str, job_spec: str) -> dict:
    """Compares user profile with job specifications to calculate match score,
    identifying matching skills and skill gaps.
    """
    sys_prompt = (
        "You are an expert AI Career Matcher. Compare the user's profile/skills with the job specification provided. "
        "Calculate a match score between 0 and 100 representing how well the user fits this role. "
        "Identify specific matching skills, critical skill gaps (missing skills/technologies), "
        "and actionable recommendations. "
        "Return ONLY a valid JSON object with the keys: "
        '"score" (integer), "matched_skills" (array of strings), '
        '"missing_skills" (array of strings), "recommendations" (array of strings). '
        "Do not include any other markdown formatting or text outside the JSON."
    )
    
    prompt = f"User Profile & Skills:\n{profile[:3000]}\n\nTarget Job Spec:\n{job_spec[:3000]}"
    
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
            
        start = content_clean.find("{")
        end = content_clean.rfind("}")
        if start != -1 and end != -1:
            content_clean = content_clean[start:end+1]
            
        data = json.loads(content_clean)
        return {
            "score": int(data.get("score") or 0),
            "matched_skills": [str(s) for s in data.get("matched_skills") or []],
            "missing_skills": [str(s) for s in data.get("missing_skills") or []],
            "recommendations": [str(r) for r in data.get("recommendations") or []]
        }
    except Exception:
        pass
        
    return {
        "score": 0,
        "matched_skills": [],
        "missing_skills": [],
        "recommendations": []
    }
