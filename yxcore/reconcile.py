"""意图对账：用户拧了哪些旋钮，产物里就必须有对应的证据。

这是「族固定」换来的最大便宜：对账规则由注册表驱动遍历，族是冻结的约 23 个，
每个族一条核对函数，**注册时就要求登记对账函数名，缺了不许注册**——所以
「新加了族却忘了补对账」这个大一修的窟窿在结构上不可能发生。

三种结果，不许混淆：

* **通过**：拿产物里的数字比对，有证据证明满足
* **违例**：有反证，用户的要求没兑现
* **已生效**：能证明旋钮被拧到位了，但没法从单份产物反推"这就是最优"——
  目标类（策略/权重）只能到这一档。把它标成"通过"是撒谎，标成"未核对"会永远拦住出方案

「未核对」仍然保留给真查不出来的情况，且照样拦下：空白证据不能出方案。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from . import slots as S
from .patch import Patch

PASS, VIOLATION, APPLIED, UNCHECKED = "通过", "违例", "已生效", "未核对"


@dataclass
class Ctx:
    selected: list[dict] = field(default_factory=list)
    rec: dict[str, Any] = field(default_factory=dict)
    data: Any = None
    base_data: Any = None
    info: dict[str, Any] = field(default_factory=dict)
    request: Any = None


def _group_rows(ctx: Ctx, spec: dict) -> list[dict]:
    pred, _ = S.group_predicate(ctx.data, spec or {"kind": "全部"})
    return [s for s in ctx.selected if pred(s)] if pred else []


def _bucket_rows(ctx: Ctx, rows: list[dict], spec: dict) -> list[dict]:
    if not spec:
        return rows
    pred, _ = S.bucket_predicate(ctx.data, spec)
    return [s for s in rows if pred(s)] if pred else []


# ── 各族核对 ────────────────────────────────────────────────────────────

def _reconcile_count(patch: Patch, ctx: Ctx) -> tuple[str, str]:
    rows = _group_rows(ctx, patch.index.get("组"))
    got = len(rows)
    want = int(patch.value)
    who = S.describe_group(ctx.data, patch.index.get("组"))
    if patch.slot == "count_min":
        return (PASS, f"{who}：排了 {got} 个，要求至少 {want}") if got >= want else \
            (VIOLATION, f"{who}：只排了 {got} 个，要求至少 {want} 个")
    return (PASS, f"{who}：排了 {got} 个，上限 {want}") if got <= want else \
        (VIOLATION, f"{who}：排了 {got} 个，超过上限 {want}")


def _reconcile_bucket(patch: Patch, ctx: Ctx) -> tuple[str, str]:
    rows = _bucket_rows(ctx, _group_rows(ctx, patch.index.get("组")), patch.index.get("桶"))
    got = len(rows)
    want = int(patch.value)
    label = patch.index.get("桶", {}).get("value", "全程")
    who = S.describe_group(ctx.data, patch.index.get("组"))
    if patch.slot == "bucket_min":
        return (PASS, f"{label} 内 {who}：排了 {got} 个，要求至少 {want}") if got >= want else \
            (VIOLATION, f"{label} 内 {who}：只排了 {got} 个，要求至少 {want} 个")
    return (PASS, f"{label} 内 {who}：排了 {got} 个，上限 {want}") if got <= want else \
        (VIOLATION, f"{label} 内 {who}：排了 {got} 个，超过上限 {want}")


def _reconcile_earliest(patch: Patch, ctx: Ctx) -> tuple[str, str]:
    minute = patch.value if isinstance(patch.value, (int, float)) else None
    if minute is None:
        minute = ctx.data.minute(str(patch.value))
    rows = _group_rows(ctx, patch.index.get("组"))
    early = [s["任务编号"] for s in rows if s["开始"] < minute]
    return (PASS, f"{len(rows)} 个任务都在 {ctx.data.stamp(minute)[:16]} 之后开工") if not early else \
        (VIOLATION, f"这些任务开工早于要求的时点：{early}")


def _reconcile_deadline(patch: Patch, ctx: Ctx) -> tuple[str, str]:
    """期限走数据补丁，所以核对的是「任务自带的期限确实被改了」+ 排程没超期。"""
    minute = patch.value if isinstance(patch.value, (int, float)) else ctx.data.minute(str(patch.value))
    ids = S.group_task_ids(ctx.data, patch.index.get("组")) or []
    tasks = {t["任务编号"]: t for t in ctx.data.tasks}
    applied = [tid for tid in ids if (tasks.get(tid, {}).get("截止分钟") or 10**9) <= minute]
    if not applied:
        return UNCHECKED, "期限没有落到任何任务上，无法核对"
    late = [s["任务编号"] for s in ctx.selected
            if s["任务编号"] in ids and s["完成"] > minute]
    return (PASS, f"{len(applied)} 个任务的期限已收紧，且都按时完工") if not late else \
        (VIOLATION, f"这些任务完工晚于收紧后的期限：{late}")


def _reconcile_line_fixed(patch: Patch, ctx: Ctx) -> tuple[str, str]:
    tid = patch.index.get("任务", {}).get("value")
    rows = [s for s in ctx.selected if s["任务编号"] == tid]
    if not rows:
        return VIOLATION, f"{tid} 根本没排进计划"
    return (PASS, f"{tid} 固定在 {rows[0]['作业线']} 号船") if rows[0]["作业线"] == int(patch.value) else \
        (VIOLATION, f"{tid} 实际在 {rows[0]['作业线']} 号船，要求是 {patch.value} 号")


def _reconcile_cap(patch: Patch, ctx: Ctx) -> tuple[str, str]:
    dim = patch.index.get("维度", {}).get("value")
    field = {"作业线": "作业线", "风机": "风机编号", "部件": "部件编号"}.get(dim)
    rows = _bucket_rows(ctx, ctx.selected, patch.index.get("桶"))
    used = {s[field] for s in rows}
    label = patch.index.get("桶", {}).get("value", "全程")
    return (PASS, f"{label} 内占用 {len(used)} 个{dim}，上限 {patch.value}") if len(used) <= int(patch.value) else \
        (VIOLATION, f"{label} 内占用了 {len(used)} 个{dim}，超过上限 {patch.value}")


def _reconcile_budget(patch: Patch, ctx: Ctx) -> tuple[str, str]:
    from . import costs  # noqa: PLC0415

    kind = patch.index.get("口径", {}).get("value")
    key = {"现金": "现金支出", "总成本": "总成本", "人工": "人工",
           "备件": "备件", "租船天": "船舶艘天"}.get(kind)
    if key is None:
        return UNCHECKED, f"预算口径 {kind!r} 未登记，无法核对"
    spec = patch.index.get("组") or {"kind": "全部"}
    whole = (spec.get("kind") or "全部") == "全部"
    want = float(patch.value)
    where = "整轮" if whole else S.describe_group(ctx.data, spec)

    if whole:
        if key not in ctx.rec:
            return UNCHECKED, f"结算里没有 {key} 这个口径，无法核对"
        got = float(ctx.rec[key])
    else:
        # 分组口径不能拿整轮结算去比——那会把「高危超了」和「整轮没超」混为一谈
        if kind not in ("人工", "备件"):
            return UNCHECKED, f"{kind} 不支持按「{where}」核对（只能整轮）"
        pred, _ = S.group_predicate(ctx.data, spec)
        rows = [s for s in ctx.selected if pred(s)] if pred else []
        cost_of = costs.labor_yuan if kind == "人工" else costs.spare_yuan
        got = sum(cost_of(ctx.data, s) for s in rows)
    return (PASS, f"{where}的{kind} {round(got, 2)}，上限 {want}") if got <= want + 1e-6 else \
        (VIOLATION, f"{where}的{kind} {round(got, 2)}，超过上限 {want}")


def _reconcile_before_seq(patch: Patch, ctx: Ctx) -> tuple[str, str]:
    seq = patch.index.get("序列", {}).get("value") or []
    gap = int(round(float(patch.value or 0) * 60))
    done = {s["任务编号"]: s for s in ctx.selected}
    missing = [t for t in seq if t not in done]
    if missing:
        return VIOLATION, f"说好要按顺序修，但这些任务没排进去：{missing}"
    bad = []
    for earlier, later in zip(seq, seq[1:]):
        if done[earlier]["完成"] + gap > done[later]["开始"]:
            bad.append(f"{earlier} 完工 {done[earlier]['完成']} 之后才轮到 {later}"
                       f"（开工 {done[later]['开始']}）")
    return (PASS, f"{len(seq)} 个任务按序排入，间隔 {gap} 分钟") if not bad else \
        (VIOLATION, "；".join(bad))


def _reconcile_same_bucket(patch: Patch, ctx: Ctx) -> tuple[str, str]:
    ids = patch.index.get("集合", {}).get("value") or []
    粒度 = patch.index.get("粒度", {}).get("value", "天")
    done = {s["任务编号"]: s for s in ctx.selected}
    missing = [t for t in ids if t not in done]
    if missing:
        return VIOLATION, f"要求同{粒度}的任务没全排进去：{missing}"
    key = (lambda s: s["开始"] // 1440) if 粒度 == "天" else (lambda s: s["窗口编号"])
    values = {key(done[t]) for t in ids}
    return (PASS, f"{len(ids)} 个任务落在同一个{'日期' if 粒度 == '天' else '窗口'}") if len(values) == 1 else \
        (VIOLATION, f"这些任务落在了 {len(values)} 个不同的{'日期' if 粒度 == '天' else '窗口'}里")


def _reconcile_all_diff(patch: Patch, ctx: Ctx) -> tuple[str, str]:
    ids = patch.index.get("集合", {}).get("value") or []
    粒度 = patch.index.get("粒度", {}).get("value", "天")
    done = {s["任务编号"]: s for s in ctx.selected}
    missing = [t for t in ids if t not in done]
    if missing:
        return VIOLATION, f"要求分在不同{粒度}的任务没全排进去：{missing}"
    key = (lambda s: s["开始"] // 1440) if 粒度 == "天" else (lambda s: s["窗口编号"])
    seen = {}
    for tid in ids:
        seen.setdefault(key(done[tid]), []).append(tid)
    clash = [v for v in seen.values() if len(v) > 1]
    return (PASS, f"{len(ids)} 个任务分在 {len(seen)} 个不同{'日期' if 粒度 == '天' else '窗口'}") if not clash else \
        (VIOLATION, f"这些任务挤在了同一{'天' if 粒度 == '天' else '个窗口'}：{clash}")


def _reconcile_sev(patch: Patch, ctx: Ctx) -> tuple[str, str]:
    pair = patch.index.get("层对", {}).get("value") or []
    gap = int(round(float(patch.value or 0) * 60)) if patch.slot == "sev_gap" else 0
    earlier, later = pair
    high = [s for s in ctx.selected if s["严重等级"] == earlier]
    low = [s for s in ctx.selected if s["严重等级"] == later]
    if not high or not low:
        return UNCHECKED, f"{earlier} 或 {later} 层在产物里没有任务，无从核对"
    latest = max(s["完成"] for s in high)
    earliest_start = min(s["开始"] for s in low)
    return (PASS, f"{earlier} 层最晚完工 {latest}，{later} 层最早开工 {earliest_start}") \
        if latest + gap <= earliest_start else \
        (VIOLATION, f"{earlier} 层最晚完工 {latest}（+{gap} 分钟），"
                    f"晚于 {later} 层最早开工 {earliest_start}")


def _reconcile_scope_only(patch: Patch, ctx: Ctx) -> tuple[str, str]:
    spec = patch.index.get("范围") or {"kind": "全部"}
    pred, _ = S.scope_predicate(ctx.data, spec)
    rows = [s for s in ctx.selected if pred(s)] if pred else []
    outside = len(ctx.selected) - len(rows)
    # 「哪一格」必须点名：两条「只修」叠着用时，光说「都在范围内」看不出是哪一条兑现了
    if (spec.get("kind") or "全部") in S.GROUP_KINDS:
        where = S.describe_group(ctx.data, spec)
    else:
        where = f"{spec.get('value')} 这个时段" if spec.get("kind") != "日期区间" else \
            f"{spec['value'][0]} 到 {spec['value'][1]}"
    return (PASS, f"只修「{where}」：产物 {len(rows)} 个都在范围内，范围外 {outside} 个") \
        if outside == 0 else \
        (VIOLATION, f"只修「{where}」：产物里还有 {outside} 个任务在范围之外")


def _reconcile_part_hours(patch: Patch, ctx: Ctx) -> tuple[str, str]:
    from dsl_core.registry import entities
    pid = entities.resolve_part(ctx.data, str(patch.index.get("部件", {}).get("value")))
    if pid is None:
        return UNCHECKED, "部件编号解析不到，无法核对"
    levels = [patch.index.get("严重度", {}).get("value")] if patch.index.get("严重度") else ["高", "中", "低"]
    seas = [patch.index.get("海况", {}).get("value")] if patch.index.get("海况") else ["常态", "恶劣但安全"]
    keys = [f"{lv}严重度{sea}维修工时（小时）" for lv in levels for sea in seas]
    want = float(patch.value)
    got = [round(float(ctx.data.parts[pid][k]), 6) for k in keys]
    ok = all(abs(g - want) < 1e-6 for g in got)
    label = _part_label(ctx.data, pid)
    return (PASS, f"{label} 的 {len(keys)} 个工时刻都已改成 {want} 小时") if ok else \
        (VIOLATION, f"{label} 的工时刻实际是 {got}，要求是 {want}")


def _part_label(data, pid: str | None) -> str:
    """对外说话用部件名称，不甩内部编号（用户看不懂「部件01」）。"""
    name = data.parts.get(pid or "", {}).get("部件名称")
    return f"{name}（{pid}）" if name else (pid or "未知部件")


def _reconcile_part_arrival(patch: Patch, ctx: Ctx) -> tuple[str, str]:
    from dsl_core.registry import entities
    pid = entities.resolve_part(ctx.data, str(patch.index.get("部件", {}).get("value")))
    minute = patch.value if isinstance(patch.value, (int, float)) else ctx.data.minute(str(patch.value))
    frozen = getattr(ctx.data, "part_freeze", {}).get(pid)
    if frozen is None:
        return UNCHECKED, f"{_part_label(ctx.data, pid)} 没有登记到货冻结，无法核对"
    early = [s["任务编号"] for s in ctx.selected
             if s["部件编号"] == pid and s["开始"] < minute]
    return (PASS, f"{_part_label(ctx.data, pid)} 在 {ctx.data.stamp(minute)[:16]} 之前没有开工") if not early and frozen >= minute else \
        (VIOLATION, f"{_part_label(ctx.data, pid)} 在到货前就开工了：{early}")


def _reconcile_weather(patch: Patch, ctx: Ctx) -> tuple[str, str]:
    day = str(patch.index.get("日期", {}).get("value"))
    row = next((r for i, r in enumerate(ctx.data.weather)
                if ctx.data.stamp(i * 1440).startswith(day[:10])), None)
    if row is None:
        return UNCHECKED, f"{day} 不在规划期内，无法核对"
    base_row = next((r for i, r in enumerate(ctx.base_data.weather)
                     if ctx.base_data.stamp(i * 1440).startswith(day[:10])), None)
    changed = base_row is None or row["海况类别"] != base_row["海况类别"] or \
        row["可用小时"] != base_row["可用小时"]
    return (PASS, f"{day} 海况改成 {row['海况类别']}（可用 {row['可用小时']} 小时）") if changed else \
        (UNCHECKED, f"{day} 的海况与基座一致，看不出这次改动生效了没有")


def _reconcile_hourly(patch: Patch, ctx: Ctx) -> tuple[str, str]:
    minute = ctx.data.minute(str(patch.index.get("时刻", {}).get("value")))
    index = minute // 60
    field = "电价" if patch.index.get("项", {}).get("value", "电价") == "电价" else "风速"
    got = float(ctx.data.hourly[index][field])
    want = float(patch.value)
    return (PASS, f"{ctx.data.stamp(minute)[:16]} 的{field} = {got}") if abs(got - want) < 1e-6 else \
        (VIOLATION, f"{ctx.data.stamp(minute)[:16]} 的{field} 实际 {got}，要求 {want}")


def _reconcile_window(patch: Patch, ctx: Ctx) -> tuple[str, str]:
    wid = str(patch.index.get("窗口", {}).get("value"))
    which = patch.index.get("端", {}).get("value", "开始")
    key = "开始" if which == "开始" else "结束"
    minute = patch.value if isinstance(patch.value, (int, float)) else ctx.data.minute(str(patch.value))
    row = next((w for w in ctx.data.windows if w["编号"] == wid), None)
    if row is None:
        return UNCHECKED, f"{wid} 不在本次规划范围内，无法核对"
    return (PASS, f"{wid} 的{key} = {ctx.data.stamp(row[key])[:16]}") if row[key] == minute else \
        (VIOLATION, f"{wid} 的{key} 实际 {ctx.data.stamp(row[key])[:16]}，要求 {ctx.data.stamp(minute)[:16]}")


def _reconcile_strategy(patch: Patch, ctx: Ctx) -> tuple[str, str]:
    """目标类只能到「已生效」：能证明策略确实传给了求解器，不能从单份产物反推最优。

    把它标成"通过"是撒谎——单份产物证不了最优性。
    """
    used = ctx.info.get("策略") or (ctx.request.strategy if ctx.request else None)
    return APPLIED, f"目标策略按「{used}」求解（单份产物证明不了最优，只能证明策略生效）"


def _reconcile_partial(patch: Patch, ctx: Ctx) -> tuple[str, str]:
    total = len(ctx.data.tasks)
    done = len(ctx.selected)
    return APPLIED, (f"按部分排程求解：{done}/{total} 个任务排入"
                     + ("（全部排下了，没有少修）" if done == total else ""))


RECONCILERS: dict[str, Callable[[Patch, Ctx], tuple[str, str]]] = {
    "reconcile_strategy": _reconcile_strategy,
    "reconcile_count": _reconcile_count,
    "reconcile_bucket": _reconcile_bucket,
    "reconcile_earliest": _reconcile_earliest,
    "reconcile_deadline": _reconcile_deadline,
    "reconcile_line_fixed": _reconcile_line_fixed,
    "reconcile_cap": _reconcile_cap,
    "reconcile_budget": _reconcile_budget,
    "reconcile_before_seq": _reconcile_before_seq,
    "reconcile_same_bucket": _reconcile_same_bucket,
    "reconcile_all_diff": _reconcile_all_diff,
    "reconcile_sev_before": _reconcile_sev,
    "reconcile_sev_gap": _reconcile_sev,
    "reconcile_scope_only": _reconcile_scope_only,
    "reconcile_part_hours": _reconcile_part_hours,
    "reconcile_part_arrival": _reconcile_part_arrival,
    "reconcile_weather": _reconcile_weather,
    "reconcile_hourly": _reconcile_hourly,
    "reconcile_window": _reconcile_window,
    "reconcile_partial": _reconcile_partial,
}


def reconcile(patches: list[Patch], ctx: Ctx) -> dict[str, Any]:
    """逐条核对。返回 {明细, 违例, 未核对, 已生效数}。"""
    detail: list[dict[str, str]] = []
    violations: list[str] = []
    unchecked: list[str] = []

    # 参数类：直接比参数表，有证据就算通过
    for patch in patches:
        if patch.slot in S.PARAM_SLOTS and patch.op != "reset":
            key = S.PARAM_ALIASES[patch.slot]
            got = ctx.data.params.get(key)
            want = ctx.request.overrides.get(key) if ctx.request else None
            if want is None:
                detail.append({"槽位": patch.slot, "结果": UNCHECKED, "说明": "参数值没落进 overrides"})
                unchecked.append(patch.slot)
            elif abs(float(got) - float(want)) < 1e-9 or str(got) == str(want):
                detail.append({"槽位": patch.slot, "结果": PASS, "说明": f"参数 {key} = {got}"})
            else:
                detail.append({"槽位": patch.slot, "结果": VIOLATION,
                               "说明": f"参数 {key} 实际 {got}，要求 {want}"})
                violations.append(f"{patch.slot} 参数没生效")
            continue
        if patch.op == "reset":
            detail.append({"槽位": patch.slot, "结果": APPLIED, "说明": "已恢复默认"})
            continue
        slot = S.SLOTS.get(patch.slot)
        if slot is None:
            detail.append({"槽位": patch.slot, "结果": UNCHECKED, "说明": "槽位不在册"})
            unchecked.append(patch.slot)
            continue
        func = RECONCILERS.get(slot.reconcile)
        if func is None:
            # 注册即承诺：走到这里说明注册表里写了个不存在的对账函数名
            detail.append({"槽位": patch.slot, "结果": UNCHECKED,
                           "说明": f"登记表里的对账函数 {slot.reconcile} 没有实现"})
            unchecked.append(patch.slot)
            continue
        state, note = func(patch, ctx)
        detail.append({"槽位": patch.slot, "结果": state, "说明": note,
                       "原话": patch.utterance})
        if state == VIOLATION:
            violations.append(f"{patch.slot}：{note}")
        elif state == UNCHECKED:
            unchecked.append(f"{patch.slot}：{note}")

    return {
        "明细": detail,
        "违例": violations,
        "未核对": unchecked,
        "通过数": sum(1 for d in detail if d["结果"] == PASS),
        "已生效数": sum(1 for d in detail if d["结果"] == APPLIED),
    }


def describe(audit: dict[str, Any]) -> str:
    lines = []
    for item in audit["明细"]:
        lines.append(f"[{item['结果']}] {item['槽位']}：{item['说明']}")
    return "\n".join(lines)


def audit_coverage() -> list[str]:
    """注册表里登记的每个对账函数都必须有实现——这是「注册即承诺」的守门员。"""
    return sorted({slot.reconcile for slot in S.SLOTS.values() if slot.reconcile not in RECONCILERS})
