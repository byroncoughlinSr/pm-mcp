"""MCP server exposing the project-management store to AI agents."""

from __future__ import annotations

import argparse
import functools
import os
from pathlib import Path
from typing import Any, Callable, Literal, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .store import PMError, Store

DEFAULT_DB = Path.home() / ".local" / "share" / "pm-mcp" / "pm.db"

TaskStatus = Literal["todo", "in_progress", "blocked", "review", "done", "cancelled"]
ProjectStatus = Literal["active", "on_hold", "completed", "archived"]
Priority = Literal["low", "medium", "high", "urgent"]

INSTRUCTIONS = """\
Shared project board for AI agents. Typical loop for a worker agent:
  1. claim_next_task(agent=<your name>) to take the highest-priority ready task.
  2. heartbeat(...) at least every lease_minutes while working, or the task
     returns to the queue for another agent.
  3. add_comment(...) for progress notes, decisions, or questions.
  4. complete_task(..., result=<summary>) when done, or release_task(...) if
     you cannot finish it. Use update_task(status="blocked") plus a comment when
     waiting on something outside the board.
Planner agents create projects, milestones and tasks, and use depends_on to
order work: a task is only claimable once all its dependencies are done.
Always pass the same agent name so ownership checks and the activity log work.
"""

mcp = MCPServer("project-management", instructions=INSTRUCTIONS, version="0.1.0")

_store: Store | None = None


def store() -> Store:
    global _store
    if _store is None:
        _store = Store(os.environ.get("PM_MCP_DB", str(DEFAULT_DB)))
    return _store


F = TypeVar("F", bound=Callable[..., Any])


def pm_errors(fn: F) -> F:
    """Report store refusals to the agent as readable tool errors."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except PMError as exc:
            raise ToolError(str(exc)) from exc

    return wrapper  # type: ignore[return-value]


# -- projects ---------------------------------------------------------------

@mcp.tool()
@pm_errors
def create_project(name: str, description: str = "", actor: str | None = None) -> dict:
    """Create a project. Names are unique."""
    return store().create_project(name, description, actor)


@mcp.tool()
@pm_errors
def list_projects(status: ProjectStatus | None = None) -> list[dict]:
    """List projects with task and done counts, most recently active first."""
    return store().list_projects(status)


@mcp.tool()
@pm_errors
def update_project(
    project_id: int,
    name: str | None = None,
    description: str | None = None,
    status: ProjectStatus | None = None,
    actor: str | None = None,
) -> dict:
    """Rename a project, edit its description, or change its status.

    Only active projects hand out work through claim_next_task.
    """
    return store().update_project(project_id, actor, name=name, description=description, status=status)


@mcp.tool()
@pm_errors
def project_report(project_id: int) -> dict:
    """Status report: counts by status, percent complete, who is working on what,
    blocked and in-review tasks, the ready queue, milestones and recent activity."""
    return store().project_report(project_id)


# -- milestones -------------------------------------------------------------

@mcp.tool()
@pm_errors
def create_milestone(
    project_id: int,
    name: str,
    description: str = "",
    due_date: str | None = None,
    actor: str | None = None,
) -> dict:
    """Add a milestone to a project. due_date is an ISO date (YYYY-MM-DD)."""
    return store().create_milestone(project_id, name, description, due_date, actor)


@mcp.tool()
@pm_errors
def list_milestones(project_id: int) -> list[dict]:
    """List a project's milestones with task and done counts."""
    return store().list_milestones(project_id)


# -- tasks ------------------------------------------------------------------

@mcp.tool()
@pm_errors
def create_task(
    project_id: int,
    title: str,
    description: str = "",
    priority: Priority = "medium",
    parent_id: int | None = None,
    milestone_id: int | None = None,
    depends_on: list[int] | None = None,
    tags: list[str] | None = None,
    estimate_hours: float | None = None,
    assignee: str | None = None,
    actor: str | None = None,
) -> dict:
    """Create a task. depends_on lists task ids that must finish first; parent_id
    makes it a subtask. Tags let agents claim only work they are suited for."""
    return store().create_task(
        project_id, title, description, priority, parent_id, milestone_id,
        depends_on, tags, estimate_hours, assignee, actor,
    )


@mcp.tool()
@pm_errors
def get_task(task_id: int) -> dict:
    """Full task detail: dependencies, tasks it blocks, subtasks and comments."""
    return store().get_task(task_id)


@mcp.tool()
@pm_errors
def list_tasks(
    project_id: int | None = None,
    status: TaskStatus | None = None,
    assignee: str | None = None,
    milestone_id: int | None = None,
    tag: str | None = None,
    ready_only: bool = False,
    include_finished: bool = True,
    limit: int = 100,
) -> list[dict]:
    """List tasks, highest priority first. ready_only returns unassigned todo
    tasks whose dependencies are all finished."""
    return store().list_tasks(
        project_id, status, assignee, milestone_id, tag, ready_only, include_finished, limit,
    )


@mcp.tool()
@pm_errors
def update_task(
    task_id: int,
    title: str | None = None,
    description: str | None = None,
    status: TaskStatus | None = None,
    priority: Priority | None = None,
    assignee: str | None = None,
    milestone_id: int | None = None,
    tags: list[str] | None = None,
    estimate_hours: float | None = None,
    actor: str | None = None,
) -> dict:
    """Edit a task. Only the fields you pass change. assignee="" unassigns;
    milestone_id=0 removes the milestone; tags replaces the whole list."""
    return store().update_task(
        task_id, actor, title=title, description=description, status=status, priority=priority,
        assignee=assignee, milestone_id=milestone_id, tags=tags, estimate_hours=estimate_hours,
    )


@mcp.tool()
@pm_errors
def add_dependency(task_id: int, depends_on_id: int, actor: str | None = None) -> dict:
    """Make task_id wait for depends_on_id. Cycles are rejected."""
    return store().add_dependency(task_id, depends_on_id, actor)


@mcp.tool()
@pm_errors
def remove_dependency(task_id: int, depends_on_id: int, actor: str | None = None) -> dict:
    """Remove a dependency between two tasks."""
    return store().remove_dependency(task_id, depends_on_id, actor)


# -- agent work queue -------------------------------------------------------

@mcp.tool()
@pm_errors
def claim_next_task(
    agent: str,
    project_id: int | None = None,
    tags: list[str] | None = None,
    lease_minutes: int = 30,
) -> dict:
    """Atomically take the highest-priority ready task. Optionally limit to one
    project or to tasks carrying any of the given tags. The claim expires after
    lease_minutes without a heartbeat (0 = never). Returns {"task": null} when
    nothing is ready."""
    return {"task": store().claim_next_task(agent, project_id, tags, lease_minutes)}


@mcp.tool()
@pm_errors
def claim_task(task_id: int, agent: str, lease_minutes: int = 30) -> dict:
    """Claim a specific task. Fails if another agent holds it or its
    dependencies are unfinished."""
    return store().claim_task(task_id, agent, lease_minutes)


@mcp.tool()
@pm_errors
def heartbeat(task_id: int, agent: str, lease_minutes: int = 30) -> dict:
    """Extend your lease on an in-progress task."""
    return store().heartbeat(task_id, agent, lease_minutes)


@mcp.tool()
@pm_errors
def release_task(task_id: int, agent: str, reason: str = "") -> dict:
    """Give a task back to the queue. The reason is saved as a comment."""
    return store().release_task(task_id, agent, reason)


@mcp.tool()
@pm_errors
def complete_task(task_id: int, agent: str, result: str = "") -> dict:
    """Mark a task done with a result summary. The response lists tasks that
    became ready because of it."""
    return store().complete_task(task_id, agent, result)


# -- comments & activity ----------------------------------------------------

@mcp.tool()
@pm_errors
def add_comment(task_id: int, author: str, body: str) -> dict:
    """Attach a progress note, decision, or question to a task."""
    return store().add_comment(task_id, author, body)


@mcp.tool()
@pm_errors
def get_activity(project_id: int | None = None, task_id: int | None = None, limit: int = 50) -> list[dict]:
    """Recent activity log entries, newest first."""
    return store().get_activity(project_id, task_id, limit)


# -- resources & prompts ----------------------------------------------------

@mcp.resource("pm://projects/{project_id}/report", mime_type="application/json")
def project_report_resource(project_id: int) -> dict:
    """The project_report for a project, as a readable resource."""
    try:
        return store().project_report(int(project_id))
    except PMError as exc:
        raise ToolError(str(exc)) from exc


@mcp.prompt()
def plan_project(goal: str, project_name: str) -> str:
    """Break a goal into a project plan on the board."""
    return (
        f"Plan the project {project_name!r} for this goal:\n\n{goal}\n\n"
        "1. Call create_project (or list_projects to reuse an existing one).\n"
        "2. Create 2-5 milestones that mark meaningful checkpoints.\n"
        "3. Create tasks small enough for one agent to finish in one session. Give each a "
        "clear title, a description with acceptance criteria, a priority, and tags for the "
        "skill it needs (e.g. backend, frontend, research, docs, test).\n"
        "4. Use depends_on so tasks only become claimable when their inputs exist.\n"
        "5. Finish with project_report and summarize the plan and the first ready tasks."
    )


@mcp.prompt()
def work_session(agent: str, project_id: int | None = None, tags: str = "") -> str:
    """Instructions for a worker agent to pull and finish tasks."""
    scope = f" in project {project_id}" if project_id is not None else ""
    tag_note = f" with tags [{tags}]" if tags else ""
    return (
        f"You are worker agent {agent!r}. Repeat until claim_next_task returns no task{scope}:\n"
        f"- claim_next_task(agent={agent!r}"
        + (f", project_id={project_id}" if project_id is not None else "")
        + (f", tags={[t.strip() for t in tags.split(',') if t.strip()]}" if tags else "")
        + f"){tag_note}.\n"
        "- Read the task with get_task, including its dependencies' results.\n"
        "- Do the work. Call heartbeat if it runs long, and add_comment for notable decisions.\n"
        "- complete_task with a concise result, or release_task / update_task(status='blocked') "
        "with a comment explaining why you stopped."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Project-management MCP server for AI agents")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", help=f"SQLite file (default: $PM_MCP_DB or {DEFAULT_DB})")
    args = parser.parse_args()
    if args.db:
        os.environ["PM_MCP_DB"] = args.db
    if args.transport == "stdio":
        mcp.run("stdio")
    else:
        mcp.run("streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
