"""过程事件必须跟着真实工具循环发，不能事后按文案补。

用户的三问是「到哪个环节了、调了哪个工具、那一步拿回了什么」。
这组测试把三问钉在事件序列上，防止以后又退化成一句模板化 loading。
"""
from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.core.agent_runtime import StreamSink
from app.core.conversation_agent import (
    ConversationAgent,
    ConversationAgentError,
    _tool_result_brief,
)
from app.schemas.agent import WorkingMemory


class _FakeStream:
    def __init__(self, final, texts=()):
        self._final = final
        self.text_stream = iter(texts)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get_final_message(self):
        return self._final


def _toolbox():
    box = MagicMock()
    box.ordered = []
    box.tool_specs.return_value = []
    box.execute.return_value = {
        "status": "ok",
        "data": {"总数": 15, "message": "读到告警清单"},
    }
    box.trace.return_value = [{"latencyMs": 12.4}]
    return box


def _answer(rounds, events):
    client = MagicMock()
    client.messages.stream.side_effect = lambda **kwargs: rounds.pop(0)
    with patch("app.core.conversation_agent.Anthropic", return_value=client):
        return ConversationAgent(api_key="sk-test").answer(
            message="现在有哪些告警？",
            history=[],
            memory=WorkingMemory(conversationId="c1"),
            toolbox=_toolbox(),
            selected_version=None,
            snapshot=None,
            on_stage=events.append,
        )


def test_stage_events_follow_real_tool_loop():
    """理解 → 调工具 → 再推理 → 整理回答，每步都有状态和可读说明。"""
    rounds = [
        _FakeStream(SimpleNamespace(content=[
            SimpleNamespace(type="tool_use", name="read_alarms", id="tu-1",
                            input={"scope": "backlog"}),
        ])),
        _FakeStream(
            SimpleNamespace(content=[SimpleNamespace(type="text", text="已完成。")]),
            ("已完成。",),
        ),
    ]
    events: list[dict] = []
    assert _answer(rounds, events) == "已完成。"

    seq = [(e["stage_id"], e["status"], e.get("tool")) for e in events]
    assert seq[0] == ("think.0", "running", None)
    assert ("think.0", "done", None) in seq
    assert ("tool.0.0", "running", "read_alarms") in seq
    assert ("tool.0.0", "done", "read_alarms") in seq
    assert ("think.1", "running", None) in seq
    assert ("answer", "running", None) in seq
    assert ("answer", "done", None) in seq
    assert seq.index(("tool.0.0", "running", "read_alarms")) < seq.index(
        ("tool.0.0", "done", "read_alarms")
    ), "running 必须先于 done 到达，前端才能显示「当前环节」"
    assert all(e.get("detail") for e in events), "每一步都要说清在做什么"

    tool_done = next(
        e for e in events
        if e["stage_id"] == "tool.0.0" and e["status"] == "done"
    )
    assert "共 15 条" in tool_done["detail"]
    assert "12 ms" in tool_done["detail"]
    assert tool_done["tool"] == "read_alarms"


def test_model_failure_marks_the_stage_as_error():
    """模型这轮没答上来时，过程不能停在一个假的运行中。"""
    client = MagicMock()
    client.messages.stream.side_effect = RuntimeError("上游超时")
    events: list[dict] = []
    with patch("app.core.conversation_agent.Anthropic", return_value=client):
        with pytest.raises(ConversationAgentError):
            ConversationAgent(api_key="sk-test").answer(
                message="现在有哪些告警？",
                history=[],
                memory=WorkingMemory(conversationId="c1"),
                toolbox=_toolbox(),
                selected_version=None,
                snapshot=None,
                on_stage=events.append,
            )
    assert events[-1]["stage_id"] == "think.0"
    assert events[-1]["status"] == "error"


def test_result_brief_never_invents_a_count():
    """data 里随便一个列表不等于「总共有几条」，拿不到明确数量就别报。"""
    brief = _tool_result_brief(
        {"status": "ok", "data": {"message": "这份数据没有清单", "窗口": [1, 2, 3]}},
        _toolbox(),
    )
    assert "已读到数据" in brief
    assert not re.search(r"共 \d+ 条", brief)

    with_counts = _tool_result_brief(
        {"status": "ok", "data": {"counts": {"total": 15, "open": 15}}}, _toolbox()
    )
    assert "共 15 条" in with_counts


def test_stream_sink_carries_tool_name_only_for_tool_steps():
    emitted: list[dict] = []
    sink = StreamSink(emitted.append)
    sink.stage("tool.0.0", "调用工具 · 读取告警清单", "running",
               "查台账里还没结案的告警", tool="read_alarms")
    sink.stage("solve", "CP-SAT 求解", "running")

    assert emitted[0]["event"] == "stage"
    assert emitted[0]["data"]["tool"] == "read_alarms"
    assert "tool" not in emitted[1]["data"]
