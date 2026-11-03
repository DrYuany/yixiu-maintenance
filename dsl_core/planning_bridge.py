"""旧解析语言 ↔ DSL 桥接（第三期：收拢解析管道）。

设计原则（见设计文档 §11.2-11.3）：
- 保留 planning_intent 的正则模式（它们是最成熟的规则层实现），不重写；
- 统一成 DSL schema：strategy/mode/weights → objectives；overrides → parameters；
  limits/必修 → constraints；data_patches → data；seconds → solver；
- PlanningRequest 不消失，它变成 DSL 编译后的产物。

两个方向：
  request_to_dsl(request)            旧编译器产物 → DSL（规则层输出统一化）
  dsl_to_request(dsl, base)          DSL → PlanningRequest（求解器输入）
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from .planning_request import PlanningRequest

from .registry.parameters import PARAM_NAME_TO_KEY

# 模型内部键 → 122 项注册表参数名称（PARAM_NAME_TO_KEY 的反向）
_KEY_TO_PARAM_NAME: dict[str, str] = {value: key for key, value in PARAM_NAME_TO_KEY.items()}


def to_registry_name(internal_key: str) -> str:
    return _KEY_TO_PARAM_NAME.get(internal_key, internal_key)


# 注册表里叫"参数"、实际落到 PlanningRequest.strategy 的字段。
# 与 product_planner.STRATEGIES 保持一致。
STRATEGY_NAMES: tuple[str, ...] = ('成本最低优先', '高风险优先抢修', '最短完成优先', '检修最多器件优先')
_STRATEGY_FIELDS: frozenset[str] = frozenset({'标准目标策略', '目标策略', '优化策略'})


# limits 键 → DSL 约束规格
def _limits_to_constraints(limits: dict[str, Any]) -> list[dict[str, Any]]:
    constraints: list[dict[str, Any]] = []
    if "最大总成本" in limits:
        constraints.append({"type": "budget", "quantity": "总经济成本", "op": "<=",
                            "value": float(limits["最大总成本"])})
    if "现金预算" in limits:
        constraints.append({"type": "budget", "quantity": "现金支出", "op": "<=",
                            "value": float(limits["现金预算"])})
    if "最低完成数" in limits:
        constraints.append({"type": "cardinality", "filter": {}, "op": ">=",
                            "value": int(limits["最低完成数"])})
    for tid in limits.get("必修任务", []) or []:
        constraints.append({"type": "must", "task": str(tid)})
    return constraints


def request_to_dsl(request: PlanningRequest) -> dict[str, Any]:
    """旧编译器产物 → 统一 DSL。"""
    dsl: dict[str, Any] = {}
    if request.mode == "加权" and request.weights:
        dsl["objectives"] = [{"type": "weighted", "weights": dict(request.weights)}]
    else:
        dsl["objectives"] = [{"type": "strategy", "name": request.strategy, "mode": request.mode}]

    parameters: list[dict[str, Any]] = []
    for key, value in sorted(request.overrides.items()):
        parameters.append({"field": to_registry_name(key), "op": "=", "value": value})
    for key, factor in sorted(request.relative_overrides.items()):
        parameters.append({"field": to_registry_name(key), "op": "*", "value": factor})
    if parameters:
        dsl["parameters"] = parameters

    constraints = _limits_to_constraints(request.limits or {})
    if constraints:
        dsl["constraints"] = constraints

    if request.data_patches:
        dsl["data"] = [dict(patch) for patch in request.data_patches]

    # 用户没点时限就不写：写一个具体数字等于替用户宣布一个他没说过的预算。
    if request.seconds is not None:
        dsl["solver"] = {"seconds": request.seconds}
    return dsl


def split_objectives(
    objectives: list[dict[str, Any]],
) -> tuple[str | None, str | None, dict[str, float] | None, list[dict[str, Any]]]:
    """把 objectives 拆成（策略, 模式, 权重, 真正的分层目标）。

    `strategy` 和 `weighted` 不是"分层目标"——它们是求解器模式，
    该落到 PlanningRequest 的 strategy / mode / weights 三个字段；
    剩下的 minimize / maximize / lexmin 才是 stages_builder 的活。

    **这个拆分只能有一份实现。** 此前 bridge 自己写了一版、runner 完全没做，
    于是同一份 DSL 在变更流程里能跑、在 runner（评测和执行侧）里直接
    抛「目标 weighted 已登记但原型未实现编译」。同一个输入两个答案，
    是最难查的一类 bug——你会以为是模型写错了。

    返回 None 表示这条 DSL 没提这件事，调用方应保留自己的默认值。
    """
    strategy: str | None = None
    mode: str | None = None
    weights: dict[str, float] | None = None
    rest: list[dict[str, Any]] = []
    for spec in objectives:
        otype = spec.get("type")
        if otype == "strategy":
            if spec.get("name"):
                strategy = str(spec["name"])
            if spec.get("mode"):
                mode = str(spec["mode"])
        elif otype == "weighted":
            mode = "加权"
            weights = {str(k): float(v) for k, v in (spec.get("weights") or {}).items()}
        else:
            rest.append(spec)
    return strategy, mode, weights, rest


def dsl_to_request(
    dsl: dict[str, Any],
    base: PlanningRequest | None = None,
) -> tuple[PlanningRequest, list[dict[str, Any]], list[dict[str, Any]]]:
    """DSL → (PlanningRequest, 剩余约束, 时序补丁)。

    能映射进 PlanningRequest 的（参数/策略/权重/预算/必修/数据补丁/时限）
    直接映射；映射不了的（co_occur/precedence 等自定义约束、lexmin 目标）
    作为"剩余约束"交给编译器的注入闭包。
    """
    request = replace(base) if base else PlanningRequest()
    residual: list[dict[str, Any]] = []
    timeline: list[dict[str, Any]] = []

    objectives = dsl.get("objectives") or []
    # 策略/权重是求解器模式，不是分层目标——拆分逻辑与 runner 共用这一份
    dsl_strategy, dsl_mode, dsl_weights, custom_objectives = split_objectives(objectives)
    if dsl_strategy or dsl_mode:
        request = replace(
            request,
            strategy=dsl_strategy or request.strategy,
            mode=dsl_mode or request.mode,
        )
    if dsl_weights is not None:
        request = replace(request, mode="加权", weights=dsl_weights)

    overrides = dict(request.overrides)
    relative = dict(request.relative_overrides)
    for spec in dsl.get("parameters") or []:
        field = to_registry_name(spec["field"])  # 接受注册表名或内部键
        field = PARAM_NAME_TO_KEY.get(field, field)
        if field in _STRATEGY_FIELDS:
            # 标准目标策略在 122 项注册表里是"选择层参数"，但它不是系统参数，
            # 落到 PlanningRequest 的 strategy 字段。不拦就会当成覆盖参数，
            # 到 load_project 才报"未支持参数：{'标准目标策略'}"。
            name = str(spec.get("value") or "").strip()
            if name in STRATEGY_NAMES:
                request = replace(request, strategy=name)
                continue
            residual.append({"type": "unsupported_parameter", "field": field, "value": spec.get("value"),
                             "reason": f"策略只能是：{'/'.join(STRATEGY_NAMES)}"})
            continue
        if spec.get("op") == "*":
            # 本轮新口径取代旧口径：相对倍率替换掉同参数的绝对覆盖
            overrides.pop(field, None)
            relative[field] = float(spec["value"])
        else:
            relative.pop(field, None)
            overrides[field] = spec["value"]
    request = replace(request, overrides=overrides, relative_overrides=relative)

    limits = dict(request.limits or {})
    data_patches = list(request.data_patches)
    for spec in dsl.get("constraints") or []:
        ctype = spec.get("type")
        if ctype == "budget" and spec.get("quantity") == "总经济成本" and spec.get("op") == "<=":
            limits["最大总成本"] = float(spec["value"])
        elif ctype == "budget" and spec.get("quantity") == "现金支出" and spec.get("op") == "<=":
            limits["现金预算"] = float(spec["value"])
        elif (ctype == "cardinality" and spec.get("op") == ">="
              and not spec.get("filter")):
            limits["最低完成数"] = int(spec["value"])
        elif ctype == "must" and spec.get("task"):
            limits["必修任务"] = sorted({*limits.get("必修任务", []), str(spec["task"])})
        else:
            # 注意 must 的 filter 形式（"严重故障必须修完"）：它没有 task 字段，
            # 不能走上面那条 set 合并，否则 str(spec["task"]) 直接 KeyError。
            # 交给编译器按 filter 展开，语义更完整。
            residual.append(spec)
    for spec in dsl.get("data") or []:
        if spec.get("kind") in ("part_hours", "part_arrival", "task_deadline"):
            data_patches.append(dict(spec))
        else:
            timeline.append(dict(spec))
    request = replace(request, limits=limits, data_patches=data_patches)

    solver = dsl.get("solver") or {}
    if "seconds" in solver:
        request = replace(request, seconds=int(solver["seconds"]))
    return request, residual, timeline


def custom_objectives(dsl: dict[str, Any]) -> list[dict[str, Any]]:
    """返回 DSL 里映射不进 PlanningRequest 的目标（lexmin/minimize 等）。"""
    return [
        spec for spec in (dsl.get("objectives") or [])
        if spec.get("type") not in ("strategy", "weighted")
    ]


def _canonical(spec: dict[str, Any]) -> str:
    import json

    return json.dumps(spec, ensure_ascii=False, sort_keys=True)


def merge_dsl(*dsls: dict[str, Any] | None) -> dict[str, Any]:
    """多轮 DSL 累加：后说的覆盖先说的，约束/数据按内容去重。

    - parameters：按 field 覆盖（同一参数后说优先）
    - constraints/data：按内容去重后追加
    - objectives：新的一轮有目标就整体替换（目标语义不适合拼接）
    - solver：合并，后者覆盖
    """
    merged: dict[str, Any] = {}
    parameters: dict[str, dict[str, Any]] = {}
    constraints: list[dict[str, Any]] = []
    data: list[dict[str, Any]] = []
    objectives: list[dict[str, Any]] = []
    solver: dict[str, Any] = {}
    for dsl in dsls:
        if not dsl:
            continue
        for spec in dsl.get("parameters") or []:
            parameters[str(spec.get("field"))] = spec
        for spec in dsl.get("constraints") or []:
            if _canonical(spec) not in {_canonical(item) for item in constraints}:
                constraints.append(spec)
        for spec in dsl.get("data") or []:
            if _canonical(spec) not in {_canonical(item) for item in data}:
                data.append(spec)
        if dsl.get("objectives"):
            objectives = list(dsl["objectives"])
        solver.update(dsl.get("solver") or {})
    if parameters:
        merged["parameters"] = list(parameters.values())
    if constraints:
        merged["constraints"] = constraints
    if data:
        merged["data"] = data
    if objectives:
        merged["objectives"] = objectives
    if solver:
        merged["solver"] = solver
    return merged
