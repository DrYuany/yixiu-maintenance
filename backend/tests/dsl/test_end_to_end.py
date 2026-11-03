"""端到端验证（原型毕业证据）：手写 DSL → 真求解 → 断言约束真的生效。

这是 golden 判分的核心：不只看"能跑通"，而是断言求解结果满足约束。
完整 25 任务模型单线程 CP-SAT 短时限内证明不了可行性（生产默认 300s），
所以用 max_tasks=10 的小规模子集验证约束语义——规模性能是主项目已有能力。

跑法：py -m pytest tests/test_end_to_end.py -v（每个用例真求解约 5~30 秒）
"""
from __future__ import annotations


from dsl_core.runner import DslError, run
import pytest

pytestmark = pytest.mark.slow


def _row(selected: list[dict], task_id: str) -> dict:
    return next(c for c in selected if c["任务编号"] == task_id)


def _day_of(selected: list[dict], task_id: str) -> int:
    return _row(selected, task_id)["开始"] // 1440


def test_co_occur_diff_day_forces_different_days() -> None:
    """

'任务001 和 002 不能同一天' —— 求解结果必须真的不同天。"""
    report = run({
        "constraints": [
            {"type": "co_occur", "mode": "diff_day", "tasks": ["任务001", "任务002"]},
        ],
    }, seconds=60, max_tasks=10)
    selected = report["排程"]
    assert selected is not None, f"求解未产生排程: {report['求解']['终止原因']}"
    assert _day_of(selected, "任务001") != _day_of(selected, "任务002")


def test_precedence_forces_before_before_after() -> None:
    """'先修001再修002' —— 001 完工 ≤ 002 开工。"""
    report = run({
        "constraints": [
            {"type": "precedence", "before": "任务001", "after": "任务002"},
        ],
    }, seconds=60, max_tasks=10)
    selected = report["排程"]
    assert selected is not None
    assert _row(selected, "任务001")["完成"] <= _row(selected, "任务002")["开始"]


def test_earliest_blocks_early_candidates() -> None:
    """'任务001 最早 11 月 5 日 08:00 开工'（窗口02 第 3 天）。"""
    report = run({
        "constraints": [
            {"type": "earliest", "task": "任务001", "time": "2026-11-05T08:00:00+08:00"},
        ],
    }, seconds=60, max_tasks=10)
    selected = report["排程"]
    assert selected is not None
    from pathlib import Path
    from product_data import load_project

    data = load_project(Path(__file__).resolve().parents[3], base_id="基座01",
                        as_of="2026-10-23T18:00:00+08:00")
    minute = data.minute("2026-11-05T08:00:00+08:00")
    assert _row(selected, "任务001")["开始"] >= minute


def test_baseline_has_no_dsl_side_effects() -> None:
    """不带 DSL 也能正常求解（验证注入钩子默认路径未受影响）。"""
    report = run({}, seconds=60, max_tasks=10)
    assert report["排程"] is not None


def test_invalid_dsl_raises_clear_error() -> None:
    with pytest.raises(DslError) as exc:
        run({
            "constraints": [
                {"type": "co_occur", "mode": "diff_day", "tasks": ["任务001", "任务999"]},
            ],
        }, seconds=30, max_tasks=10)
    assert any("不在当前告警快照" in e for e in exc.value.errors)


def test_deadline_that_starves_tasks_is_refused_not_silently_dropped() -> None:
    """「严重故障11月8日前必须修完」不能变成"那四个任务不见了"。

    期限补丁把候选按截止时间截断：任务在期限前干不完，就一个候选都生不出来，
    于是它从"必须排进去"的名单里消失，求解照常返回 FEASIBLE，四条躺在"未安排"。
    用户说的是"必须修完"，拿到的是一份漏了四个高危任务的计划。
    """
    with pytest.raises(DslError) as exc:
        run({
            "data": [{"kind": "task_deadline", "severity": "高",
                      "at": "2026-11-08T08:00:00+08:00"}],
        }, seconds=30, max_tasks=6, as_of="2026-10-30T00:00:00+08:00")
    assert any("排不进来" in e for e in exc.value.errors), exc.value.errors


def test_heuristic_fallback_is_never_returned_as_a_solution() -> None:
    """约束把模型逼到不可行时，不许把"没考虑约束的基座方案"当结果交回去。

    product_planner.solve 曾用 ``selected=fallback`` 起手，那份方案是在**没有**
    DSL 约束的模型上排的。实测：同时要求"禁止任务001"和"必须任务001"，返回的
    方案里任务001 排得好好的，解状态还写 FEASIBLE，只有来源字段藏着
    HEURISTIC_FALLBACK。现在 solve 失败即返回 None，runner 直接报错。
    """
    with pytest.raises(DslError) as exc:
        run({
            "constraints": [
                {"type": "exclude", "task": "任务001"},
                {"type": "must", "task": "任务001"},
            ],
        }, seconds=30, max_tasks=6)
    # 失败诊断上线后，文案从"没有找到解"换成了带证据的"不可行；[已证明] …"。
    # 这条用例真正要守的是"不许把兜底方案当解返回"，所以两种文案都收，
    # 但要求必须明确说了不可行——不能退化回一句含糊的"请放宽约束"。
    assert any(
        ("不可行" in text) or ("没有在满足这些约束的前提下找到解" in text)
        for text in exc.value.errors
    ), exc.value.errors
