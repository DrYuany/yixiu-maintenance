# Maintenance Scheduling Agent MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a runnable standalone maintenance scheduling Agent, then connect its canonical decision contract to a durable asynchronous real-solver lifecycle before calling the MVP complete.

**Architecture:** Reuse the existing React/Vite conversation store and FastAPI/SQLite backend, replacing the drawer-first workbench shell with a dedicated Agent workspace. A canonical decision contract sits between language planning and the solver; persisted solve jobs, independent verification, and evidence artifacts make every result replayable and honest.

**Tech Stack:** React 18, TypeScript, Vite, Vitest, FastAPI, Pydantic v2, SQLite, pytest.

---

## Acceptance and Non-Goals

- The normalized context fingerprint includes prompt/history, working memory, business-data snapshot identity, schema/compiler version, model/prompt version, and solver profile; volatile timestamps and trace IDs are excluded.
- The decision/model artifact passed to the solver is the artifact referenced by solve evidence.
- Primary solver status is exactly `OPTIMAL | FEASIBLE | UNKNOWN | INFEASIBLE`; incumbent availability and failure phase are separate fields.
- The 180-second threshold is soft: the job continues and the user may wait, move it to background, accept an incumbent when present, or stop it.
- Decision artifacts, transitions, and verification evidence survive reload and conversation switching.
- The MVP plans only. It does not create or mutate work orders, approvals, manifests, inventory, or execution records.

### Task 1: Canonical Decision Contract

**Files:**
- Create: `backend/app/core/decision_contract.py`
- Modify: `backend/app/schemas/agent.py`
- Test: `backend/tests/test_decision_contract.py`

- [ ] Write tests for stable canonical JSON and fingerprints across key ordering, set-like list ordering, numeric normalization, locale/timezone, and fresh compiler instances.
- [ ] Write tests proving prompt/history, working memory, business state, schema/compiler/model/prompt/solver profile, objective order, and numeric changes alter the correct fingerprint while volatile fields do not.
- [ ] Run the tests and verify they fail because the contract module does not exist.
- [ ] Implement canonicalization, explicit metadata, context/decision/model fingerprints, and serialization.
- [ ] Run `python -m pytest backend/tests/test_decision_contract.py -q`.

### Task 2: Honest Solver State and Soft Waiting Policy

**Files:**
- Create: `backend/app/core/solve_policy.py`
- Modify: `backend/app/schemas/agent.py`
- Test: `backend/tests/test_solve_policy.py`

- [ ] Write tests for primary `OPTIMAL`, `FEASIBLE`, `UNKNOWN`, and `INFEASIBLE` with orthogonal incumbent, proof, phase, and reason fields.
- [ ] Write fake-clock tests proving 180 seconds requests a user decision without stopping or relabeling the run.
- [ ] Run the tests and verify they fail because the policy module does not exist.
- [ ] Implement the state mapper and soft-threshold policy.
- [ ] Run `python -m pytest backend/tests/test_solve_policy.py -q`.

### Task 3: Durable Solve Jobs, Solver Adapter, and Independent Evidence

**Files:**
- Create: `backend/app/core/solve_jobs.py`
- Create: `backend/app/core/solution_verifier.py`
- Modify: `backend/app/core/agent_runtime.py`
- Modify: `backend/app/core/skill_executor.py`
- Modify: `backend/app/schemas/agent.py`
- Modify: `backend/app/api/agent.py`
- Modify: `backend/app/api/conversations.py`
- Modify: `backend/app/db/database.py`
- Modify: `backend/app/db/models.py`
- Modify: `backend/app/main.py`
- Modify: `backend/tests/test_agent_api.py`
- Create: `backend/tests/test_solve_jobs.py`
- Create: `backend/tests/test_solution_verifier.py`

- [ ] Write fake-clock tests for persisted transitions, polling, prompt, background continuation, conditional incumbent acceptance, stop behavior, and reload.
- [ ] Write corruption tests for artifact hash, assignment feasibility, objective recomputation, bound/gap, and false optimality.
- [ ] Write an integration test proving the response fingerprint equals the exact artifact given to the solver adapter.
- [ ] Run the tests and verify the expected missing seams.
- [ ] Implement persisted jobs, status/control endpoints, canonical solver adapter, and independent verifier.
- [ ] Run `python -m pytest backend/tests/test_solve_jobs.py backend/tests/test_solution_verifier.py backend/tests/test_agent_api.py -q`.

### Task 4: Standalone Three-Column Agent Workspace

**Files:**
- Create: `src/components/agent/AgentWorkspace.tsx`
- Create: `src/components/agent/ConversationRail.tsx`
- Create: `src/components/agent/ConversationCanvas.tsx`
- Create: `src/components/agent/EvidencePanel.tsx`
- Create: `src/components/agent/SolveWaitDialog.tsx`
- Modify: `src/App.tsx`
- Modify: `src/store/types.ts`
- Modify: `src/store/agent-store.tsx`
- Modify: `src/lib/api-client.ts`
- Modify: `src/styles/globals.css`
- Test: `src/components/agent/__tests__/AgentWorkspace.test.tsx`

- [ ] Write tests for the three columns, decision evidence, honest states, job polling/control, and responsive behavior.
- [ ] Write a test proving the wait dialog offers wait/background/incumbent/stop and never says “无解” for `UNKNOWN`.
- [ ] Run the tests and verify they fail against the drawer-only product.
- [ ] Implement the rail, Codex-style timeline, evidence panel, polling/actions, and responsive layout.
- [ ] Switch the entry point to the Agent product while preserving workbench code for later integration.
- [ ] Run `npm test -- src/components/agent/__tests__/AgentWorkspace.test.tsx`.

### Task 5: Deterministic Evaluator and Release Gates

**Files:**
- Create: `backend/app/evals/__init__.py`
- Create: `backend/app/evals/scorers.py`
- Create: `backend/app/evals/run_eval.py`
- Create: `backend/eval_cases/compile_cases.jsonl`
- Test: `backend/tests/test_eval_scorers.py`
- Create: `backend/tests/test_eval_runner.py`
- Modify: `README.md`

- [ ] Write scorer tests for repeatability, hard-constraint precision/recall, status honesty, evidence integrity, and completeness.
- [ ] Write CLI tests proving critical failures exit nonzero and passing L0/L1/L2/L2b/L3 gates exit zero.
- [ ] Run the tests and verify expected failures.
- [ ] Implement dependency-free scorers, versioned cases, explicit thresholds, and the runner CLI.
- [ ] Document local gates as authoritative and Langfuse as trace/experiment storage.
- [ ] Run `python -m pytest backend/tests/test_eval_scorers.py backend/tests/test_eval_runner.py -q`.

### Task 6: Integrated Verification and Standalone Artifact

**Files:**
- Restrict fixes to files named by the failing task; update this plan before entering another subsystem.

- [ ] Run `python -m pytest backend/tests -q`.
- [ ] Run `npm test`.
- [ ] Run `npm run build` and confirm `检修窗口原型.html` is regenerated as a directly openable standalone file.
- [ ] Inspect changed files and list any unfinished acceptance item without claiming the MVP complete.
