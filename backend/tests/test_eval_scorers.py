from app.evals.scorers import (
    artifact_complete,
    constraint_scores,
    fingerprint_repeatable,
    status_honest,
)


def test_repeatability_requires_every_fingerprint_to_match():
    assert fingerprint_repeatable(["abc", "abc", "abc"])
    assert not fingerprint_repeatable(["abc", "def", "abc"])


def test_hard_constraint_precision_and_recall_are_structural():
    scores = constraint_scores(
        expected={"daily_hours=8", "max_lines=2"},
        actual={"daily_hours=8", "vessel_budget=3"},
    )
    assert scores == {"precision": 0.5, "recall": 0.5, "f1": 0.5}


def test_unknown_cannot_claim_infeasible_or_optimal():
    assert status_honest("UNKNOWN", "尚未找到方案，未证明不可行", proof=False)
    assert not status_honest("UNKNOWN", "当前模型无解", proof=False)
    assert not status_honest("FEASIBLE", "已证明最优", proof=False)


def test_artifact_completeness_requires_full_lineage():
    artifact = {
        "context_fingerprint": "c",
        "decision_fingerprint": "d",
        "model_fingerprint": "m",
        "solver_profile": {"workers": 1},
        "compiler_version": "v1",
        "verifier_version": "v1",
    }
    assert artifact_complete(artifact)
    artifact.pop("model_fingerprint")
    assert not artifact_complete(artifact)

