"""AI Resume Tailoring and Cover Letter Drafting Engine."""
from __future__ import annotations
from ...llm import LLMRouter

def tailor_resume(llm: LLMRouter, resume_text: str, job_spec: str) -> str:
    """Tailor resume highlights/skills to fit the target job specification."""
    sys_prompt = (
        "You are a professional resume writer. Review the user's resume/experience and the target job description. "
        "Tailor the resume to highlight matching skills, keywords, and project impacts. "
        "Maintain absolute honesty, but present the user's achievements in the most compelling alignment to the job description."
    )
    prompt = f"Original Resume/Skills:\n{resume_text}\n\nJob Description:\n{job_spec}"
    content, _ = llm.generate(sys_prompt, prompt, sensitive=False)
    return content

def draft_cover_letter(llm: LLMRouter, profile: str, job_spec: str) -> str:
    """Draft a professional cover letter linking user's accomplishments to job requirements."""
    sys_prompt = (
        "You are a seasoned career counselor and professional writer. Draft a custom cover letter "
        "addressing the hiring team. Highlight the key areas where the user's profile "
        "perfectly aligns with the target job's requirements, expressing genuine enthusiasm and clear fit."
    )
    prompt = f"User Profile/Experience:\n{profile}\n\nJob Description:\n{job_spec}"
    content, _ = llm.generate(sys_prompt, prompt, sensitive=False)
    return content
