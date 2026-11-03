from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import re
from dataclasses import dataclass, field
from datetime import datetime

from app.core.planning_intent import PlanningIntentCompiler, PlanningRequest

_PATCH_IDENTITY = PlanningIntentCompiler._patch_identity

# 一句话里的每个“动作单位”：按 X 算一遍 / 用 X 排一版
_SCENARIO_RE = re.compile(
    r"(?:按|用|以|按照|采用)(.+?)"
    r"(?:算一遍|排一遍|算一版|排一版|生成一版|来一版|算一次|排一次|排个|算个|排|算)"
)
_COMPARE_RE = re.compile(r"(?:然后|最后|接着|并且|再|顺便)?(?:帮我)?(?:对比|比较|比一比|比一下)")
# 「默认」单字太宽：「默认权重」「用默认权重」是加权模式的正常说法，
# 不能被当成「按默认配置重跑」，否则场景会被沿用当前模式、两版撞成同一个。
# 只认整段确实在说「按原样再来一遍」的词组。
_DEFAULT_TOKENS = ("默认配置", "默认方法", "默认参数", "原样", "不变", "当前配置", "原来的", "照旧")

# 「加权和分层，都以成本最低优先，算一版」——名词并列 + 共享谓语。
# 动词并列（按 A 算一版、按 B 算一版）由 _SCENARIO_RE 管，这种句式它一条也匹配不到。
_SHARED_MARK_RE = re.compile(r"都|各|分别|一并|同时")
_PLANNING_VERB_RE = re.compile(r"算|排|生成|做|跑")
_LEADING_NOISE_RE = re.compile(r"^(?:帮我|请|麻烦|我想|我要|那就|就|再)\s*")
_MODE_WORDS = ("加权", "分层")
# 「不要加权」「而非分层」不能算声明了那个模式，否则会把用户明确排除的版本算出来。
_NEGATED_MODE_RE = re.compile(
    r"(?:不要|不用|别用|别|不采用|不按|而非|而不是)\s*(?:用|按|走|要)?\s*(?:加权|分层)"
)


PARAMETER_LABELS = {
    "船租（元每艘每天）": "船租",
    "人工单价（元每人时）": "人工单价",
    "最多作业线数": "最多作业线数",
    "人员总上限": "人员总上限",
    "每天最多维修小时数": "每天最多维修小时数",
    "风机间转移小时数": "风机间转移时间",
    "每天维修开始时间": "每天开工时刻",
    "每天维修结束时间": "每天收工时刻",
}

# 预览里每个参数该带什么单位。key 用展示名，因为 _append_change 已把原名换成展示名。
PARAMETER_UNITS = {
    "船租": lambda v: f"{float(v) / 10000:.2f} 万元",
    "人工单价": lambda v: f"{float(v):.4g} 元/人时",
    "人员总上限": lambda v: f"{int(v)} 人",
    "风机间转移时间": lambda v: f"{float(v):.4g} 小时",
    "每天最多维修小时数": lambda v: f"{float(v):.4g} 小时",
    "最多作业线数": lambda v: f"{int(v)} 条",
    "每天开工时刻": lambda v: f"{v} 开工",
    "每天收工时刻": lambda v: f"{v} 收工",
}

CONFIRM_TOKENS = (
    "确认生成沙盘",
    "确认生成方案",
    "确认开始",
    "开始求解",
    "发起求解",
    "执行变更",
    "跑吧",
)
CANCEL_TOKENS = ("取消变更", "取消", "先不改", "算了", "停止变更")

SOLVE_EXECUTION_COMMANDS = {
    "发起求解",
    "你发起求解",
    "开始求解",
    "现在开始求解",
    "直接求解",
    "按默认参数求解",
    "用默认参数求解",
    "默认生成",
    "那你就默认生成",
}


@dataclass
class ModelChangePlan:
    request: PlanningRequest
    operations: list[dict[str, Any]]
    preview: str
    source_prompt: str
    base_version_id: str | None
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return bool(self.errors)


def is_confirmation(message: str) -> bool:
    text = "".join(message.split())
    return any(token in text for token in CONFIRM_TOKENS)


def is_solve_execution_command(message: str) -> bool:
    text = "".join(message.split()).strip("，。！？!?；;")
    return text in SOLVE_EXECUTION_COMMANDS


def is_cancellation(message: str) -> bool:
    text = "".join(message.split())
    return any(token in text for token in CANCEL_TOKENS)


def build_change_plan(
    message: str,
    current: PlanningRequest,
    base_version_id: str | None,
    compiler: PlanningIntentCompiler | None = None,
) -> ModelChangePlan:
    compiler = compiler or PlanningIntentCompiler()
    compiled = compiler.compile_detailed(message, current=current)
    updated = compiled.request
    operations = _diff_requests(current, updated)
    if compiled.errors:
        operations = []
    elif not operations and any(
        token in "".join(message.split())
        for token in ("重新排程", "重新求解", "重新安排", "再算一版")
    ):
        operations.append(
            {
                "kind": "rerun",
                "key": "求解配置",
                "before": "当前方案",
                "after": "沿用当前配置重新求解",
            }
        )
    if compiled.errors:
        preview = "\n".join(
            ["这次没有开始求解，因为有一项设定无法成立：", *compiled.errors]
        )
    elif not operations:
        preview = (
            "我还没有识别出需要修改的模型内容。\n"
            "可以明确说参数（船租、日工时、作业线）、约束（预算、必修任务）、"
            "目标策略或权重，例如“船租上涨 20%，其他不变”。"
        )
        if compiled.notes:
            preview = preview + "\n" + "\n".join(compiled.notes)
    else:
        preview = _format_preview(operations, base_version_id)
        if compiled.notes:
            preview = preview + "\n" + "\n".join(compiled.notes)
    return ModelChangePlan(
        request=updated,
        operations=operations,
        preview=preview,
        source_prompt=message,
        base_version_id=base_version_id,
        notes=compiled.notes,
        errors=compiled.errors,
    )


@dataclass
class ScenarioPlan:
    """一次请求的其中一个场景。每个场景都是相对同一基准的独立变体，不做叠加。"""

    label: str
    request: PlanningRequest
    operations: list[dict[str, Any]]
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    source_prompt: str = ""


@dataclass
class ScenarioBatch:
    scenarios: list[ScenarioPlan]
    compare: bool
    source_prompt: str
    base_version_id: str | None

    @property
    def blocked(self) -> bool:
        return any(item.errors for item in self.scenarios)

    @property
    def preview(self) -> str:
        return _format_batch_preview(self)


def split_scenarios(message: str) -> list[str]:
    """把一句话拆成多个场景。

    两种句式都认：
      - 动词并列：「按加权算一版，再按分层算一版」——正则逐个抽；
      - 名词并列：「加权和分层，都以成本最低优先，算一版」——拆前半个句子的并列项，
        再把共享谓语接回去。光杆模式词不成句，交给 _normalize_segment 补全。
    """
    segments = [
        match.group(1).strip("，,。.、 ")
        for match in _SCENARIO_RE.finditer(message)
    ]
    if len(segments) >= 2:
        return segments
    shared = _split_shared_predicate(message)
    return shared or segments


def _split_shared_predicate(message: str) -> list[str]:
    """名词并列 + 共享谓语：「加权和分层，都以成本最低优先，算一版」。

    「都/各/分别」是并列标记，后面接的谓语由两个场景共享。标记位置之后必须
    出现排程动词，否则「成本最低优先和最短完成优先都要考虑」会被误拆。
    """
    marker = _SHARED_MARK_RE.search(message)
    if not marker:
        return []
    heads_text = _LEADING_NOISE_RE.sub("", message[: marker.start()].strip("，,。.、 "))
    tails = message[marker.start():].strip("，,。.、 ")
    if not _PLANNING_VERB_RE.search(tails):
        return []
    heads = [
        head.strip("，,。.、 ")
        for head in re.split(r"(?:和|与|及|、|,|，)", heads_text)
        if head.strip("，,。.、 ")
    ]
    if len(heads) < 2 or not all(_is_scenario_head(head) for head in heads):
        return []
    # 每一项都要补上共享谓语，否则「加权」两个字单独编译不出任何东西。
    return [f"{head}，{tails}" for head in heads]


def _is_scenario_head(text: str) -> bool:
    """并列项必须是模式词或策略名——避免把「船租和人工都按…」也当场景拆开。"""
    head = text.strip()
    if head in _MODE_WORDS:
        return True
    try:
        from dsl_core import objective_catalog as catalog

        return catalog.is_strategy(head)
    except Exception:  # noqa: BLE001 - 目录不可用就不拆，交给下游
        return False


def _normalize_segment(segment: str) -> str:
    """把光杆模式词补成「改成加权」，全局解析器才认。

    只对整段就是模式词、或模式词打头接逗号的情形动手，不动句子中间的词，
    所以“成本分层核算”这类说法不会被误伤。
    """
    text = segment.strip("，,。.、 ")
    bare = re.fullmatch(r"(加权|分层)\s*(?:目标模式|目标|模式)?", text)
    if bare:
        return f"改成{bare.group(1)}"
    head = re.match(r"^(加权|分层)\s*(?:目标模式|目标|模式)?\s*[，,、]\s*(.+)$", text)
    if head:
        return f"改成{head.group(1)}，{head.group(2)}"
    return segment


def _declared_mode(segment: str) -> str | None:
    """场景里点名的目标模式；明确排除的不算。"""
    text = _NEGATED_MODE_RE.sub("", segment)
    for word in _MODE_WORDS:
        if word in text:
            return word
    return None


def _force_mode(request: PlanningRequest, mode: str) -> PlanningRequest:
    """场景点了名就以它为准。

    解析器只认「改成加权／加权模式」这几种说法，「按加权算一版」「目标模式加权」
    都识别不出来——识别不出就会沿用当前模式，两段场景编译成同一份，
    用户要两版、系统拦下报“完全相同”。这里补的是这层兜底，不是替代解析器。
    """
    from dataclasses import replace

    updates: dict[str, Any] = {"mode": mode}
    if mode == "加权" and not request.weights:
        try:
            from dsl_core import objective_catalog as catalog

            updates["weights"] = dict(catalog.weighted_defaults(request.strategy))
        except Exception:  # noqa: BLE001 - 目录取不到就交给下游校验
            pass
    return replace(request, **updates)


def _declared_label(strategy: str, mode: str | None) -> str:
    return f"{strategy}·{mode}" if mode else strategy


def _scenario_label(raw: str) -> str:
    """能用「加权」「分层」这种短标签，就别把整句塞进预览。"""
    text = raw.strip("，,。.、 ")
    head = re.sub(r"^(?:改成|改为|设成|设为|用|按|走)", "", text.split("，")[0].strip())
    head = re.sub(r"(?:目标模式|目标|模式)$", "", head).strip()
    if head in _MODE_WORDS or _is_scenario_head(head):
        return head
    return text


def wants_compare(message: str) -> bool:
    return bool(_COMPARE_RE.search(message))


def is_scenario_batch(message: str) -> bool:
    return len(split_scenarios(message)) >= 2


def build_scenario_batch(
    message: str,
    current: PlanningRequest,
    base_version_id: str | None,
    compiler: PlanningIntentCompiler | None = None,
) -> ScenarioBatch:
    return build_scenario_batch_from_segments(
        split_scenarios(message),
        current,
        base_version_id,
        compiler,
        compare=wants_compare(message),
        source_prompt=message,
    )


def build_scenario_batch_from_segments(
    segments: list[str],
    current: PlanningRequest,
    base_version_id: str | None,
    compiler: PlanningIntentCompiler | None = None,
    *,
    compare: bool = True,
    source_prompt: str | None = None,
) -> ScenarioBatch:
    """把若干场景片段各自编译成一份独立变体。

    片段可以来自正则拆分，也可以直接来自模型（solve 的 scenarios 参数）。
    无论哪来都过一遍 _normalize_segment，保证光杆「加权／分层」也能编译。
    """
    compiler = compiler or PlanningIntentCompiler()
    plans: list[ScenarioPlan] = []
    for raw in segments:
        segment = _normalize_segment(raw)
        # 点名了模式的场景一律走正常编译，哪怕文案里顺带出现「默认权重」。
        # 这是语义护栏：以后再冒出含「默认」的新说法也不会误判成默认重跑。
        declared_mode = _declared_mode(segment)
        if not declared_mode and any(token in segment for token in _DEFAULT_TOKENS):
            # “按默认方法算一遍”没有改动项，但仍然是一次真实的求解。
            plans.append(
                ScenarioPlan(
                    label="默认配置",
                    request=current,
                    operations=[{
                        "kind": "rerun",
                        "key": "求解配置",
                        "before": "当前配置",
                        "after": "沿用当前配置重新求解",
                    }],
                    source_prompt=segment,
                )
            )
            continue
        compiled = compiler.compile_detailed(segment, current=current)
        request = compiled.request
        named_mode = declared_mode
        if named_mode and request.mode != named_mode:
            request = _force_mode(request, named_mode)
        operations = [] if compiled.errors else _diff_requests(current, request)
        errors = list(compiled.errors)
        if not errors and not operations:
            # 场景里点名了完整配置、只是恰好和当前一致时，它仍然是一次独立求解。
            declared = compiler.declared_strategy(segment)
            mode = named_mode
            if declared or mode:
                operations = [{
                    "kind": "rerun",
                    "key": "求解配置",
                    "before": "当前配置",
                    # 不写模式的话，两版预览会长得一样，用户没法确认哪版是哪版。
                    "after": f"按「{_declared_label(declared or current.strategy, mode)}」重新求解",
                }]
            else:
                errors.append(
                    f"没有认出“{segment}”要改什么。可以说参数（船租、日工时、作业线）、"
                    "约束（预算、必修任务、到货、期限）或目标策略。"
                )
        plans.append(
            ScenarioPlan(
                label=_scenario_label(raw),
                request=request,
                operations=operations,
                notes=list(compiled.notes),
                errors=errors,
                source_prompt=segment,
            )
        )
    _reject_duplicate_scenarios(plans)
    return ScenarioBatch(
        scenarios=plans,
        compare=compare,
        source_prompt=source_prompt or "；".join(segments),
        base_version_id=base_version_id,
    )


def _reject_duplicate_scenarios(plans: list[ScenarioPlan]) -> None:
    """两个场景编译出同一份请求，说明模式或权重有一环没被识别——拆了等于没拆。

    宁可当场报错，也不能给用户两份一模一样、还挂着“对比”的方案：
    那会比只给一版更误导。
    """
    from app.core.decision_contract import fingerprint

    seen: dict[str, int] = {}
    for index, item in enumerate(plans, start=1):
        if item.errors:
            continue
        try:
            key = fingerprint(item.request.to_payload())
        except Exception:  # noqa: BLE001 - 指纹算不出就别拦，不能把正常请求挡死
            continue
        first = seen.get(key)
        if first is not None:
            item.errors.append(
                f"和场景 {first} 编译出的模型完全相同，不是两个不同的方案。"
                "请说清这两版差在哪里（目标模式或权重）。"
            )
            continue
        seen[key] = index


def _format_batch_preview(batch: ScenarioBatch) -> str:
    if batch.blocked:
        lines = ["这次没有开始求解，因为其中有的场景无法成立："]
        for index, item in enumerate(batch.scenarios, start=1):
            for error in item.errors:
                lines.append(f"场景 {index}「{item.label}」：{error}")
        return "\n".join(lines)
    lines = [
        f"这句话我拆成了 {len(batch.scenarios)} 个场景，确认后会依次生成 "
        f"{len(batch.scenarios)} 个独立方案，不覆盖现有方案。",
    ]
    for index, item in enumerate(batch.scenarios, start=1):
        detail = "；".join(
            f"{operation['key']}：{_format_value(operation)}" for operation in item.operations
        )
        lines.append(f"场景 {index}「{item.label}」：{detail}")
        for note in item.notes:
            lines.append(f"　　{note}")
    if batch.compare:
        lines.append("全部算完后，我会把这几版放在一起对比。")
    lines.extend(
        [
            "回复“确认生成沙盘”依次执行；回复“取消”放弃本次变更。",
        ]
    )
    return "\n".join(lines)


def _append_change(
    operations: list[dict[str, Any]],
    *,
    kind: str,
    key: str,
    before: Any,
    after: Any,
) -> None:
    if before == after:
        return
    operations.append(
        {
            "kind": kind,
            "key": key,
            "before": before,
            "after": after,
        }
    )


def _diff_requests(current: PlanningRequest, updated: PlanningRequest) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    _append_change(operations, kind="strategy", key="优化策略", before=current.strategy, after=updated.strategy)
    _append_change(operations, kind="mode", key="目标模式", before=current.mode, after=updated.mode)

    for key in sorted(set(current.overrides) | set(updated.overrides)):
        _append_change(
            operations,
            kind="parameter",
            key=PARAMETER_LABELS.get(key, key),
            before=current.overrides.get(key),
            after=updated.overrides.get(key),
        )
    for key in sorted(set(current.relative_overrides) | set(updated.relative_overrides)):
        _append_change(
            operations,
            kind="relative_parameter",
            key=PARAMETER_LABELS.get(key, key),
            before=current.relative_overrides.get(key),
            after=updated.relative_overrides.get(key),
        )
    for key in sorted(set(current.limits) | set(updated.limits)):
        _append_change(
            operations,
            kind="constraint",
            key=key,
            before=current.limits.get(key),
            after=updated.limits.get(key),
        )
    current_patches = {_PATCH_IDENTITY(p): p for p in current.data_patches}
    updated_patches = {_PATCH_IDENTITY(p): p for p in updated.data_patches}
    for key in sorted(updated_patches, key=lambda item: str(item)):
        after = updated_patches[key]
        before = current_patches.get(key)
        if before == after:
            continue
        operations.append(
            {
                "kind": "data_patch",
                "key": _patch_label(after),
                "before": before,
                "after": after,
            }
        )
    current_weights = current.weights or {}
    updated_weights = updated.weights or {}
    if updated_weights:
        for key in sorted(set(current_weights) | set(updated_weights)):
            after = updated_weights.get(key)
            before = current_weights.get(key)
            if not after and not before:
                continue
            _append_change(
                operations,
                kind="weight",
                key=key,
                before=before,
                after=after,
            )
    return operations


def _patch_label(patch: dict[str, Any]) -> str:
    kind = patch.get("kind")
    if kind == "part_arrival":
        return f"备件到货·{patch.get('partName', '')}"
    if kind == "part_hours":
        scope = "·".join(
            part for part in (
                f"{patch['severity']}严重度" if patch.get("severity") else "",
                str(patch.get("sea") or ""),
            ) if part
        )
        suffix = f"·{scope}" if scope else ""
        return f"维修工时·{patch.get('partName', '')}{suffix}"
    if kind == "task_deadline":
        return f"完工期限·{patch.get('scope', '')}"
    return str(kind)


def _patch_text(patch: dict[str, Any]) -> str:
    kind = patch.get("kind")
    if kind == "part_arrival":
        at = datetime.fromisoformat(patch["at"])
        return f"{at.strftime('%Y-%m-%d')} 到货 {patch['quantity']} 套"
    if kind == "part_hours":
        # 定点信息必须出现在预览里：只写"15 小时"，用户不知道改的是哪一档，
        # 也就没法定点否决（比如他只想改常态，不想动恶劣海况那一列）。
        scope = "·".join(
            part for part in (
                f"{patch['severity']}严重度" if patch.get("severity") else "",
                str(patch.get("sea") or ""),
            ) if part
        )
        where = f"（{scope}）" if scope else "（全部严重度·两种海况）"
        if "hours" in patch:
            return f"定为 {float(patch['hours']):g} 小时{where}"
        return f"整件工时 ×{float(patch['factor']):.4g}{where}"
    if kind == "task_deadline":
        at = datetime.fromisoformat(patch["at"])
        return f"{at.strftime('%Y-%m-%d')} 前完成"
    return str(patch)


def _format_value(operation: dict[str, Any]) -> str:
    after = operation["after"]
    before = operation["before"]
    if after is None:
        # 多轮纠错把旧口径改掉时，该键在新请求里不存在
        return "取消该设置（继承当前方案）"
    if operation["kind"] == "data_patch":
        text = _patch_text(after)
        if before is None:
            return text
        return f"{_patch_text(before)} → {text}"
    if operation["kind"] == "relative_parameter":
        return f"×{float(after):.4g}（原值 × 倍率）"
    if operation["kind"] == "constraint":
        if operation["key"] in ("最大总成本", "现金预算"):
            return f"{float(after) / 10000:.2f} 万元"
        if operation["key"] == "必修任务":
            return "、".join(after) if isinstance(after, list) else str(after)
        return str(after)
    if operation["kind"] == "parameter":
        unit = PARAMETER_UNITS.get(operation["key"])
        if unit:
            return unit(after)
    if operation["kind"] == "weight":
        return f"{float(after):.4g}"
    if before is None:
        return str(after)
    return f"{before} → {after}"


def _format_preview(operations: list[dict[str, Any]], base_version_id: str | None) -> str:
    lines = [
        f"已基于{'方案 ' + base_version_id if base_version_id else '当前默认配置'}准备变更，尚未求解。",
        "变更预览：",
    ]
    for index, operation in enumerate(operations, start=1):
        lines.append(f"{index}. {operation['key']}：{_format_value(operation)}")
    lines.extend(
        [
            "未提及字段继承当前方案。",
            "这次会生成独立沙盘版本，不覆盖现有方案。",
            "回复“确认生成沙盘”执行；回复“取消”放弃本次变更。",
        ]
    )
    return "\n".join(lines)
