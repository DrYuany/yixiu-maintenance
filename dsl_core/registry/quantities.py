"""可引用量注册表：数学形式 × 系数类型 × 过滤维度 + 具名量。

可引用量 = 生成器(形式, 系数, 过滤)，不是枚举清单。
注册的是三个有限维度及其编译定义（generator.py 实现编译）。

原型实现范围：
  - 已实现形式：count（Σx）、weighted_sum（Σc·x）
  - 未实现形式：max（需辅助变量）、derived（船租等，需辅助变量）——引用时给出明确报错
"""
from __future__ import annotations

# 数学形式（5 种；max/derived 原型未实现，但登记在册以便校验器给出明确报错；
# metrics 由 build_model 的 metrics dict 提供，在编译器物化，生成器算不了）
FORMS: tuple[str, ...] = ("count", "weighted_sum", "max", "derived", "metrics")
IMPLEMENTED_FORMS: frozenset[str] = frozenset({"count", "weighted_sum", "metrics"})

# 系数类型（加权和形式的系数，从候选字段提取）
COEFFS: tuple[str, ...] = (
    "finish",        # 完成分钟
    "start",         # 开始分钟
    "day",           # 所在日（开始//1440）
    "work_hours",    # 实际作业工时（作业段总时长）
    "labor",         # 人工成本（工时×人工单价）
    "material",      # 备件成本（维修包成本）
    "other_fee",     # 其他费用
)

# 过滤维度（7 个，来源于候选字段：任务/风机/部件/严重等级/窗口/日/作业线）
FILTERS: tuple[str, ...] = (
    "task", "turbine", "part", "severity", "window", "day", "line",
)

# 具名量（糖）：名字 → 生成器表达式。原 11 个指标是首批具名量。
# 标 {"form": "metrics"} 的：由 build_model 的 metrics dict 直接提供线性表达式
# （编译器物化时引用），生成器无法从候选字段重算（max/复合形式）。
NAMED_QUANTITIES: dict[str, dict | None] = {
    "完成任务数": {"form": "count", "filter": {}},
    "高严重完成数": {"form": "count", "filter": {"severity": "高"}},
    "中严重完成数": {"form": "count", "filter": {"severity": "中"}},
    "低严重完成数": {"form": "count", "filter": {"severity": "低"}},
    "完工时间和": {"form": "weighted_sum", "coeff": "finish", "filter": {}},
    "高严重完工时间和": {"form": "weighted_sum", "coeff": "finish", "filter": {"severity": "高"}},
    "中严重完工时间和": {"form": "weighted_sum", "coeff": "finish", "filter": {"severity": "中"}},
    "低严重完工时间和": {"form": "weighted_sum", "coeff": "finish", "filter": {"severity": "低"}},
    # 以下由 build_model 的 metrics 提供（max/复合形式，生成器算不了）
    "最后完工时间": {"form": "metrics"},
    "总经济成本": {"form": "metrics"},
    "现金支出": {"form": "metrics"},
}


def resolve_quantity(ref: object) -> tuple[str, str, dict] | str | None:
    """解析量引用。

    具名量（str）→ 展开成 (form, coeff, filter)；返回 None 表示名字未注册。
    原始三元组（dict）→ 原样返回 dict，由校验器检查合法性。
    """
    if isinstance(ref, str):
        expr = NAMED_QUANTITIES.get(ref, "MISSING")
        if expr is None:
            return "UNIMPLEMENTED"  # 已注册但依赖未实现的形式
        if expr == "MISSING":
            return None
        return (expr["form"], expr.get("coeff", "one"), expr.get("filter", {}))
    if isinstance(ref, dict):
        return ref
    return None
