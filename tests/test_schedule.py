import unittest
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

from relay.core import Task
from relay.schedule import Calendar, gantt, progress, recalculate


def at(hour, minute=0):
    return datetime.fromisoformat(f"2030-02-10T{hour:02}:{minute:02}:00+09:00")


def task(key, hours=1, role="backend", dependencies=(), **changes):
    return replace(Task(key, key, "test fixture", role, hours, hours,
                        dependencies=list(dependencies), acceptance=["verified"],
                        deliverables=["artifact"], requirement_ids=["R1"]), **changes)


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.members = {"d1": "design", "d2": "design", "fe": "frontend", "be": "backend", "hw": "hardware"}
        self.calendar = {
            "start": "2030-02-10T09:00", "end": "2030-02-10T21:00",
            "feature_freeze": at(19), "buffer_hours": 0.5, "milestones": {"demo": at(20)},
            "availability": {p: [[at(9), at(12)], [at(13), at(21)]] for p in self.members},
        }
        self.cfg = SimpleNamespace(members=self.members, calendar=self.calendar)

    def run_plan(self, tasks, *, now=None):
        return recalculate(tasks, self.cfg, now=now or at(9))

    def test_cpm_ignores_capacity_and_keeps_parallel_work(self):
        tasks = [task("build", 2), task("skin", 1, "frontend"),
                 task("verify", 1, "backend", ["build", "skin"]), task("other", 1)]
        plan = self.run_plan(tasks)
        self.assertEqual(plan.duration_hours, 3)
        self.assertEqual(plan.critical_paths, [["build", "verify"]])
        self.assertEqual(plan.tasks[1].metrics, {"es": 0, "ef": 1, "lf": 2, "ls": 1, "total_float": 1})
        self.assertEqual(plan.segments["other"], [(at(11), at(12))])
        self.assertEqual(plan.segments["verify"], [(at(13), at(14))])
        self.assertEqual(plan.segments["skin"], [(at(9), at(10))])
        self.assertEqual(plan.finish, at(14, 30))
        self.assertEqual(self.run_plan(reversed(tasks)).segments, plan.segments)

    def test_equal_critical_paths(self):
        plan = self.run_plan([task("A"), task("B", dependencies=["A"]),
                              task("C", dependencies=["A"]), task("D", dependencies=["B", "C"])])
        self.assertEqual(plan.path_count, 2)
        self.assertEqual(plan.critical_paths, [["A", "B", "D"], ["A", "C", "D"]])

    def test_gaps_before_future_reservation_are_reused(self):
        tasks = [task("a", 2, "frontend"), task("b", 1, dependencies=["a"]), task("c", 2.25)]
        plan = self.run_plan(tasks)
        self.assertEqual(plan.segments["b"], [(at(11), at(12))])
        self.assertEqual(plan.segments["c"], [(at(9), at(11)), (at(13), at(13, 15))])

    def test_design_capacity_is_tentative_and_respects_claim(self):
        tasks = [task("a", 2, "design", assignee="d2"), task("b", 2, "design"), task("c", 2, "design")]
        plan = self.run_plan(tasks)
        self.assertEqual(plan.owners, {"a": "d2", "b": "d1", "c": "d1"})
        self.assertIsNone(plan.tasks[1].assignee)
        self.assertEqual(plan.segments["c"], [(at(11), at(12)), (at(13), at(14))])
        for owner in self.members:
            intervals = sorted(seg for k, p in plan.owners.items() if p == owner for seg in plan.segments[k])
            self.assertTrue(all(prev[1] <= nxt[0] for prev, nxt in zip(intervals, intervals[1:])))

    def test_missing_unknown_blocked_and_cancelled_dependencies(self):
        for cause in ({"status": "Blocked"}, {"status": "Cancelled"}, {"remaining_hours": None},
                      {"status": "Done", "actual_end": None}):
            with self.subTest(cause=cause):
                plan = self.run_plan([task("a", **cause), task("b", dependencies=["a"]),
                                      task("independent", role="frontend")])
                self.assertNotIn("b", plan.segments)
                self.assertIn("independent", plan.segments)
                self.assertTrue(plan.risks)
                self.assertIsNone(plan.finish)
                self.assertIsNone(plan.tasks[1].forecast_start)

    def test_actual_baseline_deadlines_and_period_change(self):
        origin = self.run_plan([task("a", 2), task("b", 2, dependencies=["a"], deadline=at(16).isoformat())])
        done = replace(origin.tasks[0], status="Done", actual_start=at(9).isoformat(),
                       actual_end=at(15).isoformat(), remaining_hours=0)
        self.cfg.calendar["end"] = at(17)
        changed = self.run_plan([done, origin.tasks[1]], now=at(15, 1))
        self.assertEqual(changed.tasks[1].baseline_end, origin.tasks[1].baseline_end)
        self.assertEqual(changed.tasks[0].actual_end, done.actual_end)
        self.assertEqual(changed.tasks[1].deadline, at(16).isoformat())
        self.assertEqual(changed.segments["b"], [(at(15, 1), at(17, 1))])
        self.assertEqual(changed.finish, at(17, 31))
        self.assertIn("misses task deadline", progress(changed))
        self.assertIn("Project end at risk", progress(changed))
        self.assertEqual(self.cfg.calendar["end"], at(17))
        self.assertTrue(gantt(changed).startswith(b"\x89PNG\r\n\x1a\n"))

    def test_unavailable_calendar_and_bad_graph(self):
        del self.calendar["availability"]["be"]
        plan = self.run_plan([task("a")])
        self.assertIsNone(plan.finish)
        self.assertIsNone(plan.tasks[0].baseline_start)
        for tasks in ([task("a"), task("a")], [task("a", dependencies=["a"])],
                      [task("a", dependencies=["absent"])]):
            with self.assertRaises(ValueError):
                self.run_plan(tasks)

    def test_overlapping_availability_not_double_counted(self):
        self.calendar["availability"]["be"] = [[at(9), at(10)], [at(9, 30), at(10, 30)]]
        self.assertIsNone(self.run_plan([task("a", 2)]).finish)

    def test_timezones_dst_and_utc_input(self):
        self.assertEqual(self.run_plan([task("a")], now=datetime.fromisoformat("2030-02-10T00:01Z")).finish,
                         at(10, 31))
        self.calendar.update(timezone="America/New_York", start="2030-03-10T02:30")
        with self.assertRaisesRegex(ValueError, "Nonexistent"):
            Calendar.from_dict(self.calendar)
        self.calendar.update(start="2030-11-03T01:30")
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            Calendar.from_dict(self.calendar)

    def test_buffer_applied_once_after_parallel_work(self):
        self.assertEqual(self.run_plan([task("a"), task("b", role="hardware")]).finish, at(10, 30))
        overdue = self.run_plan([task("a", status="Blocked", deadline=at(10).isoformat())], now=at(11))
        self.assertTrue(any("deadline" in r for r in overdue.risks))
