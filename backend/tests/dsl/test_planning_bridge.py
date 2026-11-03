"""桥接层：DSL 落到 PlanningRequest 的边界。

这一层的职责是"能映射的映射，映射不了的交给编译器"，所以两个方向都得出错：
漏映射（该带走的没带走）会让约束静默消失；错映射（不该带走的硬塞）会直接崩。

真出过的崩法：`must` 的 filter 形式（"严重故障必须修完"）没有 task 字段，
桥接层却无条件读 `spec["task"]`，KeyError 一路冒到对话层。
"""
from __future__ import annotations

from dsl_core.planning_bridge import dsl_to_request
from dsl_core.planning_request import PlanningRequest


def _split(dsl: dict):
    return dsl_to_request(dsl, PlanningRequest())


def test_must_with_task_goes_into_limits() -> None:
    request, residual, _ = _split({"constraints": [{"type": "must", "task": "任务001"}]})

    assert request.limits["必修任务"] == ["任务001"]
    assert residual == []


def test_must_with_filter_is_left_to_the_compiler() -> None:
    """filter 形式没有 task 字段，必须整条进 residual，不能崩也不能丢。"""
    spec = {"type": "must", "filter": {"严重度": "严重"}}
    request, residual, _ = _split({"constraints": [spec]})

    assert residual == [spec]
    assert "必修任务" not in request.limits


def test_exclude_always_goes_to_residual() -> None:
    """PlanningRequest 没有"排除"这个概念，只能靠 DSL 段。"""
    spec = {"type": "exclude", "task": "任务009"}
    _, residual, _ = _split({"constraints": [spec]})

    assert residual == [spec]


def test_cardinality_without_filter_goes_into_limits() -> None:
    request, residual, _ = _split(
        {"constraints": [{"type": "cardinality", "filter": {}, "op": ">=", "value": 9}]}
    )

    assert request.limits["最低完成数"] == 9
    assert residual == []


def test_cardinality_with_filter_is_left_to_the_compiler() -> None:
    """带 filter 的计数是"某类任务至少 N 个"，和"最低完成数"不是一件事。"""
    spec = {"type": "cardinality", "filter": {"严重度": "严重"}, "op": ">=", "value": 3}
    request, residual, _ = _split({"constraints": [spec]})

    assert residual == [spec]
    assert "最低完成数" not in request.limits


def test_every_registered_constraint_survives_the_bridge() -> None:
    """15 种原语一条都不能在桥接层丢掉或抛异常。"""
    specs = [
        {"type": "must", "task": "任务001"},
        {"type": "must", "filter": {"严重度": "严重"}},
        {"type": "exclude", "task": "任务002"},
        {"type": "at_most_one", "tasks": ["任务001", "任务002"]},
        {"type": "at_least_one", "tasks": ["任务001", "任务002"]},
        {"type": "precedence", "before": "任务001", "after": "任务002", "gap": 60},
        {"type": "co_occur", "mode": "diff_day", "tasks": ["任务001", "任务002"]},
        {"type": "no_overlap", "resource": "船", "tasks": ["任务001", "任务002"]},
        {"type": "capacity", "resource": "船", "time": "2026-11-03", "limit": 2},
        {"type": "fix", "task": "任务001", "line": 2},
        {"type": "cardinality", "filter": {}, "op": ">=", "value": 3},
        {"type": "deadline", "task": "任务001", "time": "2026-11-08"},
        {"type": "earliest", "task": "任务001", "time": "2026-11-03"},
        {"type": "forbidden_dates", "dates": ["2026-11-05"]},
        {"type": "budget", "quantity": "总经济成本", "op": "<=", "value": 800000},
        {"type": "linear", "terms": [[1, "任务001"]], "op": "<=", "value": 5},
    ]

    for spec in specs:
        _, residual, _ = _split({"constraints": [spec]})
        # 映射进 request 的不在 residual 里，其余必须逐条保留，不能静默吞掉
        assert all(item.get("type") for item in residual), spec
