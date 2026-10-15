"""CLI entry points for local analysis, optional agent use, and the local UI."""

import argparse
import asyncio
from pathlib import Path
import subprocess
import sys

from tau_job_application.parsing import load_document
from tau_job_application.pipeline import analyze_files, render_markdown

def _project_root() -> Path:
    """Find fixtures when installed as either editable or regular package."""
    for directory in (Path.cwd(), *Path.cwd().parents):
        if (directory / "fixtures").is_dir():
            return directory
    return Path.cwd()


PROJECT_ROOT = _project_root()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evidence-first job-readiness assistant")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo", help="Run the deterministic synthetic demo")
    analyze = commands.add_parser("analyze", help="Analyze two local CV/job documents")
    analyze.add_argument("candidate", type=Path)
    analyze.add_argument("job", type=Path)
    agent = commands.add_parser("agent", help="Run the optional Tau agent (requires model credentials)")
    agent.add_argument("candidate", type=Path)
    agent.add_argument("job", type=Path)
    commands.add_parser("ui", help="Launch the local Streamlit application")

    prepare = commands.add_parser("prepare", help="Fit-check jobs and draft application packs for review (never submits)")
    prepare.add_argument("candidate", type=Path)
    prepare.add_argument("jobs", type=Path, nargs="+")
    prepare.add_argument("--location", action="append", default=[], help="Target location; repeat for several")
    prepare.add_argument("--no-remote", action="store_true", help="Do not accept remote roles outside target locations")
    prepare.add_argument("--exclude", action="append", default=[], help="Company to skip; repeat for several")
    prepare.add_argument("--min-verdict", choices=["Strong Fit", "Potential Fit", "Stretch"], default="Stretch")
    prepare.add_argument("--llm", action="store_true", help="Draft with a model using Agent_skills (needs OPENAI_API_KEY and MODEL_NAME)")
    prepare.add_argument("--out", type=Path, default=None, help="Folder for application packs")
    prepare.add_argument("--confirm-skill", action="append", default=[], dest="confirm_skills",
                         help="A skill you can evidence that your CV does not list in a skills line; repeat for several")

    tracker = commands.add_parser("tracker", help="List tracked applications")
    tracker.add_argument("--status", default=None)
    show = commands.add_parser("show", help="Show one application pack and its history")
    show.add_argument("id", type=int)
    for name, help_text in (("approve", "Approve a reviewed pack (you)"), ("applied", "Record that you submitted it yourself"),
                            ("skip", "Skip a role"), ("reopen", "Re-open a skipped role")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("id", type=int)
        command.add_argument("--note", default=None)
    outcome = commands.add_parser("outcome", help="Record an outcome after applying")
    outcome.add_argument("id", type=int)
    outcome.add_argument("status", choices=["interviewing", "offer", "rejected", "withdrawn", "no_response"])
    outcome.add_argument("--note", default=None)
    commands.add_parser("follow-ups", help="List applications due a follow-up")
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command == "demo":
        print(render_markdown(analyze_files(PROJECT_ROOT / "fixtures" / "candidate.txt", PROJECT_ROOT / "fixtures" / "job.txt")))
    elif args.command == "analyze":
        print(render_markdown(analyze_files(args.candidate, args.job)))
    elif args.command == "ui":
        subprocess.run([sys.executable, "-m", "streamlit", "run", str(Path(__file__).with_name("ui.py"))], check=True)
    elif args.command == "agent":
        asyncio.run(_run_agent(args.candidate, args.job))
    else:
        _application_command(args)


def _application_command(args: argparse.Namespace) -> None:
    from tau_job_application.tracker import ApplicationTracker, TransitionError

    tracker = ApplicationTracker(PROJECT_ROOT / ".job_assistant" / "assistant.sqlite")
    try:
        if args.command == "prepare":
            print(_prepare(args, tracker))
        elif args.command == "tracker":
            rows = tracker.list(args.status)
            print("\n".join(f"{row.id:>4}  {row.status:<12} {row.verdict or '-':<13} {row.match_score if row.match_score is not None else '-':>3}  "
                            f"{row.job_title} at {row.company}" for row in rows) or "No tracked applications.")
        elif args.command == "show":
            row = tracker.get(args.id)
            print(row.pack_markdown or "No pack drafted yet.")
            print("\n## History")
            for event in tracker.history(args.id):
                print(f"- {event['created_at']} {event['actor']}: {event['from_status'] or 'new'} -> {event['to_status']}")
        elif args.command == "follow-ups":
            rows = tracker.due_follow_ups()
            print("\n".join(f"{row.id:>4}  {row.job_title} at {row.company} (due {row.follow_up_at})" for row in rows) or "No follow-ups due.")
        else:
            target = {"approve": "approved", "applied": "applied", "skip": "skipped", "reopen": "assessed"}.get(args.command, getattr(args, "status", None))
            row = tracker.transition(args.id, target, actor="user", note=args.note)
            print(f"{row.id}: {row.job_title} at {row.company} is now '{row.status}'.")
    except (TransitionError, KeyError) as exc:
        raise SystemExit(f"Error: {exc}") from exc


def _prepare(args: argparse.Namespace, tracker) -> str:
    from tau_job_application.application_pack import LLMDrafter, TemplateDrafter
    from tau_job_application.fit_check import CandidatePreferences
    from tau_job_application.orchestrator import ApplicationAgent, JobInput, render_report

    preferences = CandidatePreferences(target_locations=args.location, remote_ok=not args.no_remote,
                                       excluded_companies=args.exclude, minimum_verdict=args.min_verdict)
    drafter = TemplateDrafter()
    if args.llm:
        from tau_job_application.agent import build_completion
        drafter = LLMDrafter(build_completion(), PROJECT_ROOT / "Agent_skills")
    agent = ApplicationAgent(tracker, export_dir=args.out or PROJECT_ROOT / ".job_assistant" / "packs",
                             preferences=preferences, drafter=drafter)
    jobs = [JobInput(text=load_document(path)) for path in args.jobs]
    return render_report(agent.run(load_document(args.candidate), jobs, confirmed_skills=args.confirm_skills))


async def _run_agent(candidate_path: Path, job_path: Path) -> None:
    candidate_text, job_text = load_document(candidate_path), load_document(job_path)
    prompt = "Use tools to analyse this candidate and job. Do not add unsupported facts.\n\nCANDIDATE\n" + candidate_text + "\n\nJOB\n" + job_text
    # Import Tau only for the optional model-backed command. The deterministic CLI stays dependency-light.
    from tau_job_application.agent import build_agent

    async for event in build_agent().prompt(prompt):
        print(event)
