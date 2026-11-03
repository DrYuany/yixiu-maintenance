"""请求级 trace：把一次对话回合的可观测事实持久化下来。

一次 turn 的关键是“走了哪条路、调了哪些工具、数字来自哪个版本”，而不是聊天文本本身。
trace 记录这些事实，供前端审计证据页、评测打分器和在线监控回放。

一条 trace 至少包含：
  - requestId / conversationId：能按请求回放。
  - route：deterministic / tool_agent / solve_preview / clarify / solve / refuse。
  - toolCalls：每次工具调用的 tool / 参数 / 状态 / 延迟 / 是否缓存 / 是否重试。
  - model / tokens / modelFingerprint / latencyMs：模型与求解的可观测信息。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from app.db.database import Database, dumps, loads


class ToolCallRecord(BaseModel):
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: str | None = None
    latencyMs: float = 0.0
    cached: bool = False
    retried: bool = False


class AgentTrace(BaseModel):
    requestId: str
    conversationId: str
    route: str = "unknown"
    message: str = ""
    intent: str | None = None
    toolCalls: list[ToolCallRecord] = Field(default_factory=list)
    model: str | None = None
    tokens: dict[str, int] = Field(default_factory=dict)
    modelFingerprint: str | None = None
    latencyMs: float = 0.0
    # 这一轮是不是走了兜底。空表示正常；有值说明模型的答复没拿到，
    # 记录的是降级原因（agent_unavailable / model_question_fallback …）。
    # 有它才能回答"同一个问题昨天好好的今天答得干巴巴"这种问题。
    degraded: str | None = None
    createdAt: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def add_tool_call(self, record: ToolCallRecord) -> None:
        self.toolCalls.append(record)

    @property
    def tools_used(self) -> list[str]:
        return [call.tool for call in self.toolCalls]


class AgentTraceRepository:
    """SQLite 持久化 agent_traces。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def add(self, trace: AgentTrace) -> AgentTrace:
        self.database.execute(
            """
            INSERT INTO agent_traces (request_id, conversation_id, payload)
            VALUES (?, ?, ?)
            """,
            (trace.requestId, trace.conversationId, dumps(trace.model_dump())),
        )
        return trace

    def get(self, request_id: str) -> AgentTrace | None:
        row = self.database.fetch_one(
            "SELECT payload FROM agent_traces WHERE request_id = ?", (request_id,)
        )
        return AgentTrace(**loads(row["payload"])) if row else None

    def list_by_conversation(self, conversation_id: str, limit: int = 50) -> list[AgentTrace]:
        rows = self.database.fetch_all(
            "SELECT payload FROM agent_traces WHERE conversation_id = ? ORDER BY rowid DESC LIMIT ?",
            (conversation_id, max(1, int(limit))),
        )
        return [AgentTrace(**loads(row["payload"])) for row in rows]


def new_trace_id() -> str:
    return f"trace_{uuid4().hex[:12]}"
