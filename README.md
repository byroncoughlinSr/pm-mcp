# pm-mcp

A Model Context Protocol server that gives AI agents a shared project board:
projects, milestones, tasks with dependencies, and a work queue that several
agents can pull from at once without stepping on each other.

Data lives in one SQLite file (default `~/.local/share/pm-mcp/pm.db`, override
with `PM_MCP_DB` or `--db`). Every agent can run its own server process against
the same file; task claims are atomic.

## Setup

```bash
cd /development/pm-mcp
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest        # 14 tests
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

## How agents use it

**Planner agents** build the board:
`create_project` → `create_milestone` → `create_task` (with `depends_on`,
`parent_id`, `tags`, `priority`). The `plan_project` prompt walks a model
through this.

**Worker agents** loop (the `work_session` prompt spells it out):

1. `claim_next_task(agent="worker-1", tags=["backend"])` takes the
   highest-priority task that is `todo`, unassigned, in an active project, and
   has all dependencies done or cancelled.
2. `heartbeat` while working. A claim lasts `lease_minutes` (default 30); if it
   lapses, the task goes back to the queue so a crashed agent can't hold work
   forever. `lease_minutes=0` means no expiry.
3. `add_comment` for notes, decisions, questions.
4. `complete_task(result=...)` (the reply lists tasks that just became ready),
   or `release_task(reason=...)`, or `update_task(status="blocked")` plus a comment.

**Anyone** can check progress with `project_report` (also readable as the
resource `pm://projects/{id}/report`), `list_tasks`, `get_task` and
`get_activity`.

## Tools

| Area | Tools |
|---|---|
| Projects | `create_project`, `list_projects`, `update_project`, `project_report` |
| Milestones | `create_milestone`, `list_milestones` |
| Tasks | `create_task`, `get_task`, `list_tasks`, `update_task`, `add_dependency`, `remove_dependency` |
| Work queue | `claim_next_task`, `claim_task`, `heartbeat`, `release_task`, `complete_task` |
| Notes & history | `add_comment`, `get_activity` |

Task statuses: `todo`, `in_progress`, `blocked`, `review`, `done`, `cancelled`.
Priorities: `low`, `medium`, `high`, `urgent`. Project statuses: `active`,
`on_hold`, `completed`, `archived` (only `active` projects hand out work).

Rules the server enforces: unique project names; dependencies and parents must
be in the same project; dependency cycles are rejected; only the assigned agent
can heartbeat, release or complete a task; a task can't be claimed while its
dependencies are unfinished or another agent holds it.

## Layout

```
src/pm_mcp/store.py    SQLite schema and all business rules
src/pm_mcp/server.py   MCP tools, resource, prompts, CLI entry point
tests/                 store unit tests + end-to-end test through an MCP client
```

Built on the `mcp` Python SDK 2.x (`MCPServer`).
