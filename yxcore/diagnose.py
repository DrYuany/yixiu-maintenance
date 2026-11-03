"""不可行诊断：说清是哪一格拧过头了，而且理由要带数字。

这是小一修相对大一修的**新增能力**：族是冻结的，所以能在跑求解器之前就把一批
「必然做不到」拦下来（静态预检，见 validate）；真要跑求解器才发现排不出时，也能逐条
归因——因为补丁只有几十个、每一条的形状都预写过，可以一条一条试。

纪律（沿用 dsl_core/diagnose 的）：**只报算得出来的**。算不出来就明说"未能定位"，
不拿"可能冲突"糊过去。
"""
from __future__ import annotations

import time
from typing import Any

from . import slots as S
from .patch import Patch
from .validate import _count_planable

# 单条归因的时限：只要能证明「这一条单独就排不出」，不需要最优解
PROBE_SECONDS = 5

# 诊断探针的总预算。探针条数是 单条 n + 两两 C(n,2)，每条最坏跑满主问题时限：
# 6 条补丁、40 秒时限 = 840 秒，一条卡死的需求能把整次调用拖到十几分钟。
# 超预算就停手并**说清楚还有几对没试**——不许装作「都试过了」。
PROBE_TOTAL_SECONDS = 240


def _budget_note(untried_singles: list[str], untried_pairs: int) -> str:
    """探针预算用完时，把「没试完」说清楚——没试过不等于没问题。"""
    parts = []
    if untried_singles:
        parts.append(f"{len(untried_singles)} 条单独没试（{'、'.join(untried_singles)}）")
    if untried_pairs:
        parts.append(f"{untried_pairs} 对组合没试")
    return ("诊断探针到了时间预算（%d 秒），还有 %s——这几处没查过，"
            "不能当成「没问题」。" % (PROBE_TOTAL_SECONDS, "；".join(parts)))


def _capacity_hint(patch: Patch, data, options) -> str | None:
    """这一格现在最多能排几个——能算出来就给数字，算不出来不吭声。"""
    if patch.slot in ("count_min", "count_max"):
        ids = S.group_task_ids(data, patch.index.get("组", {}))
        able = _count_planable(data, options, ids, allowed=None) if ids is not None else None
        if able is None:
            return None
        return f"这一格里最多能排 {able} 个（当前要求 {patch.value}）"
    if patch.slot in ("bucket_min", "bucket_max"):
        ids = S.group_task_ids(data, patch.index.get("组", {}))
        able = _count_planable(data, options, ids, patch.index.get("桶")) if ids is not None else None
        if able is None:
            return None
        label = patch.index.get("桶", {}).get("value", "全程")
        return f"{label} 内最多能排 {able} 个（当前要求 {patch.value}）"
    if patch.slot == "cap_max":
        dim = patch.index.get("维度", {}).get("value")
        if dim == "作业线":
            lines = min(data.params["最多作业线数"], data.params["人员总上限"])
            return f"当前一共只有 {lines} 条作业线（要求上限 {patch.value}）"
    return None


def _describe_patch(patch: Patch, data) -> str:
    """把补丁说成一句话：哪个旋钮、哪一格、拧到几。

    光靠原话归因不够——原话可能没写、也可能说的是另一件事；归因必须自带格子信息，
    否则用户看不出要松开哪一格。
    """
    slot = S.SLOTS.get(patch.slot) or S.PARAM_SLOTS.get(patch.slot)
    label = slot.label if isinstance(slot, S.Slot) else f"参数「{patch.slot}」"
    cells = []
    for key, spec in patch.index.items():
        if key in ("组", "范围"):
            cells.append(S.describe_group(data, spec))
            continue
        if not isinstance(spec, dict):
            cells.append(str(spec))
            continue
        value = spec.get("value")
        joiner = "→" if key == "层对" else "、"  # 顺序用箭头，集合用顿号
        cells.append(f"{key}={joiner.join(str(v) for v in value)}"
                     if isinstance(value, (list, tuple)) else f"{key}={value}")
    cell = "·".join(cells)
    if patch.op == "reset":
        tail = "拧回默认"
    elif patch.value is None:
        tail = "点亮"
    else:
        tail = {"=": f"={patch.value}", "*": f"×{patch.value}",
                "+": f"+{patch.value}", "-": f"−{patch.value}"}.get(patch.op, str(patch.value))
    return f"{label}（{cell}）{tail}" if cell else f"{label} {tail}"


def _label(patch: Patch, data) -> str:
    return f"「{patch.utterance}」（{_describe_patch(patch, data)}）" if patch.utterance else \
        f"「{_describe_patch(patch, data)}」"


def _static_reason(patch: Patch, data, options) -> str | None:
    """静态算得出来的矛盾：不用跑求解器就能判的。"""
    if patch.slot == "count_min" and isinstance(patch.value, (int, float)):
        ids = S.group_task_ids(data, patch.index.get("组", {}))
        able = _count_planable(data, options, ids) if ids is not None else None
        if able is not None and patch.value > able:
            return f"要求至少 {int(patch.value)} 个，但排得进去的只有 {able} 个"
    if patch.slot == "bucket_min" and isinstance(patch.value, (int, float)):
        ids = S.group_task_ids(data, patch.index.get("组", {}))
        able = _count_planable(data, options, ids, patch.index.get("桶")) if ids is not None else None
        if able is not None and patch.value > able:
            label = patch.index.get("桶", {}).get("value", "该时段")
            return f"{label} 内要求 {int(patch.value)} 个，但排得进去的只有 {able} 个"
    if patch.slot == "count_max" and patch.value == 0:
        ids = S.group_task_ids(data, patch.index.get("组", {}))
        if ids is not None and len(ids) == len(data.tasks):
            return "把所有任务都排除了，这次一个都修不成"
    return None


def _fallback_hint(patches: list[Patch], data, base_id: str, seconds: int) -> str | None:
    """「做不到全部」后面必须跟一句「能做多少」——否则用户只知道被拒了。

    只在原本不是部分排程时试（已经是部分排程还排不出，就是别的要求太紧）。
    试不出来就不吭声：给一个「也许可以」的空头支票比不说更糟。
    """
    if any(p.slot == "allow_partial" and p.op != "reset" for p in patches):
        return None
    from .pipeline import run

    flag = Patch(slot="allow_partial", value=True, op="=", strength="hard",
                 utterance="允许少修几个")
    try:
        result = run(patches + [flag], base_id=base_id, seconds=seconds, _diagnose=False)
    except Exception:  # noqa: BLE001 - 退路试不出来就不提
        return None
    done = len(result["排程"] or [])
    total = len(data.tasks)
    if done in (0, total):
        return None
    return (f"给个退路：如果允许少修几个，这组要求能排出 {done}/{total} 个"
            f"（少 {total - done} 个）。把最紧的那条改成「尽量」就行。")


def explain_infeasible(patches: list[Patch], data, options, info: dict[str, Any],
                       request, seconds: int = PROBE_SECONDS) -> str:
    """给出不可行的原因，再补一句退路。返回一段说给用户听的话。"""
    text = _explain(patches, data, options, info, request, seconds)
    if str(info.get("解状态") or "") and "INFEASIBLE" in str(info.get("解状态") or ""):
        base_id = (data.meta or {}).get("场景") or "基座01"
        hint = _fallback_hint(patches, data, base_id, max(int(seconds), PROBE_SECONDS))
        if hint:
            text = text + "\n" + hint
    return text


def _explain(patches: list[Patch], data, options, info: dict[str, Any],
             request, seconds: int = PROBE_SECONDS) -> str:
    status = str(info.get("解状态") or "")
    head = f"这组旋钮排不出方案（求解器判定：{status or '未给出状态'}）"

    # 求解器没证明不可行（超时/未知）就不能说"做不到"——那是两件事。
    if status and "INFEASIBLE" not in status:
        return (f"没能在时限内找到可行方案（{status}）。这不等于一定排不出来，"
                "可以加长时限再试一次，或者先松开最紧的那条要求。")

    lines = [head]

    # 第一层：不用跑求解器就能定罪的
    static = []
    for patch in patches:
        reason = _static_reason(patch, data, options)
        if reason:
            static.append(f"{_label(patch, data)}：{reason}")
    if static:
        lines.append("直接能看出来的矛盾：")
        lines.extend("  * " + item for item in static)
        return "\n".join(lines)

    # 第二层：逐条单跑，找出「单独就排不出」的那条
    from .pipeline import YxError, run  # 延迟导入，避免循环

    base_id = (data.meta or {}).get("场景") or "基座01"
    culprits: list[str] = []
    uncertain: list[str] = []
    broken: list[str] = []
    # 单条探针不能比主问题更短：主问题给 30 秒、探针只给 5 秒，
    # 探针会因为没跑完而"证明"一条其实没问题的要求做不到（此前就这样误判过）。
    probe_seconds = max(int(seconds), PROBE_SECONDS)
    started = time.monotonic()
    untried_singles: list[str] = []
    untried_pairs = 0          # 只有补丁 ≥2 条才会用到，但结论里要看它，先初始化
    for patch in patches:
        if time.monotonic() - started >= PROBE_TOTAL_SECONDS:
            untried_singles.append(_label(patch, data))
            continue
        try:
            run([patch], base_id=base_id, seconds=probe_seconds, _diagnose=False)
        except YxError as exc:
            hint = _capacity_hint(patch, data, options)
            if exc.code == "不可行":
                culprits.append(f"{_label(patch, data)} 单独就要不到方案"
                                + (f"；{hint}" if hint else ""))
            elif exc.code == "未证不可行":
                uncertain.append(f"{_label(patch, data)} 单独跑也没在时限内出解"
                                 f"（不等于做不到，只是没跑完）")
            elif exc.code in ("对账", "复核"):
                broken.append(f"{_label(patch, data)} 单独能排出方案，但产物自己对不上"
                              f"：{str(exc)[:120]}")
            # 校验/编译类不会走到这里：那一步在进求解器之前就拦掉了
        except Exception as exc:  # noqa: BLE001 - 探针不许把主流程带崩
            uncertain.append(f"{_label(patch, data)} 探针异常：{str(exc)[:100]}")
    if culprits:
        lines.append("逐条查了一遍，这几条单独就排不出：")
        lines.extend("  * " + item for item in culprits)
        lines.append("先松开这几条里的任意一条，再排一次。")
        if uncertain:
            lines.append("另外这几条没跑完，不能算在它们头上：")
            lines.extend("  * " + item for item in uncertain)
        if untried_singles:
            lines.append(_budget_note(untried_singles, 0))
        return "\n".join(lines)
    if broken:
        lines.append("这几条单独排出来的方案自己对不上账（不是排不出，是产物不一致）：")
        lines.extend("  * " + item for item in broken)
        if untried_singles:
            lines.append(_budget_note(untried_singles, 0))
        return "\n".join(lines)

    # 第三层：两两组合。单条都行，但两条放一起就排不出——这一层能直接点名是哪一对。
    # 组合数按族数算很小（C(6,2)=15），但每条都要跑一次求解，所以封顶 6 条。
    if 2 <= len(patches) <= 6:
        clashes: list[str] = []
        untried_pairs = 0
        for i in range(len(patches)):
            for j in range(i + 1, len(patches)):
                if time.monotonic() - started >= PROBE_TOTAL_SECONDS:
                    untried_pairs += 1
                    continue
                pair = [patches[i], patches[j]]
                try:
                    run(pair, base_id=base_id, seconds=probe_seconds, _diagnose=False)
                except YxError as exc:
                    if exc.code == "不可行":
                        clashes.append(f"{_label(patches[i], data)} + {_label(patches[j], data)}")
                except Exception:  # noqa: BLE001 - 探针异常不算冲突证据
                    continue
        if clashes:
            lines.append("两两试下来，这两条放一起就排不出（单独都没问题）：")
            lines.extend("  * " + item for item in clashes[:5])
            lines.append("这两条里松开任意一条，剩下的就能排。")
            if uncertain:
                lines.append("提醒：这几条单独跑没跑完，结论可能受其影响：")
                lines.extend("  * " + item for item in uncertain)
            if untried_singles or untried_pairs:
                lines.append(_budget_note(untried_singles, untried_pairs))
            return "\n".join(lines)

    # 第四层：组合冲突，但两两都能排（三条以上互相咬）。诚实说，不编造
    if untried_singles or untried_pairs:
        if len(untried_singles) == len(patches):
            lines.append("探针一条都没跑成（预算不够），所以哪条咬住都还没查。")
        else:
            lines.append("试过的那部分都没查出错，但**没试完**——剩下的还没排除，"
                         "所以不能断言是三条以上互相咬。")
    elif len(patches) < 2:
        lines.append("这条要求自己单独就跑不出方案。先松开它，或者把时限加长再试一次。")
    elif uncertain:
        lines.append("单条和两两组合里，凡是跑完的都没出现不可行；但还有几条没跑完，"
                     "所以到底是两条咬住还是三条以上咬住，这次没能说清。")
    else:
        lines.append("每一条单独都能排出来，任意两条也能排，是三条以上合在一起咬住了。")
    hints = [h for h in (_capacity_hint(p, data, options) for p in patches) if h]
    if hints:
        lines.append("各条的余量：")
        lines.extend("  * " + h for h in hints)
    lines.append("可以把最紧的那条先松开试试。")
    if uncertain:
        lines.append("提醒：这几条单独跑没跑完，冲突可能出在它们身上：")
        lines.extend("  * " + item for item in uncertain)
    if untried_singles or untried_pairs:
        lines.append(_budget_note(untried_singles, untried_pairs))
    return "\n".join(lines)
