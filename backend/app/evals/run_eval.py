from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.evals.scorers import artifact_complete, fingerprint_repeatable, status_honest


def _score_case(case: dict[str, Any]) -> bool:
    if "status" in case:
        return status_honest(case["status"], case.get("message", ""), bool(case.get("proof")))
    if "fingerprints" in case:
        return fingerprint_repeatable(case["fingerprints"])
    if "artifact" in case:
        return artifact_complete(case["artifact"])
    return False


def evaluate_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    results = [{"id": case["id"], "layer": case["layer"], "passed": _score_case(case)} for case in cases]
    layers = sorted({item["layer"] for item in results})
    layer_scores = {
        layer: sum(item["passed"] for item in results if item["layer"] == layer)
        / sum(1 for item in results if item["layer"] == layer)
        for layer in layers
    }
    failed = [item["id"] for item in results if not item["passed"]]
    return {"passed": not failed, "failed_cases": failed, "layer_scores": layer_scores, "results": results}


def load_cases(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run deterministic maintenance Agent release gates")
    parser.add_argument("cases", type=Path)
    args = parser.parse_args()
    report = evaluate_cases(load_cases(args.cases))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

