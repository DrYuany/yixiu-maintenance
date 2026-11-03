# ModelSpec 1.0

ModelSpec is a transport contract between language interpretation, deterministic validation, and a solver adapter. It is data, not executable code. The validator is structural and deliberately conservative.

## Top-level shape

```json
{
  "schema_version": "1.0",
  "status": "ready",
  "problem_statement": "Minimize economic loss while completing the severe repairs by the selected deadline.",
  "data": {"blade_loss": 1200, "bearing_loss": 900},
  "variables": [],
  "objectives": [],
  "constraints": [],
  "unresolved_data": [],
  "assumptions": [],
  "provenance": []
}
```

`data` maps stable parameter IDs to finite numeric values. Put units, source, observation/forecast time, and validity interval in provenance records, for example `{"parameter":"price_day_1","unit":"CNY/MWh","source":"forecast batch 42","observed_at":"2026-06-30T08:00+08:00"}`. Values without a defensible source may be labeled assumptions, never silently treated as facts. `unresolved_data` lists missing quantities that affect the formulation or answer.

## Variables

Each variable has a unique `id`, `type` (`binary`, `integer`, or `continuous`), finite numeric `lower_bound` and `upper_bound`, and non-empty `unit`. Integer bounds must be integral; binary bounds are exactly 0 and 1. Choose units explicitly and keep coefficients dimensionally compatible. Use indices in IDs for time, turbine, component, crew/resource when required. Do not encode a huge implicit index set in prose; materialize it in the spec or a reviewed generator.

## Linear expression

The only expression shape is:

```json
{"constant": 0, "terms": [
  {"variable": "repair_blade", "coefficient": {"data": "blade_loss"}},
  {"variable": "repair_bearing", "coefficient": 900}
]}
```

The `constant` and every coefficient can be a finite number or exactly `{"data":"known_parameter_id"}`. Terms are summed. Do not nest expressions or put variables inside coefficients. A constraint row has a linear `lhs`, `sense` (`<=`, `>=`, `==`), and numeric/data-reference `rhs`. To express `x + y <= 1`, use two terms in one row. To express `x <= 3`, use `x` on the left and 3 on the right.

## Objectives and constraints

Objectives require unique IDs, unique positive integer priorities, `sense` (`minimize`/`maximize`), and a linear expression. Priority order supports lexicographic optimization in a solver adapter; do not emulate it with unexplained large weights. Constraints require unique IDs, `kind: "linear_row"`, a human-readable `meaning`, lhs, sense, and rhs. A solver adapter must explicitly preserve lexicographic priorities; if it cannot, do not imply the requested priority order was honored.

The validator only recognizes canonical linear rows; the component catalog describes how to construct them. Keep a business explanation and parameter provenance. `assumptions` and `provenance` are arrays so the interpretation remains auditable.

## Readiness

Only `status: "ready"` with an empty `unresolved_data` can pass the solver gate. Clarification, extension proposal, and rejection specs are useful outputs but intentionally cannot be dispatched. Structural validity is not a feasibility proof; solve, inspect actual solver status, and explain infeasibility or unboundedness separately.
