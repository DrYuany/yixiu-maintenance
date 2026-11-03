from __future__ import annotations

from pathlib import Path

from app.core.agent_trace import AgentTrace, AgentTraceRepository, ToolCallRecord, new_trace_id
from app.db.database import Database


def _trace(conversation_id: str = "conv_1") -> AgentTrace:
    trace = AgentTrace(
        requestId=new_trace_id(),
        conversationId=conversation_id,
        route="assistant_reply",
        message="对比方案01和02",
    )
    trace.add_tool_call(
        ToolCallRecord(
            tool="read_versions",
            arguments={"versions": ["方案01", "方案02"]},
            status="ok",
            latencyMs=12.3,
        )
    )
    return trace


def test_new_trace_id_has_prefix() -> None:
    assert new_trace_id().startswith("trace_")


def test_repository_roundtrip(temp_db_path: Path) -> None:
    repo = AgentTraceRepository(Database(temp_db_path))
    trace = _trace()
    repo.add(trace)

    got = repo.get(trace.requestId)
    assert got is not None
    assert got.requestId == trace.requestId
    assert got.route == "assistant_reply"
    assert got.tools_used == ["read_versions"]
    assert got.toolCalls[0].status == "ok"
    assert got.toolCalls[0].latencyMs == 12.3


def test_repository_lists_by_conversation(temp_db_path: Path) -> None:
    repo = AgentTraceRepository(Database(temp_db_path))
    repo.add(_trace("conv_a"))
    repo.add(_trace("conv_a"))
    repo.add(_trace("conv_b"))

    assert len(repo.list_by_conversation("conv_a")) == 2
    assert len(repo.list_by_conversation("conv_b")) == 1
    assert len(repo.list_by_conversation("conv_missing")) == 0


def test_repository_get_missing_returns_none(temp_db_path: Path) -> None:
    repo = AgentTraceRepository(Database(temp_db_path))
    assert repo.get("trace_nope") is None


