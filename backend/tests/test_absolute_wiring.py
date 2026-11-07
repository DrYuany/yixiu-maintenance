"""绝对优先在「澄清 / 候选意图推荐」界面上要看得出来是硬约束。

后端不再只是把意图名原样渲染：硬约束候选带 ``性质=硬约束`` / ``推荐=true``，
存进 pending 状态时不能丢，选项标签上要显示「硬约束·推荐」，点选后拿到的
必须是 ``count_min`` 补丁而不是一句空话。
"""
from __future__ import annotations

from app.core.agent_runtime import AgentRuntime
from yxcore.patch import Patch


def _group():
    hard = Patch(slot="count_min", index={"组": {"kind": "严重度", "value": "高"}},
                 value=5, strength="hard", utterance="高危最好全修")
    soft = Patch(slot="strategy", value="高风险优先抢修", strength="soft",
                 utterance="高危最好全修")
    return {
        "问": "是必须全部修到，还是只是相对偏好？",
        "候选": [
            {"意图": "高严重度的任务 5 个必须全修", "性质": "硬约束", "推荐": True,
             "patches": [hard]},
            {"意图": "只是尽量先修高严重度的任务", "性质": "相对偏好", "推荐": False,
             "patches": [soft]},
        ],
    }


def test_澄清选项标签区分硬约束与加权偏好():
    runtime = AgentRuntime.__new__(AgentRuntime)
    text, options = runtime._clarify_group_text(_group(), 0, 1)
    assert "硬约束" in options[0].label and "推荐" in options[0].label
    assert "相对偏好" in options[1].label
    assert options[0].value == "1" and options[1].value == "2"
    assert options[-1].value == "__custom__"  # 自由输入出口还在
    assert "硬约束" in text and "相对偏好" in text


def test_硬软标签随待确认状态存下来且点1拿到硬约束补丁():
    serialized = AgentRuntime._serialize_clarify_groups([_group()])
    cands = serialized[0]["候选"]
    assert cands[0]["性质"] == "硬约束" and cands[0]["推荐"] is True
    assert cands[1]["性质"] == "相对偏好" and cands[1]["推荐"] is False

    picked = AgentRuntime._match_clarify_choice("1", cands)
    assert picked is not None
    assert any(p["槽位"] == "count_min" and p["值"] == 5 and p["强度"] == "hard"
               for p in picked["patches"])
    # 手打「硬约束」也能选中；空回复不能被当成选中第一条
    assert AgentRuntime._match_clarify_choice("硬约束", cands) is picked
    assert AgentRuntime._match_clarify_choice("相对偏好", cands) is cands[1]
    assert AgentRuntime._match_clarify_choice("", cands) is None
