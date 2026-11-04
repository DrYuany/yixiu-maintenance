"""行为级评测的 pytest 钩子。

- 离线层：用例文件本身要规范（每条都有 id/桶/话术，且至少一条期望）——不花 token。
- 在线层：真实话术 → 真实 agent → 机械判分，跟已存基线比对，只拦「退化」，不要求全绿。
  用**运行时 skip**（不靠装饰器，因为 app.main 在收集阶段就会 load .env.local，装饰器的
  configured() 判断会失真），conftest 的 SKIP_LOCAL_ENV=1 一设就跳过。
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
    if os.environ.get("SKIP_LOCAL_ENV") == "1":
        pytest.skip("测试环境（SKIP_LOCAL_ENV=1）不加载 .env.local，跳过在线行为评测")
    if not configured():
        pytest.skip("未配置 DEEPSEEK/MINIMAX key，跳过在线行为评测")
    cases = load_cases(CASES_PATH)
    report, _buckets = run(cases)
    tracker = BaselineTracker(BASELINE_PATH)
    regressions = tracker.regressions(report, tracker.load())
    assert not regressions, f"行为评测退化：{', '.join(regressions)}"
