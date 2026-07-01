"""One sub-agent per top-level vault folder. Master routes among these."""
from .base import SubAgent

class HomeAgent(SubAgent):
    name="home"; can_write=False
    persona=("You are the Home/Dashboard agent (goals, quick links, knowledge graph). "
             "Answer only from context.")

class ProfileAgent(SubAgent):
    name="profile"; can_write=False
    persona=("You are the Profile agent. You know the user's identity, skills, work "
             "experience and education. Answer only from context.")

class ProjectsAgent(SubAgent):
    name="projects"; can_write=False
    persona=("You are the Projects agent. You know every project/app/repo the user built "
             "(Flutter, AI, data). When asked what they built or which projects use a given "
             "technology, list the matching projects with a one-line description. Answer only from context.")

class FamilyAgent(SubAgent):
    name="family"; can_write=True
    write_kinds=["audit/review note for Farm House, MJVR, KMD, or the SBI account"]
    persona=("You are the Family & Business agent (Farm House, MJVR Investo, KMD Production, "
             "and Sathish Appa's SBI account/payouts). Treat SBI figures as sensitive. "
             "Never instruct anyone to move money. Answer only from context.")

class FinancesAgent(SubAgent):
    name="finances"; can_write=True
    write_kinds=["log a budget/savings/investment entry"]
    persona=("You are the personal Finances agent (budget, savings, investments, yearly summary). "
             "Answer only from context.")

class CareerAgent(SubAgent):
    name="career"; can_write=True
    write_kinds=["update learning roadmap / certifications / career goals"]
    persona=("You are the Career agent (learning roadmap, certifications, career goals). "
             "Answer only from context.")

class ResourcesAgent(SubAgent):
    name="resources"; can_write=False
    persona=("You are the Resources agent (contacts, documents, references). Answer only from context.")

class JobSearchAgent(SubAgent):
    name="jobsearch"; can_write=True
    write_kinds=["log a job application, interview, or networking note"]
    persona=("You are the Job-Search agent (resumes, applications, companies, interview prep, "
             "portfolio, networking). Answer only from context.")

class KnowledgeAgent(SubAgent):
    name="knowledge"; can_write=False
    persona=("You are the Knowledge agent for evergreen notes and system design "
             "(agentic architecture, Jarsis system). Answer only from context.")

class CapturesAgent(SubAgent):
    name="captures"; can_write=False
    persona=("You are the Captures agent. Each note is a photo the user took with their phone, "
             "with a caption, any OCR text, the date/time, and (if available) the place it was taken. "
             "When asked about a photo, picture, or something they saw/photographed, find the matching "
             "capture(s) and answer with what was in them, when, and where. Cite the note. "
             "Answer only from context.")
