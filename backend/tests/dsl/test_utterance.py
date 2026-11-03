"""原话 ↔ DSL 对账：只判能判死的三件事（数量、对象、数值）。

每条都钉两件事：抽不抽得到（宁可不抽，也不能抽错），以及判得对不对。
抽错比不抽更糟——不抽只是没核，抽错会把一句好变更挡在门外。
"""
from __future__ import annotations

import pytest

from dsl_core.utterance import (
    FAIL,
    PASS,
    UNVERIFIED,
    World,
    audit_utterance,
    extract_claims,
    format_audit,
    format_metrics,
    world_from_project,
)

_TURBINES = ("风机001", "风机002", "风机003")
_SEVERITIES = ("高", "中", "低")


def _world(tasks: int = 13) -> World:
    rows = []
    for index in range(1, tasks + 1):
        rows.append(
            {
                "任务编号": f"任务{index:03d}",
                "风机编号": _TURBINES[(index - 1) % len(_TURBINES)],
                "严重等级": _SEVERITIES[index % len(_SEVERITIES)],
                "部件编号": "部件01",
            }
        )
    parts = {
        "部件01": {
            "部件编号": "部件01",
            "部件名称": "叶片",
            "高严重度常态维修工时（小时）": 12,
            "中严重度常态维修工时（小时）": 8,
            "低严重度常态维修工时（小时）": 4,
        }
    }
    return World(tasks=tuple(rows), parts=parts)


def _kinds(text: str) -> list[str]:
    return [claim.kind for claim in extract_claims(text, _world())]


# ───────────────────────── 抽取：该抽的一定抽到 ─────────────────────────


def test_count_limit_phrase_is_recognised() -> None:
    claims = extract_claims("只修前8个，其余不安排", _world())

    assert [claim.kind for claim in claims] == ["count"]
    assert claims[0].payload == {"n": 8, "strict": True}


def test_count_limit_accepts_upper_bound_wording() -> None:
    claims = extract_claims("最多修5台", _world())

    assert claims[0].payload == {"n": 5, "strict": False}


def test_exclusion_range_is_expanded() -> None:
    claims = extract_claims("任务009到任务011不要排", _world())

    assert claims[0].kind == "exclude"
    assert claims[0].payload["tasks"] == ["任务009", "任务010", "任务011"]


def test_turbine_exclusion_expands_to_its_tasks() -> None:
    claims = extract_claims("003号风机别修了", _world())

    assert claims[0].kind == "exclude"
    assert claims[0].payload["turbines"] == ["风机003"]


def test_deadline_phrase_becomes_a_date_claim() -> None:
    claims = extract_claims("任务001在11月8日前必须修完", _world())

    assert claims[0].kind == "deadline"
    assert claims[0].payload["month"] == 11 and claims[0].payload["day"] == 8
    assert claims[0].payload["tasks"] == ["任务001"]


def test_forbidden_date_is_not_mistaken_for_a_deadline() -> None:
    claims = extract_claims("11月3号别开工", _world())

    assert [claim.kind for claim in claims] == ["forbidden_date"]


def test_forbidden_date_range_covers_every_day() -> None:
    """「11月3日到5日别开工」只核 3 号的话，4、5 号照样会被排活。"""
    claims = extract_claims("11月3日到5日别开工", _world())

    assert [claim.payload["day"] for claim in claims] == [3, 4, 5]


def test_deadline_before_the_date_is_recognised() -> None:
    claims = extract_claims("任务018不能晚于11月8日", _world())

    assert [claim.kind for claim in claims] == ["deadline"]
    assert claims[0].payload["tasks"] == ["任务018"]


def test_part_arrival_is_not_a_task_deadline() -> None:
    """备件到货是数据变更。判成"没写期限"会挡住一条正确的 part_arrival。"""
    assert _kinds("齿轮箱备件11月7日前到货") == []


def test_order_phrase_captures_both_ends() -> None:
    claims = extract_claims("先修任务001再修任务002", _world())

    assert claims[0].payload == {"before": "任务001", "after": "任务002"}


def test_hours_phrase_attaches_the_part() -> None:
    claims = extract_claims("叶片严重故障得修15小时", _world())

    assert claims[0].kind == "hours"
    assert claims[0].payload["hours"] == 15.0
    assert claims[0].payload["part"] == "叶片"
    assert claims[0].payload["severity"] == "高"


def test_turbine_scope_is_recognised() -> None:
    claims = extract_claims("只修003号风机", _world())

    assert [claim.kind for claim in claims] == ["turbine_scope"]
    assert claims[0].payload["turbine"] == "风机003"


# ───────────────────────── 抽取：不该抽的一个都别抽 ─────────────────────────
# 下面这些句子都是正常的参数/数据变更，抽错一条就会被挡在门外。


def test_plain_parameter_change_yields_no_claims() -> None:
    for text in (
        "船租改成5万",
        "船租上涨20%",
        "人工单价改成300元",
        "每天最多8小时",
        "最多两条作业线",
        "人员上限改成20人",
        "每天8点开工",
        "人手砍一半",
        "风机之间的转移时间改成1小时",
    ):
        assert _kinds(text) == [], text


def test_question_is_not_treated_as_an_instruction() -> None:
    assert _kinds("只修8个的话成本是多少？") == []


def test_politeness_without_numbers_yields_nothing() -> None:
    assert _kinds("好的，谢谢") == []


# ───────────────────────── 判定：违例 ─────────────────────────


def test_count_limit_without_a_cap_is_a_violation() -> None:
    """真出过的事：说只修 8 个，模型编出 8 条 must，求解把 13 个全排上。"""
    dsl = {"constraints": [{"type": "must", "task": f"任务{i:03d}"} for i in range(1, 9)]}

    audit = audit_utterance("只修前8个", dsl, _world())

    assert audit["违例"], audit
    assert "最多会排 13 个" in audit["明细"][0]["证据"]


def test_exclusion_missing_one_task_is_a_violation() -> None:
    dsl = {"constraints": [{"type": "exclude", "task": "任务009"}]}

    audit = audit_utterance("任务009到任务011不要排", dsl, _world())

    assert "任务010" in audit["明细"][0]["证据"]
    assert "任务011" in audit["明细"][0]["证据"]


def test_deadline_on_a_different_day_is_a_violation() -> None:
    dsl = {"constraints": [{"type": "deadline", "task": "任务001", "time": "2026-11-06T08:00:00+08:00"}]}

    audit = audit_utterance("任务001在11月8日前必须修完", dsl, _world())

    assert audit["违例"]
    assert "11 月 6 日" in audit["明细"][0]["证据"]


def test_missing_deadline_is_a_violation_not_a_pass() -> None:
    audit = audit_utterance("任务001在11月8日前必须修完", {}, _world())

    assert audit["违例"]
    assert "没有任何完工期限" in audit["明细"][0]["证据"]


def test_reversed_precedence_is_a_violation() -> None:
    dsl = {"constraints": [{"type": "precedence", "before": "任务002", "after": "任务001"}]}

    audit = audit_utterance("先修任务001再修任务002", dsl, _world())

    assert audit["违例"]
    assert "写反" in audit["明细"][0]["证据"]


def test_hours_not_changed_is_a_violation() -> None:
    audit = audit_utterance("叶片严重故障得修15小时", {}, _world())

    assert audit["违例"]
    assert "12 小时" in audit["明细"][0]["证据"]


def test_hours_changed_to_the_wrong_value_is_a_violation() -> None:
    dsl = {"data": [{"kind": "part_hours", "part": "叶片", "severity": "高", "hours": 13.2}]}

    audit = audit_utterance("叶片严重故障得修15小时", dsl, _world())

    assert audit["违例"]
    assert "13.2 小时" in audit["明细"][0]["证据"]


def test_turbine_scope_that_still_schedules_others_is_a_violation() -> None:
    """must 只写了 003，但不排除其余的：完整模式照样全排，等于没限定。"""
    dsl = {"constraints": [{"type": "must", "filter": {"turbine": "风机003"}}]}

    audit = audit_utterance("只修003号风机", dsl, _world())

    assert audit["违例"]
    assert "全排上" in audit["明细"][0]["证据"]


def test_turbine_scope_with_exclusions_passes() -> None:
    others = [f"任务{i:03d}" for i in range(1, 14) if (i - 1) % 3 != 2]
    dsl = {"constraints": [{"type": "exclude", "task": task_id} for task_id in others]}

    audit = audit_utterance("只修003号风机", dsl, _world())

    assert audit["明细"][0]["结论"] == PASS


# ───────────────────────── 判定：通过 ─────────────────────────


def test_count_limit_backed_by_cardinality_passes() -> None:
    dsl = {"constraints": [{"type": "cardinality", "filter": {}, "op": "<=", "value": 8}]}

    audit = audit_utterance("只修前8个", dsl, _world())

    assert audit["违例"] == []
    assert audit["明细"][0]["结论"] == PASS


def test_count_limit_backed_by_exclusions_passes() -> None:
    dsl = {"constraints": [{"type": "exclude", "task": f"任务{i:03d}"} for i in range(9, 14)]}

    audit = audit_utterance("只修前8个", dsl, _world())

    assert audit["明细"][0]["结论"] == PASS
    assert "排除 5 个" in audit["明细"][0]["证据"]


def test_exclusion_covered_by_a_filter_passes() -> None:
    dsl = {"constraints": [{"type": "exclude", "filter": {"turbine": "风机003"}}]}

    audit = audit_utterance("003号风机别修了", dsl, _world())

    assert audit["明细"][0]["结论"] == PASS


def test_hours_patch_with_the_right_value_passes() -> None:
    """定点定值：数值对上，且范围没超出用户点名的严重度，才判通过。"""
    dsl = {"data": [{"kind": "part_hours", "part": "叶片", "severity": "高", "hours": 15}]}

    audit = audit_utterance("叶片严重故障得修15小时", dsl, _world())

    assert audit["明细"][0]["结论"] == PASS
    assert "15 小时" in audit["明细"][0]["证据"]


def test_whole_part_factor_that_matches_the_number_is_still_out_of_scope() -> None:
    """×1.25 恰好把高风险 12 凑成 15，数值对上了，但中/低风险也被乘了。

    「定点修改」要防的正是这种：数值核对抓不到，得单独判范围有没有越界。
    """
    dsl = {"data": [{"kind": "part_hours", "part": "叶片", "factor": 1.25}]}

    audit = audit_utterance("叶片严重故障得修15小时", dsl, _world())

    assert audit["违例"]
    assert "超出" in audit["明细"][0]["证据"]


def test_deadline_with_the_same_day_passes() -> None:
    dsl = {"constraints": [{"type": "deadline", "task": "任务001", "time": "2026-11-08T08:00:00+08:00"}]}

    audit = audit_utterance("任务001在11月8日前必须修完", dsl, _world())

    assert audit["明细"][0]["结论"] == PASS


# ───────────────────────── 未核对：说不知道，不说通过 ─────────────────────────


def test_unknown_task_is_reported_as_unverified() -> None:
    audit = audit_utterance("任务099不要排", {"constraints": [{"type": "exclude", "task": "任务099"}]}, _world())

    assert audit["明细"][0]["结论"] == UNVERIFIED
    assert audit["通过"] is False


def test_no_claim_does_not_count_as_a_pass() -> None:
    audit = audit_utterance("船租改成5万", {"parameters": [{"field": "船舶日租金", "op": "=", "value": 50000}]}, _world())

    assert audit["检查项"] == 0
    assert audit["通过"] is False, "一条都没核，不能算通过"
    assert audit["结论"] == "没有可核对的诉求"
    assert "不能说它等于你要的那件事" in format_audit(audit)


def test_missing_world_says_so_instead_of_passing() -> None:
    audit = audit_utterance("只修前8个", {"constraints": []}, None)

    assert audit["通过"] is False
    assert "没法核对" in audit["结论"]


def test_leftover_fragments_are_listed_verbatim() -> None:
    audit = audit_utterance("只修前8个，其余你看着办", {}, _world())

    assert "其余你看着办" in audit["未核对片段"]


def test_format_audit_marks_each_claim() -> None:
    dsl = {"constraints": [{"type": "exclude", "task": "任务009"}]}
    text = format_audit(audit_utterance("任务009到任务011不要排", dsl, _world()))

    assert "对不上" in text
    assert "任务009到任务011不要排" not in text  # 列的是机器表述，不是整句复读
    assert "求解会照排" in text


# ───────────────────────── 真实在册数据 ─────────────────────────


def test_real_project_world_loads() -> None:
    world = world_from_project()

    assert world is not None, "读不到在册任务，对账就成了摆设"
    assert world.task_ids
    assert all(row["任务编号"].startswith("任务") for row in world.tasks)


def test_real_project_catches_the_reported_regression() -> None:
    """复盘里那句"只修前 8 个"对着真实在册数据也要判得出来。"""
    world = world_from_project()
    assert world is not None
    total = len(world.task_ids)
    dsl = {"constraints": [{"type": "must", "task": task_id} for task_id in sorted(world.task_ids)[:8]]}

    audit = audit_utterance("只修前8个", dsl, world)

    if total > 8:
        assert audit["违例"], f"在册 {total} 个，只压了 8 个必须修，应当判违例"
        assert f"最多会排 {total} 个" in audit["明细"][0]["证据"]


# ───────────── 真求解：对账说"对上"的，产物必须真的是那样 ─────────────
# 这几条要跑 CP-SAT，标 slow。不跑真实求解就断言，等于只验了文字。


@pytest.mark.slow
def test_audit_and_real_solver_agree_on_the_count_limit() -> None:
    """「只修 8 个」：DSL 写了上限 → 对账对上 → 产物真的只有 8 个。"""
    pytest.importorskip("ortools")
    from dsl_core.runner import run

    world = world_from_project()
    assert world is not None
    dsl = {"constraints": [{"type": "cardinality", "filter": {}, "op": "<=", "value": 8}]}

    audit = audit_utterance("只修前8个", dsl, world)
    report = run(dsl, seconds=60)
    scheduled = len(report["排程"] or [])

    assert audit["违例"] == [], audit["明细"]
    assert scheduled <= 8, f"对账说最多 8 个，实际排了 {scheduled} 个"
    assert report["求解"]["意图对账"]["通过"] is True


@pytest.mark.slow
def test_audit_catches_what_the_solver_would_really_do() -> None:
    """对照组：只写 8 条 must，对账判违例——真实求解确实会把在册任务全排上。

    这条是整段的理由：不是"理论上可能漏"，是"不拦就一定多排"。
    """
    pytest.importorskip("ortools")
    from dsl_core.runner import run

    world = world_from_project()
    assert world is not None
    dsl = {"constraints": [{"type": "must", "task": f"任务{i:03d}"} for i in range(1, 9)]}

    audit = audit_utterance("只修前8个", dsl, world)
    report = run(dsl, seconds=60)
    scheduled = len(report["排程"] or [])

    assert audit["违例"], "8 条 must 不等于只修 8 个，必须判违例"
    assert scheduled > 8, f"如果真实求解只排 8 个，这条对账就是多余的；实际排了 {scheduled}"


def test_executor_refuses_before_solving_when_the_utterance_disagrees() -> None:
    """第二道门：求解执行体自己也要拦。

    预览阶段已经拦过一次，但 pending 可能来自上一轮、也可能被人改过。
    只靠前一道门，等于把"执行的必须是用户批准的那件事"押在一条路径上。
    """
    from app.core.maintenance_product_executor import (
        MaintenanceProductExecutionError,
        MaintenanceProductExecutor,
    )
    from app.core.planning_intent import PlanningRequest

    executor = MaintenanceProductExecutor()
    dsl = {"constraints": [{"type": "must", "task": "任务001"}]}

    with pytest.raises(MaintenanceProductExecutionError) as caught:
        executor.execute(PlanningRequest(seconds=5), "job-utterance", dsl=dsl, utterance="只修前8个")

    message = str(caught.value)
    assert "原话核对未通过" in message
    assert "没有把数量压到 8 个" in message


# ───────────── 复盘指标：断言覆盖率 / 对账召回（A2） ─────────────
# 两个数只披露、不参与判定。加它们的目的：单看「对上 / 检查项」会把
# 「抽得少」伪装成「判得准」（Precision 虚高），得有分母把它戳破。


def test_metrics_are_exposed_with_recomputable_numerator_and_denominator() -> None:
    dsl = {"constraints": [{"type": "cardinality", "filter": {}, "op": "<=", "value": 8}]}

    audit = audit_utterance("只修前8个", dsl, _world())

    assert audit["断言覆盖率"] == 1.0
    assert audit["对账召回"] == 1.0
    metrics = audit["评测口径"]
    assert metrics["已覆盖片段"] == metrics["值得列出片段"] == 1
    assert metrics["已判定断言"] == metrics["检查项"] == 1
    assert metrics["未覆盖片段"] == 0
    assert metrics["未核对断言"] == 0


def test_coverage_drops_when_a_worthwhile_fragment_has_no_claim() -> None:
    """「其余你看着办」值得列出，但抽不出断言 —— 覆盖率必须掉下来。"""
    audit = audit_utterance("只修前8个，其余你看着办", {}, _world())

    metrics = audit["评测口径"]
    assert metrics["值得列出片段"] == 2
    assert metrics["未覆盖片段"] == 1
    assert audit["断言覆盖率"] == 0.5
    assert "其余你看着办" in audit["未核对片段"]


def test_recall_drops_when_a_claim_cannot_be_judged() -> None:
    """任务不在册 → 判不了 → 未核对。对账召回要如实低于 1。"""
    audit = audit_utterance(
        "任务099不要排", {"constraints": [{"type": "exclude", "task": "任务099"}]}, _world()
    )

    assert audit["明细"][0]["结论"] == UNVERIFIED
    assert audit["评测口径"]["已判定断言"] == 0
    assert audit["对账召回"] == 0.0
    assert audit["通过"] is False


def test_metrics_are_none_when_nothing_is_worth_listing() -> None:
    """纯参数变更没有能判的片段：分母 0 不许报成 100%，闸门口径也不能变。"""
    audit = audit_utterance(
        "船租改成5万", {"parameters": [{"field": "船舶日租金", "op": "=", "value": 50000}]}, _world()
    )

    assert audit["评测口径"]["值得列出片段"] == 0
    assert audit["断言覆盖率"] is None
    assert audit["对账召回"] is None
    assert audit["通过"] is False, "披露数字不能把『根本没核』变成通过"


def test_metrics_are_none_when_world_cannot_be_read() -> None:
    audit = audit_utterance("只修前8个", {}, None)

    assert audit["断言覆盖率"] is None
    assert audit["对账召回"] is None
    assert "读不到" in audit["评测口径"]["说明"]


def test_metrics_do_not_change_the_gate_verdict() -> None:
    """回归护栏：加了两个数字，『通过』仍由违例/未核对决定，不受覆盖率影响。"""
    dsl = {"constraints": [{"type": "exclude", "task": f"任务{i:03d}"} for i in range(9, 14)]}

    audit = audit_utterance("只修前8个", dsl, _world())

    assert audit["断言覆盖率"] == 1.0
    assert audit["对账召回"] == 1.0
    assert audit["通过"] is True
    assert audit["违例"] == [] and audit["未核对"] == []


def test_format_metrics_reports_fractions_not_just_percentages() -> None:
    text = format_metrics(audit_utterance("只修前8个", {}, _world()))

    assert "复盘口径" in text
    assert "1/1" in text
    assert "100%" in text


def test_format_metrics_never_claims_full_score_on_empty_denominator() -> None:
    text = format_metrics(audit_utterance("船租改成5万", {}, _world()))

    assert "没有能判的片段" in text
    assert "一条断言都没抽到" in text
    assert "100%" not in text
    assert "不算通过" in text
