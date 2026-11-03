"""Deterministic routing for site-data lookups.

The conversational model is good at interpreting compound questions, but it is
not allowed to decide whether the product *has* alarm or inventory data.  Pure
lookup questions are answered from the same read-only tools without a model
round-trip; anything that needs comparison, diagnosis or planning stays with the
agent.
"""
from __future__ import annotations

import re
from typing import Any


_ALARM_TOKENS = ("告警", "报警", "缺陷", "故障", "待修", "待安排")
_LOOKUP_TOKENS = (
    "有哪些",
    "哪些",
    "有什么",
    "多少",
    "几个",
    "清单",
    "列表",
    "看看",
    "查一下",
    "查询",
    "现在",
    "当前",
    "今天",
    "昨天",
    "前天",
    "最近",
    "本周",
    "这周",
    "这几天",
    "情况",
    "概况",
    "统计",
    "分布",
    "新增",
    "什么时候",
    "哪天",
    "是什么",
    "有没有",
    "有吗",
    "够不够",
    "够吗",
    "缺不缺",
    "怎么取",
    "数量",
    "截止",
    "上周",
    "本月",
    "历史",
)
_REASONING_TOKENS = (
    "为什么",
    "怎么理解",
    "如何理解",
    "分析",
    "建议",
    "先修",
    "优先修",
    "结合",
    "影响",
    "如果",
    "假设",
    "对比",
    "比较",
    "相比",
    "优劣",
    "最多",
    "排名",
    "排序",
    "占比",
)
_PLANNING_TOKENS = (
    "重排",
    "重新排程",
    "重新安排",
    "调整排程",
    "生成方案",
    "帮我排",
    "求解",
    "改成",
    "设为",
    "调整为",
)
_MATH_TOKENS = ("目标函数", "约束", "权重", "系数", "模型", "公式", "变量")
_VERSION_SCOPED = re.compile(
    r"方案\s*0*\d|这版|这一版|这个方案|该方案|当前方案|ver_[a-f0-9]{8}",
    re.IGNORECASE,
)

_SITE_ROUTES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("inventory", ("库存", "备件", "备品", "领料", "补库", "缺件")),
    ("windows", ("窗口", "海事窗口", "作业窗口", "可用窗口")),
    (
        "fleet",
        (
            "风机台账",
            "风机清单",
            "哪些风机",
            "多少台风机",
            "额定功率",
            "风机型号",
            "风机状态",
            "设备状态",
            "运行状态",
        ),
    ),
    (
        "components",
        (
            "部件库",
            "部件参数",
            "维修工时",
            "维修要多久",
            "维修多久",
            "哪些部件",
            "部件清单",
            "部件名称",
        ),
    ),
    ("work_orders", ("检修单", "工单", "出海清单", "派工")),
    ("execution", ("执行反馈", "实际完工", "实际开工", "退料", "修完没", "执行情况")),
    (
        "parameters",
        (
            "船租",
            "人工单价",
            "作业线",
            "人员上限",
            "系统参数",
            "转移小时",
            "维修时间",
            "建模参数",
            "参数总表",
            "粒度",
        ),
    ),
    (
        "overview",
        ("数据来源", "数据性质", "数据时点", "规划时点", "数据概况", "有哪些数据", "数据范围"),
    ),
)


def _text(message: str) -> str:
    return re.sub(r"\s+", "", message or "")


def _has_any(text: str, tokens: tuple[str, ...]) -> bool:
    return any(token in text for token in tokens)


def detect_operations_question(message: str) -> tuple[str, str | None] | None:
    """Return ("alarms", None) or ("site", scope) for a pure data lookup."""
    text = _text(message)
    if not text:
        return None
    if _VERSION_SCOPED.search(text) or _has_any(text, _MATH_TOKENS):
        return None
    if _has_any(text, _PLANNING_TOKENS):
        return None
    if _has_any(text, _REASONING_TOKENS):
        return None

    if _has_any(text, _ALARM_TOKENS):
        if _has_any(text, _LOOKUP_TOKENS):
            return ("alarms", None)
        if text in {"告警", "当前告警", "现在告警", "告警情况"}:
            return ("alarms", "summary")
        return None

    if "数据" in text and any(
        token in text for token in ("真实", "合成", "来源", "可靠", "哪来", "精度")
    ):
        return ("site", "overview")
    if "风机" in text and "状态" in text:
        return ("site", "fleet")
    if "设备" in text and "状态" in text:
        return ("site", "fleet")
    if "设备" in text and any(token in text for token in ("在线", "离线", "运行")):
        return ("site", "fleet")
    if "风机" in text and any(token in text for token in ("在线", "离线", "运行")):
        return ("site", "fleet")
    if any(
        name in text
        for name in (
            "叶片",
            "轮毂",
            "主轴",
            "齿轮箱",
            "发电机",
            "偏航电机",
            "变桨电机",
            "变频器",
        )
    ) and any(token in text for token in ("维修", "工时", "多久")):
        return ("site", "components")

    for scope, tokens in _SITE_ROUTES:
        if not _has_any(text, tokens):
            continue
        if _has_any(text, _LOOKUP_TOKENS):
            return ("site", scope)
        # Bare domain nouns such as “风机台账”“部件参数”“系统参数” are
        # themselves a request for the list; “窗口修什么” is a plan question.
        if any(
            token in text
            for token in (
                "台账",
                "清单",
                "参数",
                "库存",
                "备件",
                "检修单",
                "执行反馈",
                "出海清单",
            )
        ):
            if any(token in text for token in ("修什么", "任务", "方案")):
                return None
            return ("site", scope)
    return None


def _days(text: str) -> int:
    if "今天" in text or "当日" in text:
        return 1
    if "一个月" in text or "30天" in text or "四周" in text:
        return 30
    if "两周" in text or "14天" in text or "半个月" in text:
        return 14
    if "三天" in text or "3天" in text:
        return 3
    if "一周" in text or "7天" in text or "本周" in text or "这周" in text:
        return 7
    if "上周" in text:
        return 7
    if "本月" in text or "这个月" in text:
        return 30
    match = re.search(r"(\d{1,2})\s*天", text)
    if match:
        return max(1, min(int(match.group(1)), 30))
    return 7


def alarm_arguments(message: str) -> dict[str, Any]:
    text = _text(message)
    arguments: dict[str, Any] = {}
    explicit_date = re.search(r"(20\d{2}-\d{2}-\d{2})", text)
    if explicit_date:
        arguments["scope"] = "recent"
        arguments["date"] = explicit_date.group(1)
    elif "昨天" in text:
        arguments["scope"] = "recent"
        arguments["date"] = "yesterday"
    elif "前天" in text:
        arguments["scope"] = "recent"
        arguments["date"] = "前天"
    elif "今天" in text:
        arguments["scope"] = "recent"
        arguments["date"] = "today"
    elif any(
        token in text
        for token in ("最近", "新增", "本周", "这周", "这几天", "上周", "本月", "这个月")
    ):
        arguments["scope"] = "recent"
        arguments["days"] = _days(text)
    elif any(
        token in text
        for token in ("概况", "统计", "分布", "情况", "重复", "最早", "最晚")
    ):
        arguments["scope"] = "summary"
    else:
        arguments["scope"] = "backlog"

    if "高" in text or "严重" in text:
        arguments["severity"] = "高"
    elif "中" in text and "告警" in text:
        arguments["severity"] = "中"
    elif "低" in text and "告警" in text:
        arguments["severity"] = "低"

    if "待补充窗口" in text:
        arguments["status"] = "待补充窗口"
    elif "待安排" in text:
        arguments["status"] = "待安排"
    elif "已完成" in text or "修完" in text:
        arguments["status"] = "已完成"
        arguments["includeClosed"] = True
    elif "已取消" in text:
        arguments["status"] = "已取消"
        arguments["includeClosed"] = True
    elif "历史" in text or "全部告警" in text:
        arguments["includeClosed"] = True

    task = re.search(r"任务\s*0*(\d{2,4})", text)
    if task:
        arguments["taskId"] = f"任务{int(task.group(1)):03d}"
    turbine = re.search(r"风机\s*0*(\d{1,3})", text)
    if turbine:
        arguments["turbineId"] = f"风机{int(turbine.group(1)):03d}"
    for name in (
        "叶片",
        "轮毂",
        "主轴",
        "齿轮箱",
        "发电机",
        "偏航电机",
        "变桨电机",
        "变频器",
    ):
        if name in text:
            arguments["component"] = name
            break
    return arguments
