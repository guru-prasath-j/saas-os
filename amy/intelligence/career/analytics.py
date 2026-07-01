"""Career analytics engine: computes pipeline metrics and conversion rates."""
from __future__ import annotations
from ...vault import Note

def get_pipeline_funnel(notes: list[Note]) -> dict:
    """Analyze the user's job notes and return pipeline stages and conversion statistics."""
    stats = {"draft": 0, "applied": 0, "interviewing": 0, "offer": 0, "rejected": 0}
    interviews = []
    
    for n in notes:
        # Check if the note is a job application
        is_job = False
        if n.category == "job-application" or "job-application" in n.tags or n.path.startswith("06_Job_Search/"):
            is_job = True
            
        if not is_job:
            continue
            
        status = n.meta.get("status") or "draft"
        status_clean = str(status).lower().strip()
        if status_clean in stats:
            stats[status_clean] += 1
            
        if status_clean == "interviewing":
            interviews.append({
                "title": n.title,
                "company": n.meta.get("company") or "Unknown",
                "path": n.path,
                "date": n.meta.get("interview_date") or n.meta.get("updated") or "Not scheduled"
            })
            
    total = sum(stats.values())
    
    # Calculate conversion metrics (avoid division by zero)
    applied_total = stats["applied"] + stats["interviewing"] + stats["offer"] + stats["rejected"]
    applied_to_interview_rate = 0.0
    applied_to_offer_rate = 0.0
    
    if applied_total > 0:
        interviewed_total = stats["interviewing"] + stats["offer"]
        applied_to_interview_rate = round((interviewed_total / applied_total) * 100, 1)
        applied_to_offer_rate = round((stats["offer"] / applied_total) * 100, 1)
        
    return {
        "stages": stats,
        "metrics": {
            "total_tracked": total,
            "total_applied": applied_total,
            "applied_to_interview_rate_pct": applied_to_interview_rate,
            "applied_to_offer_rate_pct": applied_to_offer_rate
        },
        "active_interviews": interviews
    }
