from __future__ import annotations

from pathlib import Path

from app.core.conversation_memory import (
    ConversationMemoryCache,
    ConversationMemoryRepository,
    build_memory_snapshot,
    render_memory_context,
)
from app.db.database import Database
from app.schemas.agent import Message, WindowVersion, WorkingMemory


def _msg(
    id: str,
    role: str,
    content: str,
    *,
    version_ids: list[str] | None = None,
) -> Message:
    return Message(
        id=id,
        conversationId="c1",
        role=role,
        content=content,
        createdAt=f"2026-10-23T18:00:0{id[-1]}+08:00",
        versionIds=version_ids or [],
    )


def _version(id: str, strategy: str, status: str) -> WindowVersion:
    return WindowVersion(
        id=id,
        conversationId="c1",
        generatedAt="2026-10-23T18:10:00+08:00",
        userPrompt="排一版",
        focusTag=strategy,
        intentType="change",
        parameterSnapshot={},
        resultSummary={
            "strategy": strategy,
            "scheduledTaskCount": 11,
            "totalTaskCount": 20,
            "totalCost": 2821043.19,
            "solver": {"解状态": status},
        },
    )


def _memory() -> WorkingMemory:
    return WorkingMemory(
        conversationId="c1",
        currentGoal="生成并管理窗口版本",
        activeView="P01",
        latestResultId="v1",
    )


def test_build_snapshot_captures_constraints_artifacts_actions_qa() -> None:
    messages = [
        _msg("m1", "user", "船租上涨20%，最多2条作业线"),
        _msg("m2", "assistant", "已生成变更预览。", version_ids=["v1"]),
        _msg("m3", "user", "为什么任务018没排上"),
        _msg("m4", "assistant", "因为备件到货晚，截止窗口冲突。"),
    ]
    snapshot = build_memory_snapshot("c1", messages, _memory(), [_version("v1", "成本最低优先", "FEASIBLE")])

    assert snapshot.conversationId == "c1"
    assert snapshot.messageCount == 4
    assert "最多 2 条作业线" in snapshot.constraints
    assert snapshot.artifacts[0]["versionId"] == "v1"
    assert snapshot.artifacts[0]["status"] == "FEASIBLE"
    assert snapshot.actions[0]["type"] == "solve"
    assert snapshot.qa[0]["q"] == "为什么任务018没排上"
    assert "备件到货晚" in snapshot.qa[0]["a"]
    assert snapshot.state["latestResultId"] == "v1"
    assert "压缩记录" in snapshot.summary


def test_extract_qa_pairs_user_question_with_following_assistant_answer() -> None:
    messages = [
        _msg("m1", "user", "库存还有多少"),
        _msg("m2", "assistant", "叶片备件还有 3 套。", version_ids=["v1"]),
    ]
    snapshot = build_memory_snapshot("c1", messages, _memory(), [])

    assert len(snapshot.qa) == 1
    assert snapshot.qa[0]["q"] == "库存还有多少"
    assert snapshot.qa[0]["versionId"] == "v1"


def test_non_question_user_message_is_not_recorded_as_qa() -> None:
    messages = [
        _msg("m1", "user", "船租上涨20%"),
        _msg("m2", "assistant", "已预览。"),
    ]
    snapshot = build_memory_snapshot("c1", messages, _memory(), [])
    assert snapshot.qa == []


def test_render_memory_context_is_bounded_and_labeled() -> None:
    messages = [
        _msg("m1", "user", "船租上涨20%"),
        _msg("m2", "assistant", "已预览。"),
    ]
    snapshot = build_memory_snapshot("c1", messages, _memory(), [_version("v1", "成本最低优先", "FEASIBLE")])
    text = render_memory_context(snapshot)

    assert "已确认约束" in text
    assert "本会话已产生的方案" in text
    assert "动作时间线" in text
    assert "会话摘要" in text
    assert "FEASIBLE" in text


def test_render_with_no_data_has_no_constraints_artifacts_actions_qa() -> None:
    snapshot = build_memory_snapshot("c1", [], _memory(), [])
    text = render_memory_context(snapshot)
    assert "已确认约束" not in text
    assert "本会话已产生的方案" not in text
    assert "动作时间线" not in text
    assert "近期问答" not in text


def test_conversation_memory_repository_roundtrip(temp_db_path: Path) -> None:
    repo = ConversationMemoryRepository(Database(temp_db_path))
    snapshot = build_memory_snapshot("c1", [], _memory(), [])

    assert repo.get("c1") is None
    repo.upsert(snapshot)
    loaded = repo.get("c1")

    assert loaded is not None
    assert loaded.conversationId == "c1"
    assert loaded.messageCount == 0
    assert loaded.state["latestResultId"] == "v1"


def test_llm_summary_is_stored_and_rendered() -> None:
    snapshot = build_memory_snapshot("c1", [], _memory(), [], llm_summary="用户偏好先修高风险风机。")
    assert snapshot.llmSummary == "用户偏好先修高风险风机。"
    assert "会话背景（LLM 摘要）" in render_memory_context(snapshot)
    assert "用户偏好先修高风险风机" in render_memory_context(snapshot)


def test_memory_cache_hits_on_same_message_count_and_misses_on_change() -> None:
    cache = ConversationMemoryCache()
    snapshot = build_memory_snapshot("c1", [], _memory(), [])

    cache.put(snapshot)
    assert cache.get("c1", 0) is snapshot
    assert cache.get("c1", 1) is None
    assert cache.get("other", 0) is None


def test_memory_cache_evicts_oldest_beyond_maxsize() -> None:
    cache = ConversationMemoryCache(maxsize=2)
    for conversation_id in ("a", "b", "c"):
        cache.put(build_memory_snapshot(conversation_id, [], _memory(), []))

    assert cache.get("a", 0) is None
    assert cache.get("b", 0) is not None
    assert cache.get("c", 0) is not None


def test_summarize_returns_none_when_llm_disabled(monkeypatch) -> None:
    from app.core.conversation_agent import ConversationAgent

    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    agent = ConversationAgent()
    assert not agent.enabled
    assert agent.summarize([_msg("m1", "user", "船租上涨20%")]) is None
