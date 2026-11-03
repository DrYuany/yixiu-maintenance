"""理解引擎的默认与回滚。

产线默认 LLM 主导（llm-first）；规则主导留着当回滚开关，靠环境变量切，
不改代码、不重新发版。请求里显式带了 engine 的（前端切换控件）以请求为准。
"""
from __future__ import annotations

from app.api import agent as agent_api
from app.core import agent_runtime


def test_default_is_llm_first(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_ENGINE", raising=False)
    assert agent_runtime.default_engine() == "llm"


def test_env_var_rolls_back_to_rule(monkeypatch) -> None:
    """回滚开关：设了 AGENT_ENGINE=rule 才算 rule，别的任何值都不回滚。"""
    monkeypatch.setenv("AGENT_ENGINE", "rule")
    assert agent_runtime.default_engine() == "rule"

    monkeypatch.setenv("AGENT_ENGINE", "RULE")
    assert agent_runtime.default_engine() == "rule"

    monkeypatch.setenv("AGENT_ENGINE", "llm")
    assert agent_runtime.default_engine() == "llm"


def test_explicit_engine_wins_over_default(monkeypatch) -> None:
    """前端切换控件发什么就是什么，环境变量管不了它。"""
    monkeypatch.setenv("AGENT_ENGINE", "rule")
    assert agent_runtime.resolve_engine("llm") == "llm"
    assert agent_runtime.resolve_engine("rule") == "rule"


def test_missing_or_unknown_engine_falls_back_to_default(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_ENGINE", raising=False)
    assert agent_runtime.resolve_engine(None) == "llm"
    assert agent_runtime.resolve_engine("") == "llm"
    assert agent_runtime.resolve_engine("gpt") == "llm"


def test_api_default_follows_production_default(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_ENGINE", raising=False)
    assert agent_api.AgentMessageRequest(conversationId="c1", message="hi").engine == "llm"

    monkeypatch.setenv("AGENT_ENGINE", "rule")
    assert agent_api.AgentMessageRequest(conversationId="c1", message="hi").engine == "rule"


def test_llm_engine_falls_back_when_llmfirst_is_unavailable(monkeypatch) -> None:
    """默认切到 llm 之后，最坏情况必须是「退回规则路径」，不能是「答错或崩」。

    llmfirst 没配 key / 没接好时 _llm_first_parse 返回 None，
    上层拿 None 就走原来的对话路径——这条断言盯的就是这个约定。
    """
    monkeypatch.delenv("AGENT_ENGINE", raising=False)
    runtime = object.__new__(agent_runtime.AgentRuntime)
    monkeypatch.setattr(agent_runtime, "llmfirst_available", lambda: False, raising=False)

    class _NoLlmFirst:
        def __getattr__(self, name: str):
            raise ImportError("llmfirst 不可用")

    monkeypatch.setitem(
        __import__("sys").modules, "llmfirst", _NoLlmFirst()
    )
    monkeypatch.setitem(
        __import__("sys").modules, "llmfirst.pipeline", _NoLlmFirst()
    )
    monkeypatch.setitem(
        __import__("sys").modules, "llmfirst.client", _NoLlmFirst()
    )

    assert runtime._llm_first_parse("只修前8个") is None
