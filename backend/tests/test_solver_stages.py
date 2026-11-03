"""分层求解必须把每一段都跑到，而不是第一段超时后面全不跑。

以前 solve 每段都拿"剩余全部时间"，第一段用光 30 秒后，后面三级连
solver.Solve 都没进；界面上写着"分层求解"，实际只优化了成本一项。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "maintenance-milp-modeler" / "scripts"
for extra in (ROOT, SCRIPTS):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))


@pytest.fixture(scope="module")
def small_report(tmp_path_factory: pytest.TempPathFactory) -> dict:
    import os

    from product_data import load_project
    from product_planner import recommend

    data = load_project(ROOT)
    data.tasks = data.tasks[:6]
    output = tmp_path_factory.mktemp("stage-budget")
    # 这组用例只验证"每段都跑"，不验证求解质量；把预算压到 10 秒，
    # 否则两个时间型阶段会把 60 秒预算耗满，整个测试套件被拖慢一分钟。
    previous = os.environ.get("MWA_SOLVE_SECONDS")
    os.environ["MWA_SOLVE_SECONDS"] = "10"
    try:
        return recommend(data, output)
    finally:
        if previous is None:
            os.environ.pop("MWA_SOLVE_SECONDS", None)
        else:
            os.environ["MWA_SOLVE_SECONDS"] = previous


def test_every_planned_stage_is_executed(small_report: dict) -> None:
    info = small_report["求解"]

    planned = info["计划阶段数"]
    assert planned >= 2, "这套模板至少应该有成本与完成数两段"
    assert info["执行阶段数"] == planned, (
        f"只跑了 {info['执行阶段数']}/{planned} 段：{info['阶段计划']}"
    )
    assert info["分层进度"] == f"{planned}/{planned}"


def test_stage_plan_shows_allocated_seconds(small_report: dict) -> None:
    executed = [item for item in small_report["求解"]["阶段计划"] if item["结果"] == "执行"]

    assert executed, "没有执行任何阶段"
    assert all(item.get("分配秒", 0) > 0 for item in executed)


def test_budget_and_gap_come_from_one_place(small_report: dict) -> None:
    info = small_report["求解"]

    assert info["预算"]["seconds"] == info["求解器参数"]["max_time_in_seconds"]
    assert info["预算"]["gapLimit"] == info["求解器参数"]["relative_gap_limit"]
    assert info["预算"]["secondsSource"] in {"参数总表", "环境变量上限", "调用方指定", "代码默认"}


def test_cost_stage_records_its_own_gap(small_report: dict) -> None:
    stages = {item["目标"]: item for item in small_report["求解"]["阶段"]}

    cost = stages.get("总经济成本")
    assert cost is not None
    assert "相对差距" in cost
    assert cost.get("最优性") in {"已证明最优", "容差内最优", "未证明最优"}
    # 证明最优时差距按定义就是 0，不拿浮点界反推出"最优但差 0.9%"这种自相矛盾。
    if cost["状态"] == "OPTIMAL":
        assert cost["相对差距"] == 0.0
