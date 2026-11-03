"""把评测的快层挂进 pytest——评测只在手动跑时起作用，等于没有评测。

E1（校验）/ E2离线（gold 与注册表一致）/ E5（多轮状态机）/ E4（提示词一致性）
都不跑求解器，几秒就完，所以每次提交都跑。
E3（端到端）/ E6（复合需求，含求解）/ E7（多轮在线）要跑求解或调模型，
留在 `eval/run_eval.py` 里单独跑。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

EVAL_DIR = Path(__file__).resolve().parents[1] / "eval"
sys.path.insert(0, str(EVAL_DIR))

import run_eval  # noqa: E402


def test_e1_validator_layer():
    failures = run_eval.run_e1()
    assert not failures, "\n".join(failures)


def test_e2_gold_matches_registry():
    failures = run_eval.run_e2_offline()
    assert not failures, "\n".join(failures)


def test_e5_multiturn_state_machine():
    """多轮语义：覆盖、叠加、撤销、改主意没撤旧的要拦下。"""
    failures = run_eval.run_e5()
    assert not failures, "\n".join(failures)


def test_e4_prompt_matches_registry():
    failures = run_eval.run_e4()
    assert not failures, "\n".join(failures)


@pytest.mark.skipif(not __import__("yxcore.parse", fromlist=["configured"]).configured(),
                    reason="未配置 DEEPSEEK_API_KEY，跳过在线翻译回归")
def test_e2_online_translation():
    """在线层：判分是确定性的（逐字段比对 gold），不配 key 就跳过，不假装跑过。"""
    failures, cautions = run_eval.run_e2_online()
    assert not failures, "\n".join(failures)
    for note in cautions:
        print("过慎：", note)
