"""Applications tab: prepare packs with the application agent, review, approve and track.

Nothing on this page submits an application. "Mark as applied" records that the
user submitted it themselves.
"""

from __future__ import annotations

import os
from pathlib import Path

import streamlit as st

from tau_job_application.application_pack import LLMDrafter, TemplateDrafter
from tau_job_application.fit_check import CandidatePreferences
from tau_job_application.job_pages import fetch_job_url
from tau_job_application.orchestrator import ApplicationAgent, JobInput
from tau_job_application.tracker import ApplicationTracker, TransitionError

JOB_SEPARATOR = "---"
OUTCOMES = ["interviewing", "offer", "rejected", "no_response", "withdrawn"]
STATUS_LABELS = {
    "drafted": "To review", "approved": "Approved", "applied": "Applied", "interviewing": "Interviewing",
    "no_response": "No response", "offer": "Offer", "assessed": "Assessed", "skipped": "Skipped",
    "rejected": "Rejected", "withdrawn": "Withdrawn",
}


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
        cv_text = st.text_area("Your CV", value=default_cv, height=180, key="apps_cv",
                               help="Defaults to the CV saved in your shared profile.")
        job_texts = st.text_area("Job descriptions", height=200, key="apps_jobs",
                                 help="Paste one or more descriptions, separated by a line with only ---")
        job_urls = st.text_area("Or job page URLs (one per line)", height=80, key="apps_urls")
        left, middle, right = st.columns(3)
        locations = left.text_input("Target locations (comma separated)", key="apps_locations")
        excluded = middle.text_input("Companies to skip (comma separated)", key="apps_excluded")
        minimum = right.selectbox("Prepare packs from", ["Stretch", "Potential Fit", "Strong Fit"], key="apps_min")
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
            target_locations=_csv(locations), remote_ok=remote_ok, excluded_companies=_csv(excluded), minimum_verdict=minimum)
        drafter = TemplateDrafter()
        if use_model:
            from tau_job_application.agent import build_completion
            drafter = LLMDrafter(build_completion(), skills_dir)
        agent = ApplicationAgent(tracker, export_dir=export_dir, preferences=preferences, drafter=drafter)
        with st.spinner(f"Preparing {len(jobs)} job(s)…"):
            report = agent.run(cv_text, jobs)
        counts = report.counts()
        st.success("Done: " + ", ".join(f"{count} {outcome}" for outcome, count in counts.items()) + ". Nothing was submitted.")
        st.dataframe([{
            "Role": f"{result.job_title} at {result.company}", "Verdict": result.verdict or "-",
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
    rows = sorted(rows, key=lambda row: priority.get(row.status, 9))
    labels = {row.id: f"#{row.id} · {STATUS_LABELS[row.status]} · {row.job_title} at {row.company} ({row.verdict or '-'})" for row in rows}
    selected = st.selectbox("Application", list(labels), format_func=labels.get, key="apps_selected")
    _application_detail(tracker, selected)


def _application_detail(tracker: ApplicationTracker, application_id: int) -> None:
    row = tracker.get(application_id)
    st.markdown(f"**{row.job_title}** at **{row.company}** · status **{STATUS_LABELS[row.status]}** · "
                f"verdict {row.verdict or '-'} · match {row.match_score if row.match_score is not None else '-'}/100 · "
                f"claim check {row.claim_status or '-'}")
    if row.job_url:
        st.markdown(f"[Open the job posting]({row.job_url})")
    current = _pack_text(row)
    if row.status in {"drafted", "approved"}:
        edited = st.text_area("Application pack (edit it, fill every [GAP …], then approve)", value=current, height=480,
                              key=f"apps_pack_{row.id}_{_draft_version(tracker, row.id)}")
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
    else:
        with st.expander("Application pack", expanded=False):
            st.markdown(current or "No pack drafted.")
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


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]
