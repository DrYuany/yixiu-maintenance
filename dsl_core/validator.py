"""确定性校验器：DSL JSON → 错误列表。

"受控"的命门。只做确定性检查，不调用 LLM：
  1. 结构合法（constraints/objectives 列表）
  2. 原语在注册表、required 字段齐全
  3. 实体引用可解析（传入 data 时）
  4. 可引用量合法（形式/系数/过滤都在注册表；max/derived 原型明确报错）
  5. 线性（系数必须有限常数）、op 合法、界合法
"""
from __future__ import annotations

import math
from typing import Any

from .registry import entities as entity_reg
from .registry import primitives as prim_reg
from .registry import quantities as qty_reg

# data 段各补丁类型的必需字段（第二期）
# part_hours 只强制 part：factor / hours 二选一，在 _validate_data_patch 里判。
_DATA_PATCH_REQUIRED: dict[str, tuple[str, ...]] = {
    "part_hours": ("part",),
    "part_arrival": ("part", "at"),
    "task_deadline": ("at",),
    "weather": ("day",),
    "hourly": ("time",),
    "window": ("window",),
}


def validate_dsl(dsl: dict[str, Any], data=None) -> list[str]:
    errors: list[str] = []
    if not isinstance(dsl, dict):
        return ["DSL 必须是 JSON 对象"]
    for key, item in dsl.items():
        if key not in ("constraints", "objectives", "parameters", "data", "solver"):
            errors.append(f"未知字段: {key}")
    solver = dsl.get("solver")
    if solver is not None:
        if not isinstance(solver, dict):
            errors.append("solver 必须是 JSON 对象")
        else:
            if set(solver) - {"seconds"}:
                errors.append(f"solver 不允许字段 {set(solver) - {'seconds'}}")
            if "seconds" in solver:
                errors.extend(_check_bound(solver["seconds"], "solver", "seconds",
                                           minimum=30, integer=True))
                if solver["seconds"] > 1800:
                    errors.append("solver.seconds 最大 1800")
    for index, spec in enumerate(dsl.get("constraints") or []):
        errors.extend(_validate_constraint(spec, data, f"constraints[{index}]"))
    for index, spec in enumerate(dsl.get("objectives") or []):
        errors.extend(_validate_objective(spec, data, f"objectives[{index}]"))
    for index, spec in enumerate(dsl.get("parameters") or []):
        errors.extend(_validate_parameter(spec, f"parameters[{index}]"))
    for index, spec in enumerate(dsl.get("data") or []):
        errors.extend(_validate_data_patch(spec, data, f"data[{index}]"))
    return errors


def _validate_parameter(spec: Any, where: str) -> list[str]:
    """parameters 段：全局参数覆盖，字段必须在 122 项参数注册表且可改。"""
    from .registry import parameters as param_reg

    errors: list[str] = []
    if not isinstance(spec, dict):
        return [f"{where}: 参数必须是 JSON 对象"]
    field = spec.get("field")
    if not isinstance(field, str) or param_reg.lookup(field) is None:
        errors.append(f"{where}: 参数 {field!r} 不在 122 项参数注册表")
        return errors
    if not param_reg.is_changeable(field):
        if param_reg.is_unsupported(field):
            errors.append(
                f"{where}: 参数 {field!r} 已登记但尚未接入，当前改不动"
                f"（{param_reg.unsupported_reason(field)}）"
            )
        else:
            errors.append(f"{where}: 参数 {field!r} 登记为固定值，不可改")
        return errors
    route = param_reg.route_of(field)
    if route != "parameters":
        # 一个参数只有一个正确位置。写错位置就是确定性错误——过去这里放行，
        # 结果一路走到 load_project 才报"未支持参数"，把错误推给了执行期。
        errors.append(
            f"{where}: {field!r} 不能写进 parameters；"
            f"{param_reg.ROUTE_LABELS.get(route, '请写到别的段')}"
        )
        return errors
    op = spec.get("op")
    if op not in ("=", "*"):
        errors.append(f"{where}: op 必须是 '='（绝对）或 '*'（相对倍率）")
    if "value" not in spec:
        errors.append(f"{where}: 缺少 value")
    else:
        value = spec["value"]
        if isinstance(value, list):
            # 列表型参数（候选风机/候选部件/强制维修任务/禁止新开工风机…）
            if not value:
                errors.append(f"{where}: value 不能为空列表")
            elif not all(isinstance(item, (str, int, float)) for item in value):
                errors.append(f"{where}: 列表 value 的元素必须是字符串或数字")
        elif isinstance(value, bool) or isinstance(value, str):
            # 开关类（是/否）与时刻类（HH:MM）参数：取值由 load_project 校验
            if isinstance(value, str) and not value.strip():
                errors.append(f"{where}: value 不能为空")
        else:
            errors.extend(_check_bound(value, where, "value", minimum=0))
    return errors


def _validate_data_patch(spec: Any, data, where: str) -> list[str]:
    """data 段：行级数据补丁 + 时序假设修改（第二期）。"""
    errors: list[str] = []
    if not isinstance(spec, dict):
        return [f"{where}: 数据补丁必须是 JSON 对象"]
    kind = spec.get("kind")
    if kind not in _DATA_PATCH_REQUIRED:
        return [f"{where}: 未知数据补丁类型 {kind!r}，允许 {tuple(_DATA_PATCH_REQUIRED)}"]
    for field_name in _DATA_PATCH_REQUIRED[kind]:
        if field_name not in spec:
            errors.append(f"{where}: 补丁 {kind} 缺少必需字段 {field_name}")
    if kind == "part_hours":
        # 两种写法，二选一：
        #   factor：整件工时乘倍率（"齿轮箱维修工时上涨 20%"）
        #   hours ：把指定范围的工时定成绝对值（"叶片高风险修 15 小时"）
        # 同时给会让人分不清"到底是乘还是定"，直接拒。
        has_factor, has_hours = "factor" in spec, "hours" in spec
        if has_factor and has_hours:
            errors.append(f"{where}: part_hours 的 factor 与 hours 只能给一个")
        if not has_factor and not has_hours:
            errors.append(f"{where}: part_hours 需要 factor（倍率）或 hours（绝对小时）之一")
        if has_factor:
            errors.extend(_check_bound(spec["factor"], where, "factor", minimum=0))
        if has_hours:
            errors.extend(_check_bound(spec["hours"], where, "hours", minimum=0))
        if "severity" in spec and spec["severity"] not in ("高", "中", "低"):
            errors.append(f"{where}: part_hours 的 severity 只能是 高/中/低，收到 {spec['severity']!r}")
        if "sea" in spec and spec["sea"] not in ("常态", "恶劣但安全"):
            errors.append(
                f"{where}: part_hours 的 sea 只能是 常态/恶劣但安全，收到 {spec['sea']!r}"
            )
    if kind == "part_arrival":
        if "quantity" in spec:
            errors.extend(_check_bound(spec["quantity"], where, "quantity", integer=True, minimum=1))
        if "at" in spec and data is not None:
            from .registry.entities import resolve_minute

            if resolve_minute(data, spec["at"]) is None:
                errors.append(f"{where}: 到货时间 {spec['at']!r} 不在规划范围内")
    if kind == "task_deadline":
        if "at" in spec and data is not None:
            from .registry.entities import resolve_minute

            if resolve_minute(data, spec["at"]) is None:
                errors.append(f"{where}: 期限 {spec['at']!r} 不在规划范围内")
    if kind == "weather":
        if not any(key in spec for key in ("wind", "wave", "hours", "state")):
            errors.append(f"{where}: weather 补丁至少要给 wind/wave/hours/state 之一")
        if "state" in spec and spec["state"] not in ("常态", "恶劣但安全", "不可作业"):
            errors.append(f"{where}: 海况类别 {spec['state']!r} 不合法")
    if kind == "hourly":
        if not any(key in spec for key in ("price", "wind", "power")):
            errors.append(f"{where}: hourly 补丁至少要给 price/wind/power 之一")
    if kind == "window":
        if not any(key in spec for key in ("start", "end")):
            errors.append(f"{where}: window 补丁至少要给 start/end 之一")
    return errors


def _validate_constraint(spec: Any, data, where: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(spec, dict):
        return [f"{where}: 约束必须是 JSON 对象"]
    ctype = spec.get("type")
    if ctype not in prim_reg.CONSTRAINT_PRIMITIVES:
        return [f"{where}: 未知约束原语 {ctype!r}，注册表共 {len(prim_reg.CONSTRAINT_PRIMITIVES)} 种"]
    schema = prim_reg.CONSTRAINT_PRIMITIVES[ctype]
    for field in schema["required"]:
        if field not in spec:
            errors.append(f"{where}: 原语 {ctype} 缺少必需字段 {field}")
    allowed = set(schema["required"]) | set(schema.get("optional", [])) | {"type"}
    for key in spec:
        if key not in allowed:
            errors.append(f"{where}: 原语 {ctype} 不允许字段 {key!r}")
    if ctype not in prim_reg.COMPILED_CONSTRAINTS:
        reason = prim_reg.UNIMPLEMENTED_REASONS.get(ctype)
        suffix = f"（{reason}）" if reason else ""
        errors.append(f"{where}: 原语 {ctype} 已登记但原型未实现编译{suffix}")

    if "op" in spec:
        if spec["op"] not in prim_reg.OPS:
            errors.append(f"{where}: 非法运算符 {spec['op']!r}，允许 {prim_reg.OPS}")
    if "value" in spec:
        errors.extend(_check_bound(spec["value"], where, "value"))
    if "gap" in spec and spec["gap"] is not None:
        errors.extend(_check_bound(spec["gap"], where, "gap", minimum=0))
    if "line" in spec:
        errors.extend(_check_bound(spec["line"], where, "line", integer=True, minimum=1))
    if "limit" in spec:
        errors.extend(_check_bound(spec["limit"], where, "limit", integer=True, minimum=0))

    if ctype == "co_occur":
        mode = spec.get("mode")
        if mode not in prim_reg.CO_OCCUR_MODES:
            errors.append(f"{where}: 非法 co_occur 模式 {mode!r}，允许 {prim_reg.CO_OCCUR_MODES}")
        elif mode not in prim_reg.COMPILED_CO_OCCUR_MODES:
            errors.append(f"{where}: co_occur 模式 {mode!r} 已登记但原型未实现")
        errors.extend(_check_task_list(spec.get("tasks"), data, where))
    if ctype == "must":
        # 二选一：点名任务，或按维度批量。两个都给会让人分不清"到底必须哪些"，
        # 两个都不给等于"全部必须"——那更像 card.>=N 或别的原语，不该从这里混进来。
        has_task, has_filter = "task" in spec, "filter" in spec
        if has_task and has_filter:
            errors.append(f"{where}: must 的 task 与 filter 只能给一个")
        if not has_task and not has_filter:
            errors.append(f"{where}: must 需要 task 或 filter 之一")
        if has_task:
            errors.extend(_check_task_ref(spec.get("task"), data, where))
        if has_filter:
            errors.extend(_check_filter(spec.get("filter"), where))
            # must 是"哪些任务必须做"，只能按任务行自己的属性筛；
            # day/window/line 是候选的属性，一个任务可以有多个候选，筛它没有意义。
            unsupported = set(spec.get("filter") or {}) - set(prim_reg.TASK_FILTER_KEYS)
            if unsupported:
                errors.append(
                    f"{where}: must 的 filter 只支持 {'/'.join(prim_reg.TASK_FILTER_KEYS)}，"
                    f"不支持 {sorted(unsupported)}"
                )
    if ctype in ("exclude", "deadline", "earliest", "fix"):
        errors.extend(_check_task_ref(spec.get("task"), data, where))
    if ctype == "precedence":
        errors.extend(_check_task_ref(spec.get("before"), data, where))
        errors.extend(_check_task_ref(spec.get("after"), data, where))
    if ctype in ("at_most_one", "at_least_one"):
        errors.extend(_check_task_list(spec.get("tasks"), data, where))
    if ctype == "capacity":
        # resource 是维度名，不是具体线号：写"2号船"会被当未知维度拒掉，
        # 那种诉求属于 fix(task, line) 或 exclude，不该从 capacity 混进来。
        if spec.get("resource") not in prim_reg.CAPACITY_RESOURCES:
            errors.append(
                f"{where}: capacity 的 resource 只支持 {prim_reg.CAPACITY_RESOURCES}，"
                f"收到 {spec.get('resource')!r}（要指定具体某条线请用 fix）"
            )
        if data is not None:
            if entity_reg.resolve_day(data, spec.get("time")) is None:
                errors.append(
                    f"{where}: capacity 的时间 {spec.get('time')!r} 不在规划范围内"
                )
    if ctype == "forbidden_dates":
        if not isinstance(spec.get("dates"), list) or not spec["dates"]:
            errors.append(f"{where}: forbidden_dates 需要非空日期列表")
        elif data is not None:
            for date in spec["dates"]:
                if entity_reg.resolve_minute(data, date) is None:
                    errors.append(f"{where}: 日期 {date!r} 不在规划范围内")
    if ctype == "cardinality":
        errors.extend(_check_filter(spec.get("filter"), where))
    if ctype == "only":
        # 范围裁剪：filter 必须非空（空 filter 命中全部任务，等于没约束），
        # 键只能是任务行自己的属性。
        filter_spec = spec.get("filter")
        errors.extend(_check_filter(filter_spec, where))
        if isinstance(filter_spec, dict):
            if not filter_spec:
                errors.append(f"{where}: only 的 filter 不能为空（空条件等于不裁剪范围）")
            unsupported = set(filter_spec) - set(prim_reg.TASK_FILTER_KEYS)
            if unsupported:
                errors.append(
                    f"{where}: only 的 filter 只支持 {'/'.join(prim_reg.TASK_FILTER_KEYS)}，"
                    f"不支持 {sorted(unsupported)}"
                )
    if ctype == "budget":
        errors.extend(_check_quantity(spec.get("quantity"), where))
    if ctype == "linear":
        errors.extend(_check_linear_terms(spec.get("terms"), where))
    return errors


def _validate_objective(spec: Any, data, where: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(spec, dict):
        return [f"{where}: 目标必须是 JSON 对象"]
    otype = spec.get("type")
    if otype not in prim_reg.OBJECTIVE_PRIMITIVES:
        return [f"{where}: 未知目标原语 {otype!r}"]
    schema = prim_reg.OBJECTIVE_PRIMITIVES[otype]
    for field in schema["required"]:
        if field not in spec:
            errors.append(f"{where}: 目标 {otype} 缺少必需字段 {field}")
    if otype not in prim_reg.COMPILED_OBJECTIVES:
        errors.append(f"{where}: 目标 {otype} 已登记但原型未实现")
    if otype == "strategy":
        from . import objective_catalog

        name = spec.get("name")
        if not name or name not in objective_catalog.strategies():
            errors.append(f"{where}: 未知优化策略 {name!r}，允许 {objective_catalog.strategies()}")
        mode = spec.get("mode")
        if mode is not None and mode not in ("分层", "加权"):
            errors.append(f"{where}: 目标模式只能是 分层/加权，收到 {mode!r}")
    if otype in ("minimize", "maximize"):
        errors.extend(_check_quantity(spec.get("q"), where))
    if otype == "lexmin":
        stages = spec.get("stages")
        if not isinstance(stages, list) or not stages:
            errors.append(f"{where}: lexmin 的 stages 需要非空列表")
        else:
            for i, stage in enumerate(stages):
                if not isinstance(stage, dict) or "q" not in stage:
                    errors.append(f"{where}.stages[{i}]: 缺少 q")
                    continue
                errors.extend(_check_quantity(stage["q"], f"{where}.stages[{i}]"))
                if stage.get("dir") not in ("minimize", "maximize"):
                    errors.append(f"{where}.stages[{i}]: dir 必须是 minimize/maximize")
    if otype == "weighted":
        weights = spec.get("weights")
        if not isinstance(weights, dict) or not weights:
            errors.append(f"{where}: weighted 需要非空权重字典")
        else:
            for name, w in weights.items():
                errors.extend(_check_quantity(name, where))
                errors.extend(_check_bound(w, where, f"权重 {name}", minimum=0))
    return errors


# ── 子检查 ───────────────────────────────────────────────────────────────────

def _check_bound(value: Any, where: str, label: str, minimum=None, integer=False) -> list[str]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return [f"{where}: {label} 必须是有限数值，收到 {value!r}"]
    if minimum is not None and value < minimum:
        return [f"{where}: {label} 必须 ≥ {minimum}，收到 {value!r}"]
    if integer and float(value) != int(value):
        return [f"{where}: {label} 必须是整数，收到 {value!r}"]
    return []


def _check_task_ref(ref: Any, data, where: str) -> list[str]:
    if not isinstance(ref, str) or not ref.strip():
        return [f"{where}: 任务引用必须是任务编号字符串"]
    if data is not None and entity_reg.resolve_task(data, ref) is None:
        return [f"{where}: 任务 {ref!r} 不在当前告警快照"]
    return []


def _check_task_list(refs: Any, data, where: str) -> list[str]:
    if not isinstance(refs, list) or len(refs) < 2:
        return [f"{where}: tasks 需要至少两个任务"]
    errors: list[str] = []
    for ref in refs:
        errors.extend(_check_task_ref(ref, data, where))
    return errors


def _check_filter(filter_spec: Any, where: str) -> list[str]:
    if not isinstance(filter_spec, dict):
        return [f"{where}: filter 必须是字典"]
    errors: list[str] = []
    for dim in filter_spec:
        if dim not in qty_reg.FILTERS:
            errors.append(f"{where}: 未知过滤维度 {dim!r}，允许 {qty_reg.FILTERS}")
    return errors


def _check_quantity(ref: Any, where: str) -> list[str]:
    """检查量引用：具名量 / 原始三元组 / 未注册名。"""
    resolved = qty_reg.resolve_quantity(ref)
    if resolved is None:
        return [f"{where}: 量 {ref!r} 未注册（具名量或三元组）"]
    if resolved == "UNIMPLEMENTED":
        return [f"{where}: 量 {ref!r} 依赖 max/derived 形式，原型未实现"]
    if isinstance(resolved, dict):
        errors: list[str] = []
        form = resolved.get("form")
        if form not in qty_reg.FORMS:
            errors.append(f"{where}: 未知数学形式 {form!r}，允许 {qty_reg.FORMS}")
        elif form not in qty_reg.IMPLEMENTED_FORMS:
            errors.append(f"{where}: 形式 {form!r} 已登记但原型未实现")
        coeff = resolved.get("coeff", "one")
        if coeff != "one" and coeff not in qty_reg.COEFFS:
            errors.append(f"{where}: 未知系数 {coeff!r}，允许 {qty_reg.COEFFS}")
        errors.extend(_check_filter(resolved.get("filter", {}), where))
        return errors
    if isinstance(resolved, tuple):
        # 具名量展开成 (form, coeff, filter)：同样要过形式与过滤维度这两道。
        # 不查的话，将来往 NAMED_QUANTITIES 里加一个 max 形式的糖，
        # 校验器会一路放行，到生成器才报"原型未实现"。
        form, coeff, flt = (list(resolved) + ["one", {}])[:3]
        errors = []
        if form not in qty_reg.FORMS:
            errors.append(f"{where}: 未知数学形式 {form!r}，允许 {qty_reg.FORMS}")
        elif form not in qty_reg.IMPLEMENTED_FORMS:
            errors.append(f"{where}: 形式 {form!r} 已登记但原型未实现")
        if coeff != "one" and coeff not in qty_reg.COEFFS:
            errors.append(f"{where}: 未知系数 {coeff!r}，允许 {qty_reg.COEFFS}")
        errors.extend(_check_filter(flt, where))
        return errors
    return []


def _quantity_form(ref: Any) -> str | None:
    """量引用解析出来的数学形式；具名量回元组、三元组回 dict，这里统一成字符串。"""
    resolved = qty_reg.resolve_quantity(ref)
    if isinstance(resolved, tuple) and resolved:
        return str(resolved[0])
    if isinstance(resolved, dict):
        form = resolved.get("form")
        return str(form) if form is not None else None
    return None


def _check_linear_terms(terms: Any, where: str) -> list[str]:
    """linear 原语的 terms：[(系数, 量引用), ...]，系数必须有限常数。"""
    if not isinstance(terms, list) or not terms:
        return [f"{where}: linear 的 terms 需要非空列表"]
    errors: list[str] = []
    for i, term in enumerate(terms):
        if not isinstance(term, (list, tuple)) or len(term) != 2:
            errors.append(f"{where}.terms[{i}]: 每项必须是 [系数, 量]")
            continue
        coef, ref = term
        if isinstance(coef, bool) or not isinstance(coef, (int, float)) or not math.isfinite(float(coef)):
            errors.append(f"{where}.terms[{i}]: 系数必须有限常数，收到 {coef!r}")
        errors.extend(_check_quantity(ref, f"{where}.terms[{i}]"))
        # metrics 形式的量（最后完工时间、总经济成本、现金支出）由求解内核的
        # metrics dict 直接提供，只有**目标**物化得出来；生成器算不了它。
        # 放行会让"完工时间不能晚于11月20日"这种句子：validator 说能跑、
        # 验收判过，到执行侧才炸「原型不支持的形式 'metrics'」——
        # 三处判据三套答案，最后是用户在界面上看到失败。
        if _quantity_form(ref) == "metrics":
            errors.append(
                f"{where}.terms[{i}]: {ref!r} 由求解内核直接提供，只能在目标里引用；"
                "linear 约束编译不了它（想表达整体完工期限，目前没有对应原语）"
            )
    return errors
