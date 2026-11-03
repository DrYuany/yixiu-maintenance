"""参数可改性的一致性测试。

这条测试存在的理由：
过去 `test_catalog_lists_every_changeable_param` 断言 "目录条数 == 51"，
而 51 来自同一张手抄表——它只证明"提示词和注册表抄得一样"，不证明
"改得动"。结果 51 项里有 35 项一填就报"未支持参数"，测试全绿。

现在改成证能力，不证文案：
  1. 设计意义上的可改参数（GENERATION_LAYER ∪ SELECTION_LAYER）必须都有落点，
     不许出现"登记了但没说改到哪"；
  2. 有落点的每一项，都要能真的走过 validator → bridge → load_project；
  3. 没落点的不许出现在提示词"你能改什么"里。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = BACKEND_ROOT / "eval_cases"
if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))

import probe_param_coverage as probe  # noqa: E402

from dsl_core.registry import parameters as param_reg  # noqa: E402
from dsl_core.registry import schema_prompt as sp  # noqa: E402

DESIGN_CHANGEABLE = param_reg.GENERATION_LAYER | param_reg.SELECTION_LAYER


def test_every_design_changeable_param_declares_a_route() -> None:
    missing = DESIGN_CHANGEABLE - set(param_reg.APPLY_ROUTE)
    assert not missing, f"这些参数被归为可改，却没登记落点：{sorted(missing)}"
    extra = set(param_reg.APPLY_ROUTE) - DESIGN_CHANGEABLE
    assert not extra, f"路由表里有设计上不属于可改的参数：{sorted(extra)}"


def test_route_values_are_known() -> None:
    allowed = {"parameters", "objectives.strategy", "constraints.budget",
               "constraints.cardinality", "constraints.must", "unsupported"}
    unknown = {name: route for name, route in param_reg.APPLY_ROUTE.items() if route not in allowed}
    assert not unknown, f"未知落点：{unknown}"


@pytest.mark.parametrize("name", sorted(n for n in DESIGN_CHANGEABLE if param_reg.is_changeable(n)))
def test_changeable_param_actually_applies(name: str) -> None:
    """每一项都要走通真实链路；断在哪一步就报哪一步。"""
    record = probe._classify(name)
    verdict = str(record["结论"])
    assert verdict.startswith(("生效", "接受")), f"{name} 断在 {record['断点']}：{record.get('提示', verdict)}"


def test_unsupported_params_never_advertised() -> None:
    catalog = sp.param_catalog()
    advertised = [n for n in DESIGN_CHANGEABLE if param_reg.is_unsupported(n)]
    assert advertised, "应当存在尚未接入的参数，否则本测试无意义"
    for name in advertised:
        assert name not in catalog, f"{name} 改不动，却仍在『你能改什么』目录里"


def test_unsupported_params_are_named_in_prompt() -> None:
    """改不动的也要让模型知道，否则它会硬写一遍再报错。"""
    block = sp.unsupported_block()
    for name in DESIGN_CHANGEABLE:
        if param_reg.is_unsupported(name):
            assert name in block


def test_changeable_count_is_what_we_can_deliver() -> None:
    """数字本身不是目标；这个断言防的是"悄悄多抄一项回来"。"""
    assert len(sp.param_names()) == 19
    assert sum(1 for n in DESIGN_CHANGEABLE if param_reg.is_unsupported(n)) == 32


def _adversarial_rows() -> list[dict]:
    import json

    path = BACKEND_ROOT / "eval_cases" / "adversarial_slots.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_scored_cases_only_expect_writable_slots() -> None:
    """计分的用例不许要求填一个改不动的参数。

    之前 70 条里有 25 条（36%）在考"能不能填出改不动的参数"——不管模型多准，
    系统都会报错。这种用例会把评测变成自欺。
    """
    offenders = []
    for row in _adversarial_rows():
        if row.get("status") == "not_implemented":
            continue
        expect = row["expect"]
        if isinstance(expect, str):
            continue
        for want in expect.get("parameters") or []:
            if param_reg.route_of(want["field"]) != "parameters":
                offenders.append((row["id"], want["field"]))
    assert not offenders, f"这些用例要求写 parameters 但该参数不落在这里：{offenders}"


def test_not_implemented_flag_is_not_a_dumping_ground() -> None:
    """标了未实现的，必须真的未实现——不许拿它藏起答错的用例。"""
    for row in _adversarial_rows():
        if row.get("status") != "not_implemented":
            continue
        expect = row["expect"]
        assert not isinstance(expect, str), f"{row['id']} 不是参数类用例，不该标未实现"
        fields = [w["field"] for w in expect.get("parameters") or []]
        assert fields, f"{row['id']} 没有 parameters 期望项，不该标未实现"
        assert all(param_reg.is_unsupported(f) for f in fields), row["id"]
