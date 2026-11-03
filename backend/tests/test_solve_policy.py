from app.core.solve_policy import SolveEvidence, SolvePolicy


def test_unknown_never_becomes_infeasible_without_proof():
    evidence = SolveEvidence(
        status="UNKNOWN",
        has_incumbent=False,
        proof=False,
        elapsed_seconds=240,
        phase="search",
        reason="time budget reached",
    )

    presentation = SolvePolicy().present(evidence)

    assert presentation.status == "UNKNOWN"
    assert presentation.title == "尚未找到可行方案"
    assert "未证明不可行" in presentation.detail
    assert "无解" not in presentation.detail


def test_soft_threshold_prompts_without_stopping_running_job():
    evidence = SolveEvidence(
        status="UNKNOWN",
        has_incumbent=True,
        proof=False,
        elapsed_seconds=180,
        phase="search",
        reason=None,
        running=True,
    )

    presentation = SolvePolicy(soft_threshold_seconds=180).present(evidence)

    assert presentation.needs_user_decision is True
    assert presentation.running is True
    assert presentation.actions == ("wait", "background", "use_incumbent", "stop")
    assert presentation.status == "UNKNOWN"


def test_infeasible_wording_requires_solver_proof():
    policy = SolvePolicy()
    proven = policy.present(
        SolveEvidence(
            status="INFEASIBLE",
            has_incumbent=False,
            proof=True,
            elapsed_seconds=12,
            phase="solve",
            reason=None,
        )
    )
    unproven = policy.present(
        SolveEvidence(
            status="INFEASIBLE",
            has_incumbent=False,
            proof=False,
            elapsed_seconds=12,
            phase="solve",
            reason=None,
        )
    )

    assert proven.title == "已证明当前约束不可行"
    assert unproven.status == "UNKNOWN"
    assert "未证明不可行" in unproven.detail
