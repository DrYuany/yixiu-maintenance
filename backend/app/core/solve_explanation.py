"""求解结果的原因说明：有解没证最优、不可行、没算完，都要说清「为什么」。

判分口径和 ``dsl_core/diagnose.py`` 一致：只用求解器记录里真实存在的字段说话，
不猜、不写「可能是工时不够」这类补白；算不出来就明说没能定位。

``explain_not_optimal``  有可行方案但状态是 FEASIBLE 时用。以前回复只有一句
    「尚未证明最优」，用户不知道是时间用尽、容差内主动停、决胜没跑完，还是自己取消了。
    决胜段已整块删除，当前求解器不会再产生 TIE_NOT_PROVEN；相关分支仅为
    「小一修/runs/ 里的历史归档产物」保留，查旧方案时仍能说清当年为什么没证完。
``explain_no_solution``  没产出方案时的兜底。优先级永远低于 diagnose 算出来的诊断，
    只在诊断缺席时顶上，避免又退回一句空话。
"""
from __future__ import annotations

from typing import Any


def _names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None and str(item).strip()]


def _num(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _budget(solver: dict[str, Any]) -> dict[str, Any]:
    raw = solver.get("预算")
    return raw if isinstance(raw, dict) else {}


def explain_not_optimal(solver: dict[str, Any] | None) -> str:
    """有可行方案、但没证明最优时，说明卡在哪一步。

    按「先排除人为取消 → 再说哪一阶段因为什么停下」的顺序，避免把
    「容差内主动停止」和「时间用尽被迫停止」混成一句。
    """
    data = solver or {}
    reason = str(data.get("终止原因") or "").strip()
    unproven = _names(data.get("未达最优阶段"))
    tolerant = _names(data.get("容差内阶段"))
    gap = _num(data.get("最大相对差距"))
    gap_limit = _num(_budget(data).get("gapLimit"))

    if reason == "USER_CANCELLED":
        return "未证明最优的原因：求解被取消，这是取消前已经找到的最好方案。"

    parts: list[str] = []
    if tolerant:
        limit = f"{gap_limit:.0%}" if gap_limit else "设定容差"
        parts.append(f"「{'、'.join(tolerant)}」在 {limit} 容差内提前停止，属于允许的近似")

    if reason == "STAGE_TIME_LIMIT" and unproven:
        parts.append(f"「{'、'.join(unproven)}」用满了分到的时间，没来得及证明最优")
    elif reason == "TIME_LIMIT" and unproven:
        parts.append(f"整体时间用尽，「{'、'.join(unproven)}」没证明最优")
    elif reason in ("TIME_LIMIT", "TIE_NOT_PROVEN") and data.get("确定性决胜完成") is False:
        if reason == "TIME_LIMIT":
            parts.append("目标阶段都算完了，但收尾的确定性决胜没跑完（整场求解时限用尽）")
        else:
            parts.append(
                "目标阶段都算完了，但收尾的确定性决胜没证到最优；"
                "时间尚未用尽，是这一段自己没算到最优"
            )
    elif unproven:
        parts.append(f"还有 {len(unproven)} 段没证明最优（{'、'.join(unproven[:2])}）")

    if gap is not None and gap > 0:
        parts.append(f"未证最优的阶段里，离证明最优最多还差 {gap:.1%}")

    if not parts:
        parts.append("求解器给出了可行解，但这次没有证明它是最优的")

    return "未证明最优的原因：" + "；".join(parts) + "。"


def explain_no_solution(status: str, solver: dict[str, Any] | None) -> str:
    """没产出方案时的兜底理由（诊断模块没给出结论时用）。

    UNKNOWN 和 INFEASIBLE 必须分开：把「算不完」讲成「无解」，用户会白改一通
    本来没问题的约束。
    """
    data = solver or {}
    reason = str(data.get("终止原因") or "").strip()
    elapsed = _num(data.get("总耗时秒"))
    seconds = _num(_budget(data).get("seconds"))
    if seconds is None:
        params = data.get("求解器参数")
        seconds = _num(params.get("max_time_in_seconds")) if isinstance(params, dict) else None

    if status == "UNKNOWN" or reason in ("TIME_LIMIT", "STAGE_TIME_LIMIT"):
        used = f"用时 {elapsed:.1f} 秒" if elapsed is not None else "用时未知"
        limit = f"，时限 {seconds:.0f} 秒" if seconds is not None else ""
        return (
            f"求解器在时限内既没给出解、也未证明无解（{used}{limit}）；"
            "这属于「没算完」，不是「不可行」。放宽求解时限再跑一次，才能区分两者。"
        )
    if status == "INFEASIBLE":
        return (
            "求解器已经证明：当前约束下没有可行排程。"
            "系统没能定位到具体是哪条约束造成的，建议逐条去掉一条约束重跑来定位。"
        )
    if reason == "USER_CANCELLED":
        return "求解被取消，没有产出排程。"
    return f"求解器返回状态 {status or '未知'}，系统没能进一步定位原因。"
