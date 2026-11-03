"""原语注册表：约束（七维度 + linear 兜底）与目标（3 个）。

每个原语登记：required 字段、optional 字段、原型是否已实现编译。
编译器（compiler.py）按此表把 DSL 约束编译成 CP-SAT 线性约束。
"""
from __future__ import annotations

OPS: tuple[str, ...] = ("<=", ">=", "==")

# ── 约束原语（七维度）───────────────────────────────────────────────────────

CONSTRAINT_PRIMITIVES: dict[str, dict] = {
    # 选择维度
    # must 两种写法：点名一个任务（task），或按过滤维度批量（filter）。
    # 批量那条是补一个真实的坑：「严重故障11月8日前必须修完」——只写 deadline
    # 会让排不下的任务静默进"未安排"，加上 must 才是"要么修完、要么明确不可行"。
    "must":            {"required": [], "optional": ["task", "filter"]},
    "exclude":         {"required": ["task"]},
    # only 是**范围裁剪**：「只修 1 号风机的叶片」= 任务集收缩到 filter 命中的那些，
    # 其余一律不排。和 exclude 不重复：exclude 说的是"排除命中的"，要表达"只修 X"
    # 就得枚举补集，而 DSL 的 filter 没有否定形式，模型只能瞎猜补集。
    #
    # filter 的键只能是任务行自己的属性（TASK_FILTER_KEYS），不能是 day/window/line
    # ——那是候选的属性，同一个任务可以落在不同天/窗/线，拿来筛任务没有意义。
    "only":            {"required": ["filter"]},
    "at_most_one":     {"required": ["tasks"]},
    "at_least_one":    {"required": ["tasks"]},
    # 关系维度
    "precedence":      {"required": ["before", "after"], "optional": ["gap"]},
    "co_occur":        {"required": ["mode", "tasks"]},
    # 资源维度
    "no_overlap":      {"required": ["resource", "tasks"]},
    # capacity 是**日配额**：「某天最多开 N 条线 / 动 N 台风机 / 用 N 个部件」。
    # resource 是维度名（line/turbine/part），不是具体线号——具体到哪条线用 fix。
    # time 写哪一天（日期字符串或分钟偏移），整条约束只作用在那一天。
    "capacity":        {"required": ["resource", "time", "limit"]},
    "fix":             {"required": ["task", "line"]},
    # 计数维度
    "cardinality":     {"required": ["filter", "op", "value"]},
    # 时间维度
    "deadline":        {"required": ["task", "time"]},
    "earliest":        {"required": ["task", "time"]},
    "forbidden_dates": {"required": ["dates"]},
    # 成本维度
    "budget":          {"required": ["quantity", "op", "value"]},
    # 兜底
    "linear":          {"required": ["terms", "op", "value"]},
}

# 原型已实现编译的约束原语；其余登记在册但报"原型未实现"
COMPILED_CONSTRAINTS: frozenset[str] = frozenset({
    "must", "exclude", "only", "precedence", "co_occur", "fix",
    "cardinality", "deadline", "earliest", "forbidden_dates", "budget", "linear",
    "at_most_one", "at_least_one", "capacity",
})

# 任务过滤维度：能拿来筛**任务行**的属性。
# day/window/line 不在这里——它们是候选（一个任务可以有多个候选），筛它们筛不出任务。
# must/only/cardinality/deadline 的 filter 共用这一份，避免各写各的再走偏。
TASK_FILTER_KEYS: tuple[str, ...] = ("task", "severity", "turbine", "part")

# capacity 的资源维度：问的是"那天开了几条线"这类**维度级**配额。
# crew（班组）不在里面：模型里没有班组变量，"人员总上限"只用来算线数，
# 人员是按任务工时折成本的，没有"谁在哪天干什么"。
CAPACITY_RESOURCES: tuple[str, ...] = ("line", "turbine", "part")

# no_overlap 登记在册但不实现编译。原因不是做不了（模型里已有 IntervalVar +
# AddNoOverlap，按线、按风机各一组），而是**按当前语义做出来是恒真约束**：
# 同线不重叠、同风机不重叠模型本来就保证，再加一条解空间不变。
# 要它有意义，得等班组维度（crew）或跨窗口重叠的线；那属于模型结构改动。
UNIMPLEMENTED_REASONS: dict[str, str] = {
    "no_overlap": "当前资源维度（线/风机）模型已保证不重叠，加了不改变解；班组维度尚无变量",
}

# co_occur 模式：同/不同 × 日/窗口/线；原型实现 day 两种，其余登记在册
CO_OCCUR_MODES: tuple[str, ...] = (
    "same_day", "diff_day", "same_window", "diff_window", "same_line", "diff_line",
)
COMPILED_CO_OCCUR_MODES: frozenset[str] = frozenset({"same_day", "diff_day"})

# ── 目标原语（本质 2 个 + 加权糖）───────────────────────────────────────────

OBJECTIVE_PRIMITIVES: dict[str, dict] = {
    # 策略快捷方式（第三期：四模板策略并入 DSL，mode 可选 分层/加权）
    "strategy": {"required": ["name"], "optional": ["mode"]},
    "minimize": {"required": ["q"]},
    "maximize": {"required": ["q"]},
    "lexmin":   {"required": ["stages"]},   # stages: [{"q":..., "dir":"minimize|maximize"}, ...]
    "weighted": {"required": ["weights"]},  # weights: {具名量: 权重}
}

# 原型已实现编译的目标原语：minimize/maximize/lexmin（q 限于已实现形式的量）
COMPILED_OBJECTIVES: frozenset[str] = frozenset({"strategy", "weighted", "minimize", "maximize", "lexmin"})
