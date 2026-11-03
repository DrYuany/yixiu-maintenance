# Dynamic Maintenance MILP Skill Implementation Plan

> **For agentic workers:** Use this checklist to implement and verify the approved dynamic MILP modeling skill in the current project and install it for the current Windows user.

**Goal:** Build a Codex skill that dynamically composes maintenance optimization models while enforcing an auditable LP/MILP-only boundary.

**Architecture:** Natural-language guidance emits a typed ModelSpec built from declared variables, linear objective terms, registered templates, and custom linear rows. A small deterministic validator gates solver readiness and rejects nonlinear structure; tests use standard-library `unittest` and golden scenarios.

**Tech Stack:** Markdown, JSON, Python 3 standard library, Codex skill package.

---

### Task 1: Define and test ModelSpec validation

**Files:**
- Create: `maintenance-milp-modeler/scripts/validate_model_spec.py`
- Create: `maintenance-milp-modeler/tests/test_validate_model_spec.py`

- [x] Write tests for valid dynamic linear rows, invalid variable products, undeclared variables, missing finite bounds, and solver gating on unresolved specs.
- [x] Run tests and verify expected failures because the validator is not implemented.
- [x] Implement the minimal validator and run tests to green.

### Task 2: Write the reusable skill package

**Files:**
- Create: `maintenance-milp-modeler/SKILL.md`
- Create: `maintenance-milp-modeler/references/model-spec.md`
- Create: `maintenance-milp-modeler/references/milp-components.md`
- Create: `maintenance-milp-modeler/references/linear-milp-boundary.md`
- Create: `maintenance-milp-modeler/references/examples.md`
- Create: `maintenance-milp-modeler/tests/scenarios.json`

- [x] Define routing against the existing fixed-parameter Skill, ready/clarify/propose/reject statuses, and permitted solver-tool discovery.
- [x] Define variable, objective, constraint, data provenance, and extension-proposal contracts.
- [x] Add examples for the severe-fault July schedule and nonlinear boundary cases.
- [x] Validate every ready golden scenario with the Python validator.

### Task 3: Evaluate scenarios and install the skill

**Files:**
- Create: `maintenance-milp-modeler/TEST_REPORT.md`
- Install: `C:\Users\Lenovo\.codex\skills\maintenance-milp-modeler\`

- [x] Run `python -m unittest discover -s maintenance-milp-modeler/tests -v`.
- [x] Run the skill-creator `quick_validate.py` against the package.
- [x] Review every golden natural-language case against the skill instructions and record pass/fail evidence.
- [x] Copy the verified package to the user Codex skills directory and verify installed/project copies match.
