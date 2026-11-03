"""意图对账的判分测试：DSL 说了什么，产物里必须找得到证据。

这些用例不跑求解器，直接构造"产物"来喂 `reconcile`，所以能精确地把每一类
"说了没做"钉死：

- 说要排除的任务还在排程里
- 说必须修的任务没排进去
- 参数写进了 DSL，但没进模型输入
- 声明了三段目标，实际只跑了一段
- 兜底方案被当成求解结果

真求解的对账另见 `test_exclude_partial.py::test_audit_passes_on_real_schedule`。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dsl_core.reconcile import reconcile

BASE = datetime(2026, 11, 1, tzinfo=timezone(timedelta(hours=8)))


class _Data:
    """最小快照桩：只要对账用到的任务表、参数和分钟换算。"""

    def __init__(self, params: dict | None = None) -> None:
        self.tasks = [
            {
                "任务编号": f"任务{i:03d}",
                "风机编号": f"风机{i:03d}",
                "部件编号": "部件01",
                "严重等级": "高" if i <= 4 else "中",
            }
            for i in range(1, 11)
        ]
        self.parts = {"部件01": {"部件名称": "叶片"}}
        self.params = dict(params or {})
        self.meta = {"参数覆盖": {}}
        self.end = 30 * 1440

    def minute(self, iso: str) -> int:
        moment = datetime.fromisoformat(iso)
        return int((moment - BASE).total_seconds() // 60)


def _report(
    ids: list[str],
    *,
    source: str = "CP_SAT",
    line: int = 1,
    start: int = 5 * 1440 + 480,
    finish: int = 5 * 1440 + 960,
    stages: list[dict] | None = None,
    cost: float = 1_000_000,
    cash: float = 500_000,
) -> dict:
    rows = [
        {
            "任务编号": task,
            "开始": start,
            "完成": finish,
            "作业线": line,
            "严重等级": "高" if int(task[-3:]) <= 4 else "中",
            "风机编号": f"风机{task[-3:]}",
            "部件名称": "叶片",
            "窗口编号": "窗口02",
        }
        for task in ids
    ]
    return {
        "求解": {"方案来源": source, "阶段计划": stages or []},
        "推荐": {"排程": rows, "总成本": cost, "现金支出": cash},
        "数据": {"参数覆盖": {}},
    }


def test_exclude_that_still_shows_up_is_a_violation() -> None:
    """预览说排除了 009-011，产物里它们还在——这就是事故原形。"""
    dsl = {"constraints": [{"type": "exclude", "task": "任务009"}]}
    report = _report([f"任务{i:03d}" for i in range(1, 11)])

    audit = reconcile(dsl, report, data=_Data())

    assert audit["通过"] is False
    assert any("任务009" in item["诉求"] for item in audit["违例"])


def test_must_task_missing_is_a_violation() -> None:
    dsl = {"constraints": [{"type": "must", "task": "任务001"}]}
    report = _report(["任务002", "任务003"])

    audit = reconcile(dsl, report, data=_Data())

    assert audit["通过"] is False
    assert audit["违例"][0]["诉求"].endswith("任务001 必须修")


def test_must_by_severity_requires_every_match() -> None:
    dsl = {"constraints": [{"type": "must", "filter": {"severity": "高"}}]}
    report = _report(["任务001", "任务002", "任务003"])

    audit = reconcile(dsl, report, data=_Data())

    assert audit["通过"] is False
    assert "任务004" in audit["违例"][0]["证据"]


def test_parameter_written_but_not_applied_is_a_violation() -> None:
    dsl = {"parameters": [{"field": "船舶日租金", "op": "=", "value": 15000}]}
    data = _Data(params={"船租（元每艘每天）": 12000})

    audit = reconcile(
        dsl,
        _report(["任务001"]),
        data=data,
        overrides={"船租（元每艘每天）": 15000},
    )

    assert audit["通过"] is False
    assert "12000" in audit["违例"][0]["证据"]


def test_fallback_solution_never_passes_audit() -> None:
    """启发式基线不是按本次约束求出来的，哪怕每一项看起来都对。"""
    report = _report(["任务001"], source="HEURISTIC_FALLBACK")

    audit = reconcile({"constraints": [{"type": "must", "task": "任务001"}]}, report, data=_Data())

    assert audit["通过"] is False
    assert any("HEURISTIC_FALLBACK" in item["证据"] for item in audit["违例"])


def test_declared_stages_must_all_run() -> None:
    """声明三段字典序，实际只跑一段，日志里看不出来，对账要看出来。"""
    dsl = {
        "objectives": [
            {
                "type": "lexmin",
                "stages": [
                    {"q": "最后完工时间", "dir": "minimize"},
                    {"q": "总经济成本", "dir": "minimize"},
                ],
            }
        ]
    }
    stages = [{"目标": "dsl_0_0_minimize", "结果": "执行"}]

    audit = reconcile(dsl, _report(["任务001"], stages=stages), data=_Data())

    assert audit["通过"] is False
    assert "声明 2 段" in audit["违例"][0]["诉求"]


def test_deadline_violation_is_caught() -> None:
    dsl = {"constraints": [{"type": "deadline", "task": "任务001", "time": "2026-11-03T00:00:00+08:00"}]}
    report = _report(["任务001"], start=5 * 1440, finish=5 * 1440 + 480)

    audit = reconcile(dsl, report, data=_Data())

    assert audit["通过"] is False
    assert "不晚于" in audit["违例"][0]["诉求"]


def test_budget_violation_is_caught() -> None:
    dsl = {
        "constraints": [
            {"type": "budget", "quantity": "现金支出", "op": "<=", "value": 100_000}
        ]
    }

    audit = reconcile(dsl, _report(["任务001"], cash=500_000), data=_Data())

    assert audit["通过"] is False
    assert "现金支出" in audit["违例"][0]["诉求"]


def test_matching_plan_passes() -> None:
    """该排的排了、该排除的没排、参数也生效了，才算通过。"""
    dsl = {
        "parameters": [{"field": "船舶日租金", "op": "=", "value": 15000}],
        "constraints": [
            {"type": "must", "task": "任务001"},
            {"type": "exclude", "task": "任务009"},
        ],
    }
    report = _report([f"任务{i:03d}" for i in range(1, 9)])

    audit = reconcile(
        dsl,
        report,
        data=_Data(params={"船租（元每艘每天）": 15000}),
        overrides={"船租（元每艘每天）": 15000},
    )

    assert audit["通过"] is True, audit["明细"]
    assert audit["检查项"] >= 3
