"""生成器符号层单测（纯 Python，用假候选/假数据）。"""
from __future__ import annotations

from types import SimpleNamespace

from dsl_core.generator import (
    apply_filter,
    per_task_terms,
    quantity_terms,
    resolve_quantity_terms,
)


def _fake_data():
    return SimpleNamespace(
        params={"人工单价（元每人时）": 150.0},
        parts={
            "部件01": {"部件名称": "叶片", "维修包成本（元每套）": 8000.0, "其他费用（元每任务）": 0.0},
            "部件04": {"部件名称": "齿轮箱", "维修包成本（元每套）": 10000.0, "其他费用（元每任务）": 0.0},
        },
        end=91 * 1440,
    )


def _fake_cands():
    """四个候选：任务018 高严重齿轮箱（两个开工时点×两条线）、任务022 中严重叶片。"""
    return [
        {"任务编号": "任务018", "风机编号": "风机001", "部件编号": "部件04", "部件名称": "齿轮箱",
         "严重等级": "高", "窗口编号": "窗口02", "开始": 480, "完成": 840,
         "下项最早开始": 1200, "作业段": [[480, 840]], "作业线": 1},
        {"任务编号": "任务018", "风机编号": "风机001", "部件编号": "部件04", "部件名称": "齿轮箱",
         "严重等级": "高", "窗口编号": "窗口02", "开始": 1920, "完成": 2280,
         "下项最早开始": 2640, "作业段": [[1920, 2280]], "作业线": 2},
        {"任务编号": "任务022", "风机编号": "风机002", "部件编号": "部件01", "部件名称": "叶片",
         "严重等级": "中", "窗口编号": "窗口02", "开始": 960, "完成": 1440,
         "下项最早开始": 1800, "作业段": [[960, 1440]], "作业线": 1},
        {"任务编号": "任务022", "风机编号": "风机002", "部件编号": "部件01", "部件名称": "叶片",
         "严重等级": "中", "窗口编号": "窗口02", "开始": 1920, "完成": 2400,
         "下项最早开始": 2760, "作业段": [[1920, 2400]], "作业线": 2},
    ]


def test_count_with_severity_filter() -> None:
    terms = quantity_terms(_fake_cands(), "count", "one", {"severity": "高"}, _fake_data())
    assert [i for _c, i in terms] == [0, 1]


def test_weighted_sum_finish_coeff() -> None:
    terms = quantity_terms(_fake_cands(), "weighted_sum", "finish", {}, _fake_data())
    assert terms == [(840.0, 0), (2280.0, 1), (1440.0, 2), (2400.0, 3)]


def test_labor_coeff_uses_hourly_rate() -> None:
    # 任务018 齿轮箱：作业段 360 分钟 = 6 小时 × 150 元 = 900 元
    terms = per_task_terms(_fake_cands(), "任务018", "labor", _fake_data())
    assert terms == [(900.0, 0), (900.0, 1)]


def test_material_coeff_reads_part_table() -> None:
    terms = quantity_terms(_fake_cands(), "weighted_sum", "material", {"part": "齿轮箱"}, _fake_data())
    assert terms == [(10000.0, 0), (10000.0, 1)]


def test_filter_by_line() -> None:
    selected = apply_filter(_fake_cands(), {"line": 2}, _fake_data())
    assert selected == [1, 3]


def test_filter_by_day_int() -> None:
    # day = 开始//1440：480//1440=0，1920//1440=1
    selected = apply_filter(_fake_cands(), {"day": 0}, _fake_data())
    assert selected == [0, 2]


def test_named_quantity_expands_to_terms() -> None:
    terms = resolve_quantity_terms(_fake_cands(), "高严重完工时间和", _fake_data())
    assert terms == [(840.0, 0), (2280.0, 1)]


def test_raw_triple_expands_to_terms() -> None:
    ref = {"form": "count", "filter": {"turbine": "风机002"}}
    terms = resolve_quantity_terms(_fake_cands(), ref, _fake_data())
    assert [i for _c, i in terms] == [2, 3]
