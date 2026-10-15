"""One transparent 0-100 "should I apply?" score with its components.

It is a decision aid built from the fit check, the evidence inventory and the
claim check, not a prediction of being hired. A failed hard filter caps it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from tau_job_application.fit_check import FitAssessment
from tau_job_application.models import MatchResult

HARD_FILTER_CAP = 25
UNSCOREABLE_CAP = 60  # "Apply with care" at most: the score rests on filters alone


class ApplyComponent(BaseModel):
    name: str
    score: int
    weight: int
    explanation: str
    counted: bool = True


class ApplyScore(BaseModel):
    score: int
    label: Literal["Apply", "Apply with care", "Probably skip", "Skip"]
    components: list[ApplyComponent]
    capped_reason: str | None = None


def compute_apply_score(fit: FitAssessment, match: MatchResult, inventory_strengths: list[str],
                        claim_status: str | None) -> ApplyScore:
    passed = sum(item.result == "pass" for item in fit.hard_filters)
    unknown = sum(item.result == "unknown" for item in fit.hard_filters)
    failed = [item for item in fit.hard_filters if item.result == "fail"]
    filters_score = 0 if failed else round(100 * (passed + 0.5 * unknown) / max(1, len(fit.hard_filters)))
    relevant = [strength for strength in inventory_strengths if strength != "Uncertain"]
    strong = sum(strength == "Strong" for strength in relevant)
    partial = sum(strength == "Partial" for strength in relevant)
    evidence_score = round(100 * (strong + 0.5 * partial) / len(relevant)) if relevant else 50
    readiness = {"ok": 100, "needs_review": 70, "blocked": 0}.get(claim_status or "", 50)
    scoreable = bool(relevant)
    components = [
        ApplyComponent(name="Requirement match", score=match.score, weight=45, counted=scoreable,
                       explanation=f"Confirmed evidence covers {round(match.required_score * 100)}% of required and "
                                   f"{round(match.preferred_score * 100)}% of preferred skills." if scoreable else
                                   "Not counted: the posting names no skills the agent can score, so read it and judge the fit yourself."),
        ApplyComponent(name="Hard filters", score=filters_score, weight=25,
                       explanation=f"{passed} passed, {unknown} unknown, {len(failed)} failed (location, start date, exclusions)."),
        ApplyComponent(name="Evidence strength", score=evidence_score, weight=20, counted=scoreable,
                       explanation=f"{strong} strong and {partial} partial pieces of CV evidence for {len(relevant)} scoreable needs."
                                   if scoreable else "Not counted: nothing in the posting to weigh your evidence against."),
        ApplyComponent(name="Pack readiness", score=readiness, weight=10,
                       explanation={"ok": "Claim check passed.", "needs_review": "Placeholders or flagged claims to review.",
                                    "blocked": "The claim check blocked the pack."}.get(claim_status or "", "No pack drafted yet.")),
    ]
    counted = [component for component in components if component.counted]
    score = round(sum(component.score * component.weight for component in counted) / sum(component.weight for component in counted))
    capped = None
    if not scoreable:
        score = min(score, UNSCOREABLE_CAP)
        capped = f"Capped at {UNSCOREABLE_CAP}: without scoreable skills the agent cannot vouch for the fit."
    if failed:
        score = min(score, HARD_FILTER_CAP)
        capped = "Capped at 25 because a hard filter failed: " + " ".join(item.detail for item in failed)
    label: Literal["Apply", "Apply with care", "Probably skip", "Skip"]
    if failed:
        label = "Skip"
    elif score >= 75:
        label = "Apply"
    elif score >= 55:
        label = "Apply with care"
    else:
        label = "Probably skip"
    return ApplyScore(score=score, label=label, components=components, capped_reason=capped)
