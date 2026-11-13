# -*- coding: utf-8 -*-
"""solve 工具的结构化出口：turn.场景 → kind=batch；turn.澄清 → kind=clarify。

单循环后这些出口只能从 solve 工具进来（路由直通逻辑降为降级险），
所以工具层必须把每个出口产出成 runtime pendingChange 状态机认识的同构 payload。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.agent_tools import AgentToolbox, serialize_clarify_groups


def _patch(pid, value="经济优先"):
    # 描述一条补丁要用到 slot/value（describe_patch 的入参），fake 也得有。
    return SimpleNamespace(to_json=lambda: {"id": pid}, pid=pid,
                           slot="strategy", index=None, value=value)


def _turn(**kwargs):
    base = dict(场景=None, 澄清=None, 错误=None, 拒绝=None,
                新增=None, 修订=None, 撤销=None, echo=lambda: ["- echo"])
    base.update(kwargs)
    return SimpleNamespace(**base)


def _engine(turn):
    session = SimpleNamespace(say=lambda text, timeout=90, resolved_intent=None: turn,
                              summary=lambda: ["- 现在：最少完成 5 项"],
                              patches=lambda: [], base_data=None)
    return SimpleNamespace(session=session)


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

    def say(text, timeout=90, resolved_intent=None):
        captured["text"] = text
        return _turn(场景=[{"标签": "经济优先", "patches": [_patch("p1")]}])

    engine = SimpleNamespace(session=SimpleNamespace(
        say=say, summary=lambda: [], patches=lambda: [], base_data=None))
    toolbox, persisted = _toolbox(engine)
    result = toolbox._solve_inner(
        {"instruction": "改成经济优先", "scenarios": ["经济优先", "严重性优先"]})
    assert captured["text"] == "只完成前五个任务"  # user_message 优先于模型转写
    assert result.get("status") == "needs_confirmation"
    payload = persisted[0]
    assert payload["kind"] == "batch" and payload["sourcePrompt"] == captured["text"]


def test_solve_falls_back_to_instruction_without_user_message():
    captured = {}

    def say(text, timeout=90, resolved_intent=None):
        captured["text"] = text
        return _turn(新增=[_patch("p1")], echo=lambda: ["- echo"])

    engine = SimpleNamespace(session=SimpleNamespace(
        say=say, summary=lambda: [], patches=lambda: [], base_data=None))
    toolbox = AgentToolbox(
        conversation_id="c1",
        versions=SimpleNamespace(list_by_conversation=lambda cid: []),
        model_artifacts=None,
        yx_engine=engine,
        user_message="",
    )
    toolbox._solve_inner({"instruction": "船租涨两成"})
    assert captured["text"] == "船租涨两成"


def test_solve_passes_original_words_and_resolved_intent_separately():
    """用户回「可以」时，翻译用的仍是原话，主模型补出的完整意思另送一份。"""
    seen = {}

    def say(text, timeout=90, resolved_intent=None):
        seen.update(text=text, resolved_intent=resolved_intent)
        return _turn()

    engine = SimpleNamespace(session=SimpleNamespace(
        say=say, summary=lambda: [], patches=lambda: [], base_data=None))
    toolbox, _ = _toolbox(engine)
    toolbox.user_message = "可以"
    toolbox._solve_inner({"instruction": "把船租单价上调两成，按现状排一版"})
    assert seen["text"] == "可以", "原话不能被补全的意思替换"
    assert "船租" in (seen["resolved_intent"] or ""), "完整意思要另传到翻译层"


def test_solve_skips_resolved_intent_when_it_equals_original_words():
    """模型原样抄了原话时没有需要消解的东西，不多送一份。"""
    seen = {}

    def say(text, timeout=90, resolved_intent=None):
        seen.update(text=text, resolved_intent=resolved_intent)
        return _turn()

    engine = SimpleNamespace(session=SimpleNamespace(
        say=say, summary=lambda: [], patches=lambda: [], base_data=None))
    toolbox, _ = _toolbox(engine)
    toolbox.user_message = "船租涨两成"
    toolbox._solve_inner({"instruction": "船租涨两成"})
    assert seen["text"] == "船租涨两成"
    assert seen["resolved_intent"] is None
