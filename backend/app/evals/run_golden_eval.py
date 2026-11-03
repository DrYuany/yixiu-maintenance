"""Golden 用例评测 CLI：离线发布闸门。

用法：
    py -m app.evals.run_golden_eval backend/eval_cases/golden_cases.jsonl \
        --traces backend/eval_cases/golden_traces.jsonl \
        --baseline product-output/golden-baseline.json

traces 文件每行一个回合（可选；缺省时用 case 内嵌的 reply/actionType）：
    {"caseId": "...", "trace": {AgentTrace.model_dump()}, "reply": "...", "actionType": "..."}

本地程序判分是发布闸门，不依赖 LLM。exit code 0 表示全部通过，1 表示有失败或退化。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.core.agent_trace import AgentTrace
from app.evals.golden_eval import BaselineTracker, CaseResult, EvalReport, evaluate


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _load_traces(path: Path) -> dict[str, dict[str, Any]]:
    traces: dict[str, dict[str, Any]] = {}
    for line in _load_jsonl(path):
        traces[line["caseId"]] = line
    return traces


def run(cases: list[dict[str, Any]], traces: dict[str, dict[str, Any]]) -> EvalReport:
    results: list[CaseResult] = []
    for case in cases:
        entry = traces.get(case.get("id"))
        if entry and "trace" in entry:
            trace = AgentTrace(**entry["trace"])
        else:
            trace = None
        reply = (entry or {}).get("reply", case.get("reply", ""))
        action_type = (entry or {}).get("actionType", case.get("actionType", "assistant_reply"))
        results.append(evaluate(case, trace, reply, action_type))
    passed_count = sum(1 for result in results if result.passed)
    failed_cases = [result.case_id for result in results if not result.passed]
    return EvalReport(
        passed=not failed_cases,
        total=len(results),
        passed_count=passed_count,
        failed_cases=failed_cases,
        results=results,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run deterministic golden-case eval gate")
    parser.add_argument("cases", type=Path)
    parser.add_argument("--traces", type=Path, default=None)
    parser.add_argument("--baseline", type=Path, default=None)
    args = parser.parse_args(argv)

    cases = _load_jsonl(args.cases)
    traces = _load_traces(args.traces) if args.traces else {}
    report = run(cases, traces)

    if args.baseline:
        tracker = BaselineTracker(args.baseline)
        report.regressions = tracker.regressions(report, tracker.load())
        tracker.save(report)

    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.passed and not report.regressions else 1


if __name__ == "__main__":
    raise SystemExit(main())
