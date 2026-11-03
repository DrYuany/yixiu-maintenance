"""端到端 runner：DSL JSON + 主项目求解内核 → 求解报告。

流程：load_project → candidates → build_model(注入 DSL 闭包) → solve → 报告。
已下发检修单的锁定/部分排程兜底在原型里未处理（正式合并时走 recommend 路径）。

用法：
    from dsl_core.runner import run
    report = run(dsl, base_id="基座01", seconds=30)
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_SCRIPTS = PROJECT_ROOT / "maintenance-milp-modeler" / "scripts"


class DslError(RuntimeError):
    """校验或编译失败，携带全部错误信息。

    ``diagnosis``：求解没产出方案时的结构化原因（见 ``diagnose.py``）。
    没有它的时候是 None——空着比编一句"可能冲突"好。
    """

    def __init__(self, errors: list[str], diagnosis: dict | None = None) -> None:
        super().__init__("；".join(errors))
        self.errors = errors
        self.diagnosis = diagnosis

    @property
    def gate_class(self) -> str:
        """这次拦截落在哪道门（复盘/测评计数用）。

        只读、不参与判定；归因口径见 ``gate_metrics.py``。延迟导入避免
        runner 加载时就把统计模块拖进来。
        """
        from .gate_metrics import from_dsl_error

        return from_dsl_error(self)


def _solver_modules():
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    from product_data import load_project  # noqa: F401
    from product_planner import candidates, heuristic, solve  # noqa: F401
    return load_project, candidates, heuristic, solve


def run(
    dsl: dict[str, Any],
    *,
    base_id: str = "基座01",
    as_of: str | None = None,
    seconds: int | None = None,
    output: Path | None = None,
    strategy: str = "成本最低优先",
    mode: str = "分层",
    weights: dict[str, float] | None = None,
    max_tasks: int | None = None,
    partial: bool = False,
) -> dict[str, Any]:
    """校验 DSL → 注入求解 → 返回 {报告, dsl, 求解信息}。

    max_tasks：原型测试用的小规模任务子集（完整 25 任务模型单线程 CP-SAT 在
    短时限内证明不了可行/不可行，生产默认 300s；golden 用例用 8~10 个任务
    验证约束语义，规模性能是主项目已有能力，不属于本原型验证范围）。
    partial：部分排程模式（允许丢任务），exclude/must/cardinality 等
    "选择类"原语只在部分模式下有意义（完整模式所有任务强制排入）。
    """
    from .compiler import (
        compile_data,
        compile_parameters,
        constraints_builder,
        needs_partial,
        stages_builder,
    )
    from .validator import validate_dsl

    # exclude / at_most_one / cardinality 上限都是"允许少修"的明确信号：
    # 完整模式把每个任务强制排入（product_planner 里 sum(x)==1），跟 exclude 的
    # sum(x)==0、"这批里最多排一个"、"最多完成 8 个"直接矛盾，必然判不可行。
    # at_least_one 不在此列（完整模式下它恒真，切 partial 会把"全修"降级成"挑一个"）。
    # 判定逻辑收在 compiler.needs_partial，执行器和原话对账共用同一份，别各写一套。
    if not partial and needs_partial(dsl):
        partial = True

    load_project, candidates, heuristic, solve = _solver_modules()

    # 基座先读一次：相对参数倍率（op="*"）要按基座值换算
    base_data = load_project(PROJECT_ROOT, base_id=base_id, as_of=as_of)
    overrides = None
    if dsl.get("parameters"):
        overrides = compile_parameters(dsl["parameters"], base_data)
    data_patches = None
    timeline_patches = None
    if dsl.get("data"):
        data_patches, timeline_patches = compile_data(dsl["data"], base_data)

    try:
        data = load_project(
            PROJECT_ROOT,
            base_id=base_id,
            as_of=as_of,
            overrides=overrides,
            data_patches=data_patches,
            timeline_patches=timeline_patches,
        )
    except ValueError as exc:
        # 补丁落地时的拒绝理由本来就是说给用户听的（"这次改不会影响结果"、
        # "期限没让任何任务提前"），但它抛的是裸 ValueError，走出去会被当成
        # 系统内部错误。转成 DslError，让调用方按"需求表达有误"处理。
        raise DslError([str(exc)]) from exc
    if max_tasks:
        data.tasks = data.tasks[:max_tasks]

    errors = validate_dsl(dsl, data)
    if errors:
        raise DslError(errors)

    options = {t["任务编号"]: candidates(data, t) for t in data.tasks}
    # 期限补丁会让候选按截止时间截断（product_data.candidates 里 hi=min(窗口, 截止)）。
    # 一个任务若干不完，候选数直接变成 0 —— 它于是从"必须排进去"的名单里消失，
    # 求解照常返回 FEASIBLE，那几条躺在"未安排"里。用户说的是"必须修完"。
    # 这里提前认出来：基座里排得进、加完期限却排不进，就是期限挤掉的，不是本来就没窗口。
    if data_patches and not partial:
        starved = []
        for task in data.tasks:
            tid = task["任务编号"]
            if options.get(tid) or task.get("截止分钟") is None:
                continue
            base_task = next(
                (t for t in base_data.tasks if t["任务编号"] == tid), None
            )
            if base_task is not None and candidates(base_data, base_task):
                starved.append(tid)
        if starved:
            raise DslError([
                "这些任务在期限之前排不进来，改期限等于把它们挪出计划："
                + "、".join(starved)
            ])
    # 与生产 recommend() 一致：启发式基座当热启动提示（不牺牲最优性证明）。
    baselines = {name: heuristic(data, options, name) for name in ("严重性优先", "低风速优先")}
    hint = max(baselines.values(), key=len)
    output = output or (PROJECT_ROOT / "product-output" / "dsl-run")
    output.mkdir(parents=True, exist_ok=True)

    constraints = dsl.get("constraints") or []
    objectives = dsl.get("objectives") or []

    # 策略 / 权重是**求解器模式**，不是分层目标。先挑出来，否则
    # stages_builder 会拿 strategy / weighted 去建分层阶段，直接抛
    # 「已登记但原型未实现编译」——而同一份 DSL 走变更流程
    # （planning_bridge.dsl_to_request）明明是能跑的。同一个输入两个答案，
    # 是最难查的一类缺陷：你会以为是模型写错了。
    # 拆分逻辑只有一份，在 bridge 里；这里只是共用它。
    from .planning_bridge import split_objectives

    dsl_strategy, dsl_mode, dsl_weights, objectives = split_objectives(objectives)
    strategy = dsl_strategy or strategy
    mode = dsl_mode or mode
    if dsl_weights is not None:
        weights = dsl_weights

    extra = constraints_builder(constraints, data) if constraints else None
    stages = stages_builder(objectives, data) if objectives else None

    selected, info = solve(
        data,
        options,
        output,
        seconds=seconds,
        strategy=strategy,
        mode=mode,
        weights=weights,
        hint=hint,
        partial=partial,
        extra_constraints=extra,
        custom_stages=stages,
    )
    # 没解出来就是没解出来，不能把未考虑约束的基座方案当结果交出去。
    #
    # 以前 solve 用 selected=fallback 起手，约束把模型逼到不可行时会把旧方案
    # 原样返回、解状态还写 FEASIBLE，只能靠来源字段事后识别。现在 solve 只在
    # 真解出来时返回 selected，这里直接按"有没有解"判定，不再看来源。
    if selected is None and constraints:
        # 报"没找到解"等于什么都没说。这里把能算出来的原因一并给出：
        # 直接矛盾、时间窗为空、任务无候选、配额上限不够——每一条都带证据，
        # 算不出来就明说"未能定位"，不拿"可能冲突"糊过去。
        from .diagnose import describe as describe_diagnosis
        from .diagnose import diagnose

        diagnosis = diagnose(
            dsl=dsl, data=data, options=options, info=info, partial=partial
        )
        raise DslError([describe_diagnosis(diagnosis)], diagnosis=diagnosis)

    # 意图对账：DSL 说了什么，产物里就必须有什么。
    #
    # 独立复核（verify_schedule）只证明这份排程"自己站得住"——窗口、工时、
    # 转场、库存都合法。它不知道用户说过什么，所以"只修前 8 个"那种事它永远
    # 查不出来。这里拿 DSL 原文逐条对产物，对不上就报错，不再产出一份
    # "复核通过、但不是你要的方案"。
    from .reconcile import describe, reconcile

    recommended = None
    if selected is not None:
        from product_planner import account  # noqa: PLC0415

        try:
            recommended = account(data, selected)
        except ValueError as exc:
            # account 内部会跑 verify_schedule：窗口、工时、转场、库存。
            # 它失败说明求解器给的这份排程不合法，不能带着"复核通过"交出去。
            raise DslError([f"求解结果未通过独立复核：{exc}"]) from exc
    audit = reconcile(
        dsl,
        {"求解": info, "排程": selected, "推荐": recommended, "数据": data.meta},
        data=data,
        overrides=overrides,
        context={"策略": strategy, "目标模式": mode, "自定义权重": weights},
    )
    info["意图对账"] = audit
    # 违例是"对不上"，未核对是"没法证明对得上"。两者都不能出方案：
    # 前者是反证，后者是空白证据——用户拿到的仍然是一份没被验证过诉求的方案。
    # 收紧的直接后果是：任何一条诉求没有对账实现，都会在这里被拦下来，
    # 而不是悄悄放过去、让用户以为约束生效了。
    if audit["违例"] or audit["未核对"]:
        raise DslError(["意图对账未通过：" + describe(audit)])
    return {
        "数据": data.meta,
        "求解": info,
        "排程": selected,
        "dsl": dsl,
    }


def assert_constraint_held(selected: list[dict] | None, checker, label: str) -> None:
    """golden 判分辅助：对求解结果做业务断言，违反抛 AssertionError。"""
    assert selected is not None, f"{label}: 求解未产生排程"
    assert checker(selected), f"{label}: 排程未满足约束"
