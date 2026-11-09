"""Tests for the Student AI Agent. Need a PostgreSQL DATABASE_URL with student data (see tests/README)."""
import os
from types import SimpleNamespace as NS

import pytest

import student_agent as agent

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")


def test_rejects_unknown_operator_and_field():
    assert "not allowed" in agent.run_tool("query_students", {"filters": [{"field": "name", "op": "; drop table x", "value": 1}]}, {})["error"]
    assert "Unknown field" in agent.run_tool("query_students", {"filters": [{"field": "password", "op": "eq", "value": 1}]}, {})["error"]
    assert "Unknown field" in agent.run_tool("query_students", {"group_by": ["name; drop"]}, {})["error"]


def test_group_counts_match_total():
    r = agent.run_tool("query_students", {"group_by": ["batch"], "metrics": [{"fn": "count"}]}, {})
    assert sum(x["students"] for x in r["rows"]) == r["matching_students"]


def test_ui_filters_are_always_applied():
    all_ = agent.run_tool("query_students", {}, {})["matching_students"]
    some = agent.run_tool("query_students", {}, {"state": "Telangana"})["matching_students"]
    assert 0 < some < all_


class FakeClient:
    """Simulates Claude: first asks for a grouped query, then answers."""
    def __init__(self):
        self.calls = 0
        self.messages = self

    def create(self, **kw):
        self.calls += 1
        if self.calls == 1:
            return NS(stop_reason="tool_use", content=[NS(type="tool_use", id="t1", name="query_students",
                      input={"filters": [{"field": "due", "op": "gt", "value": 0}], "group_by": ["state"], "metrics": [{"fn": "count"}]})])
        last = kw["messages"][-1]["content"][0]
        assert last["type"] == "tool_result" and not last["is_error"]
        return NS(stop_reason="end_turn", content=[NS(type="text", text="Telangana has the most students with dues.\nFOLLOWUPS: Show them | Dues by batch | Total due")])


def test_agent_loop_returns_table_chart_and_followups(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(agent, "_client", lambda: fake)
    out = agent.answer("Which state has most students with dues?", [], {})
    assert out["mode"] == "ai" and out["answer"].startswith("Telangana")
    assert out["followups"] == ["Show them", "Dues by batch", "Total due"]
    assert out["rows"] and out["chart"] and out["chart"]["value"] == "students"
    assert out["steps"][0]["tool"] == "query_students"
