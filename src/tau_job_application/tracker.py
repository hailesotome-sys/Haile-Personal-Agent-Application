"""Local application tracker with an explicit state machine and human-only gates.

The agent may discover, assess, skip and draft. Only the user may approve a pack,
record that they applied, or record an outcome. The tracker has no submit state:
"applied" means the user says they submitted it themselves.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Literal

from pydantic import BaseModel

Status = Literal[
    "assessed", "skipped", "drafted", "approved", "applied",
    "interviewing", "offer", "rejected", "withdrawn", "no_response",
]
Actor = Literal["agent", "user"]

TRANSITIONS: dict[str, set[str]] = {
    "assessed": {"skipped", "drafted"},
    "skipped": {"assessed"},
    "drafted": {"approved", "skipped", "drafted"},
    "approved": {"applied", "drafted", "skipped"},
    "applied": {"interviewing", "rejected", "offer", "withdrawn", "no_response"},
    "interviewing": {"offer", "rejected", "withdrawn"},
    "no_response": {"interviewing", "rejected", "withdrawn"},
    "offer": {"withdrawn"},
    "rejected": set(),
    "withdrawn": set(),
}
USER_ONLY = {"assessed", "approved", "applied", "interviewing", "offer", "rejected", "withdrawn", "no_response"}
FOLLOW_UP_AFTER = timedelta(days=7)


class TrackedApplication(BaseModel):
    id: int
    fingerprint: str
    company: str
    job_title: str
    job_url: str | None
    status: Status
    verdict: str | None
    match_score: int | None
    claim_status: str | None
    pack_markdown: str | None
    pack_path: str | None = None
    apply_score: int | None = None
    analysis_json: str | None = None
    follow_up_at: str | None
    notes: str
    created_at: str
    updated_at: str


class TransitionError(ValueError):
    pass


class ApplicationTracker:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS applications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT NOT NULL UNIQUE,
                    company TEXT NOT NULL,
                    job_title TEXT NOT NULL,
                    job_url TEXT,
                    status TEXT NOT NULL,
                    verdict TEXT,
                    match_score INTEGER,
                    claim_status TEXT,
                    pack_markdown TEXT,
                    pack_path TEXT,
                    apply_score INTEGER,
                    analysis_json TEXT,
                    follow_up_at TEXT,
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS application_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    application_id INTEGER NOT NULL REFERENCES applications(id),
                    created_at TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );
            """)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(applications)")}
            for column, kind in (("pack_path", "TEXT"), ("apply_score", "INTEGER"), ("analysis_json", "TEXT")):
                if column not in columns:  # databases created before these columns existed
                    connection.execute(f"ALTER TABLE applications ADD COLUMN {column} {kind}")

    @staticmethod
    def fingerprint(company: str, job_title: str, job_url: str | None) -> str:
        key = "|".join(" ".join(part.casefold().split()) for part in (company, job_title, job_url or ""))
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    def find(self, company: str, job_title: str, job_url: str | None) -> TrackedApplication | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM applications WHERE fingerprint = ?",
                                     (self.fingerprint(company, job_title, job_url),)).fetchone()
        return TrackedApplication(**dict(row)) if row else None

    def record_assessment(self, *, company: str, job_title: str, job_url: str | None, verdict: str, match_score: int) -> TrackedApplication:
        """Create or refresh an assessed role. Roles already approved or further along are left alone."""
        existing = self.find(company, job_title, job_url)
        now = self._now()
        if existing is None:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO applications (fingerprint, company, job_title, job_url, status, verdict, match_score, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 'assessed', ?, ?, ?, ?)",
                    (self.fingerprint(company, job_title, job_url), company, job_title, job_url, verdict, match_score, now, now),
                )
                self._log(connection, int(cursor.lastrowid), "agent", None, "assessed", {"verdict": verdict, "match_score": match_score})
            return self.get(int(cursor.lastrowid))
        if existing.status in {"assessed", "skipped", "drafted"}:
            with self._connect() as connection:
                connection.execute("UPDATE applications SET verdict = ?, match_score = ?, updated_at = ? WHERE id = ?",
                                   (verdict, match_score, now, existing.id))
        return self.get(existing.id)

    def save_analysis(self, application_id: int, *, analysis_json: str, apply_score: int) -> TrackedApplication:
        with self._connect() as connection:
            connection.execute("UPDATE applications SET analysis_json = ?, apply_score = ? WHERE id = ?",
                               (analysis_json, apply_score, application_id))
        return self.get(application_id)

    def save_pack(self, application_id: int, *, pack_markdown: str, claim_status: str, pack_path: Path | None = None) -> TrackedApplication:
        current = self.get(application_id)
        if current.status not in {"assessed", "drafted", "approved"}:
            raise TransitionError(f"Cannot attach a new draft to an application in status '{current.status}'")
        with self._connect() as connection:
            connection.execute("UPDATE applications SET pack_markdown = ?, claim_status = ?, pack_path = ?, updated_at = ? WHERE id = ?",
                               (pack_markdown, claim_status, str(pack_path) if pack_path else None, self._now(), application_id))
        return self.transition(application_id, "drafted", actor="agent", details={"claim_status": claim_status})

    def transition(self, application_id: int, to_status: Status, *, actor: Actor, details: dict | None = None, note: str | None = None) -> TrackedApplication:
        current = self.get(application_id)
        if to_status not in TRANSITIONS[current.status]:
            raise TransitionError(f"Cannot move from '{current.status}' to '{to_status}'")
        if to_status in USER_ONLY and actor != "user":
            raise TransitionError(f"Only the user can set status '{to_status}'")
        if to_status == "approved" and current.claim_status == "blocked":
            raise TransitionError("The claim check blocked this pack; fix the unsupported claims and re-draft first")
        if to_status == "approved":
            self._accept_reviewed_pack(current)
        follow_up = (datetime.now(UTC) + FOLLOW_UP_AFTER).isoformat() if to_status == "applied" else None
        with self._connect() as connection:
            connection.execute(
                "UPDATE applications SET status = ?, follow_up_at = COALESCE(?, CASE WHEN ? IN ('rejected','offer','withdrawn') THEN NULL ELSE follow_up_at END), "
                "notes = CASE WHEN ? IS NULL THEN notes ELSE TRIM(notes || char(10) || ?) END, updated_at = ? WHERE id = ?",
                (to_status, follow_up, to_status, note, note, self._now(), application_id),
            )
            self._log(connection, application_id, actor, current.status, to_status, details or {})
        return self.get(application_id)

    def save_reviewed_text(self, application_id: int, text: str) -> TrackedApplication:
        """Store the user's edited pack. Editing an approved pack sends it back for re-approval."""
        current = self.get(application_id)
        if current.status not in {"drafted", "approved"}:
            raise TransitionError(f"Cannot edit the pack of an application in status '{current.status}'")
        previous = Path(current.pack_path).read_text(encoding="utf-8") if current.pack_path and Path(current.pack_path).exists() else current.pack_markdown
        if current.pack_path:
            Path(current.pack_path).write_text(text, encoding="utf-8")
        with self._connect() as connection:
            connection.execute("UPDATE applications SET pack_markdown = ?, updated_at = ? WHERE id = ?", (text, self._now(), application_id))
        if current.status == "approved" and text != previous:
            return self.transition(application_id, "drafted", actor="user", details={"reason": "edited after approval"})
        return self.get(application_id)

    def _accept_reviewed_pack(self, current: TrackedApplication) -> None:
        """Approval reads the pack the user edited; every [GAP] placeholder must be resolved."""
        if current.pack_path and Path(current.pack_path).exists():
            reviewed = Path(current.pack_path).read_text(encoding="utf-8")
            with self._connect() as connection:
                connection.execute("UPDATE applications SET pack_markdown = ? WHERE id = ?", (reviewed, current.id))
        else:
            reviewed = current.pack_markdown or ""
        if not reviewed:
            raise TransitionError("There is no application pack to approve")
        if "[GAP" in reviewed:
            where = current.pack_path or "the pack"
            raise TransitionError(f"Fill in or delete every [GAP ...] placeholder in {where} before approving")

    def get(self, application_id: int) -> TrackedApplication:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM applications WHERE id = ?", (application_id,)).fetchone()
        if row is None:
            raise KeyError(f"No application with id {application_id}")
        return TrackedApplication(**dict(row))

    def list(self, status: Status | None = None) -> list[TrackedApplication]:
        query, params = "SELECT * FROM applications", ()
        if status:
            query, params = query + " WHERE status = ?", (status,)
        with self._connect() as connection:
            rows = connection.execute(query + " ORDER BY updated_at DESC, id DESC", params).fetchall()
        return [TrackedApplication(**dict(row)) for row in rows]

    def history(self, application_id: int) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute("SELECT created_at, actor, from_status, to_status, details_json FROM application_events "
                                      "WHERE application_id = ? ORDER BY id", (application_id,)).fetchall()
        return [{**dict(row), "details": json.loads(row["details_json"])} for row in rows]

    def due_follow_ups(self, now: datetime | None = None) -> list[TrackedApplication]:
        moment = (now or datetime.now(UTC)).isoformat()
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM applications WHERE follow_up_at IS NOT NULL AND follow_up_at <= ? "
                                      "AND status IN ('applied', 'no_response') ORDER BY follow_up_at", (moment,)).fetchall()
        return [TrackedApplication(**dict(row)) for row in rows]

    def _log(self, connection: sqlite3.Connection, application_id: int, actor: str, from_status: str | None, to_status: str, details: dict) -> None:
        connection.execute("INSERT INTO application_events (application_id, created_at, actor, from_status, to_status, details_json) VALUES (?, ?, ?, ?, ?, ?)",
                           (application_id, self._now(), actor, from_status, to_status, json.dumps(details)))

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()
