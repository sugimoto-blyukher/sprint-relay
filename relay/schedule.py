"""Dependency-only CPM and deterministic capacity-aware forecasts."""

from __future__ import annotations

import heapq
import io
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .core import Task

UTC = timezone.utc
Interval = tuple[datetime, datetime]


def _instant(value, zone: ZoneInfo | timezone) -> datetime:
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, time())
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if stamp.tzinfo is None:
        local = stamp.replace(tzinfo=zone)
        if local.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != stamp:
            raise ValueError(f"Nonexistent local time {stamp} in {zone}")
        if local.utcoffset() != local.replace(fold=1).utcoffset():
            raise ValueError(f"Ambiguous local time {stamp} in {zone}; supply its UTC offset")
        stamp = local
    return stamp.astimezone(UTC)


def _hours(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{label} must be a finite non-negative number of hours")
    return float(value)


def _minutes(value, label: str) -> int:
    return math.ceil(60 * _hours(value, label))


@dataclass
class Calendar:
    zone: ZoneInfo
    start: datetime
    end: datetime
    feature_freeze: datetime | None
    milestones: dict[str, datetime]
    availability: dict[str, list[Interval]]
    buffer: timedelta

    @classmethod
    def from_dict(cls, data) -> Calendar:
        try:
            zone = ZoneInfo(data.get("timezone", "Asia/Tokyo"))
            start, end = (_instant(data[key], zone) for key in ("start", "end"))
        except (KeyError, ZoneInfoNotFoundError) as exc:
            raise ValueError(f"Invalid calendar timezone or missing period: {exc}") from exc
        if end <= start:
            raise ValueError("Project end must follow project start")
        availability = {}
        for owner, intervals in data.get("availability", {}).items():
            windows = sorted((_instant(s, zone), _instant(e, zone)) for s, e in intervals)
            merged: list[Interval] = []
            for s, e in windows:
                if s >= e:
                    raise ValueError(f"Invalid availability interval for {owner}")
                if merged and s <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], e))
                else:
                    merged.append((s, e))
            availability[str(owner)] = merged
        freeze = data.get("feature_freeze")
        milestones = {name: _instant(value, zone) for name, value in data.get("milestones", {}).items()}
        return cls(zone, start, end, _instant(freeze, zone) if freeze else None, milestones,
                   availability, timedelta(minutes=_minutes(data.get("buffer_hours", 0), "buffer")))


@dataclass
class Plan:
    tasks: list[Task]
    calendar: Calendar
    now: datetime
    segments: dict[str, list[Interval]]
    owners: dict[str, str]  # Tentative capacity reservations, including unclaimed design.
    duration_hours: float | None
    critical_paths: list[list[str]]
    path_count: int
    finish: datetime | None
    risks: list[str]


def _graph(tasks):
    by_key = {t.key: t for t in tasks}
    if len(by_key) != len(tasks) or any(not k for k in by_key):
        raise ValueError("Missing or duplicate task key")
    children = defaultdict(list)
    indegree = {}
    ready = []
    for task in tasks:
        indegree[task.key] = len(task.dependencies)
        for key in task.dependencies:
            if key not in by_key:
                raise ValueError(f"{task.key} has unknown dependency {key}")
            children[key].append(task.key)
        if not task.dependencies:
            heapq.heappush(ready, _rank(task))
    order = []
    while ready:
        _, _, key = heapq.heappop(ready)
        order.append(key)
        for child in children[key]:
            indegree[child] -= 1
            if indegree[child] == 0:
                heapq.heappush(ready, _rank(by_key[child]))
    if len(order) != len(tasks):
        raise ValueError("Cycle in task dependencies")
    return by_key, children, order


def _rank(task):
    return (task.status not in {"In Progress", "In Review"}, task.priority, task.key)


def _cpm(by_key, children, order):
    # Use original estimates and no capacity limits. This is a structural lower
    # bound for the original work, distinct from a forecast of remaining effort.
    active = [key for key in order if by_key[key].status != "Cancelled"]
    if any(by_key[key].estimated_hours is None or
           any(by_key[p].status == "Cancelled" for p in by_key[key].dependencies) for key in active):
        return {}, None, [], 0
    es, ef, ls, lf = {}, {}, {}, {}
    for key in active:
        es[key] = max((ef[p] for p in by_key[key].dependencies), default=0)
        ef[key] = es[key] + _minutes(by_key[key].estimated_hours, key)
    duration = max(ef.values(), default=0)
    for key in reversed(active):
        lf[key] = min((ls[c] for c in children[key] if c in es), default=duration)
        ls[key] = lf[key] - (ef[key] - es[key])
    metrics = {k: {"es": es[k] / 60, "ef": ef[k] / 60, "ls": ls[k] / 60, "lf": lf[k] / 60,
                   "total_float": (ls[k] - es[k]) / 60} for k in active}
    critical = {k for k in active if es[k] == ls[k]}
    links = {k: sorted(c for c in children[k] if c in critical and es[c] == ef[k]) for k in critical}
    roots = sorted(critical - {c for edges in links.values() for c in edges})
    counts = {}
    for key in reversed(active):
        if key in links:
            counts[key] = sum(counts[c] for c in links[key]) if links[key] else 1
    # Parallel diamonds can have exponentially many critical paths. Count them
    # all but retain only the first 20 deterministic examples for the UI.
    paths = []
    stack = [[k] for k in reversed(roots)]
    while stack and len(paths) < 20:
        path = stack.pop()
        if links[path[-1]]:
            stack.extend(path + [c] for c in reversed(links[path[-1]]))
        else:
            paths.append(path)
    return metrics, duration / 60, paths, sum(counts[k] for k in roots)


def _fit(windows, release, effort):
    segments = []
    for start, end in windows:
        start = max(start, release)
        if end <= start:
            continue
        used = min(end - start, effort)
        segments.append((start, start + used))
        effort -= used
        if not effort:
            return segments
    return None


def _reserve(windows, segments):
    # Both inputs are sorted, and each reserved segment fits in a single window.
    used = iter(segments)
    slot = next(used, None)
    result = []
    for start, end in windows:
        while slot and slot[0] < end:
            if start < slot[0]:
                result.append((start, slot[0]))
            start = slot[1]
            slot = next(used, None)
        if start < end:
            result.append((start, end))
    return result


def _forecast(task, by_key, ends, free, members, release):
    if task.status == "Blocked":
        return None, None, "blocked; owner must confirm resolution before forecasting"
    if task.remaining_hours is None:
        return None, None, "remaining effort unknown; request an estimate"
    for key in task.dependencies:
        if by_key[key].status == "Cancelled":
            return None, None, f"dependency {key} cancelled; review scope or dependencies"
        if key not in ends:
            return None, None, f"waiting for a forecast or completion date for {key}"
        release = max(release, ends[key])
    people = sorted(p for p, role in members.items() if role == task.role)
    if task.assignee is not None:
        if task.assignee not in people:
            return None, None, "assigned member does not match role; request owner review"
        people = [task.assignee]
    elif task.role != "design" and len(people) != 1:
        return None, None, "fixed role has no unique owner; review configuration"
    duration = timedelta(minutes=_minutes(task.remaining_hours, task.key))
    if duration == timedelta(0):
        return None, [(release, release)], None
    candidates = []
    for person in people:
        segments = _fit(free.get(person, []), release, duration)
        if segments:
            candidates.append((segments[-1][1], segments[0][0], person, segments))
    if not candidates:
        return None, None, "insufficient declared availability; review capacity, scope or period"
    _, _, person, segments = min(candidates)
    free[person] = _reserve(free[person], segments)
    return person, segments, None


def recalculate(tasks, config, *, now: datetime | None = None) -> Plan:
    """Calculate fresh copies. Persist changed fields using the canonical adapter."""
    tasks = list(tasks)
    calendar = Calendar.from_dict(config.calendar)
    unknown_members = calendar.availability.keys() - config.members.keys()
    if unknown_members:
        raise ValueError(f"Availability for unknown members: {sorted(unknown_members)}")
    if sum(role == "design" for role in config.members.values()) > 2:
        raise ValueError("The design pool supports at most two members")
    now = datetime.now(UTC) if now is None else now
    if now.tzinfo is None:
        raise ValueError("Pass an offset-aware calculation time")
    now = now.astimezone(UTC)
    minute = now.replace(second=0, microsecond=0)
    release = max(calendar.start, minute if now == minute else minute + timedelta(minutes=1))
    by_key, children, order = _graph(tasks)
    metrics, duration, paths, path_count = _cpm(by_key, children, order)
    free = calendar.availability.copy()
    ends, owners, allocations, updated = {}, {}, {}, {}
    risks = []
    unresolved = False
    for key in order:
        task = by_key[key]
        update = {"metrics": metrics.get(key, {}), "forecast_start": None, "forecast_end": None}
        if task.status == "Done":
            if task.actual_end:
                ends[key] = _instant(task.actual_end, UTC)
            else:
                unresolved = True
                risks.append(f"{key}: completion date missing; confirm actual end")
        elif task.status != "Cancelled":
            person, slots, problem = _forecast(task, by_key, ends, free, config.members, release)
            if problem:
                unresolved = True
                risks.append(f"{key}: {problem}")
            else:
                start, end = slots[0][0], slots[-1][1]
                ends[key] = end
                allocations[key] = slots
                if person:
                    owners[key] = person
                update.update(forecast_start=start.isoformat(), forecast_end=end.isoformat())
                if not task.baseline_start and not task.baseline_end:
                    update.update(baseline_start=start.isoformat(), baseline_end=end.isoformat())
        if task.deadline and task.status != "Cancelled":
            deadline = _instant(task.deadline, calendar.zone)
            if ends.get(key, now) > deadline:
                risks.append(f"{key}: misses task deadline; review scope or available hours")
        updated[key] = replace(task, **update)
    finish = None
    if not unresolved:
        finish = max(ends.values(), default=calendar.start) + (calendar.buffer if ends else timedelta(0))
    # Even a partial forecast can establish that the period will be exceeded.
    earliest_finish = max(ends.values(), default=calendar.start)
    if earliest_finish + (calendar.buffer if ends else timedelta(0)) > calendar.end:
        risks.append("Project end at risk including buffer; review scope, capacity or period")
    if calendar.feature_freeze and earliest_finish > calendar.feature_freeze:
        risks.append("Work extends beyond feature freeze; confirm which deliverables must freeze")
    if unresolved:
        risks.append("Project finish unknown until unresolved tasks have forecasts")
    return Plan([updated[t.key] for t in tasks], calendar, now, allocations, owners, duration,
                paths, path_count, finish, risks)


def progress(plan: Plan) -> str:
    counts = Counter(t.status for t in plan.tasks)
    roles = defaultdict(list)
    for task in plan.tasks:
        if task.status not in {"Done", "Cancelled"}:
            roles[task.role].append(task)
    def effort(tasks):
        unknown = sum(t.remaining_hours is None for t in tasks)
        total = sum(t.remaining_hours or 0 for t in tasks)
        return f"{total:g} h" + (f" + {unknown} unknown" if unknown else "")
    pending = [t for tasks in roles.values() for t in tasks]
    critical = sorted(t.key for t in plan.tasks if t.metrics.get("total_float") == 0)
    local_finish = plan.finish.astimezone(plan.calendar.zone).isoformat(timespec="minutes") if plan.finish else "unknown"
    statuses = ("Todo", "In Progress", "In Review", "Blocked", "Done", "Cancelled")
    lines = ["Status: " + ", ".join(f"{s} {counts[s]}" for s in statuses),
             f"Team remaining: {effort(pending)}",
             "Remaining by role: " + (", ".join(f"{r} {effort(ts)}" for r, ts in sorted(roles.items())) or "none"),
             "Blockers: " + (", ".join(sorted(t.key for t in pending if t.status == "Blocked")) or "none"),
             "Dependency-only CPM: " + ("unknown" if plan.duration_hours is None else f"{plan.duration_hours:g} h"),
             "Critical tasks: " + (", ".join(critical) or "none"),
             f"Calendar finish including buffer: {local_finish}"]
    return "\n".join(lines + ["Risk: " + risk for risk in plan.risks])


def gantt(plan: Plan) -> bytes:
    """Render minute-positioned intervals and local-time labels to a Discord PNG."""
    from PIL import Image, ImageDraw, ImageFont

    cal = plan.calendar
    labels = [("start", cal.start), ("end", cal.end)]
    if cal.feature_freeze:
        labels.append(("freeze", cal.feature_freeze))
    labels += sorted(cal.milestones.items(), key=lambda item: (item[1], item[0]))
    if plan.finish:
        labels.append(("finish (buffer included)", plan.finish))
    points = [stamp for _, stamp in labels]
    rows = []
    for task in sorted(plan.tasks, key=lambda t: t.key):
        bars = []
        if task.baseline_start and task.baseline_end:
            bars.append(("baseline", _instant(task.baseline_start, cal.zone), _instant(task.baseline_end, cal.zone)))
        for start, end in plan.segments.get(task.key, []):
            bars.append(("critical" if task.metrics.get("total_float") == 0 else "forecast", start, end))
        if task.actual_start:
            start = _instant(task.actual_start, cal.zone)
            end = _instant(task.actual_end, cal.zone) if task.actual_end else max(start, plan.now)
            bars.append(("actual", start, end))
        deadline = _instant(task.deadline, cal.zone) if task.deadline else None
        if deadline:
            points.append(deadline)
        for _, start, end in bars:
            points.extend((start, end))
        rows.append((task.key, bars, deadline))

    lo, hi = min(points), max(points)
    width, left, right = 1120, 173, 1100
    header, row_height = 85, 35
    bottom = header + max(len(rows), 1) * row_height
    height = bottom + 34 + 16 * len(labels)
    image = Image.new("RGB", (width, height), "#ffffff")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    def label(x, y, value, color="#202938"):
        draw.text((x, y), value, fill=color, font=font)
    def xcoord(stamp):
        return left + round((stamp - lo) / (hi - lo) * (right - left))

    colors = {"baseline": "#9ba4b1", "forecast": "#267cc1", "critical": "#bf3e33", "actual": "#178153"}
    label(10, 7, f"SprintRelay schedule  |  {cal.zone}")
    for index, (name, color) in enumerate(colors.items()):
        x = 10 + index * 120
        draw.rectangle((x, 32, x + 20, 38), fill=color)
        label(x + 26, 28, name)
    label(510, 28, "triangle = task deadline   |   numbered lines = project dates")
    # At short events, label every hour (or quarter-hour); keep long events readable.
    span_minutes = (hi - lo).total_seconds() / 60
    steps = (15, 30, 60, 120, 240, 480, 720, 1440, 2880, 10080, 43200)
    step = next((n for n in steps if span_minutes / n <= 9), math.ceil(span_minutes / 9))
    tick_delta = timedelta(minutes=step)
    anchor = lo.astimezone(cal.zone).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    tick = anchor + math.ceil((lo - anchor) / tick_delta) * tick_delta
    while tick <= hi:
        x = xcoord(tick)
        draw.line((x, header - 2, x, bottom), fill="#d7dce3")
        label(min(x, width - 84), header - 20, tick.astimezone(cal.zone).strftime("%m/%d %H:%M"))
        tick += tick_delta
    # Project markers share the exact temporal scale with all bars.
    for number, (_, stamp) in enumerate(labels, 1):
        x = xcoord(stamp)
        draw.line((x, header - 4, x, bottom), fill="#73634f")
        label(x - 4, 45, str(number), "#73634f")
    lanes = {"baseline": 3, "forecast": 12, "critical": 12, "actual": 21}
    for i, (key, bars, deadline) in enumerate(rows):
        y = header + i * row_height
        display_key = key
        while display_key and draw.textlength(display_key, font=font) > left - 25:
            display_key = display_key[:-1]
        label(10, y + 7, display_key + ("..." if display_key != key else ""))
        draw.line((10, y + row_height - 1, right, y + row_height - 1), fill="#eceff3")
        for kind, start, end in bars:
            top = y + lanes[kind]
            x1, x2 = xcoord(start), xcoord(end)
            draw.rectangle((x1, top, max(x1 + 1, x2), top + 5), fill=colors[kind])
        if deadline:
            x = xcoord(deadline)
            draw.polygon([(x, y + 29), (x - 4, y + 34), (x + 4, y + 34)], fill="#853151")
    for number, (name, stamp) in enumerate(labels, 1):
        local = stamp.astimezone(cal.zone).strftime("%Y-%m-%d %H:%M %z")
        label(10, bottom + 8 + (number - 1) * 16, f"{number}. {name}   {local}")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()
