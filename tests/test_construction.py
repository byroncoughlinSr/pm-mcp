import sqlite3
from datetime import date

import pytest

from pm_mcp import construction as cons_mod
from pm_mcp.construction import Construction
from pm_mcp.schedule import Calendar, ScheduleTask, compute_schedule
from pm_mcp.store import SCHEMA, PMError, Store
from pm_mcp.templates import BUILTIN_TEMPLATES, Template


@pytest.fixture
def s(tmp_path):
    st = Store(tmp_path / "pm.db")
    yield st
    st.close()


@pytest.fixture
def c(s):
    return Construction(s)


@pytest.fixture
def house(c):
    return c.create_project_from_template(
        "Smith Residence", "residential_new",
        details={"start_date": "2026-11-02", "target_completion": "2027-09-30", "contract_value": 650000},
    )


def _record(c, project_id, rtype, title_part):
    return next(r for r in c.list_records(project_id, rtype) if title_part in r["title"])


# -- templates --------------------------------------------------------------

@pytest.mark.parametrize("key", sorted(BUILTIN_TEMPLATES))
def test_builtin_templates_cover_every_phase_and_finish_in_one_place(key):
    t = BUILTIN_TEMPLATES[key]
    used = {task.phase for task in t.tasks}
    assert used == {p.key for p in t.phases}
    # Every task feeds forward to a single project end (no dangling branches
    # except the warranty follow-ups that hang off handover).
    depended_on = {d for task in t.tasks for d in task.depends_on}
    ends = [task.key for task in t.tasks if task.key not in depended_on]
    assert all(t.tasks[[x.key for x in t.tasks].index(e)].phase == "warranty" for e in ends), ends


def test_template_validation_rejects_cycles_and_unknown_refs(c):
    base = {"key": "mine", "name": "Mine", "phases": [{"key": "p", "name": "P"}]}
    with pytest.raises(PMError, match="cycle"):
        c.save_template({**base, "tasks": [
            {"key": "a", "title": "A", "phase": "p", "depends_on": ["b"]},
            {"key": "b", "title": "B", "phase": "p", "depends_on": ["a"]},
        ]})
    with pytest.raises(PMError, match="unknown phase"):
        c.save_template({**base, "tasks": [{"key": "a", "title": "A", "phase": "nope"}]})
    with pytest.raises(PMError, match="inspection_type"):
        c.save_template({**base, "tasks": [{"key": "a", "title": "A", "phase": "p", "kind": "inspection"}]})
    with pytest.raises(PMError, match="built-in"):
        c.save_template({**BUILTIN_TEMPLATES["residential_new"].model_dump()})


def test_custom_template_round_trip(c):
    body = BUILTIN_TEMPLATES["residential_remodel"].model_dump()
    body.update(key="pool", name="Pool build")
    assert c.save_template(body)["tasks"] == len(body["tasks"])
    assert any(t["key"] == "pool" and t["source"] == "custom" for t in c.list_templates())
    project = c.create_project_from_template("Backyard pool", "pool")
    assert project["tasks_created"] == len(body["tasks"])
    c.delete_template("pool")
    with pytest.raises(PMError, match="not found"):
        c.create_project_from_template("Another", "pool")


# -- project creation -------------------------------------------------------

def test_project_from_template(s, c, house):
    t = BUILTIN_TEMPLATES["residential_new"]
    pid = house["project"]["id"]
    assert house["tasks_created"] == len(t.tasks)
    assert [p["name"] for p in house["phases"]] == [p.name for p in t.phases]
    assert house["project"]["details"]["project_type"] == "residential_new"
    assert house["schedule"]["start_date"] == "2026-11-02"

    inspections = c.list_records(pid, "inspection")
    permits = c.list_records(pid, "permit")
    assert len(inspections) == sum(x.kind == "inspection" for x in t.tasks)
    assert len(permits) == sum(x.kind == "permit" for x in t.tasks)
    assert all(r["task_id"] for r in inspections + permits)

    # Only the first task is ready; planned dates and phase due dates were saved.
    ready = s.list_tasks(pid, ready_only=True)
    assert [r["title"] for r in ready] == ["Owner needs & program interview"]
    assert ready[0]["planned_start"] == "2026-11-02"
    assert all(m["due_date"] for m in s.list_milestones(pid))


def test_template_creation_is_atomic(c, house):
    with pytest.raises(PMError, match="already exists"):
        c.create_project_from_template("Smith Residence", "residential_remodel")
    assert len(c.store.list_projects()) == 1


# -- gates ------------------------------------------------------------------

def test_inspection_gate_pass_fail_cycle(s, c, house):
    pid = house["project"]["id"]
    insp = _record(c, pid, "inspection", "Foundation")
    gate = insp["task_id"]
    with pytest.raises(PMError, match="gated by inspection record"):
        s.complete_task(gate, "inspector-agent")
    with pytest.raises(PMError, match="gated"):
        s.update_task(gate, status="done")

    failed = c.record_inspection_result(insp["id"], "failed", "Missing hold-downs", "J. Ortiz")
    fix = s.get_task(failed["correction_task"])
    assert fix["tags"] == ["concrete"] and fix["priority"] == "high"
    assert gate in [b["id"] for b in fix["blocks"]]
    assert "Missing hold-downs" in fix["description"]

    s.complete_task(fix["id"], "concrete-agent")
    passed = c.record_inspection_result(insp["id"], "passed", inspector="J. Ortiz")
    assert passed["completed_task"] == gate
    assert s.get_task(gate)["status"] == "done"
    assert [a["result"] for a in c.get_record(insp["id"])["data"]["attempts"]] == ["failed", "passed"]


def test_permit_approval_completes_gate(s, c, house):
    pid = house["project"]["id"]
    permit = _record(c, pid, "permit", "Building permit")
    c.update_record(permit["id"], status="applied", data={"authority": "City of Murrieta"})
    assert s.get_task(permit["task_id"])["status"] == "todo"
    approved = c.update_record(permit["id"], status="approved", data={"permit_number": "B26-0142"})
    assert approved["completed_task"] == permit["task_id"]
    assert approved["data"]["approved_date"] and approved["data"]["authority"] == "City of Murrieta"


# -- records ----------------------------------------------------------------

def test_record_validation_numbering_and_extra_fields(c, house):
    pid = house["project"]["id"]
    with pytest.raises(PMError, match="question"):
        c.create_record(pid, "rfi", data={})
    with pytest.raises(PMError, match="status must be"):
        c.create_record(pid, "rfi", status="pending", data={"question": "?"})
    one = c.create_record(pid, "rfi", data={"question": "Header size at garage door?", "app_ref": "X-9"})
    two = c.create_record(pid, "rfi", data={"question": "Tile layout in primary bath?"})
    assert (one["number"], two["number"]) == (1, 2)
    assert one["title"].startswith("RFI #1") and one["data"]["app_ref"] == "X-9"
    answered = c.update_record(one["id"], status="answered", data={"answer": "4x12 LVL"})
    assert answered["data"]["answered_date"]
    assert [r["id"] for r in c.list_records(pid, "rfi", open_only=True)] == [one["id"], two["id"]]
    c.update_record(one["id"], status="closed")
    assert [r["id"] for r in c.list_records(pid, "rfi", open_only=True)] == [two["id"]]


def test_budget_summary(c, house):
    pid = house["project"]["id"]
    c.create_record(pid, "budget_item", data={"cost_code": "03 - Concrete", "category": "subcontract",
                                              "budgeted": 48000, "committed": 50500})
    c.create_record(pid, "budget_item", data={"cost_code": "06 - Wood", "category": "material",
                                              "budgeted": 90000, "committed": 40000, "actual": 42000})
    c.create_record(pid, "budget_item", data={"cost_code": "01 - General", "category": "contingency",
                                              "budgeted": 30000, "actual": 5000})
    co = c.create_record(pid, "change_order", data={"cost_impact": 12000, "schedule_impact_days": 5})
    c.create_record(pid, "change_order", status="submitted", data={"cost_impact": 3000})
    c.update_record(co["id"], status="approved")
    b = c.budget_summary(pid)
    assert b["totals"] == {"budgeted": 168000.0, "committed": 90500.0, "actual": 47000.0,
                           "forecast": 97500.0, "variance": 70500.0}
    assert b["by_cost_code"]["03 - Concrete"]["variance"] == -2500.0
    assert b["contingency"]["remaining"] == 25000.0
    assert b["change_orders"]["approved_cost"] == 12000.0 and b["change_orders"]["pending_cost"] == 3000.0
    assert b["revised_contract_value"] == 662000.0


# -- schedule ---------------------------------------------------------------

def test_calendar_skips_weekends():
    cal = Calendar(date(2026, 10, 31))  # a Saturday
    assert cal.start == date(2026, 11, 2)  # rolls to Monday
    assert cal.to_date(4) == date(2026, 11, 6)
    assert cal.to_date(5) == date(2026, 11, 9)
    assert cal.to_offset(date(2026, 11, 9)) == 5
    assert cal.to_offset(date(2026, 11, 7)) == 5  # Saturday counts as the next workday


def test_critical_path():
    tasks = [
        ScheduleTask(1, "Dig", 3),
        ScheduleTask(2, "Pour", 2, [1]),
        ScheduleTask(3, "Order windows", 1, [1]),
        ScheduleTask(4, "Frame", 5, [2, 3]),
    ]
    r = compute_schedule(tasks, date(2026, 11, 2))
    by_id = {t["id"]: t for t in r["tasks"]}
    assert [t["id"] for t in r["critical_path"]] == [1, 2, 4]
    assert by_id[3]["float_days"] == 1 and not by_id[3]["critical"]
    assert r["duration_days"] == 10
    assert r["finish_date"] == "2026-11-13"


def test_forecast_moves_unstarted_work_to_today(c, house, monkeypatch):
    pid = house["project"]["id"]
    plan = c.schedule(pid, mode="plan")
    monkeypatch.setattr(cons_mod, "_today", lambda: date(2027, 3, 1))
    forecast = c.schedule(pid, mode="forecast")
    assert forecast["finish_date"] > plan["finish_date"]
    assert forecast["days_past_target"] > plan["days_past_target"]


def test_schedule_needs_start_date(c):
    c.create_project_from_template("No date", "commercial_tenant_improvement")
    pid = c.store.list_projects()[0]["id"]
    with pytest.raises(PMError, match="no start date"):
        c.schedule(pid)
    assert c.schedule(pid, start_date="2027-01-04")["start_date"] == "2027-01-04"


# -- dashboard & integration ------------------------------------------------

def test_dashboard(c, house):
    pid = house["project"]["id"]
    c.create_record(pid, "rfi", data={"question": "?"}, due_date="2000-01-01")
    d = c.dashboard(pid)
    assert d["current_phase"] == "1. Pre-design & feasibility"
    assert d["phases"][0]["status"] == "not_started"
    assert d["open_items"]["rfi"] == 1 and "budget_item" not in d["open_items"]
    assert d["overdue_records"][0]["type"] == "rfi"
    assert d["ready_tasks_by_role"] == {"architect": 1, "project_manager": 1}
    assert d["schedule"]["target_completion"] == "2027-09-30"


def test_external_links_and_export(c, house):
    pid = house["project"]["id"]
    c.link_external("project", pid, "vr-construction", "42")
    c.link_external("project", pid, "vr-construction", "42")  # idempotent
    with pytest.raises(PMError, match="already linked"):
        other = c.store.create_project("Other")
        c.link_external("project", other["id"], "vr-construction", "42")
    assert c.find_external("vr-construction", "42")[0]["entity_id"] == pid
    c.unlink_external("project", "vr-construction", "42")  # move it to the other project and back
    c.link_external("project", other["id"], "vr-construction", "42")
    assert c.find_external("vr-construction", "42")[0]["entity_id"] == other["id"]
    assert c.unlink_external("project", "vr-construction", "42")["entity_id"] == other["id"]
    with pytest.raises(PMError, match="not linked"):
        c.unlink_external("project", "vr-construction", "42")
    c.link_external("project", pid, "vr-construction", "42")
    export = c.export_project(pid)
    assert export["format"] == "pm-mcp/1"
    assert len(export["tasks"]) == house["tasks_created"]
    assert export["external_refs"] == [{"entity_type": "project", "entity_id": pid,
                                        "system": "vr-construction", "external_id": "42"}]
    assert any(t["depends_on"] for t in export["tasks"])


def test_upgrades_a_version_1_database(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.execute("INSERT INTO projects (name, created_at, updated_at) VALUES ('Legacy', 'x', 'x')")
    db.commit()
    db.close()
    st = Store(path)
    assert st.list_projects()[0]["details"] == {}
    assert st._db.execute("PRAGMA user_version").fetchone()[0] == 3
    Construction(st).create_record(1, "contact", data={"role": "owner", "name": "Pat"})
    st.close()
    assert Store(path).list_projects()[0]["name"] == "Legacy"  # reopening doesn't re-migrate


# -- additions for apps that manage the plan themselves ----------------------

def test_started_at_task_costs_and_kind_switch(s, c):
    p = s.create_project("Shed")
    t = s.create_task(p["id"], "Pour pad", budgeted_cost=1200, actual_cost=0)
    assert t["started_at"] is None and t["budgeted_cost"] == 1200
    started = s.update_task(t["id"], status="in_progress")
    assert started["started_at"]
    s.update_task(t["id"], status="todo")
    assert s.get_task(t["id"])["started_at"] == started["started_at"]  # first start is kept

    # Turning a task into an inspection gate makes its record hold completion.
    s.update_task(t["id"], kind="inspection")
    insp = c.create_record(p["id"], "inspection", data={"inspection_type": "slab"}, task_id=t["id"])
    with pytest.raises(PMError, match="gated"):
        s.update_task(t["id"], status="done")
    assert s.update_task(t["id"], status="done", kind="task")["status"] == "done"  # switching back frees it
    assert c.get_record(insp["id"])["status"] == "not_scheduled"
    with pytest.raises(PMError, match="kind must be"):
        s.update_task(t["id"], kind="chore")
    assert c.budget_summary(p["id"])["task_costs"] == {"budgeted": 1200.0, "actual": 0.0}


def test_update_milestone(s):
    p = s.create_project("Shed")
    m = s.create_milestone(p["id"], "Site prep")
    s.create_milestone(p["id"], "Framing")
    u = s.update_milestone(m["id"], name="Site work", due_date="2026-12-01", details={"progress": 40, "x": 1})
    assert (u["name"], u["due_date"], u["details"]) == ("Site work", "2026-12-01", {"progress": 40, "x": 1})
    u = s.update_milestone(m["id"], due_date="", details={"x": None})
    assert u["due_date"] is None and u["details"] == {"progress": 40}
    with pytest.raises(PMError, match="already exists"):
        s.update_milestone(m["id"], name="Framing")
    assert s.list_milestones(p["id"])[0]["details"] == {"progress": 40}


def test_delete_task_and_record(s, c):
    p = s.create_project("Shed")
    a = s.create_task(p["id"], "A")
    b = s.create_task(p["id"], "B", depends_on=[a["id"]])
    rfi = c.create_record(p["id"], "rfi", data={"question": "?"}, task_id=a["id"])
    c.link_external("task", a["id"], "app", "T-1")
    s.delete_task(a["id"])
    assert s.get_task(b["id"])["depends_on"] == []
    assert c.get_record(rfi["id"])["task_id"] is None
    assert c.find_external("app", "T-1") == []
    c.delete_record(rfi["id"])
    with pytest.raises(PMError, match="not found"):
        c.get_record(rfi["id"])


def test_apply_template_to_existing_project(s, c):
    p = s.create_project("Kitchen", details={"start_date": "2027-02-01"})
    s.create_milestone(p["id"], "2. Design")  # reused by name
    result = c.apply_template(p["id"], "residential_remodel")
    assert result["tasks_created"] == len(BUILTIN_TEMPLATES["residential_remodel"].tasks)
    assert len(s.list_milestones(p["id"])) == 8
    assert result["schedule"]["start_date"] == "2027-02-01"


def test_planned_dates_by_hand(s):
    p = s.create_project("Shed")
    t = s.create_task(p["id"], "Frame")
    u = s.update_task(t["id"], planned_start="2027-03-01", planned_finish="2027-03-05")
    assert (u["planned_start"], u["planned_finish"]) == ("2027-03-01", "2027-03-05")
    assert s.update_task(t["id"], planned_finish="")["planned_finish"] is None
    with pytest.raises(PMError, match="ISO date"):
        s.update_task(t["id"], planned_start="March")
