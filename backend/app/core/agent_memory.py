"""分层记忆：在会话状态（WorkingMemory）之上补三层持久化记忆。

排程域的记忆价值在“结构化决策”，不在“语义相似的历史句子”，所以这里不做向量检索，
只把会话里能确定下来的约束、偏好和决策结论存成可回查的结构。

三层：
  1. 会话摘要（conversation_summary）：消息超阈值后压缩成“已确认约束 / 已否决方案 /
     待办 / 最近结论”，解决长对话丢上下文。
  2. 用户偏好（user_profile）：默认策略、风险偏好、常用船线等，支撑“按老规矩排”。
  3. 决策记忆（decision_memories）：某次方案的关键结论，支撑“上次类似情况怎么排的”。

摘要默认走确定性提取（正则抽取约束 + 最近版本事实 + 滚动窗口），可选注入 LLM，
但 LLM 缺失时绝不阻塞。摘要永远标为“非完整记录”，不冒充权威事实。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from app.db.database import Database, dumps, loads


class ConversationSummary(BaseModel):
    conversationId: str
    summaryText: str = ""
    confirmedConstraints: list[str] = Field(default_factory=list)
    rejectedPlans: list[str] = Field(default_factory=list)
    openTodos: list[str] = Field(default_factory=list)
    latestVersionId: str | None = None
    messageCount: int = 0
    updatedAt: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class UserProfile(BaseModel):
    userId: str
    defaultStrategy: str | None = None
    riskPreference: str | None = None
    commonLines: list[str] = Field(default_factory=list)
    preferences: dict[str, Any] = Field(default_factory=dict)
    updatedAt: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class DecisionMemory(BaseModel):
    conversationId: str
    strategy: str
    conclusion: str
    versionId: str | None = None
    createdAt: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ── 确定性约束抽取 ────────────────────────────────────────────────────────────

_CONSTRAINT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("work_hours", re.compile(r"每天\s*最多\s*(\d+)\s*小时")),
    ("work_lines", re.compile(r"最多\s*(\d+)\s*条\s*(?:作业)?线")),
    ("ship_rent", re.compile(r"船租\s*(?:上涨|涨|上调)?\s*(\d+(?:\.\d+)?)\s*%")),
    ("strategy", re.compile(r"(?:先)?按\s*(高风险优先抢修|成本最低优先|最短完成优先|检修最多器件优先)\s*(?:优先)?")),
]

_REJECT_HINTS = ("取消", "不要", "否了", "作废", "撤销")
_TODO_HINTS = ("待确认", "待审批", "待补", "还要")


def extract_constraints(messages: list[Any]) -> list[str]:
    """从用户消息里抽取可确认的排程约束，返回规范化中文描述。

    只抽取明确量化的说法，不凭语义猜测；命中的约束去重后按出现顺序返回。
    """
    found: list[str] = []
    seen: set[str] = set()
    for message in messages:
        text = getattr(message, "content", message)
        if not isinstance(text, str):
            continue
        for key, pattern in _CONSTRAINT_PATTERNS:
            for match in pattern.finditer(text):
                if key == "work_hours":
                    item = f"每天最多 {match.group(1)} 小时"
                elif key == "work_lines":
                    item = f"最多 {match.group(1)} 条作业线"
                elif key == "ship_rent":
                    item = f"船租上涨 {match.group(1)}%"
                else:
                    item = f"策略 {match.group(1)}"
                if item not in seen:
                    seen.add(item)
                    found.append(item)
    return found


def extract_rejected_plans(messages: list[Any]) -> list[str]:
    rejected: list[str] = []
    seen: set[str] = set()
    for message in messages:
        text = getattr(message, "content", message)
        if not isinstance(text, str) or not any(hint in text for hint in _REJECT_HINTS):
            continue
        # 只保留含方案/版本/这次等指代的句子，避免把“不要发送密码”误收进来。
        if not re.search(r"方案|版本|这版|这次|计划|排程", text):
            continue
        snippet = text.strip()
        if len(snippet) > 80:
            snippet = snippet[:80] + "…"
        if snippet not in seen:
            seen.add(snippet)
            rejected.append(snippet)
    return rejected


def extract_todos(messages: list[Any]) -> list[str]:
    todos: list[str] = []
    seen: set[str] = set()
    for message in messages:
        text = getattr(message, "content", message)
        if not isinstance(text, str) or not any(hint in text for hint in _TODO_HINTS):
            continue
        snippet = text.strip()
        if len(snippet) > 80:
            snippet = snippet[:80] + "…"
        if snippet not in seen:
            seen.add(snippet)
            todos.append(snippet)
    return todos


def build_summary(
    conversation_id: str,
    messages: list[Any],
    latest_version_id: str | None,
) -> ConversationSummary:
    """确定性摘要：约束 + 否决 + 待办 + 最近结论，标注非完整记录。"""
    constraints = extract_constraints(messages)
    rejected = extract_rejected_plans(messages)
    todos = extract_todos(messages)
    parts: list[str] = []
    if constraints:
        parts.append("已确认约束：" + "；".join(constraints))
    if latest_version_id:
        parts.append(f"最近有效方案：{latest_version_id}")
    if rejected:
        parts.append("已否决：" + "；".join(rejected))
    if todos:
        parts.append("待办：" + "；".join(todos))
    parts.append("（本摘要为压缩记录，非完整对话，具体数字以方案与工具为准。）")
    return ConversationSummary(
        conversationId=conversation_id,
        summaryText="\n".join(parts),
        confirmedConstraints=constraints,
        rejectedPlans=rejected,
        openTodos=todos,
        latestVersionId=latest_version_id,
        messageCount=len(messages),
    )


# ── 仓储 ──────────────────────────────────────────────────────────────────────

class ConversationSummaryRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def upsert(self, summary: ConversationSummary) -> ConversationSummary:
        self.database.execute(
            """
            INSERT INTO conversation_summary (conversation_id, payload)
            VALUES (?, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET payload = excluded.payload
            """,
            (summary.conversationId, dumps(summary.model_dump())),
        )
        return summary

    def get(self, conversation_id: str) -> ConversationSummary | None:
        row = self.database.fetch_one(
            "SELECT payload FROM conversation_summary WHERE conversation_id = ?",
            (conversation_id,),
        )
        return ConversationSummary(**loads(row["payload"])) if row else None


class UserProfileRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def upsert(self, profile: UserProfile) -> UserProfile:
        self.database.execute(
            """
            INSERT INTO user_profile (user_id, payload)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET payload = excluded.payload
            """,
            (profile.userId, dumps(profile.model_dump())),
        )
        return profile

    def get(self, user_id: str) -> UserProfile | None:
        row = self.database.fetch_one(
            "SELECT payload FROM user_profile WHERE user_id = ?", (user_id,)
        )
        return UserProfile(**loads(row["payload"])) if row else None


class DecisionMemoryRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def add(self, decision: DecisionMemory) -> DecisionMemory:
        self.database.execute(
            """
            INSERT INTO decision_memories (conversation_id, strategy, payload)
            VALUES (?, ?, ?)
            """,
            (decision.conversationId, decision.strategy, dumps(decision.model_dump())),
        )
        return decision

    def list_by_conversation(self, conversation_id: str, limit: int = 50) -> list[DecisionMemory]:
        rows = self.database.fetch_all(
            "SELECT payload FROM decision_memories WHERE conversation_id = ? ORDER BY rowid DESC LIMIT ?",
            (conversation_id, max(1, int(limit))),
        )
        return [DecisionMemory(**loads(row["payload"])) for row in rows]
