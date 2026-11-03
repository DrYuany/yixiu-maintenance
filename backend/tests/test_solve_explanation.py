"""求解结果的「为什么」必须有据可查：只认求解记录里的字段，不许编原因。"""
from __future__ import annotations

from types import SimpleNamespace

from app.core.agent_runtime import AgentRuntime
from app.core.solve_explanation import explain_no_solution, explain_not_optimal


def _solver(**overrides):
    base = {
        "解状态": "FEASIBLE",
        "终止原因": "COMPLETED",
        "总耗时秒": 42.0,
        "未达最优阶段": [],
        "容差内阶段": [],
        "最大相对差距": 0.0,
        "确定性决胜完成": True,
        "预算": {"seconds": 180, "gapLimit": 0.01},
    }
    base.update(overrides)
    return base


def test_stage_time_limit_names_the_unproven_stages() -> None:
    text = explain_not_optimal(_solver(
        终止原因="STAGE_TIME_LIMIT",
        未达最优阶段=["最后完工时间", "完工时间和"],
        最大相对差距=0.0327,
    ))
    assert "未证明最优的原因" in text
    assert "最后完工时间" in text and "完工时间和" in text
    assert "用满了分到的时间" in text
    assert "3.3%" in text


def test_tolerance_stop_is_not_described_as_timeout() -> None:
    """容差内主动停止，不能说成时间用尽——两者是不同的停法。"""
    text = explain_not_optimal(_solver(
        终止原因="COMPLETED",
        容差内阶段=["总经济成本"],
        最大相对差距=0.008,
    ))
    assert "1% 容差内提前停止" in text
    assert "用满了分到的时间" not in text
    assert "整体时间用尽" not in text


def test_user_cancel_is_its_own_reason() -> None:
    text = explain_not_optimal(_solver(终止原因="USER_CANCELLED", 未达最优阶段=["完工时间和"]))
    assert "求解被取消" in text
    assert "用满了分到的时间" not in text


def test_tie_break_unfinished_is_called_out() -> None:
    text = explain_not_optimal(_solver(终止原因="TIME_LIMIT", 确定性决胜完成=False))
    assert "确定性决胜没跑完" in text
    assert "整场求解时限用尽" in text


def test_tie_not_proven_never_claims_time_ran_out() -> None:
    """14 秒就返回、预算还剩一大截时，不许说「时间用尽」——这是假理由。"""
    text = explain_not_optimal(_solver(终止原因="TIE_NOT_PROVEN", 确定性决胜完成=False))
    assert "时间尚未用尽" in text
    assert "时间用尽" not in text
    assert "时限用尽" not in text


def test_empty_record_still_explains_without_inventing() -> None:
    text = explain_not_optimal({})
    assert "未证明最优的原因" in text
    assert "没有证明它是最优的" in text


def test_unknown_is_never_called_infeasible() -> None:
    text = explain_no_solution("UNKNOWN", _solver(终止原因="TIME_LIMIT", 总耗时秒=180.0))
    assert "未证明无解" in text
    assert "时限 180 秒" in text
    assert "不是「不可行」" in text
    assert "放宽求解时限" in text


def test_unknown_without_budget_does_not_fabricate_a_limit() -> None:
    text = explain_no_solution("UNKNOWN", {"终止原因": "TIME_LIMIT"})
    assert "用时未知" in text
    assert "时限" not in text or "时限 未知" not in text


def test_infeasible_states_proof_not_timeout() -> None:
    text = explain_no_solution("INFEASIBLE", _solver(解状态="INFEASIBLE", 终止原因="INFEASIBLE"))
    assert "已经证明" in text
    assert "没有可行排程" in text
    assert "没算完" not in text


def test_reply_template_carries_the_reason() -> None:
    """对话回复里必须能看到理由，不能只写一句「尚未证明最优」。"""
    version = SimpleNamespace(
        windows=[SimpleNamespace(
            windowId="窗口03",
            metrics=SimpleNamespace(startTime="2026-11-15 00:00", endTime="2026-11-20 00:00"),
        )]
    )
    summary = {
        "totalCost": 2_433_300.0,
        "scheduledTaskCount": 13,
        "totalTaskCount": 13,
        "baselineSavings": {},
        "solver": _solver(终止原因="STAGE_TIME_LIMIT", 未达最优阶段=["完工时间和"], 最大相对差距=0.02),
    }
    reply = AgentRuntime._format_product_reply("FEASIBLE", summary, version)

    assert "尚未证明最优" in reply
    assert "未证明最优的原因" in reply
    assert "完工时间和" in reply


def test_optimal_reply_does_not_add_a_reason() -> None:
    version = SimpleNamespace(windows=[])
    summary = {
        "totalCost": 1_000_000.0,
        "scheduledTaskCount": 2,
        "totalTaskCount": 2,
        "baselineSavings": {},
        "solver": _solver(解状态="OPTIMAL", 终止原因="COMPLETED"),
    }
    reply = AgentRuntime._format_product_reply("OPTIMAL", summary, version)
    assert "已证明最优" in reply
    assert "未证明最优的原因" not in reply
