from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


class PlanningHorizonError(ValueError):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class WindowSelection:
    as_of: datetime
    planning_boundary: datetime
    observation_end: datetime
    target_window: dict | None
    skipped_windows: list[dict]
    status: str


def _aware_datetime(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise PlanningHorizonError("INVALID_INPUT", f"{label}不是有效的 ISO 8601 时间") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PlanningHorizonError("INVALID_INPUT", f"{label}必须包含时区")
    return parsed


def select_target_window(
    as_of: str,
    windows: list[dict],
    *,
    lead_days: int = 7,
    horizon_days: int = 90,
    run_mode: str = "HYPOTHETICAL",
) -> WindowSelection:
    if run_mode not in {"PRODUCTION", "HYPOTHETICAL"}:
        raise PlanningHorizonError("INVALID_INPUT", "run_mode 必须是 PRODUCTION 或 HYPOTHETICAL")
    if lead_days < 0 or horizon_days <= lead_days:
        raise PlanningHorizonError("INVALID_INPUT", "准备期和观察期配置无效")

    planning_time = _aware_datetime(as_of, "规划时点")
    planning_boundary = planning_time + timedelta(days=lead_days)
    observation_end = planning_time + timedelta(days=horizon_days)
    identifiers: set[str] = set()
    normalized: list[tuple[datetime, str, datetime, dict]] = []

    for item in windows:
        window_id = str(item.get("编号", "")).strip()
        if not window_id or window_id in identifiers:
            raise PlanningHorizonError("INVALID_INPUT", "窗口编号缺失或重复")
        identifiers.add(window_id)
        start = _aware_datetime(item.get("开始时间"), f"{window_id}开始时间")
        end = _aware_datetime(item.get("结束时间不含"), f"{window_id}结束时间")
        if start >= end:
            raise PlanningHorizonError("INVALID_INPUT", f"{window_id}的时间端点无效")
        normalized.append((start.astimezone(timezone.utc), window_id, end.astimezone(timezone.utc), item))

    boundary_utc = planning_boundary.astimezone(timezone.utc)
    observation_end_utc = observation_end.astimezone(timezone.utc)
    candidates: list[tuple[datetime, str, dict]] = []
    skipped: list[dict] = []
    for start, window_id, end, item in sorted(normalized, key=lambda row: (row[0], row[1])):
        reason = None
        if start < boundary_utc:
            reason = "STARTS_BEFORE_PLANNING_BOUNDARY"
        elif end > observation_end_utc:
            reason = "ENDS_AFTER_OBSERVATION_BOUNDARY"
        elif run_mode == "PRODUCTION" and item.get("审批状态") != "已批准":
            reason = "NOT_APPROVED_FOR_PRODUCTION"
        if reason:
            skipped.append({"编号": window_id, "原因码": reason})
        else:
            candidates.append((start, window_id, item))

    target = candidates[0][2] if candidates else None
    return WindowSelection(
        as_of=planning_time,
        planning_boundary=planning_boundary,
        observation_end=observation_end,
        target_window=target,
        skipped_windows=skipped,
        status="READY" if target else "NO_ELIGIBLE_WINDOW",
    )
