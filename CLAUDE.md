# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'   # setup
.venv/bin/python -m pytest                                   # all tests
.venv/bin/python -m pytest tests/test_construction.py::test_inspection_gate_pass_fail_cycle   # one test
.venv/bin/pm-mcp                                             # run over stdio
.venv/bin/pm-mcp --transport streamable-http --port 8765     # run over HTTP (/mcp)
.venv/bin/pm-mcp --db /tmp/scratch.db                        # use a throwaway board
```

There is no linter or formatter configured.

## Purpose

An MCP server for AI agents to run construction projects through every phase
(pre-design → design → permitting → procurement → pre-construction →
construction → closeout → warranty). It must stay **app-agnostic**: don't
add code specific to any one consuming app (e.g. the VR construction project).
Integration goes through `external_refs`, `export_project`, free-form
`details`/`data` keys, and custom templates.

## Architecture

Layers, bottom up:

- `store.py`: generic agent work board. SQLite schema plus migrations;
  projects, milestones, tasks, dependencies, claims/leases, comments, activity.
  Also the gate check (`_check_gate`) because completion happens here.
- `records.py`: construction record types. Each `RecordType` is a pydantic
  data model, an ordered status list (the first is the default), the statuses
  that count as closed, and an optional `amount_field` mirrored into the indexed
  `amount` column.
- `templates.py`: the `Template` format (validated: unique keys, known phases,
  known dependencies, no cycles) and the four built-in templates.
- `schedule.py`: pure CPM (forward/backward pass) on a weekday calendar. No DB.
- `construction.py`: `Construction(store)` service. It applies templates, runs
  record CRUD with workflow hooks, and builds the budget, schedule, dashboard,
  external links and export. It reaches into `Store` internals (`_tx`,
  `_insert_*`, `_log`, `_finish_task`) on purpose, so multi-step operations
  share one transaction.
- `server.py`: thin MCP layer. Every tool forwards to `store()` or `cx()`.

### Phases are milestones, gates are task kinds

A template phase becomes a milestone. Task `kind` is `task`, `approval`,
`milestone`, `inspection` or `permit`. Inspection- and permit-kind tasks are
gates: each is linked to a record via `records.task_id`. `Store._check_gate`
refuses to finish a gate (via `complete_task` or `update_task`) unless its
linked record is `passed` (inspection) or `approved` (permit).
`Construction._after_status_change` does the reverse: when the record reaches
that status it finishes the task. A failed or partial inspection inserts a
correction task, tagged from the gate's predecessor tasks, and makes the gate
depend on it. New status-driven side effects belong in `_after_status_change`.

### Error handling

The store and service raise `PMError` for every refusal; pydantic
`ValidationError`s are converted to `PMError` with readable messages. In
`server.py`, tools are decorated `@mcp.tool()` on top of `@pm_errors` (that
order), which turns `PMError` into `ToolError`. Any other exception reaches the
client only as a generic `UnexpectedToolError` with the message hidden.

### MCP SDK version

This uses the `mcp` Python SDK **2.x**, where `FastMCP` was renamed `MCPServer`
(`from mcp.server.mcpserver import MCPServer`). Code written for v1
(`mcp.server.fastmcp`) won't import. The in-process test client is `mcp.Client(server.mcp)`.

### Concurrency and leases

Several agents each run their own server process against one SQLite file (WAL
mode). Every write goes through `Store._tx()` (`BEGIN IMMEDIATE`), so
`claim_next_task` can't hand the same task to two agents
(`test_concurrent_agents_never_share_a_task`). Keep new write paths inside
`_tx()`. Expired leases are reset lazily by `_expire_leases()`, which runs at the
start of claims, `list_tasks` and `project_report`; there is no background job.

### Schema changes

`SCHEMA` is version 1. Changes go in a new entry appended to `MIGRATIONS`
(tracked with `PRAGMA user_version`); never edit a shipped migration.
Migrations run statement by statement, split on `;`, so don't put semicolons
inside SQL strings. `test_upgrades_a_version_1_database` covers the path.

### Data conventions

- Priority is stored as an integer (1–4) so SQL can sort by it. `_task_dict()` converts it back to
  `low`/`medium`/`high`/`urgent`; return tasks through `_task_dict()`, projects through
  `_project_dict()` (parses `details`) and records through `_record_dict()` (parses `data`).
- `tags` (roles) is a JSON array, kept sorted and de-duplicated. Tag filtering happens in Python.
- "Ready" means `todo`, unassigned, in an `active` project, with every
  dependency in `FINISHED_STATUSES` (`done`, `cancelled`).
- In updates, `None` means "leave unchanged". To clear a field, pass
  `assignee=""`, `milestone_id=0` or `task_id=0`. In `details`/`data` merges, a key set
  to `None` is removed.
- Record and detail models use `extra="allow"`, so unknown keys are kept for consuming apps.
- Durations are working days (`ceil`'d; `None` counts as 1). Schedule day
  ranges are half-open `[early_start, early_finish)`.
- Timestamps are UTC ISO-8601 strings. Tests move time by monkeypatching
  `store._now` or `construction._today`.
- Every mutation writes a row to `activity` via `_log()`.

### Templates

Built-in templates are built from the `_t` / `_insp` / `_permit` helpers. Every
non-warranty task must be depended on by something downstream, so nothing
dangles off the path to handover;
`test_builtin_templates_cover_every_phase_and_finish_in_one_place` enforces it.
Inspection tasks need `inspection_type` and permit tasks need `permit_type`;
applying the template creates the linked records.

### Server store singleton

`server.store()` lazily opens one `Store` from `PM_MCP_DB` (default
`~/.local/share/pm-mcp/pm.db`; `--db` sets the env var). Tests set `PM_MCP_DB`
to a temp path and reset `server._store = None`; do the same in new
server-level tests.
