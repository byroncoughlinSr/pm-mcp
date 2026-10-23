import threading
from datetime import timedelta

import pytest

from pm_mcp import store as store_mod
from pm_mcp.store import PMError, Store


@pytest.fixture
def s(tmp_path):
    st = Store(tmp_path / "pm.db")
    yield st
    st.close()


@pytest.fixture
def project(s):
    return s.create_project("Website", "Relaunch", actor="planner")


def test_project_names_are_unique(s, project):
    with pytest.raises(PMError, match="already exists"):
        s.create_project("Website")


def test_claim_respects_priority_and_dependencies(s, project):
    pid = project["id"]
    design = s.create_task(pid, "Design", priority="medium")
    build = s.create_task(pid, "Build", priority="urgent", depends_on=[design["id"]])
    docs = s.create_task(pid, "Docs", priority="low")

    # Build is urgent but waits on Design, so Design (medium) goes first.
    first = s.claim_next_task("agent-a", pid)
    assert first["id"] == design["id"]
    assert first["status"] == "in_progress" and first["assignee"] == "agent-a"

    second = s.claim_next_task("agent-b", pid)
    assert second["id"] == docs["id"]
    assert s.claim_next_task("agent-c", pid) is None

    done = s.complete_task(design["id"], "agent-a", result="Figma link")
    assert done["status"] == "done" and done["completed_at"]
    assert [t["id"] for t in done["unblocked"]] == [build["id"]]
    assert s.claim_next_task("agent-c", pid)["id"] == build["id"]


def test_tag_filter(s, project):
    pid = project["id"]
    s.create_task(pid, "API", tags=["backend"])
    ui = s.create_task(pid, "UI", tags=["frontend"], priority="low")
    assert s.claim_next_task("fe-agent", pid, tags=["frontend"])["id"] == ui["id"]
    assert s.claim_next_task("fe-agent", pid, tags=["frontend"]) is None


def test_ownership_checks(s, project):
    t = s.create_task(project["id"], "Task")
    s.claim_task(t["id"], "agent-a")
    with pytest.raises(PMError, match="already assigned to agent-a"):
        s.claim_task(t["id"], "agent-b")
    with pytest.raises(PMError, match="not agent-b"):
        s.complete_task(t["id"], "agent-b")
    with pytest.raises(PMError, match="not agent-b"):
        s.release_task(t["id"], "agent-b")
    released = s.release_task(t["id"], "agent-a", reason="need credentials")
    assert released["status"] == "todo" and released["assignee"] is None
    assert s.get_task(t["id"])["comments"][0]["body"] == "Released: need credentials"


def test_claim_task_refuses_unfinished_dependencies(s, project):
    a = s.create_task(project["id"], "A")
    b = s.create_task(project["id"], "B", depends_on=[a["id"]])
    with pytest.raises(PMError, match="waiting on unfinished"):
        s.claim_task(b["id"], "agent")


def test_dependency_cycles_rejected(s, project):
    pid = project["id"]
    a = s.create_task(pid, "A")
    b = s.create_task(pid, "B", depends_on=[a["id"]])
    c = s.create_task(pid, "C", depends_on=[b["id"]])
    with pytest.raises(PMError, match="cycle"):
        s.add_dependency(a["id"], c["id"])
    with pytest.raises(PMError, match="itself"):
        s.add_dependency(a["id"], a["id"])


def test_cross_project_links_rejected(s, project):
    other = s.create_project("Other")
    foreign = s.create_task(other["id"], "Foreign")
    with pytest.raises(PMError, match="different project"):
        s.create_task(project["id"], "Local", depends_on=[foreign["id"]])
    with pytest.raises(PMError, match="different project"):
        s.create_task(project["id"], "Local", parent_id=foreign["id"])


def test_expired_lease_returns_task_to_queue(s, project, monkeypatch):
    t = s.create_task(project["id"], "Long job")
    s.claim_task(t["id"], "agent-a", lease_minutes=5)
    real_now = store_mod._now
    monkeypatch.setattr(store_mod, "_now", lambda: real_now() + timedelta(minutes=10))
    reclaimed = s.claim_next_task("agent-b", project["id"])
    assert reclaimed["id"] == t["id"] and reclaimed["assignee"] == "agent-b"
    actions = [a["action"] for a in s.get_activity(task_id=t["id"])]
    assert "lease_expired" in actions


def test_heartbeat_extends_lease(s, project):
    t = s.create_task(project["id"], "Job")
    claimed = s.claim_task(t["id"], "agent-a", lease_minutes=1)
    beat = s.heartbeat(t["id"], "agent-a", lease_minutes=60)
    assert beat["lease_expires_at"] > claimed["lease_expires_at"]


def test_inactive_projects_hand_out_no_work(s, project):
    s.create_task(project["id"], "Task")
    s.update_project(project["id"], status="on_hold")
    assert s.claim_next_task("agent") is None


def test_update_task_fields_and_clearing(s, project):
    m = s.create_milestone(project["id"], "Beta", due_date="2026-12-01")
    t = s.create_task(project["id"], "Task", assignee="agent-a", milestone_id=m["id"])
    u = s.update_task(t["id"], priority="high", tags=["x", "x", "a"], assignee="", milestone_id=0)
    assert u["priority"] == "high" and u["tags"] == ["a", "x"]
    assert u["assignee"] is None and u["milestone_id"] is None
    with pytest.raises(PMError, match="status must be"):
        s.update_task(t["id"], status="finished")


def test_report(s, project):
    pid = project["id"]
    s.create_milestone(pid, "Past", due_date="2000-01-01")
    m = s.list_milestones(pid)[0]
    a = s.create_task(pid, "A", milestone_id=m["id"])
    s.create_task(pid, "B", depends_on=[a["id"]])
    s.create_task(pid, "C")
    s.update_task(s.create_task(pid, "D")["id"], status="cancelled")
    s.complete_task(a["id"], "agent")
    r = s.project_report(pid)
    assert r["total_tasks"] == 4
    assert r["task_counts"]["done"] == 1
    assert r["percent_complete"] == pytest.approx(33.3)
    assert {t["title"] for t in r["ready_queue"]} == {"B", "C"}
    assert r["milestones"][0]["overdue"] is False  # its only task is done


def test_concurrent_agents_never_share_a_task(tmp_path):
    path = tmp_path / "shared.db"
    setup = Store(path)
    pid = setup.create_project("Load")["id"]
    for i in range(40):
        setup.create_task(pid, f"T{i}")
    setup.close()

    claimed: list[int] = []
    lock = threading.Lock()

    def worker(name):
        st = Store(path)  # separate connection, like a separate server process
        while (task := st.claim_next_task(name, pid)) is not None:
            with lock:
                claimed.append(task["id"])
        st.close()

    threads = [threading.Thread(target=worker, args=(f"agent-{n}",)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(claimed) == 40 and len(set(claimed)) == 40
