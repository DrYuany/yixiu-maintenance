from __future__ import annotations

import hashlib
import json
from typing import Any

from app.schemas.agent import CandidateWindow, WindowVersion


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _solver(version: WindowVersion) -> dict[str, Any]:
    return _dict((version.resultSummary or {}).get("solver"))


def _status(version: WindowVersion) -> str:
    value = (
        _solver(version).get("解状态")
        or _solver(version).get("求解状态")
        or _solver(version).get("状态")
    )
    return str(value) if value else "UNKNOWN"


def _money(value: Any) -> float | None:
    try:
        return round(float(value) / 10_000, 2)
    except (TypeError, ValueError):
        return None


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_window(version: WindowVersion) -> CandidateWindow | None:
    windows = version.windows or []
    return windows[0] if windows else None


def _window_range(version: WindowVersion) -> dict[str, str | None]:
    window = _first_window(version)
    if not window:
        return {"id": None, "start": None, "end": None}
    return {
        "id": window.windowId,
        "start": window.metrics.startTime,
        "end": window.metrics.endTime,
    }


def _schedule_map(version: WindowVersion) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in version.repairSchedule or []:
        result[item.taskId] = {
            "taskId": item.taskId,
            "turbineId": item.turbineId,
            "componentName": item.componentName,
            "startTime": item.startTime,
            "endTime": item.endTime,
            "durationHours": item.durationHours,
        }
    return result


def _unscheduled_map(version: WindowVersion) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in _list((version.resultSummary or {}).get("unscheduledTasks")):
        raw = _dict(item)
        task_id = raw.get("任务编号") or raw.get("taskId")
        if not task_id:
            continue
        result[str(task_id)] = raw
    return result


def _data_snapshot(version: WindowVersion, artifact: dict[str, Any] | None) -> dict[str, Any]:
    if artifact:
        return _dict(artifact.get("dataSnapshot"))
    summary = _dict((version.resultSummary or {}).get("data"))
    return {
        "asOf": (
            summary.get("规划时点")
            or summary.get("数据时点")
            or summary.get("asOf")
        ),
        "inputHash": summary.get("输入哈希") or summary.get("inputHash"),
    }


def _artifact_parameters(artifact: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not artifact:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for item in _list(artifact.get("parameters")):
        raw = _dict(item)
        key = str(raw.get("id") or raw.get("label") or "")
        if key:
            result[key] = raw
    return result


def _artifact_stages(artifact: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not artifact:
        return []
    return [
        _dict(item)
        for item in _list(_dict(artifact.get("objectives")).get("stages"))
    ]


def _artifact_constraints(artifact: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not artifact:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for item in _list(_dict(artifact.get("constraints")).get("groups")):
        raw = _dict(item)
        kind = str(raw.get("kind") or raw.get("id") or "")
        if kind:
            result[kind] = raw
    return result


# ── 收敛状态：差值能不能当结论，取决于两侧算到什么程度 ─────────────────────
#
# 曾经这里只比大小：242.52 与 243.17 差 0.65 万，就写"方案02 更贵"。
# 可两版都是 FEASIBLE，各自离自己的最优还差 1%~3%，0.65 万完全落在
# 这个不确定区间里——那不是结论，是求解器停在哪里的偶然。
# 现在把每版的收敛情况摊开，用区间判断：只有两个区间不重叠才敢说谁更省。

_COST_STAGE = "总经济成本"


def _stage_rows(version: WindowVersion) -> list[dict[str, Any]]:
    return [_dict(item) for item in _list(_solver(version).get("阶段"))]


def _budget(version: WindowVersion) -> dict[str, Any]:
    solver = _solver(version)
    budget = _dict(solver.get("预算"))
    params = _dict(solver.get("求解器参数"))
    seconds = _number(budget.get("seconds"))
    if seconds is None:
        seconds = _number(params.get("max_time_in_seconds"))
    gap = _number(budget.get("gapLimit"))
    if gap is None:
        gap = _number(params.get("relative_gap_limit"))
    return {
        "seconds": seconds,
        "gapLimit": gap,
        "secondsSource": budget.get("secondsSource"),
        "gapSource": budget.get("gapSource"),
    }


def _cost_gap(version: WindowVersion) -> float | None:
    """成本阶段自己的相对差距；没有这一阶段就退回全场最大差距。"""
    for stage in _stage_rows(version):
        if str(stage.get("目标") or "") == _COST_STAGE:
            return _number(stage.get("相对差距"))
    return _number(_solver(version).get("最大相对差距"))


def _cost_gap_metric(version: WindowVersion) -> str:
    """这个差距到底属于哪个目标，别把「加权目标最优」说成「成本最优」。

    分层模式的求解日志里除了业务阶段还混着「确定性决胜」条目，那些只是打破平局、
    不是业务目标，数阶段时得先把它们摘出去。加权模式已经不产生这些条目。
    """
    stages = [
        item
        for item in _stage_rows(version)
        if not str(item.get("目标") or "").startswith("确定性决胜")
    ]
    if any(str(item.get("目标") or "") == _COST_STAGE for item in stages):
        return _COST_STAGE
    if len(stages) == 1:
        return str(stages[0].get("目标") or "本版目标")
    return "本版目标"


def convergence_of(version: WindowVersion) -> dict[str, Any]:
    solver = _solver(version)
    stages = _stage_rows(version)
    planned = _number(solver.get("计划阶段数"))
    executed = _number(solver.get("执行阶段数"))
    if planned is None:
        planned = len(stages)
    if executed is None:
        executed = len([item for item in stages if item.get("状态") in ("OPTIMAL", "FEASIBLE")])
    optimal = len([item for item in stages if item.get("状态") == "OPTIMAL"])
    status = _status(version)
    gap = _cost_gap(version)
    return {
        "status": status,
        "quality": solver.get("最优性") or ("已证明最优" if status == "OPTIMAL" else "未证明最优"),
        "progress": solver.get("分层进度") or f"{int(executed)}/{int(planned)}",
        "stagesPlanned": int(planned),
        "stagesExecuted": int(executed),
        "stagesOptimal": optimal,
        "stagesWithinTolerance": _list(solver.get("容差内阶段")),
        "stagesUnproven": _list(solver.get("未达最优阶段")),
        "costGap": gap,
        "costGapMetric": _cost_gap_metric(version),
        "maxRelativeGap": _number(solver.get("最大相对差距")),
        "stopReason": solver.get("终止原因"),
        "elapsedSeconds": _number(solver.get("总耗时秒")),
        "budgetSeconds": _budget(version).get("seconds"),
        "gapLimit": _budget(version).get("gapLimit"),
    }


def _cost_range(version: WindowVersion, convergence: dict[str, Any]) -> dict[str, Any] | None:
    """成本的可能区间（万元）。

    最小化问题里：当前解 best 是上界，最优不可能比 best 更差；
    相对差距 g 意味着最优值不低于 best*(1-g)。所以区间是
    [best*(1-g), best]，不是 [best, best*(1+g)]。
    """
    best = _number((version.resultSummary or {}).get("totalCost"))
    if best is None:
        return None
    best_wan = round(best / 10_000, 2)
    status = convergence.get("status")
    gap = convergence.get("costGap")
    if status == "OPTIMAL":
        gap = 0.0
    if gap is None:
        return {"best": best_wan, "gap": None, "low": None, "high": best_wan, "proven": False}
    gap = max(0.0, float(gap))
    return {
        "best": best_wan,
        "gap": round(gap, 6),
        "low": round(best_wan * (1 - gap), 2),
        "high": best_wan,
        "proven": gap == 0.0,
    }


def _cost_decision(
    left: dict[str, Any] | None,
    right: dict[str, Any] | None,
    *,
    comparable: bool,
) -> dict[str, Any]:
    if not comparable:
        return {"certainty": "incomparable", "cheaper": None, "reason": "两次运行的数据或任务集合不同，成本不可比。"}
    if not left or not right:
        return {"certainty": "incomparable", "cheaper": None, "reason": "至少一侧缺少总成本。"}
    if left.get("gap") is None or right.get("gap") is None:
        return {
            "certainty": "unverified",
            "cheaper": None,
            "reason": "至少一侧没有记录收敛状态，成本差值未核实，不能当结论。",
        }
    if left["proven"] and right["proven"]:
        if left["best"] == right["best"]:
            return {"certainty": "proven", "cheaper": "tie", "reason": "两侧都证明最优，成本相同。"}
        cheaper = "right" if right["best"] < left["best"] else "left"
        return {
            "certainty": "proven",
            "cheaper": cheaper,
            "reason": "两侧都证明最优，成本差值就是结论。",
        }
    if left["high"] < right["low"]:
        return {
            "certainty": "beyondTolerance",
            "cheaper": "left",
            "reason": "左侧最差情况仍低于右侧最好情况，差值超出两侧求解容差。",
        }
    if right["high"] < left["low"]:
        return {
            "certainty": "beyondTolerance",
            "cheaper": "right",
            "reason": "右侧最差情况仍低于左侧最好情况，差值超出两侧求解容差。",
        }
    return {
        "certainty": "inconclusive",
        "cheaper": None,
        "reason": (
            "两侧都没证明最优，成本区间有重叠：差值落在求解容差之内，"
            "不能据此判定谁更省。"
        ),
    }


def _metric(
    key: str,
    label: str,
    left: Any,
    right: Any,
    unit: str | None = None,
    better: str | None = None,
) -> dict[str, Any]:
    delta: float | None = None
    pct: float | None = None
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        delta = round(float(right) - float(left), 2)
        if float(left) != 0:
            pct = round((float(right) - float(left)) / abs(float(left)) * 100, 2)
    winner: str | None = None
    if better and delta is not None:
        higher = right > left
        lower = right < left
        if not higher and not lower:
            winner = "tie"
        elif better == "lower":
            winner = "right" if lower else "left"
        elif better == "higher":
            winner = "right" if higher else "left"
    return {
        "key": key,
        "label": label,
        "left": left,
        "right": right,
        "unit": unit,
        "delta": delta,
        "deltaPct": pct,
        "winner": winner,
    }


def _task_rows(
    task_ids: set[str],
    schedule: dict[str, dict[str, Any]],
    other_unscheduled: dict[str, dict[str, Any]],
    limit: int = 12,
) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    for task_id in sorted(task_ids):
        item = dict(schedule.get(task_id) or {"taskId": task_id})
        if task_id in other_unscheduled:
            other = other_unscheduled[task_id]
            item["otherReason"] = other.get("原因") or other.get("reason")
            item["otherStatus"] = other.get("任务状态") or other.get("status")
        rows.append(item)
    return rows[:limit], len(rows)


def _changed_parameters(
    left: dict[str, dict[str, Any]],
    right: dict[str, dict[str, Any]],
    limit: int = 20,
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for key in sorted(set(left) | set(right)):
        before = left.get(key)
        after = right.get(key)
        before_value = before.get("value") if before else None
        after_value = after.get("value") if after else None
        if before_value == after_value and bool(before) == bool(after):
            continue
        changes.append(
            {
                "id": key,
                "label": (after or before or {}).get("label") or key,
                "unit": (after or before or {}).get("unit"),
                "left": before_value,
                "right": after_value,
                "source": (after or before or {}).get("source"),
                "asOf": (after or before or {}).get("asOf"),
            }
        )
    return changes[:limit]


def _changed_stages(
    left: list[dict[str, Any]],
    right: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for index in range(max(len(left), len(right))):
        before = left[index] if index < len(left) else {}
        after = right[index] if index < len(right) else {}
        signature_before = (
            before.get("name"),
            before.get("sense"),
            before.get("expressionTermCount"),
        )
        signature_after = (
            after.get("name"),
            after.get("sense"),
            after.get("expressionTermCount"),
        )
        if signature_before == signature_after:
            continue
        changes.append(
            {
                "priority": after.get("priority") or before.get("priority") or index + 1,
                "left": {
                    "name": before.get("name"),
                    "sense": before.get("sense"),
                    "terms": before.get("expressionTermCount"),
                },
                "right": {
                    "name": after.get("name"),
                    "sense": after.get("sense"),
                    "terms": after.get("expressionTermCount"),
                },
            }
        )
    return changes


def _changed_constraints(
    left: dict[str, dict[str, Any]],
    right: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for kind in sorted(set(left) | set(right)):
        before = _number((left.get(kind) or {}).get("count")) or 0
        after = _number((right.get(kind) or {}).get("count")) or 0
        if before == after:
            continue
        changes.append(
            {
                "kind": kind,
                "meaning": (right.get(kind) or left.get(kind) or {}).get("meaning"),
                "left": int(before),
                "right": int(after),
            }
        )
    return changes


def _comparison_id(left: WindowVersion, right: WindowVersion) -> str:
    raw = f"{left.id}|{right.id}|{left.modelFingerprint}|{right.modelFingerprint}"
    return "cmp_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def compare_versions(
    left: WindowVersion,
    right: WindowVersion,
    *,
    left_number: int,
    right_number: int,
    left_artifact: dict[str, Any] | None = None,
    right_artifact: dict[str, Any] | None = None,
) -> dict[str, Any]:
    left_summary = left.resultSummary or {}
    right_summary = right.resultSummary or {}
    left_schedule = _schedule_map(left)
    right_schedule = _schedule_map(right)
    left_unscheduled = _unscheduled_map(left)
    right_unscheduled = _unscheduled_map(right)
    left_snapshot = _data_snapshot(left, left_artifact)
    right_snapshot = _data_snapshot(right, right_artifact)

    left_tasks = set(left_schedule)
    right_tasks = set(right_schedule)
    only_left = left_tasks - right_tasks
    only_right = right_tasks - left_tasks
    moved = {
        task_id
        for task_id in left_tasks & right_tasks
        if left_schedule[task_id].get("startTime") != right_schedule[task_id].get("startTime")
        or left_schedule[task_id].get("endTime") != right_schedule[task_id].get("endTime")
    }

    left_stages = _artifact_stages(left_artifact)
    right_stages = _artifact_stages(right_artifact)
    left_parameters = _artifact_parameters(left_artifact)
    right_parameters = _artifact_parameters(right_artifact)
    left_constraints = _artifact_constraints(left_artifact)
    right_constraints = _artifact_constraints(right_artifact)
    left_solver = _solver(left)
    right_solver = _solver(right)
    left_conv = convergence_of(left)
    right_conv = convergence_of(right)

    same_data = bool(
        left_snapshot.get("inputHash")
        and right_snapshot.get("inputHash")
        and left_snapshot.get("inputHash") == right_snapshot.get("inputHash")
    )
    if not left_snapshot.get("inputHash") or not right_snapshot.get("inputHash"):
        same_data = bool(
            left_snapshot.get("asOf")
            and right_snapshot.get("asOf")
            and left_snapshot.get("asOf") == right_snapshot.get("asOf")
        )
    same_universe = left_summary.get("totalTaskCount") == right_summary.get("totalTaskCount")
    same_objectives = [item.get("name") for item in left_stages] == [
        item.get("name") for item in right_stages
    ]
    same_model = bool(
        left.modelFingerprint
        and right.modelFingerprint
        and left.modelFingerprint == right.modelFingerprint
    )
    cost_comparable = same_data and same_universe
    left_range = _cost_range(left, left_conv)
    right_range = _cost_range(right, right_conv)
    # 「差值能不能当结论」比「成本数字能不能并列」更严：两次完成的任务集合
    # 不同，服务量就不等价，即使两侧区间不重叠也不能说谁更省。
    cost_decision = _cost_decision(
        left_range, right_range, comparable=cost_comparable and left_tasks == right_tasks
    )
    # 只有差值真的超出两侧容差（或两侧都证明最优）才允许说"省了多少"。
    savings_allowed = bool(
        cost_comparable
        and left_tasks == right_tasks
        and cost_decision["certainty"] in ("proven", "beyondTolerance")
        and cost_decision["cheaper"] in ("left", "right")
    )

    notes: list[str] = []
    if not same_data:
        notes.append("两次运行的数据快照不同，成本差异不能全部归因于参数或目标。")
    if not same_universe:
        notes.append("两次运行的待修任务总数不同，完成量不能直接横比。")
    if left_tasks != right_tasks:
        notes.append("两次完成的任务集合不同，不能宣称同等服务下节省。")
    if same_model:
        notes.append("两次运行的模型指纹相同；结论应一致，差异更可能来自求解时间或随机性。")
    if not left_artifact or not right_artifact:
        notes.append("至少一侧缺少完整模型产物，参数与约束差异只能部分核对。")
    if cost_decision["certainty"] == "inconclusive":
        notes.append(cost_decision["reason"])
    elif cost_decision["certainty"] == "unverified":
        notes.append(cost_decision["reason"])

    total_left = _money(left_summary.get("totalCost"))
    total_right = _money(right_summary.get("totalCost"))
    cash_left = _money(left_summary.get("cashCost"))
    cash_right = _money(right_summary.get("cashCost"))
    loss_left = _money(left_summary.get("generationLoss"))
    loss_right = _money(right_summary.get("generationLoss"))
    tasks_left = int(left_summary.get("scheduledTaskCount") or len(left_tasks))
    tasks_right = int(right_summary.get("scheduledTaskCount") or len(right_tasks))
    vessel_left = int(left_summary.get("vesselDays") or 0)
    vessel_right = int(right_summary.get("vesselDays") or 0)

    left_window = _window_range(left)
    right_window = _window_range(right)
    window_metric = _metric(
        "windowStart",
        "窗口开始",
        left_window.get("start"),
        right_window.get("start"),
    )
    metrics = [
        {
            "key": "window",
            "label": "建议窗口",
            "left": left_window,
            "right": right_window,
            "changed": left_window != right_window,
        },
        _metric("totalCost", "总成本", total_left, total_right, "万元", "lower"),
        _metric("cashCost", "现金支出", cash_left, cash_right, "万元", "lower"),
        _metric("generationLoss", "停机损失", loss_left, loss_right, "万元", "lower"),
        {
            "key": "scheduledTasks",
            "label": "完成任务",
            "left": {"completed": tasks_left, "total": left_summary.get("totalTaskCount")},
            "right": {"completed": tasks_right, "total": right_summary.get("totalTaskCount")},
            "unit": "项",
            "delta": tasks_right - tasks_left,
            "winner": (
                "tie"
                if tasks_right == tasks_left
                else "right" if tasks_right > tasks_left else "left"
            ),
        },
        _metric("vesselDays", "船舶艘天", vessel_left, vessel_right, "艘天", "lower"),
        _metric(
            "latestCompletion",
            "最晚完工",
            left_summary.get("latestCompletion"),
            right_summary.get("latestCompletion"),
        ),
        _metric(
            "solverStatus",
            "求解状态",
            _status(left),
            _status(right),
        ),
    ]

    left_only_rows, left_only_total = _task_rows(
        only_left, left_schedule, right_unscheduled
    )
    right_only_rows, right_only_total = _task_rows(
        only_right, right_schedule, left_unscheduled
    )
    moved_rows = [
        {
            "taskId": task_id,
            "left": {
                "startTime": left_schedule[task_id].get("startTime"),
                "endTime": left_schedule[task_id].get("endTime"),
            },
            "right": {
                "startTime": right_schedule[task_id].get("startTime"),
                "endTime": right_schedule[task_id].get("endTime"),
            },
        }
        for task_id in sorted(moved)[:12]
    ]

    layer1 = {
        "left": {
            "id": left.id,
            "planNumber": left_number,
            "label": f"方案{left_number:02d}",
            "generatedAt": left.generatedAt,
            "prompt": left.userPrompt,
            "strategy": left_summary.get("strategy"),
            "objectiveMode": left_summary.get("objectiveMode"),
            "status": _status(left),
            "verified": left_summary.get("verified"),
            "modelFingerprint": left.modelFingerprint,
            "convergence": left_conv,
        },
        "right": {
            "id": right.id,
            "planNumber": right_number,
            "label": f"方案{right_number:02d}",
            "generatedAt": right.generatedAt,
            "prompt": right.userPrompt,
            "strategy": right_summary.get("strategy"),
            "objectiveMode": right_summary.get("objectiveMode"),
            "status": _status(right),
            "verified": right_summary.get("verified"),
            "modelFingerprint": right.modelFingerprint,
            "convergence": right_conv,
        },
        "comparability": {
            "sameDataSnapshot": same_data,
            "sameTaskUniverse": same_universe,
            "sameObjectiveStructure": same_objectives,
            "sameModelFingerprint": same_model,
            "costComparable": cost_comparable,
            "savingsClaimAllowed": savings_allowed,
            # 差值本身可不可信，和"两侧数据是否可比"是两件事：
            # 数据一致但都没算到最优，照样不能下结论。
            "costCertainty": cost_decision["certainty"],
            "costConclusion": cost_decision["reason"],
            "costRange": {"left": left_range, "right": right_range},
            "notes": notes,
        },
        "metrics": metrics,
        "windowChange": {
            "changed": left_window != right_window,
            "left": left_window,
            "right": right_window,
            "startMetric": window_metric,
        },
        "quickVerdict": {
            "cheaper": cost_decision["cheaper"],
            "cheaperCertainty": cost_decision["certainty"],
            "moreTasks": (
                "tie" if tasks_left == tasks_right else "right" if tasks_right > tasks_left else "left"
            ),
            "fewerVesselDays": (
                "tie"
                if vessel_left == vessel_right
                else "right" if vessel_right < vessel_left else "left"
            ),
        },
    }

    layer2 = {
        "taskSet": {
            "onlyLeft": left_only_rows,
            "onlyLeftCount": left_only_total,
            "onlyRight": right_only_rows,
            "onlyRightCount": right_only_total,
            "bothCount": len(left_tasks & right_tasks),
            "moved": moved_rows,
            "movedCount": len(moved),
        },
        "costDrivers": {
            "totalCost": _metric("totalCost", "总成本", total_left, total_right, "万元", "lower"),
            "cashCost": _metric("cashCost", "现金支出", cash_left, cash_right, "万元", "lower"),
            "generationLoss": _metric(
                "generationLoss", "停机损失", loss_left, loss_right, "万元", "lower"
            ),
        },
        "parameterChanges": _changed_parameters(left_parameters, right_parameters),
        "objectiveChanges": _changed_stages(left_stages, right_stages),
        "constraintChanges": _changed_constraints(left_constraints, right_constraints),
        "solver": {
            "left": {
                "status": _status(left),
                "elapsedSeconds": left_solver.get("总耗时秒") or left_solver.get("总耗时"),
                "parameters": left_solver.get("求解器参数"),
                "stopReason": left_solver.get("终止原因"),
                "verified": left_summary.get("verified"),
                "convergence": left_conv,
            },
            "right": {
                "status": _status(right),
                "elapsedSeconds": right_solver.get("总耗时秒") or right_solver.get("总耗时"),
                "parameters": right_solver.get("求解器参数"),
                "stopReason": right_solver.get("终止原因"),
                "verified": right_summary.get("verified"),
                "convergence": right_conv,
            },
            "costDecision": cost_decision,
        },
        "baselines": {
            "left": left_summary.get("baselineSavings"),
            "right": right_summary.get("baselineSavings"),
        },
    }

    layer3 = {
        "drilldown": {
            "versionIds": [left.id, right.id],
            "readTool": "read_versions",
            "taskDetail": "read_versions(versions=[...], include=[\"tasks\", \"unscheduled\"])",
            "modelMath": "read_versions(versions=[...], include=[\"model\"])",
            "note": "逐任务、逐约束或原始模型需要时再单独读取；本对比只带有限条目。",
        },
        "artifactAvailability": {
            "left": bool(left_artifact),
            "right": bool(right_artifact),
        },
        "counts": {
            "leftTasks": len(left_tasks),
            "rightTasks": len(right_tasks),
            "leftUnscheduled": len(left_unscheduled),
            "rightUnscheduled": len(right_unscheduled),
        },
    }

    payload = {
        "schema": "version-compare-v1",
        "comparisonId": _comparison_id(left, right),
        "layer1Facts": layer1,
        "layer2Drivers": layer2,
        "layer3Index": layer3,
    }
    payload["payloadBytes"] = len(
        json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    )
    return payload


def render_compare_summary(payload: dict[str, Any]) -> str:
    layer1 = _dict(payload.get("layer1Facts"))
    layer2 = _dict(payload.get("layer2Drivers"))
    left = _dict(layer1.get("left"))
    right = _dict(layer1.get("right"))
    metrics = {
        item.get("key"): item
        for item in _list(layer1.get("metrics"))
        if isinstance(item, dict)
    }

    def value(key: str, side: str, unit: str = "") -> str:
        item = _dict(metrics.get(key))
        raw = item.get(side)
        if isinstance(raw, dict):
            if "completed" in raw:
                text = f"{raw.get('completed')}/{raw.get('total') or '?'}"
            elif "start" in raw:
                text = f"{raw.get('start')} 至 {raw.get('end')}"
            else:
                text = "未记录"
        elif raw is None:
            text = "未记录"
        else:
            text = str(raw)
        return f"{text} {unit}".strip()

    def delta(key: str, unit: str = "") -> str:
        item = _dict(metrics.get(key))
        raw = item.get("delta")
        if raw is None:
            return "不可比"
        return f"{raw:+.2f} {unit}".strip() if isinstance(raw, float) else f"{raw} {unit}".strip()

    comparability = _dict(layer1.get("comparability"))
    task_set = _dict(layer2.get("taskSet"))
    certainty = str(comparability.get("costCertainty") or "")

    def title(side: dict[str, Any]) -> str:
        label = str(side.get("label") or "未命名方案")
        mode = str(side.get("objectiveMode") or "").strip()
        return f"{label}（{mode}）" if mode else label

    def convergence_text(side: dict[str, Any]) -> str:
        conv = _dict(side.get("convergence"))
        planned = conv.get("stagesPlanned") or 0
        if not planned:
            return "未记录收敛状态"
        executed = conv.get("stagesExecuted") or 0
        parts = [f"{executed}/{planned} 阶段"]
        tolerant = _list(conv.get("stagesWithinTolerance"))
        unproven = _list(conv.get("stagesUnproven"))
        if tolerant:
            parts.append(f"{len(tolerant)} 段容差内")
        if unproven:
            names = "、".join(str(item) for item in unproven[:2])
            more = "等" if len(unproven) > 2 else ""
            parts.append(f"{len(unproven)} 段未证明最优（{names}{more}）")
        gap = conv.get("costGap")
        metric = str(conv.get("costGapMetric") or "目标")
        if isinstance(gap, (int, float)):
            parts.append(f"{metric}相对差距 {gap:.2%}" if gap else f"{metric}已证明最优")
        elapsed = conv.get("elapsedSeconds")
        budget = conv.get("budgetSeconds")
        if isinstance(elapsed, (int, float)):
            if isinstance(budget, (int, float)) and budget:
                parts.append(f"用时 {elapsed:.1f}/{budget:.0f} 秒")
            else:
                parts.append(f"用时 {elapsed:.1f} 秒")
        return "，".join(parts)

    if certainty == "inconclusive":
        cost_note = "；差值落在两侧求解容差内，不能据此判定谁更省"
    elif certainty == "unverified":
        cost_note = "；缺收敛记录，差值未核实"
    elif certainty == "incomparable":
        cost_note = "；两次数据或任务集合不同，不可直接比"
    else:
        cost_note = ""

    if certainty == "inconclusive":
        comparable_text = (
            "两次数据快照与任务集合一致，但两侧都未证明最优："
            "成本差值小于求解容差，不能据此宣称谁更省。"
        )
    elif certainty == "unverified":
        comparable_text = "两次数据快照与任务集合一致，但缺少收敛记录，成本差值未经核实。"
    elif certainty == "proven":
        comparable_text = (
            "两次数据快照与任务集合一致，两版各自算到了本目标的最优；"
            "注意分层先锁定成本最小、加权优化的是加权和，两者成本数字可对照，但目标口径不同。"
        )
    elif comparability.get("savingsClaimAllowed"):
        comparable_text = "两次数据快照与任务集合一致，差值超出求解容差，成本可直接比较。"
    else:
        comparable_text = "存在数据快照、任务集合或模型产物差异，不能直接宣称节省。"

    lines = [
        f"**{title(left)}与{title(right)}的事实对比**",
        "",
    ]
    if left.get("objectiveMode") or right.get("objectiveMode"):
        lines.append(
            f"- 目标模式：{left.get('objectiveMode') or '未记录'} → "
            f"{right.get('objectiveMode') or '未记录'}"
        )
    lines.extend([
        f"- 窗口：{value('window', 'left')} → {value('window', 'right')}",
        (
            f"- 总成本：{value('totalCost', 'left', '万元')} → "
            f"{value('totalCost', 'right', '万元')}（{delta('totalCost', '万元')}{cost_note}）"
        ),
        (
            f"- 完成任务：{value('scheduledTasks', 'left')} → "
            f"{value('scheduledTasks', 'right')}"
        ),
        (
            f"- 船舶艘天：{value('vesselDays', 'left', '艘天')} → "
            f"{value('vesselDays', 'right', '艘天')}（{delta('vesselDays', '艘天')}）"
        ),
        (
            f"- 求解状态：{value('solverStatus', 'left')} → "
            f"{value('solverStatus', 'right')}"
        ),
        f"- 收敛：左侧 {convergence_text(left)}；右侧 {convergence_text(right)}",
        (
            f"- 任务集合：仅左侧 {task_set.get('onlyLeftCount', 0)} 项，"
            f"仅右侧 {task_set.get('onlyRightCount', 0)} 项，"
            f"共同 {task_set.get('bothCount', 0)} 项，改期 {task_set.get('movedCount', 0)} 项"
        ),
        "",
        "可比性：" + comparable_text,
    ])
    notes = _list(comparability.get("notes"))
    if notes:
        lines.append("限制：" + "；".join(str(item) for item in notes[:3]))
    lines.append("逐任务、逐约束或原始模型可在方案详情中查看。")
    return "\n".join(lines)
