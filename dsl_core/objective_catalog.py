"""目标模板目录，唯一事实来源是 references/四种目标模板.json。

对话能力回答、意图编译器的可加权指标校验、求解器模板都读这一份文件，
不在 Python 里再手抄策略名、指标名或默认权重，避免三份口径互相漂移。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


REGISTRY_PATH = (
    Path(__file__).resolve().parents[1]
    / "maintenance-milp-modeler"
    / "references"
    / "四种目标模板.json"
)

WEIGHTED_MODE = "加权"
LAYERED_MODE = "分层"

_DIRECTION_WORDS = {"minimize": "最小化", "maximize": "最大化"}


class CatalogError(RuntimeError):
    """目标模板缺失或结构不合法。"""


@lru_cache(maxsize=1)
def _registry() -> dict[str, Any]:
    if not REGISTRY_PATH.exists():
        raise CatalogError(f"目标模板文件不存在：{REGISTRY_PATH}")
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def template_version() -> str:
    return str(_registry().get("模板版本") or "")


def strategies() -> tuple[str, ...]:
    return tuple(item["模板编号"] for item in _registry()["模板"])


def is_strategy(name: str) -> bool:
    return name in strategies()


def template(strategy: str) -> dict[str, Any]:
    for item in _registry()["模板"]:
        if item["模板编号"] == strategy:
            return item
    raise CatalogError(f"未知目标策略：{strategy}")


def weighted_defaults(strategy: str) -> dict[str, float]:
    return dict(template(strategy)["加权默认值"])


def weighted_metrics(strategy: str) -> tuple[str, ...]:
    return tuple(template(strategy)["加权默认值"])


def layered_objectives(strategy: str) -> tuple[tuple[str, str], ...]:
    return tuple(
        (item["指标"], item["方向"]) for item in template(strategy)["分层目标"]
    )


def all_weighted_metrics() -> tuple[str, ...]:
    seen: list[str] = []
    for strategy in strategies():
        for name in weighted_metrics(strategy):
            if name not in seen:
                seen.append(name)
    return tuple(seen)


def strategies_with_metric(metric: str) -> tuple[str, ...]:
    return tuple(
        strategy for strategy in strategies() if metric in weighted_metrics(strategy)
    )


def shared_weighted_metrics() -> tuple[str, ...]:
    """所有策略都能加权的指标，只有这些才能无条件使用。"""
    shared = set(weighted_metrics(strategies()[0]))
    for strategy in strategies()[1:]:
        shared &= set(weighted_metrics(strategy))
    return tuple(name for name in all_weighted_metrics() if name in shared)


def catalog_markdown() -> str:
    """给对话 Agent 的能力目录，短、全、可直接引用。"""
    lines = [
        f"可选优化策略（目标模板 v{template_version()}，共 {len(strategies())} 种）："
    ]
    for index, strategy in enumerate(strategies(), start=1):
        layered = " → ".join(
            f"{name}（{_DIRECTION_WORDS.get(sense, sense)}）"
            for name, sense in layered_objectives(strategy)
        )
        defaults = weighted_defaults(strategy)
        weighted = "、".join(
            f"{name}（默认 {value:.2f}）" for name, value in defaults.items()
        )
        lines.append(f"{index}. {strategy}")
        lines.append(f"   分层顺序：{layered}")
        lines.append(f"   可加权指标：{weighted}")
    shared = "、".join(shared_weighted_metrics())
    lines.append("")
    lines.append("加权规则：")
    lines.append(
        "- 加权模式必须覆盖所选策略的全部可加权指标；用户没提到的指标按 0 处理。"
    )
    lines.append(f"- 四种策略共同可加权的指标只有：{shared}。其余指标只在部分策略里存在，"
                 "不能跨策略混用。")
    lines.append("- 权重是有限非负数，至少一个大于 0。")
    lines.append(
        "- 目标模式只有两种：分层（按顺序逐级最优）和加权（按权重合并成单目标）。"
    )
    return "\n".join(lines)


def weight_table_markdown() -> str:
    """能力问句的直答版：一张表，先说共同项，再说各策略专属项。"""
    lines = [
        f"**可加权的目标指标**（目标模板 v{template_version()}，共 {len(strategies())} 种策略）",
        "",
        "| 策略 | 可加权指标（默认权重） |",
        "| --- | --- |",
    ]
    for strategy in strategies():
        defaults = weighted_defaults(strategy)
        cell = "、".join(f"{name} {value:.2f}" for name, value in defaults.items())
        lines.append(f"| {strategy} | {cell} |")
    shared = "、".join(shared_weighted_metrics())
    lines.extend(
        [
            "",
            f"四种策略都能加权的是：{shared}。",
            "其他指标只属于部分策略，换策略就不能沿用。",
            "",
            "加权时要么报全该策略的指标，要么只报关心的几项——没提到的按 0 处理，"
            "系统会补全后再求解。想改权重直接说，例如“按高风险优先抢修，改成加权，"
            "高严重完成数 0.6，总经济成本 0.4”。",
        ]
    )
    return "\n".join(lines)


def structure_markdown() -> str:
    """没有具体方案时的结构性回答：模板长什么样，而不是这次取了多少。"""
    first = strategies()[0]
    layered = " \\succ ".join(name for name, _ in layered_objectives(first))
    lines = [
        "**目标函数的结构**（来自目标模板，不是某一次求解的取值）",
        "",
        "决策变量 $x_{i,k,l}\\in\\{0,1\\}$ 表示任务 $i$ 在候选 $k$、作业线 $l$ 上执行；"
        "分层模式的写法是字典序：",
        "",
        "$$",
        "\\operatorname{lexmin}\\;\\big(F_1,\\,F_2,\\,\\ldots,\\,F_m\\big)",
        "$$",
        "",
        f"以「{first}」为例，顺序是 {layered}；每一项 $F_j$ 都是同一批 0-1 变量的线性函数，"
        "按顺序逐级求最优，前一级最优值固定后才优化下一级。",
        "",
        "加权模式则把这些指标按权重 $w_j\\ge 0$ 合并成单目标：",
        "",
        "$$",
        "\\min\\;\\sum_{j} w_j\\,F_j",
        "$$",
        "",
        "各策略具体有哪几项、默认权重多少，可以直接问“有哪些目标函数可以设置权重”。"
        "某一次求解实际用了哪些系数，要读那一版的模型产物。",
    ]
    return "\n".join(lines)


def capability_markdown() -> str:
    """完整能力目录：能改什么、能加什么、能怎么说。

    这份目录是能力问句的直答，不依赖任何已生成的方案。
    """
    from .part_catalog import part_catalog_text

    lines = [weight_table_markdown(), "", "**能改的数据**", "",
             "| 类别 | 说法示例 |", "| --- | --- |",
             "| 船租 | 船租改成50万 / 船租上涨20% |",
             "| 人工单价 | 人工单价改成300元 / 人工成本200 / 人工单价上调20% |",
             "| 作业线数 | 最多两条作业线 |",
             "| 人员上限 | 人员总上限设为30人 / 最多30人 / 总人数不超过40 |",
             "| 每日工时 | 每天只能工作8小时 / 每天最多维修10小时 |",
             "| 开工收工时刻 | 每天8点开工 / 下午5点半收工 / 改成06:30开工 |",
             "| 风机间转移 | 转移时间改成3小时 / 风机间转移4小时 |",
             f"| 备件到货 | 叶片备件11月5日到货（现有部件：{part_catalog_text()}） |",
             "| 任务期限 | 严重故障11月8日前必须修完 |",
             "| 部件工时 | 齿轮箱维修工时上涨20% |", "",
             "**能加的限制**", "",
             "| 限制 | 说法示例 |", "| --- | --- |",
             "| 总成本上限 | 总成本控制在800万 |",
             "| 现金支出上限 | 现金支出不超过500万 |",
             "| 必修任务 | T01必须修 |", "",
             "**一次说多个场景**", "",
             '例：“先按默认方法算一遍，再按每天只能工作8小时算一遍，然后对比这两版”',
             "确认后会依次生成多个独立方案，全部算完自动放在一起对比。", "",
             "日期必须落在本次规划窗口内；日期不在窗口内、或比现有期限更晚时，"
             "会直接告诉你这次改动不会影响结果，不会默默算一遍。"]
    return "\n".join(lines)


def validate() -> list[str]:
    """目录自检，测试里断言为空。"""
    problems: list[str] = []
    try:
        registry = _registry()
    except CatalogError as exc:
        return [str(exc)]
    if not registry.get("模板版本"):
        problems.append("缺少模板版本")
    if not registry.get("模板"):
        problems.append("模板列表为空")
        return problems
    known = set(strategies())
    for item in registry["模板"]:
        name = item.get("模板编号")
        defaults = item.get("加权默认值") or {}
        layered = item.get("分层目标") or []
        if not defaults:
            problems.append(f"{name} 缺少加权默认值")
        if not layered:
            problems.append(f"{name} 缺少分层目标")
        layered_names = {entry.get("指标") for entry in layered}
        for metric in defaults:
            if metric not in layered_names:
                problems.append(f"{name} 的加权指标 {metric} 不在分层目标里")
        total = sum(defaults.values())
        if defaults and abs(total - 1.0) > 1e-6:
            problems.append(f"{name} 的默认权重之和为 {total}，不等于 1")
        for entry in layered:
            if entry.get("方向") not in {"minimize", "maximize"}:
                problems.append(f"{name} 的目标 {entry.get('指标')} 方向不合法")
    if len(known) != len(registry["模板"]):
        problems.append("策略名重复")
    return problems
