from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from relay.config import Config
from relay.core import Task, apply_action, assign_owner, new_task, validate_tasks

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.toml"
NOW = datetime(2030, 1, 20, tzinfo=timezone.utc)


class TaskRulesTest(unittest.TestCase):
    def setUp(self):
        self.config = Config.load(EXAMPLE)
        self.proposal = dict(key="TASK-1", title="Fixture preview",
                             description="Render the prototype", role="design",
                             estimated_hours=1.5, requirement_ids=["REQ-1"],
                             deliverables=["Prototype"], acceptance=["Owner can open the prototype"])

    def test_design_claim_and_acceptance_gate(self):
        config, proposal = self.config, self.proposal
        task = new_task(proposal, config)
        assert task.remaining_hours == 1.5
        assert task.assignee is None
        assert Task.from_dict(task.to_dict()) == task
        with self.assertRaises(PermissionError):
            apply_action(task, "claim", "10104", config)
        claimed = apply_action(task, "claim", "10102", config)
        with self.assertRaisesRegex(ValueError, "claimed"):
            apply_action(claimed, "claim", "10101", config)
        with self.assertRaises(PermissionError):
            apply_action(claimed, "start", "10101", config)
        started = apply_action(claimed, "start", "10102", config, now=NOW)
        with self.assertRaises(ValueError):
            apply_action(started, "done", "10101", config, "looks good")
        with self.assertRaisesRegex(ValueError, "URL"):
            apply_action(started, "review", "10102", config)
        review = apply_action(started, "review", "10102", config, "https://example.org/design")
        with self.assertRaises(PermissionError):
            apply_action(review, "done", "10102", config, "approve")
        with self.assertRaises(ValueError):
            apply_action(review, "done", "10101", config)
        rejected = apply_action(review, "reject", "10101", config, "Missing mobile layout")
        assert rejected.status == "In Progress"
        review = apply_action(rejected, "review", "10102", config)
        done = apply_action(review, "done", "10101", config, "Prototype opened", now=NOW)
        assert (done.status, done.remaining_hours) == ("Done", 0)
        assert done.actual_start == NOW.isoformat() == done.actual_end
        with self.assertRaises(ValueError):
            apply_action(done, "cancel", "10101", config, "obsolete")

    def test_engineering_assignment_and_unknown_estimate(self):
        config, proposal = self.config, self.proposal
        task = new_task(dict(proposal, role="hardware"), config)
        assert (task.assignee, task.repo) == ("10105", config.default_repo)
        with self.assertRaisesRegex(ValueError, "assignee"):
            assign_owner(replace(task, assignee="10104"), config)
        for status in ("Todo", "In Progress", "In Review"):
            blocked = apply_action(replace(task, status=status), "block", "10105", config, "Parts delayed")
            assert blocked.block_reason == "Parts delayed"
            assert apply_action(blocked, "unblock", "10105", config).status == status
        task = apply_action(task, "estimate", "10105", config, "unknown")
        assert Task.from_dict(task.to_dict()).remaining_hours is None
        assert apply_action(task, "estimate", "10105", config, "0.5").remaining_hours == 0.5
        for hours in (False, "NaN", "inf", -0.1):
            with self.assertRaises(ValueError):
                apply_action(task, "estimate", "10105", config, hours)

    def test_untrusted_task_proposals(self):
        cases = [
            {"key": ""}, {"role": "admin"}, {"estimated_hours": 8.1}, {"estimated_hours": True},
            {"estimated_hours": 0.49}, {"requirement_ids": []}, {"deliverables": []}, {"acceptance": []},
            {"assignee": "10105"}, {"status": "Done"}, {"repo": "other/unknown"}, {"issue": 42},
            {"deadline": "2030-01-01"}, {"dependencies": "TASK-2"}, {"priority": True},
        ]
        for edits in cases:
            with self.subTest(edits=edits), self.assertRaises(ValueError):
                new_task(dict(self.proposal, **edits), self.config)

    def test_task_estimated_effort_accepts_full_range(self):
        for hours in (0.5, 1, 4, 4.5, 8):
            with self.subTest(hours=hours):
                task = new_task(dict(self.proposal, estimated_hours=hours), self.config)
                self.assertEqual(task.estimated_hours, hours)
                self.assertEqual(task.remaining_hours, hours)

    def test_validate_dependency_graph(self):
        config, proposal = self.config, self.proposal
        a = new_task(proposal, config)
        b = replace(a, key="TASK-2", dependencies=[a.key])
        validate_tasks([b, a])
        for tasks, error in (([a, a], "duplicate"), ([b], "unknown"),
                             ([replace(a, dependencies=[b.key]), b], "cycle")):
            with self.assertRaisesRegex(ValueError, error):
                validate_tasks(tasks)

    def test_config_keeps_distinct_roles_repos_and_identities(self):
        config = self.config
        with tempfile.TemporaryDirectory() as tmpdir:
            self.check_invalid_configs(config, Path(tmpdir))

    def check_invalid_configs(self, config, tmp_path):
        assert config.members["10103"] == "frontend"
        assert config.github_users.get("10101") is None
        assert config.calendar["timezone"] == "Asia/Tokyo"
        text = EXAMPLE.read_text()
        for before, after in (("backend-example", "Frontend-Example"),
                              ("10105", "10104"), ("example/relay-management", config.default_repo)):
            path = tmp_path / "bad.toml"
            path.write_text(text.replace(before, after, 1))
            with self.assertRaises(ValueError):
                Config.load(path)
