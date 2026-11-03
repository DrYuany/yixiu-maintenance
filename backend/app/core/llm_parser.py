"""三层解析器：自然语言 → DSL JSON。

    规则层（正则直出）      高频确定性话术，slot 抽取后直接产出 DSL，零成本零延迟
    向量层（模板检索）      本地字符 n-gram 检索"已验证 DSL 模板库"，作为 few-shot
                            喂给 LLM——降低误判、提高一致性（模板曾通过校验+求解）
    LLM 层（生成 + 校验）   新话术兜底，产物必须过确定性校验器（validator）

路由（ParseResult.route）：一句话要么进 DSL，要么不进：
  - dsl          修改类 → 产出 DSL JSON
  - batch        批量多场景（主产品 build_scenario_batch 处理，DSL 不认领）
  - data_patch   行级数据补丁（主产品 planning_intent 处理，DSL 不认领）
  - reject       越界诉求（非线性/无意义数据），明确拒绝，不静默编造
  - not_change   闲聊/查询/解释/对比 → 读工具或直接回答，不进 DSL

确定性边界：解析层可能非确定（LLM），产物必须落库进证据链；"可复现"承诺
从"NL→模型"降级为"DSL→模型"（见设计文档 §7.1）。
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import dataclass, field

from app.core.demo_access import resolve_api_key
from pathlib import Path
from typing import Any, Callable

from dsl_core.validator import validate_dsl

LIBRARY_PATH = Path(__file__).resolve().parents[2] / "eval_cases" / "dsl_validated_library.jsonl"

_TASK = r"(任务\d{1,3}|T\d{1,3})"
# 年作为可选捕获组，保证 groups 索引对齐 (year, month, day)
_MONTH_DAY = r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})[日号]"


def _task_ref(value: str) -> str:
    plain = re.sub(r"^任务|^T", "", value)
    return f"任务{int(plain):03d}"


def _all_task_refs(text: str) -> list[str]:
    """按出现顺序提取全部任务引用（去重）。"""
    seen: list[str] = []
    for raw in re.findall(_TASK, text):
        ref = _task_ref(raw)
        if ref not in seen:
            seen.append(ref)
    return seen


def _month_day(year: str | None, month: str, day: str) -> str:
    return f"2026-{int(month):02d}-{int(day):02d}T08:00:00+08:00"


_CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3}
_SEVERITY_MAP = {"严重故障": "高", "高严重": "高", "高危": "高",
                 "中等故障": "中", "中严重": "中", "低严重": "低"}

_STRATEGY_CANONICAL = {
    "高风险优先抢修": "高风险优先抢修", "高风险优先": "高风险优先抢修", "风险优先": "高风险优先抢修", "省钱第一": "成本最低优先",
    "成本最低优先": "成本最低优先", "成本最低": "成本最低优先", "经济优先": "成本最低优先",
    "成本优先": "成本最低优先", "省钱优先": "成本最低优先",
    "最短完成优先": "最短完成优先", "最短完成": "最短完成优先", "工期优先": "最短完成优先",
    "检修最多器件优先": "检修最多器件优先", "检修最多": "检修最多器件优先", "数量优先": "检修最多器件优先",
}


def _cn_int(value: str) -> int:
    """中文数字转整数；默认值急求值陷阱（dict.get(key, int(x))）需绕过。"""
    if value in _CN_NUM:
        return _CN_NUM[value]
    return int(value)


def _parameter_dsl(kind: str, groups: list[str]) -> dict[str, Any]:
    """参数类规则 → DSL parameters 段（字段名 = 122 项注册表参数名称）。"""
    if kind == "param_ship_rent_rel":
        return {"parameters": [{"field": "船舶日租金", "op": "*", "value": 1 + float(groups[0]) / 100}]}
    if kind == "param_ship_rent_abs":
        value = float(groups[0])
        if groups[1] == "万":
            value *= 10_000
        return {"parameters": [{"field": "船舶日租金", "op": "=", "value": value}]}
    if kind == "param_labor":
        return {"parameters": [{"field": "维修人工单价", "op": "=", "value": float(groups[0])}]}
    if kind == "param_lines":
        return {"parameters": [{"field": "最大作业线", "op": "=", "value": _cn_int(groups[0])}]}
    if kind == "param_daily_hours":
        return {"parameters": [{"field": "每日有效维修上限", "op": "=", "value": float(groups[0])}]}
    if kind == "param_headcount":
        return {"parameters": [{"field": "可调动人员总数", "op": "=", "value": int(groups[0])}]}
    if kind == "param_transfer":
        return {"parameters": [{"field": "风机间转移", "op": "=", "value": float(groups[0])}]}
    if kind == "param_work_start":
        return {"parameters": [{"field": "每天开始维修", "op": "=", "value": f"{int(groups[0]):02d}:00"}]}
    if kind == "param_work_end":
        return {"parameters": [{"field": "每天结束维修", "op": "=", "value": f"{int(groups[0]):02d}:00"}]}
    if kind == "param_granularity":
        return {"parameters": [{"field": "候选开工粒度", "op": "=", "value": int(groups[0])}]}
    if kind == "param_no_multiday":
        return {"parameters": [{"field": "允许跨天暂停", "op": "=", "value": "否"}]}
    if kind == "param_multiday":
        return {"parameters": [{"field": "允许跨天暂停", "op": "=", "value": "是"}]}
    if kind == "param_no_night_transfer":
        return {"parameters": [{"field": "允许夜间转移", "op": "=", "value": "否"}]}
    if kind == "param_weather_threshold":
        level, metric, value = groups
        field = ("常态" if level == "常态" else "安全") + (
            "风速上限" if "风速" in metric else "浪高上限"
        )
        return {"parameters": [{"field": field, "op": "=", "value": float(value)}]}
    return {}


def _data_dsl(kind: str, groups: list[str], text: str) -> dict[str, Any]:
    """数据补丁/时序假设规则 → DSL data 段。"""
    def _date(month: str, day: str) -> str:
        return f"2026-{int(month):02d}-{int(day):02d}"

    if kind == "data_arrival":
        part = (groups[0] or "").strip().rstrip("备件")
        spec: dict[str, Any] = {
            "kind": "part_arrival", "part": part,
            "at": _date(groups[1], groups[2]) + "T08:00:00+08:00",
        }
        if groups[3]:
            spec["quantity"] = int(groups[3])
        if re.search(r"(?:之前|前)(?:不能|不可|禁止|无法|不许|不得)(?:开工|修|用|施工|安排)", text):
            spec["blockBefore"] = True
        return {"data": [spec]}
    if kind == "data_part_hours":
        return {"data": [{
            "kind": "part_hours", "part": (groups[0] or "").strip(),
            "factor": 1 + float(groups[1]) / 100,
        }]}
    if kind == "data_deadline":
        spec = {"kind": "task_deadline",
                "at": _date(groups[1], groups[2]) + "T08:00:00+08:00"}
        if groups[0]:
            spec["severity"] = _SEVERITY_MAP.get(groups[0], groups[0])
        return {"data": [spec]}
    if kind == "data_weather_wind":
        return {"data": [{"kind": "weather", "day": _date(groups[0], groups[1]),
                          "wind": float(groups[2])}]}
    if kind == "data_weather_state":
        return {"data": [{"kind": "weather", "day": _date(groups[0], groups[1]),
                          "state": groups[2]}]}
    return {}


_QUESTION_HINTS = re.compile(r"[吗么呢][?？]?$|是不是|能不能|行不行|什么时候|多久|怎么")


_METRIC = r"(高严重完成数|中严重完成数|低严重完成数|完工时间和|完工时间|总经济成本|经济成本|现金支出|完成任务数|完成数|成本)"
_METRIC_ALIASES = {
    "成本": "总经济成本", "经济成本": "总经济成本",
    "完工时间": "最后完工时间", "完成数": "完成任务数",
}
_DIR_ALIASES = {"最小化": "minimize", "降低": "minimize", "压缩": "minimize", "最大化": "maximize"}

# ── 规则层：修改类正则 + slot 抽取 ────────────────────────────────────────────

RULE_PATTERNS: list[tuple[str, str]] = [
    # ── 参数类（parameters 段；字段名 = 122 项注册表的参数名称）──
    (r"船租(?:上涨|上调|涨)\s*(\d+(?:\.\d+)?)\s*%", "param_ship_rent_rel"),
    (r"船租(?:改成|改为|设为)\s*(\d+(?:\.\d+)?)\s*(万|元)?", "param_ship_rent_abs"),
    (r"人工(?:单价|成本|费用|费|工资|工钱)\s*(?:改成|改为|设为|调整为|涨到|降到|上调|下调|提高|降低)?\s*(\d+(?:\.\d+)?)\s*(?:元|块钱|块)?", "param_labor"),
    (r"(?:最多|不超过|限定|只上|只剩)\s*([\d一两二三]+)\s*条(?:作业)?(?:线|船)", "param_lines"),
    (r"(?:每天|每日|一天).{0,6}(?:最多|只能|干|工作)(?:维修|作业|施工|检修|干活|时间)?\s*(\d+(?:\.\d+)?)\s*小时", "param_daily_hours"),
    (r"(?:每天|每日)?(?:有效)?工时.{0,4}(?:改为|改成|设为|调整为|调到)\s*(\d+(?:\.\d+)?)\s*小时", "param_daily_hours"),
    (r"(?:人员|人手|工人|施工人员|作业人员)(?:总)?(?:人数|人员|人力|上限)\s*(?:改成|设为|为)?\s*(\d+)\s*人", "param_headcount"),
    (r"(?:最多|不超过|限定|只有)\s*(\d+)\s*人", "param_headcount"),
    (r"(?:风机间转移|转移时间|转移|换风机).{0,4}(?:改成|要|用|留|需要)?\s*(\d+(?:\.\d+)?)\s*小时", "param_transfer"),
    (r"每天\s*(\d{1,2})点(?:开工|开始|干活)", "param_work_start"),
    (r"每天\s*(\d{1,2})点(?:收工|结束|下班)", "param_work_end"),
    # ── 生成层规则参数（第三期）──
    (r"(?:候选)?开工粒度(?:\s*(?:改成|改为|设为|调整为|调到|为)\s*)?(\d+)\s*分钟", "param_granularity"),
    (r"(?:不允许|禁止|不能)跨天(?:暂停|施工|维修)?", "param_no_multiday"),
    (r"允许跨天(?:暂停|施工|维修)?", "param_multiday"),
    (r"(?:不能|禁止|不允许)夜间转移", "param_no_night_transfer"),
    (r"(常态|安全)(?:最大)?(风速|浪高|波高).{0,4}(?:改成|设为|调到|调整为|为)\s*(\d+(?:\.\d+)?)", "param_weather_threshold"),
    # ── 数据补丁（data 段；行级）──
    (r"(.{0,6}?)(\d{1,2})月(\d{1,2})[日号](?:之前|前)?(?:才|能)?到货(?:(\d+)\s*(?:套|个|件))?", "data_arrival"),
    (r"(.{0,6})维修工时(?:上涨|上调|增加|提高|翻倍)\s*(\d+(?:\.\d+)?)\s*%", "data_part_hours"),
    # ── 时序假设（data 段；第二期核心新增）──
    (r"(\d{1,2})月(\d{1,2})[日号].{0,4}风速\s*(\d+(?:\.\d+)?)\s*(?:米每秒|米/秒|m/s)?", "data_weather_wind"),
    (r"(\d{1,2})月(\d{1,2})[日号].{0,6}(?:海况|天气).{0,6}(不可作业|恶劣但安全|常态)", "data_weather_state"),
    # ── 约束/目标类（原有）──
    # 自定义分层目标："先最小化完工时间和，再最小化成本"（lexmin，不是批量）
    (rf"(?:先|首先|优先).*?(最小化|最大化|降低|压缩).*?{_METRIC}.*?(?:再|然后|接着).*?(最小化|最大化|降低|压缩).*?{_METRIC}", "lexmin"),
    # 互斥：不能/别/不要 同一天、同时
    (rf"{_TASK}[和与、,，]{_TASK}.*?(?:不能|不要|别).*?(?:同[一1]天|同时|一起)", "co_occur_diff_day"),
    # 先后：先修 A 再修 B；A 修完才能动 B；A 在 B 之前
    (rf"先修?{_TASK}.*?再修?{_TASK}", "precedence"),
    (rf"{_TASK}.*?(?:修完|完成).{{0,4}}(?:才能|再|之后).{{0,4}}(?:动|修|开工).{{0,4}}{_TASK}", "precedence"),
    (rf"{_TASK}.*?必须.*?在{_TASK}.*?之前", "precedence_after"),
    # 最早开工（有日期的才算修改；"最早什么时候"是问句，被问句守卫拦截）
    (rf"{_TASK}.*?最早.{{0,8}}?{_MONTH_DAY}", "earliest"),
    (rf"{_TASK}.*?(?:不得|不能)早于.{0,8}?{_MONTH_DAY}", "earliest"),
    (r"(\d{3})\s*(?:最早|不得早于|不能早于).{0,8}?(\d{1,2})月(\d{1,2})[日号]", "earliest_bare"),
    # 推迟/延后/顺延 到某日 → earliest（相对"顺延 N 天"无基准需澄清，绝对日期可规则化）
    (rf"{_TASK}.*?(?:推迟到|延后到|顺延到|推迟至|延至|推到).{{0,4}}?{_MONTH_DAY}", "earliest"),
    # 必修
    (rf"{_TASK}.*?(?:必须|一定|务必).*?(?:修|安排|完成|做)", "must"),
    # 排除
    (rf"(?:别排|不要排|排除|不排){_TASK}", "exclude"),
    # 固定作业线：只能上 N 号船/线；用 N 号线
    (rf"{_TASK}.*?只能.{{0,6}}(\d+)号(?:船|线)", "fix"),
    (rf"{_TASK}.*?(?:用|上)(\d+)号(?:船|线)", "fix"),
    # 期限：最晚 X 月 Y 日前修完；X 月 Y 日前修完（带任务引用的走约束，不带走数据补丁）
    (rf"{_TASK}.*?(?:最晚|最迟).*?{_MONTH_DAY}.*?(?:前|之前).*?(?:修完|完成|修好)", "deadline"),
    (rf"{_TASK}.*?{_MONTH_DAY}.*?(?:前|之前).*?(?:修完|完成|修好|搞定)", "deadline"),
    (r"(\d{3})\s*必须.{0,6}(\d{1,2})月(\d{1,2})[日号](?:前|之前).*?(?:修完|完成|修好|搞定)", "deadline_bare"),
    (r"(严重故障|高严重|高危|中等故障|中严重|低严重)?(\d{1,2})月(\d{1,2})[日号](?:前|之前)(?:必须|得|要)?(?:修完|完成|修好|搞定)", "data_deadline"),
    # 禁开工日期
    (rf"{_MONTH_DAY}.*?(?:别开工|不要开工|禁止开工)", "forbidden_dates"),
    # 预算（时间量）：完工时间和/完成数 + 不超过/最多/压到 + 数值
    (rf"(完工时间和|完成数|完成任务数).*?(?:不超过|最多|压到|别超过)\s*(\d+)", "budget"),
    # 预算（成本量）：总经济成本/现金支出 ≤ N 万
    (r"(总经济成本|总成本|现金支出|现金预算).{0,8}?(?:不超过|最多|控制在|别超过|以内)\s*(\d+(?:\.\d+)?)\s*(万)?", "budget_cost"),
    # 策略快捷方式（严格前缀/全句，避免比较问句误判）
    (r"(?:按|改成|改为|用|采用)\s*(高风险优先抢修|高风险优先|风险优先|成本最低优先|成本最低|经济优先|成本优先|最短完成优先|最短完成|工期优先|检修最多器件优先|检修最多|数量优先|省钱优先|省钱第一)", "strategy_name"),
    (r"^(高风险优先抢修|高风险优先|风险优先|成本最低优先|成本最低|经济优先|成本优先|最短完成优先|最短完成|工期优先|检修最多器件优先|检修最多|数量优先|省钱优先|省钱第一)(?:优先|第一)?\s*$", "strategy_name"),
    # 计数下界：至少完成 N 项
    (rf"(?:至少|最少|不低于).{0,4}(?:完成|修|排).{0,6}(\d+)\s*项", "cardinality_min"),
]


def parse_by_rules(message: str) -> dict[str, Any] | None:
    """规则层：单约束快速通道。

    收集全部匹配：
      - 只产出一个不同 DSL → 直接返回（高频、确定、零成本）
      - 产出多个不同约束 → 视为复合句，返回 None 交给 LLM
        （旧实现只取第一个匹配，会静默丢掉后面的约束）
    """
    text = message.strip()
    if _QUESTION_HINTS.search(text):
        return None
    results: list[tuple[int, int, dict[str, Any]]] = []
    for pattern, kind in RULE_PATTERNS:
        match = re.search(pattern, text)
        if not match:
            continue
        dsl = _rule_dsl(kind, list(match.groups()), text)
        if dsl:
            start, end = match.span()
            results.append((start, end, dsl))
    if not results:
        return None
    # 跨度包含剔除：「不允许跨天暂停」里同时命中「允许跨天暂停」（否定式包含肯定子串）
    kept = [
        item for item in results
        if not any(
            other[0] <= item[0] and item[1] <= other[1] and (other[1] - other[0]) > (item[1] - item[0])
            for other in results
        )
    ]
    unique: list[dict[str, Any]] = []
    for *_span, dsl in kept:
        if dsl not in unique:
            unique.append(dsl)
    if not unique:
        return None
    return unique[0] if len(unique) == 1 else None


def _rule_dsl(kind: str, groups: list[str], text: str) -> dict[str, Any] | None:
    """单个规则命中 → DSL 片段。"""
    if True:
        if kind.startswith("param_"):
            return _parameter_dsl(kind, groups)
        if kind.startswith("data_"):
            return _data_dsl(kind, groups, text)
        if kind == "lexmin":
            dir1, metric1, dir2, metric2 = groups
            stages = [
                {"q": _METRIC_ALIASES.get(metric1, metric1), "dir": _DIR_ALIASES[dir1]},
                {"q": _METRIC_ALIASES.get(metric2, metric2), "dir": _DIR_ALIASES[dir2]},
            ]
            return {"objectives": [{"type": "lexmin", "stages": stages}]}
        if kind == "strategy_name":
            return {"objectives": [{"type": "strategy",
                                    "name": _STRATEGY_CANONICAL.get(groups[0], "成本最低优先")}]}
        if kind == "earliest_bare":
            task, month, day = groups
            return {"constraints": [{
                "type": "earliest", "task": f"任务{int(task):03d}",
                "time": _month_day(None, month, day),
            }]}
        if kind == "deadline_bare":
            task, month, day = groups
            return {"constraints": [{
                "type": "deadline", "task": f"任务{int(task):03d}",
                "time": _month_day(None, month, day),
            }]}
        if kind == "budget_cost":
            metric, value, unit = groups
            metric = {"总成本": "总经济成本", "现金预算": "现金支出"}.get(metric, metric)
            amount = float(value) * (10_000 if unit == "万" else 1)
            return {"constraints": [{
                "type": "budget", "quantity": metric, "op": "<=", "value": amount,
            }]}
        if kind == "co_occur_diff_day":
            return {"constraints": [{
                "type": "co_occur", "mode": "diff_day",
                "tasks": [_task_ref(groups[0]), _task_ref(groups[1])],
            }]}
        if kind == "precedence":
            return {"constraints": [{
                "type": "precedence",
                "before": _task_ref(groups[0]), "after": _task_ref(groups[1]),
            }]}
        if kind == "precedence_after":
            return {"constraints": [{
                "type": "precedence",
                "before": _task_ref(groups[1]), "after": _task_ref(groups[0]),
            }]}
        if kind == "earliest":
            task, year, month, day = groups[0], groups[1], groups[2], groups[3]
            return {"constraints": [{
                "type": "earliest", "task": _task_ref(task),
                "time": _month_day(year, month, day),
            }]}
        if kind == "must":
            tasks = _all_task_refs(text)
            if len(tasks) > 1:
                return {"constraints": [{"type": "must", "task": task} for task in tasks]}
            return {"constraints": [{"type": "must", "task": _task_ref(groups[0])}]}
        if kind == "exclude":
            tasks = _all_task_refs(text)
            if len(tasks) > 1:
                return {"constraints": [{"type": "exclude", "task": task} for task in tasks]}
            return {"constraints": [{"type": "exclude", "task": _task_ref(groups[0])}]}
        if kind == "fix":
            return {"constraints": [{
                "type": "fix", "task": _task_ref(groups[0]), "line": int(groups[1]),
            }]}
        if kind == "deadline":
            task, year, month, day = groups[0], groups[1], groups[2], groups[3]
            return {"constraints": [{
                "type": "deadline", "task": _task_ref(task),
                "time": _month_day(year, month, day),
            }]}
        if kind == "forbidden_dates":
            year, month, day = groups[0], groups[1], groups[2]
            return {"constraints": [{
                "type": "forbidden_dates",
                "dates": [_month_day(year, month, day)],
            }]}
        if kind == "budget":
            metric, value = groups
            metric = _METRIC_ALIASES.get(metric, metric)
            return {"constraints": [{
                "type": "budget", "quantity": metric, "op": "<=", "value": int(value),
            }]}
        if kind == "cardinality_min":
            return {"constraints": [{
                "type": "cardinality", "filter": {}, "op": ">=", "value": int(groups[0]),
            }]}
    return None


# ── 意图门控：批量 / 数据补丁 / 参数修改 / 越界 / 非修改 ─────────────────────

_BATCH_RE = re.compile(
    r"(?:先|首先).{0,24}(?:再|然后|接着).{0,24}(?:算|排|求解|对比|比较|看看)"
    r"|两个.{0,6}场景|两种.{0,6}情况|敏感性"
    r"|(?:各|分别)(?:算|排)(?:一版|一遍|一次)"
)

_DATA_PATCH_RE = re.compile(
    r"到货|维修工时.{0,4}(?:上涨|翻倍|增加|提高)|(?:更费时|耗时变长|修得更久)"
    r"|(?:严重故障|严重告警|高严重|高危|中等故障|中等告警|低严重).{0,8}"
    r"(?:\d{1,2}月)?\d{1,2}[日号].{0,6}(?:前|之前).{0,4}(?:修完|完成|修好|搞定)"
    r"|补库延期"
)

# 参数类修改（主产品 planning_intent 处理，DSL 不认领但门控必须认）
_PARAMETER_RE = re.compile(
    r"船租.{0,6}(?:改成|改为|设为|上涨|上调|翻倍|翻一番|翻一倍|涨|增加)"
    r"|人工.{0,6}(?:改成|改为|设为|上涨|上调|涨|增加)"
    r"|(?:最多|不超过|限定|只上|只剩)\s*[\d一两二三]+\s*(?:条|人|小时)"
    r"|(?:少|减)\s*[\d一两二三]+\s*(?:条|人)"
    r"|每天.{0,6}(?:最多|只能|干|工作)\s*\d+.*小时|每天\s*\d+(?:\.\d+)?.*小时"
    r"|人员.{0,6}(?:上限|改成|设为|砍)"
    r"|人手.{0,6}(?:砍|减|少)"
    r"|(?:转移|挪动|换风机).{0,6}(?:改成|要|用)\s*\d+"
    r"|(?:早上|每天)\s*\d+点(?:开工|开始|干活)"
    r"|每天\s*\d+点(?:收工|结束)"
)

# 策略/加权目标（主产品四模板处理，DSL 不认领但门控必须认）
_STRATEGY_RE = re.compile(
    r"(?:按|改成|改为|用|采用).{0,4}(?:高风险优先|风险优先|严重性优先|成本最低|经济优先|成本优先"
    r"|最短完成|工期优先|检修最多|数量优先|省钱)"
    r"|先修严重的|严重的先|早点干完|尽快完成|改成加权|权重|加权模式"
    r"|省钱优先|最短完成优先|检修最多器件优先|数量优先"
)

_ILLEGAL_RE = re.compile(
    r"平方|相乘|除以|绝对值|非线性|方差|标准差"
    r"|碳排放|碳足迹|颜色|外观|声音|噪音|振动|温度"
    r"|天气.{0,4}(?:改|变好|变差|调)"
    r"|最大化.{0,12}距离|最小化.{0,12}距离|距离(?:最大|最小)"
    r"|删掉|删除|报废|卖掉"
)

# 需要澄清的诉求（模糊/涉及未实现能力）：预算引成本量（未实现）、安全应急
_CLARIFY_RE = re.compile(
    r"预算.{0,6}(?:不超过|控制在|别超过|压到)\s*\d"
    r"|台风|保安全|应急模式"
    r"|(?:明天|今天|后天|周[一二三四五六日天]).{0,6}(?:只有|只剩|就)\s*[\d一两二三]+\s*(?:个)?人"
)

NON_CHANGE_PATTERNS: list[tuple[str, str]] = [
    (r"^(你好|您好|嗨|哈喽|hello|hi|在吗|在不在|谢谢|多谢|再见|拜拜)[!！。.～~ ]*$", "chitchat"),
    (r"你(?:叫什么|是谁|是啥|会什么|能干什么|能做什么)", "chitchat"),
    (r"(?:写首诗|讲个笑话|聊聊天|中午吃什么|今天天气|点外卖)", "chitchat"),
    # 对话修复 / 元问题：用户在对"上一轮回答"表达不满或追问，不是排程修改
    (r"没听懂|没听明白|没理解|不理解|你没看懂|说错了|答非所问|答偏了"
     r"|我是说|我说的是|我问的是|我指的是|不是这个意思|你理解错|误会了"
     r"|上一个问题|刚才那个问题|上面那个问题|你答的|你的回答", "repair"),
    (r"为什么|为啥|为何|怎么理解|如何理解|是什么|有哪些|能(?:不能|否)介绍|介绍一下|讲讲|解释一下|什么原理|什么算法|怎么建", "explain"),
    (r"对比|比较|差在哪|差多少|区别|哪个更|哪版更", "compare"),
    (r"多少|哪些|怎么样|如何|有没有|够不够|什么时候|哪天|还剩|当前|现在|最近", "query"),
]


def _classify_not_change_only(text: str) -> str | None:
    """只判"非修改类"（闲聊/解释/对比/查询）——必须先于旧语言桥接，
    否则"经济优先和风险优先差多少"这类比较问句会被当成策略修改。"""
    for pattern, _kind in NON_CHANGE_PATTERNS:
        if re.search(pattern, text.strip()):
            return "not_change"
    return None


def _classify_non_change(message: str) -> str | None:
    """非 DSL 修改类话术的路由分类；不匹配返回 None（交 LLM 层）。"""
    text = message.strip()
    if _BATCH_RE.search(text):
        return "batch"
    if _DATA_PATCH_RE.search(text):
        return "data_patch"
    if _STRATEGY_RE.search(text):
        return "strategy"
    if _PARAMETER_RE.search(text):
        return "parameter"
    if _ILLEGAL_RE.search(text):
        return "reject"
    if _CLARIFY_RE.search(text):
        return "clarify"
    for pattern, _kind in NON_CHANGE_PATTERNS:
        if re.search(pattern, text):
            return "not_change"
    return None


# ── 向量层：本地字符 n-gram 检索（无外部依赖）────────────────────────────────

def _embed(text: str) -> dict[str, float]:
    """字符 2/3-gram 加权向量（L2 归一），纯 Python，无外部依赖。"""
    vec: dict[str, int] = {}
    for n in (2, 3):
        for i in range(len(text) - n + 1):
            gram = text[i:i + n]
            vec[gram] = vec.get(gram, 0) + 1
    norm = math.sqrt(sum(v * v for v in vec.values()))
    if not norm:
        return {}
    return {gram: count / norm for gram, count in vec.items()}


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    return sum(w * b.get(gram, 0.0) for gram, w in a.items())


def load_library(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or LIBRARY_PATH
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def retrieve(message: str, library: list[dict], top_k: int = 3) -> list[dict[str, Any]]:
    """检索 top_k 条与当前话术最相似的已验证模板（附相似度）。"""
    query = _embed(message)
    scored = [
        {**entry, "similarity": _cosine(query, _embed(entry["utterance"]))}
        for entry in library
    ]
    scored.sort(key=lambda item: item["similarity"], reverse=True)
    return scored[:max(1, top_k)]


# ── LLM 层：few-shot 生成 + 确定性校验 ────────────────────────────────────────

_PARSE_SYSTEM = """你是检修排程 DSL 生成器。把用户的排程约束诉求翻译成 DSL JSON，
只允许使用以下约束原语与字段（不要编造原语、字段或指标名）：

原语（type 取值）与字段：
- must: {"task": "任务018"} 或 {"filter": {"severity": "高"}}
- exclude: {"task": "任务018"}
- only: {"filter": {"turbine": "风机001", "part": "叶片"}}  —— 只修范围内任务，范围外一律不排
- at_most_one/at_least_one: {"tasks": ["任务018","任务022"]}
- precedence: {"before": "任务018", "after": "任务022", "gap": 分钟数(可选)}
- co_occur: {"mode": "diff_day"|"same_day", "tasks": ["任务018","任务022"]}
- fix: {"task": "任务018", "line": 1}
- capacity: {"resource": "line"|"turbine"|"part", "time": "2026-11-16", "limit": 2}  —— 只作用在那一天
- cardinality: {"filter": {"severity": "高"}, "op": ">="|"<="|"==", "value": 3}
- deadline: {"task": "任务018", "time": "2026-11-08T08:00:00+08:00"}
- earliest: {"task": "任务018", "time": "2026-11-05T08:00:00+08:00"}
- forbidden_dates: {"dates": ["2026-11-03T08:00:00+08:00"]}
- budget: {"quantity": "完工时间和", "op": "<=", "value": 数值}
- linear: {"terms": [[系数, 量]], "op": "<=", "value": 数值}

filter 只认 task/severity/turbine/part（任务行自己的属性），不要写 day/window/line。
任务编号写全称"任务018"（三位），风机写"风机001"。日期必须带时区。只输出 JSON，不要解释。

除 constraints/objectives 外，还有两段（按需输出，不要编造参数名）：
- parameters：全局参数覆盖，{"field": "122项注册表参数名", "op": "="|"*", "value": 数值}，
  例如 {"field":"船舶日租金","op":"*","value":1.2}、{"field":"最大作业线","op":"=","value":2}。
- data：行级补丁与时序假设：
  {"kind":"part_arrival","part":"叶片","at":"2026-11-05T08:00:00+08:00","quantity":2,"blockBefore":true}
  {"kind":"part_hours","part":"齿轮箱","factor":1.2}  —— 整件工时乘倍率
  {"kind":"part_hours","part":"叶片","severity":"高","hours":15}  —— 定点到高严重度的绝对小时数
  {"kind":"part_hours","part":"叶片","severity":"高","sea":"常态","hours":15}  —— 再定点到某一海况
  part_hours 的 factor 与 hours 只能给一个；severity 取 高/中/低，sea 取 常态/恶劣但安全，不给就是该维全改。
  {"kind":"task_deadline","severity":"高","at":"2026-11-08T08:00:00+08:00"}
  {"kind":"weather","day":"2026-11-03","wind":15.0} 或 {"kind":"weather","day":"2026-11-04","state":"不可作业"}
  {"kind":"hourly","time":"2026-11-03T12:00:00+08:00","price":400}
  {"kind":"window","window":"窗口02","start":"2026-11-01T00:00:00+08:00"}

如果这句话不是排程修改诉求（闲聊、查询、解释、对比），输出 {"route": "not_change", "confidence": 0.9}；
如果是非线性、无意义或越界诉求（如"最小化成本的平方""改颜色"），输出 {"route": "reject", "confidence": 0.9}。

输出必须是统一 JSON 包装（不要只输出 DSL，也不要加 markdown 围栏或解释）：
{"route": "dsl", "confidence": 0.95, "dsl": {约束或目标或参数或数据}}
其中 confidence 是 0~1 的把握度：非常确定写 0.9~1.0，吃不准写 0.5~0.7；吃不准时宁可 route 填 "clarify"（confidence 0.5）。"""


@dataclass
class ParseResult:
    dsl: dict[str, Any] | None
    source: str  # rule / legacy / vector / llm
    route: str = "dsl"  # dsl / batch / data_patch / reject / not_change / clarify
    confidence: float = 1.0
    templates_used: list[str] = field(default_factory=list)
    source_scores: dict[str, float] = field(default_factory=dict)
    latency_ms: float = 0.0


# LLM 置信度低于此值 → 降级为 clarify（EchoMind 的置信度阈值思路）。
LLM_CONFIDENCE_THRESHOLD = 0.6

# 向量直接命中阈值：与已验证模板几乎一致（任务号保留）→ 跳过 LLM。
VECTOR_HIT_THRESHOLD = 0.95

# LLM 解析缓存：重复话术即时返回（EchoMind 的 LRU 缓存思路）。
_PARSE_CACHE: dict[str, ParseResult] = {}
_PARSE_CACHE_MAX = 1000


class ParseUnavailable(RuntimeError):
    """LLM 未配置（无 API key）时抛出，调用方应回退到澄清提问。"""


def _llm_config() -> tuple[str, str, str] | None:
    # 访客自带密钥优先（演示超限后由前端传入），否则用服务端配置的。
    api_key = resolve_api_key(os.environ.get("MINIMAX_API_KEY"))
    if not api_key:
        return None
    return (
        api_key,
        os.environ.get("MINIMAX_MODEL", "deepseek-flash"),
        os.environ.get("MINIMAX_BASE_URL", "https://api.deepseek.com/anthropic"),
    )


def parse_with_llm(
    message: str,
    templates: list[dict[str, Any]],
    client=None,
    system_prompt: str | None = None,
    context_block: str | None = None,
) -> ParseResult:
    """LLM 层：带 few-shot 的生成 + 校验。client 可注入（测试 mock）。

    system_prompt 缺省时用旧的手写 _PARSE_SYSTEM（对照基线）；
    正常调用由 parse() 传入注册表自动生成的槽位说明书。
    context_block 是每轮动态上下文（当前方案/待确认变更/快照清单）。
    """
    config = _llm_config()
    if client is None and config is None:
        raise ParseUnavailable("未配置 MINIMAX_API_KEY，LLM 解析不可用")
    if client is None:
        from anthropic import Anthropic
        import httpx

        api_key, model, base_url = config
        # 本机 Claude Code 代理环境里有 ANTHROPIC_AUTH_TOKEN=PROXY_MANAGED：
        # SDK 在 auth_token=None 时读环境变量并优先于 api_key 发 Bearer 头，
        # 导致真实 key 被劫持（401 ****AGED）。构造时传空串跳过环境读取、
        # 构造后置回 None 让 SDK 走 x-api-key 头。
        # trust_env=False 让 httpx 不走全局 HTTPS_PROXY（deepseek.com 国内直连，走代理反而慢）。
        http_client = httpx.Client(trust_env=False, timeout=60.0)
        client = Anthropic(api_key=api_key, auth_token="", base_url=base_url, http_client=http_client)
        client.auth_token = None
        model_name = model
    else:
        model_name = "mock-model"

    examples = "\n".join(
        f"用户：{entry['utterance']}\nDSL：{json.dumps(entry['dsl'], ensure_ascii=False)}"
        for entry in templates
    )
    user = (
        (f"{context_block}\n\n" if context_block else "")
        + f"以下是已通过校验和求解验证的相似例子：\n\n{examples}\n\n"
        + f"现在把这句话翻译成 DSL JSON：{message}"
    )
    response = client.messages.create(
        model=model_name,
        max_tokens=1000,
        temperature=0.0,
        system=system_prompt or _PARSE_SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    content = "".join(
        block.text for block in response.content if getattr(block, "type", "") == "text"
    ).strip()
    # 容忍 markdown 代码围栏
    content = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.MULTILINE).strip()
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        # LLM 产出空/坏 JSON → 澄清（低置信），不静默不崩溃
        return ParseResult(dsl=None, source="llm", route="clarify", confidence=0.5,
                           source_scores={"llm": 0.0})

    def _conf(raw: Any) -> float:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 0.5
        return max(0.0, min(1.0, value))

    # 统一包装格式：{"route": ..., "confidence": ..., "dsl": ...}；
    # 兼容旧格式：直接输出 DSL JSON（视为 route=dsl、confidence=1.0）。
    if isinstance(payload, dict) and "route" in payload:
        route = str(payload.get("route", "dsl"))
        confidence = _conf(payload.get("confidence", 0.5))
        dsl = payload.get("dsl") if route == "dsl" else None
    else:
        route, confidence, dsl = "dsl", 1.0, payload

    if route in ("not_change", "reject"):
        return ParseResult(dsl=None, source="llm", route=route, confidence=confidence,
                           source_scores={"llm": confidence})
    if route == "clarify":
        return ParseResult(dsl=None, source="llm", route="clarify", confidence=confidence,
                           source_scores={"llm": confidence})
    if not isinstance(dsl, dict):
        return ParseResult(dsl=None, source="llm", route="clarify", confidence=0.5,
                           source_scores={"llm": confidence})
    dsl = _normalize_dsl(dsl)
    errors = validate_dsl(dsl)
    if errors:
        # 产物过不了校验 → 不静默也不抛异常，路由为"澄清"（低置信），
        # 由上层决定重试、换说法或转人工。
        return ParseResult(dsl=None, source="llm", route="clarify", confidence=0.5,
                           source_scores={"llm": confidence})
    if confidence < LLM_CONFIDENCE_THRESHOLD:
        # 低置信产物不可信 → 澄清（EchoMind 阈值降级思路）。
        return ParseResult(dsl=None, source="llm", route="clarify", confidence=confidence,
                           source_scores={"llm": confidence})
    return ParseResult(
        dsl=dsl,
        source="llm",
        route="dsl",
        confidence=confidence,
        templates_used=[entry.get("id", entry.get("utterance", "")) for entry in templates],
        source_scores={"llm": confidence},
    )


_LEGACY_COMPILER = None


def _normalize_dsl(dsl: dict[str, Any]) -> dict[str, Any]:
    """LLM 产物确定性归一化（只补省字段，不猜语义）。

    - parameters 缺 op → 默认 "="（绝对值）；带"涨/降 N%"语义的由提示词要求写 "*"。
    - constraints/objectives/parameters/data 里的 None 值字段剔除。
    """
    out = dict(dsl)
    params = []
    for spec in out.get("parameters") or []:
        if isinstance(spec, dict):
            spec = {k: v for k, v in spec.items() if v is not None}
            spec.setdefault("op", "=")
        params.append(spec)
    if params:
        out["parameters"] = params
    return out


def _legacy_compiler():
    """旧规划语言编译器（正则规则层，第三期并入统一解析器）。"""
    global _LEGACY_COMPILER
    if _LEGACY_COMPILER is None:
        from app.core.planning_intent import PlanningIntentCompiler

        _LEGACY_COMPILER = PlanningIntentCompiler()
    return _LEGACY_COMPILER


def parse_by_legacy(message: str) -> dict[str, Any] | None:
    """旧规划语言 → 统一 DSL（规则层迁移，不是重写正则）。

    完整策略/权重/参数/约束/数据补丁/时限由 planning_intent 的成熟正则
    解析，再经 planning_bridge 转成 DSL schema。
    """
    from dsl_core.planning_bridge import request_to_dsl

    compiler = _legacy_compiler()
    if not compiler.is_planning_request(message):
        return None
    compiled = compiler.compile_detailed(message)
    if compiled.blocked:
        return None
    if not _request_consumed_anything(compiled):
        # 路由认领了这句话，但一个字段都没解析出来——最典型的是
        # "钱不超过300万"：命中路由词「不超过」，预算正则只认「总成本/预算/
        # 现金支出」，于是返回一份纯默认方案，预算凭空消失且置信度 1.0。
        # 这种情况必须交回大模型，不能交给一个"看起来正常"的空结果。
        return None
    return request_to_dsl(compiled.request)


def _request_consumed_anything(compiled) -> bool:
    """规则层是否真的从这句话里取到了东西。

    两条证据取其一：字段相对默认请求有实质变化，或编译过程明确记录了
    "读懂策略"。只看字段变化会误伤"按成本最低优先排"——所读的策略恰好
    就是默认值，字段没变，但这句话是读懂的。
    """
    request = compiled.request
    if getattr(compiled, "consumed", None):
        return True
    from dsl_core.planning_request import PlanningRequest

    blank = PlanningRequest()
    return any((
        request.strategy != blank.strategy,
        request.mode != blank.mode,
        bool(request.weights),
        bool(request.overrides),
        bool(request.relative_overrides),
        bool(request.limits),
        bool(request.data_patches),
        request.seconds != blank.seconds,
    ))


def parse(
    message: str,
    library: list[dict] | None = None,
    client=None,
    on_stage: Callable[[str, str, str, str | None], None] | None = None,
    system_prompt: str | None = None,
    context_block: str | None = None,
) -> ParseResult:
    """解析入口：先尝试复合句拆分合并，失败再走单句主链。"""
    parts = _split_compounds(message)
    if parts:
        results: list[ParseResult] = []
        for part in parts:
            try:
                results.append(
                    _parse_core(
                        part, library=library, client=client,
                        system_prompt=system_prompt, context_block=context_block,
                    )
                )
            except Exception:  # noqa: BLE001 - 某一段解析失败 → 退回整句主链
                results = []
                break
        if results and all(result.route == "dsl" and result.dsl for result in results):
            merged = _merge_dsls([result.dsl for result in results])
            if merged:
                if on_stage:
                    on_stage(
                        "parse.compound",
                        f"复合句拆成 {len(parts)} 条并合并",
                        "done",
                        " ／ ".join(parts)[:90],
                    )
                    on_stage("route", "判断意图路由", "done", "dsl")
                return ParseResult(
                    dsl=merged,
                    source="compound",
                    route="dsl",
                    confidence=min(result.confidence for result in results),
                    source_scores={"compound": 1.0},
                )
    return _parse_core(
        message, library=library, client=client, on_stage=on_stage,
        system_prompt=system_prompt, context_block=context_block,
    )


_SPLIT_RE = re.compile(r"[，,；;。\s]+")


def _split_compounds(message: str) -> list[str]:
    """按逗号/分号/句号切分；问句不拆。"""
    if _QUESTION_HINTS.search(message):
        return []
    parts = [part.strip() for part in _SPLIT_RE.split(message) if len(part.strip()) >= 2]
    return parts if len(parts) >= 2 else []


def _merge_dsls(dsls: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for dsl in dsls:
        for key in ("constraints", "objectives", "parameters", "data"):
            for item in dsl.get(key) or []:
                bucket = merged.setdefault(key, [])
                if item not in bucket:
                    bucket.append(item)
        if dsl.get("solver"):
            merged["solver"] = dsl["solver"]
    return merged


def _parse_core(
    message: str,
    library: list[dict] | None = None,
    client=None,
    on_stage: Callable[[str, str, str, str | None], None] | None = None,
    system_prompt: str | None = None,
    context_block: str | None = None,
) -> ParseResult:
    """三层主链：批量 → 规则 → 旧规划语言桥接 → 意图门控 → 向量 → LLM。

    路由结果：dsl / batch / data_patch / parameter / strategy / reject / not_change / clarify。

    on_stage(id, label, status, detail) 在每层切换处回调（过程可视化 / 评测埋点）。
    system_prompt 缺省时由注册表自动生成（registry/schema_prompt.py）；
    传 None 不会退回手写提示词——只有显式传 "legacy" 才用旧的 _PARSE_SYSTEM。
    """
    def stage(stage_id: str, label: str, status: str = "running", detail: str | None = None) -> None:
        if on_stage:
            on_stage(stage_id, label, status, detail)

    t0 = time.monotonic()

    stage("route", "判断意图路由", "running")
    if _BATCH_RE.search(message):
        stage("route", "判断意图路由", "done", "批量场景")
        return ParseResult(dsl=None, source="rule", route="batch", confidence=1.0,
                           source_scores={"rule": 1.0}, latency_ms=_elapsed(t0))
    ruled = parse_by_rules(message)
    if ruled is not None:
        stage("parse.rule", "规则层命中", "done")
        stage("route", "判断意图路由", "done", "dsl")
        return ParseResult(dsl=ruled, source="rule", route="dsl", confidence=1.0,
                           source_scores={"rule": 1.0}, latency_ms=_elapsed(t0))
    if _classify_not_change_only(message) is not None:
        stage("route", "判断意图路由", "done", "not_change")
        return ParseResult(dsl=None, source="rule", route="not_change", confidence=0.9,
                           source_scores={"rule": 0.9}, latency_ms=_elapsed(t0))
    bridged = parse_by_legacy(message)
    if bridged is not None:
        stage("parse.legacy", "旧规划语言桥接", "done")
        stage("route", "判断意图路由", "done", "dsl")
        return ParseResult(dsl=bridged, source="rule", route="dsl", confidence=1.0,
                           source_scores={"rule": 1.0}, latency_ms=_elapsed(t0))
    classified = _classify_non_change(message)
    if classified in {"not_change", "reject", "clarify"}:
        # 只有确定性能判死的路由才由门控直出。
        stage("route", "判断意图路由", "done", classified)
        return ParseResult(dsl=None, source="rule", route=classified, confidence=0.9,
                           source_scores={"rule": 0.9}, latency_ms=_elapsed(t0))
    # parameter / strategy / data_patch 是旧规划器的职责，但走到这里说明
    # parse_by_legacy 已经解析不了（否则上面就返回了）。继续截走只会让系统
    # 退化成自由聊天；交给注册表驱动的 LLM 填槽才是正确行为。
    library = library if library is not None else load_library()

    # LLM 结果缓存：重复/接近重复的话术即时返回（EchoMind 的 LRU 缓存思路）。
    cache_key = message.strip()
    if cache_key in _PARSE_CACHE:
        result = _PARSE_CACHE[cache_key]
        stage("parse.llm", "缓存命中", "done", result.route)
        stage("route", "判断意图路由", "done", result.route)
        return result

    stage("parse.retrieve", "检索相似模板", "running")
    templates = retrieve(message, library, top_k=3)
    top1 = templates[0] if templates else None
    top_sim = float(top1.get("similarity", 0.0)) if top1 else 0.0
    stage("parse.retrieve", "检索相似模板", "done",
          f"top-{len(templates)} 条（相似度 {top_sim:.2f}）")

    # 向量直接命中：与已验证模板几乎一致（任务号保留）→ 跳过 LLM。
    if top1 is not None and top_sim >= VECTOR_HIT_THRESHOLD:
        result = ParseResult(
            dsl=top1["dsl"], source="vector", route="dsl", confidence=top_sim,
            templates_used=[top1.get("id", top1.get("utterance", ""))],
            source_scores={"vector": top_sim}, latency_ms=_elapsed(t0),
        )
        stage("parse.llm", "向量直接命中", "done", f"相似度 {top_sim:.2f}")
        stage("route", "判断意图路由", "done", "dsl")
        _cache_put(cache_key, result)
        return result

    stage("parse.llm", "调用大模型解析", "running")
    if system_prompt is None:
        # 注册表 → 槽位说明书（L0 目录 + L1 检索详解 + 原语/数据/实体 + 输出契约）
        from dsl_core.registry import schema_prompt as schema_sp

        system_prompt = schema_sp.build_system_prompt(message)
    result = parse_with_llm(
        message, templates, client=client,
        system_prompt=system_prompt, context_block=context_block,
    )
    result.source_scores["vector"] = top_sim
    result.latency_ms = _elapsed(t0)
    stage("parse.llm", "调用大模型解析", "done",
          f"{result.route}（置信度 {result.confidence:.2f}）")
    stage("route", "判断意图路由", "done", result.route)
    _cache_put(cache_key, result)
    return result


def _cache_put(key: str, result: ParseResult) -> None:
    if len(_PARSE_CACHE) >= _PARSE_CACHE_MAX:
        for k in list(_PARSE_CACHE)[:_PARSE_CACHE_MAX // 2]:
            del _PARSE_CACHE[k]
    _PARSE_CACHE[key] = result


def _elapsed(t0: float) -> float:
    return round((time.monotonic() - t0) * 1000, 1)
