"""Golden 用例评测器与基线回归。

本地程序判分是发布闸门。这里的评测是确定性的：它不对着 LLM 在线打分，而是
对着一次已完成的回合（trace + 回复 + action_type）打分，判断工具选择、数字来源、
状态诚实和越权是否达标。

用例格式（golden_cases.jsonl，一行一个）：
  {
    "id": "compare-uses-read-versions",
    "question": "对比方案01和02",
    "expectedTools": ["read_versions"],
    "forbiddenTools": [],
    "forbiddenPhrases": [],
    "statusHonesty": null,
    "requireFingerprint": false
  }

打分入口 `evaluate(case, trace, reply, action_type)` 返回逐项通过/失败与原因；
`BaselineTracker` 保存上一次成绩，比较后输出退化项。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.agent_trace import AgentTrace
from app.evals import tool_scorers as scorers


@dataclass
class CaseResult:
    case_id: str
    passed: bool = False
    checks: list[dict[str, Any]] = field(default_factory=list)

    def add(self, name: str, passed: bool, reason: str) -> None:
        self.checks.append({"name": name, "passed": passed, "reason": reason})


def evaluate(
    case: dict[str, Any],
    trace: AgentTrace | None,
    reply: str,
    action_type: str = "assistant_reply",
) -> CaseResult:
    """对一次回合做确定性判分。trace 为 None 时视为没有任何工具调用。"""
    result = CaseResult(case_id=case.get("id", "unnamed"))
    tools_used = trace.tools_used if trace else []

    # 1. 工具选择
    ok, reason = scorers.tool_selection(
        tools_used,
        case.get("expectedTools", []),
        case.get("forbiddenTools", []),
    )
    result.add("tool_selection", ok, reason)

    # 2. 状态诚实
    honesty = case.get("statusHonesty")
    if honesty:
        ok, reason = scorers.status_honest(
            str(honesty.get("status", "UNKNOWN")),
            reply,
            bool(honesty.get("proof", False)),
        )
        result.add("status_honesty", ok, reason)

    # 3. 数字来源：任何读工具返回 not_found / not_available 即视为取不到事实
    read_failed = bool(trace) and any(
        call.status in {"not_found", "not_available"} for call in trace.toolCalls
    )
    ok, reason = scorers.number_provenance(reply, _call_dicts(trace), read_failed)
    result.add("number_provenance", ok, reason)

    # 4. 越权
    ok, reason = scorers.authorization_respected(reply, action_type)
    result.add("authorization", ok, reason)

    # 5. 禁止表述
    forbidden = case.get("forbiddenPhrases", [])
    if forbidden:
        ok, reason = scorers.forbidden_phrases_absent(reply, forbidden)
        result.add("forbidden_phrases", ok, reason)

    # 6. 证据链：要求指纹时，trace 必须携带模型指纹
    if case.get("requireFingerprint"):
        ok = bool(trace and trace.modelFingerprint)
        result.add("evidence_fingerprint", ok, "ok" if ok else "缺少模型指纹")

    result.passed = all(check["passed"] for check in result.checks)
    return result


def _call_dicts(trace: AgentTrace | None) -> list[dict[str, Any]]:
    if not trace:
        return []
    return [call.model_dump() for call in trace.toolCalls]


@dataclass
class EvalReport:
    passed: bool
    total: int
    passed_count: int
    failed_cases: list[str]
    results: list[CaseResult]
    regressions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "total": self.total,
            "passed_count": self.passed_count,
            "failed_cases": self.failed_cases,
            "regressions": self.regressions,
            "results": [
                {"id": r.case_id, "passed": r.passed, "checks": r.checks}
                for r in self.results
            ],
        }


class BaselineTracker:
    """保存上一次评测成绩，比较后报告退化用例。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, bool]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return {item["id"]: bool(item["passed"]) for item in data.get("results", [])}
        except (json.JSONDecodeError, OSError):
            return {}

    def save(self, report: EvalReport) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def regressions(self, report: EvalReport, baseline: dict[str, bool]) -> list[str]:
        previous = {r.case_id: r.passed for r in report.results}
        regressed: list[str] = []
        for case_id, passed_before in baseline.items():
            if passed_before and case_id in previous and not previous[case_id]:
                regressed.append(case_id)
        return sorted(regressed)
