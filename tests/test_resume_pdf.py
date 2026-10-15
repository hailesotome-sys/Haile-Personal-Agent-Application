import sqlite3
from io import BytesIO

from pypdf import PdfReader

from tau_job_application.application_pack import build_evidence_inventory
from tau_job_application.apply_score import compute_apply_score
from tau_job_application.fit_check import CandidatePreferences, assess_fit
from tau_job_application.matching import calculate_match
from tau_job_application.orchestrator import ApplicationAgent, JobInput, render_report
from tau_job_application.parsing import parse_candidate_text, parse_job_text
from tau_job_application.resume_document import (
    parse_resume, parse_resume_markdown, pdf_preview_png, render_resume_markdown, render_resume_pdf, tailor_resume_document,
)
from tau_job_application.tracker import ApplicationTracker

CV = """Sam Example
sam@example.com | +33 6 00 00 00 00 | Paris
EDUCATION
SKEMA BUSINESS SCHOOL Paris, France
MSc Corporate Financial Management 2025 – 2027
• Courses in budgeting, forecasting and financial reporting
EXPERIENCE
EXAMPLE BANK Paris, France
Finance intern Jan 2026 – Jun 2026
• Organised team events and onboarding sessions
• Built the monthly budgeting pack in Excel for 12 cost centres
• Prepared forecasting notes for the CFO
SKILLS
Skills: Excel, Budgeting, Forecasting, Financial reporting
"""
JOB = ("Title: FP&A Graduate\nCompany: Example Group\nLocation: Paris\n"
       "Required skills: Excel, Budgeting, Forecasting\nPreferred skills: Power BI\n")


def test_cv_is_parsed_into_sections_and_entries() -> None:
    document = parse_resume(CV)
    assert document.name == "Sam Example"
    assert "sam@example.com" in document.contact
    assert [section.title for section in document.sections] == ["EDUCATION", "EXPERIENCE", "SKILLS"]
    internship = document.sections[1].entries[0]
    assert (internship.organisation, internship.location) == ("EXAMPLE BANK", "Paris, France")
    assert (internship.role, internship.dates) == ("Finance intern", "Jan 2026 – Jun 2026")
    assert len(internship.bullets) == 3


def test_tailoring_only_reorders_and_explains() -> None:
    document = parse_resume(CV)
    tailored, changes = tailor_resume_document(document, "FP&A Graduate", "Example Group", ["Excel", "Budgeting", "Forecasting"])
    bullets = tailored.sections[1].entries[0].bullets
    assert bullets[0].startswith("Built the monthly budgeting pack")
    assert sorted(bullets) == sorted(document.sections[1].entries[0].bullets)
    assert tailored.target == "Application: FP&A Graduate, Example Group"
    assert [change.kind for change in changes][:2] == ["Added", "Moved up"]
    assert "Excel" in changes[1].detail and changes[-1].kind == "Kept"


def test_pdf_is_one_page_and_markdown_edits_round_trip() -> None:
    tailored, _ = tailor_resume_document(parse_resume(CV), "FP&A Graduate", "Example Group", ["Excel"])
    pdf = render_resume_pdf(tailored)
    assert len(PdfReader(BytesIO(pdf)).pages) == 1
    assert "EXAMPLE BANK" in PdfReader(BytesIO(pdf)).pages[0].extract_text()
    assert pdf_preview_png(pdf).startswith(b"\x89PNG")
    markdown = render_resume_markdown(tailored)
    assert parse_resume_markdown(markdown) == tailored
    edited = parse_resume_markdown(markdown.replace("Prepared forecasting notes", "Wrote forecasting notes"))
    assert "Wrote forecasting notes for the CFO" in edited.sections[1].entries[0].bullets


def test_apply_score_weights_and_hard_filter_cap() -> None:
    candidate = parse_candidate_text(CV)
    job = parse_job_text(JOB)
    match = calculate_match(candidate, job)
    good = compute_apply_score(assess_fit(job, match, CandidatePreferences(target_locations=["Paris"])), match, ["Strong"] * 3, "ok")
    assert good.label == "Apply" and good.score >= 75
    assert [component.weight for component in good.components] == [45, 25, 20, 10]
    excluded = assess_fit(job, match, CandidatePreferences(excluded_companies=["Example Group"]))
    capped = compute_apply_score(excluded, match, ["Strong"] * 3, "ok")
    assert capped.label == "Skip" and capped.score <= 25 and capped.capped_reason


def test_agent_saves_pdf_score_and_analysis(tmp_path) -> None:
    tracker = ApplicationTracker(tmp_path / "db.sqlite")
    report = ApplicationAgent(tracker, export_dir=tmp_path / "packs",
                              preferences=CandidatePreferences(target_locations=["Paris"])).run(CV, [JobInput(text=JOB)])
    result = report.results[0]
    assert result.outcome == "drafted" and result.pdf_path and result.pdf_path.endswith("-cv.pdf")
    assert len(PdfReader(result.pdf_path).pages) == 1
    row = tracker.get(result.application_id)
    assert row.apply_score == result.apply_score and '"resume_changes"' in row.analysis_json
    assert f"{result.apply_score} ({result.apply_label})" in render_report(report)


def test_old_databases_gain_the_new_columns(tmp_path) -> None:
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE applications (id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT NOT NULL UNIQUE, "
                           "company TEXT NOT NULL, job_title TEXT NOT NULL, job_url TEXT, status TEXT NOT NULL, verdict TEXT, "
                           "match_score INTEGER, claim_status TEXT, pack_markdown TEXT, follow_up_at TEXT, "
                           "notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    tracker = ApplicationTracker(path)
    row = tracker.record_assessment(company="A", job_title="B", job_url=None, verdict="Stretch", match_score=10)
    assert tracker.save_analysis(row.id, analysis_json="{}", apply_score=12).apply_score == 12


def test_apply_score_does_not_count_unscoreable_postings() -> None:
    job = parse_job_text("Title: Graduate Programme\nCompany: Example Group\nLocation: Paris\nStart date: September 2027.\nRequirements: degree in Accounting or Finance. Fluent English.\n")
    match = calculate_match(parse_candidate_text(CV), job)
    score = compute_apply_score(assess_fit(job, match, CandidatePreferences(target_locations=["Paris"])), match,
                              [row.strength for row in build_evidence_inventory(parse_candidate_text(CV), job, match)], "needs_review")
    assert not score.components[0].counted and score.score <= 60 and score.label != "Apply"
    assert "cannot vouch" in score.capped_reason
