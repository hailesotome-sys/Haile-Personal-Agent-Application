from pathlib import Path

import pytest

from tau_job_application.application_pack import (
    LLMDrafter, build_application_pack, build_evidence_inventory, check_claims, load_skill,
)
from tau_job_application.fit_check import CandidatePreferences, assess_fit
from tau_job_application.matching import calculate_match
from tau_job_application.orchestrator import ApplicationAgent, JobInput, render_report
from tau_job_application.parsing import parse_candidate_text, parse_job_text
from tau_job_application.tracker import ApplicationTracker, TransitionError

ROOT = Path(__file__).parents[1]
CV = "Name: Alex Example\nHeadline: Python developer\nSkills: Python, SQL, FastAPI, Git, pytest\nExperience: Built and tested small Python services.\n"
GOOD_JOB = "Title: Junior Backend Engineer\nCompany: Example Systems\nLocation: Zurich or remote\nRequired skills: Python, SQL, Docker\nPreferred skills: FastAPI, AWS\n"
POOR_JOB = "Title: Embedded Engineer\nCompany: Chip Co\nLocation: Zurich\nRequired skills: Verilog, FPGA, RTOS\n"


def _analysis(job_text: str = GOOD_JOB):
    candidate, job = parse_candidate_text(CV), parse_job_text(job_text)
    return candidate, job, calculate_match(candidate, job)


def test_fit_verdicts_follow_required_coverage() -> None:
    _, job, match = _analysis()
    fit = assess_fit(job, match)
    assert fit.verdict == "Potential Fit"
    assert any("Docker" in gap for gap in fit.critical_gaps)
    assert fit.should_prepare_application

    _, poor_job, poor_match = _analysis(POOR_JOB)
    assert assess_fit(poor_job, poor_match).verdict == "Not a Fit"


def test_hard_filters_fail_closed() -> None:
    _, job, match = _analysis()
    excluded = assess_fit(job, match, CandidatePreferences(excluded_companies=["example systems"]))
    assert excluded.verdict == "Not a Fit" and not excluded.should_prepare_application
    elsewhere = assess_fit(job, match, CandidatePreferences(target_locations=["Berlin"], remote_ok=False))
    assert elsewhere.verdict == "Not a Fit"
    remote = assess_fit(job, match, CandidatePreferences(target_locations=["Berlin"], remote_ok=True))
    assert remote.verdict == "Potential Fit"
    picky = assess_fit(job, match, CandidatePreferences(minimum_verdict="Strong Fit"))
    assert not picky.should_prepare_application


def test_inventory_marks_missing_evidence() -> None:
    candidate, job, match = _analysis()
    rows = {row.employer_need: row.strength for row in build_evidence_inventory(candidate, job, match)}
    assert rows["Python"] == "Strong" and rows["Docker"] == "MISSING"


def test_template_drafts_never_claim_missing_skills() -> None:
    candidate, job, match = _analysis()
    pack = build_application_pack(candidate, job, match, assess_fit(job, match))
    assert not [issue for issue in pack.claim_check.issues if issue.kind != "open_gap"]
    assert "Docker" not in pack.resume_draft
    assert pack.claim_check.status == "needs_review"


def test_claim_check_blocks_invented_numbers_and_flags_skills() -> None:
    candidate, job, _ = _analysis()
    check = check_claims({"cover_letter": "I cut latency by 40% using Docker and Kubernetes."}, candidate, job)
    kinds = {(issue.kind, issue.text) for issue in check.issues}
    assert check.status == "blocked"
    assert ("unsupported_number", "40") in kinds
    assert ("unsupported_skill", "Kubernetes") in kinds


def test_llm_drafter_uses_skill_instructions() -> None:
    calls = []

    def fake_complete(system: str, prompt: str) -> str:
        calls.append((system, prompt))
        return "Alex Example\nPython, SQL"

    candidate, job, match = _analysis()
    pack = build_application_pack(candidate, job, match, assess_fit(job, match), LLMDrafter(fake_complete, ROOT / "Agent_skills"))
    assert pack.drafted_by == "llm" and pack.claim_check.status == "ok"
    assert "Resume Tailor Skill" in calls[0][0] and "Cover Letter Writer Skill" in calls[1][0]
    assert "untrusted data" in calls[0][0] and "EVIDENCE INVENTORY" in calls[0][1]
    assert load_skill(ROOT / "Agent_skills", "fit_check").startswith("---")


def test_agent_drafts_skips_and_never_approves(tmp_path: Path) -> None:
    tracker = ApplicationTracker(tmp_path / "db.sqlite")
    agent = ApplicationAgent(tracker, export_dir=tmp_path / "packs")
    report = agent.run(CV, [JobInput(text=GOOD_JOB), JobInput(text=POOR_JOB), JobInput(text="nothing useful here")])
    outcomes = [result.outcome for result in report.results]
    assert outcomes == ["drafted", "skipped", "error"]
    drafted = report.results[0]
    assert Path(drafted.pack_path).exists()
    assert tracker.get(drafted.application_id).status == "drafted"
    assert "Nothing was submitted" in render_report(report)
    with pytest.raises(TransitionError):
        tracker.transition(drafted.application_id, "approved", actor="agent")

    again = agent.run(CV, [JobInput(text=GOOD_JOB), JobInput(text=POOR_JOB)])
    assert [result.outcome for result in again.results] == ["drafted", "unchanged"]
    assert len(tracker.list()) == 2


def test_user_review_gates_approval_and_tracks_outcomes(tmp_path: Path) -> None:
    tracker = ApplicationTracker(tmp_path / "db.sqlite")
    agent = ApplicationAgent(tracker, export_dir=tmp_path / "packs")
    result = agent.run(CV, [JobInput(text=GOOD_JOB)]).results[0]
    app_id, path = result.application_id, Path(result.pack_path)

    with pytest.raises(TransitionError, match="GAP"):
        tracker.transition(app_id, "approved", actor="user")
    with pytest.raises(TransitionError):
        tracker.transition(app_id, "applied", actor="user")

    path.write_text(path.read_text().replace("[GAP", "(filled by user) "), encoding="utf-8")
    assert tracker.transition(app_id, "approved", actor="user").status == "approved"
    assert "(filled by user)" in tracker.get(app_id).pack_markdown

    rerun = agent.run(CV, [JobInput(text=GOOD_JOB)]).results[0]
    assert rerun.outcome == "unchanged"

    applied = tracker.transition(app_id, "applied", actor="user", note="Submitted on the careers page")
    assert applied.follow_up_at and "careers page" in applied.notes
    from datetime import UTC, datetime, timedelta
    assert tracker.due_follow_ups() == []
    assert [row.id for row in tracker.due_follow_ups(datetime.now(UTC) + timedelta(days=8))] == [app_id]
    assert tracker.transition(app_id, "rejected", actor="user").follow_up_at is None
    assert [event["actor"] for event in tracker.history(app_id)] == ["agent", "agent", "user", "user", "user"]


def test_blocked_pack_cannot_be_approved(tmp_path: Path) -> None:
    tracker = ApplicationTracker(tmp_path / "db.sqlite")
    row = tracker.record_assessment(company="A", job_title="B", job_url=None, verdict="Strong Fit", match_score=90)
    tracker.save_pack(row.id, pack_markdown="Cut costs by 40%", claim_status="blocked")
    with pytest.raises(TransitionError, match="blocked"):
        tracker.transition(row.id, "approved", actor="user")



def test_tau_completion_adapter_returns_model_text() -> None:
    from tau_agent.messages import AssistantMessage, TextContent
    from tau_ai import AssistantDoneEvent, AssistantStartEvent, FakeProvider

    from tau_job_application.agent import build_completion

    reply = AssistantMessage(content=[TextContent(text="Draft text")])
    provider = FakeProvider([[AssistantStartEvent(partial=AssistantMessage()), AssistantDoneEvent(reason="stop", message=reply)]])
    complete = build_completion(provider=provider, model="fake")
    assert complete("system rules", "write it") == "Draft text"
    assert provider.calls[0][1] == "system rules" and provider.calls[0][3] == []
