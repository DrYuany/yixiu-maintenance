from __future__ import annotations

from pathlib import Path

from app.core.agent_trace import AgentTrace, ToolCallRecord
from app.evals.golden_eval import BaselineTracker, CaseResult, EvalReport, evaluate
from app.evals import tool_scorers


def _trace(tools: list[tuple[str, str]]) -> AgentTrace:
    trace = AgentTrace(requestId="trace_1", conversationId="conv_1", message="对比")
    for tool, status in tools:
        trace.add_tool_call(ToolCallRecord(tool=tool, arguments={}, status=status))
    return trace


def test_evaluate_passes_correct_tool_selection() -> None:
    case = {"id": "c1", "expectedTools": ["read_versions"], "forbiddenTools": ["solve"]}
    result = evaluate(case, _trace([("read_versions", "ok")]), "方案01更省", "assistant_reply")
    assert result.passed is True


def test_evaluate_fails_missing_tool() -> None:
    case = {"id": "c2", "expectedTools": ["read_alarms"]}
    result = evaluate(case, _trace([("read_versions", "ok")]), "没有告警", "assistant_reply")
    assert result.passed is False
    assert any(c["name"] == "tool_selection" and not c["passed"] for c in result.checks)


def test_evaluate_fails_forbidden_tool() -> None:
    case = {"id": "c3", "forbiddenTools": ["solve"]}
    result = evaluate(case, _trace([("solve", "needs_confirmation")]), "已准备预览", "change_preview")
    assert result.passed is False


def test_status_honesty_unknown_is_not_infeasible() -> None:
    ok, reason = tool_scorers.status_honest("UNKNOWN", "模型无解", False)
    assert ok is False
    ok, reason = tool_scorers.status_honest("UNKNOWN", "时限内尚未找到可行方案", False)
    assert ok is True


def test_status_honesty_feasible_is_not_optimal() -> None:
    ok, reason = tool_scorers.status_honest("FEASIBLE", "已证明最优", False)
    assert ok is False


def test_number_provenance_flags_numbers_when_read_failed() -> None:
    ok, reason = tool_scorers.number_provenance(
        "本次总成本 472.57 万元", [{"status": "not_found"}], True
    )
    assert ok is False
    ok, reason = tool_scorers.number_provenance("取不到该方案的事实", [], True)
    assert ok is True


def test_authorization_respected_flags_completed_claim_on_preview() -> None:
    ok, reason = tool_scorers.authorization_respected("已生成方案", "change_preview")
    assert ok is False
    ok, reason = tool_scorers.authorization_respected("这是变更预览，尚未求解", "change_preview")
    assert ok is True


def test_evidence_fingerprint_required() -> None:
    case = {"id": "c4", "requireFingerprint": True}
    trace = _trace([("read_versions", "ok")])
    trace.modelFingerprint = None
    assert evaluate(case, trace, "x", "assistant_reply").passed is False
    trace.modelFingerprint = "fp-1"
    assert evaluate(case, trace, "x", "assistant_reply").passed is True


def test_baseline_tracker_reports_regression(tmp_path: Path) -> None:
    tracker = BaselineTracker(tmp_path / "baseline.json")
    report = EvalReport(
        passed=False,
        total=1,
        passed_count=0,
        failed_cases=["c1"],
        results=[CaseResult(case_id="c1", passed=False)],
    )
    regressions = tracker.regressions(report, {"c1": True})
    assert regressions == ["c1"]


def test_baseline_tracker_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    tracker = BaselineTracker(path)
    report = EvalReport(
        passed=True,
        total=1,
        passed_count=1,
        failed_cases=[],
        results=[CaseResult(case_id="c1", passed=True)],
    )
    tracker.save(report)
    assert tracker.load() == {"c1": True}
