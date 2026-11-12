"""Read-only site data for the conversational maintenance agent.

Everything here comes from the same snapshot the optimizer uses.  The module
never treats the synthetic demo feed as live SCADA data, never reads alarm
records that arrive after the planning time, and never invents a missing
domain.  Every report carries data nature, source and as-of time.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.core.environment_data import SHANGHAI_TZ, load_snapshot


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEMO_DIR = PROJECT_ROOT / "data" / "phase1-demo"

_SEVERITY_ORDER = {"高": 0, "中": 1, "低": 2}
_SEVERITY_LABEL = {"高": "高", "中": "中", "低": "低"}

# Runtime params and the modeling-parameter catalog use different labels.
# Keep the aliases here so a question about “船租” reaches “船舶日租金”.
_PARAMETER_ALIASES: dict[str, tuple[str, ...]] = {
    "船舶日租金": ("船租（元每艘每天）", "船租", "日租金"),
    "维修人工单价": ("人工单价（元每人时）", "人工单价", "人工费"),
    "最大作业线": ("最多作业线数", "最多作业线", "作业线数", "作业线"),
    "可调动人员总数": ("人员总上限", "人员总数", "人员上限", "人员"),
    "每日有效维修上限": ("每天最多维修小时数", "每天维修小时数", "维修小时"),
    "每天开始维修": ("每天维修开始时间", "维修开始时间", "开始维修"),
    "每天结束维修": ("每天维修结束时间", "维修结束时间", "结束维修"),
    "风机间转移": ("风机间转移小时数", "转移小时数", "转移小时"),
    "总求解预算": ("求解时间上限（秒）", "求解时间上限", "求解时间"),
    "截止窗口锁定依据": ("截止规则", "截止窗口依据"),
    "告警接收规划时刻": ("每日告警处理时间", "告警处理时间"),
    "高严重度截止窗口序号": ("高严重度截止窗口",),
    "中严重度截止窗口序号": ("中严重度截止窗口",),
    "低严重度截止窗口序号": ("低严重度截止窗口",),
}


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _fmt_minute(snapshot: Any, minute: int | None) -> str | None:
    if minute is None:
        return None
    return snapshot.stamp(int(minute))[:16].replace("T", " ")


def _parse_minute(snapshot: Any, value: str | None) -> int | None:
    if not value:
        return None
    try:
        return snapshot.minute(value)
    except (TypeError, ValueError):
        return None


def _as_of(snapshot: Any) -> str:
    return str((snapshot.meta or {}).get("规划时点") or "")[:16].replace("T", " ")


def _nature(snapshot: Any) -> str:
    return str((snapshot.meta or {}).get("数据性质") or "未标注")


def _source(snapshot: Any) -> str:
    value = str((snapshot.meta or {}).get("来源") or "")
    return Path(value).name if value else "未记录"


def _component_name(snapshot: Any, component_id: str | None) -> str:
    part = (snapshot.parts or {}).get(component_id or "", {})
    return str(part.get("部件名称") or component_id or "未记录")


def _number(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.4f}".rstrip("0").rstrip(".")


def _scalar(value: Any) -> Any:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    return int(number) if number.is_integer() else number


def _same_id(left: Any, right: Any, prefix: str) -> bool:
    def normalize(value: Any) -> str:
        text = str(value or "").strip()
        if text.startswith(prefix):
            text = text[len(prefix) :]
        return text.lstrip("0") or "0"

    return normalize(left) == normalize(right)


def _alarm_row(snapshot: Any, task: dict[str, Any]) -> dict[str, Any]:
    deadline = task.get("截止分钟")
    return {
        "taskId": task.get("任务编号"),
        "alarmId": task.get("告警编号"),
        "turbineId": task.get("风机编号"),
        "componentId": task.get("部件编号"),
        "componentName": _component_name(snapshot, task.get("部件编号")),
        "severity": task.get("严重等级"),
        "fault": task.get("故障描述"),
        "occurredAt": _fmt_minute(snapshot, task.get("发生分钟")),
        "receivedAt": _fmt_minute(snapshot, task.get("接收分钟")),
        "firstPlannedAt": task.get("首次规划时间"),
        "deadline": _fmt_minute(snapshot, deadline),
        "deadlineWindow": task.get("截止来源窗口编号"),
        "status": task.get("任务状态"),
        "spareQty": task.get("备件需求数量"),
        "remainingOutput": task.get("剩余出力"),
    }


def _sort_alarms(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda item: (
            _SEVERITY_ORDER.get(str(item.get("severity")), 9),
            item.get("deadline") is None,
            str(item.get("deadline") or "9999"),
            str(item.get("taskId") or ""),
        ),
    )


def _historical_alarm_rows(snapshot: Any) -> list[dict[str, Any]]:
    """All alarm-task rows received by the planning time, including closed."""
    rows: list[dict[str, Any]] = []
    for task in _read_csv(DEMO_DIR / "告警任务.csv"):
        received = _parse_minute(snapshot, task.get("首次规划时间"))
        if received is None or received > int(snapshot.now):
            continue
        enriched = dict(task)
        enriched["接收分钟"] = received
        enriched["发生分钟"] = _parse_minute(snapshot, task.get("发生时间"))
        enriched["截止分钟"] = _parse_minute(snapshot, task.get("截止时间（不含）"))
        enriched["截止来源窗口编号"] = task.get("截止窗口编号")
        rows.append(_alarm_row(snapshot, enriched))
    return _sort_alarms(rows)


def _filter_alarms(
    rows: list[dict[str, Any]],
    *,
    severity: str | None = None,
    status: str | None = None,
    turbine_id: str | None = None,
    component: str | None = None,
    task_id: str | None = None,
) -> list[dict[str, Any]]:
    result = rows
    if severity:
        result = [item for item in result if item.get("severity") == severity]
    if status:
        result = [item for item in result if item.get("status") == status]
    if turbine_id:
        result = [
            item
            for item in result
            if _same_id(item.get("turbineId"), turbine_id, "风机")
        ]
    if component:
        needle = str(component)
        result = [
            item
            for item in result
            if needle in str(item.get("componentName") or "")
            or needle == str(item.get("componentId") or "")
        ]
    if task_id:
        result = [
            item
            for item in result
            if _same_id(item.get("taskId"), task_id, "任务")
        ]
    return _sort_alarms(result)


def _alarm_table(
    rows: list[dict[str, Any]],
    *,
    include_time: bool = False,
) -> list[str]:
    lines = [
        "| 任务 | 风机 | 部件 | 等级 | 截止 | 状态 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for item in rows:
        task = str(item.get("taskId") or "")
        if include_time and item.get("receivedAt"):
            task += f"（{str(item['receivedAt'])[11:16]}）"
        deadline = item.get("deadline") or "待补充窗口"
        if item.get("deadline") and item.get("deadlineWindow"):
            deadline = f"{str(deadline)[5:16]}（{item['deadlineWindow']}）"
        lines.append(
            f"| {task} | {item.get('turbineId')} | {item.get('componentName')} | "
            f"{item.get('severity')} | {deadline} | {item.get('status')} |"
        )
    return lines


def _alarm_detail(item: dict[str, Any]) -> list[str]:
    deadline = item.get("deadline") or "待补充窗口"
    if item.get("deadline") and item.get("deadlineWindow"):
        deadline = f"{deadline}（{item['deadlineWindow']}）"
    return [
        f"**{item.get('taskId')} · {item.get('turbineId')} · "
        f"{item.get('componentName')} · {item.get('severity')}**",
        "",
        f"- 故障：{item.get('fault') or '未记录'}",
        f"- 接收：{item.get('receivedAt') or '未记录'}",
        f"- 截止：{deadline}",
        f"- 状态：{item.get('status') or '未记录'}",
    ]


def _counts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """按任务状态分档。

    「待安排」只数真正还没进任何检修单的任务；已排程的任务在等执行，不该算进去。
    以前两者混在一起，所以「待安排 N 项」实际等于「收到过且没标完成/取消的全部告警」。
    """
    pending_statuses = {"待安排", "待补充窗口", "未记录"}
    scheduled_statuses = {"已排程", "执行中"}
    closed_statuses = {"已完成", "已取消"}
    by_severity = {"高": 0, "中": 0, "低": 0}
    by_status: dict[str, int] = {}
    by_window: dict[str, int] = {}
    by_component: dict[str, int] = {}
    pending_count = 0
    scheduled_count = 0
    for item in rows:
        status = str(item.get("status") or "未记录")
        severity = str(item.get("severity") or "")
        if status in pending_statuses:
            pending_count += 1
            if severity in by_severity:
                by_severity[severity] += 1
        elif status in scheduled_statuses:
            scheduled_count += 1
        by_status[status] = by_status.get(status, 0) + 1
        window = str(item.get("deadlineWindow") or "未记录")
        by_window[window] = by_window.get(window, 0) + 1
        component = str(item.get("componentName") or "未记录")
        by_component[component] = by_component.get(component, 0) + 1
    return {
        # open 保留给既有调用，语义已收窄为「还没排进检修单」。
        "open": pending_count,
        "pending": pending_count,
        "scheduled": scheduled_count,
        "closed": len(rows) - pending_count - scheduled_count,
        "total": len(rows),
        "bySeverity": by_severity,
        "byStatus": by_status,
        "byDeadlineWindow": by_window,
        "byComponent": by_component,
    }


def _daily_feed(
    snapshot: Any,
    *,
    days: int,
    anchor_date: Any = None,
) -> list[dict[str, Any]]:
    as_of_minute = int(snapshot.now)
    end_date = snapshot.base + timedelta(minutes=as_of_minute)
    end_day = anchor_date or end_date.date()
    start_date = end_day - timedelta(days=max(1, days) - 1)
    rows = _read_csv(DEMO_DIR / "每日告警接收汇总.csv")
    wanted = {
        (start_date + timedelta(days=offset)).isoformat()
        for offset in range(max(1, days))
    }
    result: list[dict[str, Any]] = []
    for date in sorted(wanted):
        explicit = next((item for item in rows if item.get("日期") == date), None)
        if explicit:
            result.append(
                {
                    "date": date,
                    "newCount": int(float(explicit.get("新增告警数量") or 0)),
                    "duplicateCount": int(float(explicit.get("重复上报数量") or 0)),
                    "receivedCount": int(float(explicit.get("收到记录总数") or 0)),
                    "result": explicit.get("接收结果"),
                    "file": explicit.get("日报文件"),
                }
            )
        else:
            daily_file = DEMO_DIR / "每日告警单" / f"{date}告警单.csv"
            result.append(
                {
                    "date": date,
                    "newCount": 0,
                    "duplicateCount": 0,
                    "receivedCount": 0,
                    "result": "无告警记录" if daily_file.exists() else "无日报记录",
                    "file": f"{date}告警单.csv",
                }
            )
    # The feed stops at the planning time; future daily files are never read.
    for item in result:
        if item["date"] > end_date.date().isoformat():
            item["newCount"] = 0
    return result


def _resolve_date(snapshot: Any, value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    as_of = (snapshot.base + timedelta(minutes=int(snapshot.now))).date()
    if text in {"今天", "today"}:
        return as_of.isoformat()
    if text in {"昨天", "yesterday"}:
        return (as_of - timedelta(days=1)).isoformat()
    if text in {"前天"}:
        return (as_of - timedelta(days=2)).isoformat()
    try:
        return datetime.fromisoformat(text).date().isoformat()
    except ValueError:
        return None


def alarm_report(
    snapshot: Any,
    *,
    scope: str = "backlog",
    days: int = 7,
    severity: str | None = None,
    status: str | None = None,
    turbine_id: str | None = None,
    component: str | None = None,
    task_id: str | None = None,
    limit: int = 40,
    include_closed: bool = False,
    date: str | None = None,
) -> dict[str, Any]:
    """Alarm backlog / recent feed / summary as of the planning snapshot."""
    if include_closed or status in {"已完成", "已取消"}:
        all_rows = _historical_alarm_rows(snapshot)
    else:
        all_rows = _sort_alarms([_alarm_row(snapshot, task) for task in snapshot.tasks])
    universe = _counts(all_rows)
    as_of = _as_of(snapshot)
    nature = _nature(snapshot)

    if scope == "recent":
        days = max(1, min(int(days or 7), 30))
        resolved_date = _resolve_date(snapshot, date)
        if resolved_date:
            days = 1
            from_dt = datetime.fromisoformat(f"{resolved_date}T00:00:00+08:00")
            rows = _sort_alarms(
                [
                    item
                    for item in all_rows
                    if str(item.get("receivedAt") or "")[:10] == resolved_date
                ]
            )
            daily = _daily_feed(
                snapshot,
                days=1,
                anchor_date=datetime.fromisoformat(resolved_date).date(),
            )
            heading = f"**{resolved_date} 新增告警 {len(rows)} 项**（截至 {as_of}）"
        else:
            as_of_dt = snapshot.base + timedelta(minutes=int(snapshot.now))
            from_dt = as_of_dt - timedelta(days=days - 1)
            from_minute = int((from_dt - snapshot.base).total_seconds() / 60)
            # Filter on raw task minutes, not the formatted display string.
            recent_rows: list[dict[str, Any]] = []
            for task in snapshot.tasks:
                received = int(task.get("接收分钟") or 0)
                if received >= from_minute:
                    recent_rows.append(_alarm_row(snapshot, task))
            rows = _sort_alarms(recent_rows)
            daily = _daily_feed(snapshot, days=days)
            heading = (
                f"**最近 {days} 天新增告警 {len(rows)} 项**"
                f"（{from_dt.date().isoformat()} 至 {as_of[:10]}，截至 {as_of}）"
            )
        shown = rows[: max(1, min(int(limit or 40), 60))]
        lines = [heading, ""]
        feed_rows = [
            item for item in daily if item.get("result") != "无日报记录"
        ]
        if feed_rows:
            lines.extend(
                [
                    "| 日期 | 新增 | 重复 | 收到 | 接收结果 |",
                    "| --- | --- | --- | --- | --- |",
                ]
            )
            for item in feed_rows:
                lines.append(
                    f"| {item['date'][5:]} | {item['newCount']} | {item['duplicateCount']} | "
                    f"{item['receivedCount']} | {item['result'] or '未记录'} |"
                )
        missing = [
            item["date"] for item in daily if item.get("result") == "无日报记录"
        ]
        if missing:
            by_date: dict[str, int] = {}
            for item in rows:
                day = str(item.get("receivedAt") or "")[:10]
                if day:
                    by_date[day] = by_date.get(day, 0) + 1
            notes = []
            for day in missing:
                count = by_date.get(day, 0)
                if count:
                    notes.append(
                        f"{day[5:]} 没有日报文件，但任务库记录了 {count} 项接收任务，"
                        "已计入清单；不能据此说该日没有新增。"
                    )
            empty_days = [day[5:] for day in missing if not by_date.get(day)]
            if empty_days:
                notes.append(
                    f"{'、'.join(empty_days)} 没有日报文件，任务库也没有接收记录。"
                )
            if notes:
                lines.append("")
                lines.extend(notes)
        lines.append("")
        if shown:
            lines.extend(_alarm_table(shown, include_time=True))
            if len(rows) > len(shown):
                lines.append(f"其余 {len(rows) - len(shown)} 项可在告警清单中查看。")
        else:
            lines.append("这段时间没有新接收的告警。")
        lines.append("")
        lines.append(f"数据性质：{nature}；只读取截至 {as_of} 已接收的告警。")
        return {
            "scope": "recent",
            "days": days,
            "from": from_dt.date().isoformat(),
            "to": as_of[:10],
            "asOf": as_of,
            "alarmWaterline": (snapshot.meta or {}).get("告警水位线"),
            "dataNature": nature,
            "source": _source(snapshot),
            "inputHash": (snapshot.meta or {}).get("输入哈希"),
            "daily": daily,
            "counts": _counts(rows),
            "rows": rows,
            "returned": len(shown),
            "markdown": "\n".join(lines),
            "verbatim": True,
        }

    if scope == "summary":
        daily = _daily_feed(snapshot, days=7)
        dated = [item for item in all_rows if item.get("deadline")]
        earliest = min(dated, key=lambda item: item["deadline"]) if dated else None
        latest = max(dated, key=lambda item: item["deadline"]) if dated else None
        lines = [
            f"**告警概况**（截至 {as_of}，{nature}数据）",
            "",
            f"待安排 {universe['open']} 项：高 {universe['bySeverity']['高']} · "
            f"中 {universe['bySeverity']['中']} · 低 {universe['bySeverity']['低']}。",
        ]
        if universe["scheduled"]:
            lines.append(f"另有已排程 {universe['scheduled']} 项在等执行，不计入待安排。")
        if universe["byDeadlineWindow"]:
            parts = "、".join(
                f"{name} {count} 项"
                for name, count in sorted(universe["byDeadlineWindow"].items())
            )
            lines.append(f"按截止窗口：{parts}。")
        if universe["byComponent"]:
            parts = "、".join(
                f"{name} {count} 项"
                for name, count in sorted(
                    universe["byComponent"].items(),
                    key=lambda item: (-item[1], item[0]),
                )
            )
            lines.append(f"按部件：{parts}。")
        if earliest:
            lines.append(
                f"最早截止：{earliest['taskId']}（{earliest['turbineId']} · "
                f"{earliest['componentName']}，{earliest['severity']}）"
                f"截止 {earliest['deadline']}。"
            )
        if latest and latest is not earliest:
            lines.append(
                f"最晚截止：{latest['taskId']}（{latest['turbineId']} · "
                f"{latest['componentName']}，{latest['severity']}）"
                f"截止 {latest['deadline']}。"
            )
        lines.extend(
            [
                "",
                "| 日期 | 新增 | 重复 | 收到 |",
                "| --- | --- | --- | --- |",
            ]
        )
        for item in daily:
            lines.append(
                f"| {item['date'][5:]} | {item['newCount']} | {item['duplicateCount']} | "
                f"{item['receivedCount']} |"
            )
        lines.extend(
            [
                "",
                "告警是计划快照，不是实时 SCADA 推送；只统计截至规划时点已接收的记录。",
            ]
        )
        return {
            "scope": "summary",
            "asOf": as_of,
            "alarmWaterline": (snapshot.meta or {}).get("告警水位线"),
            "dataNature": nature,
            "source": _source(snapshot),
            "inputHash": (snapshot.meta or {}).get("输入哈希"),
            "counts": universe,
            "daily": daily,
            "markdown": "\n".join(lines),
            "verbatim": True,
        }

    rows = _filter_alarms(
        all_rows,
        severity=severity,
        status=status,
        turbine_id=turbine_id,
        component=component,
        task_id=task_id,
    )
    counts = _counts(rows)
    shown = rows[: max(1, min(int(limit or 40), 60))]
    title_parts = []
    if severity:
        title_parts.append(f"{_SEVERITY_LABEL.get(severity, severity)}风险")
    if status:
        title_parts.append(status)
    title = "".join(title_parts) or ("历史" if include_closed else "待安排")
    lines = [
        f"**{title}告警 {len(rows)} 项**（截至 {as_of}，{nature}数据）",
        "",
        f"{'当前筛选' if (severity or status or turbine_id or component or task_id or include_closed) else '全部'} "
        f"{counts['total']} 项（待安排 {counts['open']}"
        + (f"、已关闭 {counts['closed']}" if counts["closed"] else "")
        + f"）：高 {counts['bySeverity']['高']} · "
        f"中 {counts['bySeverity']['中']} · 低 {counts['bySeverity']['低']}。",
    ]
    if severity or status or turbine_id or component or task_id or include_closed:
        lines.append(f"当前待安排共 {universe['open']} 项，作为对照。")
    lines.append("")
    if shown and task_id and len(shown) == 1:
        lines.extend(_alarm_detail(shown[0]))
    elif shown:
        lines.extend(_alarm_table(shown))
        if len(rows) > len(shown):
            lines.append(f"其余 {len(rows) - len(shown)} 项未展开。")
    else:
        lines.append("没有符合筛选条件的告警。")
    lines.extend(
        [
            "",
            "按严重度、截止时间排序；具体先修哪些由当次排程的目标与资源约束决定。",
            f"数据性质：{nature}；只读取截至 {as_of} 已接收的告警。",
        ]
    )
    return {
        "scope": "backlog",
        "asOf": as_of,
        "alarmWaterline": (snapshot.meta or {}).get("告警水位线"),
        "dataNature": nature,
        "source": _source(snapshot),
        "inputHash": (snapshot.meta or {}).get("输入哈希"),
        "counts": counts,
        "rows": rows,
        "returned": len(shown),
        "markdown": "\n".join(lines),
        "verbatim": True,
    }


def _fleet_rows() -> list[dict[str, Any]]:
    return [
        {
            "turbineId": item.get("风机编号"),
            "ratedPowerMw": item.get("额定功率（兆瓦）"),
            "applicableComponents": item.get("适用部件"),
        }
        for item in _read_csv(DEMO_DIR / "风机台账.csv")
    ]


def _component_rows(snapshot: Any) -> list[dict[str, Any]]:
    rows = []
    for part in (snapshot.parts or {}).values():
        rows.append(
            {
                "componentId": part.get("部件编号"),
                "componentName": part.get("部件名称"),
                "scope": part.get("维修范围"),
                "unit": part.get("备件单位"),
                "sparePerTask": part.get("每任务备件数量"),
                "peoplePerTask": part.get("固定维修人数"),
                "hours": {
                    "高": {
                        "常态": part.get("高严重度常态维修工时（小时）"),
                        "恶劣但安全": part.get("高严重度恶劣但安全维修工时（小时）"),
                    },
                    "中": {
                        "常态": part.get("中严重度常态维修工时（小时）"),
                        "恶劣但安全": part.get("中严重度恶劣但安全维修工时（小时）"),
                    },
                    "低": {
                        "常态": part.get("低严重度常态维修工时（小时）"),
                        "恶劣但安全": part.get("低严重度恶劣但安全维修工时（小时）"),
                    },
                },
                "spareCostYuan": part.get("维修包成本（元每套）"),
                "otherCostYuan": part.get("其他费用（元每任务）"),
            }
        )
    return sorted(rows, key=lambda item: str(item.get("componentId") or ""))


def _window_rows(snapshot: Any) -> list[dict[str, Any]]:
    selection = snapshot.window_selection or {}
    target_id = selection.get("目标窗口编号")
    as_of_minute = int(snapshot.now)
    rows = []
    previous_end = None
    for item in snapshot.observation_windows:
        start = int(item["开始"])
        end = int(item["结束"])
        if end <= as_of_minute:
            state = "已结束"
        elif start <= as_of_minute < end:
            state = "进行中"
        else:
            state = "后续"
        if item["编号"] == target_id:
            state = "目标窗口"
        rows.append(
            {
                "windowId": item["编号"],
                "start": _fmt_minute(snapshot, start),
                "end": _fmt_minute(snapshot, end),
                "days": round((end - start) / 1440, 2),
                "gapDays": (
                    round((start - previous_end) / 1440, 2)
                    if previous_end is not None
                    else 0
                ),
                "approval": item.get("审批状态"),
                "state": state,
                "isTarget": item["编号"] == target_id,
            }
        )
        previous_end = end
    return rows


def _inventory_rows(snapshot: Any) -> list[dict[str, Any]]:
    target = next(
        (item for item in snapshot.observation_windows if item["编号"] == snapshot.window_selection.get("目标窗口编号")),
        None,
    )
    target_start = int(target["开始"]) if target else int(snapshot.now)
    demand: dict[str, int] = {}
    for task in snapshot.tasks:
        part = str(task.get("部件编号") or "")
        demand[part] = demand.get(part, 0) + int(float(task.get("备件需求数量") or 0))
    rows = []
    for part_id, stock in (snapshot.stocks or {}).items():
        available_at_target = int(snapshot.availability(part_id, target_start))
        pending = int(demand.get(part_id, 0))
        rows.append(
            {
                "componentId": part_id,
                "componentName": _component_name(snapshot, part_id),
                "unit": (snapshot.parts or {}).get(part_id, {}).get("备件单位"),
                "stock": int(stock),
                "availableNow": int(snapshot.availability(part_id, int(snapshot.now))),
                "availableAtTargetStart": available_at_target,
                "pendingDemand": pending,
                "shortage": max(0, pending - available_at_target),
            }
        )
    return sorted(rows, key=lambda item: str(item.get("componentId") or ""))


def _work_order_rows() -> list[dict[str, Any]]:
    return _read_csv(DEMO_DIR / "检修单.csv")


def _manifest_rows() -> list[dict[str, Any]]:
    return _read_csv(DEMO_DIR / "出海清单.csv")


def _execution_rows() -> list[dict[str, Any]]:
    return _read_csv(DEMO_DIR / "执行反馈.csv")


def _parameter_rows(snapshot: Any) -> list[dict[str, Any]]:
    params = snapshot.params or {}
    catalog_rows = _read_csv(PROJECT_ROOT / "data" / "模型参数" / "参数总表.csv")
    catalog = {
        str(item.get("参数名称") or ""): item
        for item in catalog_rows
        if item.get("参数名称")
    }
    catalog_by_alias: dict[str, dict[str, Any]] = {}
    for canonical, aliases in _PARAMETER_ALIASES.items():
        row = catalog.get(canonical)
        if not row:
            continue
        catalog_by_alias[canonical] = row
        for alias in aliases:
            catalog_by_alias[alias] = row
    specs = [
        ("船租（元每艘每天）", "元/艘天", "按自然日计租；同窗口首任务开工至末任务完成"),
        ("人工单价（元每人时）", "元/人时", "维修人工"),
        ("最多作业线数", "条", "同时在场的作业线上限"),
        ("人员总上限", "人", "全场人员上限"),
        ("每天最多维修小时数", "小时", "每日维修工时上限"),
        ("每天维修开始时间", "时刻", "每日可作业开始"),
        ("每天维修结束时间", "时刻", "每日可作业结束"),
        ("风机间转移小时数", "小时", "风机间转移耗时"),
        ("求解时间上限（秒）", "秒", "单次求解时限；超时不等于无解"),
        ("求解相对差距", "比例", "最优性相对容差"),
        ("高严重度截止窗口序号", "窗口序号", "首次规划锁定未来第几个海事窗口"),
        ("中严重度截止窗口序号", "窗口序号", "首次规划锁定未来第几个海事窗口"),
        ("低严重度截止窗口序号", "窗口序号", "首次规划锁定未来第几个海事窗口"),
        ("截止规则", "", "期限不因海况或库存冲突顺延"),
        ("每日告警处理时间", "", "每天该时刻之前的告警参与本轮"),
        ("预测性质", "", "数据性质与精度声明"),
    ]
    # 这两行必须显示"真正会用的数"：表里写 60、实际被部署上限压到 30，
    # 等于把参数总表当摆设。其余参数照原样显示。
    from solve_budget import resolve as resolve_budget

    budget = resolve_budget(params)
    effective = {
        "求解时间上限（秒）": (
            budget["seconds"],
            f"本次生效 {budget['secondsSource']}；表内值 {budget['tableSeconds']}"
            if budget["secondsSource"] != "参数总表"
            else "参数总表默认值；用户点名时限时按点名值执行，实际值见本次求解信息",
        ),
        "求解相对差距": (
            budget["gapLimit"],
            "仅成本阶段生效；完成数等整数阶段要求证明最优",
        ),
    }
    return [
        {
            "name": name,
            "value": effective[name][0] if name in effective else params.get(name),
            "unit": unit,
            "note": effective[name][1] if name in effective else note,
            "tableValue": params.get(name),
            "constraint": catalog_by_alias.get(name, {}).get("取值约束"),
            "sourceStatus": catalog_by_alias.get(name, {}).get("来源与确认状态"),
            "category": catalog_by_alias.get(name, {}).get("参数类别"),
            "modelingImpact": catalog_by_alias.get(name, {}).get("建模影响"),
        }
        for name, unit, note in specs
        if name in params
    ]


def _parameter_search(
    snapshot: Any,
    query: str,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Search the 122-row model-parameter catalog by name or short label."""
    params = snapshot.params or {}
    rows: list[dict[str, Any]] = []
    for item in _read_csv(PROJECT_ROOT / "data" / "模型参数" / "参数总表.csv"):
        name = str(item.get("参数名称") or "")
        if not name:
            continue
        base = name.split("（")[0]
        aliases = _PARAMETER_ALIASES.get(name, ())
        if query and (
            query in name
            or (base and base in query)
            or any(alias in query for alias in aliases)
        ):
            rows.append(
                {
                    "id": item.get("参数编号"),
                    "name": name,
                    "value": _scalar(params.get(name, item.get("默认值"))),
                    "unit": item.get("单位"),
                    "category": item.get("参数类别"),
                    "constraint": item.get("取值约束"),
                    "sourceStatus": item.get("来源与确认状态"),
                    "modelingImpact": item.get("建模影响"),
                    "description": item.get("业务与计费说明"),
                }
            )
    return rows[: max(1, min(limit, 50))]


def site_report(
    snapshot: Any,
    *,
    scope: str = "overview",
    turbine_id: str | None = None,
    limit: int = 20,
    query: str | None = None,
) -> dict[str, Any]:
    """One report per site-data scope, with a compact Markdown view."""
    as_of = _as_of(snapshot)
    nature = _nature(snapshot)
    as_of_source = str((snapshot.meta or {}).get("时点来源") or "未标注")
    selection = snapshot.window_selection or {}
    target = next(
        (
            item
            for item in snapshot.observation_windows
            if item["编号"] == selection.get("目标窗口编号")
        ),
        None,
    )
    meta = {
        "asOf": as_of,
        "alarmWaterline": (snapshot.meta or {}).get("告警水位线"),
        "dataNature": nature,
        "source": _source(snapshot),
        "inputHash": (snapshot.meta or {}).get("输入哈希"),
        "planningBoundary": selection.get("规划边界"),
        "observationEnd": selection.get("观察终点"),
        "targetWindow": (
            {
                "windowId": target["编号"],
                "start": _fmt_minute(snapshot, target["开始"]),
                "end": _fmt_minute(snapshot, target["结束"]),
                "approval": target.get("审批状态"),
            }
            if target
            else None
        ),
        "selectionStatus": selection.get("状态"),
        "skippedWindows": selection.get("跳过窗口") or [],
    }

    if scope == "fleet":
        rows = _fleet_rows()
        if turbine_id:
            rows = [
                item
                for item in rows
                if _same_id(item.get("turbineId"), turbine_id, "风机")
            ]
        shown = rows[: max(1, min(int(limit or 20), 100))]
        ratings: dict[str, int] = {}
        for item in rows:
            key = str(item.get("ratedPowerMw") or "未记录")
            ratings[key] = ratings.get(key, 0) + 1
        lines = [
            f"**风机台账 {len(rows)} 台**（截至 {as_of}，{nature}数据）",
            "",
            "| 风机 | 额定功率 | 适用部件 |",
            "| --- | --- | --- |",
        ]
        for item in shown:
            lines.append(
                f"| {item['turbineId']} | {item['ratedPowerMw']} MW | "
                f"{item['applicableComponents']} |"
            )
        if len(rows) > len(shown):
            lines.append(f"其余 {len(rows) - len(shown)} 台未展开。")
        lines.extend(
            [
                "",
                "功率分布：" + "、".join(f"{k} MW {v} 台" for k, v in sorted(ratings.items())) + "。",
                "台账只有额定功率与适用部件，没有实时运行状态或可用性；设备在线状态不在本产品数据内。",
            ]
        )
        return {
            "scope": "fleet",
            **meta,
            "rows": rows,
            "returned": len(shown),
            "markdown": "\n".join(lines),
            "verbatim": True,
        }

    if scope == "components":
        rows = _component_rows(snapshot)
        lines = [
            f"**部件参数 {len(rows)} 类**（截至 {as_of}，{nature}数据）",
            "",
            "| 部件 | 维修范围 | 高（常态/恶劣） | 中（常态/恶劣） | 低（常态/恶劣） | 备件成本 | 其他费用 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for item in rows:
            hours = item["hours"]
            lines.append(
                f"| {item['componentName']} | {item['scope']} | "
                f"{_number(hours['高']['常态'])}/{_number(hours['高']['恶劣但安全'])} h | "
                f"{_number(hours['中']['常态'])}/{_number(hours['中']['恶劣但安全'])} h | "
                f"{_number(hours['低']['常态'])}/{_number(hours['低']['恶劣但安全'])} h | "
                f"{_number(item['spareCostYuan'])} 元/套 | {_number(item['otherCostYuan'])} 元/任务 |"
            )
        lines.extend(
            [
                "",
                "每任务固定 1 人、1 套备件；工时按严重度与海况取值。",
            ]
        )
        return {
            "scope": "components",
            **meta,
            "rows": rows,
            "markdown": "\n".join(lines),
            "verbatim": True,
        }

    if scope == "windows":
        rows = _window_rows(snapshot)
        lines = [
            f"**海事检修窗口 {len(rows)} 个**（截至 {as_of}，{nature}数据）",
            "",
            "| 窗口 | 开始 | 结束 | 天数 | 与上一窗口间隔 | 审批 | 状态 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for item in rows:
            lines.append(
                f"| {item['windowId']} | {item['start']} | {item['end']} | "
                f"{_number(item['days'])} | {_number(item['gapDays'])} 天 | "
                f"{item['approval']} | {item['state']} |"
            )
        lines.extend(
            [
                "",
                "目标窗口按“规划时点 + 7 天准备期”选取：取开始时间不早于规划边界、"
                "且已批准的完整窗口；若规划时点正落在某个窗口内，也跳过该窗口。",
            ]
        )
        if meta["targetWindow"]:
            lines.append(
                f"本次目标窗口：{meta['targetWindow']['windowId']}"
                f"（{meta['targetWindow']['start']} 至 {meta['targetWindow']['end']}）。"
            )
        if meta["skippedWindows"]:
            labels = "、".join(
                str(item.get("编号") if isinstance(item, dict) else item)
                for item in meta["skippedWindows"]
            )
            lines.append(f"被跳过的窗口：{labels}。")
        return {
            "scope": "windows",
            **meta,
            "rows": rows,
            "markdown": "\n".join(lines),
            "verbatim": True,
        }

    if scope == "inventory":
        rows = _inventory_rows(snapshot)
        movements = [
            {
                "movementId": item.get("流水编号"),
                "componentId": item.get("部件编号"),
                "componentName": _component_name(snapshot, item.get("部件编号")),
                "direction": item.get("收发方向"),
                "qty": item.get("数量"),
                "plannedAt": item.get("计划时间"),
                "status": item.get("流水状态"),
                "relatedOrder": item.get("关联单号"),
                "note": item.get("备注"),
            }
            for item in (snapshot.movements or [])
        ]
        upcoming = [
            item
            for item in movements
            if _parse_minute(snapshot, item.get("plannedAt")) is not None
            and int(_parse_minute(snapshot, item.get("plannedAt")) or 0) >= int(snapshot.now)
        ]
        lines = [
            f"**备件库存**（截至 {as_of}，{nature}数据）",
            "",
            "| 部件 | 现有库存 | 当前可用 | 目标窗口开始时可用 | 待安排任务总需求 | 缺口 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for item in rows:
            lines.append(
                f"| {item['componentName']} | {item['stock']} | {item['availableNow']} | "
                f"{item['availableAtTargetStart']} | {item['pendingDemand']} | "
                f"{item['shortage']} |"
            )
        shortages = [item for item in rows if item["shortage"] > 0]
        if shortages:
            lines.append(
                "按全部待安排任务的总需求估算，以下部件存在缺口："
                + "、".join(
                    f"{item['componentName']} {item['shortage']} 套"
                    for item in shortages
                )
                + "。"
            )
        else:
            lines.append("按全部待安排任务的总需求估算，目标窗口开始时没有备件缺口。")
        lines.extend(
            [
                "",
                "“待安排任务总需求”是全部未完成告警的备件需求，不是本次排程的实际领用；"
                "实际消耗以审批后的检修单为准。",
            ]
        )
        if upcoming:
            lines.extend(
                [
                    "",
                    "**规划时点之后的收发计划**",
                    "",
                    "| 流水 | 部件 | 方向 | 数量 | 计划时间 | 关联单号 |",
                    "| --- | --- | --- | --- | --- | --- |",
                ]
            )
            for item in upcoming[:12]:
                lines.append(
                    f"| {item['movementId']} | {item['componentName']} | {item['direction']} | "
                    f"{item['qty']} | {str(item['plannedAt'])[:16].replace('T', ' ')} | "
                    f"{item['relatedOrder'] or '—'} |"
                )
            if len(upcoming) > 12:
                lines.append(f"其余 {len(upcoming) - 12} 条流水未展开。")
        return {
            "scope": "inventory",
            **meta,
            "rows": rows,
            "movements": upcoming,
            "markdown": "\n".join(lines),
            "verbatim": True,
        }

    if scope == "work_orders":
        orders = _work_order_rows()
        manifests = _manifest_rows()
        by_status: dict[str, int] = {}
        for item in orders:
            status = str(item.get("审批状态") or "未记录")
            by_status[status] = by_status.get(status, 0) + 1
        lines = [
            f"**检修单**（截至 {as_of}，{nature}数据）",
            "",
        ]
        if orders:
            lines.append(
                f"已下发 {len(orders)} 张："
                + "、".join(f"{name} {count} 张" for name, count in by_status.items())
                + "。"
            )
            lines.extend(
                [
                    "",
                    "| 检修单 | 方案版本 | 任务 | 风机 | 窗口 | 计划开工 | 计划完工 | 审批 |",
                    "| --- | --- | --- | --- | --- | --- | --- | --- |",
                ]
            )
            for item in orders[:20]:
                lines.append(
                    f"| {item.get('检修单号')} | {item.get('方案版本')} | {item.get('任务编号')} | "
                    f"{item.get('风机编号')} | {item.get('窗口编号')} | "
                    f"{str(item.get('计划开工时间') or '')[:16].replace('T', ' ')} | "
                    f"{str(item.get('计划完工时间') or '')[:16].replace('T', ' ')} | "
                    f"{item.get('审批状态')} |"
                )
        else:
            lines.append(
                "当前没有已下发的检修单：数据文件为空。"
                "方案审批通过后才会生成检修单和出海清单。"
            )
        lines.append(
            f"出海清单 {len(manifests)} 条。"
            if manifests
            else "出海清单当前也为空。"
        )
        return {
            "scope": "work_orders",
            **meta,
            "counts": {"total": len(orders), "byStatus": by_status, "manifests": len(manifests)},
            "rows": orders,
            "markdown": "\n".join(lines),
            "verbatim": True,
        }

    if scope == "execution":
        rows = _execution_rows()
        lines = [f"**执行反馈**（截至 {as_of}，{nature}数据）", ""]
        if rows:
            lines.extend(
                [
                    "| 反馈 | 检修单 | 实际开工 | 实际完工 | 实际消耗 | 退料 | 故障消除 |",
                    "| --- | --- | --- | --- | --- | --- | --- |",
                ]
            )
            for item in rows[:20]:
                lines.append(
                    f"| {item.get('反馈编号')} | {item.get('检修单号')} | "
                    f"{str(item.get('实际开工时间') or '')[:16].replace('T', ' ')} | "
                    f"{str(item.get('实际完工时间') or '')[:16].replace('T', ' ')} | "
                    f"{item.get('实际消耗数量')} | {item.get('退料数量')} | "
                    f"{item.get('故障是否消除')} |"
                )
        else:
            lines.append(
                "暂无执行反馈：还没有回写的实际开工、完工或领退料记录。"
                "这不等于窗口没有执行，只表示反馈数据尚未入库。"
            )
        return {
            "scope": "execution",
            **meta,
            "rows": rows,
            "markdown": "\n".join(lines),
            "verbatim": True,
        }

    if scope == "parameters":
        if query:
            matches = _parameter_search(snapshot, query, limit=limit)
            if matches:
                lines = [
                    f"**参数查询：{query}**（截至 {as_of}，{nature}数据）",
                    "",
                    "| 参数 | 取值 | 约束 | 来源状态 |",
                    "| --- | --- | --- | --- |",
                ]
                for item in matches:
                    lines.append(
                        f"| {item['name']} | {item['value']} {item['unit'] or ''} | "
                        f"{item['constraint'] or '未记录'} | {item['sourceStatus'] or '未记录'} |"
                    )
                lines.extend(
                    [
                        "",
                        "来源状态为“用户已确定/沿用已确认数据”时可直接引用；"
                        "标为演示默认或假设的参数，不能当成现场实测值。",
                    ]
                )
                return {
                    "scope": "parameters",
                    **meta,
                    "query": query,
                    "rows": matches,
                    "markdown": "\n".join(lines),
                    "verbatim": True,
                }
        rows = _parameter_rows(snapshot)
        lines = [
            f"**系统参数与口径**（截至 {as_of}，{nature}数据）",
            "",
            "| 参数 | 取值 | 取值约束 | 来源状态 |",
            "| --- | --- | --- | --- |",
        ]
        if query:
            lines.insert(
                2,
                f"没有在参数总表中匹配到“{query}”，下面是关键系统参数。",
            )
        for item in rows:
            lines.append(
                f"| {item['name']} | {_number(item['value']) if item['value'] is not None else '未记录'} "
                f"{item['unit']} | {item['constraint'] or item['note']} | "
                f"{item['sourceStatus'] or '未记录'} |"
            )
        lines.extend(
            [
                "",
                "这些是基础数据；某个方案若覆盖了参数，以该方案模型产物中的参数值为准。",
                "参数总表还登记了作业、成本、库存和历史承诺等建模参数；"
                "具体某项的建模影响可在该方案模型产物中核对。",
            ]
        )
        return {
            "scope": "parameters",
            **meta,
            "rows": rows,
            "markdown": "\n".join(lines),
            "verbatim": True,
        }

    # overview
    alarms = _sort_alarms([_alarm_row(snapshot, task) for task in snapshot.tasks])
    counts = _counts(alarms)
    fleet_count = len(_fleet_rows())
    component_count = len(snapshot.parts or {})
    lines = [
        f"**现场数据概览**（截至 {as_of}，{nature}数据）",
        "",
        f"- 规划时点来源：{as_of_source}。钉住时点可复现，跟随当前时刻则按真实日历累积告警。",
        f"- 风机台账 {fleet_count} 台，部件 {component_count} 类。",
        f"- 待安排告警 {counts['open']} 项：高 {counts['bySeverity']['高']} · "
        f"中 {counts['bySeverity']['中']} · 低 {counts['bySeverity']['低']}。",
        f"- 已排程 {counts['scheduled']} 项在等执行；已完成 {counts['byStatus'].get('已完成', 0)} 项。",
        f"- 告警水位线：{str((snapshot.meta or {}).get('告警水位线') or '未记录')[:16].replace('T', ' ')}；"
        "此后收到的告警不参与本轮。",
        f"- 海事窗口 {len(snapshot.observation_windows)} 个；目标窗口 "
        f"{meta['targetWindow']['windowId'] if meta['targetWindow'] else '未选出'}。",
        f"- 备件 {len(snapshot.stocks or {})} 类，已确认收发流水 {len(snapshot.movements or [])} 条。",
        f"- 已下发检修单 {len({row.get('检修单号') for row in _work_order_rows()})} 张"
        f"（{len(_work_order_rows())} 个任务行），执行反馈 {len(_execution_rows())} 条。",
        "",
        f"数据来源：{_source(snapshot)}；输入哈希 "
        f"`{str((snapshot.meta or {}).get('输入哈希') or '')[:12]}`。",
        "本产品没有接入实时 SCADA、船舶定位或财务实账；告警与现场数据都是求解快照。",
    ]
    return {
        "scope": "overview",
        **meta,
        "counts": {
            "turbines": fleet_count,
            "components": component_count,
            "alarms": counts,
            "windows": len(snapshot.observation_windows),
            "inventorySkus": len(snapshot.stocks or {}),
            "movements": len(snapshot.movements or []),
            "workOrders": len(_work_order_rows()),
            "executionFeedback": len(_execution_rows()),
        },
        "markdown": "\n".join(lines),
        "verbatim": True,
    }


def load_site_snapshot(base: str = "基座01", overrides: dict[str, Any] | None = None):
    return load_snapshot(base, overrides)
