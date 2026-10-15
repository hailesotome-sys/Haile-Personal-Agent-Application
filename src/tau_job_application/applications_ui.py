"""Applications tab: prepare packs with the application agent, review, approve and track.

Nothing on this page submits an application. "Mark as applied" records that the
user submitted it themselves.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile

import streamlit as st

from tau_job_application.application_pack import JobAnalysis, LLMDrafter, TemplateDrafter
from tau_job_application.fit_check import CandidatePreferences
from tau_job_application.job_pages import fetch_job_url
from tau_job_application.orchestrator import ApplicationAgent, JobInput
from tau_job_application.parsing import load_document
from tau_job_application.resume_document import ResumeDocument, parse_resume_markdown, pdf_preview_png, render_resume_pdf
from tau_job_application.tracker import ApplicationTracker, TransitionError

JOB_SEPARATOR = "---"
OUTCOMES = ["interviewing", "offer", "rejected", "no_response", "withdrawn"]
STATUS_LABELS = {
    "drafted": "To review", "approved": "Approved", "applied": "Applied", "interviewing": "Interviewing",
    "no_response": "No response", "offer": "Offer", "assessed": "Assessed", "skipped": "Skipped",
    "rejected": "Rejected", "withdrawn": "Withdrawn",
}
FILTER_ICONS = {"pass": "✅ Pass", "fail": "❌ Fail", "unknown": "❔ Unknown"}
STRENGTH_ICONS = {"Strong": "✅ Matched", "Partial": "🟡 In CV, not confirmed", "MISSING": "❌ Missing", "Uncertain": "❔ Unclear in posting"}
CV_SECTION = re.compile(r"^## Tailored CV draft\s*$(.*?)^## Cover letter draft\s*$", re.M | re.S)


def model_configured() -> bool:
    return bool(os.environ.get("MODEL_NAME") and (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY")))


def split_job_texts(text: str) -> list[str]:
    """Pasted descriptions are separated by a line containing only ---."""
    jobs, current = [], []
    for line in text.splitlines():
        if line.strip() == JOB_SEPARATOR:
            jobs.append("\n".join(current))
            current = []
        else:
            current.append(line)
    jobs.append("\n".join(current))
    return [job.strip() for job in jobs if job.strip()]


def show_applications(tracker: ApplicationTracker, export_dir: Path, skills_dir: Path, default_cv: str = "") -> None:
    st.markdown('<div class="section-kicker">Application agent</div>', unsafe_allow_html=True)
    st.subheader("Prepare, review and track applications")
    st.caption("The agent fit-checks each job, drafts a tailored CV and cover letter, and stops for your review. "
               "It never submits anything: you apply yourself, then record it here.")
    _prepare_section(tracker, export_dir, skills_dir, default_cv)
    _follow_ups(tracker)
    _review_section(tracker)


def _prepare_section(tracker: ApplicationTracker, export_dir: Path, skills_dir: Path, default_cv: str) -> None:
    with st.expander("Prepare new applications", expanded=not tracker.list()):
        uploaded = st.file_uploader("Upload your CV (PDF, DOCX, TXT)", type=["pdf", "docx", "txt", "md"], key="apps_cv_file")
        if uploaded is not None:
            try:
                default_cv = _uploaded_text(uploaded)
            except ValueError as exc:
                st.error(str(exc))
        cv_text = st.text_area("Your CV", value=default_cv, height=180, key=f"apps_cv_{uploaded.file_id if uploaded else 'typed'}",
                               help="Defaults to the uploaded file, or to the CV saved in your shared profile.")
        vouched = st.text_input("Skills you can back up that your CV doesn't list in a skills line (comma separated)",
                                key="apps_confirmed",
                                help="For example Management control, Financial reporting. Only these and your CV's own skills lines earn match credit.")
        job_texts = st.text_area("Job descriptions", height=200, key="apps_jobs",
                                 help="Paste one or more descriptions, separated by a line with only ---")
        job_urls = st.text_area("Or job page URLs (one per line)", height=80, key="apps_urls")
        left, middle, right = st.columns(3)
        locations = left.text_input("Target locations (comma separated)", key="apps_locations")
        excluded = middle.text_input("Companies to skip (comma separated)", key="apps_excluded")
        minimum = right.selectbox("Prepare packs from", ["Stretch", "Potential Fit", "Strong Fit"], key="apps_min")
        available = left.date_input("Available from", value=None, key="apps_available",
                                    help="Roles that start earlier are skipped.")
        remote_ok = st.checkbox("Accept remote roles outside my locations", value=True, key="apps_remote")
        can_use_model = model_configured()
        use_model = st.checkbox("Draft with a model (follows Agent_skills)", value=can_use_model,
                                    disabled=not can_use_model, key="apps_model",
                                    help=None if can_use_model else "Set MODEL_NAME and ANTHROPIC_API_KEY or OPENAI_API_KEY to enable.")
        if not st.button("Run the agent", type="primary", key="apps_run"):
            return
        if not cv_text.strip():
            st.error("Add your CV first.")
            return
        jobs: list[JobInput] = [JobInput(text=text) for text in split_job_texts(job_texts)]
        for url in (line.strip() for line in job_urls.splitlines()):
            if not url:
                continue
            try:
                posting = fetch_job_url(url)
            except (OSError, ValueError) as exc:
                st.warning(f"Could not read {url}: {exc}")
                continue
            jobs.append(JobInput(text=posting.description or "", title=posting.title, company=posting.company, url=url))
        if not jobs:
            st.error("Add at least one job description or URL.")
            return
        preferences = CandidatePreferences(
            target_locations=_csv(locations), remote_ok=remote_ok, excluded_companies=_csv(excluded), minimum_verdict=minimum,
            available_from=available)
        drafter = TemplateDrafter()
        if use_model:
            from tau_job_application.agent import build_completion
            drafter = LLMDrafter(build_completion(), skills_dir)
        agent = ApplicationAgent(tracker, export_dir=export_dir, preferences=preferences, drafter=drafter)
        with st.spinner(f"Preparing {len(jobs)} job(s)…"):
            report = agent.run(cv_text, jobs, confirmed_skills=_csv(vouched))
        counts = report.counts()
        st.success("Done: " + ", ".join(f"{count} {outcome}" for outcome, count in counts.items()) + ". Nothing was submitted.")
        st.dataframe([{
            "Role": f"{result.job_title} at {result.company}",
            "Apply score": f"{result.apply_score} · {result.apply_label}" if result.apply_score is not None else "-",
            "Verdict": result.verdict or "-",
            "Match": result.match_score, "Claims": result.claim_status or "-", "Outcome": result.outcome, "Note": result.reason,
        } for result in report.results], hide_index=True, width="stretch")


def _follow_ups(tracker: ApplicationTracker) -> None:
    due = tracker.due_follow_ups()
    if due:
        st.markdown('<div class="next-action"><strong>Follow-ups due</strong><br>' + "<br>".join(
            f"{row.job_title} at {row.company}" for row in due) + "</div>", unsafe_allow_html=True)


def _review_section(tracker: ApplicationTracker) -> None:
    rows = tracker.list()
    if not rows:
        st.info("No applications yet. Paste your CV and some jobs above, then run the agent.")
        return
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    columns = st.columns(4)
    for column, (label, statuses) in zip(columns, (("To review", {"drafted"}), ("Approved", {"approved"}),
                                                   ("Applied", {"applied", "no_response", "interviewing"}), ("Skipped", {"skipped"}))):
        column.metric(label, sum(counts.get(status, 0) for status in statuses))
    priority = {"drafted": 0, "approved": 1, "applied": 2, "no_response": 2, "interviewing": 2, "offer": 3}
    rows = sorted(rows, key=lambda row: (priority.get(row.status, 9), -(row.apply_score or 0)))
    labels = {row.id: f"#{row.id} · {STATUS_LABELS[row.status]} · {row.job_title} at {row.company} · "
                      f"apply {row.apply_score if row.apply_score is not None else '-'}/100" for row in rows}
    selected = st.selectbox("Application", list(labels), format_func=labels.get, key="apps_selected")
    _application_detail(tracker, selected)


def _application_detail(tracker: ApplicationTracker, application_id: int) -> None:
    row = tracker.get(application_id)
    analysis = _analysis(row)
    st.markdown(f"**{row.job_title}** at **{row.company}** · status **{STATUS_LABELS[row.status]}**")
    if row.job_url:
        st.markdown(f"[Open the job posting]({row.job_url})")
    if analysis:
        _apply_score(analysis)
    current = _pack_text(row)
    editable = row.status in {"drafted", "approved"}
    names = (["CV (PDF)"] if current else []) + ["Fit check", "What changed"] + (["Review and approve"] if editable else ["Application pack"])
    tabs = dict(zip(names, st.tabs(names)))
    edited = current
    if editable:
        with tabs["Review and approve"]:
            edited = _review_pack(tracker, row, current)
    elif "Application pack" in tabs:
        with tabs["Application pack"]:
            st.markdown(current or "No pack drafted.")
    if "CV (PDF)" in tabs:
        with tabs["CV (PDF)"]:
            _cv_pdf(row, edited, analysis)
    with tabs["Fit check"]:
        _fit_check(row, analysis)
    with tabs["What changed"]:
        _changes(analysis)
    if row.status in {"applied", "no_response", "interviewing", "offer"}:
        options = [status for status in OUTCOMES if status in _allowed(row.status)]
        outcome = st.selectbox("Record an outcome", options, format_func=STATUS_LABELS.get, key=f"apps_outcome_{row.id}")
        note = st.text_input("Note (optional)", key=f"apps_outcome_note_{row.id}")
        if st.button("Save outcome", key=f"apps_save_outcome_{row.id}"):
            _transition(tracker, row.id, outcome, note or None)
    if row.status == "skipped" and st.button("Re-open", key=f"apps_reopen_{row.id}"):
        _transition(tracker, row.id, "assessed")
    if row.notes:
        st.caption("Notes: " + row.notes.replace("\n", " · "))
    with st.expander("History"):
        for event in tracker.history(row.id):
            st.caption(f"{event['created_at'][:16].replace('T', ' ')} · {event['actor']} · "
                       f"{STATUS_LABELS.get(event['from_status'], 'new')} → {STATUS_LABELS[event['to_status']]}")


def _review_pack(tracker: ApplicationTracker, row, current: str) -> str:
    edited = st.text_area("Application pack (edit it, fill every [GAP …], then approve)", value=current, height=480,
                          key=f"apps_pack_{row.id}_{_draft_version(tracker, row.id)}",
                          help="Edits to the CV section show up in the PDF tab.")
    st.download_button("Download pack (.md)", edited, file_name=Path(row.pack_path or f"application-{row.id}.md").name,
                       mime="text/markdown", key=f"apps_download_{row.id}")
    if row.claim_status == "blocked":
        st.error("The claim check blocked this pack: it states numbers your CV does not contain. Remove them before approving.")
    save, approve, skip = st.columns(3)
    if save.button("Save edits", key=f"apps_save_{row.id}"):
        tracker.save_reviewed_text(row.id, edited)
        st.success("Saved.")
    if row.status == "drafted" and approve.button("Approve", type="primary", key=f"apps_approve_{row.id}"):
        tracker.save_reviewed_text(row.id, edited)
        _transition(tracker, row.id, "approved")
    if skip.button("Skip this role", key=f"apps_skip_{row.id}"):
        _transition(tracker, row.id, "skipped")
    if row.status == "approved":
        st.info("Approved. Apply on the employer's site yourself, then record it below.")
        note = st.text_input("Where or how you applied (optional)", key=f"apps_note_{row.id}")
        if st.button("I applied", type="primary", key=f"apps_applied_{row.id}"):
            _transition(tracker, row.id, "applied", note or None)
    return edited


def _apply_score(analysis: JobAnalysis) -> None:
    score = analysis.apply_score
    colour = {"Apply": "green", "Apply with care": "orange"}.get(score.label, "red")
    st.markdown(f"### Apply score {score.score}/100 :{colour}-badge[{score.label}]")
    st.markdown("\n".join(f"- **{component.name}** ({component.weight}% of the score): {component.score}/100. {component.explanation}"
                          if component.counted else f"- **{component.name}**: {component.explanation}"
                          for component in score.components))
    if score.capped_reason:
        st.info(score.capped_reason)
    st.caption("A decision aid built from the checks below, not a prediction of being hired.")


def _cv_pdf(row, pack_text: str, analysis: JobAnalysis | None, key: str = "apps_pdf") -> None:
    document = _cv_document(pack_text, analysis)
    if document is None:
        st.info("The CV section of this pack could not be laid out as a PDF. Download the pack as text instead.")
        return
    _pdf_preview(document, row.company, f"{key}_{row.id}", "Built from the CV section of the pack, including your edits. One A4 page.")


def _pdf_preview(document: ResumeDocument, company: str, key: str, caption: str) -> None:
    pdf = render_resume_pdf(document)
    st.download_button("Download CV (PDF)", pdf, file_name=f"CV {document.name} - {company}.pdf",
                       mime="application/pdf", type="primary", key=key)
    st.caption(caption)
    st.image(_preview(pdf), width="stretch")


def show_cv_pdfs(tracker: ApplicationTracker, workspace_result=None) -> None:
    """CV studio: the adapted CV as a PDF, for this job workspace or any prepared application."""
    from tau_job_application.resume_document import parse_resume, tailor_resume_document

    options: dict[str, object] = {}
    if workspace_result is not None and workspace_result.candidate.raw_cv:
        options[f"This workspace: {workspace_result.job.title} at {workspace_result.job.company}"] = workspace_result
    for row in tracker.list():
        if row.pack_path or row.pack_markdown:
            options[f"Application #{row.id}: {row.job_title} at {row.company}"] = row
    st.markdown("<div class='section-kicker'>Adapted CV as PDF</div>", unsafe_allow_html=True)
    if not options:
        st.info("Run the agent in the Applications tab, or analyze a job in Job brief, to get an adapted CV as a PDF.")
        return
    choice = st.selectbox("CV adapted for", list(options), key="studio_pdf_choice")
    source = options[choice]
    if hasattr(source, "pack_path"):
        analysis = _analysis(source)
        with st.expander("What changed for this role"):
            _changes(analysis)
        _cv_pdf(source, _pack_text(source), analysis, key="studio_pdf")
        return
    try:
        document = parse_resume(source.candidate.raw_cv)
    except ValueError:
        st.info("Your CV could not be laid out as a PDF.")
        return
    skills = [item.skill for item in source.job.requirements if item.importance != "uncertain"]
    tailored, changes = tailor_resume_document(document, source.job.title, source.job.company, skills)
    with st.expander("What changed for this role"):
        _changes(JobAnalysis.model_construct(resume_changes=changes))
    _pdf_preview(tailored, source.job.company, "studio_pdf_workspace", "Your CV's own wording, reordered for this role. One A4 page.")


def _cv_document(pack_text: str, analysis: JobAnalysis | None) -> ResumeDocument | None:
    section = CV_SECTION.search(pack_text or "")
    if section:
        try:
            return parse_resume_markdown(section[1])
        except ValueError:
            pass
    return analysis.resume_document if analysis else None


@st.cache_data(show_spinner=False, max_entries=20)
def _preview(pdf: bytes) -> bytes:
    return pdf_preview_png(pdf)


def _fit_check(row, analysis: JobAnalysis | None) -> None:
    if analysis is None:
        st.info(f"Verdict {row.verdict or '-'}, match {row.match_score if row.match_score is not None else '-'}/100. "
                "Run the agent again on this job for the detailed check.")
        return
    fit = analysis.fit
    st.markdown(f"**{fit.verdict}.** {fit.summary}")
    st.markdown(f"**Recommendation:** {fit.recommendation}")
    st.markdown("**Hard filters**")
    st.dataframe([{"Filter": item.name, "Result": FILTER_ICONS[item.result], "Detail": item.detail}
                  for item in fit.hard_filters], hide_index=True, width="stretch")
    st.markdown("**Requirements, one by one**")
    st.dataframe([{"Employer need": item.employer_need, "Status": STRENGTH_ICONS.get(item.strength, item.strength),
                   "Your evidence": item.candidate_evidence, "What the job says": item.job_quote or "-",
                   "Importance": item.importance}
                  for item in analysis.inventory], hide_index=True, width="stretch")
    if any(item.strength == "Partial" for item in analysis.inventory):
        st.caption("🟡 means the word appears in your CV but you have not confirmed it. Add it to "
                   "\"Skills you can back up\" and re-run if you can evidence it in an interview.")
    if analysis.claim_check and analysis.claim_check.issues:
        st.markdown(f"**Claim check: {analysis.claim_check.status}**")
        st.dataframe([{"Where": issue.document.replace("_", " "), "Issue": issue.kind.replace("_", " "), "Text": issue.text,
                       "Detail": issue.detail} for issue in analysis.claim_check.issues], hide_index=True, width="stretch")


def _changes(analysis: JobAnalysis | None) -> None:
    if analysis is None or not analysis.resume_changes:
        st.info("No CV changes recorded for this role.")
        return
    icons = {"Added": "➕", "Moved up": "⬆️", "Kept": "✔️"}
    for change in analysis.resume_changes:
        st.markdown(f"{icons.get(change.kind, '•')} **{change.kind}** · {change.where}: {change.detail}")


def _analysis(row) -> JobAnalysis | None:
    if not row.analysis_json:
        return None
    try:
        return JobAnalysis.model_validate_json(row.analysis_json)
    except ValueError:
        return None


def _transition(tracker: ApplicationTracker, application_id: int, status: str, note: str | None = None) -> None:
    try:
        tracker.transition(application_id, status, actor="user", note=note)
    except TransitionError as exc:
        st.error(str(exc))
        return
    st.rerun()


def _draft_version(tracker: ApplicationTracker, application_id: int) -> int:
    """Changes only when the agent re-drafts, so the editor keeps the user's unsaved edits otherwise."""
    return sum(1 for event in tracker.history(application_id) if event["actor"] == "agent" and event["to_status"] == "drafted")


def _allowed(status: str) -> set[str]:
    from tau_job_application.tracker import TRANSITIONS
    return TRANSITIONS[status]


def _pack_text(row) -> str:
    if row.pack_path and Path(row.pack_path).exists():
        return Path(row.pack_path).read_text(encoding="utf-8")
    return row.pack_markdown or ""


def _uploaded_text(uploaded) -> str:
    with tempfile.NamedTemporaryFile(suffix=Path(uploaded.name).suffix, delete=False) as temporary:
        temporary.write(uploaded.getvalue())
        path = Path(temporary.name)
    try:
        return load_document(path)
    finally:
        path.unlink(missing_ok=True)


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]
