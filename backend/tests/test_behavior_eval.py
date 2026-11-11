"""行为级评测的 pytest 钩子。

- 离线层：用例文件本身要规范（每条都有 id/桶/话术，且至少一条期望）——不花 token。
- 在线层：真实话术 → 真实 agent → 机械判分，跟已存基线比对，只拦「退化」，不要求全绿。
  用真实 DeepSeek key（conftest 从 .env.local 载入），不注入假模型；没配 key 才
  运行时 skip，不假装跑过。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.evals.golden_eval import BaselineTracker
from app.evals.run_behavior_eval import CASES_PATH, load_cases, run
from yxcore.parse import configured

BASELINE_PATH = CASES_PATH.parent / "behavior-baseline.json"


def test_behavior_cases_are_wellformed() -> None:
    """离线：用例文件格式对——每条都有桶/话术，且至少一条期望。"""
    cases = load_cases(CASES_PATH)
    assert len(cases) >= 4, "行为用例至少要有 4 条，覆盖 改排程/指代/复合/查询 四桶"
    for case in cases:
        assert case.get("id"), f"用例缺 id：{case}"
        assert case.get("桶"), f"用例缺 桶：{case}"
        assert case.get("话术"), f"用例缺 话术：{case}"
        has_expectation = any(
            case.get(key) is not None
            for key in ("期望action", "期望关键词", "期望选项数")
        )
        assert has_expectation, f"用例 {case.get('id')} 没有任何期望判据"


def test_behavior_no_regression() -> None:
    """在线：真实话术进真实 agent，机械判分，只拦退化（比基线更差的）。"""
    if not configured():
        pytest.skip("未配置 DEEPSEEK/MINIMAX key，跳过在线行为评测")
    cases = load_cases(CASES_PATH)
    report, _buckets = run(cases)
    tracker = BaselineTracker(BASELINE_PATH)
    regressions = tracker.regressions(report, tracker.load())
    assert not regressions, f"行为评测退化：{', '.join(regressions)}"


def test_action_alternative_accepts_loop_reply() -> None:
    """查询桶双归属：删词表路由后同一句在线会变成 assistant_reply（由循环答），判分必须认。"""
    from app.evals.behavior_eval import evaluate_behavior

    case = {"id": "t", "期望action": "alarm_answer", "期望action备选": ["assistant_reply"]}
    result = evaluate_behavior(case, {"actionType": "assistant_reply",
                                      "assistantMessage": {"content": "ok"}})
    assert result.passed


def test_action_alternative_still_rejects_wrong() -> None:
    from app.evals.behavior_eval import evaluate_behavior

    case = {"id": "t", "期望action": "alarm_answer", "期望action备选": ["assistant_reply"]}
    result = evaluate_behavior(case, {"actionType": "change_preview",
                                      "assistantMessage": {"content": "ok"}})
    assert not result.passed
