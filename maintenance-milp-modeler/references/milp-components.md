# Maintenance Planning Component Catalog

This is a starter library, not a claim that each formulation already exists in the application. Instantiate only a family whose data and exact semantics are available. A new row can be composed from existing typed variables, but a new variable family, temporal semantics, or reusable formulation is an extension proposal pending review.

## Variable families

- Task selection/completion: binary `x[i]`; use an explicit `required` requirement or completion rows.
- Start assignment: binary `y[i,t]`, task `i` starts in slot `t`.
- Time: bounded integer slot `s[i]` or completion `c[i]`; continuous time only with a clear discretization and solver support.
- Inventory: bounded integer/continuous stock by part and time; inventory balance carries opening stock, arrivals, consumption, and replenishment.
- Weather/resource: binary operating eligibility or bounded capacity use by slot; source forecast and threshold are explicit data.
- Slack/unmet demand: bounded nonnegative continuous or integer variable with a visible penalty or service constraint. Never hide violations behind a penalty when they are actually hard requirements.

## Row families

- Authorized window: prohibit task starts/completions outside approved slots, or require assignment to one approved window. Encode approved periods as explicit eligible indices or validated binary rows.
- Inventory availability: cumulative consumption through slot `t` cannot exceed opening stock plus arrivals received by `t`. Parts arriving July 5 must not be available July 1–4.
- Weather eligibility/capacity: disallow work in unsafe slots; optionally limit work-hours per slot by forecast-derived capacity. The relation must be linear and its policy/data origin recorded.
- Task selection/completion: a mandatory repair must be selected/completed; optional tasks need explicit value/cost and no accidental default selection.
- Precedence: if task `j` follows `i`, use a registered time-indexed or bounded big-M template with finite, justified M. Without such a reviewed template, return an extension proposal.
- Resource capacity: aggregate assigned work in a slot within available capacity; non-overlap requires a reviewed time-indexed or sequencing formulation, not an informal claim.
- Deadline/service level: completion before deadline, or a bounded slack with a quantified penalty if late completion is allowed. “Prioritize” must be operationalized rather than guessed.

## Objective terms

- Expected lost energy/revenue: slot-specific outage energy times a known price or a precomputed fixed coefficient. If both price and outage quantity are fixed data, their product is a fixed coefficient; if both depend on decisions, the expression is not linear without a reviewed formulation.
- Delay risk/cost: explicit per-slot or per-task coefficients grounded in data.
- Direct cost: labor/material/vessel cost when in scope and supported by data.
- Makespan: bounded completion variable with a linear minimization objective or lexicographic tier.
- Unmet-priority penalty: only when a quantified tradeoff is intended; distinguish it from hard deadline constraints.

## Preference policy

Use hard constraints for safety, legal authorization, and non-negotiable service requirements. Use lexicographic objective tiers for true rank ordering. Use weighted sums only when stakeholders can explain/approve the tradeoff and coefficients use compatible units or explicit normalization. Record the interpretation of “severe first”, “soon”, “economic”, “do what can be finished”, and similar language.
