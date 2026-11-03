# Dynamic Maintenance MILP Modeler Skill Design

## Goal

Create and install a Codex skill that turns natural-language maintenance goals into dynamically composed, validated linear/MILP model specifications and selects available data, validation, and solver tools without allowing unverified nonlinear formulations.

## Product boundary

- The skill may instantiate variables from typed domains, compose registered objective/constraint templates, and add new linear rows using declared variables and known data.
- A new reusable modeling primitive is a proposal until its meaning, units, bounds, linearity, and examples are reviewed and validated.
- The skill does not emit solver code or claim an optimum. A separate deterministic validator/compiler and an available MILP solver must validate and solve the specification.
- Nonlinear expressions are rejected unless transformed through an explicitly registered, valid MILP linearization with finite bounds.

## Architecture

The package contains a concise `SKILL.md`, a typed ModelSpec contract, a domain component catalog, a strict LP/MILP boundary reference, worked examples, natural-language evaluation cases, and a standard-library Python validator. The validator checks declared variables, finite domains, linear-row structure, objective/constraint references, solver readiness, and nonlinear coefficient attempts. Evaluation cases cover composition, clarification, extension proposals, infeasibility, and rejection.

## Initial domain primitives

- Variables: binary selection/start/assignment/outage; bounded integer counts and time slots; bounded continuous work quantity, energy, and slack.
- Linear objective components: expected maintenance energy loss, delay loss/risk (when explicitly quantified), direct cost, makespan, and unmet-priority penalty. Priority tiers use lexicographic solving or explicit service constraints, not unexplained giant weights.
- Constraint templates: authorized windows, weather eligibility/capacity, inventory release dates, task precedence, resource capacity/non-overlap, completion deadlines, and task selection/completion.
- Data coefficients are constants for a given solve. Multiplying a data parameter by a decision variable is linear; multiplying two decision variables is not.

## Workflow

1. Parse the request into goals, hard constraints, soft preferences, and unresolved semantics.
2. Discover only available tools and relevant data; mark missing values rather than inventing them.
3. Compose a ModelSpec from typed variables, objective tiers, registered templates, and justified custom linear rows.
4. Validate linearity, bounds, references, units/provenance requirements, and solver compatibility.
5. If ambiguous or invalid, ask/return a clarification or an extension proposal; do not dispatch a solver.
6. Dispatch only a validated MILP to an available supported solver; report its actual status and expose assumptions and infeasibility.

## Verification

Use standard-library `unittest` for the validator and golden ModelSpec scenarios. Include positive tests for dynamic linear constraint composition and negative tests for variable-by-variable products, division by decisions, missing bounds/references, unsupported nonlinear operators, and unresolved data. Run the skill-creator quick validator and a focused scenario review after installation.
