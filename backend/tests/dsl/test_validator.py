"""校验器单测（纯 Python，不依赖 OR-Tools/求解）。"""
from __future__ import annotations

from dsl_core.validator import validate_dsl


def test_valid_co_occur_dsl_passes_structure_check() -> None:
    dsl = {
        "constraints": [
            {"type": "co_occur", "mode": "diff_day", "tasks": ["任务018", "任务022"]},
            {"type": "precedence", "before": "任务018", "after": "任务022", "gap": 120},
        ],
        "objectives": [
            {"type": "lexmin", "stages": [
                {"q": "高严重完工时间和", "dir": "minimize"},
                {"q": {"form": "count", "filter": {"severity": "中"}}, "dir": "maximize"},
            ]},
        ],
    }
    assert validate_dsl(dsl) == []


def test_unknown_primitive_rejected() -> None:
    errors = validate_dsl({"constraints": [{"type": "magic_wand", "task": "任务018"}]})
    assert any("未知约束原语" in e for e in errors)


def test_missing_required_field_rejected() -> None:
    errors = validate_dsl({"constraints": [{"type": "precedence", "before": "任务018"}]})
    assert any("缺少必需字段 after" in e for e in errors)


def test_unknown_field_rejected() -> None:
    errors = validate_dsl({"constraints": [{"type": "must", "task": "任务018", "color": "red"}]})
    assert any("不允许字段" in e for e in errors)


def test_illegal_operator_rejected() -> None:
    errors = validate_dsl({"constraints": [{"type": "budget", "quantity": "完工时间和", "op": "≈", "value": 100}]})
    assert any("非法运算符" in e for e in errors)


def test_non_finite_bound_rejected() -> None:
    errors = validate_dsl({"constraints": [{"type": "cardinality", "filter": {}, "op": ">=", "value": "十"}]})
    assert any("有限数值" in e for e in errors)


def test_illegal_co_occur_mode_rejected() -> None:
    errors = validate_dsl({"constraints": [{"type": "co_occur", "mode": "same_universe", "tasks": ["任务001", "任务002"]}]})
    assert any("非法 co_occur 模式" in e for e in errors)


def test_co_occur_window_mode_registered_but_uncompiled() -> None:
    errors = validate_dsl({"constraints": [{"type": "co_occur", "mode": "same_window", "tasks": ["任务001", "任务002"]}]})
    assert any("原型未实现" in e for e in errors)


def test_unregistered_quantity_rejected() -> None:
    errors = validate_dsl({"objectives": [{"type": "minimize", "q": "碳排放"}]})
    assert any("未注册" in e for e in errors)


def test_metrics_form_quantities_now_valid() -> None:
    """最后完工时间/总经济成本/现金支出经 metrics 形式可引用（编译器物化）。"""
    for name in ("最后完工时间", "总经济成本", "现金支出"):
        errors = validate_dsl({"objectives": [{"type": "minimize", "q": name}]})
        assert errors == [], f"{name} 应可引用，错误: {errors}"


def test_raw_triple_with_illegal_filter_rejected() -> None:
    errors = validate_dsl({"constraints": [{
        "type": "cardinality", "filter": {"color": "红"}, "op": ">=", "value": 1,
    }]})
    assert any("未知过滤维度" in e for e in errors)


def test_raw_triple_with_illegal_coeff_rejected() -> None:
    errors = validate_dsl({"objectives": [{
        "type": "minimize", "q": {"form": "weighted_sum", "coeff": "magic", "filter": {}},
    }]})
    assert any("未知系数" in e for e in errors)


def test_linear_with_non_constant_coef_rejected() -> None:
    errors = validate_dsl({"constraints": [{
        "type": "linear", "op": "<=", "value": 100,
        "terms": [["x", "完成任务数"]],
    }]})
    assert any("系数必须有限常数" in e for e in errors)


def test_linear_cannot_reference_kernel_metrics() -> None:
    """「完工时间不能晚于11月20日」这类句子过去会被判成"能跑"，其实跑不了。

    最后完工时间/总经济成本/现金支出由求解内核的 metrics 直接提供，只有**目标**
    物化得出来；生成器算不了它。校验器原来只说"形式已实现"就放行，
    于是 validator 说没事、verify 判通过，到执行侧才抛
    `原型不支持的形式 'metrics'`——用户看到的是失败，前面三道判据全说好。
    """
    errors = validate_dsl({"constraints": [{
        "type": "linear", "op": "<=", "value": 1795161600,
        "terms": [[1, "最后完工时间"]],
    }]})
    assert any("只能在目标里引用" in e for e in errors)


def test_linear_cannot_reference_kernel_metrics_via_triple() -> None:
    errors = validate_dsl({"constraints": [{
        "type": "linear", "op": "<=", "value": 1,
        "terms": [[1, {"form": "metrics"}]],
    }]})
    assert errors


def test_linear_over_count_quantity_still_passes() -> None:
    """收紧归收紧，能用 linear 的那些不能跟着被误伤。"""
    assert validate_dsl({"constraints": [{
        "type": "linear", "op": ">=", "value": 4,
        "terms": [[1, "高严重完成数"]],
    }]}) == []


def test_must_can_target_a_whole_severity_by_filter() -> None:
    """「严重故障11月8日前必须修完」需要 must 按维度批量。

    只写 deadline 的话，排不下的任务会有零个候选、从"必须排进去"的名单里消失，
    求解报 FEASIBLE，未安排里躺着四个高危任务——用户说的是"必须修完"。
    """
    assert validate_dsl({"constraints": [
        {"type": "must", "filter": {"severity": "高"}},
    ]}) == []


def test_must_without_task_or_filter_rejected() -> None:
    errors = validate_dsl({"constraints": [{"type": "must"}]})
    assert any("需要 task 或 filter" in e for e in errors)


def test_must_with_both_task_and_filter_rejected() -> None:
    errors = validate_dsl({"constraints": [
        {"type": "must", "task": "任务001", "filter": {"severity": "高"}},
    ]})
    assert any("只能给一个" in e for e in errors)


def test_must_filter_rejects_candidate_level_dims() -> None:
    """day/window/line 是候选的属性，不是任务的属性，筛不了。"""
    errors = validate_dsl({"constraints": [
        {"type": "must", "filter": {"day": "2026-11-03"}},
    ]})
    assert any("只支持 task/severity/turbine/part" in e for e in errors)


def test_negative_weight_rejected() -> None:
    errors = validate_dsl({"objectives": [{"type": "weighted", "weights": {"完成任务数": -1.0}}]})
    assert any("权重" in e for e in errors)


def test_unknown_top_level_field_rejected() -> None:
    errors = validate_dsl({"wishes": [{"type": "must", "task": "任务001"}]})
    assert any("未知字段" in e for e in errors)


def test_tasks_list_needs_at_least_two() -> None:
    errors = validate_dsl({"constraints": [{"type": "co_occur", "mode": "diff_day", "tasks": ["任务001"]}]})
    assert any("至少两个" in e for e in errors)


# ── 第二期：parameters/data 段校验 ───────────────────────────────────────────

def test_parameter_section_validates_registry() -> None:
    errors = validate_dsl({"parameters": [{"field": "船舶日租金", "op": "*", "value": 1.2}]})
    assert errors == []


def test_parameter_unknown_field_rejected() -> None:
    errors = validate_dsl({"parameters": [{"field": "不存在的参数", "op": "=", "value": 1}]})
    assert any("不在 122 项" in e for e in errors)


def test_parameter_unchangeable_field_rejected() -> None:
    errors = validate_dsl({"parameters": [{"field": "人工审批", "op": "=", "value": "否"}]})
    assert any("不可改" in e for e in errors)


def test_parameter_bad_op_rejected() -> None:
    errors = validate_dsl({"parameters": [{"field": "船舶日租金", "op": "+", "value": 1}]})
    assert any("op 必须是" in e for e in errors)


def test_data_weather_requires_day() -> None:
    errors = validate_dsl({"data": [{"kind": "weather", "wind": 15}]})
    assert any("缺少必需字段 day" in e for e in errors)


def test_data_unknown_kind_rejected() -> None:
    errors = validate_dsl({"data": [{"kind": "magic_patch"}]})
    assert any("未知数据补丁类型" in e for e in errors)


def test_strategy_objective_valid() -> None:
    errors = validate_dsl({"objectives": [{"type": "strategy", "name": "成本最低优先"}]})
    assert errors == []


def test_strategy_objective_unknown_name_rejected() -> None:
    errors = validate_dsl({"objectives": [{"type": "strategy", "name": "随便"}]})
    assert any("未知优化策略" in e for e in errors)


def test_solver_seconds_bounds() -> None:
    assert validate_dsl({"solver": {"seconds": 60}}) == []
    assert any("最大 1800" in e for e in validate_dsl({"solver": {"seconds": 9999}}))


# ── 未接入参数（2026-10-30：校验器必须在最早一步挡住）──────────────────────
#
# 这两条原来断言"候选风机/强制维修任务 写进 parameters 合法"。它们和
# 提示词里的写法示例互相印证，看起来是自洽的，但 load_project 的白名单
# 根本不收这两个键——测试全绿，功能一用就报"未支持参数"。
# 现在改成断言"在这里就被挡住，并且告诉调用方该写到哪"。

def test_unconnected_parameter_is_rejected_early() -> None:
    errors = validate_dsl({"parameters": [{"field": "候选风机", "op": "=", "value": ["风机001", "风机002"]}]})
    assert errors and "尚未接入" in errors[0]
    assert "constraints.exclude" in errors[0], "拒绝时要给出替代出口，否则调用方只能猜"


def test_parameter_with_other_exit_points_at_that_exit() -> None:
    errors = validate_dsl({"parameters": [{"field": "强制维修任务", "op": "=", "value": ["任务001"]}]})
    assert errors and "type:must" in errors[0]
