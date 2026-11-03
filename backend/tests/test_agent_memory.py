from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.core.agent_memory import (
    ConversationSummary,
    ConversationSummaryRepository,
    DecisionMemory,
    DecisionMemoryRepository,
    UserProfile,
    UserProfileRepository,
    build_summary,
    extract_constraints,
    extract_rejected_plans,
    extract_todos,
)
from app.db.database import Database


def _msg(content: str):
    return SimpleNamespace(content=content)


def test_extract_constraints_normalizes_known_patterns() -> None:
    messages = [
        _msg("每天最多 8 小时，最多 2 条作业线"),
        _msg("船租上涨 20%，先按高风险优先抢修排"),
    ]
    constraints = extract_constraints(messages)
    assert "每天最多 8 小时" in constraints
    assert "最多 2 条作业线" in constraints
    assert "船租上涨 20%" in constraints
    assert "策略 高风险优先抢修" in constraints


def test_extract_constraints_deduplicates() -> None:
    messages = [_msg("每天最多 8 小时"), _msg("再强调每天最多 8 小时")]
    assert extract_constraints(messages) == ["每天最多 8 小时"]


def test_extract_rejected_plans_only_catches_plan_references() -> None:
    messages = [
        _msg("取消方案02，这次不要了"),
        _msg("请不要发送密码"),
    ]
    rejected = extract_rejected_plans(messages)
    assert any("取消方案02" in item for item in rejected)
    assert not any("密码" in item for item in rejected)


def test_build_summary_marks_as_compressed() -> None:
    messages = [_msg("每天最多 8 小时"), _msg("先按成本最低优先排")]
    summary = build_summary("conv_1", messages, "ver_00000001")
    assert summary.latestVersionId == "ver_00000001"
    assert summary.messageCount == 2
    assert "每天最多 8 小时" in summary.summaryText
    assert "非完整对话" in summary.summaryText


def test_conversation_summary_repository_roundtrip(temp_db_path: Path) -> None:
    repo = ConversationSummaryRepository(Database(temp_db_path))
    summary = build_summary("conv_1", [_msg("每天最多 8 小时")], None)
    repo.upsert(summary)
    got = repo.get("conv_1")
    assert got is not None
    assert got.confirmedConstraints == ["每天最多 8 小时"]
    assert repo.get("conv_missing") is None


def test_user_profile_repository_roundtrip(temp_db_path: Path) -> None:
    repo = UserProfileRepository(Database(temp_db_path))
    profile = UserProfile(userId="user_1", defaultStrategy="成本最低优先", riskPreference="低风险")
    repo.upsert(profile)
    got = repo.get("user_1")
    assert got is not None
    assert got.defaultStrategy == "成本最低优先"
    assert got.riskPreference == "低风险"
    assert repo.get("user_missing") is None


def test_decision_memory_repository_roundtrip(temp_db_path: Path) -> None:
    repo = DecisionMemoryRepository(Database(temp_db_path))
    repo.add(DecisionMemory(conversationId="conv_1", strategy="成本最低优先", conclusion="完成任务 25/28，总成本 472 万"))
    repo.add(DecisionMemory(conversationId="conv_1", strategy="高风险优先抢修", conclusion="完成任务 28/28"))
    items = repo.list_by_conversation("conv_1")
    assert len(items) == 2
    assert items[0].strategy == "高风险优先抢修"  # 倒序，最新的在前
