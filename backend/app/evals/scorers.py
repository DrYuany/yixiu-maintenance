from __future__ import annotations

from typing import Any


REQUIRED_ARTIFACT_FIELDS = {
    "context_fingerprint",
    "decision_fingerprint",
    "model_fingerprint",
    "solver_profile",
    "compiler_version",
    "verifier_version",
}


def fingerprint_repeatable(fingerprints: list[str]) -> bool:
    return bool(fingerprints) and len(set(fingerprints)) == 1


def constraint_scores(expected: set[str], actual: set[str]) -> dict[str, float]:
    overlap = len(expected & actual)
    precision = overlap / len(actual) if actual else float(not expected)
    recall = overlap / len(expected) if expected else float(not actual)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": precision, "recall": recall, "f1": f1}


def status_honest(status: str, message: str, proof: bool) -> bool:
    normalized = "".join(message.split()).lower()
    false_infeasible_claims = ("确认无解", "模型无解", "没有可行解", "已证明不可行")
    false_optimal_claims = ("已证明最优", "全局最优", "确定最优")
    if status != "INFEASIBLE" or not proof:
        if any(claim in normalized for claim in false_infeasible_claims):
            return False
    if status != "OPTIMAL" or not proof:
        if any(claim in normalized for claim in false_optimal_claims):
            return False
    if status == "INFEASIBLE" and not proof:
        return False
    return True


def artifact_complete(artifact: dict[str, Any]) -> bool:
    return REQUIRED_ARTIFACT_FIELDS <= artifact.keys() and all(
        artifact.get(field) not in (None, "", {}) for field in REQUIRED_ARTIFACT_FIELDS
    )

