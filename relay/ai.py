"""Validate agent submissions using the MCP wire schemas; no model transport."""
from pydantic import Field

from .mcp_contracts import Checks, Minutes, Record, TaskProposal, Texts


class Review(Record):
    checks: Checks
    blocking_questions: Texts


class TaskPlan(Record):
    tasks: list[TaskProposal] = Field(min_length=1, max_length=200)
