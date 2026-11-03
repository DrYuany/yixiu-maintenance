"""失败诊断：只说算得出来的原因，算不出来就说不知道。

这一层的验收标准跟别处不一样：**不是"有输出"，而是"输出里没有一个字是猜的"**。
所以除了正向用例（能定位），还必须有一条守门用例——定位不到的时候，
不许出现"可能""大概""也许是"这类没有证据的措辞。

真出过的事：求解器只回一句"没找到解，请放宽约束"，模型接着自己编一句
"因为工时不够"。用户把模型的话当成系统解释，回头去改工时，白改一轮。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from dsl_core.diagnose import PROVEN, UNKNOWN, UPPER_BOUND, describe, diagnose


def _data(tasks=None, windows=None, params=None):
    return SimpleNamespace(
        tasks=tasks or [],
        windows=windows or [{"编号": "窗口03", "开始": 33120, "结束": 40320}],
        params={
            "每天最多维修小时数": 10,
            "人工单价（元每人时）": 500,
            **(params or {}),
        },
        parts={
            "部件01": {"部件名称": "叶片", "维修包成本（元每套）": 20000, "其他费用（元每任务）": 0}
        },
    )


def _candidate(task_id: str, start: int, finish: int) -> dict:
    return {
        "任务编号": task_id,
        "风机编号": "风机001",
        "部件编号": "部件01",
        "部件名称": "叶片",
        "严重等级": "高",
        "开始": start,
        "完成": finish,
        "作业段": [[start, finish]],
        "作业线": 1,
    }


def _info(status: str = "INFEASIBLE", reason: str = "COMPLETED", seconds: float = 12.0):
    return {
        "解状态": status,
        "终止原因": reason,
        "总耗时秒": seconds,
        "求解器参数": {"max_time_in_seconds": 60},
    }


def test_unknown_is_reported_as_unfinished_not_infeasible() -> None:
    """超时没算完，不能说成"不可行"——用户会去改本来没问题的约束。"""
    result = diagnose(
        dsl={"constraints": [{"type": "budget", "quantity": "总经济成本", "op": "<=", "value": 9}]},
        data=_data(),
        options={},
        info=_info(status="UNKNOWN", reason="TIME_LIMIT"),
    )

    assert result["结论"] == "未算完"
    assert "不是" in result["原因"][0]["说明"], "要说清这不是无解"
    assert "12.0" in result["原因"][0]["证据"]


def test_must_and_exclude_on_same_task_is_proven() -> None:
    result = diagnose(
        dsl={
            "constraints": [
                {"type": "must", "task": "任务001"},
                {"type": "exclude", "task": "任务001"},
            ]
        },
        data=_data(tasks=[{"任务编号": "任务001"}]),
        options={"任务001": [_candidate("任务001", 100, 200)]},
        info=_info(),
    )

    assert result["结论"] == "不可行"
    assert result["原因"][0]["判定"] == PROVEN
    assert "任务001" in result["原因"][0]["证据"]


def test_task_without_any_candidate_is_proven() -> None:
    """候选生成就排不进来的任务，任何约束都救不回来——这是可证明的。"""
    result = diagnose(
        dsl={"constraints": [{"type": "exclude", "task": "任务009"}]},
        data=_data(tasks=[{"任务编号": "任务001"}, {"任务编号": "任务009"}]),
        options={"任务001": [_candidate("任务001", 100, 200)], "任务009": []},
        info=_info(),
        partial=False,
    )

    assert result["结论"] == "不可行"
    starved = [item for item in result["原因"] if "开工时刻" in item["说明"]]
    assert starved, result["原因"]
    assert "任务009" in starved[0]["证据"]


def test_capacity_bound_carries_real_numbers() -> None:
    """配额不够时，必须给出"需要多少 vs 最多能给多少"，不能只说不够。"""
    options = {
        f"任务{i:03d}": [_candidate(f"任务{i:03d}", 33120, 33120 + 8 * 60)]
        for i in range(1, 14)
    }
    tasks = [{"任务编号": f"任务{i:03d}"} for i in range(1, 14)]

    result = diagnose(
        dsl={
            "constraints": [
                {"type": "capacity", "resource": "line", "time": "2026-11-16", "limit": 1}
            ]
        },
        data=_data(tasks=tasks),
        options=options,
        info=_info(),
    )

    assert result["结论"] == "不可行"
    item = result["原因"][0]
    assert item["判定"] == UPPER_BOUND
    assert "小时" in item["证据"] and "104" in item["证据"], item["证据"]


def test_budget_bound_only_uses_direct_cost_as_lower_bound() -> None:
    options = {"任务001": [_candidate("任务001", 33120, 33120 + 8 * 60)]}

    result = diagnose(
        dsl={"constraints": [{"type": "budget", "quantity": "总经济成本", "op": "<=", "value": 100}]},
        data=_data(tasks=[{"任务编号": "任务001"}]),
        options=options,
        info=_info(),
    )

    item = result["原因"][0]
    assert item["判定"] == UPPER_BOUND
    assert "只是下界" in item["证据"], "必须说明这是下界，不能让用户以为是精确成本"


def test_cardinality_above_available_is_proven() -> None:
    result = diagnose(
        dsl={"constraints": [{"type": "cardinality", "filter": {}, "op": ">=", "value": 20}]},
        data=_data(tasks=[{"任务编号": f"任务{i:03d}"} for i in range(1, 14)]),
        options={f"任务{i:03d}": [_candidate(f"任务{i:03d}", 33120, 34000)] for i in range(1, 14)},
        info=_info(),
    )

    item = result["原因"][0]
    assert item["判定"] == PROVEN
    assert "20" in item["说明"] and "13" in item["说明"]


def test_unlocatable_says_so_without_guessing() -> None:
    """定位不到时必须承认不知道，且不许出现任何猜测性措辞。"""
    result = diagnose(
        dsl={"constraints": [{"type": "linear", "terms": [[1, "任务001"]], "op": ">=", "value": 5}]},
        data=_data(tasks=[{"任务编号": "任务001"}]),
        options={"任务001": [_candidate("任务001", 33120, 34000)]},
        info=_info(),
    )

    assert result["结论"] == "未能定位"
    text = describe(result) + "".join(item["说明"] + item["证据"] for item in result["原因"])
    for word in ("可能", "大概", "也许", "应该是", "或许是"):
        assert word not in text, f"未定位时出现了猜测性措辞：{word}"


def test_describe_has_no_dangling_separator() -> None:
    result = diagnose(
        dsl={"constraints": [{"type": "must", "task": "任务001"}, {"type": "exclude", "task": "任务001"}]},
        data=_data(tasks=[{"任务编号": "任务001"}]),
        options={"任务001": [_candidate("任务001", 100, 200)]},
        info=_info(),
    )

    text = describe(result)
    assert "：" not in text[:4] or "；" not in text[:8]
    assert not text.startswith("不可行：；")
