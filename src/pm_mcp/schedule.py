"""Critical-path scheduling over a task dependency graph.

Pure functions, no database access. Time is counted in whole days from the
project start; with ``workdays_only`` those days skip Saturdays and Sundays.
A task occupies days [early_start, early_finish), so a 5-day task starting
on day 0 finishes at the end of day 4.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta


@dataclass
class ScheduleTask:
    id: int
    title: str
    duration_days: float | None
    depends_on: list[int] = field(default_factory=list)
    status: str = "todo"
    completed_on: date | None = None
    milestone_id: int | None = None


class Calendar:
    def __init__(self, start: date, workdays_only: bool = True):
        self.workdays_only = workdays_only
        self.start = self._roll_forward(start)

    def _roll_forward(self, d: date) -> date:
        if self.workdays_only:
            while d.weekday() >= 5:
                d += timedelta(days=1)
        return d

    def to_date(self, offset: int) -> date:
        if not self.workdays_only:
            return self.start + timedelta(days=offset)
        weeks, rest = divmod(offset, 5)
        d = self.start + timedelta(weeks=weeks)
        while rest:
            d += timedelta(days=1)
            if d.weekday() < 5:
                rest -= 1
        return d

    def to_offset(self, d: date) -> int:
        """Working days from the start to ``d``; dates before the start are 0."""
        if d <= self.start:
            return 0
        if not self.workdays_only:
            return (d - self.start).days
        d = self._roll_forward(d)
        weeks, extra = divmod((d - self.start).days, 7)
        count = weeks * 5
        cursor = self.start + timedelta(weeks=weeks)
        for _ in range(extra):
            if cursor.weekday() < 5:
                count += 1
            cursor += timedelta(days=1)
        return count


def _duration(task: ScheduleTask) -> int:
    if task.status == "cancelled":
        return 0
    if task.duration_days is None:
        return 1
    return max(0, math.ceil(task.duration_days))


def _topological_order(tasks: dict[int, ScheduleTask]) -> list[int]:
    indegree = {tid: 0 for tid in tasks}
    successors: dict[int, list[int]] = {tid: [] for tid in tasks}
    for t in tasks.values():
        for dep in t.depends_on:
            if dep in tasks:
                indegree[t.id] += 1
                successors[dep].append(t.id)
    ready = sorted(tid for tid, n in indegree.items() if n == 0)
    order = []
    while ready:
        tid = ready.pop(0)
        order.append(tid)
        for nxt in successors[tid]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
    if len(order) != len(tasks):
        raise ValueError("dependency cycle in schedule")
    return order


def compute_schedule(
    task_list: list[ScheduleTask],
    start: date,
    workdays_only: bool = True,
    today: date | None = None,
) -> dict:
    """Forward and backward pass. Pass ``today`` for a forecast: finished tasks
    end on their completion date and unfinished work can't start before today.
    Without it the result is the baseline plan."""
    cal = Calendar(start, workdays_only)
    tasks = {t.id: t for t in task_list}
    order = _topological_order(tasks)
    today_offset = cal.to_offset(today) if today else None

    es: dict[int, int] = {}
    ef: dict[int, int] = {}
    for tid in order:
        t = tasks[tid]
        start_at = max((ef[d] for d in t.depends_on if d in tasks), default=0)
        dur = _duration(t)
        if today_offset is not None and t.status == "done" and t.completed_on:
            # Actuals: the task ends on the day it was completed.
            done_day = cal.to_offset(t.completed_on)
            ef[tid] = done_day + 1 if dur else done_day
            es[tid] = max(0, ef[tid] - dur)
            continue
        if today_offset is not None and t.status not in ("done", "cancelled"):
            start_at = max(start_at, today_offset)
        es[tid], ef[tid] = start_at, start_at + dur

    project_end = max(ef.values(), default=0)
    successors: dict[int, list[int]] = {tid: [] for tid in tasks}
    for t in tasks.values():
        for dep in t.depends_on:
            if dep in tasks:
                successors[dep].append(t.id)
    ls: dict[int, int] = {}
    lf: dict[int, int] = {}
    for tid in reversed(order):
        lf[tid] = min((ls[s] for s in successors[tid]), default=project_end)
        ls[tid] = lf[tid] - (ef[tid] - es[tid])

    def finish_date(tid: int) -> date:
        # Zero-length tasks (approvals, milestones) land on their start day.
        return cal.to_date(max(es[tid], ef[tid] - 1))

    rows = []
    for tid in order:
        t = tasks[tid]
        slack = ls[tid] - es[tid]
        finished = t.status in ("done", "cancelled")
        rows.append({
            "id": tid,
            "title": t.title,
            "status": t.status,
            "milestone_id": t.milestone_id,
            "duration_days": ef[tid] - es[tid],
            "early_start": cal.to_date(es[tid]).isoformat(),
            "early_finish": finish_date(tid).isoformat(),
            "late_start": cal.to_date(ls[tid]).isoformat(),
            "late_finish": cal.to_date(max(ls[tid], lf[tid] - 1)).isoformat(),
            "float_days": slack,
            "critical": slack == 0 and not finished,
        })
    rows.sort(key=lambda r: (r["early_start"], r["id"]))

    phases: dict[int, dict] = {}
    for r in rows:
        if r["milestone_id"] is None:
            continue
        p = phases.setdefault(r["milestone_id"], {"milestone_id": r["milestone_id"],
                                                   "start": r["early_start"], "finish": r["early_finish"]})
        p["start"] = min(p["start"], r["early_start"])
        p["finish"] = max(p["finish"], r["early_finish"])

    return {
        "start_date": cal.start.isoformat(),
        "finish_date": cal.to_date(max(0, project_end - 1)).isoformat() if tasks else cal.start.isoformat(),
        "duration_days": project_end,
        "workdays_only": workdays_only,
        "mode": "forecast" if today else "plan",
        "critical_path": [{"id": r["id"], "title": r["title"]} for r in rows if r["critical"]],
        "phases": list(phases.values()),
        "tasks": rows,
    }
