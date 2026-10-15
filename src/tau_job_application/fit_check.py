"""Deterministic fit verdict mirroring the `fit-check` skill's output format.

The verdict is computed from the evidence-based match and the user's own hard
preferences. Missing evidence never counts as a pass, and an unknown preference
check is reported as unknown rather than silently passed.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from tau_job_application.models import JobPosting, MatchResult, RequirementStatus

Verdict = Literal["Strong Fit", "Potential Fit", "Stretch", "Not a Fit"]
VERDICT_ORDER: tuple[Verdict, ...] = ("Not a Fit", "Stretch", "Potential Fit", "Strong Fit")


class CandidatePreferences(BaseModel):
    """User-owned hard constraints. Empty lists mean "no constraint"."""

    target_locations: list[str] = Field(default_factory=list)
    remote_ok: bool = True
    excluded_companies: list[str] = Field(default_factory=list)
    minimum_verdict: Verdict = "Stretch"


class HardFilter(BaseModel):
    name: str
    result: Literal["pass", "fail", "unknown"]
    detail: str


class FitAssessment(BaseModel):
    verdict: Verdict
    summary: str
    hard_filters: list[HardFilter]
    strongest_alignments: list[str]
    critical_gaps: list[str]
    uncertain: list[str]
    recommendation: str
    should_prepare_application: bool


def assess_fit(job: JobPosting, match: MatchResult, preferences: CandidatePreferences | None = None) -> FitAssessment:
    preferences = preferences or CandidatePreferences()
    required = [item for item in match.requirements if item.required]
    matched = [item.skill for item in match.requirements if item.status == RequirementStatus.MATCHED]
    missing_required = [item.skill for item in required if item.status == RequirementStatus.MISSING]
    missing_preferred = [item.skill for item in match.requirements if not item.required and item.status == RequirementStatus.MISSING]
    uncertain = [item.skill for item in match.requirements if item.status == RequirementStatus.UNKNOWN]

    filters = _hard_filters(job, preferences)
    failed = [item for item in filters if item.result == "fail"]

    if failed:
        verdict: Verdict = "Not a Fit"
    elif match.required_score >= 0.85 and match.score >= 75:
        verdict = "Strong Fit"
    elif match.required_score >= 0.6:
        verdict = "Potential Fit"
    elif match.required_score >= 0.3:
        verdict = "Stretch"
    else:
        verdict = "Not a Fit"

    meets_minimum = VERDICT_ORDER.index(verdict) >= VERDICT_ORDER.index(preferences.minimum_verdict)
    gaps = [f"Required: {skill} (no confirmed evidence in the CV)" for skill in missing_required]
    gaps += [f"Preferred: {skill}" for skill in missing_preferred]
    return FitAssessment(
        verdict=verdict,
        summary=_summary(job, verdict, match, failed, missing_required),
        hard_filters=filters,
        strongest_alignments=[f"{skill}: confirmed in the CV and requested by the role" for skill in matched],
        critical_gaps=gaps,
        uncertain=uncertain,
        recommendation=_recommendation(verdict, meets_minimum, matched, missing_required),
        should_prepare_application=verdict != "Not a Fit" and meets_minimum,
    )


def _hard_filters(job: JobPosting, preferences: CandidatePreferences) -> list[HardFilter]:
    filters: list[HardFilter] = []
    company = job.company.casefold()
    excluded = next((name for name in preferences.excluded_companies if name.casefold() == company), None)
    filters.append(HardFilter(
        name="Company not excluded",
        result="fail" if excluded else "pass",
        detail=f"{job.company} is on your exclusion list." if excluded else "Not on your exclusion list.",
    ))
    if preferences.target_locations:
        location = (job.location or "").casefold()
        if not location:
            filters.append(HardFilter(name="Location", result="unknown", detail="The posting does not state a location; check it manually."))
        elif any(target.casefold() in location for target in preferences.target_locations):
            filters.append(HardFilter(name="Location", result="pass", detail=f"{job.location} matches your target locations."))
        elif preferences.remote_ok and "remote" in location:
            filters.append(HardFilter(name="Location", result="pass", detail=f"{job.location} allows remote work."))
        else:
            filters.append(HardFilter(name="Location", result="fail", detail=f"{job.location} is outside your target locations."))
    return filters


def _summary(job: JobPosting, verdict: Verdict, match: MatchResult, failed: list[HardFilter], missing_required: list[str]) -> str:
    if failed:
        return f"{verdict} for {job.title} at {job.company}: " + "; ".join(item.detail for item in failed)
    coverage = round(match.required_score * 100)
    text = f"{verdict} for {job.title} at {job.company}: confirmed evidence covers {coverage}% of required skills (match {match.score}/100)."
    if missing_required:
        text += " Missing required evidence: " + ", ".join(missing_required) + "."
    return text


def _recommendation(verdict: Verdict, meets_minimum: bool, matched: list[str], missing_required: list[str]) -> str:
    if verdict == "Not a Fit":
        return "Skip this role, or fix the failed hard filter or the missing must-haves before applying."
    if not meets_minimum:
        return f"{verdict} is below your minimum verdict, so no application pack is prepared."
    lead = "Lead with " + ", ".join(matched[:3]) + "." if matched else "Confirm your relevant skills before drafting."
    if missing_required:
        return f"Apply, but address the gaps honestly ({', '.join(missing_required)}): do not claim them. {lead}"
    return f"Apply. {lead}"
