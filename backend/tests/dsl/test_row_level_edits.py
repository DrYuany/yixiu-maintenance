"""行级定点修改：工时按（部件 × 严重度 × 海况）定绝对值，任务集按维度裁剪。

验收口径和项目一致：不是"代码跑通了"，而是**改动在数据/产物里留下了可核对的痕迹**。
快测钉死三件事：定点只动该动的格子、DSL 收不收、原话对账判不判得死。
慢测真求解一次，证明约束不只是被解析，而是排程里真的只剩那几个任务。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dsl_core.compiler import needs_partial
from dsl_core.utterance import FAIL, PASS, World, audit_utterance, extract_claims
from dsl_core.validator import validate_dsl
from product_data import _patched_business

_BASE = datetime(2026, 10, 23, 18, 0, tzinfo=timezone(timedelta(hours=8)))


def _business() -> dict:
    return {
        "部件库": [
            {
                "部件编号": "部件01",
                "部件名称": "叶片",
                "高严重度常态维修工时（小时）": 12,
                "高严重度恶劣但安全维修工时（小时）": 18,
                "中严重度常态维修工时（小时）": 8,
                "中严重度恶劣但安全维修工时（小时）": 12,
                "低严重度常态维修工时（小时）": 4,
                "低严重度恶劣但安全维修工时（小时）": 6,
            }
        ],
        "库存收发计划": [],
    }


def _row(patches: list[dict]) -> dict:
    patched, _frozen = _patched_business(_business(), patches, _BASE, 60 * 24 * 30)
    return patched["部件库"][0]


# ── 工时定点：真落到部件行，且只落那一格 ────────────────────────────────────


def test_hours_absolute_changes_only_that_severity() -> None:
    """「叶片高风险修 15 小时」：高严重度两格变 15，中/低一格不动。"""
    row = _row([{"kind": "part_hours", "partId": "部件01", "severity": "高", "hours": 15}])
    assert row["高严重度常态维修工时（小时）"] == 15
    assert row["高严重度恶劣但安全维修工时（小时）"] == 15
    assert row["中严重度常态维修工时（小时）"] == 8
    assert row["中严重度恶劣但安全维修工时（小时）"] == 12
    assert row["低严重度常态维修工时（小时）"] == 4


def test_hours_absolute_can_target_one_sea_column() -> None:
    """再给 sea，就只改那一列；另一列保持原值。"""
    row = _row([{
        "kind": "part_hours", "partId": "部件01", "severity": "高",
        "sea": "常态", "hours": 15,
    }])
    assert row["高严重度常态维修工时（小时）"] == 15
    assert row["高严重度恶劣但安全维修工时（小时）"] == 18


def test_factor_still_multiplies_the_whole_part() -> None:
    """回归：老的整件倍率写法不能因为加了 hours 就变语义。"""
    row = _row([{"kind": "part_hours", "partId": "部件01", "factor": 1.2}])
    assert row["高严重度常态维修工时（小时）"] == pytest.approx(14.4)
    assert row["高严重度恶劣但安全维修工时（小时）"] == pytest.approx(21.6)
    assert row["中严重度常态维修工时（小时）"] == pytest.approx(9.6)


def test_factor_can_also_be_scoped() -> None:
    """倍率同样支持定点：只涨高严重度，中/低不动。"""
    row = _row([{"kind": "part_hours", "partId": "部件01", "severity": "高", "factor": 2}])
    assert row["高严重度常态维修工时（小时）"] == 24
    assert row["高严重度恶劣但安全维修工时（小时）"] == 36
    assert row["中严重度常态维修工时（小时）"] == 8


def test_patches_apply_in_order_absolute_then_factor() -> None:
    """顺序不能乱：先定 15 小时，再 ×2，得到 30；反过来是 15。"""
    a = _row([
        {"kind": "part_hours", "partId": "部件01", "severity": "高", "sea": "常态", "hours": 15},
        {"kind": "part_hours", "partId": "部件01", "severity": "高", "sea": "常态", "factor": 2},
    ])
    b = _row([
        {"kind": "part_hours", "partId": "部件01", "severity": "高", "sea": "常态", "factor": 2},
        {"kind": "part_hours", "partId": "部件01", "severity": "高", "sea": "常态", "hours": 15},
    ])
    assert a["高严重度常态维修工时（小时）"] == 30
    assert b["高严重度常态维修工时（小时）"] == 15


def test_bad_scope_is_rejected_not_silently_ignored() -> None:
    with pytest.raises(ValueError, match="严重度"):
        _row([{"kind": "part_hours", "partId": "部件01", "severity": "特高", "hours": 15}])
    with pytest.raises(ValueError, match="必须为正"):
        _row([{"kind": "part_hours", "partId": "部件01", "hours": 0}])


# ── DSL 校验 ───────────────────────────────────────────────────────────────


def test_validator_accepts_both_hours_forms_and_rejects_mixing() -> None:
    assert validate_dsl({
        "data": [{"kind": "part_hours", "part": "叶片", "severity": "高", "hours": 15}]
    }) == []
    assert validate_dsl({
        "data": [{"kind": "part_hours", "part": "叶片", "factor": 1.2}]
    }) == []
    errors = validate_dsl({
        "data": [{"kind": "part_hours", "part": "叶片", "factor": 1.2, "hours": 15}]
    })
    assert any("只能给一个" in text for text in errors)
    assert any("之一" in text for text in validate_dsl({
        "data": [{"kind": "part_hours", "part": "叶片"}]
    }))


def test_validator_rejects_bad_severity_sea_and_candidate_only_filters() -> None:
    errors = validate_dsl({
        "data": [{"kind": "part_hours", "part": "叶片", "severity": "特高", "sea": "台风", "hours": 15}]
    })
    assert any("severity" in text for text in errors)
    assert any("sea" in text for text in errors)

    errors = validate_dsl({"constraints": [{"type": "only", "filter": {}}]})
    assert any("不能为空" in text for text in errors)
    errors = validate_dsl({"constraints": [{"type": "only", "filter": {"day": 2}}]})
    assert any("only 的 filter 只支持" in text for text in errors)
    assert validate_dsl({
        "constraints": [{"type": "only", "filter": {"turbine": "风机001", "part": "叶片"}}]
    }) == []


def test_only_forces_partial_scheduling() -> None:
    """完整模式给每个任务钉 sum(x)==1，跟"只修这几个"直接打架。"""
    assert needs_partial({"constraints": [{"type": "only", "filter": {"part": "叶片"}}]}) is True
    assert needs_partial({"constraints": [{"type": "at_least_one", "tasks": ["任务001"]}]}) is False


# ── 原话 ↔ DSL 对账 ────────────────────────────────────────────────────────


def _world(parts_blade: bool = True) -> World:
    rows = []
    for index in range(1, 13):
        rows.append({
            "任务编号": f"任务{index:03d}",
            "风机编号": f"风机{(index - 1) % 3 + 1:03d}",
            "严重等级": ("高", "中", "低")[index % 3],
            "部件编号": "部件01" if parts_blade or index % 2 else "部件02",
        })
    parts = {
        "部件01": {
            "部件编号": "部件01", "部件名称": "叶片",
            "高严重度常态维修工时（小时）": 12,
            "高严重度恶劣但安全维修工时（小时）": 18,
            "中严重度常态维修工时（小时）": 8,
            "低严重度常态维修工时（小时）": 4,
        },
        "部件02": {
            "部件编号": "部件02", "部件名称": "齿轮箱",
            "高严重度常态维修工时（小时）": 10,
            "高严重度恶劣但安全维修工时（小时）": 15,
            "中严重度常态维修工时（小时）": 6,
            "低严重度常态维修工时（小时）": 3,
        },
    }
    return World(tasks=tuple(rows), parts=parts)


def test_utterance_hours_absolute_is_checked() -> None:
    dsl = {"data": [{"kind": "part_hours", "part": "叶片", "severity": "高", "hours": 15}]}
    audit = audit_utterance("叶片在高风险需要修15小时", dsl, _world(), partial=False)
    assert audit["对上"] == 1, audit
    assert audit["违例"] == []

    # 写成倍率就对不上：系统必须能算出差在哪，不能"大概一致"就放行。
    wrong = {"data": [{"kind": "part_hours", "part": "叶片", "factor": 1.25}]}
    audit = audit_utterance("叶片在高风险需要修15小时", wrong, _world(), partial=False)
    assert audit["违例"], audit


def test_utterance_hours_sea_scope_is_checked_on_that_column() -> None:
    dsl = {"data": [{
        "kind": "part_hours", "part": "叶片", "severity": "高",
        "sea": "恶劣但安全", "hours": 20,
    }]}
    audit = audit_utterance("叶片在恶劣海况下修20小时", dsl, _world(), partial=False)
    assert audit["对上"] == 1, audit
    assert audit["违例"] == []


def test_utterance_scope_claim_keeps_the_part_constraint() -> None:
    """「只修1号风机的叶片」——只写风机、漏掉部件，必须判不通过。"""
    world = _world(parts_blade=False)
    claims = extract_claims("只修1号风机的叶片", world)
    assert len(claims) == 1
    assert claims[0].payload["part"] == "叶片"

    full = {"constraints": [{"type": "only", "filter": {"turbine": "风机001", "part": "叶片"}}]}
    assert audit_utterance("只修1号风机的叶片", full, world, partial=True)["违例"] == []

    loose = {"constraints": [{"type": "only", "filter": {"turbine": "风机001"}}]}
    audit = audit_utterance("只修1号风机的叶片", loose, world, partial=True)
    assert audit["违例"], "漏掉部件限定却判通过，等于把用户说的『叶片』吞了"


def test_utterance_scope_without_only_fails() -> None:
    audit = audit_utterance("只修1号风机的叶片", {}, _world(parts_blade=False), partial=True)
    assert audit["违例"]


# ── 预览文本：用户批准前必须看得见定点范围 ─────────────────────────────────


def test_preview_text_shows_the_scope() -> None:
    from app.core.model_change import _patch_text

    text = _patch_text({"kind": "part_hours", "partName": "叶片", "severity": "高", "hours": 15})
    assert "15 小时" in text and "高严重度" in text
    text = _patch_text({"kind": "part_hours", "partName": "叶片", "factor": 1.2})
    assert "整件工时" in text and "两种海况" in text


# ── 自然语言解析（规则路径）──────────────────────────────────────────────


def test_intent_parses_hours_absolute_without_hijacking_strategy() -> None:
    from app.core.planning_intent import PlanningIntentCompiler

    compiler = PlanningIntentCompiler()
    result = compiler.compile_detailed("叶片在高风险需要修15小时")
    assert result.request.data_patches == [{
        "kind": "part_hours", "partId": "部件01", "partName": "叶片",
        "hours": 15.0, "severity": "高",
    }]
    # "高风险"是工时档位，不是"按风险优先排"——策略不能被这句话偷偷换掉。
    assert result.request.strategy == "成本最低优先"
    assert PlanningIntentCompiler.is_planning_request("叶片在高风险需要修15小时")


def test_intent_does_not_confuse_daily_cap_with_part_hours() -> None:
    from app.core.planning_intent import PlanningIntentCompiler

    result = PlanningIntentCompiler().compile_detailed("叶片每天最多修8小时")
    assert result.request.data_patches == []
    assert result.request.overrides["每天最多维修小时数"] == 8


def test_intent_keeps_scoped_patches_separate() -> None:
    """整件倍率 + 高风险定点，是两件事，不能被去重成一条。"""
    from app.core.planning_intent import PlanningIntentCompiler

    compiler = PlanningIntentCompiler()
    first = compiler.compile("叶片维修工时上涨20%")
    second = compiler.compile_detailed("叶片高风险修15小时", current=first)
    assert len(second.request.data_patches) == 2
    assert {p.get("severity") for p in second.request.data_patches} == {None, "高"}


# ── 真求解（慢测）─────────────────────────────────────────────────────────


@pytest.mark.slow
def test_only_really_limits_the_schedule() -> None:
    """约束不只是被解析：产物里只能出现 风机001 的叶片任务。"""
    from dsl_core.runner import PROJECT_ROOT, _solver_modules, run

    load_project, *_ = _solver_modules()
    data = load_project(PROJECT_ROOT, base_id="基座01")
    keep = {
        task["任务编号"] for task in data.tasks
        if task["风机编号"] == "风机001"
        and data.parts[task["部件编号"]]["部件名称"] == "叶片"
    }
    assert keep, "演示数据里 风机001 应当有叶片任务，否则这条用例没有意义"

    report = run(
        {"constraints": [{"type": "only", "filter": {"turbine": "风机001", "part": "叶片"}}]},
        seconds=60,
    )
    scheduled = {row["任务编号"] for row in report["排程"] or []}
    assert scheduled, "范围裁剪后应当仍然解得出来"
    assert scheduled <= keep, scheduled


@pytest.mark.slow
def test_hours_absolute_really_enters_the_model() -> None:
    """工时定点改完仍能求解，且原话对账通过——不是只在内存里改了个字典。"""
    from dsl_core.runner import run

    report = run(
        {"data": [{"kind": "part_hours", "part": "叶片", "severity": "高", "hours": 15}]},
        seconds=60,
    )
    assert report["排程"] is not None
    assert report["求解"]["意图对账"]["通过"] is True
