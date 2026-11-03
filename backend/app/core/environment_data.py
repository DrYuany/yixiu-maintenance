"""基座里的环境数据：逐时风速、电价、每日海况。

和求解器读的是同一份 load_project 快照，不另开数据源；返回时带输入哈希、
规划时点和数据性质，避免把合成数据说成真实市场数据。
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = PROJECT_ROOT / "maintenance-milp-modeler" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

SHANGHAI_TZ = timezone(timedelta(hours=8))
DEFAULT_BASE = "基座01"
_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
_BLOCKS = "▁▂▃▄▅▆▇"


def _planning_key() -> str:
    """缓存键里必须带日期，否则进程常驻时会一直返回启动那天的快照。"""
    import os

    pinned = os.environ.get("PLANNING_AS_OF")
    if pinned:
        return pinned
    now = datetime.now(SHANGHAI_TZ)
    # 每天 18:00 是告警接收时点，跨过它就要换一份快照
    return now.strftime("%Y-%m-%d") + ("-PM" if now.hour >= 18 else "-AM")


@lru_cache(maxsize=8)
def _snapshot(base: str, overrides_key: tuple[tuple[str, Any], ...], day_key: str):
    from product_data import load_project

    return load_project(PROJECT_ROOT, base, overrides=dict(overrides_key))


def load_snapshot(base: str = DEFAULT_BASE, overrides: dict[str, Any] | None = None):
    """Shared read-only snapshot for environment and site data tools."""
    return _snapshot(base, tuple(sorted((overrides or {}).items())), _planning_key())


def environment_summary(
    *,
    base: str = DEFAULT_BASE,
    overrides: dict[str, Any] | None = None,
    days: int = 7,
    hourly: bool = False,
    window: str | None = None,
) -> dict[str, Any]:
    """返回从规划时点起 N×24 小时的聚合结果，按自然日分组。"""
    days = max(1, min(int(days or 7), 31))
    snapshot = load_snapshot(base, overrides)
    window_info: dict[str, Any] | None = None
    if window:
        target = None
        if window in {"target", "目标窗口", "本次窗口"}:
            target_id = (snapshot.window_selection or {}).get("目标窗口编号")
        else:
            match = re.search(r"窗口\s*0*(\d+)", window)
            target_id = f"窗口{int(match.group(1)):02d}" if match else window
        target = next(
            (
                item
                for item in snapshot.observation_windows
                if item["编号"] == target_id
            ),
            None,
        )
        if not target:
            raise ValueError(f"没有找到海事窗口：{window}")
        start = int(target["开始"]) // 60
        stop = min(len(snapshot.hourly), target["结束"] // 60)
        days = max(1, round((stop - start) / 24))
        window_info = {
            "id": target["编号"],
            "start": snapshot.stamp(target["开始"]),
            "end": snapshot.stamp(target["结束"]),
            "approval": target.get("审批状态"),
        }
    else:
        start = int(snapshot.now) // 60
        stop = min(len(snapshot.hourly), start + days * 24)
    if start >= len(snapshot.hourly):
        raise ValueError("规划时点之后没有逐时预测数据")

    buckets: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for hour in range(start, stop):
        stamp = snapshot.base + timedelta(hours=hour)
        key = stamp.date().isoformat()
        row = snapshot.hourly[hour]
        bucket = buckets.get(key)
        if bucket is None:
            day_index = (stamp.date() - snapshot.base.date()).days
            weather = (
                snapshot.weather[day_index]
                if 0 <= day_index < len(snapshot.weather)
                else {}
            )
            bucket = {
                "date": key,
                "weekday": _WEEKDAYS[stamp.weekday()],
                "seaState": weather.get("海况类别"),
                "usableHours": weather.get("可用小时"),
                "hours": 0,
                "winds": [],
                "prices": [],
                "energyMwh": 0.0,
                "revenueYuan": 0.0,
            }
            buckets[key] = bucket
            order.append(key)
        bucket["hours"] += 1
        bucket["winds"].append(row["风速"])
        bucket["prices"].append(row["电价"])
        bucket["energyMwh"] += row["功率"]
        bucket["revenueYuan"] += row["功率"] * row["电价"]

    daily = []
    for key in order:
        bucket = buckets[key]
        daily.append(
            {
                "date": bucket["date"],
                "weekday": bucket["weekday"],
                "seaState": bucket["seaState"],
                "usableHours": bucket["usableHours"],
                "hours": bucket["hours"],
                "partial": bucket["hours"] < 24,
                "windMin": round(min(bucket["winds"]), 2),
                "windMean": round(sum(bucket["winds"]) / len(bucket["winds"]), 2),
                "windMax": round(max(bucket["winds"]), 2),
                "priceMin": round(min(bucket["prices"]), 1),
                "priceMean": round(sum(bucket["prices"]) / len(bucket["prices"]), 1),
                "priceMax": round(max(bucket["prices"]), 1),
                "energyMwh": round(bucket["energyMwh"], 1),
                "revenueWan": round(bucket["revenueYuan"] / 10_000, 2),
            }
        )

    meta = snapshot.meta or {}
    selection = snapshot.window_selection or {}
    target_id = selection.get("目标窗口编号")
    target = next(
        (item for item in snapshot.observation_windows if item["编号"] == target_id),
        None,
    )
    summary: dict[str, Any] = {
        "base": base,
        "requestedDays": days,
        "window": window_info,
        "hoursCovered": stop - start,
        "horizonEnd": (
            snapshot.base + timedelta(hours=stop)
        ).strftime("%m-%d %H:%M"),
        "daily": daily,
        "asOf": meta.get("规划时点"),
        "planningBoundary": selection.get("规划边界"),
        "observationEnd": selection.get("观察终点"),
        "targetWindow": (
            {
                "id": target_id,
                "start": snapshot.stamp(target["开始"]),
                "end": snapshot.stamp(target["结束"]),
                "approval": target.get("审批状态"),
            }
            if target
            else None
        ),
        "dataNature": meta.get("数据性质"),
        "source": meta.get("来源"),
        "inputHash": meta.get("输入哈希"),
    }
    if hourly:
        summary["hourly"] = [
            {
                "time": (snapshot.base + timedelta(hours=hour)).isoformat(),
                "wind": round(snapshot.hourly[hour]["风速"], 2),
                "price": round(snapshot.hourly[hour]["电价"], 1),
                "powerMw": round(snapshot.hourly[hour]["功率"], 2),
            }
            for hour in range(start, stop)
        ]
    return summary


def _sparkline(values: list[float]) -> str:
    if not values:
        return ""
    low, high = min(values), max(values)
    if high - low < 1e-9:
        return _BLOCKS[len(_BLOCKS) // 2] * len(values)
    scale = len(_BLOCKS) - 1
    return "".join(
        _BLOCKS[int(round((value - low) / (high - low) * scale))] for value in values
    )


def render_markdown(summary: dict[str, Any]) -> str:
    daily = summary.get("daily") or []
    nature = summary.get("dataNature") or "未标注"
    as_of = str(summary.get("asOf") or "未记录")[:16].replace("T", " ")
    hours = summary.get("hoursCovered") or 0
    end = summary.get("horizonEnd")
    lines = [
        f"**接下来 {hours} 小时的风况与电价**"
        f"（{as_of} 起{f' → {end}' if end else ''}；"
        f"{summary.get('base')}·{nature}）",
        "",
        "| 日期 | 海况 | 可用小时 | 风速 m/s 均（区间） | 电价 元/MWh 均（区间） | 潜在发电 | 折合收入 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in daily:
        label = f"{item['date'][5:]} {item['weekday']}"
        if item["partial"]:
            label += "（部分）"
        usable = item["usableHours"]
        lines.append(
            "| {label} | {sea} | {usable} | {wmean}（{wmin}~{wmax}） | "
            "{pmean}（{pmin}~{pmax}） | {energy} MWh | {revenue} 万元 |".format(
                label=label,
                sea=item["seaState"] or "未记录",
                usable="—" if usable is None else f"{float(usable):.1f}",
                wmean=item["windMean"],
                wmin=item["windMin"],
                wmax=item["windMax"],
                pmean=item["priceMean"],
                pmin=item["priceMin"],
                pmax=item["priceMax"],
                energy=item["energyMwh"],
                revenue=item["revenueWan"],
            )
        )
    if daily:
        lines.extend(
            [
                "",
                f"风速日均：`{_sparkline([item['windMean'] for item in daily])}`",
                f"电价日均：`{_sparkline([item['priceMean'] for item in daily])}`",
            ]
        )
    target = summary.get("targetWindow")
    if target:
        lines.append(
            f"这一周是准备期；规划边界之后的第一个可用窗口是 {target['id']}"
            f"（{str(target['start'])[:16].replace('T', ' ')} → "
            f"{str(target['end'])[:16].replace('T', ' ')}，{target.get('approval') or '待确认'}）。"
        )
    lines.append(
        f"数据来自求解用的同一份基座快照，输入哈希 `{str(summary.get('inputHash'))[:12]}…`；"
        "这是预测数据，不替代出海 go/no-go 授权。"
    )
    return "\n".join(lines)


def render_window_markdown(summary: dict[str, Any]) -> str:
    """A window-anchored view: the weather the optimizer actually plans on."""
    window = summary.get("window") or {}
    daily = summary.get("daily") or []
    nature = summary.get("dataNature") or "未标注"
    start = str(window.get("start") or "")[:16].replace("T", " ")
    end = str(window.get("end") or "")[:16].replace("T", " ")
    total_usable = sum(
        float(item.get("usableHours") or 0) for item in daily
    )
    lines = [
        f"**{window.get('id') or '目标窗口'} 的风况与电价**"
        f"（{start} 至 {end}，{summary.get('base')}·{nature}）",
        "",
        "| 日期 | 海况 | 可用小时 | 风速 m/s 均（区间） | 电价 元/MWh 均（区间） |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in daily:
        label = f"{item['date'][5:]} {item['weekday']}"
        usable = item["usableHours"]
        usable_text = "—" if usable is None else f"{float(usable):.1f}"
        lines.append(
            f"| {label} | {item['seaState'] or '未记录'} | "
            f"{usable_text} | "
            f"{item['windMean']}（{item['windMin']}~{item['windMax']}） | "
            f"{item['priceMean']}（{item['priceMin']}~{item['priceMax']}） |"
        )
    if daily:
        lines.extend(
            [
                "",
                f"窗口内可用小时合计 {total_usable:.1f} 小时；"
                f"日均风速 "
                f"{sum(item['windMean'] for item in daily) / len(daily):.2f} m/s，"
                f"日均电价 "
                f"{sum(item['priceMean'] for item in daily) / len(daily):.1f} 元/MWh。",
                f"风速日均：`{_sparkline([item['windMean'] for item in daily])}`",
                f"电价日均：`{_sparkline([item['priceMean'] for item in daily])}`",
            ]
        )
    lines.append(
        f"数据来自求解用的同一份基座快照，输入哈希 `{str(summary.get('inputHash'))[:12]}…`；"
        "窗口是已批准的海事窗口，天气与电价是预测值；不替代出海 go/no-go 授权。"
    )
    return "\n".join(lines)


def build_chart_spec(
    summary: dict[str, Any],
    *,
    default_range: int = 7,
) -> dict[str, Any]:
    """把环境快照压成可随消息持久化的版本化图表契约。"""
    hourly = list(summary.get("hourly") or [])[: 7 * 24]
    daily = list(summary.get("daily") or [])[:30]
    complete_hourly_days = len(hourly) // 24
    available_ranges = [
        days
        for days in (3, 7, 14, 30)
        if (days <= 7 and complete_hourly_days >= days)
        or (days > 7 and len(daily) >= days)
    ]
    if not available_ranges:
        available_ranges = [min(7, max(1, len(daily)))]
    if default_range not in available_ranges:
        lower = [days for days in available_ranges if days <= default_range]
        default_range = lower[-1] if lower else available_ranges[0]

    target = summary.get("targetWindow")
    return {
        "schemaVersion": "environment-chart.v1",
        "title": "风速与电价",
        "dataNature": summary.get("dataNature"),
        "source": summary.get("source"),
        "inputHash": summary.get("inputHash"),
        "asOf": summary.get("asOf"),
        "timezone": "Asia/Shanghai",
        "defaultRange": default_range,
        "availableRanges": available_ranges,
        "units": {
            "wind": "m/s",
            "price": "元/MWh",
            "power": "MW",
            "usableHours": "小时",
        },
        "hourly": hourly,
        "daily": [
            {
                "date": item.get("date"),
                "windMean": item.get("windMean"),
                "priceMean": item.get("priceMean"),
                "seaState": item.get("seaState"),
                "usableHours": item.get("usableHours"),
            }
            for item in daily
        ],
        "annotations": {
            "planningBoundary": summary.get("planningBoundary"),
            "targetWindow": (
                {
                    "id": target.get("id"),
                    "start": target.get("start"),
                    "end": target.get("end"),
                    "approval": target.get("approval"),
                }
                if isinstance(target, dict)
                else None
            ),
        },
    }


def render_chart_markdown(summary: dict[str, Any], chart_spec: dict[str, Any]) -> str:
    """输出简短文字摘要和机器可读图表块，避免把 30 天表格塞满对话。"""
    default_range = chart_spec["defaultRange"]
    nature = summary.get("dataNature") or "未标注"
    target = summary.get("targetWindow")
    lines = [
        f"**未来 {default_range} 天风速与电价**",
        "",
        f"数据性质：{nature}；图中可切换 3 / 7 / 14 / 30 天。",
    ]
    if target:
        lines.append(
            f"目标窗口：{target['id']}（{str(target['start'])[:16].replace('T', ' ')}"
            f" 至 {str(target['end'])[:16].replace('T', ' ')}）。"
        )
    else:
        lines.append("当前快照没有可标注的目标窗口。")
    lines.extend(
        [
            "",
            "```chart",
            json.dumps(chart_spec, ensure_ascii=False, indent=2),
            "```",
            "",
            f"数据与求解使用同一份基座快照，输入哈希 `{str(summary.get('inputHash'))[:12]}…`。",
        ]
    )
    return "\n".join(lines)
