# Linear and MILP Boundary

## Allowed core

LP/MILP permits linear equalities/inequalities and a linear objective. Decision variables may be continuous, integer, or binary, with finite numeric bounds in this ModelSpec. Fixed data times a decision variable is linear: if price `p` is known for this solve, `p*x` is allowed. Sums of such terms are allowed.

## Reject unless a reviewed linearization applies

- Decision × decision, such as `x*y` or `x²`.
- Division by a decision variable, ratios with decision-dependent denominators, products of decision-dependent quantities, or arbitrary smooth/piecewise nonlinear functions.
- `max`, `min`, `abs`, conditional expressions, and indicator implications written as operators. Some have valid MILP formulations, but only use an explicit reviewed template with correct finite bounds.
- Big-M values guessed from convenience. M must be derived from declared bounds and documented; prefer a tighter native indicator if the chosen solver supports it and the adapter maps it safely.
- Missing/infinite bounds when a template requires finite bounds, or data parameters that are missing, non-finite, stale, or not in consistent units.

## Extension protocol

When language maps to an unsupported primitive, return `extension_proposal`: describe the intended semantics, variables/data/bounds required, candidate known MILP formulation (if known), proof/conditions for equivalence, and test cases. Do not sneak it into a linear row or silently approximate. After a reviewer accepts a reusable formulation, add it as a named template with bounds, units, applicability limits, and positive/negative tests. Approximation is a separate user-visible decision with an error/tolerance statement.

## Validate in layers

1. Schema/structure validator: allowed fields/shapes, declared references, finite domains/data, linearity syntax, ready status.
2. Formulation review: dimensional consistency, policy meaning, source freshness, valid linearization and bounds.
3. Solver: actual feasibility/optimality status and adapter support.
4. Result review: recompute reported metrics from the chosen decisions and original data. A structural validator alone cannot establish layers 2–4.
