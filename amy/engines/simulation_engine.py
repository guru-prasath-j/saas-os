"""Simulation Engine (PIOS) — "what if" scenario modelling.

Given explicit scenario inputs, it computes transparent, deterministic
projections and a recommendation. No randomness, no ML — every number is
explained so the user can trust the reasoning.

Scenarios:
  * job_change       -> simulate_job_change(...)
  * financial_change -> simulate_financial_change(...)
  * learning_path    -> simulate_learning_path(...)
  * project_timeline -> simulate_project_timeline(...)

`simulate(scenario, **params)` dispatches by name.
"""
from __future__ import annotations


class SimulationEngine:
    def __init__(self, collab_db=None):
        # collab_db optional — simulations are pure functions of their inputs,
        # but the store can supply defaults (e.g. current salary) later.
        self.db = collab_db.conn if collab_db is not None else None

    def simulate(self, scenario: str, **params) -> dict:
        fn = {
            "job_change": self.simulate_job_change,
            "financial_change": self.simulate_financial_change,
            "learning_path": self.simulate_learning_path,
            "project_timeline": self.simulate_project_timeline,
        }.get(scenario)
        if fn is None:
            return {"error": f"unknown scenario '{scenario}'",
                    "scenarios": ["job_change", "financial_change",
                                  "learning_path", "project_timeline"]}
        return fn(**params)

    # --- job change -----------------------------------------------------
    def simulate_job_change(self, current_salary: float = 0.0, new_salary: float = 0.0,
                            relocation_cost: float = 0.0, commute_change_min: float = 0.0,
                            **_) -> dict:
        delta = new_salary - current_salary
        pct = (delta / current_salary * 100) if current_salary else None
        months_to_recoup = (relocation_cost / (delta / 12)) if delta > 0 and relocation_cost else None
        if pct is None:
            rec = "Provide your current salary to evaluate the offer."
        elif pct >= 20:
            rec = "Strong raise (≥20%). Likely worth it if the role aligns with your goals."
        elif pct >= 5:
            rec = "Moderate raise. Weigh non-salary factors (growth, commute, culture)."
        elif pct >= 0:
            rec = "Marginal pay change — only move for non-financial reasons."
        else:
            rec = "Pay cut. Justify only with significant growth or lifestyle gains."
        return {
            "scenario": "job_change",
            "salary_delta": round(delta, 2),
            "salary_change_pct": round(pct, 1) if pct is not None else None,
            "annual_after": round(new_salary, 2),
            "relocation_recoup_months": round(months_to_recoup, 1) if months_to_recoup else None,
            "commute_change_min_per_day": commute_change_min,
            "recommendation": rec,
        }

    # --- financial change ----------------------------------------------
    def simulate_financial_change(self, monthly_income: float = 0.0, monthly_expenses: float = 0.0,
                                  one_time_change: float = 0.0, monthly_change: float = 0.0,
                                  horizon_months: int = 12, **_) -> dict:
        base_net = monthly_income - monthly_expenses
        new_net = base_net + monthly_change
        projected = one_time_change + new_net * horizon_months
        if new_net < 0:
            rec = ("This plan runs a monthly deficit — you'd lose "
                   f"{abs(round(new_net,2))}/mo. Cut expenses or raise income.")
        elif new_net < base_net:
            rec = "Cash flow is positive but lower than today. Proceed cautiously."
        else:
            rec = "Cash flow improves. Financially sustainable over the horizon."
        return {
            "scenario": "financial_change",
            "current_monthly_net": round(base_net, 2),
            "new_monthly_net": round(new_net, 2),
            "horizon_months": horizon_months,
            "projected_balance_change": round(projected, 2),
            "recommendation": rec,
        }

    # --- learning path --------------------------------------------------
    def simulate_learning_path(self, total_hours: float = 0.0, hours_per_week: float = 0.0,
                               skill: str = "the skill", **_) -> dict:
        weeks = (total_hours / hours_per_week) if hours_per_week else None
        if weeks is None:
            rec = "Set weekly study hours to estimate a completion date."
        elif hours_per_week < 3:
            rec = (f"At {hours_per_week}h/week, mastering {skill} takes "
                   f"~{round(weeks)} weeks. Consider increasing the pace.")
        else:
            rec = (f"At {hours_per_week}h/week you finish {skill} in "
                   f"~{round(weeks)} weeks — a realistic, sustainable plan.")
        return {
            "scenario": "learning_path",
            "skill": skill,
            "total_hours": total_hours,
            "hours_per_week": hours_per_week,
            "estimated_weeks": round(weeks, 1) if weeks else None,
            "estimated_months": round(weeks / 4.345, 1) if weeks else None,
            "recommendation": rec,
        }

    # --- project timeline ----------------------------------------------
    def simulate_project_timeline(self, total_tasks: int = 0, completed_tasks: int = 0,
                                  tasks_per_week: float = 0.0, deadline_weeks: float = None,
                                  **_) -> dict:
        remaining = max(0, total_tasks - completed_tasks)
        weeks = (remaining / tasks_per_week) if tasks_per_week else None
        on_track = None
        if weeks is not None and deadline_weeks is not None:
            on_track = weeks <= deadline_weeks
        if weeks is None:
            rec = "Provide a throughput (tasks/week) to project a finish date."
        elif on_track is False:
            need = round(remaining / deadline_weeks, 1) if deadline_weeks else None
            rec = (f"Behind schedule. To hit the deadline you need ~{need} tasks/week "
                   f"(currently {tasks_per_week}). Cut scope or add capacity.")
        elif on_track is True:
            rec = "On track to meet the deadline at the current pace."
        else:
            rec = f"At {tasks_per_week} tasks/week you finish in ~{round(weeks)} weeks."
        return {
            "scenario": "project_timeline",
            "remaining_tasks": remaining,
            "tasks_per_week": tasks_per_week,
            "estimated_weeks": round(weeks, 1) if weeks else None,
            "deadline_weeks": deadline_weeks,
            "on_track": on_track,
            "recommendation": rec,
        }
