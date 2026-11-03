"""注入口闭包：槽位 → CP-SAT 约束。

内核的 ``build_model(extra_constraints=闭包)`` 在 choices/metrics 建好之后回调，
签名 ``(m, choices, task_vars, metrics) -> 错误列表``。所有走注入口的槽位都在这里
编译，**内核一个字都不用改**。

每一族的形态都是预写的（约束条数可能随数据变，形式不变）：

  * 计数类：``Σx ≥/≤ v``
  * 分桶类：``Σ{x : 桶 ∧ 组} ≥/≤ v``
  * 压零类：``Σ{x : 补集} == 0``（固定作业线、只修哪些、最早开工）
  * 容量类：``Σ 实例占用指示 ≤ v``（指示用 AddMaxEquality 从候选里取）
  * 时序类：``Σ完成·x + 间隔 ≤ Σ开始·x``（每任务恰选一个候选，所以和式就是它的时间）
  * 共现/互异：两个和式相等 / AddAllDifferent
  * 层间：两个辅助变量（AddMaxEquality / AddMinEquality）比大小

两条纪律：

1. **不引入大 M**。凡是带指示变量的条件约束（"若 018 排入则 019 必须…"）一律不做，
   那是大一修的能力，小一修明确拒绝（见 README 边界清单）。时序族要求涉及的任务
   **必须被修**（"先修018再修022"本来就是这个意思），由 apply 把它们加进必修名单，
   因此每任务恰选一个候选，和式直接就是它的时间，不需要松弛。
2. **算不出来就报错，不静默跳过**。闭包返回的错误会被内核拼成 ValueError 抛出。
   悄悄跳过的约束等于"用户以为生效了、其实没有"——这是大一修最惨的一类事故。
"""
from __future__ import annotations

from typing import Any, Callable

from . import slots as S

HOUR = 60  # 小时 → 分钟


def _money() -> int:
    from product_planner import MONEY  # noqa: PLC0415
    return MONEY


class _Ctx:
    """闭包编译上下文：把内核给的四样东西和快照绑在一起。"""

    def __init__(self, m, choices, task_vars, metrics, data):
        self.m = m
        self.choices = choices
        self.task_vars = task_vars
        self.metrics = metrics
        self.data = data
        self.errors: list[str] = []

    def pick(self, *predicates) -> list[Any]:
        """取出同时满足所有谓词的候选变量。"""
        rows = self.choices
        for pred in predicates:
            rows = [(c, x) for c, x in rows if pred(c)]
        return [x for _, x in rows]

    def start_of(self, task_id: str):
        return sum(c["开始"] * x for c, x in self.task_vars[task_id])

    def finish_of(self, task_id: str):
        return sum(c["完成"] * x for c, x in self.task_vars[task_id])

    def day_of(self, task_id: str):
        return sum((c["开始"] // 1440) * x for c, x in self.task_vars[task_id])

    def window_index_of(self, task_id: str):
        order = {w["编号"]: i for i, w in enumerate(self.data.windows)}
        return sum(order[c["窗口编号"]] * x for c, x in self.task_vars[task_id])


# ── 各族编译 ────────────────────────────────────────────────────────────

def _compile_count(patch, ctx: _Ctx) -> None:
    value = int(patch.value)
    pred, error = S.group_predicate(ctx.data, patch.index.get("组", {}))
    if error:
        ctx.errors.append(f"count_{patch.slot[-3:]}: {error}")
        return
    rows = ctx.pick(pred)
    if not rows:
        ctx.errors.append(f"{patch.slot}：这一格里没有任何候选，约束无法落地")
        return
    if patch.slot == "count_min":
        if value <= 0:
            return  # 不生效
        ctx.m.Add(sum(rows) >= value)
    else:
        total = len(ctx.data.tasks)
        if value >= total:
            return  # 恒真，不写进模型
        ctx.m.Add(sum(rows) <= value)


def _compile_bucket(patch, ctx: _Ctx) -> None:
    value = int(patch.value)
    group, error = S.group_predicate(ctx.data, patch.index.get("组", {}))
    if error:
        ctx.errors.append(f"{patch.slot}：{error}")
        return
    bucket, error = S.bucket_predicate(ctx.data, patch.index.get("桶", {}))
    if error:
        ctx.errors.append(f"{patch.slot}：{error}")
        return
    rows = ctx.pick(group, bucket)
    if not rows:
        ctx.errors.append(f"{patch.slot}：该时段这一格里没有任何候选，"
                          "这条要求在当前窗口/日期下不可能满足")
        return
    if patch.slot == "bucket_min":
        if value <= 0:
            return
        ctx.m.Add(sum(rows) >= value)
    else:
        if value < 0:
            ctx.errors.append(f"{patch.slot}：上限不能为负")
            return
        ctx.m.Add(sum(rows) <= value)


def _compile_earliest(patch, ctx: _Ctx) -> None:
    # 值可能是 ISO 时刻字符串（校验层已认过），也可能是分钟数；统一折成分钟。
    if isinstance(patch.value, str):
        try:
            minute = int(ctx.data.minute(patch.value))
        except (ValueError, AttributeError):
            ctx.errors.append(f"earliest：时刻 {patch.value!r} 读不出来")
            return
    else:
        minute = int(patch.value)
    group, error = S.group_predicate(ctx.data, patch.index.get("组", {}))
    if error:
        ctx.errors.append(f"earliest：{error}")
        return
    rows = [(c, x) for c, x in ctx.choices if group(c) and c["开始"] < minute]
    if rows:
        ctx.m.Add(sum(x for _, x in rows) == 0)


def _compile_line_fixed(patch, ctx: _Ctx) -> None:
    tid = S.group_task_ids(ctx.data, {"kind": "任务", "value": patch.index.get("任务", {}).get("value")})
    if not tid:
        ctx.errors.append("line_fixed：任务不存在")
        return
    tid = tid[0]
    if tid not in ctx.task_vars:
        ctx.errors.append(f"line_fixed：{tid} 不在本次告警快照里")
        return
    line = int(patch.value)
    rows = [x for c, x in ctx.task_vars[tid] if c["作业线"] != line]
    if rows:
        ctx.m.Add(sum(rows) == 0)
    if not [x for c, x in ctx.task_vars[tid] if c["作业线"] == line]:
        ctx.errors.append(f"line_fixed：{tid} 在 {line} 号船上没有任何可开工的候选")


def _compile_cap(patch, ctx: _Ctx) -> None:
    """某桶内被占用的实例数上限：给每个实例建一个「是否被占用」的指示变量。"""
    dim = patch.index.get("维度", {}).get("value")
    value = int(patch.value)
    bucket, error = S.bucket_predicate(ctx.data, patch.index.get("桶", {}))
    if error:
        ctx.errors.append(f"cap_max：{error}")
        return
    field = {"作业线": "作业线", "风机": "风机编号", "部件": "部件编号"}.get(dim)
    if field is None:
        ctx.errors.append(f"cap_max：维度 {dim!r} 未登记")
        return
    groups: dict[Any, list] = {}
    for c, x in ctx.choices:
        if bucket(c):
            groups.setdefault(c[field], []).append(x)
    if not groups:
        ctx.errors.append("cap_max：该时段内没有任何候选，约束无法落地")
        return
    used = []
    for key, rows in groups.items():
        u = ctx.m.NewBoolVar(f"used_{dim}_{key}")
        ctx.m.AddMaxEquality(u, rows)
        used.append(u)
    ctx.m.Add(sum(used) <= value)


def _compile_budget(patch, ctx: _Ctx) -> None:
    """预算上限。现金/总成本走内核 limits（见 apply），这里管注入口能算的口径。

    「组」不是装饰：用户说「高危备件费不超 30 万」时，管的必须只是高危那几个任务的
    备件费。整轮口径直接取内核 metrics；分组口径现场按候选重建和式，公式与内核同一份
    （见 costs.py）。租船天按船·天计、不分任务，只支持整轮。
    """
    from . import costs  # noqa: PLC0415

    口径 = patch.index.get("口径", {}).get("value")
    value = float(patch.value)
    if 口径 in ("现金", "总成本"):
        return  # 已经由 apply 走内核 limits 加了，这里不要再写一遍

    group_spec = patch.index.get("组") or {"kind": "全部"}
    whole = (group_spec.get("kind") or "全部") == "全部"

    if 口径 == "租船天":
        if not whole:
            ctx.errors.append(
                f"budget_max：租船天是按「船·天」算的，没法只算"
                f"「{S.describe_group(ctx.data, group_spec)}」的租船天数——请按整轮给上限")
            return
        expr = ctx.metrics.get("船舶艘天")
        limit = int(round(value))
    elif 口径 in ("人工", "备件"):
        if whole:
            expr = ctx.metrics.get("人工费" if 口径 == "人工" else "备件费")
            if isinstance(expr, int) and expr == 0:
                ctx.errors.append(f"budget_max：口径 {口径!r} 在本次模型里恒为 0，约束无从谈起")
                return
        else:
            pred, error = S.group_predicate(ctx.data, group_spec)
            if error:
                ctx.errors.append(f"budget_max：{error}")
                return
            rows = ctx.pick(pred)
            if not rows:
                ctx.errors.append(
                    f"budget_max：{S.describe_group(ctx.data, group_spec)}里没有任何候选，"
                    "这条上限无从落地")
                return
            cost_of = costs.labor_yuan if 口径 == "人工" else costs.spare_yuan
            terms = [round(cost_of(ctx.data, c) * costs.money()) * x for c, x in ctx.choices if pred(c)]
            expr = sum(terms)
        limit = round(value * costs.money())
    else:
        ctx.errors.append(f"budget_max：口径 {口径!r} 不走注入口（现金/总成本走内核 limits）")
        return
    ctx.m.Add(expr <= limit)


def _compile_before_seq(patch, ctx: _Ctx) -> None:
    seq = patch.index.get("序列", {}).get("value") or []
    gap = int(round(float(patch.value or 0) * HOUR))
    for tid in seq:
        if tid not in ctx.task_vars:
            ctx.errors.append(f"before_seq：{tid} 不在本次告警快照里")
            return
    for earlier, later in zip(seq, seq[1:]):
        ctx.m.Add(ctx.finish_of(earlier) + gap <= ctx.start_of(later))


def _compile_same_bucket(patch, ctx: _Ctx) -> None:
    ids = patch.index.get("集合", {}).get("value") or []
    粒度 = patch.index.get("粒度", {}).get("value", "天")
    for tid in ids:
        if tid not in ctx.task_vars:
            ctx.errors.append(f"same_bucket：{tid} 不在本次告警快照里")
            return
    getter = ctx.day_of if 粒度 == "天" else ctx.window_index_of
    for a, b in zip(ids, ids[1:]):
        ctx.m.Add(getter(a) == getter(b))


def _compile_all_diff(patch, ctx: _Ctx) -> None:
    ids = patch.index.get("集合", {}).get("value") or []
    粒度 = patch.index.get("粒度", {}).get("value", "天")
    for tid in ids:
        if tid not in ctx.task_vars:
            ctx.errors.append(f"all_diff：{tid} 不在本次告警快照里")
            return
    getter = ctx.day_of if 粒度 == "天" else ctx.window_index_of
    days = []
    for tid in ids:
        var = ctx.m.NewIntVar(0, ctx.data.end // 1440 + 1, f"day_{tid}")
        ctx.m.Add(var == getter(tid))
        days.append(var)
    ctx.m.AddAllDifferent(days)


def _compile_sev(patch, ctx: _Ctx) -> None:
    pair = patch.index.get("层对", {}).get("value") or []
    if len(pair) != 2:
        ctx.errors.append("sev_*：需要两个严重度层")
        return
    earlier, later = pair
    highs = [t["任务编号"] for t in ctx.data.tasks if t["严重等级"] == earlier]
    lows = [t["任务编号"] for t in ctx.data.tasks if t["严重等级"] == later]
    if not highs or not lows:
        ctx.errors.append(f"sev_*：{earlier} 或 {later} 层当前没有任务，约束无从谈起")
        return
    finish_max = ctx.m.NewIntVar(0, ctx.data.end, f"finish_max_{earlier}")
    ctx.m.AddMaxEquality(finish_max, [ctx.finish_of(t) for t in highs])
    start_min = ctx.m.NewIntVar(0, ctx.data.end, f"start_min_{later}")
    ctx.m.AddMinEquality(start_min, [ctx.start_of(t) for t in lows])
    gap = int(round(float(patch.value or 0) * HOUR)) if patch.slot == "sev_gap" else 0
    ctx.m.Add(finish_max + gap <= start_min)


def _compile_scope_only(patch, ctx: _Ctx) -> None:
    group, error = S.scope_predicate(ctx.data, patch.index.get("范围", {}))
    if error:
        ctx.errors.append(f"scope_only：{error}")
        return
    rows = [x for c, x in ctx.choices if not group(c)]
    if rows:
        ctx.m.Add(sum(rows) == 0)
    if not [x for c, x in ctx.choices if group(c)]:
        ctx.errors.append("scope_only：圈定的范围里没有任何候选，这次会一个都修不成")


COMPILERS: dict[str, Callable[[Any, _Ctx], None]] = {
    "count_min": _compile_count,
    "count_max": _compile_count,
    "bucket_min": _compile_bucket,
    "bucket_max": _compile_bucket,
    "earliest": _compile_earliest,
    "line_fixed": _compile_line_fixed,
    "cap_max": _compile_cap,
    "budget_max": _compile_budget,
    "before_seq": _compile_before_seq,
    "same_bucket": _compile_same_bucket,
    "all_diff": _compile_all_diff,
    "sev_before": _compile_sev,
    "sev_gap": _compile_sev,
    "scope_only": _compile_scope_only,
}

# 走数据补丁 / 参数 / 目标 / 部分的槽位不进闭包
NON_CLOSURE = {"deadline_delta", "part_hours", "part_arrival", "weather", "hourly", "window",
               "strategy", "weight", "allow_partial"}


def build_closure(patches, data) -> Callable | None:
    """把走注入口的补丁编译成一个闭包；没有这类补丁就返回 None（内核行为不变）。"""
    active = [p for p in patches if p.slot in COMPILERS and p.op != "reset"]
    if not active:
        return None

    def closure(m, choices, task_vars, metrics):
        ctx = _Ctx(m, choices, task_vars, metrics, data)
        for patch in active:
            COMPILERS[patch.slot](patch, ctx)
        return ctx.errors

    return closure
