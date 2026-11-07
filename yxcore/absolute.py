"""绝对优先 → ``count_min`` 硬约束。加权目标只管相对偏好，管不了「必须全修」。

切到加权目标之后，``strategy=高风险优先抢修`` 只是目标函数里的一项，允许被成本项
换掉；它**不保证**任何一个高危任务真的排进去。用户说「高危必须全修」时，唯一的
诚实表达是硬约束 ``count_min[组=严重度/高] = 这一格的任务数``——写进可行域，
求解器无权拿它跟成本做交易。

这一层是**机械**的，不再让大模型判一次：

* 大模型负责听懂「必须/尽量」的语义并填补丁；
* 这里按原话复核**形状**：绝对优先的话有没有落到硬约束上、值是不是这一格的真实任务数。
  值从快照算，不采信模型写的 ``1``（把「全」写成「至少一个」，错误分类学 E4.3）
  或 ``999``（假数字）。
* 复核结果摆在回显里，不静默改：用户看得见「这句按硬约束处理了」。

为什么不放在 ``validate.py``：校验器只认补丁，不认「这句话该怎么翻译」。多轮里
手搓的补丁、脚本回归、评测 gold 都直接进校验器，给它们套一层「原话必须配硬约束」
会把一批本来合法的用例判死。形状复核只发生在**翻译出口**这一处（``parse.translate``），
也就是大模型产出落地之前。

两个出口：

* 语义明确（必须/一定/一个都不能少）→ 直接补齐/纠正硬约束，出方案；
* 语义含糊（同一小句里既有「最好」又有「全修」、或模型报 unsure）→ 出澄清问题组，
  两条候选都带**真补丁**，硬的那条排第一并标「推荐」。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from . import slots as S
from .patch import Patch

# 强标记：说了就是绝对优先，不能只落加权偏好。
_STRONG = re.compile(
    r"必须|一定|务必|不得(?:漏|少|落下)|不能(?:漏|少|落下)|不许(?:漏|少)"
    r"|一个(?:都|也)?(?:不(?:能|要|许)?(?:少|漏|落)|不落)"
    r"|全(?:部|都)?(?:要)?(?:修|排|在|完成|干完)|都得(?:修|排|在)|都要(?:修|排|在)"
)
# 「全」的口径：只有明确说了「全部/一个都不能少」才由程序补硬约束。
# 光有「必须」不够——「齿轮箱必须修」可能只指其中一件，机械地补成「所有齿轮箱
# 任务全修」是替用户把范围拧大了，比不补更危险。这一类交给大模型按原话判断。
_ALL_MARKER = re.compile(
    r"全(?:部|都)?(?:要)?(?:修|排|在|完成|干完)|全在|全都|所有.{0,4}都"
    r"|一个(?:都|也)?(?:不(?:能|要|许)?(?:少|漏|落)|不落)"
    r"|都得(?:修|排|在)|都要(?:修|排|在)"
)
# 软标记：只是相对偏好。同一小句里强弱同现才算含糊（例：「高危最好全修」）。
_SOFT = re.compile(r"尽量|尽可能|最好|争取|优先|先保|倾向|越.{0,6}越好|能修几个是几个|差不多")
# 明写了数字的要求（「高危四个必须全修」「至少修 3 个」）不该被「全」的口径覆盖。
_EXPLICIT_N = re.compile(
    r"[0-9０-９一二三四五六七八九十两]+\s*(?:个|项|台|条|件|艘)"
    r"|至少|最少|最多|不低于|不超过|前\s*[0-9]+"
)
# 带时段的话（「11月第二周必须修叶片」）要的是 bucket_min，不是整轮全修。
# 不避开就会凭「必须」补出一条「叶片整轮全修」的假硬约束。
_TIME_SCOPE = re.compile(
    r"\d{1,2}\s*月|\d{1,2}\s*[日号]|第[一二三四五六七八九十\d]+\s*周|窗口\s*\d+"
    r"|这周|本周|下周|上周|之前|以前|之内|以内|期间|当天|某日|下周之前"
)
# 「一个都不能少」里的「一个」是成语，不是数量词；查明写数字前先把它摘掉。
_ALL_PHRASE = re.compile(r"一个(?:都|也)?(?:不(?:能|要|许)?(?:少|漏|落)|不落)")
# 按小句判：让「成本压到最低，但高危必须全修」两句各走各的路。
_CLAUSE_SPLIT = re.compile(r"[，,；;。！!？?]|但(?:是)?|同时|而且|并且|另外|不过|然而|以及")

_SEVERITY: tuple[tuple[re.Pattern, dict[str, Any]], ...] = (
    (re.compile(r"高危|高风险|高严重|高等级|高告警|严重度.{0,3}高"),
     {"kind": "严重度", "value": "高"}),
    (re.compile(r"中危|中严重|严重度.{0,3}中"), {"kind": "严重度", "value": "中"}),
    (re.compile(r"低危|低严重|严重度.{0,3}低"), {"kind": "严重度", "value": "低"}),
)
_TASK = re.compile(r"任务\s*\d{2,4}")
_TURBINE = re.compile(r"风机\s*\d{2,4}")

ABSOLUTE, SOFT, AMBIGUOUS, NONE = "absolute", "soft", "ambiguous", "none"


@dataclass
class Enforced:
    """翻译出口的复核结果。``澄清`` 非空时调用方应当转去问人，不再出补丁。"""

    patches: list[Patch] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    clarify: list[dict] | None = None


def split_clauses(text: str) -> list[str]:
    """按连接词/标点切小句，丢掉空片段。"""
    return [part.strip() for part in _CLAUSE_SPLIT.split(text or "") if part.strip()]


def classify(clause: str) -> str:
    """这一小句是绝对优先、相对偏好、还是拿不准。"""
    hard = bool(_STRONG.search(clause or ""))
    soft = bool(_SOFT.search(clause or ""))
    if hard and soft:
        return AMBIGUOUS
    if hard:
        return ABSOLUTE
    if soft:
        return SOFT
    return NONE


def target_groups(clause: str, data, fallback: str = "") -> list[dict]:
    """这一小句点了哪一类任务。只认**精确**命中，匹配不上就不猜（沿用索引纪律）。

    强标记常常和组名不在同一小句（「高危优先，一个都不能少」），所以给了
    ``fallback``：本小句找不到组时，去整句话里找一次。
    """
    for text in (clause, fallback or ""):
        found: list[dict] = []
        for pattern, spec in _SEVERITY:
            if pattern.search(text):
                found.append(dict(spec))
        if found:
            return found
        task = _TASK.search(text)
        if task:
            from dsl_core.registry import entities  # noqa: PLC0415

            tid = entities.resolve_task(data, task.group().replace(" ", ""))
            if tid:
                return [{"kind": "任务", "value": tid}]
        turbine = _TURBINE.search(text)
        if turbine:
            from dsl_core.registry import entities  # noqa: PLC0415

            wid = entities.resolve_turbine(data, turbine.group().replace(" ", ""))
            if wid:
                return [{"kind": "风机", "value": wid}]
        for name in _part_names(data):
            if name and name in text:
                return [{"kind": "部件", "value": name}]
        if re.search(r"全部任务|所有任务|所有告警|全都修|全部都", text):
            return [{"kind": "全部"}]
    return []


def _part_names(data) -> list[str]:
    names = [p.get("部件名称") for p in (getattr(data, "parts", None) or {}).values()]
    # 长名先匹配，免得「叶片」把「叶片备件」截胡
    return sorted({str(n) for n in names if n}, key=len, reverse=True)


def group_size(spec: dict, data, options: dict[str, list[dict]] | None = None) -> int:
    """这一格里**排得进去**几个任务：没候选的不算（与 validate._count_planable 同源）。"""
    ids = S.group_task_ids(data, spec) or []
    if options is None:
        return len(ids)
    return sum(1 for tid in ids if options.get(tid))


def hard_patch(spec: dict, count: int, utterance: str) -> Patch:
    """「这一类必须全修」的唯一诚实形状：硬性 count_min，值取快照。"""
    return Patch(slot="count_min", index={"组": dict(spec)}, op="=", value=int(count),
                 strength="hard", utterance=utterance)


def _same_group(patch: Patch, spec: dict) -> bool:
    group = (patch.index or {}).get("组") or {}
    return (group.get("kind") or "全部") == (spec.get("kind") or "全部") \
        and group.get("value") == spec.get("value")


def _soft_strategy() -> str:
    return "高风险优先抢修" if "高风险优先抢修" in S.STRATEGY_NAMES \
        else (S.STRATEGY_NAMES[0] if S.STRATEGY_NAMES else "成本最低优先")


def _clarify_group(utterance: str, spec: dict, count: int, patches: list[Patch]) -> dict:
    """含糊语义的两条候选，都带能直接落地的补丁；硬约束排第一并标推荐。"""
    who = S.describe_group(None, spec)
    hard = hard_patch(spec, count, utterance)
    # 别的 knob（预算、上限…）两条候选都留着，别因为问一句话把用户别的要求弄丢
    rest = [p for p in patches if not (p.slot == "count_min" and _same_group(p, spec))]
    soft = list(rest)
    # 已经有 strategy 就别再加一条（同格两条会撞 D2）；没有才给那条纯偏好
    if not any(p.slot == "strategy" for p in soft):
        soft.append(Patch(slot="strategy", index={}, op="=", value=_soft_strategy(),
                          strength="soft", utterance=utterance))
    return {
        "问": (f"「{utterance}」里的「{who}」是必须全部修到，还是只是相对偏好？"
               f"硬约束会锁死这 {count} 个任务；加权偏好只影响排序，成本一高就可能漏修。"),
        "候选": [
            {"意图": f"{who} {count} 个必须全修",
             "性质": "硬约束", "推荐": True, "patches": [hard] + rest},
            {"意图": f"只是尽量先修{who}",
             "性质": "相对偏好", "推荐": False, "patches": soft},
        ],
    }


def enrich_clarification(utterance: str, groups: list[dict], data,
                         options: dict[str, list[dict]] | None = None) -> list[dict]:
    """大模型自己回了澄清时，也得保证「硬约束」这条候选在选项里。

    否则模型一拿不准就只给两条软解释，用户点哪个都拿不到硬约束——绝对优先
    在澄清这一步被悄悄消掉了。
    """
    groups = [dict(g) for g in (groups or [])]
    plans = _plans(utterance, data, options)
    if not plans or not groups:
        return groups
    for plan in plans:
        spec, count = plan["组"], plan["数量"]
        first = groups[0]
        cands = list(first.get("候选") or [])
        has_hard = any(
            any(p.slot == "count_min" and _same_group(p, spec) and int(p.value or 0) >= count
                for p in (c.get("patches") or []))
            for c in cands
        )
        if has_hard:
            continue
        injected = _clarify_group(utterance, spec, count, [])["候选"][0]
        cands.insert(0, injected)
        first["候选"] = cands
        first["问"] = first.get("问") or \
            f"「{utterance}」是必须全部修到，还是只是相对偏好？"
    return groups


def _plans(utterance: str, data, options) -> list[dict]:
    """这句话里哪些格子被要求「绝对优先全修」。按小句判，明写数字的不算。"""
    clauses = split_clauses(utterance)
    plans: list[dict] = []
    seen: set[str] = set()
    for clause in clauses:
        kind = classify(clause)
        if kind not in (ABSOLUTE, AMBIGUOUS) or not _ALL_MARKER.search(clause) \
                or _EXPLICIT_N.search(_ALL_PHRASE.sub("", clause)) \
                or _TIME_SCOPE.search(clause):
            continue
        for spec in target_groups(clause, data, fallback=utterance):
            key = f"{spec.get('kind')}:{spec.get('value')}"
            if key in seen:
                continue
            seen.add(key)
            count = group_size(spec, data, options)
            if count <= 0:
                continue
            plans.append({"组": spec, "数量": count, "语义": kind, "小句": clause})
    return plans


def enforce(utterance: str, patches: list[Patch], data,
            options: dict[str, list[dict]] | None = None) -> Enforced:
    """翻译出口的形状复核。返回 (补丁, 回显提示, 澄清问题组)。

    只做三件事，全都能机械判：
      1. 绝对优先却没落 ``count_min`` → 补一条硬约束；
      2. ``count_min`` 的值小于这一格的真实任务数 → 按快照改正（不采信模型写的 1）；
      3. 强度被标成软 → 改回硬性要求。
    语义含糊（强弱同现）或模型报 unsure 时不替用户猜，改出澄清问题组。
    """
    result = Enforced(patches=list(patches or []))
    plans = _plans(utterance, data, options)
    if not plans:
        return result

    # 「拿不准」按宪法第六节第 3 条处理：问一句，不猜默认值。
    unsure = any(p.strength == "unsure" for p in result.patches)
    for plan in plans:
        if plan["语义"] == AMBIGUOUS or unsure:
            group = _clarify_group(utterance, plan["组"], plan["数量"], result.patches)
            result.clarify = (result.clarify or []) + [group]
    if result.clarify:
        return result

    for plan in plans:
        spec, count = plan["组"], plan["数量"]
        who = S.describe_group(data, spec)
        hit = next((i for i, p in enumerate(result.patches)
                    if p.slot == "count_min" and p.op != "reset" and _same_group(p, spec)), None)
        if hit is None:
            result.patches.append(hard_patch(spec, count, utterance))
            result.notes.append(
                f"「{utterance}」是绝对优先：已补硬约束「{who}至少修 {count} 个」"
                f"（count_min）。加权目标只管相对偏好，不能替代它")
            continue
        patch = result.patches[hit]
        value = int(patch.value) if isinstance(patch.value, (int, float)) else None
        changed = False
        new_value, new_strength = patch.value, patch.strength
        if value is not None and value < count:
            new_value, changed = count, True
            result.notes.append(
                f"「{utterance}」写的是「{who}至少修 {value} 个」，但这一格现在有 "
                f"{count} 个任务；按「全修」改成硬约束至少 {count} 个")
        if patch.strength != "hard":
            new_strength, changed = "hard", True
            result.notes.append(
                f"「{utterance}」的硬约束被标成了{patch.strength}；绝对优先按硬性要求处理")
        if changed:
            result.patches[hit] = Patch(slot=patch.slot, index=patch.index, op=patch.op,
                                        value=new_value, strength=new_strength,
                                        utterance=patch.utterance or utterance)
    return result
