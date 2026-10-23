"""Construction layer on top of the generic board: templates, records with
workflow hooks, scheduling, budget, dashboard, and links to other systems.

Phases are milestones and work items are tasks, so everything in store.py
(claiming, leases, dependencies, comments) applies to construction work too.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from . import schedule as sched
from .records import RECORD_TYPES, record_type, validate_data
from .store import FINISHED_STATUSES, PMError, Store, _now, _ts
from .templates import BUILTIN_TEMPLATES, Template

ENTITY_TABLES = {"project": "projects", "milestone": "milestones", "task": "tasks", "record": "records"}


class ProjectDetails(BaseModel):
    """Construction facts about a project. Unknown keys are kept for app use."""

    model_config = ConfigDict(extra="allow")

    project_type: Literal[
        "residential_new", "residential_remodel", "commercial_new", "commercial_tenant_improvement", "other",
    ] | None = None
    site_address: str | None = None
    jurisdiction: str | None = None
    client_name: str | None = None
    start_date: date | None = None
    target_completion: date | None = None
    contract_value: float | None = None
    square_feet: float | None = None
    workdays_only: bool | None = None


def validate_details(details: dict | None) -> dict:
    try:
        return ProjectDetails.model_validate(details or {}).model_dump(mode="json", exclude_none=True)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise PMError(f"invalid project details: {problems}") from None


def _iso_date(value: str | None, field: str) -> str | None:
    if value is None or value == "":
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise PMError(f"{field} must be an ISO date like 2026-11-30") from None


def _today() -> date:
    return _now().date()


class Construction:
    def __init__(self, store: Store):
        self.store = store

    # -- templates ----------------------------------------------------------

    def _custom_templates(self) -> dict[str, Template]:
        with self.store._lock:
            rows = self.store._db.execute("SELECT key, body FROM templates ORDER BY key").fetchall()
        return {r["key"]: Template.model_validate_json(r["body"]) for r in rows}

    def _template(self, key: str) -> Template:
        template = BUILTIN_TEMPLATES.get(key) or self._custom_templates().get(key)
        if template is None:
            known = sorted({*BUILTIN_TEMPLATES, *self._custom_templates()})
            raise PMError(f"template {key!r} not found; available: {', '.join(known)}")
        return template

    def list_templates(self) -> list[dict]:
        out = []
        for source, templates in (("builtin", BUILTIN_TEMPLATES), ("custom", self._custom_templates())):
            for t in templates.values():
                out.append({
                    "key": t.key, "name": t.name, "description": t.description, "source": source,
                    "project_type": t.project_type, "phases": [p.name for p in t.phases],
                    "task_count": len(t.tasks),
                    "roles": sorted({r for task in t.tasks for r in task.roles}),
                })
        return out

    def get_template(self, key: str) -> dict:
        return self._template(key).model_dump()

    def save_template(self, body: dict, actor: str | None = None) -> dict:
        try:
            template = Template.model_validate(body)
        except ValidationError as exc:
            problems = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'template'}: {e['msg']}" for e in exc.errors())
            raise PMError(f"invalid template: {problems}") from None
        if template.key in BUILTIN_TEMPLATES:
            raise PMError(f"{template.key!r} is a built-in template; save yours under a different key")
        now = _ts(_now())
        with self.store._tx() as db:
            db.execute(
                "INSERT INTO templates (key, body, created_at, updated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (key) DO UPDATE SET body = excluded.body, updated_at = excluded.updated_at",
                (template.key, template.model_dump_json(), now, now),
            )
            self.store._log(db, "template_saved", actor, template=template.key)
        return {"key": template.key, "phases": len(template.phases), "tasks": len(template.tasks)}

    def delete_template(self, key: str, actor: str | None = None) -> dict:
        with self.store._tx() as db:
            if db.execute("DELETE FROM templates WHERE key = ?", (key,)).rowcount == 0:
                raise PMError(f"no custom template {key!r}")
            self.store._log(db, "template_deleted", actor, template=key)
        return {"deleted": key}

    def create_project_from_template(
        self, name: str, template_key: str, description: str = "", details: dict | None = None,
        actor: str | None = None,
    ) -> dict:
        """Create a project with one milestone per phase, every template task
        with its dependencies, and permit/inspection records linked to their
        gate tasks. All in one transaction."""
        template = self._template(template_key)
        details = validate_details({"project_type": template.project_type, **(details or {})})
        details["template"] = template.key
        with self.store._tx() as db:
            project_id = self.store._insert_project(db, name, description or template.description, details, actor)
            applied = self._apply_template(db, project_id, template, actor)
        return self._template_result(project_id, applied, details.get("start_date"))

    def apply_template(self, project_id: int, template_key: str, actor: str | None = None) -> dict:
        """Lay a template's phases and tasks into an existing project. Phases that
        already exist by name are reused; the template's tasks are added to them."""
        template = self._template(template_key)
        with self.store._tx() as db:
            self.store._project_row(db, project_id)
            applied = self._apply_template(db, project_id, template, actor)
        details = self.store.get_project(project_id)["details"]
        return self._template_result(project_id, applied, details.get("start_date"))

    def _apply_template(self, db, project_id: int, template: Template, actor: str | None) -> dict:
        s = self.store
        existing = {r["name"]: r["id"] for r in db.execute(
            "SELECT id, name FROM milestones WHERE project_id = ?", (project_id,))}
        phase_ids = {
            p.key: existing.get(p.name) or s._insert_milestone(db, project_id, p.name, p.description, None, actor)
            for p in template.phases
        }
        task_ids: dict[str, int] = {}
        records_created = 0
        for t in template.tasks:
            row = s._insert_task(
                db, project_id, t.title, t.description, t.priority, milestone_id=phase_ids[t.phase],
                tags=t.roles, actor=actor, kind=t.kind, duration_days=t.duration_days,
            )
            task_ids[t.key] = row["id"]
            if t.kind == "inspection":
                self._insert_record(db, project_id, "inspection", t.title, None,
                                    {"inspection_type": t.inspection_type}, None, row["id"],
                                    phase_ids[t.phase], actor)
                records_created += 1
            elif t.kind == "permit":
                self._insert_record(db, project_id, "permit", t.title, None,
                                    {"permit_type": t.permit_type}, None, row["id"], phase_ids[t.phase], actor)
                records_created += 1
        for t in template.tasks:  # the template was validated acyclic
            for dep in t.depends_on:
                db.execute("INSERT INTO task_dependencies (task_id, depends_on_id) VALUES (?, ?)",
                           (task_ids[t.key], task_ids[dep]))
        s._log(db, "template_applied", actor, project_id, template=template.key, tasks=len(task_ids))
        return {"phases": [{"id": phase_ids[p.key], "name": p.name} for p in template.phases],
                "tasks_created": len(task_ids), "records_created": records_created}

    def _template_result(self, project_id: int, applied: dict, start_date: str | None) -> dict:
        result = {"project": self.store.get_project(project_id), **applied}
        if start_date:
            plan = self.schedule(project_id, mode="plan", save=True)
            result["schedule"] = {k: plan[k] for k in ("start_date", "finish_date", "duration_days")}
            result["schedule"]["critical_path_length"] = len(plan["critical_path"])
        return result

    # -- records ------------------------------------------------------------

    @staticmethod
    def _record_dict(row) -> dict:
        rec = dict(row)
        rec["data"] = json.loads(rec["data"] or "{}")
        return rec

    def _record_row(self, db, record_id: int):
        row = db.execute("SELECT * FROM records WHERE id = ?", (record_id,)).fetchone()
        if row is None:
            raise PMError(f"record {record_id} not found")
        return row

    @staticmethod
    def _default_title(rtype: str, data: dict, number: int) -> str:
        label = {
            "permit": lambda: f"{data.get('permit_type', 'building').replace('_', ' ').title()} permit",
            "inspection": lambda: f"{data.get('inspection_type', '').replace('_', ' ').title()} inspection",
            "budget_item": lambda: data.get("cost_code", ""),
            "change_order": lambda: f"Change order #{number}",
            "rfi": lambda: f"RFI #{number}: {data.get('question', '')[:60]}",
            "submittal": lambda: f"Submittal #{number}" + (f" ({data['spec_section']})" if data.get("spec_section") else ""),
            "punch_item": lambda: data.get("description", "")[:80] or f"Punch item #{number}",
            "daily_log": lambda: f"Daily log {data.get('log_date', '')}",
            "contact": lambda: data.get("name") or data.get("company") or f"Contact #{number}",
            "document": lambda: f"Document #{number}",
        }[rtype]()
        return label.strip() or f"{rtype.replace('_', ' ').title()} #{number}"

    def _insert_record(
        self, db, project_id: int, rtype_name: str, title: str | None, status: str | None, data: dict,
        due_date: str | None, task_id: int | None, milestone_id: int | None, actor: str | None,
    ) -> dict:
        rtype = record_type(rtype_name)
        status = status or rtype.default_status
        if status not in rtype.statuses:
            raise PMError(f"{rtype.name} status must be one of {', '.join(rtype.statuses)}")
        data = validate_data(rtype, data)
        if task_id is not None and self.store._task_row(db, task_id)["project_id"] != project_id:
            raise PMError(f"task {task_id} belongs to a different project")
        if milestone_id is not None and self.store._milestone_row(db, milestone_id)["project_id"] != project_id:
            raise PMError(f"milestone {milestone_id} belongs to a different project")
        number = db.execute(
            "SELECT COALESCE(MAX(number), 0) + 1 FROM records WHERE project_id = ? AND type = ?",
            (project_id, rtype.name),
        ).fetchone()[0]
        now = _ts(_now())
        cur = db.execute(
            "INSERT INTO records (project_id, type, number, title, status, milestone_id, task_id, due_date,"
            " amount, data, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, rtype.name, number, (title or "").strip() or self._default_title(rtype.name, data, number),
             status, milestone_id, task_id, _iso_date(due_date, "due_date"),
             data.get(rtype.amount_field) if rtype.amount_field else None, json.dumps(data), actor, now, now),
        )
        self.store._log(db, "record_created", actor, project_id, task_id, record_id=cur.lastrowid,
                        type=rtype.name, status=status)
        record = self._record_dict(self._record_row(db, cur.lastrowid))
        return self._after_status_change(db, record, None, actor)

    def _after_status_change(self, db, record: dict, old_status: str | None, actor: str | None) -> dict:
        """Workflow side effects when a record reaches a new status."""
        status, data, effects = record["status"], dict(record["data"]), {}
        if status == old_status:
            return record
        today = _today().isoformat()
        rtype = record["type"]
        if rtype == "permit" and status == "approved":
            data.setdefault("approved_date", today)
            effects["completed_task"] = self._finish_gate(db, record, actor)
        elif rtype == "inspection" and status == "passed":
            effects["completed_task"] = self._finish_gate(db, record, actor)
        elif rtype == "inspection" and status in ("failed", "partial"):
            effects["correction_task"] = self._correction_task(db, record, actor)
        elif rtype == "change_order" and status == "approved":
            data.setdefault("approved_date", today)
        elif rtype == "rfi" and status == "answered":
            data.setdefault("answered_date", today)
        elif rtype == "submittal" and status in ("approved", "approved_as_noted", "revise_resubmit", "rejected"):
            data.setdefault("returned_date", today)
        if data != record["data"]:
            db.execute("UPDATE records SET data = ? WHERE id = ?", (json.dumps(data), record["id"]))
            record["data"] = data
        record.update({k: v for k, v in effects.items() if v is not None})
        return record

    def _finish_gate(self, db, record: dict, actor: str | None) -> int | None:
        if record["task_id"] is None:
            return None
        task = self.store._task_row(db, record["task_id"])
        if task["status"] in FINISHED_STATUSES:
            return None
        others = db.execute(
            "SELECT COUNT(*) FROM records WHERE task_id = ? AND type = ? AND id <> ? AND status <> ?",
            (task["id"], record["type"], record["id"], record["status"]),
        ).fetchone()[0]
        if others:  # another linked record still has to pass
            return None
        self.store._finish_task(db, task, actor, result=f"{record['title']}: {record['status']}")
        return task["id"]

    def _correction_task(self, db, record: dict, actor: str | None) -> int | None:
        if record["task_id"] is None:
            return None
        gate = self.store._task_row(db, record["task_id"])
        if gate["status"] in FINISHED_STATUSES:
            return None
        # The trades whose work led up to the inspection fix the deficiencies.
        roles = set()
        for row in db.execute(
            "SELECT t.tags FROM task_dependencies d JOIN tasks t ON t.id = d.depends_on_id WHERE d.task_id = ?",
            (gate["id"],),
        ):
            roles.update(json.loads(row["tags"]))
        roles.discard("inspection")
        notes = record["data"].get("result_notes") or "See inspection record for the correction notice."
        task = self.store._insert_task(
            db, gate["project_id"], f"Correct deficiencies: {record['title']}",
            f"Inspection record {record['id']} came back {record['status']}.\n\n{notes}",
            "high", milestone_id=gate["milestone_id"], tags=sorted(roles) or ["superintendent"],
            actor=actor, duration_days=1,
        )
        db.execute("INSERT INTO task_dependencies (task_id, depends_on_id) VALUES (?, ?)", (gate["id"], task["id"]))
        if gate["status"] == "in_progress":
            db.execute("UPDATE tasks SET status = 'todo', assignee = NULL, lease_expires_at = NULL WHERE id = ?",
                       (gate["id"],))
        return task["id"]

    def create_record(
        self, project_id: int, record_type_name: str, title: str | None = None, status: str | None = None,
        data: dict | None = None, due_date: str | None = None, task_id: int | None = None,
        milestone_id: int | None = None, actor: str | None = None,
    ) -> dict:
        with self.store._tx() as db:
            self.store._project_row(db, project_id)
            return self._insert_record(db, project_id, record_type_name, title, status, data or {},
                                       due_date, task_id, milestone_id, actor)

    def _update_record(self, db, record_id: int, actor: str | None, **fields) -> dict:
        current = self._record_dict(self._record_row(db, record_id))
        rtype = RECORD_TYPES[current["type"]]
        changes: dict = {}
        if fields.get("title"):
            changes["title"] = fields["title"].strip()
        if fields.get("status") is not None:
            if fields["status"] not in rtype.statuses:
                raise PMError(f"{rtype.name} status must be one of {', '.join(rtype.statuses)}")
            changes["status"] = fields["status"]
        if fields.get("data"):
            # Merge: keys set to None are removed.
            merged = {**current["data"], **fields["data"]}
            data = validate_data(rtype, {k: v for k, v in merged.items() if v is not None})
            changes["data"] = json.dumps(data)
            if rtype.amount_field:
                changes["amount"] = data.get(rtype.amount_field)
        if "due_date" in fields and fields["due_date"] is not None:
            changes["due_date"] = _iso_date(fields["due_date"], "due_date")
        for key, table_check in (("task_id", self.store._task_row), ("milestone_id", self.store._milestone_row)):
            value = fields.get(key)
            if value is None:
                continue
            if value == 0:
                changes[key] = None
            elif table_check(db, value)["project_id"] != current["project_id"]:
                raise PMError(f"{key} {value} belongs to a different project")
            else:
                changes[key] = value
        if not changes:
            return current
        sets = ", ".join(f"{k} = ?" for k in changes)
        db.execute(f"UPDATE records SET {sets}, updated_at = ? WHERE id = ?",
                   (*changes.values(), _ts(_now()), record_id))
        logged = {k: (json.loads(v) if k == "data" else v) for k, v in changes.items()}
        self.store._log(db, "record_updated", actor, current["project_id"], current["task_id"],
                        record_id=record_id, type=rtype.name, **logged)
        record = self._record_dict(self._record_row(db, record_id))
        return self._after_status_change(db, record, current["status"], actor)

    def update_record(self, record_id: int, actor: str | None = None, **fields) -> dict:
        with self.store._tx() as db:
            return self._update_record(db, record_id, actor, **fields)

    def record_inspection_result(
        self, inspection_id: int, result: str, notes: str = "", inspector: str | None = None,
        actor: str | None = None,
    ) -> dict:
        if result not in ("passed", "failed", "partial"):
            raise PMError("result must be passed, failed or partial")
        with self.store._tx() as db:
            rec = self._record_dict(self._record_row(db, inspection_id))
            if rec["type"] != "inspection":
                raise PMError(f"record {inspection_id} is a {rec['type']}, not an inspection")
            attempt = {"date": _today().isoformat(), "result": result, "notes": notes, "inspector": inspector}
            data = {"attempts": [*rec["data"].get("attempts", []), attempt], "result_notes": notes or None}
            if inspector:
                data["inspector"] = inspector
            return self._update_record(db, inspection_id, actor, status=result, data=data)

    def delete_record(self, record_id: int, actor: str | None = None) -> dict:
        with self.store._tx() as db:
            rec = self._record_row(db, record_id)
            db.execute("DELETE FROM records WHERE id = ?", (record_id,))
            db.execute("DELETE FROM external_refs WHERE entity_type = 'record' AND entity_id = ?", (record_id,))
            self.store._log(db, "record_deleted", actor, rec["project_id"], rec["task_id"],
                            record_id=record_id, type=rec["type"], title=rec["title"])
        return {"deleted": record_id}

    def get_record(self, record_id: int) -> dict:
        with self.store._lock:
            return self._record_dict(self._record_row(self.store._db, record_id))

    def list_records(
        self, project_id: int, record_type_name: str | None = None, status: str | None = None,
        open_only: bool = False, milestone_id: int | None = None, task_id: int | None = None, limit: int = 200,
    ) -> list[dict]:
        where, params = ["project_id = ?"], [project_id]
        if record_type_name:
            record_type(record_type_name)
            where.append("type = ?")
            params.append(record_type_name)
        if status:
            where.append("status = ?")
            params.append(status)
        for col, value in (("milestone_id", milestone_id), ("task_id", task_id)):
            if value is not None:
                where.append(f"{col} = ?")
                params.append(value)
        sql = f"SELECT * FROM records WHERE {' AND '.join(where)} ORDER BY type, number"
        with self.store._lock:
            self.store._project_row(self.store._db, project_id)
            rows = [self._record_dict(r) for r in self.store._db.execute(sql, params)]
        if open_only:
            rows = [r for r in rows if r["status"] not in RECORD_TYPES[r["type"]].closed]
        return rows[: max(1, limit)]

    # -- budget -------------------------------------------------------------

    def budget_summary(self, project_id: int) -> dict:
        details = self.store.get_project(project_id)["details"]
        items = self.list_records(project_id, "budget_item", limit=100_000)
        by_code: dict[str, dict] = {}
        by_category: dict[str, dict] = {}
        totals = {"budgeted": 0.0, "committed": 0.0, "actual": 0.0, "forecast": 0.0}
        contingency = {"budgeted": 0.0, "used": 0.0}
        for item in items:
            d = item["data"]
            row = {"budgeted": d.get("budgeted", 0.0), "committed": d.get("committed", 0.0),
                   "actual": d.get("actual", 0.0)}
            row["forecast"] = max(row["committed"], row["actual"])
            for bucket, key in ((by_code, d.get("cost_code", "uncoded")), (by_category, d.get("category", "other"))):
                agg = bucket.setdefault(key, {"budgeted": 0.0, "committed": 0.0, "actual": 0.0, "forecast": 0.0})
                for k in agg:
                    agg[k] += row[k]
            for k in totals:
                totals[k] += row[k]
            if d.get("category") == "contingency":
                contingency["budgeted"] += row["budgeted"]
                contingency["used"] += row["forecast"]
        for agg in (*by_code.values(), *by_category.values(), totals):
            agg["variance"] = round(agg["budgeted"] - agg["forecast"], 2)
            for k in list(agg):
                agg[k] = round(agg[k], 2)
        cos = self.list_records(project_id, "change_order", limit=100_000)
        approved = [c for c in cos if c["status"] == "approved"]
        pending = [c for c in cos if c["status"] in ("draft", "submitted")]
        contract = details.get("contract_value")
        approved_total = round(sum(c["data"].get("cost_impact", 0.0) for c in approved), 2)
        with self.store._lock:
            task_costs = self.store._db.execute(
                "SELECT COALESCE(SUM(budgeted_cost), 0), COALESCE(SUM(actual_cost), 0) FROM tasks"
                " WHERE project_id = ? AND status <> 'cancelled'", (project_id,),
            ).fetchone()
        return {
            "totals": totals,
            # Costs entered on tasks themselves, reported apart from the budget lines.
            "task_costs": {"budgeted": round(task_costs[0], 2), "actual": round(task_costs[1], 2)},
            "by_cost_code": dict(sorted(by_code.items())),
            "by_category": by_category,
            "contingency": {**contingency, "remaining": round(contingency["budgeted"] - contingency["used"], 2)},
            "change_orders": {
                "approved_count": len(approved),
                "approved_cost": approved_total,
                "approved_schedule_days": sum(c["data"].get("schedule_impact_days", 0) for c in approved),
                "pending_count": len(pending),
                "pending_cost": round(sum(c["data"].get("cost_impact", 0.0) for c in pending), 2),
            },
            "contract_value": contract,
            "revised_contract_value": round(contract + approved_total, 2) if contract is not None else None,
        }

    # -- schedule -----------------------------------------------------------

    def schedule(
        self, project_id: int, mode: Literal["plan", "forecast"] = "forecast", save: bool = False,
        start_date: str | None = None,
    ) -> dict:
        details = self.store.get_project(project_id)["details"]
        start = _iso_date(start_date, "start_date") or details.get("start_date")
        if not start:
            raise PMError("no start date: pass start_date or set details.start_date on the project")
        db = self.store._db
        with self.store._lock:
            rows = db.execute(
                "SELECT id, title, duration_days, status, completed_at, milestone_id FROM tasks WHERE project_id = ?",
                (project_id,),
            ).fetchall()
            deps: dict[int, list[int]] = {}
            for r in db.execute(
                "SELECT d.task_id, d.depends_on_id FROM task_dependencies d JOIN tasks t ON t.id = d.task_id"
                " WHERE t.project_id = ?", (project_id,),
            ):
                deps.setdefault(r["task_id"], []).append(r["depends_on_id"])
        tasks = [
            sched.ScheduleTask(
                id=r["id"], title=r["title"], duration_days=r["duration_days"], depends_on=deps.get(r["id"], []),
                status=r["status"], milestone_id=r["milestone_id"],
                completed_on=datetime.fromisoformat(r["completed_at"]).date() if r["completed_at"] else None,
            )
            for r in rows
        ]
        result = sched.compute_schedule(
            tasks, date.fromisoformat(start), details.get("workdays_only", True),
            today=_today() if mode == "forecast" else None,
        )
        target = details.get("target_completion")
        if target:
            result["target_completion"] = target
            result["days_past_target"] = (date.fromisoformat(result["finish_date"]) - date.fromisoformat(target)).days
        if save:
            with self.store._tx() as wdb:
                for t in result["tasks"]:
                    wdb.execute("UPDATE tasks SET planned_start = ?, planned_finish = ? WHERE id = ?",
                                (t["early_start"], t["early_finish"], t["id"]))
                for p in result["phases"]:
                    wdb.execute("UPDATE milestones SET due_date = ? WHERE id = ?", (p["finish"], p["milestone_id"]))
                self.store._log(wdb, "schedule_saved", None, project_id, mode=mode, finish=result["finish_date"])
            result["saved"] = True
        return result

    # -- dashboard ----------------------------------------------------------

    def dashboard(self, project_id: int) -> dict:
        s = self.store
        project = s.get_project(project_id)
        today = _today().isoformat()
        phases = []
        for m in s.list_milestones(project_id):
            done, total = m["done_count"], m["task_count"]
            state = "complete" if total and done == total else "not_started" if done == 0 else "in_progress"
            with s._lock:
                active = s._db.execute(
                    "SELECT COUNT(*) FROM tasks WHERE milestone_id = ? AND status IN ('in_progress', 'review')",
                    (m["id"],),
                ).fetchone()[0]
            if state == "not_started" and active:
                state = "in_progress"
            phases.append({"id": m["id"], "name": m["name"], "due_date": m["due_date"], "tasks": total,
                           "done": done, "percent": round(100 * done / total, 1) if total else 0.0, "status": state})
        current = next((p for p in phases if p["status"] != "complete"), None)

        records = self.list_records(project_id, limit=100_000)
        open_counts: dict[str, int] = {}
        overdue = []
        for r in records:
            # Budget lines and contacts are reference data, not action items.
            if r["type"] in ("budget_item", "contact") or r["status"] in RECORD_TYPES[r["type"]].closed:
                continue
            open_counts[r["type"]] = open_counts.get(r["type"], 0) + 1
            if r["due_date"] and r["due_date"] < today:
                overdue.append({k: r[k] for k in ("id", "type", "title", "status", "due_date")})

        with s._lock:
            behind = [dict(r) for r in s._db.execute(
                "SELECT id, title, status, assignee, planned_finish FROM tasks WHERE project_id = ?"
                f" AND planned_finish < ? AND status NOT IN {FINISHED_STATUSES} ORDER BY planned_finish LIMIT 15",
                (project_id, today),
            )]
        ready_by_role: dict[str, int] = {}
        for t in s.list_tasks(project_id, ready_only=True, limit=100_000):
            for tag in t["tags"] or ["untagged"]:
                ready_by_role[tag] = ready_by_role.get(tag, 0) + 1

        schedule = None
        if project["details"].get("start_date"):
            fc = self.schedule(project_id, mode="forecast")
            schedule = {k: fc.get(k) for k in ("start_date", "finish_date", "target_completion", "days_past_target")}
            schedule["critical_path"] = fc["critical_path"][:10]

        budget = self.budget_summary(project_id)
        return {
            "project": project,
            "current_phase": current["name"] if current else "complete",
            "phases": phases,
            "schedule": schedule,
            "permits": [{k: r[k] for k in ("id", "title", "status")} | {"permit_number": r["data"].get("permit_number")}
                        for r in records if r["type"] == "permit"],
            "inspections": {
                st: [{"id": r["id"], "title": r["title"]} for r in records if r["type"] == "inspection" and r["status"] == st]
                for st in ("scheduled", "failed", "partial")
            } | {"passed_count": sum(1 for r in records if r["type"] == "inspection" and r["status"] == "passed")},
            "open_items": open_counts,
            "overdue_records": overdue,
            "tasks_behind_plan": behind,
            "ready_tasks_by_role": dict(sorted(ready_by_role.items())),
            "budget": {"totals": budget["totals"], "contract_value": budget["contract_value"],
                       "revised_contract_value": budget["revised_contract_value"],
                       "pending_change_orders": budget["change_orders"]["pending_count"]},
        }

    # -- integration with other systems --------------------------------------

    def link_external(self, entity_type: str, entity_id: int, system: str, external_id: str,
                      actor: str | None = None) -> dict:
        table = ENTITY_TABLES.get(entity_type)
        if table is None:
            raise PMError(f"entity_type must be one of {', '.join(ENTITY_TABLES)}")
        if not system.strip() or not str(external_id).strip():
            raise PMError("system and external_id are required")
        with self.store._tx() as db:
            if not db.execute(f"SELECT 1 FROM {table} WHERE id = ?", (entity_id,)).fetchone():
                raise PMError(f"{entity_type} {entity_id} not found")
            existing = db.execute(
                "SELECT * FROM external_refs WHERE entity_type = ? AND system = ? AND external_id = ?",
                (entity_type, system, str(external_id)),
            ).fetchone()
            if existing and existing["entity_id"] != entity_id:
                raise PMError(f"{system}:{external_id} is already linked to {entity_type} {existing['entity_id']}")
            if not existing:
                db.execute(
                    "INSERT INTO external_refs (entity_type, entity_id, system, external_id, created_at)"
                    " VALUES (?, ?, ?, ?, ?)", (entity_type, entity_id, system, str(external_id), _ts(_now())),
                )
                self.store._log(db, "external_linked", actor, entity_type=entity_type, entity_id=entity_id,
                                system=system, external_id=str(external_id))
        return {"entity_type": entity_type, "entity_id": entity_id, "system": system, "external_id": str(external_id)}

    def find_external(self, system: str, external_id: str | None = None, entity_type: str | None = None,
                      entity_id: int | None = None) -> list[dict]:
        where, params = ["system = ?"], [system]
        for col, value in (("external_id", external_id), ("entity_type", entity_type), ("entity_id", entity_id)):
            if value is not None:
                where.append(f"{col} = ?")
                params.append(str(value) if col == "external_id" else value)
        with self.store._lock:
            rows = self.store._db.execute(
                f"SELECT entity_type, entity_id, system, external_id, created_at FROM external_refs"
                f" WHERE {' AND '.join(where)} ORDER BY id", params,
            ).fetchall()
        return [dict(r) for r in rows]

    def export_project(self, project_id: int) -> dict:
        s = self.store
        project = s.get_project(project_id)
        with s._lock:
            db = s._db
            tasks = [s._task_dict(r) for r in db.execute("SELECT * FROM tasks WHERE project_id = ? ORDER BY id",
                                                         (project_id,))]
            deps: dict[int, list[int]] = {}
            for r in db.execute(
                "SELECT d.task_id, d.depends_on_id FROM task_dependencies d JOIN tasks t ON t.id = d.task_id"
                " WHERE t.project_id = ?", (project_id,),
            ):
                deps.setdefault(r["task_id"], []).append(r["depends_on_id"])
            comments = [dict(r) for r in db.execute(
                "SELECT c.* FROM comments c JOIN tasks t ON t.id = c.task_id WHERE t.project_id = ? ORDER BY c.id",
                (project_id,),
            )]
            milestone_ids = [r[0] for r in db.execute("SELECT id FROM milestones WHERE project_id = ?", (project_id,))]
        for t in tasks:
            t["depends_on"] = sorted(deps.get(t["id"], []))
        records = self.list_records(project_id, limit=1_000_000)
        ids = {"project": [project_id], "milestone": milestone_ids, "task": [t["id"] for t in tasks],
               "record": [r["id"] for r in records]}
        refs = []
        with s._lock:
            for etype, values in ids.items():
                for chunk in range(0, len(values), 500):
                    part = values[chunk:chunk + 500]
                    if not part:
                        continue
                    refs += [dict(r) for r in s._db.execute(
                        f"SELECT entity_type, entity_id, system, external_id FROM external_refs"
                        f" WHERE entity_type = ? AND entity_id IN ({','.join('?' * len(part))})", (etype, *part),
                    )]
        return {
            "format": "pm-mcp/1",
            "exported_at": _ts(_now()),
            "project": project,
            "milestones": s.list_milestones(project_id),
            "tasks": tasks,
            "records": records,
            "comments": comments,
            "external_refs": refs,
        }
