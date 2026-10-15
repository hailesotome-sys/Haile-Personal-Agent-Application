"""Deterministic fit verdict mirroring the `fit-check` skill's output format.

The verdict is computed from the evidence-based match and the user's own hard
preferences. Missing evidence never counts as a pass, and an unknown preference
check is reported as unknown rather than silently passed.
"""

from __future__ import annotations

from datetime import date
import re
from typing import Literal

from pydantic import BaseModel, Field

from tau_job_application.models import JobPosting, MatchResult, RequirementStatus

Verdict = Literal["Strong Fit", "Potential Fit", "Stretch", "Not a Fit"]
MIN_SCOREABLE_FOR_STRONG = 3
VERDICT_ORDER: tuple[Verdict, ...] = ("Not a Fit", "Stretch", "Potential Fit", "Strong Fit")


class CandidatePreferences(BaseModel):
    """User-owned hard constraints. Empty lists mean "no constraint"."""

    target_locations: list[str] = Field(default_factory=list)
    remote_ok: bool = True
    excluded_companies: list[str] = Field(default_factory=list)
    minimum_verdict: Verdict = "Stretch"
    available_from: date | None = None


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

    scoreable = [item for item in match.requirements if item.status != RequirementStatus.UNKNOWN]
    if failed:
        verdict: Verdict = "Not a Fit"
    elif len(scoreable) < MIN_SCOREABLE_FOR_STRONG and match.required_score >= 0.6:
        # Two matched languages are not proof of fit: too little was extracted to be confident.
        verdict = "Potential Fit"
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
        recommendation=("Skip: " + " ".join(item.detail for item in failed)) if failed
        else _recommendation(verdict, meets_minimum, matched, missing_required),
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
    if preferences.available_from:
        start = job_start_date(job.description or "")
        wanted = preferences.available_from
        if start is None:
            filters.append(HardFilter(name="Start date", result="unknown", detail="No start date found in the posting; check it."))
        elif (start.year, start.month) < (wanted.year, wanted.month):
            filters.append(HardFilter(name="Start date", result="fail",
                                      detail=f"Starts {start:%B %Y}, before you are available ({wanted:%B %Y})."))
        else:
            filters.append(HardFilter(name="Start date", result="pass", detail=f"Starts {start:%B %Y}."))
    return filters


MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7,
    "août": 8, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
}
START_CUE = re.compile(r"\b(?:start(?:s|ing)?(?: date)?|begin(?:s|ning)?|join(?:s)?\b[^.\n]{0,40}?\bin|"
                       r"à pourvoir|a pourvoir|dès|début|démarrage|available from)\b", re.I)
IMMEDIATE = re.compile(r"as soon as possible|\basap\b|immediately|dès à présent|dès que possible|immédiat", re.I)
MONTH_YEAR = re.compile(r"(?:(\d{1,2})\s+)?(" + "|".join(MONTHS) + r")\s+(20\d{2})", re.I)


def job_start_date(text: str, today: date | None = None) -> date | None:
    """The posting's start date when it states one near a start cue; None when unclear."""
    for cue in START_CUE.finditer(text):
        window = text[cue.start():cue.start() + 120]
        if IMMEDIATE.search(window[:60]):
            return today or date.today()
        if match := MONTH_YEAR.search(window):
            return date(int(match[3]), MONTHS[match[2].casefold()], int(match[1] or 1))
    return None


def _summary(job: JobPosting, verdict: Verdict, match: MatchResult, failed: list[HardFilter], missing_required: list[str]) -> str:
    if failed:
        return f"{verdict} for {job.title} at {job.company}: " + "; ".join(item.detail for item in failed)
    coverage = round(match.required_score * 100)
    text = f"{verdict} for {job.title} at {job.company}: confirmed evidence covers {coverage}% of required skills (match {match.score}/100)."
    if missing_required:
        text += " Missing required evidence: " + ", ".join(missing_required) + "."
    scoreable = [item for item in match.requirements if item.status != RequirementStatus.UNKNOWN]
    if len(scoreable) < MIN_SCOREABLE_FOR_STRONG:
        text += f" Only {len(scoreable)} requirement(s) could be scored, so read the posting yourself before relying on this."
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
