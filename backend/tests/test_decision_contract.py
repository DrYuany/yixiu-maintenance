from decimal import Decimal

from app.core.decision_contract import DecisionCompiler


def context(**overrides):
    value = {
        "prompt": "优先修高风险任务，船租上涨 20%",
        "history": [{"role": "user", "content": "制定五周计划"}],
        "working_memory": {"goal": "降低风险", "selected": ["任务02", "任务01"]},
        "business_state": {"snapshot": "data-v3", "revision": 7},
        "schema_version": "intent-v1",
        "compiler_version": "compiler-v1",
        "model_version": "model-snapshot-1",
        "prompt_version": "prompt-v2",
        "solver_profile": {"workers": 1, "seed": 17},
        "trace_id": "volatile-a",
        "timestamp": "2026-10-25T10:00:00+08:00",
    }
    value.update(overrides)
    return value


def test_same_semantics_produce_same_fingerprints_across_instances():
    decision_a = {
        "hard_constraints": [
            {"name": "daily_hours", "value": 8.0},
            {"name": "max_lines", "value": Decimal("2.00")},
        ],
        "objectives": [
            {"name": "high_risk_completion", "priority": 1},
            {"name": "total_cost", "priority": 2},
        ],
    }
    decision_b = {
        "objectives": [
            {"priority": 1, "name": "high_risk_completion"},
            {"priority": 2, "name": "total_cost"},
        ],
        "hard_constraints": [
            {"value": 2, "name": "max_lines"},
            {"value": 8, "name": "daily_hours"},
        ],
    }
    context_b = context(trace_id="volatile-b", timestamp="2030-01-01T00:00:00Z")

    first = DecisionCompiler().compile(context(), decision_a, {"constraints": decision_a["hard_constraints"]})
    second = DecisionCompiler().compile(context_b, decision_b, {"constraints": decision_b["hard_constraints"]})

    assert first.context_fingerprint == second.context_fingerprint
    assert first.decision_fingerprint == second.decision_fingerprint
    assert first.model_fingerprint == second.model_fingerprint
    assert first.canonical_decision == second.canonical_decision


def test_objective_order_and_business_state_change_fingerprints():
    decision = {
        "objectives": [
            {"name": "risk", "priority": 1},
            {"name": "cost", "priority": 2},
        ]
    }
    compiler = DecisionCompiler()
    baseline = compiler.compile(context(), decision, {"objectives": decision["objectives"]})
    reordered = compiler.compile(
        context(),
        {"objectives": list(reversed(decision["objectives"]))},
        {"objectives": list(reversed(decision["objectives"]))},
    )
    changed_state = compiler.compile(
        context(business_state={"snapshot": "data-v3", "revision": 8}),
        decision,
        {"objectives": decision["objectives"]},
    )

    assert baseline.decision_fingerprint != reordered.decision_fingerprint
    assert baseline.model_fingerprint != reordered.model_fingerprint
    assert baseline.context_fingerprint != changed_state.context_fingerprint

