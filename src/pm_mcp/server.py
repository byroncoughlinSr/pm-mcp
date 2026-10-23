"""MCP server exposing the project-management store to AI agents."""

from __future__ import annotations

import argparse
import functools
import os
from pathlib import Path
from typing import Any, Callable, Literal, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .construction import Construction, validate_details
from .records import describe_record_types as _describe_record_types
from .store import PMError, Store

DEFAULT_DB = Path.home() / ".local" / "share" / "pm-mcp" / "pm.db"

TaskStatus = Literal["todo", "in_progress", "blocked", "review", "done", "cancelled"]
TaskKind = Literal["task", "approval", "milestone", "inspection", "permit"]
ProjectStatus = Literal["active", "on_hold", "completed", "archived"]
Priority = Literal["low", "medium", "high", "urgent"]
RecordType = Literal[
    "permit", "inspection", "budget_item", "change_order", "rfi", "submittal", "punch_item",
    "daily_log", "contact", "document",
]
EntityType = Literal["project", "milestone", "task", "record"]

INSTRUCTIONS = """\
Shared construction project board for AI agents. Not tied to any one app:
other systems link their own ids with link_external and pull everything with
export_project.

Start a project: list_templates, then create_construction_project with a
template (residential_new, residential_remodel, commercial_new,
commercial_tenant_improvement, or a custom one from save_template). Each phase
(pre-design, design, permitting, procurement, pre-construction, construction,
closeout, warranty) becomes a milestone; tasks carry role tags and
dependencies; permits and inspections are records linked to gate tasks.

Worker loop: claim_next_task(agent=<name>, tags=[<your role>]) -> do the work
-> heartbeat while it runs -> complete_task(result=...), or release_task /
update_task(status="blocked") with a comment. Use records for the paperwork:
rfi for questions, submittal, change_order, daily_log, punch_item, document.

Gates: inspection- and permit-kind tasks can't be completed directly. Record
the outcome on the linked record (record_inspection_result, or update_record
status="approved" for permits) and the gate task completes itself. A failed
inspection creates a correction task the gate waits on.

Oversight: construction_dashboard, compute_schedule (critical path, forecast
vs target), budget_summary, project_report. Always pass the same agent/actor
name so ownership checks and the activity log work.
"""

mcp = MCPServer("project-management", instructions=INSTRUCTIONS, version="0.2.0")

_store: Store | None = None


def store() -> Store:
    global _store
    if _store is None:
        _store = Store(os.environ.get("PM_MCP_DB", str(DEFAULT_DB)))
    return _store


def cx() -> Construction:
    return Construction(store())


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
def create_project(
    name: str, description: str = "", details: dict | None = None, actor: str | None = None,
) -> dict:
    """Create an empty project (names are unique). For construction work prefer
    create_construction_project, which lays out every phase from a template.
    details holds project facts such as start_date, site_address, contract_value."""
    return store().create_project(name, description, actor, validate_details(details))


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
    details: dict | None = None,
    actor: str | None = None,
) -> dict:
    """Rename a project, edit its description, change its status, or update
    details (merged key by key; a key set to null is removed). Known detail
    keys: project_type, site_address, jurisdiction, client_name, start_date,
    target_completion, contract_value, square_feet, workdays_only. Other keys
    are kept as-is. Only active projects hand out work through claim_next_task."""
    if details:
        known = validate_details({k: v for k, v in details.items() if v is not None})
        details = {**{k: None for k, v in details.items() if v is None}, **known}
    return store().update_project(
        project_id, actor, name=name, description=description, status=status, details=details,
    )


@mcp.tool()
@pm_errors
def project_report(project_id: int) -> dict:
    """Status report: counts by status, percent complete, who is working on what,
    blocked and in-review tasks, the ready queue, milestones and recent activity."""
    return store().project_report(project_id)


# -- construction projects & templates --------------------------------------

@mcp.tool()
@pm_errors
def list_templates() -> list[dict]:
    """Construction plan templates (built-in and custom): phases, task count, roles."""
    return cx().list_templates()


@mcp.tool()
@pm_errors
def get_template(key: str) -> dict:
    """A template's full phase and task list, in the format save_template accepts."""
    return cx().get_template(key)


@mcp.tool()
@pm_errors
def save_template(template: dict, actor: str | None = None) -> dict:
    """Store a custom template (create or replace). Shape: {key, name, description,
    project_type, phases: [{key, name, description}], tasks: [{key, title, phase,
    roles, duration_days, depends_on: [task keys], description, priority, kind,
    inspection_type (inspection kind), permit_type (permit kind)}]}. Start from
    get_template on a built-in to copy its structure."""
    return cx().save_template(template, actor)


@mcp.tool()
@pm_errors
def delete_template(key: str, actor: str | None = None) -> dict:
    """Delete a custom template. Built-ins can't be deleted."""
    return cx().delete_template(key, actor)


@mcp.tool()
@pm_errors
def create_construction_project(
    name: str,
    template: str,
    description: str = "",
    start_date: str | None = None,
    target_completion: str | None = None,
    site_address: str | None = None,
    jurisdiction: str | None = None,
    client_name: str | None = None,
    contract_value: float | None = None,
    square_feet: float | None = None,
    workdays_only: bool = True,
    extra_details: dict | None = None,
    actor: str | None = None,
) -> dict:
    """Create a full construction project from a template: one milestone per
    phase, every task with role tags, durations and dependencies, and permit /
    inspection records linked to their gate tasks. With start_date (YYYY-MM-DD)
    the baseline schedule is computed and saved to tasks and phases.
    extra_details holds app-specific keys to keep on the project."""
    details = {
        **(extra_details or {}), "start_date": start_date, "target_completion": target_completion,
        "site_address": site_address, "jurisdiction": jurisdiction, "client_name": client_name,
        "contract_value": contract_value, "square_feet": square_feet, "workdays_only": workdays_only,
    }
    return cx().create_project_from_template(
        name, template, description, {k: v for k, v in details.items() if v is not None}, actor,
    )


@mcp.tool()
@pm_errors
def apply_template(project_id: int, template: str, actor: str | None = None) -> dict:
    """Add a template's phases and tasks (with gates, permit and inspection
    records) to an existing project. Phases that already exist by name are reused.
    If the project has details.start_date the baseline schedule is saved."""
    return cx().apply_template(project_id, template, actor)


@mcp.tool()
@pm_errors
def construction_dashboard(project_id: int) -> dict:
    """One-call project overview: current phase, progress per phase, forecast
    finish vs target and the critical path, permits, inspections (scheduled,
    failed), open RFIs/submittals/change orders/punch items, overdue records,
    tasks behind plan, ready work by role, and budget totals."""
    return cx().dashboard(project_id)


@mcp.tool()
@pm_errors
def compute_schedule(
    project_id: int,
    mode: Literal["plan", "forecast"] = "forecast",
    save: bool = False,
    start_date: str | None = None,
) -> dict:
    """Critical-path schedule from task durations (working days) and dependencies.
    plan = baseline from the start date; forecast = finished tasks at their
    actual completion date and unfinished work no earlier than today. Returns
    early/late dates, float, the critical path, phase spans and days past the
    target. save=true writes planned_start/planned_finish to tasks and phase due
    dates to milestones."""
    return cx().schedule(project_id, mode, save, start_date)


@mcp.tool()
@pm_errors
def budget_summary(project_id: int) -> dict:
    """Budget rollup from budget_item records by cost code and category (budgeted,
    committed, actual, forecast, variance), contingency remaining, and change
    orders (approved and pending) against the contract value."""
    return cx().budget_summary(project_id)


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
    """Add a milestone (phase) to a project. due_date is an ISO date (YYYY-MM-DD)."""
    return store().create_milestone(project_id, name, description, due_date, actor)


@mcp.tool()
@pm_errors
def list_milestones(project_id: int) -> list[dict]:
    """List a project's milestones (phases) with task and done counts."""
    return store().list_milestones(project_id)


@mcp.tool()
@pm_errors
def update_milestone(
    milestone_id: int,
    name: str | None = None,
    description: str | None = None,
    due_date: str | None = None,
    details: dict | None = None,
    actor: str | None = None,
) -> dict:
    """Edit a milestone (phase). due_date="" clears it. details holds app data
    and is merged key by key (null removes a key)."""
    return store().update_milestone(
        milestone_id, actor, name=name, description=description, due_date=due_date, details=details,
    )


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
    duration_days: float | None = None,
    kind: TaskKind = "task",
    budgeted_cost: float | None = None,
    actual_cost: float | None = None,
    assignee: str | None = None,
    actor: str | None = None,
) -> dict:
    """Create a task. depends_on lists task ids that must finish first; parent_id
    makes it a subtask; tags (roles) let agents claim only suitable work;
    duration_days (working days) feeds compute_schedule. kind inspection/permit
    makes it a gate: link a record to it with create_record(task_id=...)."""
    return store().create_task(
        project_id, title, description, priority, parent_id, milestone_id,
        depends_on, tags, estimate_hours, assignee, actor, kind, duration_days, budgeted_cost, actual_cost,
    )


@mcp.tool()
@pm_errors
def delete_task(task_id: int, actor: str | None = None) -> dict:
    """Delete a task with its subtasks, comments and dependency links. Records
    linked to it are kept, unlinked."""
    return store().delete_task(task_id, actor)


@mcp.tool()
@pm_errors
def get_task(task_id: int) -> dict:
    """Full task detail: dependencies, tasks it blocks, subtasks, comments and linked records."""
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
    duration_days: float | None = None,
    kind: TaskKind | None = None,
    budgeted_cost: float | None = None,
    actual_cost: float | None = None,
    planned_start: str | None = None,
    planned_finish: str | None = None,
    actor: str | None = None,
) -> dict:
    """Edit a task. Only the fields you pass change. assignee="" unassigns;
    milestone_id=0 removes the milestone; tags replaces the whole list. Changing
    kind to inspection/permit turns the task into a gate. planned_start /
    planned_finish (YYYY-MM-DD, "" clears) set dates by hand; compute_schedule
    with save=true overwrites them."""
    return store().update_task(
        task_id, actor, title=title, description=description, status=status, priority=priority,
        assignee=assignee, milestone_id=milestone_id, tags=tags, estimate_hours=estimate_hours,
        duration_days=duration_days, kind=kind, budgeted_cost=budgeted_cost, actual_cost=actual_cost,
        planned_start=planned_start, planned_finish=planned_finish,
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
    became ready because of it. Inspection/permit gate tasks complete through
    their records instead."""
    return store().complete_task(task_id, agent, result)


# -- construction records ---------------------------------------------------

@mcp.tool()
@pm_errors
def describe_record_types() -> list[dict]:
    """Every record type with its statuses, which statuses count as closed, and
    its data fields (JSON schema). Extra fields beyond these are kept."""
    return _describe_record_types()


@mcp.tool()
@pm_errors
def create_record(
    project_id: int,
    record_type: RecordType,
    data: dict | None = None,
    title: str | None = None,
    status: str | None = None,
    due_date: str | None = None,
    task_id: int | None = None,
    milestone_id: int | None = None,
    actor: str | None = None,
) -> dict:
    """Create a permit, inspection, budget_item, change_order, rfi, submittal,
    punch_item, daily_log, contact or document. data holds the type's fields
    (see describe_record_types); records are numbered per project and type
    (RFI #1, #2...). task_id links it to a task (for gates, the gate task)."""
    return cx().create_record(project_id, record_type, title, status, data, due_date, task_id, milestone_id, actor)


@mcp.tool()
@pm_errors
def update_record(
    record_id: int,
    status: str | None = None,
    data: dict | None = None,
    title: str | None = None,
    due_date: str | None = None,
    task_id: int | None = None,
    milestone_id: int | None = None,
    actor: str | None = None,
) -> dict:
    """Update a record. data is merged into the existing fields (null removes a
    field); task_id/milestone_id 0 unlinks. Status changes trigger workflow:
    permit approved or inspection passed completes the linked gate task;
    inspection failed/partial creates a correction task; change_order approved,
    rfi answered and submittal returned stamp their dates."""
    return cx().update_record(
        record_id, actor, status=status, data=data, title=title, due_date=due_date,
        task_id=task_id, milestone_id=milestone_id,
    )


@mcp.tool()
@pm_errors
def record_inspection_result(
    inspection_id: int,
    result: Literal["passed", "failed", "partial"],
    notes: str = "",
    inspector: str | None = None,
    actor: str | None = None,
) -> dict:
    """Log an inspection outcome (kept in the record's attempts history).
    passed completes the gate task; failed or partial creates a high-priority
    correction task, tagged for the trades that did the inspected work, that the
    gate waits on. Re-inspect by calling this again after the correction is done."""
    return cx().record_inspection_result(inspection_id, result, notes, inspector, actor)


@mcp.tool()
@pm_errors
def delete_record(record_id: int, actor: str | None = None) -> dict:
    """Delete a record. A gate task it was linked to stays and is no longer held by it."""
    return cx().delete_record(record_id, actor)


@mcp.tool()
@pm_errors
def get_record(record_id: int) -> dict:
    """One record with all its data."""
    return cx().get_record(record_id)


@mcp.tool()
@pm_errors
def list_records(
    project_id: int,
    record_type: RecordType | None = None,
    status: str | None = None,
    open_only: bool = False,
    milestone_id: int | None = None,
    task_id: int | None = None,
    limit: int = 200,
) -> list[dict]:
    """List a project's records, optionally by type, status, phase or task.
    open_only drops records in a closed status."""
    return cx().list_records(project_id, record_type, status, open_only, milestone_id, task_id, limit)


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


# -- integration with other apps --------------------------------------------

@mcp.tool()
@pm_errors
def link_external(
    entity_type: EntityType, entity_id: int, system: str, external_id: str, actor: str | None = None,
) -> dict:
    """Record that a project, milestone, task or record corresponds to an id in
    another system (e.g. system="procore", external_id="12345"). Idempotent."""
    return cx().link_external(entity_type, entity_id, system, external_id, actor)


@mcp.tool()
@pm_errors
def unlink_external(entity_type: EntityType, system: str, external_id: str, actor: str | None = None) -> dict:
    """Remove the link from an id in another system to our entity, e.g. to
    point it at a different project with link_external afterwards."""
    return cx().unlink_external(entity_type, system, external_id, actor)


@mcp.tool()
@pm_errors
def find_external(
    system: str, external_id: str | None = None, entity_type: EntityType | None = None,
    entity_id: int | None = None,
) -> list[dict]:
    """Look up links: by external id to find our entity, or by entity to find
    its ids in another system."""
    return cx().find_external(system, external_id, entity_type, entity_id)


@mcp.tool()
@pm_errors
def export_project(project_id: int) -> dict:
    """The whole project as one JSON document (project, phases, tasks with
    dependencies, records, comments, external links) for other apps to sync."""
    return cx().export_project(project_id)


# -- resources & prompts ----------------------------------------------------

@mcp.resource("pm://projects/{project_id}/report", mime_type="application/json")
def project_report_resource(project_id: int) -> dict:
    """The project_report for a project, as a readable resource."""
    try:
        return store().project_report(int(project_id))
    except PMError as exc:
        raise ToolError(str(exc)) from exc


@mcp.resource("pm://projects/{project_id}/dashboard", mime_type="application/json")
def construction_dashboard_resource(project_id: int) -> dict:
    """The construction_dashboard for a project, as a readable resource."""
    try:
        return cx().dashboard(int(project_id))
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
def plan_construction_project(description: str, project_name: str, start_date: str = "") -> str:
    """Turn a construction brief into a full phased project on the board."""
    start = f" Use start_date={start_date}." if start_date else " Ask for or estimate a start date."
    return (
        f"Set up the construction project {project_name!r}:\n\n{description}\n\n"
        "1. list_templates and pick the closest one (new build vs remodel, residential vs commercial).\n"
        f"2. create_construction_project with that template and every fact you have (site address, "
        f"jurisdiction, client, contract value, square feet, target completion).{start}\n"
        "3. Adjust the plan to the brief: update_task durations, cancel tasks that don't apply, and "
        "create_task (with milestone_id, tags, depends_on, duration_days) for scope the template lacks.\n"
        "4. Add create_record entries you can already fill in: contacts for the owner and design team, "
        "budget_item lines from any budget, permits beyond the template's.\n"
        "5. compute_schedule(save=true), then construction_dashboard. Report the phases, forecast finish, "
        "critical path, and which roles have work ready now."
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


@mcp.prompt()
def construction_role_agent(agent: str, role: str, project_id: int) -> str:
    """Instructions for an agent working one construction role (e.g. estimator, permit, superintendent)."""
    return (
        f"You are {agent!r}, the {role} on construction project {project_id}.\n"
        f"1. construction_dashboard(project_id={project_id}) to see the current phase and open items.\n"
        f"2. Loop: claim_next_task(agent={agent!r}, project_id={project_id}, tags=[{role!r}]) until it "
        "returns no task.\n"
        "3. For each task: get_task, read the results of the tasks it depends on, and do the work. "
        "Produce real output - an estimate, a schedule, a permit packet, a scope of work - and register "
        "files with create_record(record_type='document', data={doc_type, url}).\n"
        "4. Use records for paperwork: an rfi when design information is missing, a submittal for "
        "product approvals, a change_order for scope or cost changes, a daily_log for site days, "
        "punch_item for deficiencies, budget_item for cost lines.\n"
        "5. Inspection and permit tasks finish through their records: update the permit's status as it "
        "moves, and use record_inspection_result for inspections.\n"
        "6. complete_task with a concise result, or update_task(status='blocked') plus add_comment if you "
        "are waiting on something outside the board. heartbeat on long tasks."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Construction project-management MCP server for AI agents")
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
