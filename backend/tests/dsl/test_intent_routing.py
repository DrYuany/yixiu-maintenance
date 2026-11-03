"""意图路由的现状与已知缺口。

这一组测试不是为了夸系统，而是把"规则层在前面、大模型在后面"这件事钉住，
并把已知的漏词场景标成 xfail——修好了它会自己变红（strict），提醒删掉标记。

实际链路（app/core/dsl/llm_parser.py::_parse_core）：
    1 批量句正则
    2 parse_by_rules（规则）
    3 _classify_not_change_only（关门）
    4 parse_by_legacy（旧规划语言正则编译）   ← 命中即返回，置信度 1.0
    5 _classify_non_change（关门）
    6 向量模板命中
    7 大模型

上面 agent_runtime 还有一道 is_planning_request 会把整句认领走，直接进旧编译器，
连 1~7 都不走。所以"大模型为主、规则兜底"是设计意图，不是当前实现。
"""
from __future__ import annotations

import json

import pytest

from app.core.llm_parser import parse_by_legacy
from app.core.planning_intent import PlanningIntentCompiler


def test_rule_gate_claims_strategy_sentences() -> None:
    """把现状写清楚：含策略词的句子由规则层认领，大模型不参与。"""
    compiler = PlanningIntentCompiler()
    assert compiler.is_planning_request("按成本最低优先排每天只能干8小时")


def test_pure_strategy_sentence_is_fully_covered_by_rules() -> None:
    """只有策略诉求时，规则层的结果是完整的——这种句子不需要大模型。"""
    dsl = parse_by_legacy("按成本最低优先排")
    assert dsl is not None
    assert dsl["objectives"][0]["name"] == "成本最低优先"
    assert "parameters" not in dsl and "constraints" not in dsl


def test_strategy_plus_other_change_keeps_both() -> None:
    """同一句里的两处改动都要保留。

    曾经这里标着 xfail：策略词一命中就整句返回，工时改动蒸发，大模型还没机会看。
    2026-10-30 修的是更根本的一层：路由词「不超过」会认领整句，而预算正则
    只认「总成本/预算/现金支出」——"钱不超过300万"于是变成一份纯默认方案。
    现在规则层多了两道闸：零消费不认领、动作词表补全（干/做/只/仅）。
    """
    dsl = parse_by_legacy("按成本最低优先排每天只能干8小时")
    assert dsl is not None
    assert "parameters" in dsl, f"工时改动被丢掉了，实际只解析出 {json.dumps(dsl, ensure_ascii=False)}"
    assert any(p["field"] == "每日有效维修上限" for p in dsl["parameters"])


def test_budget_word_qian_is_understood() -> None:
    """"钱不超过300万"必须变成现金预算约束。

    这句曾经被路由词「不超过」认领，却解析不出任何字段，返回默认方案、
    预算凭空消失，置信度还是 1.0——空的但看起来正常。
    """
    dsl = parse_by_legacy("钱不超过300万")
    assert dsl is not None, "规则层应当认领这句"
    budget = [c for c in dsl.get("constraints") or [] if c["type"] == "budget"]
    assert budget and budget[0]["value"] == 3_000_000
    assert budget[0]["quantity"] == "现金支出"


def test_unconsumed_sentence_is_handed_to_llm() -> None:
    """规则层读不懂的句子不许硬认领——否则"看起来正常"的空结果会盖住原话。"""
    assert parse_by_legacy("船租翻倍了") is None
    assert parse_by_legacy("这段时间到底该怎么安排比较妥当") is None


# ── 上游那道门：is_planning_request ──────────────────────────────────────────
# 它比 DSL 漏斗更靠前。为真时直接进旧规划器（纯正则），DSL 六层和大模型都不走。
# 所以它的漏判 = 整句被一个关键词带走，且没有任何兜底。

def test_comparison_question_is_not_claimed_by_the_gate() -> None:
    """「经济优先和风险优先差多少」是问句，不是"改成高风险优先"的指令。

    实测它曾被认领：问句词表里只有「是多少」，没有「多少」；
    于是落到最后一段关键词表，「风险优先」命中 → 走进旧规划器 →
    产出一份 strategy=高风险优先抢修 的"改方案"。用户问的是差多少。
    """
    compiler = PlanningIntentCompiler()
    assert compiler.is_planning_request("经济优先和风险优先差多少") is False
    assert compiler.is_planning_request("按成本最低优先排") is True


def test_gate_keeps_action_sentences() -> None:
    compiler = PlanningIntentCompiler()
    for text in ("钱不超过300万", "按成本最低优先排每天只能干8小时", "帮我排一版方案"):
        assert compiler.is_planning_request(text) is True, text
