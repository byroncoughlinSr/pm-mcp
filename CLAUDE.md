# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'   # setup
.venv/bin/python -m pytest                                   # all tests
.venv/bin/python -m pytest tests/test_store.py::test_dependency_cycles_rejected   # one test
.venv/bin/pm-mcp                                             # run over stdio
.venv/bin/pm-mcp --transport streamable-http --port 8765     # run over HTTP (/mcp)
.venv/bin/pm-mcp --db /tmp/scratch.db                        # use a throwaway board
```

There is no linter or formatter configured.

## Architecture

A project-management board for AI agents, exposed as an MCP server. Two layers:

- `src/pm_mcp/store.py` holds the SQLite schema and **all** business rules
  (validation, ownership checks, dependency cycle detection, leases, reporting).
  It has no MCP imports and is tested directly.
- `src/pm_mcp/server.py` is a thin MCP layer: each tool forwards to one `Store`
  method. It also defines the `pm://projects/{project_id}/report` resource, the
  `plan_project` / `work_session` prompts, and the CLI (`main`).

### MCP SDK version

This uses the `mcp` Python SDK **2.x**, where `FastMCP` was renamed `MCPServer`
(`from mcp.server.mcpserver import MCPServer`). Examples written for v1
(`mcp.server.fastmcp`) won't import. The in-process test client is `mcp.Client(server.mcp)`.

### Error handling

The store raises `PMError` for every refusal. In `server.py`, tools are decorated
`@mcp.tool()` on top of `@pm_errors` (that order), which turns `PMError` into
`ToolError` so the agent sees the message. Any other exception reaches the
client only as a generic `UnexpectedToolError` with the message hidden. So new
validation should raise `PMError`, and new tools need `@pm_errors`.

### Concurrency model

Several agents each run their own server process against one SQLite file (WAL
mode). Every write goes through `Store._tx()`, which uses `BEGIN IMMEDIATE`, so
the select-then-update in `claim_next_task` can't hand the same task to two
agents. `test_concurrent_agents_never_share_a_task` covers this; keep new
write paths inside `_tx()`.

Leases: claimed tasks get a `lease_expires_at`. There is no background job.
Expired leases are reset to `todo` lazily by `_expire_leases()`, which runs at
the start of `claim_next_task`, `claim_task`, `list_tasks` and `project_report`.

### Data conventions

- Priority is stored as an integer (1–4) so SQL can sort by it. `_task_dict()` converts it back to
  `low`/`medium`/`high`/`urgent`; always return tasks through `_task_dict()`.
- `tags` is a JSON array, kept sorted and de-duplicated. Tag filtering happens in
  Python after the query, not in SQL.
- "Ready" means `todo`, unassigned, in an `active` project, with every
  dependency in `FINISHED_STATUSES` (`done`, `cancelled`).
- In `update_task`, `None` means "leave unchanged". To clear a field, pass
  `assignee=""` or `milestone_id=0`.
- Timestamps are UTC ISO-8601 strings, compared as strings. Tests move time
  forward by monkeypatching `store._now`.
- Every mutation writes a row to `activity` via `_log()`.

### Server store singleton

`server.store()` lazily opens one `Store` from `PM_MCP_DB` (default
`~/.local/share/pm-mcp/pm.db`; `--db` sets the env var). Tests set `PM_MCP_DB`
to a temp path and reset `server._store = None`. Do the same in any new
server-level test so it doesn't touch the real board.
