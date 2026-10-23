"""End-to-end: drive the server through a real MCP client session."""

import json

import anyio
import pytest
from mcp import Client

from pm_mcp import server


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("PM_MCP_DB", str(tmp_path / "pm.db"))
    monkeypatch.setattr(server, "_store", None)
    yield
    if server._store is not None:
        server._store.close()
        server._store = None


def data(result):
    assert not result.is_error, result.content
    if result.structured_content is not None:
        sc = result.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(result.content[0].text)


def test_agent_workflow_over_mcp():
    async def run():
        async with Client(server.mcp) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            assert {"claim_next_task", "complete_task", "project_report"} <= names

            p = data(await c.call_tool("create_project", {"name": "Demo", "actor": "planner"}))
            a = data(await c.call_tool("create_task", {"project_id": p["id"], "title": "Spec", "priority": "high"}))
            b = data(await c.call_tool(
                "create_task", {"project_id": p["id"], "title": "Build", "depends_on": [a["id"]]}
            ))

            claim = data(await c.call_tool("claim_next_task", {"agent": "worker-1"}))
            assert claim["task"]["id"] == a["id"]
            assert data(await c.call_tool("claim_next_task", {"agent": "worker-2"}))["task"] is None

            await c.call_tool("add_comment", {"task_id": a["id"], "author": "worker-1", "body": "Draft ready"})
            done = data(await c.call_tool(
                "complete_task", {"task_id": a["id"], "agent": "worker-1", "result": "spec.md"}
            ))
            assert done["unblocked"][0]["id"] == b["id"]

            report = data(await c.call_tool("project_report", {"project_id": p["id"]}))
            assert report["task_counts"]["done"] == 1
            assert report["ready_queue"][0]["id"] == b["id"]

            # Store refusals surface as readable tool errors, not crashes.
            err = await c.call_tool("complete_task", {"task_id": a["id"], "agent": "worker-1"})
            assert err.is_error and "already done" in err.content[0].text

            prompt = await c.get_prompt("work_session", {"agent": "worker-1"})
            assert "claim_next_task" in prompt.messages[0].content.text

    anyio.run(run)
