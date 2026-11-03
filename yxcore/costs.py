"""单条候选的人工费 / 备件费。内核和注入口必须用同一份公式。

为什么单独抽一个文件：这两个口径既要在注入口闭包里建表达式（按组筛候选），
又要在对账时按产物重算（证明约束真的生效了）。两处各写一遍，迟早对不上——
对不上就会变成「约束按 A 算、对账按 B 算」，通过与否全看哪个更松。

公式抄自 `product_planner.build_model`（内核里 labor / spare 两行），不另立口径：
    人工 = 作业段总分钟 / 60 × 人工单价
    备件 = 维修包成本（元每套） + 其他费用（元每任务）
"""
from __future__ import annotations


def money() -> int:
    """内核的金额缩放（micro-yuan）。从内核读，不复制常量。"""
    from product_planner import MONEY  # noqa: PLC0415

    return MONEY


def labor_yuan(data, row: dict) -> float:
    """一条候选的人工费（元）。``row`` 是候选行，含「作业段」。"""
    segments = row.get("作业段") or []
    minutes = sum(b - a for a, b in segments)
    return minutes / 60 * float(data.params["人工单价（元每人时）"])


def spare_yuan(data, row: dict) -> float:
    """一条候选的备件费（元）。"""
    part = data.parts.get(row.get("部件编号") or "", {})
    return float(part.get("维修包成本（元每套）", 0)) + float(part.get("其他费用（元每任务）", 0))
