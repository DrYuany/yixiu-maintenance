"""补丁 → 内核入参。四路通道全部走内核的公开入口，内核不用再改。

  * ``overrides``  ← A 组标量参数（白名单见 product_data.OVERRIDES）
  * ``limits``     ← 现金/总成本预算、必修任务（内核已认的 4 键）
  * ``weights``/``strategy`` ← B 组目标
  * ``data_patches``/``timeline_patches`` ← J 组与期限（改输入行，不是改系数）
  * ``extra_constraints`` ← 其余所有族（见 closure.py）

两条纪律：

1. **相对运算在这里折成绝对值**。``*`` / ``+`` / ``-`` 按基座值换算完才落库，
   模型里不留相对量——否则「涨两成」来两次就成了 1.44 倍，指纹也复现不了。
2. **部分排程判据只有一份**（决策 D10）。执行器和对账都读 ``needs_partial``，
   不许各写一套。大一修把它收在 compiler.needs_partial，这里照抄这个纪律。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from . import slots as S
from .closure import build_closure
from .patch import Patch, absolute_value

_TRUE = {"是", "允许", True, "true"}
_FALSE = {"否", "不允许", "禁止", False, "false"}


@dataclass
class CompiledRequest:
    """编译产物：直接喂给内核 solve() 的入参，外加给用户看的回显。"""

    overrides: dict[str, Any] = field(default_factory=dict)
    limits: dict[str, Any] = field(default_factory=dict)
    strategy: str | None = None
    mode: str | None = None
    weights: dict[str, float] | None = None
    partial: bool = False
    data_patches: list[dict] = field(default_factory=list)
    timeline_patches: list[dict] = field(default_factory=list)
    closure: Callable | None = None
    echo: list[str] = field(default_factory=list)
    uncertain: list[str] = field(default_factory=list)

    def as_solve_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"extra_constraints": self.closure, "partial": self.partial}
        if self.strategy:
            kwargs["strategy"] = self.strategy
        if self.mode:
            kwargs["mode"] = self.mode
        if self.weights:
            kwargs["weights"] = self.weights
        if self.limits:
            kwargs["limits"] = self.limits
        return kwargs


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    return None


def _to_minute(data, value: Any) -> int | None:
    """时刻值归一成分钟偏移。数字按分钟，字符串走快照的 minute()。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, str):
        try:
            return int(data.minute(value))
        except (ValueError, AttributeError, TypeError):
            return None
    return None


def needs_partial(patches: list[Patch], data, options: dict[str, list[dict]] | None = None) -> bool:
    """是否必须切部分排程（允许丢任务）。**判据只有这一份**。

    完整模式强制每个任务 ``sum(x)==1``，跟「这轮不修 018」「最多修 8 个」
    「只修叶片」直接矛盾，不切就必然判不可行。

    ``options`` 给了才能判断"桶是否覆盖全部候选"——「这周别动 003」只压一个时段，
    不等于整轮不修；只有桶把所有候选都罩住时才等价于排除。给不出就按不覆盖处理，
    宁可让求解器判不可行，也不替用户猜。
    """
    for patch in patches:
        if patch.op == "reset":
            continue
        if patch.slot == "allow_partial":
            flag = _as_bool(patch.value)
            if flag:
                return True
        elif patch.slot == "count_max" and isinstance(patch.value, (int, float)):
            ids = S.group_task_ids(data, patch.index.get("组", {})) or []
            if patch.value < len(ids):
                return True
        elif patch.slot == "scope_only" and options:
            # 只有「圈定范围把某个任务整轮排除在候选之外」时才需要切部分排程。
            # 无条件切会让「只在窗口03 干」这类根本没排除任何任务的意图也丢任务——
            # 用户没说可以少修，多丢一个就是欺骗。
            group, error = S.scope_predicate(data, patch.index.get("范围", {}))
            if error:
                continue
            for tid, rows in options.items():
                if rows and not any(group(c) for c in rows):
                    return True
        elif patch.slot == "bucket_max" and patch.value == 0 and options:
            bucket, error = S.bucket_predicate(data, patch.index.get("桶", {}))
            group, gerror = S.group_predicate(data, patch.index.get("组", {}))
            if error or gerror:
                continue
            ids = S.group_task_ids(data, patch.index.get("组", {})) or []
            for tid in ids:
                rows = [c for c in options.get(tid, []) if group(c)]
                if not rows:
                    continue
                if all(bucket(c) for c in rows):
                    return True  # 该任务的所有候选都被压掉了，等于整轮不修
    return False


def compile_patches(patches: list[Patch], data, base_data) -> tuple[CompiledRequest, list[str]]:
    """编译。返回 (请求, 错误)；错误不为空就不该往下走。"""
    request = CompiledRequest()
    errors: list[str] = []
    must_do: list[str] = []

    for patch in patches:
        if patch.strength == "unsure":
            request.uncertain.append(patch.utterance or patch.slot)
        if patch.op == "reset":
            request.echo.append(f"「{patch.utterance or patch.slot}」按默认处理（不生效）")
            continue

        # 相对运算按基线折算
        baseline = None
        if patch.slot in S.PARAM_SLOTS:
            key = S.PARAM_ALIASES[patch.slot]
            baseline = getattr(base_data, "params", {}).get(key) if base_data is not None else None
        value, error = absolute_value(patch.op, patch.value, baseline)
        if error:
            errors.append(f"{patch.slot}：{error}")
            continue
        if patch.op != "=" and patch.slot in S.PARAM_SLOTS and value is not None:
            request.echo.append(
                f"参数「{patch.slot}」按基线 {baseline} {patch.op} {patch.value} 折算成 {value}")

        # ── A 组标量参数 ──
        if patch.slot in S.PARAM_SLOTS:
            request.overrides[S.PARAM_ALIASES[patch.slot]] = value
            request.echo.append(f"参数「{patch.slot}」改成 {value}")
            continue
        if patch.slot == "allow_partial":
            flag = _as_bool(value)
            if flag is None:
                errors.append(f"allow_partial：需要 是/否，收到 {value!r}")
                continue
            request.partial = request.partial or flag
            request.echo.append("允许少修几个" if flag else "要求全部排入")
            continue

        # ── B 组目标 ──
        if patch.slot == "strategy":
            request.strategy = str(value)
            # 目标模式默认「加权」：一阶段一次求解，分层（逐阶段锁等式、每阶段证明最优）
            # 是 5 分钟算不出来的主因。要绝对优先时用 count_min/count_max 硬约束表达，
            # 不靠目标分层（分层+加权见 agent_tools 的对比指令，仍有显式 mode 入口）。
            request.mode = request.mode or "加权"
            # 加权/分层都只是目标：允许跟成本折中，不构成「这一类必须修」的保证。
            # 不写清楚，用户会把「目标策略按高风险优先」当成高危已锁死。
            request.echo.append(
                f"目标策略按「{value}」（目标偏好，可与成本折中；不保证某一类任务一定排进去）")
            continue

        # ── G 组预算：现金/总成本走内核 limits ──
        if patch.slot == "budget_max":
            口径 = patch.index.get("口径", {}).get("value")
            group_spec = patch.index.get("组") or {"kind": "全部"}
            if 口径 in ("现金", "总成本") and (group_spec.get("kind") or "全部") != "全部":
                # 内核的 limits 只有一个全局口袋，分不了组。硬把「高危现金」记成整轮上限，
                # 就是「生效了但生效错了」——宁可拒绝并说清能改成什么。
                errors.append(
                    f"预算上限：{口径}只能按整轮设（内核只认一个总口袋），"
                    f"做不到只限「{S.describe_group(data, group_spec)}」的{口径}。"
                    "要按类别限，改用「人工」或「备件」口径")
                continue
            if 口径 == "现金":
                request.limits["现金预算"] = float(value)
            elif 口径 == "总成本":
                request.limits["最大总成本"] = float(value)
            request.echo.append(f"{口径}上限设为 {value}")
            continue

        # ── E 组期限：只走数据补丁（R2），且内核只做收紧 ──
        if patch.slot == "deadline_delta":
            minute = _to_minute(data, value)
            if minute is None:
                errors.append(f"deadline_delta：时刻 {value!r} 读不出来")
                continue
            ids = S.group_task_ids(data, patch.index.get("组", {})) or []
            tasks = {t["任务编号"]: t for t in data.tasks}
            affected = []
            for tid in ids:
                task = tasks.get(tid)
                if task is None:
                    continue
                current = task.get("截止分钟")
                # 内核的 task_deadline 只收紧：不比现有期限更早就不生效。
                if current is not None and minute >= current:
                    continue
                affected.append(tid)
            if not affected:
                scope = patch.index.get("组", {}).get("value", "这一批任务")
                errors.append(
                    f"期限 {value} 没有让任何「{scope}」的完工时间提前。"
                    "期限只能收紧（提前），放宽做不到——内核的期限补丁只做收紧，"
                    "要放宽得改源数据。")
                continue
            request.data_patches.append({"kind": "task_deadline", "at": data.stamp(minute)})
            request.echo.append(f"期限提前到 {data.stamp(minute)}，影响 {len(affected)} 个任务")
            continue

        # ── J 组数据补丁 ──
        if patch.slot == "part_hours":
            from dsl_core.registry import entities
            pid = entities.resolve_part(data, str(patch.index.get("部件", {}).get("value")))
            if pid is None:
                errors.append(f"part_hours：部件 {patch.index.get('部件', {}).get('value')!r} 不在部件库里")
                continue
            entry: dict[str, Any] = {"kind": "part_hours", "partId": pid}
            severity = patch.index.get("严重度", {}).get("value")
            sea = patch.index.get("海况", {}).get("value")
            if severity:
                entry["severity"] = severity
            if sea:
                entry["sea"] = sea
            entry["hours" if patch.op == "=" else "factor"] = float(value)
            request.data_patches.append(entry)
            request.echo.append(f"部件 {pid} 的维修工时改成 {value}"
                                + ("（倍率）" if patch.op != "=" else "（小时）"))
            continue
        if patch.slot == "part_arrival":
            from dsl_core.registry import entities
            pid = entities.resolve_part(data, str(patch.index.get("部件", {}).get("value")))
            minute = _to_minute(data, value)
            if pid is None or minute is None:
                errors.append("part_arrival：部件或到货时刻解析失败")
                continue
            request.data_patches.append({
                "kind": "part_arrival", "partId": pid, "at": data.stamp(minute),
                "quantity": int(patch.index.get("数量", 1)), "blockBefore": True,
            })
            request.echo.append(f"{pid} 备件 {data.stamp(minute)} 到货，此前不能开工")
            continue
        if patch.slot == "weather":
            day = patch.index.get("日期", {}).get("value")
            entry = {"kind": "weather", "day": str(day)}
            # 索引里的字段名决定改的是风速还是浪高
            field_name = patch.index.get("项", {}).get("value", "风速")
            entry["wind" if field_name == "风速" else "wave"] = float(value)
            request.timeline_patches.append(entry)
            request.echo.append(f"{day} 的{field_name}按 {value} 算")
            continue
        if patch.slot == "hourly":
            minute = _to_minute(data, patch.index.get("时刻", {}).get("value"))
            if minute is None:
                errors.append("hourly：时刻解析失败")
                continue
            entry = {"kind": "hourly", "time": data.stamp(minute)}
            field_name = patch.index.get("项", {}).get("value", "电价")
            entry["price" if field_name == "电价" else "wind"] = float(value)
            request.timeline_patches.append(entry)
            request.echo.append(f"{data.stamp(minute)} 的{field_name}按 {value} 算")
            continue
        if patch.slot == "window":
            wid = str(patch.index.get("窗口", {}).get("value"))
            entry = {"kind": "window", "window": wid}
            which = patch.index.get("端", {}).get("value", "开始")
            minute = _to_minute(data, value)
            if minute is None:
                errors.append("window：时刻解析失败")
                continue
            entry["start" if which == "开始" else "end"] = data.stamp(minute)
            request.timeline_patches.append(entry)
            request.echo.append(f"{wid} 的{which}改到 {data.stamp(minute)}")
            continue

        # ── H 组：涉及的任务隐含「都要修」，落进内核的必修名单 ──
        if patch.slot in ("before_seq", "same_bucket", "all_diff"):
            ids = patch.index.get("序列", {}).get("value") or \
                patch.index.get("集合", {}).get("value") or []
            for tid in ids:
                if tid not in must_do:
                    must_do.append(tid)
            request.echo.append(f"「{patch.utterance or patch.slot}」涉及 "
                                f"{len(ids)} 个任务，按「都要修」处理")
            continue

        # ── 其余走注入口的族：统一回显「按什么处理的」 ──
        # 强度回显是硬软消歧的落点（决策 D3）：不替用户猜，但要把判的结果摆出来让他能纠正。
        strength_word = {"hard": "硬性要求", "soft": "软性偏好", "unsure": "强度待确认"}[patch.strength]
        if patch.slot in ("count_min", "count_max") and patch.strength == "hard":
            # 硬约束写进可行域，目标函数无权拿它跟成本做交易；跟软性偏好区分开
            strength_word = "硬性要求·写进可行域，成本换不掉"
        cells = "，".join(
            f"{k}={v.get('value') if v.get('value') is not None else v.get('kind')}"
            for k, v in patch.index.items() if isinstance(v, dict)
        )
        request.echo.append(
            f"「{patch.utterance or patch.slot}」→ {patch.slot}"
            + (f"[{cells}]" if cells else "")
            + f" = {value}（{strength_word}）")

    if must_do:
        request.limits.setdefault("必修任务", []).extend(must_do)
    request.closure = build_closure(patches, data)
    return request, errors
