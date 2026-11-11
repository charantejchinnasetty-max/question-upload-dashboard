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
    """Simulates Claude: runs a SQL query, then answers."""
    def __init__(self):
        self.calls = 0
        self.messages = self

    def create(self, **kw):
        self.calls += 1
        if self.calls == 1:
            assert "students" in kw["system"] and kw["tools"][0]["name"] == "run_sql"
            return NS(stop_reason="tool_use", content=[NS(type="tool_use", id="t1", name="run_sql",
                      input={"sql": "select state, count(*) as students from students where due > 0 group by 1 order by 2 desc", "purpose": "dues by state"})])
        last = kw["messages"][-1]["content"][0]
        assert last["type"] == "tool_result" and not last["is_error"], last
        return NS(stop_reason="end_turn", content=[NS(type="text", text="Andhra Pradesh has the most students with dues.\nFOLLOWUPS: Show them | Dues by batch | Total due")])


def test_agent_loop_returns_table_chart_and_followups(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    fake = FakeClient()
    monkeypatch.setattr(agent, "_client", lambda: fake)
    out = agent.answer("Which state has most students with dues?", [], {})
    assert out["mode"] == "ai" and out["answer"].startswith("Andhra")
    assert out["followups"] == ["Show them", "Dues by batch", "Total due"]
    assert out["rows"] and out["chart"] and out["chart"]["value"] == "students"
    assert out["steps"][0]["tool"] == "dues by state"


def test_openai_compatible_loop(monkeypatch):
    """Vercel AI Gateway / OpenAI path: tool call -> tool result -> final answer."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "test")
    seen = []

    def fake_post(url, key, body):
        seen.append((url, body))
        if len(seen) == 1:
            return {"choices": [{"message": {"content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {
                "name": "run_sql", "arguments": '{"sql": "select count(*) as n from students where college_group ilike \'%kadapa%\'"}'}}]}}]}
        tool_msg = body["messages"][-1]
        assert tool_msg["role"] == "tool" and '"n": 32' in tool_msg["content"], tool_msg
        return {"choices": [{"message": {"content": "**32 students** are from Kadapa colleges."}}]}

    monkeypatch.setattr(agent, "_post_json", fake_post)
    out = agent.answer("students from kadapa", [], {})
    assert seen[0][0] == "https://ai-gateway.vercel.sh/v1/chat/completions"
    assert seen[0][1]["model"] == "anthropic/claude-sonnet-5.5"
    assert "32 students" in out["answer"] and out["provider"] == "Claude via Vercel AI Gateway"


def test_sql_tool_is_read_only_and_scoped():
    for bad in ["delete from students", "select * from student_records", "select 1; select 2", "select pg_sleep(5)",
                "select * from public.student_records", "with x as (insert into t values (1) returning *) select 1"]:
        assert "error" in agent.run_tool("run_sql", {"sql": bad}, {}), bad
    ok = agent.run_tool("run_sql", {"sql": "select count(*) as n from students where name ilike '%update%'"}, {})
    assert "error" not in ok
    all_ = agent.run_tool("run_sql", {"sql": "select count(*) as n from students"}, {})["rows"][0]["n"]
    tg = agent.run_tool("run_sql", {"sql": "select count(*) as n from students"}, {"state": "Telangana"})["rows"][0]["n"]
    assert 0 < tg < all_
    merged = agent.run_tool("run_sql", {"sql": "select college_group, count(*) as n from students where college_group ilike '%kadapa%' and college_group ilike '%government%' group by 1"}, {})
    assert merged["rows"] == [{"college_group": "Government Medical College, Kadapa", "n": 26}]
