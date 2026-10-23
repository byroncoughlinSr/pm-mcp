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

    # -- projects -----------------------------------------------------------

    def create_project(self, name: str, description: str = "", actor: str | None = None) -> dict:
        name = name.strip()
        if not name:
            raise PMError("project name is required")
        now = _ts(_now())
        with self._tx() as db:
            if db.execute("SELECT 1 FROM projects WHERE name = ?", (name,)).fetchone():
                raise PMError(f"a project named {name!r} already exists")
            cur = db.execute(
                "INSERT INTO projects (name, description, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (name, description, now, now),
            )
            self._log(db, "project_created", actor, cur.lastrowid, name=name)
            return dict(self._project_row(db, cur.lastrowid))

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
        return [{**dict(r), "done_count": r["done_count"] or 0} for r in rows]

    def update_project(self, project_id: int, actor: str | None = None, **fields) -> dict:
        allowed = {"name", "description", "status"}
        changes = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if "status" in changes and changes["status"] not in PROJECT_STATUSES:
            raise PMError(f"project status must be one of {', '.join(PROJECT_STATUSES)}")
        with self._tx() as db:
            self._project_row(db, project_id)
            if changes:
                sets = ", ".join(f"{k} = ?" for k in changes)
                db.execute(
                    f"UPDATE projects SET {sets}, updated_at = ? WHERE id = ?",
                    (*changes.values(), _ts(_now()), project_id),
                )
                self._log(db, "project_updated", actor, project_id, **changes)
            return dict(self._project_row(db, project_id))

    # -- milestones ---------------------------------------------------------

    def create_milestone(
        self, project_id: int, name: str, description: str = "", due_date: str | None = None,
        actor: str | None = None,
    ) -> dict:
        if due_date:
            try:
                datetime.fromisoformat(due_date)
            except ValueError:
                raise PMError("due_date must be an ISO date like 2026-11-30") from None
        with self._tx() as db:
            self._project_row(db, project_id)
            try:
                cur = db.execute(
                    "INSERT INTO milestones (project_id, name, description, due_date, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (project_id, name, description, due_date, _ts(_now())),
                )
            except sqlite3.IntegrityError:
                raise PMError(f"milestone {name!r} already exists in project {project_id}") from None
            self._log(db, "milestone_created", actor, project_id, milestone=name, due_date=due_date)
            return dict(self._milestone_row(db, cur.lastrowid))

    def list_milestones(self, project_id: int) -> list[dict]:
        with self._lock:
            self._project_row(self._db, project_id)
            rows = self._db.execute(
                "SELECT m.*, COUNT(t.id) AS task_count,"
                " SUM(CASE WHEN t.status = 'done' THEN 1 ELSE 0 END) AS done_count"
                " FROM milestones m LEFT JOIN tasks t ON t.milestone_id = m.id"
                " WHERE m.project_id = ? GROUP BY m.id ORDER BY m.due_date IS NULL, m.due_date, m.id",
                (project_id,),
            ).fetchall()
        return [{**dict(r), "done_count": r["done_count"] or 0} for r in rows]

    # -- tasks --------------------------------------------------------------

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
    ) -> dict:
        title = title.strip()
        if not title:
            raise PMError("task title is required")
        prio = _priority(priority)
        now = _ts(_now())
        with self._tx() as db:
            self._project_row(db, project_id)
            if parent_id is not None and self._task_row(db, parent_id)["project_id"] != project_id:
                raise PMError(f"parent task {parent_id} belongs to a different project")
            if milestone_id is not None and self._milestone_row(db, milestone_id)["project_id"] != project_id:
                raise PMError(f"milestone {milestone_id} belongs to a different project")
            cur = db.execute(
                "INSERT INTO tasks (project_id, milestone_id, parent_id, title, description, priority,"
                " assignee, tags, estimate_hours, created_by, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (project_id, milestone_id, parent_id, title, description, prio, assignee,
                 json.dumps(sorted(set(tags or []))), estimate_hours, actor, now, now),
            )
            task = self._task_row(db, cur.lastrowid)
            for dep in depends_on or []:
                self._add_dependency(db, task, dep)
            db.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now, project_id))
            self._log(db, "task_created", actor, project_id, task["id"], title=title)
            return self._task_dict(task)

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
                   "tags", "estimate_hours", "result"}
        changes = {k: v for k, v in fields.items() if k in allowed and v is not None}
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
        now = _ts(_now())
        with self._tx() as db:
            task = self._task_row(db, task_id)
            if changes.get("milestone_id") is not None:
                if self._milestone_row(db, changes["milestone_id"])["project_id"] != task["project_id"]:
                    raise PMError("milestone belongs to a different project")
            status = changes.get("status")
            if status == "done":
                changes["completed_at"] = now
            elif status and task["status"] == "done":
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
                    " updated_at = ? WHERE id = ?",
                    (agent, self._lease(lease_minutes), now, row["id"]),
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
                " updated_at = ? WHERE id = ?",
                (agent, self._lease(lease_minutes), _ts(_now()), task_id),
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

    def complete_task(self, task_id: int, agent: str, result: str = "") -> dict:
        """Mark a task done and report which tasks it unblocked."""
        with self._tx() as db:
            task = self._task_row(db, task_id)
            if task["assignee"] not in (None, agent):
                raise PMError(f"task {task_id} is assigned to {task['assignee']}, not {agent}")
            if task["status"] in FINISHED_STATUSES:
                raise PMError(f"task {task_id} is already {task['status']}")
            now = _ts(_now())
            db.execute(
                "UPDATE tasks SET status = 'done', assignee = ?, result = ?, lease_expires_at = NULL,"
                " completed_at = ?, updated_at = ? WHERE id = ?",
                (agent, result or None, now, now, task_id),
            )
            db.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now, task["project_id"]))
            self._log(db, "task_completed", agent, task["project_id"], task_id)
            unblocked = []
            for row in db.execute(
                "SELECT t.id, t.title FROM task_dependencies d JOIN tasks t ON t.id = d.task_id"
                " WHERE d.depends_on_id = ? AND t.status = 'todo'",
                (task_id,),
            ).fetchall():
                if not self._open_dependencies(db, row["id"]):
                    unblocked.append(dict(row))
            done = self._task_dict(self._task_row(db, task_id))
            done["unblocked"] = unblocked
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
            project = dict(self._project_row(db, project_id))
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
