"""工具契约回归：成本构成不能混进「数学模型」的 section 枚举。

线上实录：连问两次「标准化加权目标的公式？权重？」都拿到同一张
「方案 02 的成本构成」表。根因是 read_versions 的 section 里混了 costs，
模型把「问公式」和「问钱」选进同一个视图；旧版靠调用前改参数的
math_view_arguments() 补救（补丁），现在从契约上就不可能选错。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.core.agent_tools import AgentToolbox


def _properties() -> dict:
    box = AgentToolbox(
        conversation_id="c-contract",
        versions=SimpleNamespace(list_by_conversation=lambda _cid: []),
        model_artifacts=None,
    )
    spec = next(item for item in box.tool_specs() if item["name"] == "read_versions")
    return spec["input_schema"]["properties"]


def test_costs_is_an_include_value_not_a_section() -> None:
    props = _properties()
    assert "costs" in props["include"]["items"]["enum"], "问钱仍然要有出口"
    assert "costs" not in props["section"]["enum"], "成本不是数学模型的一节"
    assert set(props["section"]["enum"]) == {
        "all", "objective", "constraints", "variables",
    }


def test_formula_question_has_one_unambiguous_route() -> None:
    props = _properties()
    assert "section=objective" in props["include"]["description"]
    assert 'include=["costs"]' in props["include"]["description"]


def test_contract_result_declares_verbatim_and_exclusive() -> None:
    """是否原样、是否独占，由工具返回声明，不靠调用方认工具名或认关键词。"""
    result = AgentToolbox._as_contract(
        {"status": "clarify", "data": {"message": "这句没接"}}
    )
    assert result["data"]["markdown"] == "这句没接"
    assert result["data"]["verbatim"] is True
    assert result["data"]["exclusive"] is True
