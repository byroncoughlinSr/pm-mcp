"""SQLite-backed store for projects, milestones, tasks, comments and activity.

All writes run inside ``BEGIN IMMEDIATE`` transactions so several agents (each
with its own server process) can share one database file without two of them
claiming the same task.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

TASK_STATUSES = ("todo", "in_progress", "blocked", "review", "done", "cancelled")
FINISHED_STATUSES = ("done", "cancelled")
PROJECT_STATUSES = ("active", "on_hold", "completed", "archived")
PRIORITIES = {"low": 1, "medium": 2, "high": 3, "urgent": 4}
PRIORITY_NAMES = {v: k for k, v in PRIORITIES.items()}
# A task's kind says what finishing it means. Gate kinds can only be completed
# once their linked record (see construction.py) reaches GATE_PASSED_STATUS.
TASK_KINDS = ("task", "approval", "milestone", "inspection", "permit")
GATE_RECORD_TYPES = {"inspection": "inspection", "permit": "permit"}
GATE_PASSED_STATUS = {"inspection": "passed", "permit": "approved"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS milestones (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    due_date    TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE (project_id, name)
);
CREATE TABLE IF NOT EXISTS tasks (
    id               INTEGER PRIMARY KEY,
    project_id       INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    milestone_id     INTEGER REFERENCES milestones(id) ON DELETE SET NULL,
    parent_id        INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    title            TEXT NOT NULL,
    description      TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'todo',
    priority         INTEGER NOT NULL DEFAULT 2,
    assignee         TEXT,
    lease_expires_at TEXT,
    tags             TEXT NOT NULL DEFAULT '[]',
    estimate_hours   REAL,
    result           TEXT,
    created_by       TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    completed_at     TEXT
);
CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id       INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    depends_on_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, depends_on_id),
    CHECK (task_id <> depends_on_id)
);
CREATE TABLE IF NOT EXISTS comments (
    id         INTEGER PRIMARY KEY,
    task_id    INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    author     TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS activity (
    id         INTEGER PRIMARY KEY,
    project_id INTEGER,
    task_id    INTEGER,
    actor      TEXT,
    action     TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_project_status ON tasks (project_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks (assignee);
CREATE INDEX IF NOT EXISTS idx_deps_depends_on ON task_dependencies (depends_on_id);
CREATE INDEX IF NOT EXISTS idx_comments_task ON comments (task_id);
CREATE INDEX IF NOT EXISTS idx_activity_project ON activity (project_id, id);
"""

# MIGRATIONS[i] upgrades a database from user_version i+1 to i+2. SCHEMA above
# is version 1; never edit a shipped migration, append a new one.
MIGRATIONS = [
    # 2: construction support - project details, task scheduling, records,
    # links to other systems, and stored custom templates.
    """
    ALTER TABLE projects ADD COLUMN details TEXT NOT NULL DEFAULT '{}';
    ALTER TABLE tasks ADD COLUMN kind TEXT NOT NULL DEFAULT 'task';
    ALTER TABLE tasks ADD COLUMN duration_days REAL;
    ALTER TABLE tasks ADD COLUMN planned_start TEXT;
    ALTER TABLE tasks ADD COLUMN planned_finish TEXT;
    CREATE TABLE records (
        id           INTEGER PRIMARY KEY,
        project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        type         TEXT NOT NULL,
        number       INTEGER NOT NULL,
        title        TEXT NOT NULL,
        status       TEXT NOT NULL,
        milestone_id INTEGER REFERENCES milestones(id) ON DELETE SET NULL,
        task_id      INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
        due_date     TEXT,
        amount       REAL,
        data         TEXT NOT NULL DEFAULT '{}',
        created_by   TEXT,
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL,
        UNIQUE (project_id, type, number)
    );
    CREATE INDEX idx_records_project_type ON records (project_id, type, status);
    CREATE INDEX idx_records_task ON records (task_id);
    CREATE TABLE external_refs (
        id          INTEGER PRIMARY KEY,
        entity_type TEXT NOT NULL,
        entity_id   INTEGER NOT NULL,
        system      TEXT NOT NULL,
        external_id TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        UNIQUE (entity_type, system, external_id)
    );
    CREATE INDEX idx_external_refs_entity ON external_refs (entity_type, entity_id);
    CREATE TABLE templates (
        key        TEXT PRIMARY KEY,
        body       TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
    # 3: when work actually started, task-level cost, and app data on phases.
    """
    ALTER TABLE tasks ADD COLUMN started_at TEXT;
    ALTER TABLE tasks ADD COLUMN budgeted_cost REAL;
    ALTER TABLE tasks ADD COLUMN actual_cost REAL;
    ALTER TABLE milestones ADD COLUMN details TEXT NOT NULL DEFAULT '{}';
    """,
]


class PMError(ValueError):
    """A request the store refuses: unknown id, invalid value, conflicting claim."""


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _ts(dt: datetime) -> str:
    return dt.isoformat()


def _priority(value: str | int) -> int:
    if isinstance(value, int) and value in PRIORITY_NAMES:
        return value
    if isinstance(value, str) and value.lower() in PRIORITIES:
        return PRIORITIES[value.lower()]
    raise PMError(f"priority must be one of {', '.join(PRIORITIES)}")


class Store:
    def __init__(self, path: str | Path):
        path = str(path)
        if path != ":memory:":
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            path = str(Path(path).expanduser())
        self.path = path
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA busy_timeout = 10000")
        if path != ":memory:":
            self._db.execute("PRAGMA journal_mode = WAL")
        self._db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                version = max(1, self._db.execute("PRAGMA user_version").fetchone()[0])
                for target, script in enumerate(MIGRATIONS, start=2):
                    if version < target:
                        for statement in filter(str.strip, script.split(";")):
                            self._db.execute(statement)
                        version = target
                self._db.execute(f"PRAGMA user_version = {version}")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def close(self) -> None:
        self._db.close()

    # -- plumbing -----------------------------------------------------------

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def _log(self, db, action: str, actor: str | None, project_id=None, task_id=None, **detail) -> None:
        db.execute(
            "INSERT INTO activity (project_id, task_id, actor, action, detail, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (project_id, task_id, actor, action, json.dumps(detail, default=str), _ts(_now())),
        )

    def _project_row(self, db, project_id: int) -> sqlite3.Row:
        row = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if row is None:
            raise PMError(f"project {project_id} not found")
        return row

    def _task_row(self, db, task_id: int) -> sqlite3.Row:
        row = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise PMError(f"task {task_id} not found")
        return row

    def _milestone_row(self, db, milestone_id: int) -> sqlite3.Row:
        row = db.execute("SELECT * FROM milestones WHERE id = ?", (milestone_id,)).fetchone()
        if row is None:
            raise PMError(f"milestone {milestone_id} not found")
        return row

    @staticmethod
    def _project_dict(row: sqlite3.Row) -> dict[str, Any]:
        project = dict(row)
        project["details"] = json.loads(project.get("details") or "{}")
        return project

    @staticmethod
    def _milestone_dict(row: sqlite3.Row) -> dict[str, Any]:
        milestone = dict(row)
        milestone["details"] = json.loads(milestone.get("details") or "{}")
        return milestone

    @staticmethod
    def _task_dict(row: sqlite3.Row) -> dict[str, Any]:
        task = dict(row)
        task["priority"] = PRIORITY_NAMES.get(task["priority"], task["priority"])
        task["tags"] = json.loads(task["tags"] or "[]")
        return task

    def _open_dependencies(self, db, task_id: int) -> list[int]:
        rows = db.execute(
            "SELECT d.depends_on_id FROM task_dependencies d JOIN tasks t ON t.id = d.depends_on_id"
            f" WHERE d.task_id = ? AND t.status NOT IN {FINISHED_STATUSES}",
            (task_id,),
        ).fetchall()
        return [r[0] for r in rows]

    def _check_gate(self, db, task: sqlite3.Row) -> None:
        """Refuse to finish an inspection/permit task whose record hasn't passed."""
        record_type = GATE_RECORD_TYPES.get(task["kind"])
        if record_type is None:
            return
        passed = GATE_PASSED_STATUS[task["kind"]]
        pending = db.execute(
            "SELECT id, status FROM records WHERE task_id = ? AND type = ? AND status <> ?",
            (task["id"], record_type, passed),
        ).fetchall()
        if pending:
            raise PMError(
                f"task {task['id']} is gated by {record_type} record"
                f" {pending[0]['id']}, which is {pending[0]['status']}. Set the record to {passed!r}"
                f" (update_record or record_inspection_result) and the task completes itself."
            )

    def _expire_leases(self, db) -> None:
        now = _ts(_now())
        expired = db.execute(
            "SELECT id, project_id, assignee FROM tasks WHERE status = 'in_progress'"
            " AND lease_expires_at IS NOT NULL AND lease_expires_at < ?",
            (now,),
        ).fetchall()
        for row in expired:
            db.execute(
                "UPDATE tasks SET status = 'todo', assignee = NULL, lease_expires_at = NULL,"
                " updated_at = ? WHERE id = ?",
                (now, row["id"]),
            )
            self._log(db, "lease_expired", None, row["project_id"], row["id"], previous_assignee=row["assignee"])

    def _would_cycle(self, db, task_id: int, depends_on_id: int) -> bool:
        """True if ``task_id`` is already reachable from ``depends_on_id``'s dependencies."""
        seen, stack = set(), [depends_on_id]
        while stack:
            current = stack.pop()
            if current == task_id:
                return True
            if current in seen:
                continue
            seen.add(current)
            stack.extend(
                r[0] for r in db.execute(
                    "SELECT depends_on_id FROM task_dependencies WHERE task_id = ?", (current,)
                )
            )
        return False

    def _add_dependency(self, db, task: sqlite3.Row, depends_on_id: int) -> None:
        other = self._task_row(db, depends_on_id)
        if other["project_id"] != task["project_id"]:
            raise PMError(f"task {depends_on_id} belongs to a different project")
        if depends_on_id == task["id"]:
            raise PMError("a task cannot depend on itself")
        if self._would_cycle(db, task["id"], depends_on_id):
            raise PMError(f"task {task['id']} depending on {depends_on_id} would create a cycle")
        db.execute(
            "INSERT OR IGNORE INTO task_dependencies (task_id, depends_on_id) VALUES (?, ?)",
            (task["id"], depends_on_id),
        )

    def _finish_task(self, db, task: sqlite3.Row, actor: str | None, result: str | None = None) -> None:
        """Mark a task done inside an open transaction (used by gate hooks)."""
        now = _ts(_now())
        db.execute(
            "UPDATE tasks SET status = 'done', result = COALESCE(?, result), lease_expires_at = NULL,"
            " started_at = COALESCE(started_at, ?), completed_at = ?, updated_at = ? WHERE id = ?",
            (result, now, now, now, task["id"]),
        )
        self._log(db, "task_completed", actor, task["project_id"], task["id"], via="gate")

    # -- projects -----------------------------------------------------------

    def _insert_project(self, db, name: str, description: str, details: dict | None, actor: str | None) -> int:
        name = name.strip()
        if not name:
            raise PMError("project name is required")
        if db.execute("SELECT 1 FROM projects WHERE name = ?", (name,)).fetchone():
            raise PMError(f"a project named {name!r} already exists")
        now = _ts(_now())
        cur = db.execute(
            "INSERT INTO projects (name, description, details, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (name, description, json.dumps(details or {}), now, now),
        )
        self._log(db, "project_created", actor, cur.lastrowid, name=name)
        return cur.lastrowid

    def create_project(
        self, name: str, description: str = "", actor: str | None = None, details: dict | None = None,
    ) -> dict:
        with self._tx() as db:
            project_id = self._insert_project(db, name, description, details, actor)
            return self._project_dict(self._project_row(db, project_id))

    def get_project(self, project_id: int) -> dict:
        with self._lock:
            return self._project_dict(self._project_row(self._db, project_id))

    def list_projects(self, status: str | None = None) -> list[dict]:
        sql = (
            "SELECT p.*, COUNT(t.id) AS task_count,"
            " SUM(CASE WHEN t.status = 'done' THEN 1 ELSE 0 END) AS done_count"
            " FROM projects p LEFT JOIN tasks t ON t.project_id = p.id"
        )
        params: tuple = ()
        if status:
            sql += " WHERE p.status = ?"
            params = (status,)
        sql += " GROUP BY p.id ORDER BY p.updated_at DESC"
        with self._lock:
            rows = self._db.execute(sql, params).fetchall()
        return [{**self._project_dict(r), "done_count": r["done_count"] or 0} for r in rows]

    def update_project(self, project_id: int, actor: str | None = None, **fields) -> dict:
        """Edit name/description/status. ``details`` is merged key by key;
        a key set to None is removed."""
        allowed = {"name", "description", "status"}
        changes = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if "status" in changes and changes["status"] not in PROJECT_STATUSES:
            raise PMError(f"project status must be one of {', '.join(PROJECT_STATUSES)}")
        with self._tx() as db:
            current = self._project_dict(self._project_row(db, project_id))
            if fields.get("details"):
                merged = {**current["details"], **fields["details"]}
                changes["details"] = json.dumps({k: v for k, v in merged.items() if v is not None})
            if changes:
                sets = ", ".join(f"{k} = ?" for k in changes)
                db.execute(
                    f"UPDATE projects SET {sets}, updated_at = ? WHERE id = ?",
                    (*changes.values(), _ts(_now()), project_id),
                )
                logged = {k: v for k, v in changes.items() if k != "details"}
                if "details" in changes:
                    logged["details"] = fields["details"]
                self._log(db, "project_updated", actor, project_id, **logged)
            return self._project_dict(self._project_row(db, project_id))

    # -- milestones ---------------------------------------------------------

    def _insert_milestone(
        self, db, project_id: int, name: str, description: str, due_date: str | None, actor: str | None,
    ) -> int:
        if due_date:
            try:
                datetime.fromisoformat(due_date)
            except ValueError:
                raise PMError("due_date must be an ISO date like 2026-11-30") from None
        try:
            cur = db.execute(
                "INSERT INTO milestones (project_id, name, description, due_date, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (project_id, name, description, due_date, _ts(_now())),
            )
        except sqlite3.IntegrityError:
            raise PMError(f"milestone {name!r} already exists in project {project_id}") from None
        self._log(db, "milestone_created", actor, project_id, milestone=name, due_date=due_date)
        return cur.lastrowid

    def create_milestone(
        self, project_id: int, name: str, description: str = "", due_date: str | None = None,
        actor: str | None = None,
    ) -> dict:
        with self._tx() as db:
            self._project_row(db, project_id)
            milestone_id = self._insert_milestone(db, project_id, name, description, due_date, actor)
            return self._milestone_dict(self._milestone_row(db, milestone_id))

    def update_milestone(self, milestone_id: int, actor: str | None = None, **fields) -> dict:
        """Edit name/description/due_date (due_date="" clears it). ``details`` is
        merged key by key; a key set to None is removed."""
        changes = {k: fields[k] for k in ("name", "description") if fields.get(k) is not None}
        if "name" in changes and not changes["name"].strip():
            raise PMError("milestone name cannot be empty")
        if fields.get("due_date") == "":
            changes["due_date"] = None
        elif fields.get("due_date") is not None:
            try:
                changes["due_date"] = datetime.fromisoformat(fields["due_date"]).date().isoformat()
            except ValueError:
                raise PMError("due_date must be an ISO date like 2026-11-30") from None
        with self._tx() as db:
            current = self._milestone_dict(self._milestone_row(db, milestone_id))
            if fields.get("details"):
                merged = {**current["details"], **fields["details"]}
                changes["details"] = json.dumps({k: v for k, v in merged.items() if v is not None})
            if changes:
                sets = ", ".join(f"{k} = ?" for k in changes)
                try:
                    db.execute(f"UPDATE milestones SET {sets} WHERE id = ?", (*changes.values(), milestone_id))
                except sqlite3.IntegrityError:
                    raise PMError(f"milestone {changes.get('name')!r} already exists in this project") from None
                logged = {k: (fields["details"] if k == "details" else v) for k, v in changes.items()}
                self._log(db, "milestone_updated", actor, current["project_id"], milestone_id=milestone_id, **logged)
            return self._milestone_dict(self._milestone_row(db, milestone_id))

    def list_milestones(self, project_id: int) -> list[dict]:
        with self._lock:
            self._project_row(self._db, project_id)
            rows = self._db.execute(
                "SELECT m.*, COUNT(t.id) AS task_count,"
                " SUM(CASE WHEN t.status = 'done' THEN 1 ELSE 0 END) AS done_count"
                " FROM milestones m LEFT JOIN tasks t ON t.milestone_id = m.id"
                " WHERE m.project_id = ? GROUP BY m.id ORDER BY m.id",
                (project_id,),
            ).fetchall()
        return [{**self._milestone_dict(r), "done_count": r["done_count"] or 0} for r in rows]

    # -- tasks --------------------------------------------------------------

    def _insert_task(
        self, db, project_id: int, title: str, description: str = "", priority: str | int = "medium",
        parent_id: int | None = None, milestone_id: int | None = None, depends_on: list[int] | None = None,
        tags: list[str] | None = None, estimate_hours: float | None = None, assignee: str | None = None,
        actor: str | None = None, kind: str = "task", duration_days: float | None = None,
        budgeted_cost: float | None = None, actual_cost: float | None = None,
    ) -> sqlite3.Row:
        title = title.strip()
        if not title:
            raise PMError("task title is required")
        if kind not in TASK_KINDS:
            raise PMError(f"task kind must be one of {', '.join(TASK_KINDS)}")
        for name, value in (("duration_days", duration_days), ("budgeted_cost", budgeted_cost),
                            ("actual_cost", actual_cost)):
            if value is not None and value < 0:
                raise PMError(f"{name} cannot be negative")
        prio = _priority(priority)
        if parent_id is not None and self._task_row(db, parent_id)["project_id"] != project_id:
            raise PMError(f"parent task {parent_id} belongs to a different project")
        if milestone_id is not None and self._milestone_row(db, milestone_id)["project_id"] != project_id:
            raise PMError(f"milestone {milestone_id} belongs to a different project")
        now = _ts(_now())
        cur = db.execute(
            "INSERT INTO tasks (project_id, milestone_id, parent_id, title, description, priority,"
            " assignee, tags, estimate_hours, created_by, created_at, updated_at, kind, duration_days,"
            " budgeted_cost, actual_cost) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, milestone_id, parent_id, title, description, prio, assignee,
             json.dumps(sorted(set(tags or []))), estimate_hours, actor, now, now, kind, duration_days,
             budgeted_cost, actual_cost),
        )
        task = self._task_row(db, cur.lastrowid)
        for dep in depends_on or []:
            self._add_dependency(db, task, dep)
        db.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now, project_id))
        self._log(db, "task_created", actor, project_id, task["id"], title=title)
        return task

    def create_task(
        self,
        project_id: int,
        title: str,
        description: str = "",
        priority: str | int = "medium",
        parent_id: int | None = None,
        milestone_id: int | None = None,
        depends_on: list[int] | None = None,
        tags: list[str] | None = None,
        estimate_hours: float | None = None,
        assignee: str | None = None,
        actor: str | None = None,
        kind: str = "task",
        duration_days: float | None = None,
        budgeted_cost: float | None = None,
        actual_cost: float | None = None,
    ) -> dict:
        with self._tx() as db:
            self._project_row(db, project_id)
            task = self._insert_task(
                db, project_id, title, description, priority, parent_id, milestone_id, depends_on,
                tags, estimate_hours, assignee, actor, kind, duration_days, budgeted_cost, actual_cost,
            )
            return self._task_dict(task)

    def delete_task(self, task_id: int, actor: str | None = None) -> dict:
        """Delete a task with its subtasks, comments and dependency links. Records
        linked to it stay, unlinked."""
        with self._tx() as db:
            task = self._task_row(db, task_id)
            db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            db.execute("DELETE FROM external_refs WHERE entity_type = 'task' AND entity_id = ?", (task_id,))
            self._log(db, "task_deleted", actor, task["project_id"], None, deleted_task_id=task_id,
                      title=task["title"])
        return {"deleted": task_id}

    def get_task(self, task_id: int) -> dict:
        with self._lock:
            db = self._db
            task = self._task_dict(self._task_row(db, task_id))
            task["depends_on"] = [
                dict(r) for r in db.execute(
                    "SELECT t.id, t.title, t.status FROM task_dependencies d"
                    " JOIN tasks t ON t.id = d.depends_on_id WHERE d.task_id = ? ORDER BY t.id",
                    (task_id,),
                )
            ]
            task["blocks"] = [
                dict(r) for r in db.execute(
                    "SELECT t.id, t.title, t.status FROM task_dependencies d"
                    " JOIN tasks t ON t.id = d.task_id WHERE d.depends_on_id = ? ORDER BY t.id",
                    (task_id,),
                )
            ]
            task["subtasks"] = [
                dict(r) for r in db.execute(
                    "SELECT id, title, status, assignee FROM tasks WHERE parent_id = ? ORDER BY id",
                    (task_id,),
                )
            ]
            task["comments"] = [
                dict(r) for r in db.execute(
                    "SELECT id, author, body, created_at FROM comments WHERE task_id = ? ORDER BY id",
                    (task_id,),
                )
            ]
            task["records"] = [
                dict(r) for r in db.execute(
                    "SELECT id, type, number, title, status FROM records WHERE task_id = ? ORDER BY id",
                    (task_id,),
                )
            ]
            task["ready"] = task["status"] == "todo" and not any(
                d["status"] not in FINISHED_STATUSES for d in task["depends_on"]
            )
            return task

    def list_tasks(
        self,
        project_id: int | None = None,
        status: str | None = None,
        assignee: str | None = None,
        milestone_id: int | None = None,
        tag: str | None = None,
        ready_only: bool = False,
        include_finished: bool = True,
        limit: int = 100,
    ) -> list[dict]:
        where, params = [], []
        if project_id is not None:
            where.append("t.project_id = ?")
            params.append(project_id)
        if status:
            where.append("t.status = ?")
            params.append(status)
        elif not include_finished:
            where.append(f"t.status NOT IN {FINISHED_STATUSES}")
        if assignee:
            where.append("t.assignee = ?")
            params.append(assignee)
        if milestone_id is not None:
            where.append("t.milestone_id = ?")
            params.append(milestone_id)
        if ready_only:
            where.append(
                "t.status = 'todo' AND t.assignee IS NULL AND NOT EXISTS ("
                " SELECT 1 FROM task_dependencies d JOIN tasks dt ON dt.id = d.depends_on_id"
                f" WHERE d.task_id = t.id AND dt.status NOT IN {FINISHED_STATUSES})"
            )
        sql = "SELECT t.* FROM tasks t"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY t.priority DESC, t.created_at, t.id"
        with self._tx() as db:
            self._expire_leases(db)
            rows = db.execute(sql, params).fetchall()
        tasks = [self._task_dict(r) for r in rows]
        if tag:
            tasks = [t for t in tasks if tag in t["tags"]]
        return tasks[: max(1, limit)]

    def update_task(self, task_id: int, actor: str | None = None, **fields) -> dict:
        allowed = {"title", "description", "status", "priority", "assignee", "milestone_id",
                   "tags", "estimate_hours", "result", "duration_days", "kind", "budgeted_cost", "actual_cost",
                   "planned_start", "planned_finish"}
        changes = {k: v for k, v in fields.items() if k in allowed and v is not None}
        for key in ("planned_start", "planned_finish"):
            if changes.get(key) == "":
                changes[key] = None
            elif key in changes:
                try:
                    changes[key] = datetime.fromisoformat(changes[key]).date().isoformat()
                except ValueError:
                    raise PMError(f"{key} must be an ISO date like 2026-11-30") from None
        if "kind" in changes and changes["kind"] not in TASK_KINDS:
            raise PMError(f"task kind must be one of {', '.join(TASK_KINDS)}")
        for name in ("budgeted_cost", "actual_cost"):
            if changes.get(name, 0) < 0:
                raise PMError(f"{name} cannot be negative")
        # An empty assignee or a milestone_id of 0 clears the field.
        if fields.get("assignee") == "":
            changes["assignee"] = None
        if fields.get("milestone_id") == 0:
            changes["milestone_id"] = None
        if "status" in changes and changes["status"] not in TASK_STATUSES:
            raise PMError(f"task status must be one of {', '.join(TASK_STATUSES)}")
        if "priority" in changes:
            changes["priority"] = _priority(changes["priority"])
        if "tags" in changes:
            changes["tags"] = json.dumps(sorted(set(changes["tags"])))
        if changes.get("duration_days", 0) < 0:
            raise PMError("duration_days cannot be negative")
        now = _ts(_now())
        with self._tx() as db:
            task = self._task_row(db, task_id)
            if changes.get("milestone_id") is not None:
                if self._milestone_row(db, changes["milestone_id"])["project_id"] != task["project_id"]:
                    raise PMError("milestone belongs to a different project")
            status = changes.get("status")
            if status in ("in_progress", "review", "done") and not task["started_at"]:
                changes["started_at"] = now
            if status == "done" and task["status"] != "done":
                # Judge the gate by the kind the task will have after this update.
                self._check_gate(db, {**dict(task), "kind": changes.get("kind", task["kind"])})
                changes["completed_at"] = now
            elif status and status != "done" and task["status"] == "done":
                changes["completed_at"] = None
            if "assignee" in changes and changes["assignee"] != task["assignee"]:
                changes["lease_expires_at"] = None
            if status in ("todo", *FINISHED_STATUSES) and "lease_expires_at" not in changes:
                changes["lease_expires_at"] = None
            if not changes:
                return self._task_dict(task)
            sets = ", ".join(f"{k} = ?" for k in changes)
            db.execute(f"UPDATE tasks SET {sets}, updated_at = ? WHERE id = ?", (*changes.values(), now, task_id))
            db.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now, task["project_id"]))
            logged = {k: v for k, v in changes.items() if k not in ("completed_at", "lease_expires_at")}
            self._log(db, "task_updated", actor, task["project_id"], task_id, **logged)
            return self._task_dict(self._task_row(db, task_id))

    def add_dependency(self, task_id: int, depends_on_id: int, actor: str | None = None) -> dict:
        with self._tx() as db:
            task = self._task_row(db, task_id)
            self._add_dependency(db, task, depends_on_id)
            self._log(db, "dependency_added", actor, task["project_id"], task_id, depends_on=depends_on_id)
        return self.get_task(task_id)

    def remove_dependency(self, task_id: int, depends_on_id: int, actor: str | None = None) -> dict:
        with self._tx() as db:
            task = self._task_row(db, task_id)
            cur = db.execute(
                "DELETE FROM task_dependencies WHERE task_id = ? AND depends_on_id = ?",
                (task_id, depends_on_id),
            )
            if cur.rowcount == 0:
                raise PMError(f"task {task_id} does not depend on task {depends_on_id}")
            self._log(db, "dependency_removed", actor, task["project_id"], task_id, depends_on=depends_on_id)
        return self.get_task(task_id)

    # -- agent work queue ---------------------------------------------------

    def _lease(self, lease_minutes: int) -> str | None:
        return _ts(_now() + timedelta(minutes=lease_minutes)) if lease_minutes > 0 else None

    def claim_next_task(
        self, agent: str, project_id: int | None = None, tags: list[str] | None = None,
        lease_minutes: int = 30,
    ) -> dict | None:
        """Atomically assign the highest-priority ready task to ``agent``.

        A task is ready when it is ``todo``, unassigned, and every dependency is
        done or cancelled. Returns None when nothing is ready.
        """
        if not agent.strip():
            raise PMError("agent name is required")
        with self._tx() as db:
            self._expire_leases(db)
            sql = (
                "SELECT t.* FROM tasks t JOIN projects p ON p.id = t.project_id"
                " WHERE t.status = 'todo' AND t.assignee IS NULL AND p.status = 'active'"
                " AND NOT EXISTS (SELECT 1 FROM task_dependencies d JOIN tasks dt ON dt.id = d.depends_on_id"
                f"  WHERE d.task_id = t.id AND dt.status NOT IN {FINISHED_STATUSES})"
            )
            params: list = []
            if project_id is not None:
                sql += " AND t.project_id = ?"
                params.append(project_id)
            sql += " ORDER BY t.priority DESC, t.created_at, t.id"
            wanted = set(tags or [])
            for row in db.execute(sql, params).fetchall():
                if wanted and not wanted & set(json.loads(row["tags"])):
                    continue
                now = _ts(_now())
                db.execute(
                    "UPDATE tasks SET status = 'in_progress', assignee = ?, lease_expires_at = ?,"
                    " started_at = COALESCE(started_at, ?), updated_at = ? WHERE id = ?",
                    (agent, self._lease(lease_minutes), now, now, row["id"]),
                )
                self._log(db, "task_claimed", agent, row["project_id"], row["id"])
                return self._task_dict(self._task_row(db, row["id"]))
            return None

    def claim_task(self, task_id: int, agent: str, lease_minutes: int = 30) -> dict:
        if not agent.strip():
            raise PMError("agent name is required")
        with self._tx() as db:
            self._expire_leases(db)
            task = self._task_row(db, task_id)
            if task["assignee"] and task["assignee"] != agent:
                raise PMError(f"task {task_id} is already assigned to {task['assignee']}")
            if task["status"] in FINISHED_STATUSES:
                raise PMError(f"task {task_id} is already {task['status']}")
            if task["status"] == "blocked":
                raise PMError(f"task {task_id} is blocked; unblock it before claiming")
            open_deps = self._open_dependencies(db, task_id)
            if open_deps:
                raise PMError(f"task {task_id} is waiting on unfinished tasks {open_deps}")
            db.execute(
                "UPDATE tasks SET status = 'in_progress', assignee = ?, lease_expires_at = ?,"
                " started_at = COALESCE(started_at, ?), updated_at = ? WHERE id = ?",
                (agent, self._lease(lease_minutes), _ts(_now()), _ts(_now()), task_id),
            )
            self._log(db, "task_claimed", agent, task["project_id"], task_id)
            return self._task_dict(self._task_row(db, task_id))

    def _owned_task(self, db, task_id: int, agent: str) -> sqlite3.Row:
        task = self._task_row(db, task_id)
        if task["assignee"] != agent:
            owner = task["assignee"] or "nobody"
            raise PMError(f"task {task_id} is assigned to {owner}, not {agent}")
        return task

    def heartbeat(self, task_id: int, agent: str, lease_minutes: int = 30) -> dict:
        with self._tx() as db:
            task = self._owned_task(db, task_id, agent)
            if task["status"] != "in_progress":
                raise PMError(f"task {task_id} is {task['status']}, not in_progress")
            db.execute(
                "UPDATE tasks SET lease_expires_at = ?, updated_at = ? WHERE id = ?",
                (self._lease(lease_minutes), _ts(_now()), task_id),
            )
            return self._task_dict(self._task_row(db, task_id))

    def release_task(self, task_id: int, agent: str, reason: str = "") -> dict:
        with self._tx() as db:
            task = self._owned_task(db, task_id, agent)
            now = _ts(_now())
            db.execute(
                "UPDATE tasks SET status = 'todo', assignee = NULL, lease_expires_at = NULL,"
                " updated_at = ? WHERE id = ?",
                (now, task_id),
            )
            if reason:
                db.execute(
                    "INSERT INTO comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
                    (task_id, agent, f"Released: {reason}", now),
                )
            self._log(db, "task_released", agent, task["project_id"], task_id, reason=reason)
            return self._task_dict(self._task_row(db, task_id))

    def _newly_unblocked(self, db, task_id: int) -> list[dict]:
        unblocked = []
        for row in db.execute(
            "SELECT t.id, t.title FROM task_dependencies d JOIN tasks t ON t.id = d.task_id"
            " WHERE d.depends_on_id = ? AND t.status = 'todo'",
            (task_id,),
        ).fetchall():
            if not self._open_dependencies(db, row["id"]):
                unblocked.append(dict(row))
        return unblocked

    def complete_task(self, task_id: int, agent: str, result: str = "") -> dict:
        """Mark a task done and report which tasks it unblocked."""
        with self._tx() as db:
            task = self._task_row(db, task_id)
            if task["assignee"] not in (None, agent):
                raise PMError(f"task {task_id} is assigned to {task['assignee']}, not {agent}")
            if task["status"] in FINISHED_STATUSES:
                raise PMError(f"task {task_id} is already {task['status']}")
            self._check_gate(db, task)
            now = _ts(_now())
            db.execute(
                "UPDATE tasks SET status = 'done', assignee = ?, result = ?, lease_expires_at = NULL,"
                " started_at = COALESCE(started_at, ?), completed_at = ?, updated_at = ? WHERE id = ?",
                (agent, result or None, now, now, now, task_id),
            )
            db.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now, task["project_id"]))
            self._log(db, "task_completed", agent, task["project_id"], task_id)
            done = self._task_dict(self._task_row(db, task_id))
            done["unblocked"] = self._newly_unblocked(db, task_id)
            return done

    # -- comments & activity ------------------------------------------------

    def add_comment(self, task_id: int, author: str, body: str) -> dict:
        if not body.strip():
            raise PMError("comment body is required")
        with self._tx() as db:
            task = self._task_row(db, task_id)
            cur = db.execute(
                "INSERT INTO comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
                (task_id, author, body, _ts(_now())),
            )
            self._log(db, "comment_added", author, task["project_id"], task_id)
            return dict(db.execute("SELECT * FROM comments WHERE id = ?", (cur.lastrowid,)).fetchone())

    def get_activity(self, project_id: int | None = None, task_id: int | None = None, limit: int = 50) -> list[dict]:
        where, params = [], []
        if project_id is not None:
            where.append("project_id = ?")
            params.append(project_id)
        if task_id is not None:
            where.append("task_id = ?")
            params.append(task_id)
        sql = "SELECT * FROM activity"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        with self._lock:
            rows = self._db.execute(sql, (*params, max(1, limit))).fetchall()
        return [{**dict(r), "detail": json.loads(r["detail"])} for r in rows]

    # -- reporting ----------------------------------------------------------

    def project_report(self, project_id: int) -> dict:
        with self._tx() as db:
            self._expire_leases(db)
            project = self._project_dict(self._project_row(db, project_id))
        counts = {s: 0 for s in TASK_STATUSES}
        with self._lock:
            for row in self._db.execute(
                "SELECT status, COUNT(*) AS n FROM tasks WHERE project_id = ? GROUP BY status", (project_id,)
            ):
                counts[row["status"]] = row["n"]
        total = sum(counts.values())
        countable = total - counts["cancelled"]
        today = _now().date().isoformat()
        milestones = self.list_milestones(project_id)
        for m in milestones:
            m["overdue"] = bool(m["due_date"] and m["due_date"] < today and m["done_count"] < m["task_count"])
        brief = lambda t: {k: t[k] for k in ("id", "title", "priority", "assignee", "updated_at")}
        return {
            "project": project,
            "task_counts": counts,
            "total_tasks": total,
            "percent_complete": round(100 * counts["done"] / countable, 1) if countable else 0.0,
            "in_progress": [brief(t) for t in self.list_tasks(project_id, status="in_progress")],
            "blocked": [brief(t) for t in self.list_tasks(project_id, status="blocked")],
            "in_review": [brief(t) for t in self.list_tasks(project_id, status="review")],
            "ready_queue": [brief(t) for t in self.list_tasks(project_id, ready_only=True, limit=10)],
            "milestones": milestones,
            "recent_activity": self.get_activity(project_id, limit=15),
        }
