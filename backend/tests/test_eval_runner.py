from app.evals.run_eval import evaluate_cases


def test_critical_failure_fails_release_gate():
    report = evaluate_cases(
        [
            {
                "id": "bad-status",
                "layer": "L0",
                "status": "UNKNOWN",
                "message": "当前模型无解",
                "proof": False,
            }
        ]
    )
    assert report["passed"] is False
    assert report["failed_cases"] == ["bad-status"]


def test_valid_cases_pass_all_declared_layers():
    report = evaluate_cases(
        [
            {"id": "honest", "layer": "L0", "status": "UNKNOWN", "message": "未证明不可行", "proof": False},
            {"id": "repeat", "layer": "L2b", "fingerprints": ["x", "x", "x"]},
        ]
    )
    assert report["passed"] is True
    assert report["layer_scores"] == {"L0": 1.0, "L2b": 1.0}
