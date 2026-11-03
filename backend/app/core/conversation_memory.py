"""会话记忆快照：把散落在 messages / versions / working_memory 里的结构化事实
聚合成一份有界、可回查的记忆，作为每轮 LLM 系统提示词的背景补充。

不做向量检索、不做用户画像。确定性为主（复用 agent_memory 的正则抽取），
LLM 摘要可选。快照落库 + 增量可重建，冷启动时从现有表重算。
"""
from __future__ import annotations

import re
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from app.core.agent_memory import (
    extract_constraints,
    extract_rejected_plans,
    extract_todos,
)
from app.db.database import Database, dumps, loads
from app.schemas.agent import Message, WindowVersion, WorkingMemory

# 有界参数（默认值，可调）
RECENT_MESSAGES = 8
RECENT_ACTIONS = 10
RECENT_QA = 5
RECENT_ARTIFACTS = 5
SUMMARY_MAX_CHARS = 800
QA_ANSWER_MAX_CHARS = 200
QA_QUESTION_MAX_CHARS = 120

# 问句判定：与路由层的问句守卫口径一致，用于从 messages 里抽取问答对。
_QUESTION_RE = re.compile(
    r"[吗么呢][?？]?$|是不是|能不能|行不行|什么时候|多久|怎么|为什么|为啥|如何|哪些|有多少"
)


class ConversationMemory(BaseModel):
    conversationId: str
    messageCount: int = 0
    state: dict[str, Any] = Field(default_factory=dict)
    constraints: list[str] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    actions: list[dict[str, Any]] = Field(default_factory=list)
    qa: list[dict[str, Any]] = Field(default_factory=list)
    summary: str = ""
    llmSummary: str = ""
    updatedAt: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def _solver_status(version: WindowVersion) -> str:
    solver = (version.resultSummary or {}).get("solver")
    if isinstance(solver, dict):
        return str(
            solver.get("解状态") or solver.get("求解状态") or solver.get("状态") or "UNKNOWN"
        )
    return "UNKNOWN"


def _artifact(version: WindowVersion) -> dict[str, Any]:
    summary = version.resultSummary or {}
    return {
        "versionId": version.id,
        "strategy": summary.get("strategy") or version.focusTag,
        "scheduledTaskCount": summary.get("scheduledTaskCount"),
        "totalTaskCount": summary.get("totalTaskCount"),
        "totalCost": summary.get("totalCost"),
        "status": _solver_status(version),
        "at": version.generatedAt,
    }


def _extract_qa(messages: list[Message]) -> list[dict[str, Any]]:
    """从 messages 里配对「用户问句 + 紧随其后的助手回答」。"""
    qa: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if message.role != "user":
            continue
        text = message.content.strip()
        if not _QUESTION_RE.search(text):
            continue
        answer = next(
            (item for item in messages[index + 1 :] if item.role == "assistant"),
            None,
        )
        if answer is None:
            continue
        snippet = answer.content.strip()
        if len(snippet) > QA_ANSWER_MAX_CHARS:
            snippet = snippet[:QA_ANSWER_MAX_CHARS] + "…"
        qa.append(
            {
                "q": text[:QA_QUESTION_MAX_CHARS],
                "a": snippet,
                "versionId": (answer.versionIds or [None])[0],
                "at": answer.createdAt,
            }
        )
    return qa


def build_memory_snapshot(
    conversation_id: str,
    messages: list[Message],
    memory: WorkingMemory,
    versions: list[WindowVersion],
    *,
    llm_summary: str = "",
) -> ConversationMemory:
    """确定性构建会话记忆快照：约束 + 产物 + 动作 + 问答 + 摘要 + 状态。"""
    constraints = extract_constraints(messages)
    artifacts = [_artifact(version) for version in versions]
    actions = [
        {
            "type": "solve",
            "versionId": version.id,
            "status": _solver_status(version),
            "at": version.generatedAt,
        }
        for version in versions
    ]
    qa = _extract_qa(messages)
    rejected = extract_rejected_plans(messages)
    todos = extract_todos(messages)

    parts: list[str] = []
    if constraints:
        parts.append("已确认约束：" + "；".join(constraints))
    if memory.latestResultId:
        parts.append(f"最近有效方案：{memory.latestResultId}")
    if rejected:
        parts.append("已否决：" + "；".join(rejected))
    if todos:
        parts.append("待办：" + "；".join(todos))
    parts.append("（本摘要为压缩记录，非完整对话，具体数字以方案与工具为准。）")

    return ConversationMemory(
        conversationId=conversation_id,
        messageCount=len(messages),
        state={
            "currentGoal": memory.currentGoal,
            "currentIntent": memory.currentIntent,
            "activeView": memory.activeView,
            "activeFocusTag": memory.activeFocusTag,
            "selectedVersionIds": memory.selectedVersionIds,
            "activeFilters": memory.activeFilters,
            "latestResultId": memory.latestResultId,
            "pendingClarification": memory.pendingClarification,
        },
        constraints=constraints,
        artifacts=artifacts,
        actions=actions,
        qa=qa,
        summary="\n".join(parts),
        llmSummary=llm_summary,
    )


def _fmt_count(artifact: dict[str, Any]) -> str:
    scheduled = artifact.get("scheduledTaskCount")
    total = artifact.get("totalTaskCount")
    if scheduled is None and total is None:
        return "任务数未记录"
    return f"{scheduled}/{total} 项"


def _fmt_cost(artifact: dict[str, Any]) -> str:
    try:
        return f"{float(artifact.get('totalCost')) / 10_000:.2f} 万元"
    except (TypeError, ValueError):
        return "成本未记录"


def render_memory_context(snapshot: ConversationMemory) -> str:
    """把快照渲染成系统提示词里的一段有界背景，替代「塞全量历史」。"""
    blocks: list[str] = []
    if snapshot.constraints:
        blocks.append("已确认约束：" + "；".join(snapshot.constraints))
    if snapshot.artifacts:
        lines = [
            f"- {artifact['versionId']}：{artifact['strategy'] or '未记录'}，"
            f"{_fmt_count(artifact)}，{_fmt_cost(artifact)}，状态 {artifact['status']}"
            for artifact in snapshot.artifacts[-RECENT_ARTIFACTS:]
        ]
        blocks.append("本会话已产生的方案（产物）：\n" + "\n".join(lines))
    if snapshot.actions:
        lines = [
            f"- {str(action['at'])[:16].replace('T', ' ')} {action['type']} "
            f"{action['versionId']} → {action['status']}"
            for action in snapshot.actions[-RECENT_ACTIONS:]
        ]
        blocks.append("动作时间线：\n" + "\n".join(lines))
    if snapshot.qa:
        lines = [f"- 问：{qa['q']}\n  答：{qa['a']}" for qa in snapshot.qa[-RECENT_QA:]]
        blocks.append("近期问答：\n" + "\n".join(lines))
    if snapshot.llmSummary:
        blocks.append("会话背景（LLM 摘要）：\n" + snapshot.llmSummary[:SUMMARY_MAX_CHARS])
    if snapshot.summary:
        blocks.append("会话摘要：\n" + snapshot.summary[:SUMMARY_MAX_CHARS])
    return "\n\n".join(blocks)


class ConversationMemoryCache:
    """进程内 LRU：按 conversation_id 缓存最近构建的快照。

    仅当快照的 messageCount 与当前消息数一致时命中（消息未变），否则重建。
    """

    def __init__(self, maxsize: int = 128) -> None:
        self._maxsize = maxsize
        self._items: OrderedDict[str, ConversationMemory] = OrderedDict()

    def get(self, conversation_id: str, message_count: int) -> ConversationMemory | None:
        item = self._items.get(conversation_id)
        if item is not None and item.messageCount == message_count:
            self._items.move_to_end(conversation_id)
            return item
        return None

    def put(self, memory: ConversationMemory) -> None:
        self._items[memory.conversationId] = memory
        self._items.move_to_end(memory.conversationId)
        while len(self._items) > self._maxsize:
            self._items.popitem(last=False)


class ConversationMemoryRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def upsert(self, memory: ConversationMemory) -> ConversationMemory:
        self.database.execute(
            """
            INSERT INTO conversation_memory (conversation_id, payload)
            VALUES (?, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET payload = excluded.payload
            """,
            (memory.conversationId, dumps(memory.model_dump())),
        )
        return memory

    def get(self, conversation_id: str) -> ConversationMemory | None:
        row = self.database.fetch_one(
            "SELECT payload FROM conversation_memory WHERE conversation_id = ?",
            (conversation_id,),
        )
        return ConversationMemory(**loads(row["payload"])) if row else None
