"""Career agent: orchestrates job discovery, matching, resume tailoring, and tracking."""
from __future__ import annotations
import datetime
from pathlib import Path
from .base import SubAgent, AgentResult
from ..vault import Note
from .. import tools
from ..intelligence.career import normalizer, discovery, matcher, resume

class CareerAgent(SubAgent):
    name = "career"
    can_write = True
    write_kinds = [
        "log a job application update or interview note",
        "create a new job application note",
        "save a tailored resume or cover letter draft"
    ]
    persona = (
        "You are the Career & Job-Search agent. You have access to the user's profile, "
        "skills, resumes, projects, and job application pipeline. "
        "You can search/discover new jobs, perform match-score analysis, tailor resumes, "
        "and draft cover letters. Answer only from context."
    )

    def answer(self, query: str, retrieval_query: str | None = None,
               extra_context: str | None = None) -> AgentResult:
        low = query.lower()
        
        # 1. Job Discovery intent
        if any(w in low for w in ("discover jobs", "find jobs", "search jobs")):
            # extract search term
            search_query = query
            for w in ("discover jobs for", "find jobs for", "search jobs for", "discover jobs", "find jobs", "search jobs"):
                if w in low:
                    search_query = query[low.find(w) + len(w):].strip()
                    break
            if not search_query:
                search_query = "Software Engineer"
                
            jobs = discovery.discover_jobs(self.llm, search_query)
            if not jobs:
                return AgentResult(self.name, "No jobs matching your request were discovered.", [], False, "discovery")
                
            ans = f"### Discovered Job Listings for '{search_query}':\n\n"
            for i, j in enumerate(jobs, 1):
                ans += (
                    f"{i}. **{j['title']}** at **{j['company']}**\n"
                    f"   - **Salary:** {j['salary']}\n"
                    f"   - **URL:** {j['url']}\n"
                    f"   - **Description:** {j['description']}\n"
                    f"   - **Requirements:** {', '.join(j['requirements'][:3])}...\n\n"
                )
            ans += "Would you like me to analyze/match or log any of these to your pipeline?"
            return AgentResult(self.name, ans, [], False, "discovery")
            
        # 2. AI Matching & Profile Comparison intent
        if any(w in low for w in ("match score", "analyze job", "fit score", "matching", "skill gap")):
            # Retrieve profile notes for matching context
            profile_notes = self.retriever.search("profile skills projects experience", scope_prefixes=["01_Profile"])
            context_profile = "\n\n".join(f"## {n.title}\n{n.body}" for n in profile_notes)
            
            # Retrieve specific job notes if the query refers to a company in the vault
            job_notes = self.retriever.search(query, scope_prefixes=["06_Job_Search"], k=3)
            job_spec = query
            sources = [n.path for n in profile_notes]
            if job_notes:
                job_spec = f"Title: {job_notes[0].title}\nBody: {job_notes[0].body}"
                sources.append(job_notes[0].path)
                
            res = matcher.match_job(self.llm, context_profile, job_spec)
            
            ans = (
                f"### Job Fit Match Score: {res['score']}%\n\n"
                "**Matched Skills:**\n" + "\n".join(f"- {s}" for s in res["matched_skills"]) + "\n\n"
                "**Skill Gaps:**\n" + "\n".join(f"- {s}" for s in res["missing_skills"]) + "\n\n"
                "**Recommendations:**\n" + "\n".join(f"- {r}" for r in res["recommendations"])
            )
            return AgentResult(self.name, ans, sources, False, "matcher")

        # 3. Resume / Cover Letter tailoring intent
        if any(w in low for w in ("tailor resume", "tailor cv", "write cover letter", "draft cover letter")):
            # Retrieve profile notes and existing resumes for tailoring
            resume_notes = self.retriever.search("resume cv profile projects experience", scope_prefixes=["01_Profile", "06_Job_Search"])
            context_resume = "\n\n".join(f"## {n.title}\n{n.body}" for n in resume_notes)
            
            # Retrieve specific job notes if the query refers to a company in the vault
            job_notes = self.retriever.search(query, scope_prefixes=["06_Job_Search"], k=3)
            job_spec = query
            sources = [n.path for n in resume_notes]
            if job_notes:
                job_spec = f"Title: {job_notes[0].title}\nBody: {job_notes[0].body}"
                sources.append(job_notes[0].path)
                
            if "cover letter" in low:
                ans = resume.draft_cover_letter(self.llm, context_resume, job_spec)
                model = "cover-letter"
            else:
                ans = resume.tailor_resume(self.llm, context_resume, job_spec)
                model = "resume-tailor"
                
            return AgentResult(self.name, ans, sources, False, model)
            
        # 4. Analytics / Conversion Pipeline intent
        if any(w in low for w in ("pipeline", "application tracker", "analytics", "job stats")):
            # Search vault for job application notes
            job_notes = self.retriever.search("category: job-application status", scope_prefixes=["06_Job_Search"], k=40)
            
            stats = {"draft": 0, "applied": 0, "interviewing": 0, "offer": 0, "rejected": 0}
            for n in job_notes:
                status = n.meta.get("status") or "draft"
                status_clean = status.lower().strip()
                if status_clean in stats:
                    stats[status_clean] += 1
                    
            ans = (
                "### Job Application Pipeline Analytics:\n\n"
                f"- 📝 **Draft / Discovered:** {stats['draft']}\n"
                f"- 📨 **Applied:** {stats['applied']}\n"
                f"- 📅 **Interviewing:** {stats['interviewing']}\n"
                f"- 🎉 **Offer Received:** {stats['offer']}\n"
                f"- ❌ **Rejected:** {stats['rejected']}\n\n"
                f"**Total Tracked Applications:** {sum(stats.values())}\n"
            )
            return AgentResult(self.name, ans, [n.path for n in job_notes], False, "aggregate")
            
        # 5. Default/Fallback: Read-only vault query
        return super().answer(query, retrieval_query)

    def propose_write(self, query: str) -> tools.WriteProposal | None:
        """Proposes career modifications, such as logging a new job description."""
        if not self.can_write:
            return None
            
        # Check if the user is requesting to log a new job
        low = query.lower()
        if any(w in low for w in ("log job", "save job", "record job", "add job")):
            # Extract company/role using LLM or simple regex
            sys_prompt = (
                "Extract the 'title', 'company', and 'url' of the job mentioned in the prompt. "
                'Reply ONLY JSON: {"title": "<role>", "company": "<company>", "url": "<url>"} or {"error": "not found"}.'
            )
            try:
                out, _ = self.llm.generate(sys_prompt, query, sensitive=False)
                import json
                data = json.loads(out[out.find("{"): out.rfind("}") + 1])
                if "error" not in data:
                    title = data.get("title") or "Software Engineer"
                    company = data.get("company") or "Target Company"
                    url = data.get("url") or ""
                    
                    job_info = {
                        "title": title,
                        "company": company,
                        "url": url,
                        "salary": "Not specified",
                        "requirements": [],
                        "benefits": [],
                        "description": query
                    }
                    
                    target = f"06_Job_Search/{company} - {title}.md".replace("/", "_").replace("06_Job_Search_", "06_Job_Search/")
                    body = normalizer.generate_job_markdown(job_info)
                    
                    return tools.WriteProposal(
                        id=tools.uuid.uuid4().hex[:8],
                        tool="create_file",
                        target=target,
                        preview=f"Create a new Job Posting Note **{target}**:\n\n{body[:250]}...",
                        payload=body,
                        sensitive=False
                    )
            except Exception:
                pass
                
        # Default fallback to parent's Audit Log appending
        return super().propose_write(query)
