# -*- coding: utf-8 -*-
"""循环出口结构化：模型这轮调过 solve 后，必须按 pendingChange 定 actionType。

单循环后 change_preview / batch_preview / clarification 只能从这里出来——
不转换的话所有带工具循环的回复都会带着 assistant_reply 混不出区别。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.core.agent_runtime import AgentRuntime


def _runtime(filters):
    rt = AgentRuntime.__new__(AgentRuntime)
    latest = SimpleNamespace(activeFilters=dict(filters), activeView="P01")
    rt.memories = SimpleNamespace(get_or_create=lambda cid: latest)
    captured = {}

    def fake_finalize(conversation_id, content, action, view, nv, **kwargs):
        captured.update(action=action, content=content, options=kwargs.get("clarification_options"))
        return SimpleNamespace(**captured)

    rt._finalize_response = fake_finalize
    return rt, captured


def test_loop_reply_to_change_preview():
    rt, captured = _runtime({"pendingChange": {"kind": "yx", "preview": "- 船租=14400", "sourcePrompt": "船租涨两成"}})
    rt._finalize_loop_response("c1", "已按你的说法准备变更预览：\n- 船租=14400", None)
    assert captured["action"] == "change_preview"
    assert captured["content"].endswith("- 船租=14400")


def test_loop_reply_to_batch_preview():
    rt, captured = _runtime({"pendingChange": {
        "kind": "batch", "preview": "将按这些方案分别排：\n- 经济优先", "sourcePrompt": "x"}})
    rt._finalize_loop_response("c1", "我会分别准备", None)
    assert captured["action"] == "batch_preview"
    assert captured["content"] == "将按这些方案分别排：\n- 经济优先"


def test_loop_reply_to_clarification():
    groups = [{"问": "「前五个」指哪些？", "候选": [
        {"意图": "按编号前五", "性质": "硬约束", "推荐": True, "patches": []},
        {"意图": "按时间最近五天", "性质": None, "推荐": None, "patches": []},
    ]}]
    rt, captured = _runtime({"pendingChange": {"kind": "clarify", "groups": groups,
                                               "chosen": [], "nextIndex": 0}})
    rt._finalize_loop_response("c1", "这句话有几个理解方式", None)
    assert captured["action"] == "clarification"
    labels = [getattr(o, "label", None) for o in captured["options"]]
    assert any("按编号前五" in lab for lab in labels)
    assert any("都不是，我重新说" == lab for lab in labels)


def test_loop_reply_plain_when_no_pending():
    rt, captured = _runtime({})
    rt._finalize_loop_response("c1", "cp-sat 是 Google 的求解器。", None)
    assert captured["action"] == "assistant_reply"
    assert captured["content"] == "cp-sat 是 Google 的求解器。"


def test_loop_reply_plain_when_unknown_kind():
    rt, captured = _runtime({"pendingChange": {"kind": "unknown-thing"}})
    rt._finalize_loop_response("c1", "好的。", None)
    assert captured["action"] == "assistant_reply"


def _runtime_with_toolbox(filters):
    """只钉状态接线，不注入假模型：仓库/引擎都是最小桩。"""
    rt, captured = _runtime(filters)
    rt.versions = SimpleNamespace(list_by_conversation=lambda cid: [])
    rt.model_artifacts = None
    rt.jobs = None
    rt._yx_engine = lambda cid: SimpleNamespace(session=SimpleNamespace())
    rt._record_tool_call = lambda record: None
    rt._persist_agent_pending = lambda cid, payload: None
    rt._submit_pending_change = lambda cid, pending, reason: {"ok": True}
    return rt, captured


def test_toolbox_loads_persisted_preview_for_next_turn_confirmation():
    """删掉关键词确认短路后，跨轮「确认」只能靠工具读到持久化的那份预览。

    线上实测过一次回归：toolbox.pending_change 每轮新建恒为 None，
    用户回「确认」被 submit_change 拒成「还没有待确认的变更」。
    """
    preview = {"kind": "yx", "preview": "- 船租=14400", "sourcePrompt": "船租涨两成"}
    rt, _ = _runtime_with_toolbox({"pendingChange": preview})
    toolbox = rt._build_toolbox("c1", None, None)
    assert toolbox.pending_change == preview


def test_loop_exit_returns_submitted_solve_response():
    """submit_change 派出求解后，交的是系统那份结构化响应，不是模型转述。"""
    rt, captured = _runtime_with_toolbox({})
    solved = SimpleNamespace(actionType="solve_started", solveJob="job-x")
    rt._tls = SimpleNamespace(submitted_response=solved)
    assert rt._finalize_loop_response("c1", "模型说已完成求解", None) is solved
    assert not captured, "不该再走一次 _finalize_response 把任务状态盖掉"
