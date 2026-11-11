"""端到端编排：补丁 → 求解 → 回显。骨架抄 dsl_core/runner.run，但只有四步。

    load → 编译（四路通道）→ 校验（静态预检）→ solve

大一修还多一步「意图对账」（reconcile），那是第 2 期的事。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import slots as S
from .apply import CompiledRequest, compile_patches, needs_partial
from .patch import Patch, fingerprint, index_shape_error, normalize_patch
# 注意：本模块末尾也有一个 describe（描述求解结果），所以这里必须取别名，
# 否则导入会被后面的同名定义覆盖，对账的文本描述会调错函数。
from .reconcile import Ctx, reconcile
from .reconcile import describe as describe_audit
from .validate import validate
from . import PROJECT_ROOT, solver_modules


class YxError(RuntimeError):
    """拦截：校验没过或编译失败。携带全部错误信息，调用方直接说给用户听。

    ``code`` 说明是哪一类拦截。诊断层靠它区分「求解器证了做不到」和
    「时限内没找到」——把后者说成前者，就是替求解器撒谎。
    """

    def __init__(self, errors: list[str], warnings: list[str] | None = None,
                 code: str = "校验") -> None:
        super().__init__("；".join(errors))
        self.errors = errors
        self.warnings = warnings or []
        self.code = code


def run(
    patches: list[Patch],
    *,
    base_id: str = "基座01",
    as_of: str | None = None,
    seconds: int = 30,
    output: Path | None = None,
    strategy: str = "成本最低优先",
    max_tasks: int | None = None,
    _diagnose: bool = True,
) -> dict[str, Any]:
    """跑一次小一修求解。返回 {排程, 求解, 回显, 指纹, 数据, 警告}。"""
    modules = solver_modules()
    load_project = modules["load_project"]

    # 入口就归一：同一句口语从翻译层进来、从脚本手搓进来，必须落在同一格。
    # 只在 parse 里归一的话，`run()` 的其它调用方（回归、评测、将来的界面）
    # 会看到两套行为——「英文 kind 能用」取决于你是从哪条路进来的。
    bad = [str(i) for i, p in enumerate(patches, 1) if p is None]
    if bad:
        raise YxError([f"第 {'、'.join(bad)} 条补丁是空的（解析没成功）"], code="校验")
    shapes = [f"第 {i} 条：{err}" for i, p in enumerate(patches, 1)
              if (err := index_shape_error(p.slot, p.index))]
    if shapes:
        raise YxError(shapes, code="校验")
    patches = [normalize_patch(p) for p in patches]

    base_data = load_project(PROJECT_ROOT, base_id=base_id, as_of=as_of)
    request, errors = compile_patches(patches, base_data, base_data)
    if errors:
        raise YxError(errors, code="编译")

    try:
        data = load_project(
            PROJECT_ROOT, base_id=base_id, as_of=as_of,
            overrides=request.overrides or None,
            data_patches=request.data_patches or None,
            timeline_patches=request.timeline_patches or None,
        )
    except ValueError as exc:
        # 内核抛的本来就是说给用户听的（"这次改了不会影响结果"），
        # 但它是裸 ValueError，走出去会被当成系统内部错误。
        raise YxError([str(exc)], code="编译") from exc
    if max_tasks:
        data.tasks = data.tasks[:max_tasks]

    options = {t["任务编号"]: modules["candidates"](data, t) for t in data.tasks}
    errors, warnings = validate(patches, data, options)
    registry = S.audit_registry()
    if registry:
        # 注册表带病就不许出方案——「加了族却没对账」这类窟窿必须在这里炸出来
        raise YxError(["槽位注册表自检未通过：" + "；".join(registry)], code="注册表")
    if errors:
        raise YxError(errors, warnings, code="校验")

    request.partial = request.partial or needs_partial(patches, data, options)

    if request.strategy:
        strategy = request.strategy
    fp = fingerprint(patches, {"基座": base_id, "策略": strategy,
                               "部分排程": request.partial, "时限": seconds})
    output = output or (PROJECT_ROOT / "小一修" / "runs" / fp[:12])
    output.mkdir(parents=True, exist_ok=True)

    hint = modules["heuristic"](data, options, "严重性优先")
    # 策略只有一份出口：编好的请求里带就用它的，否则用 run 的默认。
    # 两边都传会变成同一个关键字传两次（strategy 槽位此前从未端到端跑过，就是这个bug）
    kwargs = request.as_solve_kwargs()
    kwargs.pop("strategy", None)
    try:
        selected, info = modules["solve"](
            data, options, output, seconds=seconds, strategy=strategy, hint=hint, **kwargs
        )
    except ValueError as exc:
        # 注入口闭包里的错误（该时段没候选、租船天没法按组算…）是内核拼成 ValueError 抛的。
        # 不接住就会变成一串 traceback——离用户那句话隔着三层，谁也看不懂。
        raise YxError([str(exc)], warnings, code="编译") from exc
    recommended = None
    if selected is None:
        # 没解出来不能把基线的旧方案交出去（大一修踩过：旧方案被当成"有解"返回，
        # 解状态还写 FEASIBLE，用户以为约束生效了）。这里直接拦，由诊断层说原因。
        from .diagnose import explain_infeasible

        info["策略"] = strategy
        # solve() 已在 RunSpec 里定稿权重；这里只补一个可能为空的回显，不许覆盖。
        info.setdefault("权重", request.weights)
        # _diagnose=False 是归因探针在调用，别再递归展开一次诊断
        reason = (explain_infeasible(patches, data, options, info, request, seconds=seconds)
                  if _diagnose else "这一条单独就排不出方案")
        proven = "INFEASIBLE" in str(info.get("解状态") or "")
        raise YxError([reason], warnings, code="不可行" if proven else "未证不可行")
    try:
        recommended = modules["account"](data, selected)
    except ValueError as exc:
        # 独立复核没过：这份排程自己站不住，不能带着"复核通过"交出去
        raise YxError([f"求解结果未通过独立复核：{exc}"], warnings, code="复核") from exc

    # 少修了几个任务，必须原原本本说出来。以前只在 request.partial（用户显式
    # 限制过范围/数量）时提醒；现在「本轮不修」是模型的合法分支，默认也会少修，
    # 再按规格层开关判断就会漏报——用户看到「排程成功」会以为全修了。
    total, done = len(data.tasks), len(selected)
    explicit = any(p.slot == "allow_partial" and p.op != "reset" for p in patches)
    if done < total and not explicit:
        reason = ("因为你限制了范围或数量" if request.partial
                  else "本轮按不修惩罚与维修成本权衡，未排任务留在台账里待后续窗口")
        warnings = list(warnings) + [
            f"这次是部分排程：{total} 个任务里排了 {done} 个，"
            f"其余 {total - done} 个这轮不修（{reason}）"
        ]
    # 策略与权重都已在 RunSpec 定稿并写进 info，这里只补缺、不许覆盖：
    # 以前 info["权重"] = request.weights 会把定稿值改回 None，正是「同一个
    # 事实各写一份」的最后一处漏点。
    info.setdefault("策略", strategy)
    info.setdefault("权重", request.weights)
    audit = reconcile(patches, Ctx(selected=selected, rec=recommended, data=data,
                                   base_data=base_data, info=info, request=request))
    # 违例是"对不上"，未核对是"没法证明对得上"——两者都不能出方案。
    # 收紧的代价是任何一条没实现对账的旋钮都会在这里被拦下，而不是悄悄放过去。
    if audit["违例"] or audit["未核对"]:
        raise YxError(["意图对账未通过：" + describe_audit(audit)], warnings, code="对账")
    audit_text = describe_audit(audit)
    # 状态诚实性：业务阶段没证到最优就要说出来，并写进产物。
    # 确定性决胜段已整块撤销，不再有「主目标证优、但没证明挑的是编号最小那张」
    # 这种把 OPTIMAL 连坐降级的假告警。
    unproven = list(info.get("未达最优阶段") or [])
    if unproven:
        warnings = list(warnings) + [
            f"这些目标没证到最优（{'、'.join(unproven)}）：方案在时限内波动，"
            "同一批旋钮换台机器排出来的内容可能不同"
        ]
    # 模型产物：求解器真实写下的变量/约束/参数清单（model_artifact.json）。
    # 不带它，用户问「你的数学模型」时工具层只能诚实说没保存——问句合法，
    # 答案不该缺。读失败不拦方案：数学模型页缺这一版只能说没存上，不该炸排程。
    artifact: dict[str, Any] | None = None
    for artifact_name in ("model_artifact.json", "diagnostic_model_artifact.json"):
        artifact_file = output / artifact_name
        if artifact_file.exists():
            try:
                artifact = json.loads(artifact_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):  # noqa: BLE001
                artifact = None
            break
    return {
        "排程": selected,
        "推荐": recommended,
        "求解": info,
        "回显": request.echo,
        "待确认": request.uncertain,
        "对账": audit_text,
        "指纹": fp,
        "数据": data.meta,
        "警告": warnings,
        # 实际有没有少修，看产物；规格层有没有开「允许少修」另存一格，别混。
        "部分排程": done < total,
        "规格层允许少修": request.partial,
        "已证最优": not unproven,
        "未证最优目标": unproven,
        "请求": request,
        "模型产物": artifact,
    }


def baseline(base_id: str = "基座01", seconds: int = 30, **kwargs) -> dict[str, Any]:
    """不带任何补丁的基线求解——每次对比都要有它，否则说不清「改了这个数」带来什么。"""
    return run([], base_id=base_id, seconds=seconds, **kwargs)


def describe(result: dict[str, Any]) -> str:
    """把结果说成人话：排了几个、花多少、哪些旋钮生效了。"""
    lines: list[str] = []
    selected = result["排程"]
    lines.append(f"排程 {'成功' if selected else '未产出'}："
                 + (f"{len(selected)} 个任务" if selected else "无"))
    if result.get("部分排程") and selected:
        lines.append(f"部分排程：只排了 {len(selected)} 个（不是全部任务）")
    if result["回显"]:
        lines.append("生效的旋钮：" + "；".join(result["回显"]))
    if result["待确认"]:
        lines.append("需要你确认：" + "；".join(result["待确认"]))
    if result["警告"]:
        lines.append("提醒：" + "；".join(result["警告"]))
    rec = result.get("推荐")
    if rec:
        lines.append(f"现金支出 {rec.get('现金支出')} 元，总成本 {rec.get('总成本')} 元，"
                     f"租船 {rec.get('船舶艘天')} 艘天")
    lines.append(f"指纹 {result['指纹'][:12]}")
    return "\n".join(lines)
