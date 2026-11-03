from __future__ import annotations

import pytest

from app.core.operations_query import alarm_arguments, detect_operations_question


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("现在告警的有哪些", ("alarms", None)),
        ("今天有什么告警", ("alarms", None)),
        ("高风险待修有哪些", ("alarms", None)),
        ("库存还有多少", ("site", "inventory")),
        ("备件够不够", ("site", "inventory")),
        ("船租多少钱一天", ("site", "parameters")),
        ("下一个窗口是什么时候", ("site", "windows")),
        ("风机的额定功率是多少", ("site", "fleet")),
        ("叶片维修工时多少", ("site", "components")),
        ("现在有检修单吗", ("site", "work_orders")),
        ("执行反馈有哪些", ("site", "execution")),
        ("数据来源是什么", ("site", "overview")),
        ("数据是真实的吗", ("site", "overview")),
        ("设备在线吗", ("site", "fleet")),
        ("风机001现在运行吗", ("site", "fleet")),
        ("已完成的告警有哪些", ("alarms", None)),
        ("历史告警", ("alarms", None)),
    ],
)
def test_pure_data_lookups_route_deterministically(message, expected) -> None:
    assert detect_operations_question(message) == expected


@pytest.mark.parametrize(
    "message",
    [
        "为什么选窗口02",
        "根据告警重新排程",
        "船租上涨20%，重新安排",
        "方案01的船租是多少",
        "告警里哪些最该先修",
        "库存够不够修完高风险任务，分析一下",
        "目标函数考虑了哪些成本",
    ],
)
def test_reasoning_planning_and_plan_questions_stay_with_the_agent(message) -> None:
    assert detect_operations_question(message) is None


def test_alarm_arguments_extract_recent_range_and_filters() -> None:
    assert alarm_arguments("今天有什么告警") == {"scope": "recent", "date": "today"}
    assert alarm_arguments("最近三天新增告警") == {"scope": "recent", "days": 3}
    assert alarm_arguments("高风险告警有哪些")["severity"] == "高"
    assert alarm_arguments("任务018的告警")["taskId"] == "任务018"
    assert alarm_arguments("风机18有什么告警")["turbineId"] == "风机018"
    assert alarm_arguments("叶片有哪些待安排告警")["component"] == "叶片"
    assert alarm_arguments("现在告警的有哪些")["scope"] == "backlog"
    assert alarm_arguments("昨天有什么告警")["date"] == "yesterday"
    assert alarm_arguments("今天有什么告警")["date"] == "today"
    assert alarm_arguments("2026-10-22 有哪些告警")["date"] == "2026-10-22"
    completed = alarm_arguments("已完成的告警有哪些")
    assert completed["status"] == "已完成"
    assert completed["includeClosed"] is True
    assert alarm_arguments("历史告警")["includeClosed"] is True
