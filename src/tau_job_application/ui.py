"""Minimalist local Streamlit UI for the evidence-first job-readiness workflow."""

from __future__ import annotations

import os
import hashlib
from html import escape
from pathlib import Path
import tempfile

import pydeck as pdk
import streamlit as st

from tau_job_application.career import contact_research_plan, parse_contact_leads
from tau_job_application.company_explorer import COMPANY_PINS, company_footprint, detected_profile_skills, recommend_companies
from tau_job_application.interview import build_interview_questions, evaluate_answer, transcribe_openai_audio
from tau_job_application.job_pages import fetch_job_url
from tau_job_application.official_sources import (
    fetch_linkedin_authorized_identity,
    fetch_smartrecruiters_postings,
    fetch_x_recent_posts,
    import_contact_export,
    import_job_export,
)
from tau_job_application.monitoring import JobMonitor
from tau_job_application.parsing import KNOWN_SKILLS, infer_profile_links, load_document
from tau_job_application.requirement_memory import RequirementMemory, document_key
from tau_job_application.requirements import extract_requirements, save_requirement_review
from tau_job_application.pipeline import analyze_texts, render_markdown
from tau_job_application.sources import enrich_github_profile, fetch_greenhouse_jobs, fetch_lever_jobs, fetch_recruitee_jobs
from tau_job_application.storage import LocalStore
from tau_job_application.workspaces import PROFILE_FIELDS, WorkspaceStore
from tau_job_application.workspace_ui import choose_scope, draft_key, show_chat, show_direction
from tau_job_application.applications_ui import show_applications
from tau_job_application.tracker import ApplicationTracker


def _project_root() -> Path:
    for directory in (Path.cwd(), *Path.cwd().parents):
        if (directory / "pyproject.toml").is_file():
            return directory
    return Path.home() / ".job-readiness-agent"


ROOT = _project_root()
STORE = LocalStore(ROOT / ".job_assistant" / "assistant.sqlite")
MONITOR = JobMonitor(ROOT / ".job_assistant" / "assistant.sqlite")
REQUIREMENT_MEMORY = RequirementMemory(ROOT / ".job_assistant" / "assistant.sqlite")
WORKSPACES = WorkspaceStore(ROOT / ".job_assistant" / "assistant.sqlite")
TRACKER = ApplicationTracker(ROOT / ".job_assistant" / "assistant.sqlite")


STYLES = """
<style>
:root {
    --ink: #19352e;
    --forest: #20483c;
    --sage: #6f8875;
    --paper: #fbfaf5;
    --mist: #eef2ea;
    --line: #dce4d9;
    --clay: #d9865c;
}
.stApp, [data-testid="stAppViewContainer"] {background: linear-gradient(135deg, #fcfbf6 0%, #f2f5ed 56%, #f8f8f1 100%); color: var(--ink); color-scheme: light;}
.block-container {padding-top: 1.35rem; padding-bottom: 3rem; max-width: 1440px;}
div[data-testid="stSidebar"] {background: #173a31;}
div[data-testid="stSidebar"] * {color: #f5f5ed;}
div[data-testid="stSidebar"] [data-testid="stCaptionContainer"] p {color: #c9d7c9;}
div[data-testid="stSidebar"] [data-testid="stProgress"] > div > div > div > div {background-color: #d6bd70;}
div[data-testid="stTextInput"] label, div[data-testid="stTextArea"] label, div[data-testid="stFileUploader"] label, div[data-testid="stCheckbox"] label, div[data-testid="stSelectbox"] label {color: #28463b !important; font-weight: 620;}
div[data-baseweb="input"] > div, div[data-baseweb="textarea"] {background-color: #fffef9 !important; border-color: #c9d7c9 !important; color: #19352e !important;}
div[data-baseweb="input"] input, div[data-baseweb="textarea"] textarea {background-color: transparent !important; color: #19352e !important; caret-color: #19352e !important;}
div[data-testid="stTextInput"] input, div[data-testid="stTextArea"] textarea {background-color: #fffef9 !important; color: #19352e !important; caret-color: #19352e !important; border: 1px solid #c9d7c9 !important;}
div[data-testid="stTextInput"] input::placeholder, div[data-testid="stTextArea"] textarea::placeholder {color: #718278 !important; opacity: 1 !important;}
div[data-testid="stFileUploader"] section {background: #fffef9 !important; border-color: #c9d7c9 !important; color: #19352e !important;}
div[data-testid="stFileUploader"] small, div[data-testid="stFileUploader"] span {color: #4e655a !important;}
button[data-testid="stBaseButton-primary"] {background: #1f5041 !important; border-color: #1f5041 !important; color: #fffef8 !important;}
button[data-testid="stBaseButton-primary"]:hover {background: #173a31 !important; border-color: #173a31 !important;}
[data-testid="stMetricLabel"], [data-testid="stMetricLabel"] *, [data-testid="stMetricValue"], [data-testid="stMetricValue"] * {color: #19352e !important;}
.workspace-hero {background: radial-gradient(circle at 86% 18%, rgba(218, 194, 119, .36), transparent 24%), linear-gradient(120deg, #173a31 0%, #285445 66%, #47735e 100%); border-radius: 18px; color: #fbfbf3; padding: 2rem 2.15rem 1.7rem; box-shadow: 0 16px 35px rgba(25, 53, 46, .14); margin-bottom: 1.3rem;}
.workspace-hero .eyebrow {font-size: .74rem; text-transform: uppercase; letter-spacing: .13em; font-weight: 700; color: #d8e5d5; margin-bottom: .55rem;}
.workspace-hero h1 {color: #fffef8; font-size: clamp(1.65rem, 3.1vw, 2.8rem); line-height: 1.1; margin: 0 0 .45rem;}
.workspace-hero p {color: #deeadf; max-width: 760px; margin: 0; font-size: 1rem;}
.workspace-hero .job-chip {display: inline-block; margin-top: 1rem; border: 1px solid rgba(255,255,255,.4); border-radius: 999px; padding: .35rem .72rem; color: #fbfbf4; font-size: .82rem;}
.metric-card {background: rgba(255, 255, 252, .86); border-radius: 14px; padding: 17px 14px; min-height: 110px; border: 1px solid var(--line); box-shadow: 0 5px 15px rgba(25, 53, 46, .045);}
.metric-label {font-size: .75rem; letter-spacing: .055em; text-transform: uppercase; color: #61726a; margin-bottom: 6px; font-weight: 700;}
.metric-value {font-size: 1.8rem; font-weight: 760; color: var(--ink); line-height: 1.05;}
.metric-note {font-size: .78rem; color: #66776e; margin-top: .35rem;}
.section-kicker {font-size: .76rem; letter-spacing: .11em; text-transform: uppercase; color: #5b7767; font-weight: 750; margin-bottom: .25rem;}
.quiet-panel {background: rgba(255,255,252,.72); border: 1px solid var(--line); border-radius: 14px; padding: 1.1rem 1.2rem; margin: .45rem 0 1rem;}
.next-action {background: #fff9eb; border-left: 4px solid #d98a58; border-radius: 5px 12px 12px 5px; padding: 1rem 1.1rem;}
.path-card {background: rgba(255,255,252,.8); border: 1px solid var(--line); border-radius: 12px; padding: .9rem 1rem; min-height: 105px;}
.path-step {font-size: .72rem; text-transform: uppercase; letter-spacing: .09em; color: #67806e; font-weight: 800;}
.path-card h4 {margin: .3rem 0; color: var(--ink);}
.path-card p {font-size: .86rem; color: #5c6e65; margin: 0;}
div[data-testid="stTabs"] [role="tab"], div[data-testid="stTabs"] [role="tab"] * {font-weight: 650 !important; color: #52665b !important; opacity: 1 !important;}
div[data-testid="stTabs"] [role="tab"][aria-selected="true"], div[data-testid="stTabs"] [role="tab"][aria-selected="true"] * {color: #173a31 !important;}
div[data-testid="stExpander"] {background: rgba(255,255,252,.66); border: 1px solid var(--line); border-radius: 10px;}
</style>
"""


def main() -> None:
    st.set_page_config(page_title="Career Workspace", page_icon="◒", layout="wide")
    st.markdown(STYLES, unsafe_allow_html=True)
    workspace_id, thread_id = choose_scope(WORKSPACES)
    _workspace_hero(WORKSPACES.thread(workspace_id, thread_id))
    overview, applications, brief, cv_studio, projects, interview, direction, opportunities, conversation = st.tabs([
        "Overview", "Applications", "Job brief", "CV studio", "Proof projects", "Interview lab", "Career track", "Explore", "Coach"])
    with overview:
        _job_workspace_dashboard(workspace_id, thread_id)
    with brief:
        _quickstart(workspace_id, thread_id)
    with cv_studio:
        _cv_studio(workspace_id, thread_id)
    with projects:
        _project_studio(workspace_id, thread_id)
    with interview:
        _interview(workspace_id, thread_id)
    with direction:
        show_direction(WORKSPACES, workspace_id)
    with opportunities:
        _opportunities(workspace_id, thread_id)
    with conversation:
        show_chat(WORKSPACES, workspace_id, thread_id)
    with applications:
        show_applications(TRACKER, ROOT / ".job_assistant" / "packs", ROOT / "Agent_skills",
                          WORKSPACES.shared_profile().get("cv_text", ""))


def _workspace_hero(thread: dict) -> None:
    """Make the selected target role visibly feel like its own Job Workspace."""
    draft = thread["draft"]
    title = draft["job_title"].strip() or thread["name"]
    company = draft["company"].strip()
    job_label = f"{title} · {company}" if company else title
    is_ready = bool(thread["analysis"]) and not thread["stale"]
    state = "Preparation plan ready" if is_ready else "Set up this job workspace"
    st.markdown(
        f"""<section class=\"workspace-hero\">
        <div class=\"eyebrow\">Job workspace · {escape(state)}</div>
        <h1>{escape(job_label)}</h1>
        <p>Turn this particular role into a focused preparation space: find the proof you already have, sharpen your CV guidance, build the missing evidence, and rehearse the conversation.</p>
        <span class=\"job-chip\">{escape(thread['name'])}</span>
        </section>""",
        unsafe_allow_html=True,
    )


def _job_workspace_dashboard(workspace_id: str, thread_id: str) -> None:
    """The role-specific landing view; all downstream work belongs to this thread."""
    thread = WORKSPACES.thread(workspace_id, thread_id)
    draft = thread["draft"]
    result = thread["analysis"] if not thread["stale"] else None
    attempts = WORKSPACES.interview_attempts(workspace_id, thread_id)

    if thread["stale"]:
        st.warning("This workspace's job brief or profile changed after its last analysis. Refresh it in Job brief before relying on the plan.")
    if not result:
        _empty_workspace_dashboard(draft)
        return

    matched = [item.skill for item in result.match.requirements if item.status == "matched"]
    missing = [item.skill for item in result.match.requirements if item.status == "missing"]
    practice_average = round(sum(item["score"] for item in attempts) / len(attempts)) if attempts else None
    coverage_note = f"{len(matched)} shown · {len(missing)} to build"
    cards = st.columns(4)
    _metric_card(cards[0], "Requirement evidence coverage", f"{result.match.score}%", coverage_note)
    _metric_card(cards[1], "CV guidance", f"{result.cv_score.score if result.cv_score else '—'}%", "Document readiness, not a hiring prediction")
    _metric_card(cards[2], "Interview practice", f"{practice_average if practice_average is not None else '—'}", f"{len(attempts)} saved rehearsal{'s' if len(attempts) != 1 else ''}")
    _metric_card(cards[3], "Proof gaps", str(len(missing)), "Role requirements without CV evidence")

    st.markdown("<div class='section-kicker'>Your next best move</div>", unsafe_allow_html=True)
    next_action, destination, reason = _next_action(result, attempts)
    st.markdown(
        f"<div class='next-action'><strong>{escape(next_action)}</strong><br><span>{escape(reason)} Open <strong>{escape(destination)}</strong> to work on it.</span></div>",
        unsafe_allow_html=True,
    )

    st.markdown("<div class='section-kicker' style='margin-top:1.4rem'>A focused route to this role</div>", unsafe_allow_html=True)
    route = st.columns(4)
    _path_card(route[0], "01 · Map", "Job evidence", f"{len(result.job.requirements)} requirements reviewed from this role.")
    _path_card(route[1], "02 · Strengthen", "CV guidance", "Truthful edits tied to the experience you already show.")
    _path_card(route[2], "03 · Prove", "Standout project", _project_summary(result))
    _path_card(route[3], "04 · Rehearse", "Interview stories", _practice_summary(attempts))

    left, right = st.columns([1.13, 0.87], gap="large")
    with left:
        st.markdown("<div class='section-kicker'>Evidence already visible</div>", unsafe_allow_html=True)
        with st.container(border=True):
            if matched:
                st.write(" · ".join(matched[:8]))
                if len(matched) > 8:
                    st.caption(f"Plus {len(matched) - 8} more requirement matches in this job brief.")
            else:
                st.write("No confirmed requirement evidence yet. Add your CV or review the job brief before making claims.")
        st.markdown("<div class='section-kicker' style='margin-top:1rem'>Proof to build</div>", unsafe_allow_html=True)
        with st.container(border=True):
            if missing:
                st.write(" · ".join(missing[:8]))
                st.caption("These are gaps in the available evidence—not a judgment of your ability or a prediction of hiring.")
            else:
                st.write("No missing scored requirement evidence was found in the current job brief.")
    with right:
        st.markdown("<div class='section-kicker'>This week's preparation plan</div>", unsafe_allow_html=True)
        with st.container(border=True):
            for index, action in enumerate(_weekly_actions(result, attempts), 1):
                st.markdown(f"**{index}.** {action}")
            st.caption("These are recommendations for this selected role; they do not submit applications or invent experience.")


def _empty_workspace_dashboard(draft: dict[str, str]) -> None:
    has_cv = bool(draft["cv_text"].strip())
    has_job = bool(draft["job_text"].strip())
    has_github = bool(draft["github"].strip())
    has_portfolio = bool(draft["portfolio"].strip())
    st.markdown("<div class='section-kicker'>Make this a real preparation room</div>", unsafe_allow_html=True)
    st.markdown("#### Every target role gets its own evidence, plan, projects, and rehearsal history.")
    route = st.columns(3)
    profile_note = "CV added" if has_cv else "Upload or paste the CV you want this role assessed against."
    if has_cv and not (has_github or has_portfolio):
        profile_note += " Add GitHub and/or a portfolio to make your profile easier to explore."
    _path_card(route[0], "01", "Add your profile", profile_note)
    _path_card(route[1], "02", "Add the job brief", "Done" if has_job else "Paste the role description or read it from the official job URL.")
    _path_card(route[2], "03", "Analyze this job", "Unlocks CV guidance, proof projects, and interview questions.")
    if not has_cv:
        st.info("Start in Job brief with your CV. We will also ask for your GitHub profile and/or portfolio if you have them; those links are optional and never verified automatically.")
    elif not (has_github or has_portfolio):
        st.info("Your CV is ready. Add your GitHub profile and/or portfolio in Job brief to make your profile more useful to review and explore.")
    if has_cv and has_job:
        st.success("This workspace is ready. Open Job brief and choose Analyze this job.")
    else:
        st.info("Start in Job brief. Nothing is sent anywhere when you add your CV or a job description.")


def _metric_card(column, label: str, value: str, note: str) -> None:
    column.markdown(
        f"<div class='metric-card'><div class='metric-label'>{escape(label)}</div><div class='metric-value'>{escape(value)}</div><div class='metric-note'>{escape(note)}</div></div>",
        unsafe_allow_html=True,
    )


def _path_card(column, step: str, title: str, detail: str) -> None:
    column.markdown(
        f"<div class='path-card'><div class='path-step'>{escape(step)}</div><h4>{escape(title)}</h4><p>{escape(detail)}</p></div>",
        unsafe_allow_html=True,
    )


def _next_action(result, attempts: list[dict]) -> tuple[str, str, str]:
    missing = [item.skill for item in result.match.requirements if item.status == "missing"]
    if result.authenticity_report and result.authenticity_report.status == "blocked":
        return ("Resolve unsupported CV claims first.", "CV studio", "The workspace cannot safely turn unsupported claims into application language.")
    if missing and result.project_plans:
        return (f"Scope {result.project_plans[0].title}.", "Proof projects", f"It is the clearest next chance to build evidence for {', '.join(missing[:2])}.")
    if not attempts:
        return ("Complete your first role-specific rehearsal.", "Interview lab", "The question bank is grounded in this job's current requirements.")
    if result.cv_score and result.cv_score.next_actions:
        return (result.cv_score.next_actions[0], "CV studio", "Review the evidence-aware guidance before changing your CV.")
    return ("Review your evidence and choose one thing to strengthen.", "CV studio", "Keep every claim grounded in work you can explain and support.")


def _project_summary(result) -> str:
    if not result.project_plans:
        return "No new project is suggested from the current evidence gaps."
    plan = result.project_plans[0]
    return f"{plan.title} · about {plan.estimated_hours} hours."


def _practice_summary(attempts: list[dict]) -> str:
    if not attempts:
        return "Start with one saved answer for this exact role."
    categories = sorted({attempt["category"] for attempt in attempts})
    return f"{len(attempts)} saved rehearsal{'s' if len(attempts) != 1 else ''} across {', '.join(categories)}."


def _weekly_actions(result, attempts: list[dict]) -> list[str]:
    actions: list[str] = []
    if result.cv_score:
        actions.extend(result.cv_score.next_actions[:1])
    if result.project_plans:
        plan = result.project_plans[0]
        actions.append(f"Choose the first milestone for “{plan.title}” and define its proof of completion.")
    if not attempts:
        actions.append("Practice one job-specific interview question and save the feedback in Interview lab.")
    else:
        actions.append("Rehearse a different question category than your last saved attempt.")
    return actions[:3]


def _quickstart(workspace_id: str, thread_id: str) -> None:
    thread = WORKSPACES.thread(workspace_id, thread_id)
    draft = thread["draft"]
    if loaded_job := st.session_state.pop(f"loaded-job:{thread_id}", None):
        draft.update(loaded_job)
        for field, value in loaded_job.items():
            st.session_state[draft_key(thread_id, field)] = value
        WORKSPACES.save_draft(workspace_id, thread_id, draft)
    if st.session_state.pop(f"refresh-profile:{thread_id}", False):
        draft.update(WORKSPACES.shared_profile())
        for field in PROFILE_FIELDS:
            st.session_state[draft_key(thread_id, field)] = draft[field]
        WORKSPACES.save_draft(workspace_id, thread_id, draft)
    st.caption(f"Job workspace: {thread['name']}. Text inputs auto-save locally. This role keeps its own profile snapshot, analysis, interview practice, and coaching history.")
    upload_error = False
    with st.container(border=True):
        st.subheader("1. Your profile — CV, GitHub, and portfolio")
        st.caption("Your CV is needed for role-specific guidance. Add your GitHub profile and/or portfolio when available; they are optional, remain local, and are never assumed to prove skills or ownership.")
        left, right = st.columns([1, 1])
        with left:
            uploaded = st.file_uploader("Drop your CV here", type=["pdf", "docx", "txt", "md"], help="PDF must have selectable text. Extracted text is editable below.", key=f"cv-upload:{thread_id}")
            if uploaded is not None:
                digest = hashlib.sha256(uploaded.getvalue()).hexdigest()
                if st.session_state.get(f"cv-digest:{thread_id}") != digest:
                    try:
                        st.session_state[draft_key(thread_id, "cv_text")] = _upload_text(uploaded)
                        st.session_state[f"cv-digest:{thread_id}"] = digest
                    except Exception as exc:
                        st.error(str(exc))
                        upload_error = True
            cv_text = st.text_area("Or paste your CV text", value=draft["cv_text"], key=draft_key(thread_id, "cv_text"), height=220, placeholder="Name: Alex\nSkills: Python, SQL\nExperience: Built a data API...")
        inferred_links = infer_profile_links(cv_text)
        if inferred_links:
            st.success("Detected from CV: " + ", ".join(f"{key} ({value})" for key, value in inferred_links.items()) + ". Please verify or edit them.")
            for field, value in inferred_links.items():
                if not draft[field]:
                    st.session_state.setdefault(draft_key(thread_id, field), value)
                    draft[field] = value
        with right:
            github = st.text_input("GitHub profile (recommended when available)", value=draft["github"], key=draft_key(thread_id, "github"), placeholder="https://github.com/yourname")
            portfolio = st.text_input("Portfolio / personal website (recommended when available)", value=draft["portfolio"], key=draft_key(thread_id, "portfolio"), placeholder="https://yourname.dev")
            linkedin = st.text_input("LinkedIn", value=draft["linkedin"], key=draft_key(thread_id, "linkedin"), placeholder="https://linkedin.com/in/yourname")
            x_url = st.text_input("X / Twitter", value=draft["x"], key=draft_key(thread_id, "x"), placeholder="https://x.com/yourname")
            enrich = st.checkbox("Inspect public GitHub languages after analysis (unconfirmed)", key=f"enrich:{thread_id}")
        profile = {"cv_text": cv_text, "github": github, "portfolio": portfolio, "linkedin": linkedin, "x": x_url}
        if st.button("Save as shared profile for new Job Workspaces", disabled=upload_error or not cv_text.strip(), key=f"save-profile:{thread_id}"):
            WORKSPACES.save_shared_profile(profile)
            st.success("New Job Workspaces will start with this CV and links. Existing workspaces and old analyses are unchanged.")
        if st.button("Use latest shared profile in this Job Workspace", key=f"load-profile:{thread_id}"):
            st.session_state[f"refresh-profile:{thread_id}"] = True
            st.rerun()

    with st.container(border=True):
        st.subheader("2. This target role")
        job_title = st.text_input("Job title", value=draft["job_title"], key=draft_key(thread_id, "job_title"), placeholder="Backend Engineer")
        company = st.text_input("Company", value=draft["company"], key=draft_key(thread_id, "company"), placeholder="Acme")
        job_url = st.text_input("Job description URL", value=draft["job_url"], key=draft_key(thread_id, "job_url"), placeholder="https://careers.example.com/job")
        if st.button("Read job URL", key=f"read-job:{thread_id}", disabled=not job_url.strip()):
            try:
                page_job = fetch_job_url(job_url)
                loaded = {"job_title": page_job.title, "company": page_job.company,
                          "job_url": page_job.url or job_url, "job_text": page_job.description or ""}
                st.session_state[f"loaded-job:{thread_id}"] = loaded
                st.success("Job title, company, description, and source URL inferred. Review the extracted text, then analyze this job.")
                st.rerun()
            except (ValueError, RuntimeError) as exc:
                st.error(str(exc))
        job_text = st.text_area("Job description", value=draft["job_text"], key=draft_key(thread_id, "job_text"), height=200, placeholder="Or paste the description: you are experienced with Python and comfortable building APIs…")
        draft = {**profile, "job_title": job_title, "company": company, "job_url": job_url, "job_text": job_text}
        WORKSPACES.save_draft(workspace_id, thread_id, draft)
        st.caption("Requirements are inferred from wording and context. Optional review appears below; ambiguous mentions are not scored.")
        if job_text.strip():
            _requirement_editor(job_text)

    existing = WORKSPACES.thread(workspace_id, thread_id)
    analysis_label = "Refresh this job workspace" if existing["analysis"] else "Analyze this job"
    if st.button(analysis_label, type="primary", use_container_width=True, disabled=upload_error, key=f"build:{thread_id}"):
        try:
            candidate_text = cv_text
            if not candidate_text.strip() or not job_text.strip():
                raise ValueError("Provide both a CV and a job description.")
            links = {"github": github or None, "portfolio": portfolio or None, "linkedin": linkedin or None, "x": x_url or None}
            result = analyze_texts(
                candidate_text, job_text,
                links=links, job_title=job_title or None, company=company or None, job_url=job_url or None,
                requirement_memory=REQUIREMENT_MEMORY,
            )
            if enrich and github:
                candidate = enrich_github_profile(result.candidate)
                result = result.model_copy(update={"candidate": candidate})
            WORKSPACES.save_analysis(workspace_id, thread_id, result)
            st.session_state["analysis"] = result
        except Exception as exc:
            st.error(str(exc))

    current = WORKSPACES.thread(workspace_id, thread_id)
    if current["stale"]:
        st.warning("Saved inputs or reviewed requirements changed. Rebuild this plan; the older snapshot is retained but excluded from comparisons and coaching.")
    result = current["analysis"] if not current["stale"] else None
    st.session_state["analysis"] = result
    if not result:
        return

    st.success("This job workspace is ready. Use CV studio, Proof projects, and Interview lab to act on the plan for this exact role.")
    with st.expander("Review the role evidence used in this plan"):
        evidence = {item.id: item for item in result.job.evidence}
        for requirement in result.job.requirements:
            source = evidence.get(requirement.evidence_id)
            label = "required" if requirement.required else "preferred"
            st.markdown(f"**{requirement.skill}** · {label}")
            if source:
                st.caption(f"“{source.quote}”")

    if result.authenticity_report and result.authenticity_report.status == "blocked":
        st.error("Download is disabled while the CV contains unsupported claims. Remove or evidence them first.")
    else:
        report = render_markdown(result)
        st.download_button("Download this job workspace plan (.md)", report, file_name="job-workspace-plan.md", mime="text/markdown")


def _cv_studio(workspace_id: str, thread_id: str) -> None:
    """Evidence-aware CV guidance, kept separate for every target role."""
    thread = WORKSPACES.thread(workspace_id, thread_id)
    result = thread["analysis"] if not thread["stale"] else None
    st.subheader("CV studio")
    st.caption("Refine your narrative for this role without inventing employers, skills, dates, or outcomes. This workspace currently provides guidance—not a finished CV editor.")
    if thread["stale"]:
        st.warning("Refresh the Job brief first. CV guidance is held back while its underlying evidence is stale.")
        return
    if not result:
        st.info("Analyze this job in Job brief to receive role-specific CV guidance.")
        return

    left, right = st.columns([0.93, 1.07], gap="large")
    with left:
        st.markdown("<div class='section-kicker'>Document readiness</div>", unsafe_allow_html=True)
        if result.cv_score:
            _metric_card(st, "CV guidance", f"{result.cv_score.score}%", "Clarity and evidence readiness—not a hiring prediction")
            for component in result.cv_score.components:
                st.progress(component.score / 100, text=f"{component.name}: {component.score}/100")
            if result.cv_score.next_actions:
                st.markdown("**Prioritized improvements**")
                for action in result.cv_score.next_actions:
                    st.markdown(f"- {action}")
        matched = [item.skill for item in result.match.requirements if item.status == "matched"]
        with st.container(border=True):
            st.markdown("**Requirement evidence you can emphasize**")
            st.write(" · ".join(matched) if matched else "No confirmed requirement evidence in the current CV yet.")
            st.caption("Only emphasize claims you can explain, substantiate, and stand behind in an interview.")
    with right:
        st.markdown("<div class='section-kicker'>Role-specific safe edits</div>", unsafe_allow_html=True)
        if result.tailored_resume:
            st.markdown(f"#### {result.tailored_resume.target_title}")
            st.write(result.tailored_resume.summary)
            st.caption(result.tailored_resume.safety_note)
            candidate_evidence = {item.id: item for item in result.candidate.evidence}
            for index, suggestion in enumerate(result.tailored_resume.suggestions, 1):
                with st.expander(f"{index}. {suggestion.section}", expanded=index == 1):
                    st.write(suggestion.recommendation)
                    st.markdown("**Copy-ready draft to review**")
                    st.code(suggestion.safe_draft, language="markdown")
                    sources = [candidate_evidence[source_id] for source_id in suggestion.evidence_ids if source_id in candidate_evidence]
                    if sources:
                        st.markdown("**Grounded in your supplied evidence**")
                        for source in sources:
                            st.caption(f"• {source.quote}")
        else:
            st.info("No tailored CV suggestions are available for this job yet.")

    if result.authenticity_report:
        report = result.authenticity_report
        st.markdown("<div class='section-kicker' style='margin-top:1rem'>Claim safety check</div>", unsafe_allow_html=True)
        status_color = {"ready": "green", "needs_review": "orange", "blocked": "red"}[report.status]
        st.markdown(f"**Status:** :{status_color}[{report.status.replace('_', ' ').title()}]")
        st.write(report.overall_guidance)
        for section in report.rewrite_sections:
            with st.expander(f"Review {section.section}"):
                st.write(section.reason)
                st.caption(section.candidate_action)


def _project_studio(workspace_id: str, thread_id: str) -> None:
    """Turn evidence gaps into an honest, role-specific portfolio build brief."""
    thread = WORKSPACES.thread(workspace_id, thread_id)
    result = thread["analysis"] if not thread["stale"] else None
    st.subheader("Proof projects")
    st.caption("Build a small, finished piece of evidence you can show and discuss. A project is not work experience until you have actually completed it.")
    if thread["stale"]:
        st.warning("Refresh the Job brief first. Project recommendations are held back while the role evidence is stale.")
        return
    if not result:
        st.info("Analyze this job in Job brief to generate projects tied to its evidence gaps.")
        return
    if not result.project_plans:
        st.success("The current job analysis did not identify a project gap that needs a new portfolio piece.")
        return

    missing = {item.skill for item in result.match.requirements if item.status == "missing"}
    requirement_by_skill = {requirement.skill: requirement for requirement in result.job.requirements}
    evidence_by_id = {item.id: item for item in result.job.evidence}
    st.markdown("<div class='quiet-panel'><strong>What makes a project stand out here?</strong><br>It should solve a credible problem, demonstrate the relevant skills, include a working proof, and explain your trade-offs and limitations.</div>", unsafe_allow_html=True)
    for index, plan in enumerate(result.project_plans, 1):
        addressed = [skill for skill in plan.skills_practised if skill in missing]
        with st.expander(f"{index}. {plan.title} · about {plan.estimated_hours} hours", expanded=index == 1):
            st.markdown(f"**Why this helps:** {plan.problem}")
            if addressed:
                st.markdown("**Evidence gaps it can help you address:** " + ", ".join(addressed))
                for skill in addressed:
                    requirement = requirement_by_skill.get(skill)
                    source = evidence_by_id.get(requirement.evidence_id) if requirement else None
                    if source:
                        st.caption(f"{skill} in this job brief: “{source.quote}”")
            else:
                st.caption("This is a supporting project idea; it is not being presented as proof of a missing requirement.")
            milestones, proof = st.columns(2)
            with milestones:
                st.markdown("**Build sequence**")
                for number, milestone in enumerate(plan.milestones, 1):
                    st.markdown(f"{number}. {milestone}")
            with proof:
                st.markdown("**Publishable proof**")
                for item in [*plan.deliverables, *plan.acceptance_tests]:
                    st.markdown(f"- {item}")
            if plan.readme_outline:
                st.markdown("**README outline**")
                st.write(" → ".join(plan.readme_outline))
            st.info("When it is complete, describe it accurately as a personal project and be ready to explain your exact contribution.")


def _requirement_editor(job_text: str) -> None:
    """Review current-job labels; reuse aliases only through a separate explicit action."""
    scope = st.session_state.get("active_scope")
    key = f"{scope[1] if scope else 'local'}-{document_key(job_text)}"
    requirements, evidence = extract_requirements(job_text, KNOWN_SKILLS, memory=REQUIREMENT_MEMORY)
    quotes = {item.id: item.quote for item in evidence}
    with st.expander("Review inferred requirements / leave parsing feedback (optional)", expanded=False):
        st.caption("English-first, rule-based inference, not a trained semantic model. Edit names or importance, remove false positives, or add a missing skill with an exact quote from the description.")
        if requirements:
            st.dataframe([{"Skill": r.skill, "Importance": r.importance, "Method": r.extraction_method,
                           "Heuristic confidence (not calibrated)": r.confidence, "Needs review": r.needs_review}
                          for r in requirements], hide_index=True)
        else:
            st.warning("No requirements were inferred yet. Add a source quote and capability manually below, or paste more job context.")
        rating = st.selectbox("Was this extraction useful?", ["useful", "partly useful", "not useful"], key=f"feedback-rating-{key}")
        feedback_comment = st.text_area("Feedback for the parser (optional)", key=f"feedback-comment-{key}", placeholder="For example: 'Treat comfortable with X as preferred in this kind of role.'")
        if st.button("Save extraction feedback", key=f"feedback-save-{key}"):
            try:
                REQUIREMENT_MEMORY.save_feedback(job_text, rating, feedback_comment)
                st.success("Feedback saved locally. It will be reviewed as product data; it does not silently retrain the model.")
            except ValueError as exc:
                st.error(str(exc))
        rows = [{"keep": True, "skill": r.skill, "importance": r.importance, "quote": quotes[r.evidence_id]}
                for r in requirements]
        edited = st.data_editor(rows or [{"keep": True, "skill": "", "importance": "required", "quote": ""}],
            num_rows="dynamic", key=f"requirements-{key}", hide_index=True,
            column_config={
                "keep": st.column_config.CheckboxColumn("Keep", default=True),
                "skill": st.column_config.TextColumn("Skill / capability", required=True),
                "importance": st.column_config.SelectboxColumn("Importance", options=["required", "preferred", "uncertain"], required=True),
                "quote": st.column_config.TextColumn("Exact source quote", required=True),
            })
        confirmed = st.checkbox("I reviewed these interpretations against the source text", key=f"review-confirm-{key}")
        if st.button("Save corrections for this description", key=f"save-review-{key}", disabled=not confirmed):
            try:
                save_requirement_review(job_text, edited, REQUIREMENT_MEMORY)
                st.session_state.pop("analysis", None)
                WORKSPACES.invalidate_description(job_text)
                st.success("Corrections saved locally. Select Refresh this job workspace to use them. Future loads of this exact description will reuse them.")
            except ValueError as exc:
                st.error(str(exc))
        if st.button("Forget corrections for this description", key=f"forget-review-{key}"):
            REQUIREMENT_MEMORY.forget_review(job_text)
            WORKSPACES.invalidate_description(job_text)
            st.session_state.pop(f"requirements-{key}", None)
            st.session_state.pop("analysis", None)
            st.rerun()
        st.markdown("**Teach a reusable phrase → skill mapping**")
        st.caption("Only learn mappings you explicitly approve. Required/preferred importance is inferred afresh in each new job, not copied from this one.")
        phrase = st.text_input("Exact phrase to recognize in future jobs", key=f"alias-phrase-{key}", placeholder="event-driven services")
        skill = st.text_input("Canonical skill name", key=f"alias-skill-{key}", placeholder="Event-driven architecture")
        remember = st.checkbox("Reuse this mapping in future job-workspace assessments", key=f"alias-confirm-{key}")
        if st.button("Remember mapping", key=f"alias-save-{key}", disabled=not remember):
            try:
                reviewed = REQUIREMENT_MEMORY.reviewed(job_text)
                if not reviewed:
                    raise ValueError("Save a reviewed description before teaching a reusable mapping")
                confirmed_quotes = {e.id: e.quote for e in reviewed[1]}
                source_quote = next((confirmed_quotes[r.evidence_id] for r in reviewed[0]
                                     if r.skill.casefold() == skill.strip().casefold() and
                                     phrase.strip().casefold() in confirmed_quotes[r.evidence_id].casefold()), None)
                if source_quote is None:
                    raise ValueError("The mapping must agree with a saved skill and its source quote")
                REQUIREMENT_MEMORY.learn_alias(phrase, skill, source_quote=source_quote, user_confirmed=True)
                st.success("Mapping remembered locally. No external model was trained and no CV data was used.")
            except ValueError as exc:
                st.error(str(exc))
        aliases = REQUIREMENT_MEMORY.aliases()
        if aliases:
            st.dataframe([{"Phrase": p, "Skill": s} for p, s in aliases.items()], hide_index=True)
            forgotten = st.selectbox("Mapping to remove", list(aliases), key=f"alias-remove-{key}")
            if st.button("Forget mapping", key=f"alias-delete-{key}"):
                REQUIREMENT_MEMORY.forget_alias(forgotten)
                st.rerun()


def _open_job_thread(job, key: str) -> None:
    if st.button("Create job workspace", key=key):
        wid = st.session_state["active_scope"][0]
        draft = {**WORKSPACES.shared_profile(), "job_title": job.title, "company": job.company,
                 "job_url": job.url or "", "job_text": job.description or ""}
        tid = WORKSPACES.create_thread(wid, f"{job.title} — {job.company}"[:100], draft)
        st.session_state[f"pending_thread:{wid}"] = tid
        st.rerun()


def _opportunities(workspace_id: str, thread_id: str) -> None:
    _company_explorer(workspace_id, thread_id)

    st.divider()
    st.subheader("Permitted job discovery")
    st.caption("User-triggered refreshes of public career-board APIs. No scraping, no stored credentials.")
    source, identifier = st.columns(2)
    with source:
        source_kind = st.selectbox("Public source", ["Greenhouse", "Lever", "Recruitee"])
    with identifier:
        token = st.text_input("Board identifier", help="Greenhouse board token, Lever site handle, or Recruitee company slug.")
    if st.button("Fetch and monitor public job postings"):
        try:
            if source_kind == "Greenhouse":
                jobs = fetch_greenhouse_jobs(token)
            elif source_kind == "Lever":
                jobs = fetch_lever_jobs(token)
            else:
                jobs = fetch_recruitee_jobs(token)
            new_jobs = MONITOR.refresh(source=source_kind, identifier=token, jobs=jobs)
            st.session_state["source_jobs"] = jobs
            st.session_state["new_source_jobs"] = new_jobs
            STORE.record_event("public_job_fetch", {"source": source_kind, "identifier": token, "count": len(jobs), "new_count": len(new_jobs)})
        except Exception as exc:
            st.error(str(exc))
    if "new_source_jobs" in st.session_state:
        st.info(f"{len(st.session_state['new_source_jobs'])} newly observed job(s) on the latest user-triggered refresh.")
    for index, job in enumerate(st.session_state.get("source_jobs", [])[:30]):
        st.markdown(f"- **{job.title}** · {job.company} · {job.location or 'Location not listed'}" + (f" · [Official page]({job.url})" if job.url else ""))
        _open_job_thread(job, f"open-source:{index}")

    st.divider()
    st.subheader("Official-platform connectors")
    st.caption("Tokens are used for this request only and are never saved. No profile scraping, messaging, or posting.")
    official_tab, import_tab = st.tabs(["Official APIs", "Import your lawful export"])
    with official_tab:
        linkedin_token = st.text_input("LinkedIn OAuth access token", value=os.environ.get("LINKEDIN_ACCESS_TOKEN", ""), type="password", help="Requires a LinkedIn app and OIDC permissions. Verifies the connected account only.")
        if st.button("Verify authorized LinkedIn identity"):
            try:
                identity = fetch_linkedin_authorized_identity(linkedin_token)
                st.session_state["linkedin_identity"] = identity
                STORE.record_event("linkedin_authorized_identity", {"subject": identity.subject})
            except Exception as exc:
                st.error(str(exc))
        if identity := st.session_state.get("linkedin_identity"):
            st.success(f"Authorized LinkedIn identity: {identity.name or identity.subject}")
        smart_company = st.text_input("SmartRecruiters company identifier")
        smart_token = st.text_input("SmartRecruiters Posting API token", type="password")
        if st.button("Fetch SmartRecruiters postings through the official API"):
            try:
                smart_jobs = fetch_smartrecruiters_postings(smart_company, smart_token)
                new_smart_jobs = MONITOR.refresh(source="SmartRecruiters", identifier=smart_company, jobs=smart_jobs)
                st.session_state["smart_jobs"] = smart_jobs
                st.success(f"Fetched {len(smart_jobs)} role(s), including {len(new_smart_jobs)} newly observed.")
                STORE.record_event("smartrecruiters_official_fetch", {"identifier": smart_company, "count": len(smart_jobs), "new_count": len(new_smart_jobs)})
            except Exception as exc:
                st.error(str(exc))
        for index, job in enumerate(st.session_state.get("smart_jobs", [])[:20]):
            st.markdown(f"- **{job.title}** · {job.company}" + (f" · [Official page]({job.url})" if job.url else ""))
            _open_job_thread(job, f"open-smart:{index}")
        x_query = st.text_input("X API v2 recent-search query", placeholder='("hiring" OR "we are hiring") (AI OR software) lang:en')
        x_token = st.text_input("X API v2 bearer token", value=os.environ.get("X_BEARER_TOKEN", ""), type="password")
        x_count = st.slider("Maximum posts", min_value=10, max_value=100, value=10)
        if st.button("Search recent X posts through the official API"):
            try:
                signals = fetch_x_recent_posts(x_query, x_token, x_count)
                st.session_state["x_signals"] = signals
                STORE.record_event("x_official_recent_search", {"query": x_query, "count": len(signals)})
            except Exception as exc:
                st.error(str(exc))
        for signal in st.session_state.get("x_signals", []):
            st.markdown(f"- {signal.text}" + (f" · [Open source post]({signal.url})" if signal.url else ""))
    with import_tab:
        st.caption("Upload CSV or JSON that you personally exported or obtained with permission. Up to 500 records/5 MB; imported facts remain reviewable.")
        contact_export = st.file_uploader("Contact export (.csv or .json)", type=["csv", "json"], key="contact_export")
        if contact_export and st.button("Import contact export"):
            try:
                leads = import_contact_export(contact_export.getvalue(), contact_export.name)
                st.session_state["imported_leads"] = leads
                STORE.record_event("contact_export_import", {"filename": contact_export.name, "count": len(leads)})
                st.success(f"Imported {len(leads)} contacts for review.")
            except Exception as exc:
                st.error(str(exc))
        job_export = st.file_uploader("Job export (.csv or .json)", type=["csv", "json"], key="job_export")
        if job_export and st.button("Import job export"):
            try:
                jobs = import_job_export(job_export.getvalue(), job_export.name)
                st.session_state["imported_jobs"] = jobs
                STORE.record_event("job_export_import", {"filename": job_export.name, "count": len(jobs)})
                st.success(f"Imported {len(jobs)} evidence-ready job records.")
            except Exception as exc:
                st.error(str(exc))
        for index, job in enumerate(st.session_state.get("imported_jobs", [])[:20]):
            st.markdown(f"- **{job.title}** · {job.company}" + (f" · [Source]({job.url})" if job.url else ""))
            _open_job_thread(job, f"open-import:{index}")

    st.divider()
    st.subheader("Human-reviewed contact research")
    result = st.session_state.get("analysis")
    default_company = result.job.company if result else ""
    default_title = result.job.title if result else ""
    company = st.text_input("Target company", value=default_company, key="contact_company")
    title = st.text_input("Target role", value=default_title, key="contact_title")
    contact_scope = st.session_state.get("active_scope", ("", "local"))[1]
    lead_text = st.text_area("Verified public contacts (one per line: Name | Role | public profile URL | public email if explicitly published)", height=110, key=f"contacts:{contact_scope}")
    observed_pattern = st.text_input("Observed company email format (optional; never used to generate addresses)", placeholder="first.last@company.com", key=f"email-pattern:{contact_scope}")
    if st.button("Create research checklist"):
        leads = [*st.session_state.get("imported_leads", []), *parse_contact_leads(lead_text)]
        plan = contact_research_plan(company, title, leads, observed_pattern or None)
        st.session_state["contact_plan"] = plan
        STORE.record_event("contact_research_plan", {"company": company, "lead_count": len(plan.leads)})
    plan = st.session_state.get("contact_plan")
    if plan:
        st.warning(plan.safety_note)
        st.write("**Prioritize:** " + " → ".join(plan.target_roles))
        st.write("**Manual search queries:**")
        st.code("\n".join(plan.search_queries))
        if plan.leads:
            st.dataframe([lead.model_dump() for lead in plan.leads], use_container_width=True, hide_index=True)
        if plan.email_pattern:
            st.caption(f"Observed format recorded: {plan.email_pattern}. This is not an individual email address and must not be used to guess one.")
    with st.expander("Local monitoring registry"):
        st.caption("Monitoring is user-triggered only. Source identifiers are retained locally; credentials are never stored.")
        watches = MONITOR.watches()
        if watches:
            st.dataframe(watches, use_container_width=True, hide_index=True)
        else:
            st.write("No source has been refreshed yet.")


def _company_explorer(workspace_id: str, thread_id: str) -> None:
    """Show transparent, profile-derived companies on an accessible world map."""
    thread = WORKSPACES.thread(workspace_id, thread_id)
    profile = thread["draft"]
    profile_source = "this Job Workspace"
    if not profile["cv_text"].strip():
        shared = WORKSPACES.shared_profile()
        if shared["cv_text"].strip():
            profile = shared
            profile_source = "your saved shared profile"

    st.subheader("Profile company explorer")
    st.caption("A small, curated world map of companies worth researching from skills detected in your CV. These are not live openings, hiring predictions, or a claim that every pin is a headquarters.")
    if not profile["cv_text"].strip():
        st.info("Add your CV in Job brief first. We will also ask for GitHub and/or a portfolio there when you have them; links are optional and no profile is fetched automatically.")
        return

    links = {field: profile[field] for field in PROFILE_FIELDS if field != "cv_text" and profile[field].strip()}
    skills = detected_profile_skills(profile["cv_text"], links)
    if not skills:
        st.warning("No skills from the local profile vocabulary were detected yet. Add a clear `Skills: Python, SQL, …` line to your CV in Job brief, then return here.")
        return

    st.caption(f"Using {profile_source}. Detected profile skills: {' · '.join(skills[:12])}" + (f" · +{len(skills) - 12} more" if len(skills) > 12 else ""))
    regions = sorted({pin.region for pin in COMPANY_PINS})
    selected_regions = st.multiselect(
        "Explore regions",
        regions,
        default=regions,
        key=f"explorer-regions:{thread_id}",
        help="Only the displayed catalogue pins are filtered. This does not imply immigration eligibility, remote-work availability, or an active role.",
    )
    recommendations = recommend_companies(
        profile["cv_text"],
        links=links,
        regions=tuple(selected_regions),
    )
    if not recommendations:
        st.info("No catalogue companies overlap with the selected profile skills in these regions. Try another region or add more explicit skills to your CV.")
        return

    map_rows = [
        {
            "company": item.company,
            "city": item.city,
            "country": item.country,
            "focus": item.focus,
            "matching_skills": ", ".join(item.matching_skills),
            "longitude": item.longitude,
            "latitude": item.latitude,
            "radius": 90_000 + (len(item.matching_skills) * 22_000),
        }
        for item in recommendations
    ]
    layer = pdk.Layer(
        "ScatterplotLayer",
        id=f"company-explorer-markers:{thread_id}",
        data=map_rows,
        get_position=["longitude", "latitude"],
        get_radius="radius",
        get_fill_color=[32, 80, 65, 190],
        radius_min_pixels=7,
        radius_max_pixels=20,
        pickable=True,
        auto_highlight=True,
    )
    deck = pdk.Deck(
        layers=[layer],
        initial_view_state=pdk.ViewState(latitude=18, longitude=8, zoom=1.08, pitch=0),
        map_style="https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
        tooltip={
            "html": "<b>{company}</b><br/>{city}, {country}<br/>{focus}<br/><br/>Profile overlap: {matching_skills}",
            "style": {"backgroundColor": "#173a31", "color": "#fffef8"},
        },
    )
    st.pydeck_chart(deck, use_container_width=True, height=480, key=f"company-explorer-map:{thread_id}")

    footprint_order = sorted(
        recommendations,
        key=lambda item: (-len(company_footprint(item.company)), -len(item.matching_skills), item.company.casefold()),
    )
    options = {f"{item.company} — {item.city}, {item.country}": item for item in footprint_order}
    selected_label = st.selectbox("Inspect a company's footprint", list(options), key=f"explorer-company:{thread_id}")
    selected = options[selected_label]
    with st.container(border=True):
        st.markdown(f"#### {selected.company} · {selected.city}, {selected.country}")
        st.write(selected.focus)
        st.write("Why it appears here: your profile overlaps with " + ", ".join(selected.matching_skills) + ".")
        st.caption("Review the company and current openings yourself. Skill overlap is only a research starting point—not evidence that a role fits, is open, or is accessible to you.")
        st.link_button(f"Open {selected.company} careers page", selected.careers_url, type="secondary")

    _company_footprint(selected, thread_id)

    st.markdown("**All visible companies**")
    st.dataframe(
        [
            {
                "Company": item.company,
                "Selected hub": f"{item.city}, {item.country}",
                "Focus": item.focus,
                "Profile overlap": ", ".join(item.matching_skills),
            }
            for item in recommendations
        ],
        hide_index=True,
        use_container_width=True,
    )


def _company_footprint(selected, thread_id: str) -> None:
    """Drill into one company's selected, source-linked locations."""
    hubs = company_footprint(selected.company)
    st.markdown(f"### {selected.company} footprint")
    st.caption("Selected source-linked locations, not a complete office directory or live-role map. A company can have one Global/Group HQ and several specialised hubs; a location is called an HQ only when the linked source supports that label.")
    if len(hubs) == 1:
        st.info("This first catalogue has one selected hub for this company. Add verified locations before treating it as a global footprint.")

    map_rows = [
        {
            "site_type": hub.site_type,
            "city": hub.city,
            "country": hub.country,
            "focus": hub.focus,
            "longitude": hub.longitude,
            "latitude": hub.latitude,
            "color": [32, 80, 65, 205] if "HQ" in hub.site_type else [210, 130, 75, 205],
        }
        for hub in hubs
    ]
    layer = pdk.Layer(
        "ScatterplotLayer",
        id=f"company-footprint-markers:{thread_id}",
        data=map_rows,
        get_position=["longitude", "latitude"],
        get_radius=115_000,
        get_fill_color="color",
        radius_min_pixels=8,
        radius_max_pixels=18,
        pickable=True,
        auto_highlight=True,
    )
    if len(hubs) == 1:
        latitude, longitude, zoom = hubs[0].latitude, hubs[0].longitude, 3.1
    else:
        latitude = sum(hub.latitude for hub in hubs) / len(hubs)
        longitude = sum(hub.longitude for hub in hubs) / len(hubs)
        zoom = 1.0
    deck = pdk.Deck(
        layers=[layer],
        initial_view_state=pdk.ViewState(latitude=latitude, longitude=longitude, zoom=zoom, pitch=0),
        map_style="https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
        tooltip={
            "html": "<b>{site_type}</b><br/>{city}, {country}<br/><br/>{focus}",
            "style": {"backgroundColor": "#173a31", "color": "#fffef8"},
        },
    )
    st.pydeck_chart(deck, use_container_width=True, height=360, key=f"company-footprint-map:{thread_id}")

    st.markdown("**What each selected site is publicly described as doing**")
    for index, hub in enumerate(hubs):
        with st.container(border=True):
            st.markdown(f"**{hub.site_type} · {hub.city}, {hub.country}**")
            st.write(hub.focus)
            source_note = f"Public source" + (f" ({hub.source_year})" if hub.source_year else "") + "."
            st.caption(source_note)
            if hub.verification_note:
                st.caption(hub.verification_note)
            st.link_button(
                f"Open source for {hub.city}",
                hub.source_url,
                type="secondary",
                key=f"footprint-source:{thread_id}:{selected.company}:{index}",
            )


def _interview(workspace_id: str, thread_id: str) -> None:
    """A saved, role-specific rehearsal area—not a generic one-off question form."""
    thread = WORKSPACES.thread(workspace_id, thread_id)
    result = thread["analysis"] if not thread["stale"] else None
    st.subheader("Interview lab")
    st.caption("Practice answers for this target role. Feedback checks answer structure, not factual or technical correctness; saved attempts stay in this job workspace.")
    if thread["stale"]:
        st.warning("Refresh the Job brief first. Interview questions are held back while the role evidence is stale.")
        return
    if not result:
        st.info("Analyze this job in Job brief to get evidence-aware, role-specific interview questions.")
        return
    attempts = WORKSPACES.interview_attempts(workspace_id, thread_id)
    if attempts:
        average = round(sum(attempt["score"] for attempt in attempts) / len(attempts))
        history_metrics = st.columns(3)
        history_metrics[0].metric("Saved rehearsals", len(attempts))
        history_metrics[1].metric("Average practice evidence score", f"{average}/100")
        history_metrics[2].metric("Question areas covered", len({attempt["category"] for attempt in attempts}))
    else:
        st.info("Your first saved rehearsal will create a private practice history for this role.")
    questions = build_interview_questions(result.candidate, result.job)
    selected = st.selectbox("Question", questions, format_func=lambda question: f"{question.category.title()} — {question.question}", key=f"interview-question:{thread_id}")
    scope = f"{thread_id}:{selected.id}"
    st.write("A strong answer should include: " + "; ".join(selected.what_good_evidence_looks_like))
    answer = st.text_area("Your answer (type it, or transcribe an optional recording below)", height=160, key=f"interview-answer:{scope}")
    try:
        audio = st.audio_input("Record an answer (optional)", key=f"interview-audio:{scope}")
    except AttributeError:
        audio = None
        st.caption("Upgrade Streamlit to use in-app audio recording; typed answers work now.")
    if audio is not None:
        st.audio(audio)
        if st.button("Transcribe recording with OpenAI"):
            key = os.environ.get("OPENAI_API_KEY")
            if not key:
                st.error("Set OPENAI_API_KEY to use optional transcription. Audio is not sent otherwise.")
            else:
                try:
                    transcript = transcribe_openai_audio(audio.getvalue(), getattr(audio, "name", "answer.wav"), key)
                    st.session_state[f"interview-transcript:{scope}"] = transcript
                except Exception as exc:
                    st.error(f"Transcription failed: {exc}")
    if transcript := st.session_state.get(f"interview-transcript:{scope}"):
        transcript = st.text_area("Transcript", transcript, height=100, key=f"transcript-display:{scope}")
        answer = answer or transcript
    if st.button("Score and save this rehearsal", type="primary"):
        if not answer.strip():
            st.error("Write or transcribe an answer before saving this rehearsal.")
        else:
            feedback = evaluate_answer(selected, answer)
            try:
                WORKSPACES.save_interview_attempt(
                    workspace_id,
                    thread_id,
                    question_id=selected.id,
                    category=selected.category,
                    question=selected.question,
                    answer=answer,
                    score=feedback.score,
                    strengths=feedback.strengths,
                    missing=feedback.missing,
                )
                st.success("Rehearsal saved in this job workspace.")
                st.metric("Practice evidence score", f"{feedback.score}/100", help="Checks answer structure, not technical correctness.")
                st.write("**Strengths:** " + (" ".join(feedback.strengths) or "Not enough evidence yet."))
                st.write("**Improve:** " + " ".join(feedback.missing))
                st.info("Follow-up: " + feedback.follow_up)
            except ValueError as exc:
                st.error(str(exc))

    if attempts:
        with st.expander("Saved practice history", expanded=False):
            for attempt in attempts[:12]:
                when = attempt["created_at"].split("T", 1)[0]
                st.markdown(f"**{attempt['category'].title()} · {attempt['score']}/100 · {when}**")
                st.caption(attempt["question"])
                st.write(attempt["answer"])
                if attempt["missing"]:
                    st.caption("Next improvement: " + " ".join(attempt["missing"]))


def _upload_text(uploaded) -> str:
    suffix = Path(uploaded.name).suffix
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temporary:
        temporary.write(uploaded.getvalue())
        path = Path(temporary.name)
    try:
        return load_document(path)
    finally:
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
