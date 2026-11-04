"""行为级评测 CLI：真实话术 → 真实 agent → 机械判分 → 分桶报告 + 基线回归。

用法：
    py -m app.evals.run_behavior_eval \
        --baseline product-output/behavior-baseline.json

需要 DEEPSEEK / MINIMAX key（真实 LLM 翻译 + 查询）。每个 case 一个独立会话，互不污染。
exit code 0 = 全部通过且无退化；1 = 有失败或退化。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.evals.behavior_eval import evaluate_behavior
from app.evals.golden_eval import BaselineTracker, EvalReport
from app.main import create_app

CASES_PATH = Path(__file__).resolve().parents[2] / "eval_cases" / "behavior_cases.json"


def load_cases(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def run(cases: list[dict[str, Any]]) -> tuple[EvalReport, dict[str, dict[str, int]]]:
    client = TestClient(create_app())
    results = []
    buckets: dict[str, dict[str, int]] = {}
    for case in cases:
        cid = client.post("/api/conversations", json={"title": "eval"}).json()["id"]
        try:
            response = client.post(
                "/api/agent/message",
                json={"conversationId": cid, "message": case["话术"]},
            ).json()
        except Exception as exc:  # noqa: BLE001
            response = None
            case = {**case, "期望action": None}  # 异常时只记 response 缺失，不计 action
        result = evaluate_behavior(case, response)
        results.append(result)
        bucket = case.get("桶", "未分桶")
        entry = buckets.setdefault(bucket, {"total": 0, "passed": 0})
        entry["total"] += 1
        entry["passed"] += 1 if result.passed else 0

    passed_count = sum(1 for r in results if r.passed)
    failed_cases = [r.case_id for r in results if not r.passed]
    report = EvalReport(
        passed=not failed_cases,
        total=len(results),
        passed_count=passed_count,
        failed_cases=failed_cases,
        results=results,
    )
    return report, buckets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run behavior-level eval gate (online)")
    parser.add_argument("--cases", type=Path, default=CASES_PATH)
    parser.add_argument("--baseline", type=Path, default=None)
    args = parser.parse_args(argv)

    cases = load_cases(args.cases)
    report, buckets = run(cases)

    if args.baseline:
        tracker = BaselineTracker(args.baseline)
        report.regressions = tracker.regressions(report, tracker.load())
        tracker.save(report)

    print("=== 分桶 ===")
    for bucket, entry in sorted(buckets.items()):
        print(f"  {bucket}: {entry['passed']}/{entry['total']}")
    print("=== 明细 ===")
    for result in report.results:
        mark = "PASS" if result.passed else "FAIL"
        checks = "; ".join(f"{c['name']}={c['passed']}" for c in result.checks)
        print(f"  [{mark}] {result.case_id}  ({checks})")
    if report.regressions:
        print("=== 退化 ===")
        print("  " + ", ".join(report.regressions))
    print(f"=== 合计 {report.passed_count}/{report.total} ===")
    return 0 if report.passed and not report.regressions else 1


if __name__ == "__main__":
    raise SystemExit(main())
