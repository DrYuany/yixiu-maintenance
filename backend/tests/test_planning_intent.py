from __future__ import annotations

from app.core.planning_intent import PlanningIntentCompiler


def test_high_risk_request_compiles_to_registered_strategy_deterministically() -> None:
    compiler = PlanningIntentCompiler()

    first = compiler.compile("按高风险优先重新排程")
    second = compiler.compile("按高风险优先重新排程")

    assert first == second
    assert first.strategy == "高风险优先抢修"
    assert first.mode == "分层"
    # 没点名时限就不该编一个数：交给参数总表（求解01），公告按它如实说。
    assert first.seconds is None


def test_operational_overrides_are_compiled_without_llm_guessing() -> None:
    compiled = PlanningIntentCompiler().compile(
        "船租上涨20%，最多两条作业线，每天最多维修8小时，重新排程"
    )

    assert compiled.relative_overrides == {"船租（元每艘每天）": 1.2}
    assert compiled.overrides == {
        "最多作业线数": 2,
        "每天最多维修小时数": 8,
    }


def test_plain_reschedule_reuses_current_contract() -> None:
    compiler = PlanningIntentCompiler()
    current = compiler.compile("按高风险优先重新排程")

    rerun = compiler.compile("重新排程", current=current)

    assert rerun.strategy == "高风险优先抢修"
    assert rerun.overrides == current.overrides


def test_product_questions_do_not_start_a_new_solve() -> None:
    compiler = PlanningIntentCompiler()

    assert compiler.is_planning_request("你用的什么算法求解") is False
    assert compiler.is_planning_request("为什么这个方案成本更低？") is False
    assert compiler.is_planning_request("按高风险优先重新排程") is True


def test_solver_budget_can_be_set_without_relabelling_timeout() -> None:
    compiled = PlanningIntentCompiler().compile("按高风险优先重新排程，求解最多30秒")

    assert compiled.seconds == 30


def test_weighted_objective_fills_unmentioned_metrics_with_zero() -> None:
    compiled = PlanningIntentCompiler().compile(
        "按高风险优先抢修，改成加权模式，高严重完成数权重 0.8，总经济成本权重 0.2，重新排程"
    )

    assert compiled.strategy == "高风险优先抢修"
    assert compiled.mode == "加权"
    assert compiled.weights == {
        "高严重完成数": 0.8,
        "中严重完成数": 0.0,
        "低严重完成数": 0.0,
        "总经济成本": 0.2,
        "最后完工时间": 0.0,
    }


def test_weight_on_metric_outside_strategy_is_reported_not_silently_dropped() -> None:
    intent = PlanningIntentCompiler().compile_detailed(
        "改成加权模式，高严重完成数权重 0.8，总经济成本权重 0.2，重新排程"
    )

    assert intent.blocked
    assert intent.request.weights is None
    assert any("不在「成本最低优先」的可加权指标里" in item for item in intent.errors)
    assert any("高风险优先抢修" in item for item in intent.errors)


def test_capability_question_about_weights_is_not_a_planning_change() -> None:
    compiler = PlanningIntentCompiler()

    assert compiler.is_planning_request("制定排程时，我有哪些目标函数可以设置权重") is False
    assert compiler.is_planning_request("权重怎么设置") is False
    assert compiler.is_planning_request("高严重完成数权重改成0.8，重新排程") is True


def test_strategy_switch_in_weighted_mode_resets_to_that_strategy_defaults() -> None:
    compiler = PlanningIntentCompiler()
    current = compiler.compile("改成加权模式，重新排程")

    switched = compiler.compile("按高风险优先抢修重新排程", current=current)

    assert switched.mode == "加权"
    assert switched.weights is not None
    assert set(switched.weights) == {
        "高严重完成数",
        "中严重完成数",
        "低严重完成数",
        "总经济成本",
        "最后完工时间",
    }
    assert abs(switched.weights["高严重完成数"] - 0.45) < 1e-9


def test_weights_without_saying_weighted_mode_switch_mode_automatically() -> None:
    compiled = PlanningIntentCompiler().compile(
        "总经济成本权重0.7，完成任务数权重0.3，重新排程"
    )

    assert compiled.mode == "加权"
    assert compiled.weights == {
        "总经济成本": 0.7,
        "完成任务数": 0.3,
        "最后完工时间": 0.0,
        "完工时间和": 0.0,
    }


def test_layered_mode_with_weights_is_rejected_as_conflict() -> None:
    intent = PlanningIntentCompiler().compile_detailed(
        "改成分层模式，总经济成本权重0.7，重新排程"
    )

    assert intent.blocked
    assert intent.request.weights is None


def test_daily_hours_can_be_rewritten_in_plain_language() -> None:
    compiled = PlanningIntentCompiler().compile("每天作业时间改成6小时，重新排程")

    assert compiled.overrides["每天最多维修小时数"] == 6


def test_ship_rent_can_be_set_as_an_absolute_value() -> None:
    compiled = PlanningIntentCompiler().compile("船租改成1.5万，重新排程")

    assert compiled.overrides["船租（元每艘每天）"] == 15_000


def test_overtime_daily_hours_phrase_is_recognized_by_the_compiler() -> None:
    compiler = PlanningIntentCompiler()
    message = "每天可以加班，最多维修10小时"

    assert compiler.is_planning_request(message) is True
    assert compiler.compile(message).overrides["每天最多维修小时数"] == 10


def test_daily_hours_two_digit_value_is_not_truncated() -> None:
    compiled = PlanningIntentCompiler().compile("每天最多维修10小时")

    assert compiled.overrides["每天最多维修小时数"] == 10



def test_same_line_phrase_is_not_parsed_as_work_line_count() -> None:
    """「同一条线」指同一条作业线，不是「作业线数=1」。"""
    compiled = PlanningIntentCompiler().compile("同一条线干完一个要歇2小时")
    assert "最多作业线数" not in compiled.overrides


def test_work_line_count_still_recognized() -> None:
    compiled = PlanningIntentCompiler().compile("最多两条线")
    assert compiled.overrides["最多作业线数"] == 2
