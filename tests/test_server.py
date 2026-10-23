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


def test_construction_workflow_over_mcp():
    async def run():
        async with Client(server.mcp) as c:
            templates = data(await c.call_tool("list_templates", {}))
            assert {"residential_new", "commercial_new"} <= {t["key"] for t in templates}

            created = data(await c.call_tool("create_construction_project", {
                "name": "Main St Retail", "template": "commercial_tenant_improvement",
                "start_date": "2027-01-04", "contract_value": 250000, "jurisdiction": "Riverside County",
                "extra_details": {"crm_deal": "D-77"}, "actor": "planner",
            }))
            pid = created["project"]["id"]
            assert created["project"]["details"]["crm_deal"] == "D-77"
            assert created["schedule"]["finish_date"] > "2027-01-04"

            # A permit agent works only its own role's tasks.
            await c.call_tool("update_project", {"project_id": pid, "details": {"crm_deal": None, "square_feet": 4200}})
            records = data(await c.call_tool("list_records", {"project_id": pid, "record_type": "permit"}))
            building = next(r for r in records if "TI building permit" in r["title"])
            done = data(await c.call_tool("update_record", {
                "record_id": building["id"], "status": "approved", "data": {"permit_number": "TI-9"},
            }))
            assert done["completed_task"] == building["task_id"]

            rfi = data(await c.call_tool("create_record", {
                "project_id": pid, "record_type": "rfi", "data": {"question": "Ceiling height at storefront?"},
            }))
            assert rfi["number"] == 1
            bad = await c.call_tool("create_record", {"project_id": pid, "record_type": "rfi", "data": {}})
            assert bad.is_error and "question" in bad.content[0].text

            dash = data(await c.call_tool("construction_dashboard", {"project_id": pid}))
            assert dash["project"]["details"]["square_feet"] == 4200
            assert "crm_deal" not in dash["project"]["details"]
            assert dash["open_items"]["rfi"] == 1

            sched = data(await c.call_tool("compute_schedule", {"project_id": pid, "mode": "plan"}))
            assert sched["critical_path"]
            types = data(await c.call_tool("describe_record_types", {}))
            assert {t["type"] for t in types} >= {"permit", "inspection", "change_order", "daily_log"}

            prompt = await c.get_prompt("construction_role_agent",
                                        {"agent": "est-1", "role": "estimator", "project_id": str(pid)})
            assert "tags=['estimator']" in prompt.messages[0].content.text

    anyio.run(run)
