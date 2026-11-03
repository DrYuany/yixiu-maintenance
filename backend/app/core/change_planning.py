"""变更计划：自然语言 → DSL 校验 → PlanningRequest 映射（运行时与工具共用）。

从 agent_runtime 抽出，原因是 agent 的 solve 工具也需要同一条链路：
模型决定「要改排程」→ 调 solve → 内部先走 DSL，表达不了再回退旧规划器。

只做「解析 + 映射 + 预览」，不碰求解、不碰版本、不碰待确认状态机。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

# co_occur 的六种模式：以前预览一律写“互斥（不同天/同时）”，六种模式长得一模一样，
# 连没实现的 diff_window 也显示成已实现的样子。模式本身是语义的一部分，必须说出来。
CO_OCCUR_LABELS: dict[str, str] = {
    "same_day": "安排在同一天",
    "diff_day": "安排在不同天",
    "same_window": "安排在同一窗口",
    "diff_window": "安排在不同窗口",
    "same_line": "安排在同一条作业线",
    "diff_line": "安排在不同作业线",
}

CAPACITY_LABELS: dict[str, str] = {
    "line": "作业线",
    "turbine": "风机",
    "part": "部件",
}


def _fmt_filter(filter_spec: dict[str, Any] | None) -> str:
    if not filter_spec:
        return "全部任务"
    return "、".join(f"{key}={value}" for key, value in filter_spec.items())


def _fmt_tasks(tasks: Any) -> str:
    if isinstance(tasks, (list, tuple)):
        return "、".join(str(item) for item in tasks)
    return str(tasks)


def _render_constraint(spec: dict[str, Any]) -> str:
    """把一条约束渲染成用户能核对的句子。

    原则：**DSL 里每一个生效字段都要在句子里出现**。预览是用户批准的唯一依据
    （原样输出、模型不得改写），少写一个字段，用户就批准了一个自己没看见的约束。
    真出过的偏差：cardinality 漏掉 filter，预览写“完成数 >= 3”，实际是“严重任务
    完成数 >= 3”；precedence 漏掉 gap，用户不知道还卡了间隔；co_occur 漏掉 mode，
    六种模式在预览里完全同形。
    """
    ctype = spec.get("type")
    op = spec.get("op", "=")

    if ctype == "must":
        if spec.get("task"):
            text = f"必修：{spec['task']}"
        else:
            text = f"必修（{_fmt_filter(spec.get('filter'))}）"
    elif ctype == "exclude":
        text = f"排除：{spec.get('task')}"
    elif ctype == "only":
        text = f"只修（{_fmt_filter(spec.get('filter'))}），其余任务一律不排"
    elif ctype == "at_most_one":
        text = f"{_fmt_tasks(spec.get('tasks'))} 中最多选 1 个"
    elif ctype == "at_least_one":
        text = f"{_fmt_tasks(spec.get('tasks'))} 中至少选 1 个"
    elif ctype == "precedence":
        text = f"先后顺序：{spec.get('before')} 完工后 {spec.get('after')} 才能开工"
        if spec.get("gap"):
            # 时间轴单位是分钟（编译器 finish + gap <= start）
            text += f"，间隔至少 {spec['gap']} 分钟"
    elif ctype == "co_occur":
        mode = spec.get("mode")
        # 保留原枚举值：预览要能被对照回 DSL，翻译丢了原值就没法核对。
        text = f"{_fmt_tasks(spec.get('tasks'))} {CO_OCCUR_LABELS.get(mode, '模式未识别')}（{mode}）"
    elif ctype == "no_overlap":
        text = f"资源 {spec.get('resource')} 上 {_fmt_tasks(spec.get('tasks'))} 不得重叠"
    elif ctype == "capacity":
        # 日配额：不写"某天最多几个作业线"，用户会以为是全局上限。
        label = CAPACITY_LABELS.get(str(spec.get("resource")), "资源")
        text = (
            f"日配额：{spec.get('time')} 当天最多用 {spec.get('limit')} 个"
            f"{label}（{spec.get('resource')}）"
        )
    elif ctype == "fix":
        text = f"固定作业线：{spec.get('task')} 只上 {spec.get('line')} 号线"
    elif ctype == "cardinality":
        text = f"{_fmt_filter(spec.get('filter'))} 的完成数 {op} {spec.get('value')}"
    elif ctype == "deadline":
        text = f"最晚完工：{spec.get('task')} 不晚于 {spec.get('time')}"
    elif ctype == "earliest":
        text = f"最早开工：{spec.get('task')} 不早于 {spec.get('time')}"
    elif ctype == "forbidden_dates":
        text = f"禁止开工日期：{_fmt_tasks(spec.get('dates'))}"
    elif ctype == "budget":
        text = f"预算：{spec.get('quantity')} {op} {spec.get('value')}"
    elif ctype == "linear":
        text = f"线性约束：{json.dumps(spec.get('terms'), ensure_ascii=False)} {op} {spec.get('value')}"
    else:
        return json.dumps(spec, ensure_ascii=False)

    # 注册表里登记了、但原型没实现编译的原语：确认后会直接报错。预览必须当场说，
    # 不能等用户点完“确认”才炸——那等于让用户批准一件做不到的事。
    try:
        from dsl_core.registry import primitives as prim_reg

        if ctype in prim_reg.CONSTRAINT_PRIMITIVES and ctype not in prim_reg.COMPILED_CONSTRAINTS:
            text += "　【原型未实现：确认后会报错，不会生效"
            reason = prim_reg.UNIMPLEMENTED_REASONS.get(str(ctype))
            text += f"；{reason}】" if reason else "】"
        if ctype == "co_occur" and spec.get("mode") not in prim_reg.COMPILED_CO_OCCUR_MODES:
            text += f"　【模式 {spec.get('mode')} 未实现：确认后会报错，不会生效】"
    except Exception:  # pragma: no cover - 注册表不可用时不影响预览
        pass

    missing = {
        key: value
        for key, value in spec.items()
        if key != "type" and not _covers(text, value)
    }
    if missing:
        # 兜底不变量：渲染器漏了字段（或以后新增原语忘了改这里）时，原样补出来，
        # 宁可难看，也不能让用户批准一个自己没看见的约束。
        text += f"（原始字段：{json.dumps(missing, ensure_ascii=False)}）"
    return text


def _covers(text: str, value: Any) -> bool:
    """粗略判断 value 是否已经在渲染文本里露过面。"""
    if isinstance(value, dict):
        return all(_covers(text, item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return all(_covers(text, item) for item in value)
    if isinstance(value, bool):
        return ("是" if value else "否") in text or str(value).lower() in text.lower()
    if isinstance(value, (int, float)):
        # 0.6 在预览里可能写成 0.60，两种都得认，否则整条约束被误判为漏字段
        for candidate in {str(value), f"{value:g}", f"{value:.2f}"}:
            if re.search(rf"(?<!\d){re.escape(candidate)}(?!\d)", text):
                return True
        return False
    return str(value) in text


def format_dsl_preview(dsl: dict[str, Any]) -> str:
    """把 DSL 约束/目标渲染成待确认预览。"""
    lines = ["已基于当前配置准备自定义约束/目标，尚未求解。", "变更预览："]
    index = 1
    for spec in dsl.get("constraints") or []:
        lines.append(f"{index}. {_render_constraint(spec)}")
        index += 1
    for spec in dsl.get("objectives") or []:
        kind = spec.get("type")
        if kind == "strategy":
            mode = spec.get("mode")
            suffix = f"（{mode}）" if mode else ""
            text = f"优化策略：{spec.get('name')}{suffix}"
        elif kind == "weighted":
            weights = "、".join(
                f"{name} {value:.2f}" for name, value in (spec.get("weights") or {}).items()
            )
            text = f"加权目标：{weights}"
        elif kind == "lexmin":
            stages = " → ".join(
                f"{'最小化' if s.get('dir') == 'minimize' else '最大化'}{s.get('q')}（{s.get('dir')}）"
                for s in spec.get("stages", [])
            )
            text = f"自定义分层目标：{stages}"
        else:
            text = f"自定义目标：{json.dumps(spec, ensure_ascii=False)}"
        missing = {
            key: value
            for key, value in spec.items()
            if key != "type" and not _covers(text, value)
        }
        if missing:
            text += f"（原始字段：{json.dumps(missing, ensure_ascii=False)}）"
        lines.append(f"{index}. {text}")
        index += 1
    for spec in dsl.get("parameters") or []:
        lines.append(f"{index}. 参数：{spec.get('field')} {spec.get('op', '=')} {spec.get('value')}")
        index += 1
    for spec in dsl.get("data") or []:
        lines.append(f"{index}. 数据假设：{spec.get('kind')} {json.dumps(spec, ensure_ascii=False)}")
        index += 1
    lines.extend([
        "这次会生成独立沙盘版本，不覆盖现有方案。",
        "回复“确认生成沙盘”执行；回复“取消”放弃本次变更。",
    ])
    return "\n".join(lines)


@dataclass
class DslChange:
    """一次 DSL 变更计划的产物。"""

    dsl: dict[str, Any]
    request: Any
    operations: list[dict[str, Any]] = field(default_factory=list)
    residual: bool = False
    timeline: bool = False
    custom: bool = False
    preview: str = ""
    source: str = "llm"
    confidence: float = 1.0

    @property
    def expressible(self) -> bool:
        """能否完全用 PlanningRequest 表达（否则要保留 DSL 段）。"""
        return not (self.residual or self.timeline or self.custom)


def plan_dsl_change(
    content: str,
    current: Any,
    base_version_id: str | None,
    context_block: str | None = None,
) -> DslChange | None:
    """解析 → 校验 → 映射。解析不到 DSL 或表达不了时返回 None。"""
    try:
        from app.core.llm_parser import parse as dsl_parse
        from dsl_core.planning_bridge import custom_objectives, dsl_to_request
        from app.core.model_change import _diff_requests
    except Exception:  # pragma: no cover - DSL 包缺失时静默回退
        return None

    try:
        parsed = dsl_parse(content, context_block=context_block)
    except Exception:  # noqa: BLE001 - LLM 不可用/失败由上层决定
        return None
    if parsed.route != "dsl" or not parsed.dsl:
        return None

    updated, residual, timeline = dsl_to_request(parsed.dsl, current)
    custom = custom_objectives(parsed.dsl)
    operations = _diff_requests(current, updated)
    preview = (
        format_dsl_preview(parsed.dsl)
        if (residual or timeline or custom) or not operations
        else ""
    )
    return DslChange(
        dsl=parsed.dsl,
        request=updated,
        operations=operations,
        residual=residual,
        timeline=timeline,
        custom=custom,
        preview=preview,
        source=parsed.source,
        confidence=parsed.confidence,
    )
