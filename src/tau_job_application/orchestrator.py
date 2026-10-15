"""The application agent: discover → fit check → tailor → cover letter → human review.

It runs every step up to a reviewed application pack and stops there. It cannot
approve its own work, submit an application, or contact anyone: the tracker only
lets the user record those steps.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Literal

from pydantic import BaseModel

from tau_job_application.application_pack import Drafter, TemplateDrafter, build_application_pack, render_pack_markdown
from tau_job_application.fit_check import CandidatePreferences, assess_fit
from tau_job_application.matching import calculate_match
from tau_job_application.models import CandidateProfile, JobPosting
from tau_job_application.parsing import parse_candidate_text, parse_job_text
from tau_job_application.tracker import ApplicationTracker

Outcome = Literal["drafted", "skipped", "unchanged", "error"]
IN_PROGRESS = {"approved", "applied", "interviewing", "offer", "rejected", "withdrawn", "no_response"}


class JobInput(BaseModel):
    text: str
    title: str | None = None
    company: str | None = None
    url: str | None = None


class JobRunResult(BaseModel):
    job_title: str
    company: str
    outcome: Outcome
    application_id: int | None = None
    verdict: str | None = None
    match_score: int | None = None
    claim_status: str | None = None
    pack_path: str | None = None
    reason: str


class RunReport(BaseModel):
    candidate_name: str
    results: list[JobRunResult]

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for result in self.results:
            counts[result.outcome] = counts.get(result.outcome, 0) + 1
        return counts


class ApplicationAgent:
    def __init__(self, tracker: ApplicationTracker, *, export_dir: Path, preferences: CandidatePreferences | None = None,
                 drafter: Drafter | None = None) -> None:
        self.tracker = tracker
        self.export_dir = export_dir
        self.preferences = preferences or CandidatePreferences()
        self.drafter = drafter or TemplateDrafter()

    def run(self, candidate_text: str, jobs: list[JobInput | JobPosting]) -> RunReport:
        candidate = parse_candidate_text(candidate_text)
        results = [self._process(candidate, job) for job in jobs]
        return RunReport(candidate_name=candidate.name, results=results)

    def _process(self, candidate: CandidateProfile, job_input: JobInput | JobPosting) -> JobRunResult:
        try:
            job = job_input if isinstance(job_input, JobPosting) else parse_job_text(
                job_input.text, title=job_input.title, company=job_input.company, url=job_input.url)
        except ValueError as exc:
            title = getattr(job_input, "title", None) or "Unknown role"
            company = getattr(job_input, "company", None) or "Unknown company"
            return JobRunResult(job_title=title, company=company, outcome="error", reason=str(exc))

        existing = self.tracker.find(job.company, job.title, job.url)
        if existing and existing.status in IN_PROGRESS:
            return JobRunResult(job_title=job.title, company=job.company, outcome="unchanged", application_id=existing.id,
                                verdict=existing.verdict, match_score=existing.match_score,
                                reason=f"Already '{existing.status}'; the agent leaves reviewed applications alone.")
        if existing and existing.status == "skipped":
            return JobRunResult(job_title=job.title, company=job.company, outcome="unchanged", application_id=existing.id,
                                verdict=existing.verdict, match_score=existing.match_score,
                                reason="Previously skipped; reopen it yourself to re-assess.")

        match = calculate_match(candidate, job)
        fit = assess_fit(job, match, self.preferences)
        tracked = self.tracker.record_assessment(company=job.company, job_title=job.title, job_url=job.url,
                                                 verdict=fit.verdict, match_score=match.score)
        if not fit.should_prepare_application:
            self.tracker.transition(tracked.id, "skipped", actor="agent", details={"verdict": fit.verdict, "reason": fit.recommendation})
            return JobRunResult(job_title=job.title, company=job.company, outcome="skipped", application_id=tracked.id,
                                verdict=fit.verdict, match_score=match.score, reason=fit.recommendation)

        pack = build_application_pack(candidate, job, match, fit, self.drafter)
        markdown = render_pack_markdown(pack, fit)
        path = self._export(tracked.id, job, markdown)
        self.tracker.save_pack(tracked.id, pack_markdown=markdown, claim_status=pack.claim_check.status, pack_path=path)
        return JobRunResult(job_title=job.title, company=job.company, outcome="drafted", application_id=tracked.id,
                            verdict=fit.verdict, match_score=match.score, claim_status=pack.claim_check.status,
                            pack_path=str(path), reason="Application pack ready for your review.")

    def _export(self, application_id: int, job: JobPosting, markdown: str) -> Path:
        self.export_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", f"{job.company} {job.title}".casefold()).strip("-")[:80]
        path = self.export_dir / f"{application_id:04d}-{slug}.md"
        path.write_text(markdown, encoding="utf-8")
        return path


def render_report(report: RunReport) -> str:
    lines = [f"# Application run for {report.candidate_name}", ""]
    counts = report.counts()
    lines.append(", ".join(f"{count} {outcome}" for outcome, count in counts.items()) or "No jobs processed.")
    lines += ["", "| # | Role | Verdict | Match | Claims | Outcome | Note |", "|---|---|---|---|---|---|---|"]
    for result in report.results:
        lines.append(f"| {result.application_id or '-'} | {result.job_title} at {result.company} | {result.verdict or '-'} | "
                     f"{result.match_score if result.match_score is not None else '-'} | {result.claim_status or '-'} | "
                     f"{result.outcome} | {result.reason} |")
    lines += ["", "Nothing was submitted. Review each pack, then run `approve <id>` and apply yourself."]
    return "\n".join(lines)
