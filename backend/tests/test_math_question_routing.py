"""问「公式 / 权重」时不许把成本构成表当答案交出去。

线上实录：连问两次「标准化加权目标的公式？权重？」都拿到同一张
「方案 02 的成本构成」表。根因之一是 read_versions 的 section 枚举里混了
costs，之二是命中「公式」就走原样输出快路径、直接交卷。这里守住参数纠正。
"""
from __future__ import annotations

from app.core.conversation_agent import math_view_arguments


def test_formula_question_with_costs_section_is_corrected() -> None:
    args = {"versions": ["方案02"], "include": ["model"], "section": "costs"}
    fixed = math_view_arguments("标准化加权目标的公式？权重？", "read_versions", args)
    assert fixed["section"] == "objective"
    assert fixed["include"] == ["model"]
    assert fixed["versions"] == ["方案02"]
    assert args["section"] == "costs"  # 原对象不许被就地改坏


def test_formula_question_with_costs_include_is_corrected() -> None:
    fixed = math_view_arguments("加权目标函数的公式？", "read_versions",
                                {"include": ["costs"]})
    assert fixed["section"] == "objective"
    assert fixed["include"] == ["model"]


def test_cost_question_is_left_alone() -> None:
    args = {"include": ["costs"]}
    assert math_view_arguments("方案02 的成本构成是什么", "read_versions", args) is args
    assert math_view_arguments("船租多少钱一天", "read_versions", args) is args


def test_other_tools_are_untouched() -> None:
    args = {"section": "costs"}
    assert math_view_arguments("权重是多少", "read_environment", args) is args


def test_weights_question_hits_the_math_hint() -> None:
    fixed = math_view_arguments("这次的权重分别是多少", "read_versions",
                                {"section": "objective", "include": ["model"]})
    # 没选 costs，原参数应当原样返回（身份相同）。
    assert fixed["section"] == "objective"
