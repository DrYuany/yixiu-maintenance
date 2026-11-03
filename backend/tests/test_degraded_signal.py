"""降级必须让用户看得见。

原来的三处 `except ConversationAgentError` 都是静默的：模型调用失败就滑到
确定性兜底，产出的句子和正常答复一模一样的形状——都是通顺中文、都带数字。
于是同一个问题"昨天答得挺细，今天干巴巴"，谁都查不出为什么。

这里钉三件事：
  1. 模型挂了 → 答复里带 degraded，且写清是哪一类；
  2. 模型正常 → degraded 必须是空的（否则告警天天响，等于没有）；
  3. 上一轮降级过 → 下一轮正常时不能还挂着横幅（thread-local 要重置）。
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.core.agent_runtime import AgentRuntime
from app.core.conversation_agent import ConversationAgentError


@pytest.fixture()
def client_and_runtime(temp_db_path, monkeypatch):
    os.environ["MWA_DB_PATH"] = str(temp_db_path)
    os.environ["MINIMAX_API_KEY"] = "test-key"  # 让 conversation_agent.enabled 为真
    from app.api import agent as agent_api
    from app.main import create_app

    app = create_app()
    runtime = app.dependency_overrides[agent_api.get_runtime]()
    return TestClient(app), runtime


def _say(client: TestClient) -> dict:
    conversation = client.post("/api/conversations", json={"title": "t"}).json()
    return client.post(
        "/api/agent/message",
        json={"conversationId": conversation["id"], "message": "你好，在吗"},
    ).json()


def _conversation(client: TestClient) -> str:
    return client.post("/api/conversations", json={"title": "t"}).json()["id"]


def test_agent_failure_is_reported_in_the_response(client_and_runtime, monkeypatch) -> None:
    client, runtime = client_and_runtime

    def boom(self, **kwargs):
        raise ConversationAgentError("upstream 502")

    monkeypatch.setattr(AgentRuntime, "_answer_with_conversation_agent", boom)

    payload = _say(client)

    assert payload["degraded"] is not None, "模型挂了却什么都没说，用户会以为这是模型的答复"
    assert payload["degraded"]["kind"] == "agent_unavailable"
    assert "502" in (payload["degraded"]["detail"] or "")
    assert payload["assistantMessage"]["content"], "降级也要给出兜底答复，不能空着"
    # trace 里也要留痕：事后查"为什么这轮答得干"靠它。
    trace = runtime.get_trace(payload["traceId"])
    assert trace is not None and trace.degraded == "agent_unavailable"


def test_normal_answer_carries_no_degradation_flag(client_and_runtime, monkeypatch) -> None:
    client, _ = client_and_runtime
    monkeypatch.setattr(
        AgentRuntime, "_answer_with_conversation_agent", lambda self, **kwargs: "我在。"
    )

    payload = _say(client)

    assert payload["degraded"] is None, "没降级就不要报降级，否则告警天天响"


def test_a_later_healthy_turn_clears_the_previous_degradation(client_and_runtime, monkeypatch) -> None:
    """降级标记挂在 thread-local 上，忘了重置就会粘住。

    粘住的后果不是"多一个提示"，是**指向错的地方**：下一轮明明走了模型，
    界面却说是规则兜底，人就会去查一个不存在的故障。
    """
    client, _ = client_and_runtime
    conversation = _conversation(client)

    def boom(self, **kwargs):
        raise ConversationAgentError("boom")

    monkeypatch.setattr(AgentRuntime, "_answer_with_conversation_agent", boom)
    first = client.post(
        "/api/agent/message", json={"conversationId": conversation, "message": "你好"}
    ).json()
    assert first["degraded"] is not None

    monkeypatch.setattr(
        AgentRuntime, "_answer_with_conversation_agent", lambda self, **kwargs: "在的。"
    )
    second = client.post(
        "/api/agent/message", json={"conversationId": conversation, "message": "还在吗"}
    ).json()
    assert second["degraded"] is None
