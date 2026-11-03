"""可引用量生成器：DSL 量 → 符号项列表（纯 Python，不依赖 OR-Tools）。

符号层 = [(系数, 候选序号), ...]，对候选列表（candidate dicts）做形式 × 系数 × 过滤。
编译层（compiler.py）再把符号项物化成 cp_model 线性表达式。

候选 dict 的字段（来自 product_planner.build_model 的 choices 展开）：
  任务编号/风机编号/部件编号/部件名称/严重等级/窗口编号/开始/完成/下项最早开始/作业段/作业线
"""
from __future__ import annotations

from typing import Any

from .registry import quantities as qty_reg
from .registry.entities import resolve_minute


def candidate_rows(choices: list[tuple[dict, Any]]) -> list[dict]:
    """从 build_model 的 choices 提取候选行（只读引用，不拷贝变量）。"""
    return [candidate for candidate, _var in choices]


def apply_filter(
    cands: list[dict],
    filter_spec: dict[str, Any],
    data,
) -> list[int]:
    """按过滤维度筛候选，返回候选下标列表。空过滤 = 全部候选。"""
    if not filter_spec:
        return list(range(len(cands)))
    selected: list[int] = []
    day_offsets: dict[str, int] = {}
    for index, c in enumerate(cands):
        ok = True
        for dim, value in filter_spec.items():
            if dim == "task":
                ok = c["任务编号"] == value
            elif dim == "turbine":
                ok = c["风机编号"] == value
            elif dim == "part":
                part = data.parts.get(c["部件编号"])
                ok = value in (c["部件编号"], c["部件名称"], part["部件名称"] if part else None)
            elif dim == "severity":
                ok = c["严重等级"] == value
            elif dim == "window":
                ok = c["窗口编号"] == value
            elif dim == "line":
                ok = c.get("作业线") == value
            elif dim == "day":
                if isinstance(value, str):
                    minute = day_offsets.get(value, "MISSING")
                    if minute == "MISSING":
                        minute = resolve_minute(data, value)
                        day_offsets[value] = minute
                    ok = minute is not None and c["开始"] // 1440 == minute // 1440
                else:
                    ok = c["开始"] // 1440 == value
            else:  # pragma: no cover - 校验器已拦未知维度
                ok = False
            if not ok:
                break
        if ok:
            selected.append(index)
    return selected


def _coeff_value(c: dict, coeff: str, data) -> float:
    """从候选字段提取系数值。"""
    if coeff == "finish":
        return float(c["完成"])
    if coeff == "start":
        return float(c["开始"])
    if coeff == "day":
        return float(c["开始"] // 1440)
    if coeff == "work_hours":
        return float(sum(b - a for a, b in c["作业段"])) / 60.0
    if coeff == "labor":
        hours = sum(b - a for a, b in c["作业段"]) / 60.0
        return hours * float(data.params["人工单价（元每人时）"])
    if coeff == "material":
        return float(data.parts[c["部件编号"]]["维修包成本（元每套）"])
    if coeff == "other_fee":
        return float(data.parts[c["部件编号"]].get("其他费用（元每任务）", 0))
    raise ValueError(f"未知系数类型 {coeff!r}")  # pragma: no cover - 校验器已拦


def quantity_terms(
    cands: list[dict],
    form: str,
    coeff: str,
    filter_spec: dict[str, Any],
    data,
) -> list[tuple[float, int]]:
    """形式 × 系数 × 过滤 → 符号项 [(系数, 候选下标)]。

    仅支持 count 与 weighted_sum（校验器已保证）；max/derived 由调用方在此之前拒绝。
    """
    selected = apply_filter(cands, filter_spec, data)
    if form == "count":
        return [(1.0, index) for index in selected]
    if form == "weighted_sum":
        return [(_coeff_value(cands[index], coeff, data), index) for index in selected]
    raise ValueError(f"原型不支持的形式 {form!r}")  # pragma: no cover - 校验器已拦


def resolve_quantity_terms(
    cands: list[dict],
    ref: object,
    data,
) -> list[tuple[float, int]]:
    """量引用（具名或三元组）→ 符号项。"""
    resolved = qty_reg.resolve_quantity(ref)
    if isinstance(resolved, tuple):
        form, coeff, filter_spec = resolved
        return quantity_terms(cands, form, coeff, filter_spec, data)
    if isinstance(resolved, dict):
        return quantity_terms(
            cands,
            resolved.get("form", "count"),
            resolved.get("coeff", "one"),
            resolved.get("filter", {}),
            data,
        )
    raise ValueError(f"量引用无法解析: {ref!r}")  # pragma: no cover - 校验器已拦


def per_task_terms(
    cands: list[dict],
    task_ref: str,
    coeff: str,
    data,
) -> list[tuple[float, int]]:
    """单任务量（y_i / start_i / finish_i / day_i）= 过滤(任务)× 系数 的符号项。"""
    return quantity_terms(cands, "weighted_sum", coeff, {"task": task_ref}, data)
