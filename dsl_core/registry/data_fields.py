"""输入数据注册表（第二期）：按"表 × 字段"组织的可改数据字段。

第一期只有"系统参数表"（registry/parameters.py 的 122 项 + 消费点）。
第二期统一为六张表的字段注册：每个字段登记 类型/单位/上下界/消费点/能否改。

时序/结构数据（海况/逐时/窗口）的假设修改是本期核心新增——
"明天风变大"这类诉求此前没有任何机制，现在落在 weather/hourly/window 字段上。
"""
from __future__ import annotations

from typing import Any

# 消费点：candidates = 候选生成层（时间维度）；build_model = 模型构建层（成本/组合维度）
DATA_FIELDS: dict[str, dict[str, dict[str, Any]]] = {
    # 部件库（实体行；部件编号定位）
    "部件库": {
        "维修工时": {"type": "number", "unit": "倍率", "min": 0, "consumed_by": "candidates",
                     "changeable": True, "scope": "per_part_severity_sea"},
        "维修包成本": {"type": "number", "unit": "元/套", "min": 0, "consumed_by": "build_model",
                       "changeable": True, "scope": "per_part"},
        "其他费用": {"type": "number", "unit": "元/任务", "min": 0, "consumed_by": "build_model",
                     "changeable": True, "scope": "per_part"},
        "固定维修人数": {"type": "integer", "unit": "人", "consumed_by": "model_assumption",
                         "changeable": False},
        "每任务备件数量": {"type": "integer", "unit": "套", "consumed_by": "model_assumption",
                          "changeable": False},
    },
    # 告警任务（实体行；任务编号定位）
    "告警任务": {
        "截止时间": {"type": "time", "unit": "带时区", "consumed_by": "candidates",
                     "changeable": True, "scope": "per_task_or_severity"},
        "严重等级": {"type": "enum", "values": ["高", "中", "低"], "consumed_by": "candidates",
                     "changeable": True, "scope": "per_task"},
    },
    # 库存（实体行；部件编号 + 时间定位）
    "库存": {
        "到货": {"type": "movement", "unit": "套", "consumed_by": "candidates",
                 "changeable": True, "scope": "per_part_time"},
        "出库": {"type": "movement", "unit": "套", "consumed_by": "candidates",
                 "changeable": True, "scope": "per_part_time"},
    },
    # 每日海况（时序；日期定位）——第二期核心新增
    "海况": {
        "作业环境风速": {"type": "number", "unit": "米/秒", "min": 0, "consumed_by": "candidates",
                         "changeable": True, "scope": "per_day"},
        "有效波高": {"type": "number", "unit": "米", "min": 0, "consumed_by": "candidates",
                     "changeable": True, "scope": "per_day"},
        "安全条件允许维修小时数": {"type": "number", "unit": "小时", "min": 0, "max": 24,
                                  "consumed_by": "candidates", "changeable": True, "scope": "per_day"},
        "海况类别": {"type": "enum", "values": ["常态", "恶劣但安全", "不可作业"],
                     "consumed_by": "candidates", "changeable": True, "scope": "per_day",
                     "note": "直接设定时不再按风速/波高阈值反推"},
    },
    # 逐时预测（时序；小时定位）——第二期核心新增
    "逐时预测": {
        "预测电价": {"type": "number", "unit": "元/兆瓦时", "min": 0, "consumed_by": "build_model",
                     "changeable": True, "scope": "per_hour"},
        "轮毂高度风速": {"type": "number", "unit": "米/秒", "min": 0, "consumed_by": "build_model",
                         "changeable": True, "scope": "per_hour", "note": "改风速会联动重算功率"},
        "单台风机潜在功率": {"type": "number", "unit": "兆瓦", "min": 0, "consumed_by": "build_model",
                            "changeable": True, "scope": "per_hour"},
    },
    # 海事窗口（结构；窗口编号定位）——第二期核心新增
    "海事窗口": {
        "开始时间": {"type": "time", "unit": "带时区", "consumed_by": "candidates",
                     "changeable": True, "scope": "per_window",
                     "note": "修改窗口起止是假设对比，不表示获得新的海事许可"},
        "结束时间": {"type": "time", "unit": "带时区", "consumed_by": "candidates",
                     "changeable": True, "scope": "per_window"},
    },
}


def table_names() -> tuple[str, ...]:
    return tuple(DATA_FIELDS)


def field(table: str, name: str) -> dict[str, Any] | None:
    return DATA_FIELDS.get(table, {}).get(name)


def is_changeable(table: str, name: str) -> bool:
    entry = field(table, name)
    return bool(entry and entry.get("changeable"))


def consumption_point(table: str, name: str) -> str:
    entry = field(table, name)
    return entry.get("consumed_by", "unknown") if entry else "unknown"
