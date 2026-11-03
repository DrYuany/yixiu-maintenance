# Worked Examples

## Severe repairs moved ahead of economic optimization

User request: “Economic loss was the old priority. Now prioritize severe blade and bearing faults; approved windows are July 1–20 and August 10–30. Blade parts arrive July 5. Finish both severe repairs by July 15 if possible, then minimize loss.”

First resolve what “if possible” means: Is July 15 a hard service requirement or a preference that may be violated? Also confirm task duration/weather eligibility and whether July 5 is the arrival date at the usable site. If those facts are available and deadline is confirmed hard, model it as hard constraints; economic loss remains a lower-priority objective. If the user wants a preference, use a documented lexicographic tier or bounded slack with an approved penalty. July 5 availability is a cumulative inventory constraint, not a prose note. The exact schedule cannot be calculated until relevant inputs are supplied and a solver is available.

## Custom linear constraint is allowed

If binary `repair_blade`, `repair_bearing` already exist and the user says “complete at least one severe repair in July,” add `repair_blade + repair_bearing >= 1` only if those variables represent completion in July. If they merely mean “selected at some time,” create/approve the correct time-indexed completion variables first. Dynamic does not mean semantic guessing.

## Nonlinear boundary

“Minimize cost per repaired turbine” is a ratio objective if both cost and repaired count depend on decisions. Do not put it in a linear expression. Return an extension proposal; a transformation may be possible under particular bounded/positive-domain conditions, but do not assume those conditions. Likewise reject `repair_blade * repair_bearing` unless a reviewed binary-product linearization with the proper linking constraints is registered.

## Missing stock

If the user requests “ensure repairs only use stock on hand” but neither opening stock nor arrivals are supplied, mark inventory values unresolved and use `clarification_required`. Do not substitute zero or assume sufficient stock.
