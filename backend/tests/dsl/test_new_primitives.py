"""新放行的三个原语 + 「未核对不再算通过」的验收。

这些用例都真求解（慢测）。判定标准不是"跑通了"，而是**约束在产物里留下了
可核对的痕迹**：

- ``at_most_one``：产物里这批任务最多一个
- ``at_least_one``：产物里至少一个
- ``capacity``：那一天动用的作业线数 ≤ limit

再加一条反向用例：约束合法、求解有解，但对账拿不到证据时必须**拒绝出方案**。
收紧的意思就是这个——没有证据的"通过"不算通过。
"""
from __future__ import annotations

import pytest

from dsl_core.runner import DslError, run

pytestmark = pytest.mark.slow


def _a_day_inside_the_window(offset_days: int = 1) -> tuple[str, int]:
    """窗口内的某一天：日期字符串 + 相对基准的天序号。

    不能硬编码 2026-11-16：`PLANNING_AS_OF` 一变，data.end 跟着变，
    同一个日期可能落到规划范围外，测试会因为无关原因红。
    """
    from dsl_core.runner import PROJECT_ROOT, _solver_modules

    load_project, *_ = _solver_modules()
    data = load_project(PROJECT_ROOT, base_id="基座01")
    window = sorted(data.windows, key=lambda w: w["开始"])[0]
    day = window["开始"] // 1440 + offset_days
    return data.stamp(day * 1440), day


def test_at_most_one_keeps_a_single_task() -> None:
    """「001 和 002 最多修一个」：产物里只能出现其中一个。"""
    report = run(
        {"constraints": [{"type": "at_most_one", "tasks": ["任务001", "任务002"]}]},
        seconds=60,
    )

    scheduled = {row["任务编号"] for row in report["排程"] or []}
    assert len(scheduled & {"任务001", "任务002"}) <= 1, scheduled
    assert report["求解"]["意图对账"]["通过"] is True


def test_at_least_one_keeps_at_least_one_task() -> None:
    report = run(
        {"constraints": [{"type": "at_least_one", "tasks": ["任务001", "任务002"]}]},
        seconds=60,
    )

    scheduled = {row["任务编号"] for row in report["排程"] or []}
    assert len(scheduled & {"任务001", "任务002"}) >= 1, scheduled
    assert report["求解"]["意图对账"]["通过"] is True


def test_capacity_limits_lines_on_that_day() -> None:
    """日配额：那天最多 3 条线，产物里那天的作业线数就不能超过 3。"""
    day_text, day = _a_day_inside_the_window(1)
    report = run(
        {"constraints": [{"type": "capacity", "resource": "line", "time": day_text, "limit": 3}]},
        seconds=60,
    )

    rows = report["排程"] or []
    assert rows, "配额 3 条线应当仍然解得出来"
    day_start, day_end = day * 1440, (day + 1) * 1440
    lines = {
        row["作业线"]
        for row in rows
        if any(a < day_end and b > day_start for a, b in row["作业段"])
    }
    assert len(lines) <= 3, f"那天用了 {sorted(lines)} 条线"
    audit = report["求解"]["意图对账"]
    assert audit["通过"] is True, audit["明细"]


def test_infeasible_run_carries_a_structured_diagnosis() -> None:
    """真求解不可行时，异常里必须带结构化诊断，而不是一句"没找到解"。

    用「总经济成本 ≤ 1 元」：13 个任务各挑最便宜的做法，直接成本也远超 1 元，
    这条是确定不可行的，且诊断能给出"需要多少 vs 上限多少"。
    """
    with pytest.raises(DslError) as caught:
        run(
            {"constraints": [{"type": "budget", "quantity": "总经济成本", "op": "<=", "value": 1}]},
            seconds=60,
        )

    diagnosis = caught.value.diagnosis
    assert diagnosis is not None, "不可行时必须带结构化诊断，不能只丢一句'没找到解'"
    assert diagnosis["结论"] == "不可行"
    text = str(diagnosis)
    assert "元" in text and "下界" in text, f"诊断没有给出可复算的数字：{text}"


def test_unverified_constraint_refuses_to_produce_a_plan() -> None:
    """求解有解、但对账拿不到证据 → 拒绝出方案。

    budget 写"高严重任务的人工费 ≤ 上限"：排程行里没有人工单价，重算不出来。
    以前这种情况标"未核对"但照样出方案——等于发了一份没被验证过诉求的排程。
    现在必须拦下来。
    """
    dsl = {
        "constraints": [
            {
                "type": "budget",
                "quantity": {"form": "weighted_sum", "coeff": "labor", "filter": {"severity": "高"}},
                "op": "<=",
                "value": 99_999_999,
            }
        ]
    }

    with pytest.raises(DslError) as caught:
        run(dsl, seconds=40)

    message = str(caught.value)
    assert "未能核对" in message, message
