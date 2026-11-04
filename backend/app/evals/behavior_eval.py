"""行为级评测器：真实话术 → 真实 agent → 机械判分。

与 golden_eval（工具级，判「调了哪个工具」）互补。这里判「行为对不对」：
一句话进真实 agent，看 actionType 是否命中、回复里有没有该出现的关键词、
澄清选项够不够。全部是机械判分，不调 LLM 打分——「能机械就机械，模型的留给模型」。

用例格式（behavior_cases.json，一个对象一条）：
    {
      "id": "chg-001",
      "桶": "改排程",
      "话术": "船租涨两成",
      "期望action": "change_preview",      # 可选：路由/动作是否命中
      "期望关键词": ["14400"],              # 可选：回复正文必须包含的词
      "期望选项数": 2                       # 可选：澄清选项至少几条
    }

一条 case 有多个「期望」时，全部命中才算过；任何一项不中，报告里逐项标出。
"""
from __future__ import annotations

from typing import Any

from app.evals.golden_eval import CaseResult


def evaluate_behavior(case: dict[str, Any], response: dict[str, Any] | None) -> CaseResult:
    result = CaseResult(case_id=case.get("id", "unnamed"))
    if response is None:
        result.add("response", False, "无响应（请求异常）")
        result.passed = False
        return result

    expected_action = case.get("期望action")
    if expected_action:
        actual = response.get("actionType")
        result.add("action", actual == expected_action,
                   f"期望 {expected_action}，得到 {actual}")

    content = ((response.get("assistantMessage") or {}).get("content") or "")
    for kw in case.get("期望关键词") or []:
        result.add(f"关键词[{kw}]", kw in content, "命中" if kw in content else "未命中")

    option_min = case.get("期望选项数")
    if option_min is not None:
        got = len(response.get("clarificationOptions") or [])
        result.add("选项数", got >= option_min, f"期望 >= {option_min}，得到 {got}")

    result.passed = all(check["passed"] for check in result.checks)
    return result
