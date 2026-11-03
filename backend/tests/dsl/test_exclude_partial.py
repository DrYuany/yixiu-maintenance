"""exclude 必须自动切部分排程；求解失败必须诚实，不能拿旧方案顶替。

两组真实事故：

1. 完整模式给每个任务加 ``sum(x)==1``（必须排入），exclude 加 ``sum(x)==0``，
   两者直接矛盾。"只修前 8 个，其余不安排"因此必然判不可行。

2. ``solve`` 曾用 ``selected=fallback`` 起手：约束把模型逼到不可行时，上一版
   排程被原样当成"有解"返回，解状态写 FEASIBLE、来源字段才藏着
   HEURISTIC_FALLBACK。上层靠 ``chosen is None`` 判断要不要降级，于是永远
   降不了级——用户看到 11/13，以为约束生效了。

跑法：``py -m pytest tests/dsl/test_exclude_partial.py -v``（真求解，约 1 分钟）
"""
from __future__ import annotations

import pytest

from dsl_core.runner import DslError, run

pytestmark = pytest.mark.slow


def test_exclude_automatically_uses_partial_mode() -> None:
    """说"其余不安排"就是允许少修，不该被判不可行。

    以前这里抛 DslError（约束冲突），因为完整模式要求所有任务全部排入。
    任务范围跟着 conftest 钉住的规划时点走：2026-10-23 当天有 001~011。
    """
    constraints = [{"type": "exclude", "task": f"任务{i:03d}"} for i in (9, 10, 11)]

    report = run({"constraints": constraints}, seconds=60)

    selected = report["排程"]
    assert selected is not None, "exclude 不该被判不可行"
    ids = {row["任务编号"] for row in selected}
    assert ids == {f"任务{i:03d}" for i in range(1, 9)}
    assert report["求解"]["方案来源"] == "CP_SAT", "这是求解结果，不是兜底"


def test_contradictory_constraints_are_reported_not_faked() -> None:
    """同一个任务既必须修又必须不修：要么明确报错，要么标明兜底。"""
    constraints = [
        {"type": "must", "task": "任务001"},
        {"type": "exclude", "task": "任务001"},
    ]

    try:
        report = run({"constraints": constraints}, seconds=40, max_tasks=13)
    except DslError as exc:
        text = str(exc)
        # 诊断上线后不再用「没有找到解」这种笼统说法，改成带证据的归因。
        # 这里核三件事：明确判不可行、指出是同一个任务的冲突、点名了具体任务
        # —— 光有「不可行」三个字不够，原因得能复算、不能是编的。
        assert "不可行" in text, text
        assert "同一个任务" in text, text
        assert "任务001" in text, text
        return

    info = report["求解"]
    if report.get("排程"):
        assert info["方案来源"] == "HEURISTIC_FALLBACK", "有方案但不是求解器给的，必须标出来"
        assert info.get("兜底原因")
    else:
        assert info["解状态"] in ("INFEASIBLE", "UNKNOWN")


def test_audit_passes_on_real_schedule() -> None:
    """真求解出来的方案，也要能通过意图对账——否则说明对账口径错了。

    对账不是"多一道拦"，它得能被正确的结果满足。这条用例和对面的
    `test_reconcile.py` 一起，保证"该过的过、该拦的拦"。
    """
    constraints = [{"type": "exclude", "task": f"任务{i:03d}"} for i in (9, 10, 11)]

    report = run({"constraints": constraints}, seconds=60)

    audit = report["求解"]["意图对账"]
    assert audit["通过"] is True, audit["明细"]
    assert audit["检查项"] >= 3


def test_solve_never_returns_hint_as_result(tmp_path) -> None:
    """求解失败时不能把热启动提示当成结果返回。

    这是 31/82 份"FEASIBLE + HEURISTIC_FALLBACK"的来源：hint 一进来就坐在
    selected 位置上，模型判不可行也换不掉它。
    """
    from dsl_core.runner import PROJECT_ROOT, _solver_modules

    load_project, candidates, heuristic, solve = _solver_modules()
    data = load_project(PROJECT_ROOT, base_id="基座01")
    data.tasks = data.tasks[:13]
    options = {task["任务编号"]: candidates(data, task) for task in data.tasks}
    hint = heuristic(data, options, "严重性优先")

    def _all_candidates_off(model, choices, task_vars, metrics):
        # 所有候选清零，与"每个任务必须选一项"直接冲突，必定不可行。
        model.Add(sum(variable for _, variable in choices) == 0)
        return []

    selected, info = solve(
        data,
        options,
        tmp_path,
        seconds=20,
        hint=hint,
        extra_constraints=_all_candidates_off,
    )

    assert selected is None, "不可行就是不可行，不能把 hint 端出来"
    assert info["有解"] is False
    assert info["方案来源"] is None
