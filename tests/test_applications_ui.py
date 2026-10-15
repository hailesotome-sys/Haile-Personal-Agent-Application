from streamlit.testing.v1 import AppTest

from tau_job_application.applications_ui import split_job_texts

CV = "Name: Alex Example\nHeadline: Python developer\nSkills: Python, SQL, FastAPI, Git, pytest\nExperience: Built and tested small Python services.\n"
JOBS = ("Title: Junior Backend Engineer\nCompany: Example Systems\nLocation: Zurich\nRequired skills: Python, SQL, Docker\n"
        "---\nTitle: Embedded Engineer\nCompany: Chip Co\nLocation: Zurich\nRequired skills: Verilog, FPGA, RTOS\n")


def _app(tmp_path, monkeypatch):
    from tau_job_application import ui
    from tau_job_application.monitoring import JobMonitor
    from tau_job_application.requirement_memory import RequirementMemory
    from tau_job_application.storage import LocalStore
    from tau_job_application.tracker import ApplicationTracker
    from tau_job_application.workspaces import WorkspaceStore

    db = tmp_path / "ui.sqlite"
    for name, value in (("WORKSPACES", WorkspaceStore(db)), ("STORE", LocalStore(db)), ("MONITOR", JobMonitor(db)),
                        ("REQUIREMENT_MEMORY", RequirementMemory(db)), ("TRACKER", ApplicationTracker(db)),
                        ("ROOT", tmp_path)):
        monkeypatch.setattr(ui, name, value)
    return AppTest.from_string("from tau_job_application.ui import main\nmain()", default_timeout=30).run(), ui


def _button(app, label):
    return next(button for button in app.button if button.label == label)


def test_split_job_texts() -> None:
    assert split_job_texts("a\n---\nb\n---\n\n") == ["a", "b"]


def test_applications_tab_prepares_reviews_and_tracks(tmp_path, monkeypatch) -> None:
    app, ui = _app(tmp_path, monkeypatch)
    next(x for x in app.text_area if x.label == "Your CV").set_value(CV)
    next(x for x in app.text_area if x.label == "Job descriptions").set_value(JOBS)
    _button(app, "Run the agent").click()
    app.run()
    assert not app.exception
    assert any("1 drafted, 1 skipped" in success.value for success in app.success)

    drafted = ui.TRACKER.list("drafted")[0]
    _button(app, "Approve").click()
    app.run()
    assert any("GAP" in error.value for error in app.error)
    assert ui.TRACKER.get(drafted.id).status == "drafted"

    pack = next(x for x in app.text_area if x.label.startswith("Application pack"))
    pack.set_value(pack.value.replace("[GAP", "(answered) "))
    _button(app, "Approve").click()
    app.run()
    assert not app.exception
    assert ui.TRACKER.get(drafted.id).status == "approved", [e.value for e in app.error]

    _button(app, "I applied").click()
    app.run()
    assert ui.TRACKER.get(drafted.id).status == "applied"
    assert ui.TRACKER.get(drafted.id).follow_up_at
    _button(app, "Save outcome").click()
    app.run()
    assert ui.TRACKER.get(drafted.id).status == "interviewing"


def test_editing_an_approved_pack_requires_reapproval(tmp_path) -> None:
    from tau_job_application.orchestrator import ApplicationAgent, JobInput
    from tau_job_application.tracker import ApplicationTracker

    tracker = ApplicationTracker(tmp_path / "db.sqlite")
    result = ApplicationAgent(tracker, export_dir=tmp_path / "packs").run(CV, [JobInput(text=split_job_texts(JOBS)[0])]).results[0]
    text = tracker.get(result.application_id).pack_markdown.replace("[GAP", "(answered) ")
    tracker.save_reviewed_text(result.application_id, text)
    tracker.transition(result.application_id, "approved", actor="user")
    assert tracker.save_reviewed_text(result.application_id, text).status == "approved"
    assert tracker.save_reviewed_text(result.application_id, text + "\nExtra").status == "drafted"
