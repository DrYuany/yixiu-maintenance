"""编译器：DSL → CP-SAT 约束/目标（物化层）。

通过 product_planner.build_model 的注入闭包工作：
  - constraints_builder：build_model 建完 choices 后回调，把 DSL 约束加进模型
  - stages_builder：返回自定义分层目标，绕过四模板

量纲缩放（CP-SAT 只吃整数系数）：
  - 时间量（finish/start/day）        → 分钟，整数，scale 1
  - 工时（work_hours）                → 分钟（作业段分钟和），scale 60
  - 成本量（labor/material/other_fee）→ 微元（×1e6，与生产 MONEY 一致）

原型限制（明确报错，不静默给错数）：
  - 同一 linear 表达式内不允许混合时间量与成本量（量纲不同）
  - max/derived 形式未实现（校验器已拦）
"""
from __future__ import annotations

from typing import Any, Callable

from . import generator
from .registry import primitives as prim_reg
from .registry import quantities as qty_reg
from .registry.entities import resolve_day, resolve_minute, resolve_task

MONEY = 1_000_000  # 微元，与 product_planner.MONEY 一致

# must/only 按维度批量时允许的过滤键：只留任务行本身就有的字段。
# day/window/line 是**候选**的属性（同一个任务可以排在不同天/窗/线），
# 拿来筛任务没有意义，给了就明确报错，不要猜。
_MUST_FILTERS = frozenset(prim_reg.TASK_FILTER_KEYS)

# 出现这些原语说明用户在**挑拣**要修哪些，而不是"全都得修"。
# 完整模式给每个任务加 sum(x)==1（必须排入），跟"少修"直接矛盾：
# exclude 的 sum(x)==0 会立刻打架，cardinality 的上限则会在求解后期被证明
# 不可行，而诊断还给不出原因。所以见到就切部分排程。
#
# at_least_one 不在此列：它只说"至少一个"，没说其余能不能排，完整模式下恒真；
# 切 partial 反而会把"全修"降级成"挑一个"。
_SELECTION_PRIMITIVES = frozenset({"exclude", "only", "at_most_one"})


def needs_partial(dsl: dict[str, Any]) -> bool:
    """这份 DSL 是不是在"挑拣任务"，也就是必须走部分排程。

    只在能判死的地方返回 True：
      - exclude / only / at_most_one：本身就是"少修几个"的选择语义
      - cardinality 的上限（<= / ==）：完整模式下每个任务都被强制排入，
        这类上限只要小于在册任务数就必然不可行
    """
    for spec in dsl.get("constraints") or []:
        ctype = str(spec.get("type"))
        if ctype in _SELECTION_PRIMITIVES:
            return True
        if ctype == "cardinality" and str(spec.get("op") or "=") in ("<=", "=", "=="):
            return True
    return False


def task_matches(task: dict, filter_spec: dict, data) -> bool:
    """任务是否命中过滤维度（must/cardinality 共用；对账也复用它）。"""
    for dim, value in filter_spec.items():
        if dim == "task":
            ok = task["任务编号"] == value
        elif dim == "severity":
            ok = task["严重等级"] == value
        elif dim == "turbine":
            ok = task["风机编号"] == value
        elif dim == "part":
            part = data.parts.get(task["部件编号"])
            ok = value in (task["部件编号"], task.get("部件名称"),
                           part["部件名称"] if part else None)
        else:  # pragma: no cover - 调用方已拦
            return False
        if not ok:
            return False
    return True


def _scale_of_coeff(coeff: str) -> int:
    if coeff in ("finish", "start", "day"):
        return 1
    if coeff == "work_hours":
        return 60  # 小时 → 分钟
    if coeff in ("labor", "material", "other_fee"):
        return MONEY
    return 1


def _coeff_of_quantity(ref: object) -> str:
    resolved = qty_reg.resolve_quantity(ref)
    if isinstance(resolved, tuple):
        return resolved[1]
    if isinstance(resolved, dict):
        return resolved.get("coeff", "one")
    raise ValueError(f"量引用无法解析: {ref!r}")


# 由 build_model metrics 提供的具名量的量纲缩放（微元 vs 分钟）
_METRICS_SCALES = {
    "总经济成本": MONEY, "现金支出": MONEY,
    "最后完工时间": 1, "完工时间和": 1, "完成任务数": 1,
    "高严重完成数": 1, "中严重完成数": 1, "低严重完成数": 1,
    "高严重完工时间和": 1, "中严重完工时间和": 1, "低严重完工时间和": 1,
}


def _scale_of_quantity(ref: object) -> int:
    if isinstance(ref, str) and ref in _METRICS_SCALES:
        return _METRICS_SCALES[ref]
    return _scale_of_coeff(_coeff_of_quantity(ref))


def materialize(
    model,
    choices: list[tuple[dict, Any]],
    cands: list[dict],
    ref: object,
    data,
    metrics: dict | None = None,
):
    """量引用 → cp_model 线性表达式（带量纲缩放）。

    build_model 已算好的指标（总经济成本/现金支出/最后完工时间等）直接从
    metrics dict 引用——它们是 max/复合形式，生成器无法从候选字段重算。
    """
    if isinstance(ref, str) and metrics is not None and ref in metrics:
        return metrics[ref]
    terms = generator.resolve_quantity_terms(cands, ref, data)
    coeff = _coeff_of_quantity(ref)
    scale = _scale_of_coeff(coeff)
    return _weighted(model, choices, terms, scale)


def _weighted(model, choices: list[tuple[dict, Any]], terms: list[tuple[float, int]], scale: int):
    if not terms:
        return model.NewConstant(0)
    variables = [choices[index][1] for _coef, index in terms]
    coefficients = [int(round(coef * scale)) for coef, _index in terms]
    return sum(c * v for c, v in zip(coefficients, variables))


def _task_subset(cands: list[dict], task_ref: str, predicate) -> list[int]:
    """某任务中满足谓词的候选下标。predicate(c) -> bool。"""
    return [
        index for index, c in enumerate(cands)
        if c["任务编号"] == task_ref and predicate(c)
    ]


def _add_subset_zero(model, choices, indexes: list[int]) -> None:
    if not indexes:
        return
    variables = [choices[i][1] for i in indexes]
    model.Add(sum(variables) == 0)


# ── 约束编译 ──────────────────────────────────────────────────────────────────

def compile_constraint(
    model,
    choices: list[tuple[dict, Any]],
    cands: list[dict],
    data,
    spec: dict[str, Any],
    metrics: dict | None = None,
    task_vars: dict[str, list[tuple[dict, Any]]] | None = None,
) -> list[str]:
    """单条约束 → 加进模型，返回错误列表（空 = 成功）。"""
    ctype = spec["type"]
    errors: list[str] = []
    if ctype == "must":
        if "task" in spec:
            targets = [resolve_task(data, spec["task"])]
        else:
            # 按维度批量："高严重故障全都要修"。
            #
            # 目标**从任务表枚举，不从候选枚举**——这是关键。候选是按截止日期
            # 生成的（product_planner.candidates 里 hi = min(窗口结束, 截止分钟)），
            # 一个任务若在截止前干不完，就一个候选都生不出来，于是它从"必须排进去"
            # 的名单里悄悄消失，求解还报 FEASIBLE，未安排列表里躺着四个高危任务。
            # 从任务表枚举，这种任务会得到空变量表 → Add(0 == 1) → 明确的 INFEASIBLE。
            filter_spec = spec.get("filter") or {}
            unsupported = set(filter_spec) - _MUST_FILTERS
            if unsupported:
                return [f"must 的 filter 只支持 {sorted(_MUST_FILTERS)}，收到 {sorted(unsupported)}"]
            targets = [t["任务编号"] for t in data.tasks if task_matches(t, filter_spec, data)]
            if not targets:
                return [f"must 的 filter {filter_spec!r} 没有匹配到任何任务"]
        for task in targets:
            variables = [choices[i][1] for _c, i in generator.per_task_terms(cands, task, "day", data)]
            model.Add(sum(variables) == 1)
    elif ctype == "exclude":
        task = resolve_task(data, spec["task"])
        terms = generator.per_task_terms(cands, task, "day", data)
        variables = [choices[i][1] for _c, i in terms]
        model.Add(sum(variables) == 0)
    elif ctype == "only":
        # 范围裁剪：「只修 1 号风机的叶片」。
        #
        # 做法是把**没命中 filter 的任务**的候选变量全部压 0——也就是"其余一律不排"，
        # 而不是给命中的任务加"必须排"。差别在于：只修 ≠ 都得修。
        # 用户说"只修 1 号风机的叶片"，是说别的别动；1 号风机这几片叶片能不能全修，
        # 由窗口/工时/目标决定，不该被这条约束强行钉成"全修"。
        #
        # 命中为空必须报错：否则模型会排出一个空方案，看起来像"按你说的做了"。
        filter_spec = spec.get("filter") or {}
        keep = {t["任务编号"] for t in data.tasks if task_matches(t, filter_spec, data)}
        if not keep:
            return [f"only 的条件 {filter_spec!r} 在任务表里没有匹配到任何任务"]
        for task in data.tasks:
            if task["任务编号"] in keep:
                continue
            terms = generator.per_task_terms(cands, task["任务编号"], "day", data)
            variables = [choices[i][1] for _c, i in terms]
            model.Add(sum(variables) == 0)
    elif ctype in ("at_most_one", "at_least_one"):
        # 分组选择：「这几个里最多/至少挑一个」。
        # 每个任务在模型里有一串候选变量，且至多一个为真（完整模式恰好一个），
        # 所以 sum(所有候选变量) 就等于"这批任务里被排了几个"，直接加界即可。
        #
        # 注意完整模式：product_planner 给每个任务加了 sum(x)==1，此时
        # at_most_one 的 <=1 与"每个都必须排"直接矛盾 —— 这是真冲突，必须让它
        # 老老实实报不可行，由 diagnose 说清"你要的是一选一，模型却在全排"。
        variables: list[Any] = []
        missing: list[str] = []
        for ref in spec["tasks"]:
            task = resolve_task(data, ref)
            pairs = task_vars.get(task) if task else None
            if not pairs:
                missing.append(str(ref))
                continue
            variables.extend(variable for _c, variable in pairs)
        if missing:
            # 不能静默跳过：空变量表加 <=1 等于什么都没约束，用户以为生效了。
            return [f"{ctype} 里的任务在当前快照里没有可选排程：{'、'.join(missing)}"]
        if ctype == "at_most_one":
            model.Add(sum(variables) <= 1)
        else:
            model.Add(sum(variables) >= 1)
    elif ctype == "capacity":
        # 日配额：「某天最多用 N 个 <资源>」。
        #
        # resource 是维度名（line/turbine/part），不是具体线号——具体到哪条线
        # 由 fix 负责。这里问的是"那天开了几条线 / 动了几台风机 / 用了几个部件"。
        #
        # 判定"那天有没有作业"用作业段与当天是否有交集，不看 开始/完成：
        # 一个任务可能跨天，开始在前一天、作业落在目标日，用开始日期会漏。
        resource = spec["resource"]
        day = resolve_day(data, spec["time"])
        if day is None:
            return [f"capacity 的时间 {spec['time']!r} 不在规划范围内"]
        limit = int(spec["limit"])

        def _resource_key(candidate: dict) -> Any:
            if resource == "line":
                return candidate.get("作业线")
            if resource == "turbine":
                return candidate.get("风机编号")
            return candidate.get("部件编号")

        def _works_on_day(candidate: dict) -> bool:
            start_of_day, end_of_day = day * 1440, (day + 1) * 1440
            return any(
                a < end_of_day and b > start_of_day
                for a, b in candidate.get("作业段") or []
            )

        groups: dict[Any, list[Any]] = {}
        for candidate, variable in choices:
            if not _works_on_day(candidate):
                continue
            groups.setdefault(_resource_key(candidate), []).append(variable)
        used: list[Any] = []
        for key, variables in groups.items():
            flag = model.NewBoolVar(f"cap_{resource}_{key}_{day}")
            model.AddMaxEquality(flag, variables)
            used.append(flag)
        if used:
            model.Add(sum(used) <= limit)
    elif ctype == "precedence":
        before = resolve_task(data, spec["before"])
        after = resolve_task(data, spec["after"])
        gap = int(spec.get("gap") or 0)
        finish_b = _weighted(model, choices, generator.per_task_terms(cands, before, "finish", data), 1)
        start_a = _weighted(model, choices, generator.per_task_terms(cands, after, "start", data), 1)
        model.Add(finish_b + gap <= start_a)
    elif ctype == "co_occur":
        tasks = [resolve_task(data, ref) for ref in spec["tasks"]]
        day_terms = {t: generator.per_task_terms(cands, t, "day", data) for t in tasks}
        day_expr = {t: _weighted(model, choices, terms, 1) for t, terms in day_terms.items()}
        pairs = [(tasks[i], tasks[j]) for i in range(len(tasks)) for j in range(i + 1, len(tasks))]
        days_total = data.end // 1440
        for a, b in pairs:
            if spec["mode"] == "same_day":
                model.Add(day_expr[a] == day_expr[b])
            elif spec["mode"] == "diff_day":
                flag = model.NewBoolVar(f"dsl_diff_day_{a}_{b}")
                model.Add(day_expr[a] - day_expr[b] <= -1 + (days_total + 1) * (1 - flag))
                model.Add(day_expr[b] - day_expr[a] <= -1 + (days_total + 1) * flag)
            else:
                errors.append(f"co_occur 模式 {spec['mode']!r} 原型未实现")
    elif ctype == "fix":
        task = resolve_task(data, spec["task"])
        line = int(spec["line"])
        bad = [i for i, c in enumerate(cands) if c["任务编号"] == task and c.get("作业线") != line]
        _add_subset_zero(model, choices, bad)
    elif ctype == "cardinality":
        terms = generator.quantity_terms(cands, "count", "one", spec["filter"], data)
        expr = _weighted(model, choices, terms, 1)
        _add_op(model, expr, spec["op"], int(spec["value"]))
    elif ctype == "deadline":
        task = resolve_task(data, spec["task"])
        minute = resolve_minute(data, spec["time"])
        bad = _task_subset(cands, task, lambda c: c["完成"] > minute)
        _add_subset_zero(model, choices, bad)
    elif ctype == "earliest":
        task = resolve_task(data, spec["task"])
        minute = resolve_minute(data, spec["time"])
        bad = _task_subset(cands, task, lambda c: c["开始"] < minute)
        _add_subset_zero(model, choices, bad)
    elif ctype == "forbidden_dates":
        minutes = [resolve_minute(data, date) for date in spec["dates"]]
        bad = [i for i, c in enumerate(cands) if c["开始"] // 1440 in {m // 1440 for m in minutes}]
        _add_subset_zero(model, choices, bad)
    elif ctype == "budget":
        scale = _scale_of_quantity(spec["quantity"])
        expr = materialize(model, choices, cands, spec["quantity"], data, metrics)
        _add_op(model, expr, spec["op"], int(round(float(spec["value"]) * scale)))
    elif ctype == "linear":
        scales = {_scale_of_quantity(ref) for _c, ref in spec["terms"]}
        if len(scales) > 1:
            errors.append("linear 原语不允许混合时间量与成本量（量纲不同）")
            return errors
        scale = next(iter(scales))
        total = 0
        for coef, ref in spec["terms"]:
            for s, i in generator.resolve_quantity_terms(cands, ref, data):
                total = total + int(round(float(coef) * s * scale)) * choices[i][1]
        _add_op(model, total, spec["op"], int(round(float(spec["value"]) * scale)))
    else:
        errors.append(f"原语 {ctype} 已登记但原型未实现编译")
    return errors


def _add_op(model, expr, op: str, bound: int) -> None:
    if op == "<=":
        model.Add(expr <= bound)
    elif op == ">=":
        model.Add(expr >= bound)
    elif op == "==":
        model.Add(expr == bound)
    else:  # pragma: no cover - 校验器已拦
        raise ValueError(f"非法运算符 {op}")


# ── 注入闭包 ──────────────────────────────────────────────────────────────────

def compile_parameters(specs: list[dict[str, Any]], base_data) -> dict[str, Any]:
    """DSL parameters 段 → load_project 的 overrides（绝对参数值）。

    相对倍率（op="*"）按基座参数快照换算成绝对值；注册表参数名翻译成
    模型内部键（船舶日租金 → 船租（元每艘每天））；字段合法性由校验器保证。
    """
    from .registry.parameters import to_internal_key

    overrides: dict[str, Any] = {}
    for spec in specs:
        field = to_internal_key(spec["field"])
        if spec["op"] == "*":
            if field not in base_data.params:
                raise ValueError(f"无法对未知参数执行相对调整：{field}")
            overrides[field] = float(base_data.params[field]) * float(spec["value"])
        else:
            overrides[field] = spec["value"]
    return overrides


def compile_data(specs: list[dict[str, Any]], base_data=None) -> tuple[list[dict], list[dict]]:
    """DSL data 段 → (data_patches, timeline_patches)。

    data_patches 沿用现有行级补丁格式（part_hours/part_arrival/task_deadline）；
    部件引用（名称/编号）翻译成 partId。timeline_patches 是第二期新增的
    时序假设（weather/hourly/window）。
    """
    data_patches: list[dict] = []
    timeline_patches: list[dict] = []
    for spec in specs:
        kind = spec["kind"]
        if kind in ("part_hours", "part_arrival"):
            translated = dict(spec)
            part_ref = translated.pop("part", None)
            if base_data is not None and part_ref:
                part_id = None
                for pid, row in base_data.parts.items():
                    if part_ref in (pid, row["部件名称"]):
                        part_id = pid
                        break
                if part_id is None:
                    raise ValueError(f"部件 {part_ref} 不存在于部件库")
                translated["partId"] = part_id
                translated["partName"] = base_data.parts[part_id]["部件名称"]
            data_patches.append(translated)
        elif kind == "task_deadline":
            data_patches.append(dict(spec))
        elif kind in ("weather", "hourly", "window"):
            timeline_patches.append(dict(spec))
        else:  # pragma: no cover - 校验器已拦
            raise ValueError(f"未知数据补丁类型 {kind}")
    return data_patches, timeline_patches


def constraints_builder(
    dsl_constraints: list[dict[str, Any]],
    data,
) -> Callable:
    """build_model 的 extra_constraints 闭包：(m, choices, task_vars, metrics) -> 错误列表。"""
    def _apply(m, choices, task_vars, metrics) -> list[str]:
        cands = generator.candidate_rows(choices)
        errors: list[str] = []
        for index, spec in enumerate(dsl_constraints):
            errors.extend(compile_constraint(
                m, choices, cands, data, spec, metrics, task_vars
            ))
            if errors:
                errors.append(f"（第 {index + 1} 条约束编译失败）")
                break
        return errors
    return _apply


def stages_builder(dsl_objectives: list[dict[str, Any]], data) -> Callable:
    """build_model 的 custom_stages 闭包：(m, choices, metrics) -> stages 列表。"""
    def _build(m, choices, metrics):
        cands = generator.candidate_rows(choices)
        stages: list[tuple[str, str, Any]] = []
        for index, spec in enumerate(dsl_objectives):
            otype = spec["type"]
            if otype in ("minimize", "maximize"):
                expr = materialize(m, choices, cands, spec["q"], data, metrics)
                stages.append((f"dsl_{index}_{otype}", "minimize" if otype == "minimize" else "maximize", expr))
            elif otype == "lexmin":
                for j, stage in enumerate(spec["stages"]):
                    expr = materialize(m, choices, cands, stage["q"], data, metrics)
                    stages.append((f"dsl_{index}_{j}_{stage['dir']}", stage["dir"], expr))
            else:
                raise ValueError(f"目标 {otype} 已登记但原型未实现编译")
        return stages
    return _build
