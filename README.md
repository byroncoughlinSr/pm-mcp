# pm-mcp

A Model Context Protocol server for running construction projects with AI
agents, from the owner's first brief through design, permitting, bidding,
construction, closeout and warranty. It isn't tied to any one app: other
construction systems can link their own ids to its projects, tasks and
records and pull a full export.

Under the construction layer is a general agent work board: projects,
phases, tasks with dependencies, and a work queue that many agents can pull
from at once without two of them taking the same task.

Data lives in one SQLite file (default `~/.local/share/pm-mcp/pm.db`, override
with `PM_MCP_DB` or `--db`). Every agent can run its own server process against
the same file.

## Setup

```bash
cd /development/pm-mcp
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

## Running

```bash
.venv/bin/pm-mcp                                   # stdio (what MCP clients launch)
.venv/bin/pm-mcp --transport streamable-http --port 8765   # http://127.0.0.1:8765/mcp
.venv/bin/pm-mcp --db /path/to/team.db             # use a different board
```

### Claude Code

```bash
claude mcp add pm -- /development/pm-mcp/.venv/bin/pm-mcp
```

### OpenWork / opencode

Add to the `mcp` block of `opencode.jsonc`:

```jsonc
"pm": {
  "type": "local",
  "command": ["/development/pm-mcp/.venv/bin/pm-mcp"]
}
```

## Construction projects

`create_construction_project(name, template, start_date, ...)` builds the whole
plan in one call. Built-in templates:

| Template | For |
|---|---|
| `residential_new` | Single-family home, ground up |
| `residential_remodel` | Kitchen, bath, whole-house remodel or addition |
| `commercial_new` | Ground-up commercial building, CM/GC delivery |
| `commercial_tenant_improvement` | Interior build-out of leased space |

Each template uses the same eight phases, created as milestones:

1. Pre-design & feasibility
2. Design (schematic → design development → construction documents)
3. Permitting
4. Bidding & procurement
5. Pre-construction
6. Construction
7. Closeout
8. Warranty & post-occupancy

Tasks carry working-day durations, dependencies, and **role tags** (`architect`,
`estimator`, `permit`, `superintendent`, `concrete`, `framing`, `plumbing`,
`electrical`, `hvac`, `inspection`, ...). A specialist agent calls
`claim_next_task(agent=..., tags=["estimator"])` and only gets estimating work
that is ready to start.

Apps can store their own plans with `save_template` (start from
`get_template("residential_new")` to copy the format). `apply_template` lays a
template into a project that already exists (phases with the same name are
reused).

### Gates: permits and inspections

Permit and inspection tasks are **gates**. They can't be completed directly;
they finish when their linked record does:

- `update_record(permit_id, status="approved")` completes the permit task.
- `record_inspection_result(inspection_id, "passed")` completes the inspection
  task. `"failed"` or `"partial"` creates a high-priority correction task,
  tagged for the trades whose work was inspected, and the gate waits on it.
  Call `record_inspection_result` again after the fix. Every attempt is kept.

So the next phase of work can't start until the inspection actually passes.

### Records

`create_record` / `update_record` / `list_records` handle the project
paperwork. `describe_record_types` returns each type's fields and statuses.

| Type | Use |
|---|---|
| `permit` | Permits from the jurisdiction |
| `inspection` | Required inspections, with result history |
| `budget_item` | Cost lines: budgeted, committed, actual |
| `change_order` | Scope, cost and schedule changes |
| `rfi` | Questions to the design team or owner |
| `submittal` | Shop drawings, product data, samples for review |
| `punch_item` | Deficiencies to fix before handover |
| `daily_log` | Site reports: weather, crew, work, delays |
| `contact` | Owner, design team, subs, suppliers, inspectors |
| `document` | Drawings, specs, estimates, contracts (by URL or path) |

Records are numbered per project (RFI #1, #2...). Unknown fields in `data` are
kept, so an app can attach its own.

### Oversight

- `construction_dashboard`: current phase, progress per phase, forecast finish vs
  target, critical path, permits, inspections, open RFIs/submittals/change
  orders/punch items, overdue items, tasks behind plan, ready work by role,
  budget totals. Also readable as `pm://projects/{id}/dashboard`.
- `compute_schedule`: critical-path schedule in working days. `plan` is the
  baseline; `forecast` uses actual completion dates and pushes unstarted work
  to today. `save=true` writes planned dates to tasks and phase due dates.
- `budget_summary`: totals by cost code and category, contingency left,
  approved and pending change orders against the contract value.

### Connecting other apps

The VR construction project (`/development/vr-ai-construction-project`) is one
such app: its backend keeps each project's plan here and talks to pm-mcp over
HTTP (`--transport streamable-http --port 8765`).

- `link_external(entity_type, entity_id, system, external_id)` records that a
  project, phase, task or record matches an id in another system.
  `find_external` looks it up either way.
- `export_project` returns the whole project as one JSON document.
- `extra_details` / `details` on projects and extra `data` fields on records
  hold app-specific values.

## Agent prompts

- `plan_construction_project`: turn a brief into a phased project.
- `construction_role_agent`: work one role's tasks and keep the paperwork.
- `plan_project` / `work_session`: the same for non-construction work.

## Generic tools

| Area | Tools |
|---|---|
| Projects | `create_project`, `list_projects`, `update_project`, `project_report` |
| Phases | `create_milestone`, `list_milestones`, `update_milestone` |
| Tasks | `create_task`, `get_task`, `list_tasks`, `update_task`, `delete_task`, `add_dependency`, `remove_dependency` |
| Work queue | `claim_next_task`, `claim_task`, `heartbeat`, `release_task`, `complete_task` |
| Notes & history | `add_comment`, `get_activity` |

Tasks also carry `started_at` (set the first time work starts), optional
`budgeted_cost` / `actual_cost` (reported apart from budget lines in
`budget_summary.task_costs`), and `planned_start` / `planned_finish`, which
`compute_schedule(save=true)` writes and `update_task` can set by hand. Phases
(milestones) have a free-form `details` object for app data. Records can be
removed with `delete_record`.

A claim lasts `lease_minutes` (default 30). If the agent stops sending
`heartbeat`, the task goes back to the queue. Only the assigned agent can
heartbeat, release or complete a task; dependencies must be in the same
project and can't form cycles.

## Layout

```
src/pm_mcp/store.py         SQLite schema, migrations, tasks/claims/leases, gate check
src/pm_mcp/construction.py  Templates applied to projects, records + workflow hooks,
                            budget, dashboard, external links, export
src/pm_mcp/templates.py     Built-in phase/task templates and the template format
src/pm_mcp/records.py       Record types: fields, statuses, validation
src/pm_mcp/schedule.py      Critical-path scheduling on a working-day calendar
src/pm_mcp/server.py        MCP tools, resources, prompts, CLI
```

Built on the `mcp` Python SDK 2.x (`MCPServer`).
