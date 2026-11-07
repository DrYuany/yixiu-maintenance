# -*- coding: utf-8 -*-
"""solve 工具的结构化出口：turn.场景 → kind=batch；turn.澄清 → kind=clarify。

单循环后这些出口只能从 solve 工具进来（路由直通逻辑降为降级险），
所以工具层必须把每个出口产出成 runtime pendingChange 状态机认识的同构 payload。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.agent_tools import AgentToolbox, serialize_clarify_groups


def _patch(pid):
    return SimpleNamespace(to_json=lambda: {"id": pid}, pid=pid)


def _turn(**kwargs):
    base = dict(场景=None, 澄清=None, 错误=None, 拒绝=None,
                新增=None, 修订=None, 撤销=None, echo=lambda: ["- echo"])
    base.update(kwargs)
    return SimpleNamespace(**base)


def _engine(turn):
    session = SimpleNamespace(summary=lambda: ["- 现在：最少完成 5 项"])
    return SimpleNamespace(say=lambda text, timeout=90: turn, session=session)


def _toolbox(engine):
    persisted = []
    toolbox = AgentToolbox(
        conversation_id="c1",
        versions=SimpleNamespace(list_by_conversation=lambda cid: []),
        model_artifacts=None,
        on_pending_change=lambda p: persisted.append(p),
        yx_engine=engine,
        user_message="只完成前五个任务",
    )
    return toolbox, persisted


def test_solve_scenario_exit_makes_batch_pending():
    turn = _turn(场景=[
        {"标签": "经济优先", "patches": [_patch("p1")]},
        {"标签": "分层优先", "patches": []},
    ])
    toolbox, persisted = _toolbox(_engine(turn))
    result = toolbox._solve_inner({"instruction": "经济优先和分层各排一版"})
    assert result.get("status") == "needs_confirmation"
    data = result.get("data") or {}
    assert "将按这些方案分别排" in data.get("markdown", "")
    assert "经济优先" in data.get("markdown", "") and "分层优先" in data.get("markdown", "")
    assert data.get("verbatim") is True
    assert len(persisted) == 1
    payload = persisted[0]
    assert payload.get("kind") == "batch"
    scenarios = payload.get("scenarios")
    assert [sc["标签"] for sc in scenarios] == ["经济优先", "分层优先"]
    assert scenarios[0]["patches"] == [{"id": "p1"}]
    assert scenarios[1]["patches"] == []
    assert "将按这些方案分别排" in payload.get("preview", "")
    assert toolbox.pending_change == payload


def test_solve_clarify_exit_makes_clarify_pending():
    groups = [{"问": "「前五个」指哪些？", "候选": [
        {"意图": "按编号前五", "性质": "硬约束", "推荐": True, "patches": [_patch("p2")]},
        {"意图": "按时间最近五天", "patches": [_patch("p3")]},
    ]}]
    turn = _turn(澄清=groups)
    toolbox, persisted = _toolbox(_engine(turn))
    result = toolbox._solve_inner({"instruction": "只完成前五个任务"})
    assert result.get("status") == "clarify"
    assert len(persisted) == 1
    payload = persisted[0]
    assert payload.get("kind") == "clarify"
    assert payload.get("groups") == serialize_clarify_groups(groups)
    assert payload.get("chosen") == [] and payload.get("nextIndex") == 0
    assert payload.get("sourcePrompt") == "只完成前五个任务"
    assert toolbox.pending_change == payload


def test_solve_cleared_keeps_clarify_pending():
    """澄清也要留待用户点选——只有真没产出（错误/不可理解）才清旧遗留。"""
    groups = [{"问": "「前五个」指哪些？", "候选": [
        {"意图": "按编号前五", "性质": None, "推荐": None, "patches": []},
    ]}]
    turn = _turn(澄清=groups)
    toolbox, persisted = _toolbox(_engine(turn))
    result = toolbox._solve_inner_cleared({"instruction": "只完成前五个任务"})
    assert result.get("status") == "clarify"
    pending = toolbox.pending_change
    assert pending is not None
    assert pending.get("kind") == "clarify"


def test_solve_unparseable_still_clarifies_and_clears():
    """错误/拒绝/没有动作：envelope 走 clarify，但不算产出——pending 清空。"""
    turn = _turn(拒绝=["这句没听懂"])
    toolbox, persisted = _toolbox(_engine(turn))
    result = toolbox._solve_inner_cleared({"instruction": "随便说说"})
    assert result.get("status") == "clarify"
    assert toolbox.pending_change is None


def test_solve_translates_user_message_not_instruction():
    """模型转写会磨损语境（丢掉「分别排然后对比」），引擎必须拿用户原话翻译。"""
    captured = {}

    def say(text, timeout=90):
        captured["text"] = text
        return _turn(场景=[{"标签": "经济优先", "patches": [_patch("p1")]}])

    engine = SimpleNamespace(say=say, session=SimpleNamespace(summary=lambda: []))
    toolbox, persisted = _toolbox(engine)
    result = toolbox._solve_inner(
        {"instruction": "改成经济优先", "scenarios": ["经济优先", "严重性优先"]})
    assert captured["text"] == "只完成前五个任务"  # user_message 优先于模型转写
    assert result.get("status") == "needs_confirmation"
    payload = persisted[0]
    assert payload["kind"] == "batch" and payload["sourcePrompt"] == captured["text"]


def test_solve_falls_back_to_instruction_without_user_message():
    captured = {}

    def say(text, timeout=90):
        captured["text"] = text
        return _turn(新增=[{"字段": "x"}], echo=lambda: ["- echo"])

    engine = SimpleNamespace(say=say, session=SimpleNamespace(summary=lambda: []))
    toolbox = AgentToolbox(
        conversation_id="c1",
        versions=SimpleNamespace(list_by_conversation=lambda cid: []),
        model_artifacts=None,
        yx_engine=engine,
        user_message="",
    )
    toolbox._solve_inner({"instruction": "船租涨两成"})
    assert captured["text"] == "船租涨两成"
