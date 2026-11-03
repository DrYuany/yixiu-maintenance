"""校验器：6 条规则，全部是查表和计数，不解析任何表达式。

这是降级换来的最大便宜。大一修的校验器要验线性性、量纲、实体存在、原语合法；
小一修的族是冻结的，所以只要查「槽位在册 / 索引在册 / 数值在区间」，再加上一条
大一修根本做不到的：**静态不可行预检**——因为族固定，能在跑求解器之前就把一批
「必然做不到」拦下来，而且理由带数字。

规则清单：
  1. 槽位在册（不在册要给出最近的替代旋钮）
  2. 索引值在册（实体存在性，精确匹配，不猜）
  3. 数值类型 / 单位 / 区间
  4. 静态不可行预检（只报算得出来的，算不出来不吭声）
  5. 槽位冲突（同一任务既必修又排除等）
  6. 同一格子被写两次（决策 D2：报错，不累乘）
"""
from __future__ import annotations

import json

from typing import Any

from . import slots as S
from .patch import Patch, duplicate_cells

# 需要「任务是必修还是排除」的槽位，用于第 5 条冲突检查
_FORCE_IN = ("count_min",)
_FORCE_OUT = ("count_max", "bucket_max")


def _nearest_slot(name: str) -> str | None:
    """给一个不在册的槽位找最近的替代旋钮——告诉用户能拧什么，比只说「没有」有用。"""
    import difflib

    candidates = list(S.SLOTS) + list(S.PARAM_SLOTS)
    match = difflib.get_close_matches(name, candidates, n=1, cutoff=0.4)
    return match[0] if match else None


def _task_ids_of_group(data, spec: dict[str, Any]) -> list[str] | None:
    """组里有哪些任务；组本身解析不出来时返回 None（**不是空列表**）。

    两者必须分开：空列表是「这一类现在没有任务」，None 是「这一格没读懂」。
    混在一起就会在「部件名写错」时报「这一格最多只有 0 个」，把用户引到错的地方。
    """
    return S.group_task_ids(data, spec)


def _count_planable(data, options: dict[str, list[dict]], task_ids: list[str], bucket_spec=None,
                    allowed: set[str] | None = None) -> int | None:
    """这一格里最多能修几个：有候选、且（给了桶的话）候选能落进桶里的任务数。

    这是上界，不是「一定会修这么多」。报静态不可行时用它当理由的数字来源。

    ``allowed`` 是别的补丁已经圈死的范围（目前只有 ``scope_only``）。不交叠就会
    出现「只修叶片 + 至少修 3 个」这种校验放过、求解爆炸的组合——两个要求分开看都合理，
    合起来只有 2 个任务可用。

    返回 None 表示**算不出来**（谓词解析失败），调用方必须闭嘴：
    把「解析失败」说成「这一格只有 0 个任务」是更坏的错——用户会去改一个没问题的数。
    """
    bucket = None
    if bucket_spec:
        bucket, error = S.bucket_predicate(data, bucket_spec)
        if error:
            return None
    total = 0
    for tid in task_ids:
        if allowed is not None and tid not in allowed:
            continue
        rows = options.get(tid) or []
        if bucket is None:
            if rows:
                total += 1
        elif any(bucket(c) for c in rows):
            total += 1
    return total


def _check_value(patch: Patch, slot: S.Slot, data) -> list[str]:
    """第 3 条：数值类型 / 单位 / 区间。"""
    errors: list[str] = []
    value = patch.value
    if slot.value_type == "minutes" and patch.op in ("*", "+", "-"):
        # 时刻类给相对量必然算错单位：「推迟两天」会被当成 +2 分钟。
        errors.append(f"{slot.label}：时刻只能直接给具体日期时间"
                      f"（{patch.op} 这种相对量算不出「天」这个单位）。"
                      "「推迟两天」请自己算成 11月17号这样的日期")
        return errors
    if slot.value_type == "act":
        # 激活类：这一族只需要「点亮」，值可以省略。
        if value is not None and not (value is True or value in ("是", "启用", 1)):
            errors.append(f"{slot.label}：这是开关型旋钮，不需要数值，收到 {value!r}")
        return errors
    if value is None:
        errors.append(f"{slot.label}：需要一个值")
        return errors
    if slot.value_type == "bool":
        if not isinstance(value, bool) and value not in ("是", "否", "允许", "不允许"):
            errors.append(f"{slot.label}：需要 是/否，收到 {value!r}")
        return errors
    if slot.value_type == "enum":
        allowed = S.STRATEGY_NAMES
        if value not in allowed:
            errors.append(f"{slot.label}：{value!r} 不在可选项里，能填的是：{'、'.join(allowed)}")
        return errors
    if slot.value_type == "minutes":
        if isinstance(value, str):
            try:
                minute = data.minute(value)
            except (ValueError, AttributeError):
                errors.append(f"{slot.label}：时刻 {value!r} 读不出来，给一个规划期内的时间")
                return errors
            if not 0 <= minute <= data.end:
                errors.append(f"{slot.label}：时刻 {value!r} 落在规划范围之外")
            return errors
        if not isinstance(value, (int, float)):
            errors.append(f"{slot.label}：需要数字或时刻字符串，收到 {value!r}")
            return errors
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        errors.append(f"{slot.label}：需要数字，收到 {value!r}")
        return errors
    if slot.bounds:
        lo, hi = slot.bounds
        if not lo <= float(value) <= hi:
            errors.append(f"{slot.label}：{value} 超出允许范围 [{lo}, {hi}]")
    return errors


def _check_index(data, patch: Patch, slot: S.Slot) -> list[str]:
    """第 2 条：索引值在册。按索引种类分派到对应的解析器，精确匹配。"""
    errors: list[str] = []
    declared = dict(slot.index)
    for key, spec in patch.index.items():
        if key not in declared:
            errors.append(f"{patch.slot}：没有 {key} 这个索引维度，能填的是："
                          + "、".join(declared) if declared else f"{patch.slot}：不接受索引")
            continue
        kind = declared[key]
        if not isinstance(spec, dict):
            errors.append(f"{patch.slot} 的 {key}：需要 {{kind, value}} 形式，收到 {spec!r}")
            continue
        if kind == "group":
            _, error = S.group_predicate(data, spec)
        elif kind == "group_or_bucket":
            _, error = S.scope_predicate(data, spec)
        elif kind == "bucket":
            _, error = S.bucket_predicate(data, spec)
        elif kind == "task":
            from dsl_core.registry import entities
            error = None if entities.resolve_task(data, str(spec.get("value"))) else \
                f"任务 {spec.get('value')!r} 不在告警快照里"
        elif kind in ("task_seq", "task_set"):
            from dsl_core.registry import entities
            ids = spec.get("value")
            if not isinstance(ids, (list, tuple)) or len(ids) < 2:
                error = f"需要至少两个任务编号，收到 {ids!r}"
            else:
                missing = [v for v in ids if entities.resolve_task(data, str(v)) is None]
                error = f"任务 {missing} 不在告警快照里" if missing else None
        elif kind == "severity_pair":
            pair = spec.get("value")
            error = None if (isinstance(pair, (list, tuple)) and len(pair) == 2
                             and all(p in ("高", "中", "低") for p in pair)) else \
                f"严重度层对需要两个层级（高/中/低），收到 {pair!r}"
        elif kind == "cap_维度":
            error = None if spec.get("value") in S.CAP_DIMENSIONS else \
                f"容量维度 {spec.get('value')!r} 未登记，能填：{'、'.join(S.CAP_DIMENSIONS)}"
        elif kind == "budget_口径":
            error = None if spec.get("value") in S.BUDGET_KINDS else \
                f"预算口径 {spec.get('value')!r} 未登记，能填：{'、'.join(S.BUDGET_KINDS)}"
        elif kind in ("severity", "sea"):
            # part_hours 的可选子维度；kind 用英文登记，但模型常照提示词写中文，两种都收
            enum_key = "严重度" if kind == "severity" else "海况"
            allowed = S.OPTIONAL_ENUMS[enum_key]
            error = None if spec.get("value") in allowed else \
                f"{enum_key} 只能填 {'、'.join(allowed)}，收到 {spec.get('value')!r}"
        elif kind in S.OPTIONAL_ENUMS:
            allowed = S.OPTIONAL_ENUMS[kind]
            error = None if spec.get("value") in allowed else \
                f"{kind} 只能填 {'、'.join(allowed)}，收到 {spec.get('value')!r}"
        elif kind in ("part", "window", "day", "moment", "strategy"):
            error = None  # 数据类索引在落地（apply）时用内核自身的校验再挡一次
        else:
            error = f"索引种类 {kind} 没有解析器"
        if error:
            errors.append(f"{patch.slot} 的 {key}：{error}")
    for key in declared:
        if key in slot.optional:
            continue  # 可省略：省略即该维全改/取默认，已在槽位说明里写死
        if key not in patch.index and slot.route not in ("data", "timeline"):
            errors.append(f"{patch.slot}：缺索引 {key}（{declared[key]}）")
    return errors


def _scope_allowed(patches: list[Patch], data, options: dict[str, list[dict]]) -> set[str] | None:
    """别的补丁已经把宇宙缩小到哪几件事了（目前只有 scope_only 会缩）。

    静态预检必须在这个缩小后的宇宙里数数，否则「只修叶片 + 至少修 3 个」
    会被判成合法：分开看都合理，合起来叶片只有 2 个任务。
    """
    if not options:
        return None  # 没有候选就算不出交集；宁可不算，也不能误报"一个都修不成"
    allowed: set[str] | None = None
    for patch in patches:
        if patch.slot != "scope_only" or patch.op == "reset":
            continue
        pred, error = S.scope_predicate(data, patch.index.get("范围") or {"kind": "全部"})
        if error or pred is None:
            continue
        keep = {tid for tid, rows in options.items() if any(pred(c) for c in rows)}
        allowed = keep if allowed is None else (allowed & keep)
    return allowed


def _check_static(patch: Patch, data, options: dict[str, list[dict]],
                  allowed: set[str] | None = None) -> list[str]:
    """第 4 条：静态不可行预检——只报算得出来的，算不出来就闭嘴。"""
    errors: list[str] = []
    value = patch.value
    ids = _task_ids_of_group(data, patch.index.get("组", {}))
    if ids is None and patch.slot in ("count_min", "count_max"):
        return errors  # 这一格没读懂，交给第 2 条报，别再补一句误导的数字
    if patch.slot == "count_min":
        able = _count_planable(data, options, ids or [], allowed=allowed)
        if able is not None and isinstance(value, (int, float)) and value > able:
            errors.append(f"要求至少修 {int(value)} 个，但这一格里最多只有 {able} 个任务排得进去"
                          + ("（已扣除被「只修」圈掉的部分）" if allowed is not None else ""))
    elif patch.slot == "bucket_min":
        able = _count_planable(data, options, ids or [], patch.index.get("桶"), allowed=allowed)
        if able is not None and isinstance(value, (int, float)) and value > able:
            bucket = patch.index.get("桶", {}).get("value", "该时段")
            errors.append(f"{bucket} 内要求至少开工 {int(value)} 个，"
                          f"但排得进去的只有 {able} 个（按开工时间计）")
    elif patch.slot == "count_max":
        if isinstance(value, (int, float)) and value == 0 and patch.strength == "hard" \
                and ids is not None and len(ids) == len(data.tasks):
            errors.append("把所有任务都排除了，这次排程一个都修不成")
    elif patch.slot == "same_bucket":
        ids = _task_ids_of_group(data, {"kind": "任务清单", "value": patch.index.get("集合", {}).get("value")}) or []
        day_sets = []
        for tid in ids:
            rows = options.get(tid) or []
            days = {c["开始"] // 1440 for c in rows}
            if not days:
                errors.append(f"任务 {tid} 在规划期内没有任何可开工的候选")
                day_sets.append(set())
            else:
                day_sets.append(days)
        if day_sets and not set.intersection(*day_sets):
            errors.append("这几个任务没有共同的可用日期，安排不到同一天")
    elif patch.slot == "all_diff":
        ids = _task_ids_of_group(data, {"kind": "任务清单", "value": patch.index.get("集合", {}).get("value")}) or []
        day_sets = [{c["开始"] // 1440 for c in (options.get(tid) or [])} for tid in ids]
        if day_sets and any(not d for d in day_sets):
            errors.append("集合里有任务在规划期内没有任何可开工的候选")
        elif day_sets:
            union = set().union(*day_sets)
            if len(union) < len(ids):
                errors.append(f"要分在不同天，但这些任务总共只落在 {len(union)} 天里，"
                              f"装不下 {len(ids)} 个")
    elif patch.slot == "before_seq":
        seq = patch.index.get("序列", {}).get("value") or []
        if len(seq) != len(set(seq)):
            errors.append(f"顺序里有重复任务：{'、'.join(seq)}")
    elif patch.slot == "cap_max":
        dim = patch.index.get("维度", {}).get("value")
        if dim == "作业线" and isinstance(value, (int, float)):
            lines = min(data.params["最多作业线数"], data.params["人员总上限"])
            if value > lines:
                errors.append(f"上限 {int(value)} 超过当前可用作业线数 {lines}，"
                              "这条约束不会生效；先去改「最多作业线数」")
    return errors


def _check_cap_zero(patch: Patch) -> str | None:
    """cap_max 拧到 0 的护栏：那是「那条船/那台风机一个都不许用」，不是「全部停工」。

    「2 号船别排」被误译成 cap_max[作业线, 某日]=0 时，约束实际变成「那天一条船都不派」——
    过度封锁且不报错，是典型的"生效了但生效错了"。这里明确说出来。
    """
    if patch.slot != "cap_max" or patch.value != 0:
        return None
    dim = patch.index.get("维度", {}).get("value")
    bucket = patch.index.get("桶", {}).get("value", "全程")
    return (f"容量上限拧到 0 等于「{bucket} 一条{dim or '实例'}都不许用」。"
            f"如果只是某一条{dim or '实例'}不派，请用 bucket_max（组=作业线编号）")


def _check_conflicts(patches: list[Patch], data) -> list[str]:
    """第 5 条：槽位冲突——同一批任务既被钉死必修又被钉死不修。"""
    errors: list[str] = []
    forced_out: set[str] = set()
    forced_in: set[str] = set()
    for patch in patches:
        spec = patch.index.get("组", {}) or patch.index.get("范围", {})
        if patch.slot == "count_min" and isinstance(patch.value, (int, float)) and patch.value >= 1:
            if spec.get("kind") == "任务":
                forced_in.add(str(spec.get("value")))
            elif spec.get("kind") == "任务清单":
                forced_in.update(str(v) for v in spec.get("value", []))
        if patch.slot in ("count_max", "bucket_max") and patch.value == 0:
            if spec.get("kind") == "任务":
                forced_out.add(str(spec.get("value")))
            elif spec.get("kind") == "任务清单":
                forced_out.update(str(v) for v in spec.get("value", []))
        if patch.slot == "scope_only" and spec.get("kind") == "任务清单":
            keep = {str(v) for v in spec.get("value", [])}
            forced_out.update(
                t["任务编号"] for t in data.tasks if t["任务编号"] not in keep
            )
    # 同一格既「至少 N」又「最多 M」且 N > M：不用跑求解器就知道做不到。
    # 分开看两条都合理（高危全修 / 高危最多 2 个），合起来自相矛盾。
    floors: dict[str, int] = {}
    ceils: dict[str, int] = {}
    labels: dict[str, str] = {}
    for patch in patches:
        if patch.op == "reset" or not isinstance(patch.value, (int, float)):
            continue
        if patch.slot in ("count_min", "bucket_min"):
            table = floors
        elif patch.slot in ("count_max", "bucket_max"):
            table = ceils
        else:
            continue
        key = patch.slot.rsplit("_", 1)[0] + json.dumps(
            [patch.index.get("组"), patch.index.get("桶")], sort_keys=True, ensure_ascii=False)
        table[key] = int(patch.value)
        if key not in labels:
            bucket = (patch.index.get("桶") or {}).get("value")
            who = S.describe_group(data, patch.index.get("组"))
            labels[key] = f"{bucket} 内{who}" if bucket else who
    for key, low in floors.items():
        high = ceils.get(key)
        if high is not None and low > high:
            errors.append(f"「{labels.get(key, '同一格')}」既要求至少 {low} 个、又要求最多 {high} 个，"
                          "这两条自相矛盾")

    both = sorted(forced_in & forced_out)
    if both:
        errors.append("这些任务既被要求必须修、又被要求不修：" + "、".join(both))

    # 同一对任务既要求同一天又要求不同天
    pairs: dict[frozenset[str], set[str]] = {}
    for patch in patches:
        if patch.slot in ("same_bucket", "all_diff"):
            ids = patch.index.get("集合", {}).get("value") or []
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    pairs.setdefault(frozenset({str(ids[i]), str(ids[j])}), set()).add(patch.slot)
    for pair, kinds in pairs.items():
        if len(kinds) > 1:
            errors.append("这两个任务既要求同一天、又要求不同天：" + "、".join(sorted(pair)))
    return errors


def validate(
    patches: list[Patch], data, options: dict[str, list[dict]] | None = None
) -> tuple[list[str], list[str]]:
    """返回 (错误, 告警)。错误拦下不出方案，告警照做但要回显。

    告警目前只有一类：硬软同时点亮。按决策 D3 不报错——判错的代价两边不对称，
    该硬做成软是欺骗，该软做成硬只是坏体验，所以交给回显和用户确认，不替用户猜。
    """
    errors: list[str] = []
    warnings: list[str] = []
    options = options or {}

    dups = duplicate_cells(patches)
    if dups:
        errors.append("同一格子被写了两次（同一需求请合并成一个补丁）：" + "；".join(dups))

    # 别的补丁可能已经把可选范围缩小了（只修叶片…），静态预检要在这个范围里数
    allowed = _scope_allowed(patches, data, options)
    if allowed is not None and not allowed:
        # 两条「只修」叠起来没有交集，等于一条都不许修。多轮里这最常发生在
        # 「改成只修齿轮箱」没把上一轮的「只修叶片」撤掉的时候。
        errors.append("几条「只修」的限制叠在一起后没有任何任务剩下，这次一个都修不成；"
                      "若你是想换一个范围，请先把上一条撤掉（reset）")

    for patch in patches:
        if patch.slot in S.PARAM_SLOTS:
            spec = S.PARAM_SLOTS[patch.slot]
            value = patch.value
            if spec["类型"] == "bool" and not isinstance(value, bool) and value not in ("是", "否"):
                errors.append(f"参数「{patch.slot}」需要 是/否，收到 {value!r}")
            elif spec["类型"] in ("int", "float") and not isinstance(value, (int, float)):
                errors.append(f"参数「{patch.slot}」需要数字，收到 {value!r}")
            elif spec["界"] and isinstance(value, (int, float)):
                lo, hi = spec["界"]
                if not lo <= float(value) <= hi:
                    errors.append(f"参数「{patch.slot}」的 {value} 超出范围 [{lo}, {hi}]")
            continue
        slot = S.SLOTS.get(patch.slot)
        if slot is None:
            near = _nearest_slot(patch.slot)
            hint = f"，最接近的旋钮是「{near}」" if near else ""
            errors.append(f"没有「{patch.slot}」这个旋钮{hint}")
            continue
        errors.extend(_check_index(data, patch, slot))
        if patch.op != "reset":
            errors.extend(_check_value(patch, slot, data))
        errors.extend(_check_static(patch, data, options, allowed))

    errors.extend(_check_conflicts(patches, data))

    for patch in patches:
        hint = _check_cap_zero(patch)
        if hint:
            warnings.append(hint)

    unsure = [p for p in patches if p.strength == "unsure"]
    if unsure:
        warnings.append("这些话的强度没判准，需要向用户确认：" +
                        "；".join(p.utterance or p.slot for p in unsure))
    return errors, warnings


def validate_or_raise(patches: list[Patch], data, options: dict[str, list[dict]] | None = None
                      ) -> list[str]:
    """校验并抛出。返回告警清单（调用方负责回显）。"""
    from .patch import PatchError

    errors, warnings = validate(patches, data, options)
    if errors:
        raise PatchError(errors)
    return warnings


def registry_self_check() -> list[str]:
    return S.audit_registry()
