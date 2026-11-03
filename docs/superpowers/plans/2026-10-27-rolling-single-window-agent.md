# 最近完整窗口排程 Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将检修排程 Agent 改为每日只优化“规划时点七天后开始的第一个完整可用窗口”，扩展到 90 天数据观察范围，并在对话中以可交互卡片完整呈现可执行结果、状态和审计依据。

**Architecture:** 在建模层新增独立窗口选择器，保留 90 天观察窗口并输出唯一求解窗口；截止期先基于观察窗口固化，再让候选生成、两套人工基准、CP-SAT 和复核共享同一个单窗口上下文。后端把求解产物规范化为版本化 UI Schema，前端只消费结构化字段渲染进度、窗口、指标、时间轴、未安排任务和审计信息。

**Tech Stack:** Python 3.11、OR-Tools CP-SAT、FastAPI、Pydantic、SQLite、React 18、TypeScript、Vite、Vitest、Testing Library、Lucide React。

**Specification:** `docs/superpowers/specs/2026-10-26-rolling-single-window-planning-design.md`

**Version-control note:** 当前工作区包含大量用户文件和未跟踪内容；本计划不执行提交，除非用户另行明确要求。

---

## File Map

- Create `maintenance-milp-modeler/scripts/planning_horizon.py`: 90 天观察边界、七天准备边界、窗口资格与唯一目标窗口选择。
- Modify `maintenance-milp-modeler/scripts/product_data.py`: 分离观察窗口和求解窗口，先固化截止期，再构造单窗口上下文。
- Modify `maintenance-milp-modeler/scripts/product_planner.py`: 单窗口建模、确定性最终决胜、状态与基准结果契约。
- Modify `maintenance-milp-modeler/scripts/product_report.py`: 单窗口报告、规划边界、跳过原因和状态文案。
- Modify `maintenance-milp-modeler/scripts/generate_base_scenarios.py`: 生成并校验 90 天合成基座。
- Modify `data/场景评测/五组基座配置.json`: 将五组场景扩展为 90 天和完整窗口目录。
- Regenerate `data/场景评测/五组基座数据/**`: 生成 90 天窗口、每日海况和逐小时预测。
- Create `maintenance-milp-modeler/tests/test_planning_horizon.py`: 窗口边界、状态和确定性测试。
- Modify `maintenance-milp-modeler/tests/test_product_runtime.py`: 单窗口上下文、截止期和真实排程测试。
- Modify `maintenance-milp-modeler/tests/test_base_scenarios.py`: 90 天数据完整性测试。
- Modify `backend/app/schemas/agent.py`: UI Schema、进度、终止原因、基准状态和合法空值。
- Modify `backend/app/core/decision_contract.py`: 完整模型指纹输入及运行模式命名空间。
- Modify `backend/app/core/planning_intent.py`: 传递规划时点、运行模式、快照 ID 和 ingestion watermark。
- Create `backend/app/core/planning_snapshot.py`: 一次性冻结求解输入并供指纹和后台求解共同读取。
- Modify `backend/app/db/database.py`: 增加不可变快照表和兼容迁移。
- Modify `backend/app/main.py`: 支持测试注入数据库、快照仓库和输出根。
- Modify `backend/app/core/maintenance_product_executor.py`: 进度回调、硬时限、取消和求解状态映射。
- Modify `backend/app/core/solve_jobs.py`: 状态与终止原因分离、七阶段进度、三分钟单次提示。
- Modify `backend/app/core/result_normalizer.py`: 产品结果转换为版本化 UI Schema。
- Modify `backend/app/core/agent_runtime.py`: 保存 UI 结果、生成用户友好文本回退并接通作业控制。
- Modify `backend/tests/test_decision_contract.py`, `test_solve_jobs.py`, `test_result_normalizer.py`, `test_agent_api.py`: 后端契约和 API 回归。
- Modify `backend/tests/test_planning_intent.py`: 规划时点、运行模式和快照字段测试。
- Create `backend/tests/test_real_agent_e2e.py`: 不 mock 求解器的真实 Agent 集成测试。
- Create `backend/tests/test_backward_compatibility.py`: 历史多窗口版本和旧作业载荷兼容测试。
- Modify `src/store/types.ts`: TypeScript UI Schema 和状态联合类型。
- Modify `src/store/agent-store.tsx`: 轮询进度、终止原因和结构化结果。
- Create `src/components/agent/SolveProgressCard.tsx`: 七阶段进度到四步视觉进度的映射。
- Create `src/components/agent/PlanningResultCard.tsx`: 结果总容器与状态卡。
- Create `src/components/agent/TargetWindowCard.tsx`: 唯一目标窗口、七天边界与天气摘要。
- Create `src/components/agent/ScheduleTimeline.tsx`: 作业线时间轴。
- Create `src/components/agent/UnscheduledTasksCard.tsx`: 未安排状态与原因。
- Create `src/components/agent/AuditDetails.tsx`: 模型指纹、数据截止点、参数和基准依据。
- Create `src/components/agent/RecommendationReasons.tsx`: 最多三条结构化推荐原因。
- Modify `src/components/agent/ConversationCanvas.tsx`: 在聊天消息流中渲染进度和结构化结果。
- Modify `src/components/agent/SolveWaitDialog.tsx`: 根据复核通过的 incumbent 调整按钮文案和动作。
- Modify `src/components/agent/AgentWorkspace.tsx`: 向对话画布传入活动版本和结构化结果。
- Modify `src/index.css`: 亲和卡片、状态色、动画、窄屏和无障碍样式。
- Create `src/components/agent/__tests__/PlanningResultCard.test.tsx`: 状态矩阵和 UI 可访问性测试。
- Modify `src/components/agent/__tests__/AgentWorkspace.test.tsx`: 进度、等待和完整交互回归。
- Create `tests/e2e/agent-workspace.spec.ts`: 320px 布局、键盘、焦点和浏览器无障碍检查。
- Create `playwright.config.ts`: 使用本机 Chrome 的可重复浏览器验收配置。
- Modify `package.json`, `package-lock.json`: 增加 Playwright 与 axe 浏览器验收依赖和脚本。

### Task 1: Implement deterministic target-window selection

**Files:**
- Create: `maintenance-milp-modeler/scripts/planning_horizon.py`
- Create: `maintenance-milp-modeler/tests/test_planning_horizon.py`

- [ ] **Step 1: Write boundary-first failing tests**

```python
def test_skips_window_containing_planning_boundary():
    selection = select_target_window(
        as_of="2026-10-23T18:00:00+08:00",
        windows=[
            window("W01", "2026-10-28T00:00:00+08:00", "2026-11-01T00:00:00+08:00"),
            window("W02", "2026-11-02T00:00:00+08:00", "2026-11-07T00:00:00+08:00"),
        ],
    )
    assert selection.target_window["编号"] == "W02"
    assert selection.skipped_windows[0]["reason"] == "STARTS_BEFORE_PLANNING_BOUNDARY"

def test_accepts_window_starting_exactly_at_boundary():
    selection = select_target_window(
        as_of="2026-10-23T00:00:00+08:00",
        windows=[window("W01", "2026-10-30T00:00:00+08:00", "2026-11-04T00:00:00+08:00")],
    )
    assert selection.target_window["编号"] == "W01"
```

- [ ] **Step 2: Add tests for 90-day endpoints and invalid input**

Cover `end <= observation_end`, no eligible window, duplicate IDs, missing IDs, stable `(start_utc, window_id)` ordering, production approval filtering and hypothetical mode separation.

- [ ] **Step 3: Run the focused test and verify failure**

Run: `python -m pytest maintenance-milp-modeler/tests/test_planning_horizon.py -q`

Expected: FAIL because `planning_horizon` does not exist.

- [ ] **Step 4: Implement the focused selector**

```python
@dataclass(frozen=True)
class WindowSelection:
    as_of: datetime
    planning_boundary: datetime
    observation_end: datetime
    target_window: dict | None
    skipped_windows: list[dict]
    status: str

def select_target_window(as_of, windows, lead_days=7, horizon_days=90, run_mode="HYPOTHETICAL"):
    # Normalize aware datetimes, reject duplicate IDs, filter by approval and
    # full containment, then choose min(start_utc, window_id).
    ...
```

- [ ] **Step 5: Run focused tests**

Run: `python -m pytest maintenance-milp-modeler/tests/test_planning_horizon.py -q`

Expected: PASS.

### Task 2: Expand synthetic observation data to 90 days

**Files:**
- Modify: `data/场景评测/五组基座配置.json`
- Modify: `maintenance-milp-modeler/scripts/generate_base_scenarios.py`
- Modify: `maintenance-milp-modeler/tests/test_base_scenarios.py`
- Regenerate: `data/场景评测/五组基座数据/**`

- [ ] **Step 1: Update tests to require 90 complete days**

按时间戳断言每个基座完整覆盖 `[as_of, as_of + 90×24h)`，而不是固定依赖自然日行数。因为规划时点为 18:00，底层数据需要覆盖额外的自然日/小时。另断言窗口目录来源、发布时间、审批状态、唯一编号、观察终点和至少一个七天边界后的完整可用窗口。

- [ ] **Step 2: Run the base test and verify failure**

Run: `python -m pytest maintenance-milp-modeler/tests/test_base_scenarios.py -q`

Expected: FAIL because the current horizon is 35 days.

- [ ] **Step 3: Extend configuration and generator validation**

将底层生成范围扩展到足以覆盖精确 90×24 小时观察区间（当前 18:00 规划时点至少需要第 91 个自然日），为五组基座增加贯穿观察期的显式合成窗口，保持最少三天空档，并让生成器拒绝时间序列覆盖不足、窗口目录缺少来源/发布时间/审批状态或七天边界后没有完整窗口的场景。

- [ ] **Step 4: Regenerate all five bases**

Run: `python maintenance-milp-modeler/scripts/generate_base_scenarios.py --config data/场景评测/五组基座配置.json --output data/场景评测/五组基座数据`

Expected: five base directories plus重新生成的摘要，其时间戳范围完整覆盖精确观察区间。

- [ ] **Step 5: Verify generated data**

Run: `python -m pytest maintenance-milp-modeler/tests/test_base_scenarios.py -q`

Expected: PASS.

### Task 3: Build a single-window Snapshot without losing deadline context

**Files:**
- Modify: `maintenance-milp-modeler/scripts/product_data.py`
- Modify: `maintenance-milp-modeler/tests/test_product_runtime.py`

- [ ] **Step 1: Add failing Snapshot tests**

Verify that `observation_windows` contains the full 90-day directory, `windows` contains exactly one target window, `planning_boundary` is persisted, received alerts use `first_received_at <= as_of`, and task deadlines retain the source window from the observation directory. Add cases for versioned duplicate-alert merging, future-alert exclusion, frozen ingestion watermark and isolation between one day's snapshot and the next day's new alerts.

- [ ] **Step 2: Add missing-data status tests**

Test `NO_ELIGIBLE_WINDOW`, `MISSING_WINDOW_DATA`, `MISSING_DEADLINE_DATA` and `MISSING_FORECAST_DATA` as distinct load/prepare outcomes that never become solver `INFEASIBLE`.

- [ ] **Step 3: Run focused tests and verify failure**

Run: `python -m pytest maintenance-milp-modeler/tests/test_product_runtime.py -q`

Expected: FAIL because the current Snapshot exposes all windows to the solver.

- [ ] **Step 4: Split observation and solve scopes**

Add `observation_windows`, `window_selection`, `planning_boundary`, `observation_end` and `run_mode` to `Snapshot`. Compute deadlines from `observation_windows` before replacing `windows` with `[target_window]`. Record deadline source IDs and window-selection audit metadata.

- [ ] **Step 5: Scope forecasts and cost horizon correctly**

Retain the observation data needed for deadline and input validation, but set the solve context and planner end to the target-window end. Reject target-window forecast gaps before solver dispatch.

- [ ] **Step 6: Run focused tests**

Run: `python -m pytest maintenance-milp-modeler/tests/test_product_runtime.py maintenance-milp-modeler/tests/test_planning_horizon.py -q`

Expected: PASS.

### Task 4: Make single-window solving and reporting deterministic and honest

**Files:**
- Modify: `maintenance-milp-modeler/scripts/product_planner.py`
- Modify: `maintenance-milp-modeler/scripts/product_report.py`
- Modify: `maintenance-milp-modeler/tests/test_product_runtime.py`
- Modify: `maintenance-milp-modeler/tests/test_live_calculation.py`

- [ ] **Step 1: Add failing solver-contract tests**

Assert that no candidate, decision variable, schedule row or report window references a later observation window; both baselines use the same target window; non-comparable task sets have no savings value; and `OPTIMAL + COMPLETED` cold runs produce identical schedule rows.

- [ ] **Step 2: Run planner contract tests and verify failure**

Run: `python -m pytest maintenance-milp-modeler/tests/test_product_runtime.py maintenance-milp-modeler/tests/test_live_calculation.py -q`

Expected: FAIL because later windows still enter candidate generation and termination reason is not separate.

- [ ] **Step 3: Separate solution status from termination reason**

Return `solution_status`, `termination_reason`, `has_verified_incumbent`, completed objective stages and review status. Map timeout/cancel with a verified incumbent to `FEASIBLE`, without one to `UNKNOWN`, and review failure to `VALIDATION_FAILED`.

- [ ] **Step 4: Add collision-free canonical final tie-break**

After business objective stages are proven optimal, order the complete binary choice vector by `(task_id, start, line)` and fix it lexicographically through sequential solver stages. Prove in tests that every auxiliary interval, rental, completion and loss variable is uniquely determined by the fixed choice vector; otherwise append the undetermined auxiliary variable to the lexicographic vector. Every tie-break stage shares the same remaining hard time budget. If the budget expires before the complete vector is fixed, return `FEASIBLE + TIME_LIMIT`, never `OPTIMAL`. Do not use a summed rank expression because different assignments can collide. Include solver version and every search parameter in report metadata, and test a deliberately symmetric case with multiple business-optimal schedules across cold starts.

- [ ] **Step 5: Version both baselines**

Add stable tie rules and structured statuses `COMPARABLE`, `NOT_COMPARABLE`, `BASELINE_INFEASIBLE`; calculate money to cents and expose savings only for comparable task sets.

- [ ] **Step 6: Update report wording**

Lead with one target window, planning boundary, skipped-window reason, current alert cutoff, recommended tasks and total cost. Keep observation windows inside audit detail only.

- [ ] **Step 7: Run planner tests**

Run: `python -m pytest maintenance-milp-modeler/tests/test_product_runtime.py maintenance-milp-modeler/tests/test_live_calculation.py -q`

Expected: PASS.

### Task 5: Extend fingerprints, jobs and the versioned UI result schema

**Files:**
- Modify: `backend/app/schemas/agent.py`
- Modify: `backend/app/core/decision_contract.py`
- Modify: `backend/app/core/planning_intent.py`
- Create: `backend/app/core/planning_snapshot.py`
- Modify: `backend/app/db/database.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/core/maintenance_product_executor.py`
- Modify: `backend/app/core/solve_jobs.py`
- Modify: `backend/app/core/result_normalizer.py`
- Modify: `backend/app/core/agent_runtime.py`
- Modify: `backend/tests/test_decision_contract.py`
- Modify: `backend/tests/test_planning_intent.py`
- Modify: `backend/tests/test_solve_jobs.py`
- Modify: `backend/tests/test_result_normalizer.py`
- Modify: `backend/tests/test_agent_api.py`
- Create: `backend/tests/test_backward_compatibility.py`

- [ ] **Step 1: Add Pydantic contract tests before schema changes**

Create fixtures for `OPTIMAL`, `FEASIBLE + TIME_LIMIT`, `NO_ELIGIBLE_WINDOW`, `MISSING_WINDOW_DATA`, `MISSING_DEADLINE_DATA`, `MISSING_FORECAST_DATA`, `INVALID_INPUT`, `UNKNOWN`, `INFEASIBLE`, `VALIDATION_FAILED` and `ERROR`, covering all four termination reasons. Assert legal null combinations and reject an executable schedule for every status except `FEASIBLE|OPTIMAL`.

- [ ] **Step 2: Add failing snapshot and lifecycle tests**

Prove that a job can be created with `modelFingerprint=null` and `snapshotRef=null`, then atomically receives `snapshotRef + snapshotHash + modelFingerprint` after freezing one `as_of/run_mode/snapshot_id/watermark`. Assert background execution reads that exact immutable snapshot. Assert a next-day alert creates a new snapshot without changing the old version. Assert every pre-solver status persists as a completed run record with its failing progress stage and never calls CP-SAT. Assert cancellation/time limit preserve an incumbent for review instead of deleting output.

- [ ] **Step 3: Run focused backend tests and verify failure**

Run: `python -m pytest backend/tests/test_planning_intent.py backend/tests/test_decision_contract.py backend/tests/test_solve_jobs.py backend/tests/test_result_normalizer.py backend/tests/test_agent_api.py -q`

Expected: FAIL because requests do not carry a frozen snapshot, pre-solver failures are exceptions, and cancelled runs discard output.

- [ ] **Step 4: Define the versioned result models**

Add `PlanningUiResult`, `SolveProgress`, `TargetWindowView`, `BaselineView`, `UnscheduledTaskView`, `SolutionStatus` and `TerminationReason`. Allow `SolveJob.modelFingerprint`, `snapshotRef` and `snapshotHash` to be null only before snapshot completion. Store the normalized result on `WindowVersion` and in completed job results, and mirror the legal pre-fingerprint null state in frontend types.

- [ ] **Step 5: Freeze planning input once**

Extend `PlanningRequest` with explicit `as_of`, `run_mode`, `snapshot_id` and `ingestion_watermark`. Create a canonical frozen input snapshot once and persist it through `PlanningSnapshotRepository` in a SQLite `planning_snapshots` table using one transaction. Atomically update the job with `snapshotRef + snapshotHash + modelFingerprint`; make both fingerprint compilation and `execute()` read that exact immutable snapshot. Move snapshot creation into the background job lifecycle so the job exists before `SNAPSHOT` begins; never rely on thread memory for snapshot persistence.

- [ ] **Step 6: Expand the model fingerprint**

Include run mode, UTC planning time, planning boundary, 90-day input hashes, target window, alert watermark, fixed deadlines, generator/reviewer/app versions, solver version, seed, worker count and all solve parameters. Add separate production and hypothetical cache namespaces.

- [ ] **Step 7: Add seven-stage progress and normal pre-solver terminal states**

Persist `SNAPSHOT`, `DEADLINE_CALCULATION`, `WINDOW_SELECTION`, `INPUT_VALIDATION`, `MODEL_BUILD`, `SOLVING`, `CANCELLING`, `REVIEWING` and terminal stages, their timestamps and error code. Persist `NO_ELIGIBLE_WINDOW`, `MISSING_WINDOW_DATA`, `MISSING_DEADLINE_DATA`, `MISSING_FORECAST_DATA` and `INVALID_INPUT` as normal terminal results rather than failed jobs, and prove CP-SAT was not called. Emit the first `needsUserDecision` at 180 seconds; after “继续等待”, do not re-open unless explicitly configured.

- [ ] **Step 8: Preserve and review incumbents on stop**

The control endpoint records `cancelRequestedAt` and moves the job to `CANCELLING`; it must not call `mark_cancelled()` or return a terminal state immediately. Both “停止求解” and “停止并查看当前可行方案” call `StopSearch()`. After search stops, move to `REVIEWING`, retain output, collect any incumbent and run independent review. Only then terminate as `FEASIBLE|UNKNOWN|VALIDATION_FAILED` with `USER_CANCELLED`. Test that polling remains non-terminal during review, that the reviewed-incumbent action really stops search, and that process restart maps to `SYSTEM_INTERRUPTED`, never mathematical infeasibility.

- [ ] **Step 9: Preserve historical payload compatibility**

Add migration/default tests using a pre-change SQLite fixture whose `WindowVersion` lacks `planningUiResult` and whose `SolveJob` lacks snapshot, progress and termination fields. Old multi-window results must load read-only, render through the legacy fallback and remain distinguishable from new single-window runs.

- [ ] **Step 10: Normalize product reports without guessing**

Map every supported product status to the UI Schema. Preserve legal nulls, use `candidateSchedule` only for review failures, and generate a complete plain-text fallback from the same structured object.

- [ ] **Step 11: Run backend tests**

Run: `python -m pytest backend/tests/test_decision_contract.py backend/tests/test_solve_jobs.py backend/tests/test_result_normalizer.py backend/tests/test_agent_api.py -q`

Expected: PASS.

### Task 6: Build rich, friendly structured chat output

**Files:**
- Modify: `src/store/types.ts`
- Modify: `src/store/agent-store.tsx`
- Create: `src/components/agent/SolveProgressCard.tsx`
- Create: `src/components/agent/PlanningResultCard.tsx`
- Create: `src/components/agent/TargetWindowCard.tsx`
- Create: `src/components/agent/ScheduleTimeline.tsx`
- Create: `src/components/agent/UnscheduledTasksCard.tsx`
- Create: `src/components/agent/AuditDetails.tsx`
- Create: `src/components/agent/RecommendationReasons.tsx`
- Modify: `src/components/agent/ConversationCanvas.tsx`
- Modify: `src/components/agent/SolveWaitDialog.tsx`
- Modify: `src/components/agent/AgentWorkspace.tsx`
- Modify: `src/index.css`
- Create: `src/components/agent/__tests__/PlanningResultCard.test.tsx`
- Modify: `src/components/agent/__tests__/AgentWorkspace.test.tsx`

- [ ] **Step 1: Add UI fixtures and failing component tests**

Cover all solution states, legal nulls, hypothetical badge, non-completed termination reason, non-comparable baselines, at most three structured recommendation reasons, keyboard-accessible audit disclosure, ARIA live progress and verified-incumbent button wording. Verify the plain-text fallback carries the same non-decorative semantics.

- [ ] **Step 2: Run focused component tests and verify failure**

Run: `npm test -- --run src/components/agent/__tests__/PlanningResultCard.test.tsx src/components/agent/__tests__/AgentWorkspace.test.tsx`

Expected: FAIL because structured result components and discriminated unions do not exist.

- [ ] **Step 3: Add exact TypeScript discriminated unions**

Mirror the backend schema instead of using optional catch-all objects. Make `executableSchedule` available only to `FEASIBLE|OPTIMAL` branches and carry reason codes for every empty state.

- [ ] **Step 4: Implement progress and status cards**

Map seven backend phases to four compact visual steps. Render dedicated empty/error cards for no window, missing inputs, unknown, infeasible and validation failure; never substitute zero values for null.

- [ ] **Step 5: Implement successful result visualization**

Render a friendly target-window hero, cost/task metric tiles, baseline comparison states, responsive line timeline, up to three structured “为什么推荐” reasons, collapsible unscheduled tasks and audit details. Use icons plus text for severity and status.

- [ ] **Step 6: Integrate cards into the message stream**

Attach progress and the completed result to the relevant assistant turn. Keep ordinary question answers as normal text messages and preserve the existing duplicate-submit guard.

- [ ] **Step 7: Update long-running dialog semantics**

Always show “停止求解”; only show “停止并查看当前可行方案” after the backend reports a reviewed incumbent. Keep “继续等待” and background operation without implying infeasibility.

- [ ] **Step 8: Add responsive and accessible styling**

At 320px, prevent page-level horizontal overflow; allow internal timeline/metric scrolling. Provide visible `:focus-visible`, reduced-motion behavior, non-color status text and WCAG 2.2 AA contrast.

- [ ] **Step 9: Run frontend tests**

Run: `npm test -- --run src/components/agent/__tests__/PlanningResultCard.test.tsx src/components/agent/__tests__/AgentWorkspace.test.tsx`

Expected: PASS.

### Task 7: Prove the complete path works

**Files:**
- Modify: `backend/tests/test_agent_api.py`
- Create: `backend/tests/test_real_agent_e2e.py`
- Create: `backend/tests/test_backward_compatibility.py`
- Modify: `maintenance-milp-modeler/TEST_REPORT.md`
- Modify: `README.md`
- Create: `tests/e2e/agent-workspace.spec.ts`
- Create: `playwright.config.ts`
- Modify: `package.json`
- Modify: `package-lock.json`

- [ ] **Step 1: Add an API end-to-end test with a real normalized report**

Modify `MaintenanceProductExecutor` to accept independent `project_root`, `snapshot_repository` and `output_root`, and modify `create_app()` to accept or construct those dependencies from explicit settings. Create `test_real_agent_e2e.py` that injects a temporary SQLite database, temporary snapshot repository and unique temporary output root, does not monkeypatch planner or executor, submits “按高风险优先重新排程”, waits for completion, and asserts one target window beginning at or after the seven-day boundary, valid UI Schema, matching frozen-snapshot fingerprint, independent review and persisted conversation result. Assert the temporary output starts empty and no old cache was read. Never clear or overwrite the existing `product-output` tree.

- [ ] **Step 2: Run all modeler and backend tests**

Run: `python -m pytest maintenance-milp-modeler/tests backend/tests -q`

Expected: all tests PASS.

- [ ] **Step 3: Add browser-level layout and accessibility acceptance**

Add `@playwright/test` and `@axe-core/playwright` dev dependencies. Configure `playwright.config.ts` with two self-starting `webServer` processes: FastAPI on `127.0.0.1:8010` and Vite on `127.0.0.1:3000`. Inject a temporary DB path, snapshot store and output root through environment variables, and disable automatic browser opening. Use route interception for deterministic UI status fixtures and one unmocked happy-path test for the running stack. Prefer installed Chrome via `channel: "chrome"`; add a preflight that gives an actionable error if Chrome is unavailable and document `npx playwright install chrome` as the fallback. Assert no page-level horizontal overflow, all controls are reachable by keyboard with visible focus, live progress is announced, dialogs trap/restore focus, and axe reports no WCAG 2.2 AA violations in the supported result fixtures.

Run: `npm run test:e2e`

Expected: browser tests PASS against the local application.

- [ ] **Step 4: Run all frontend tests and production build**

Run: `npm test`

Expected: all tests PASS.

Run: `npm run build`

Expected: Vite build succeeds and `检修窗口原型.html` is regenerated as a standalone file.

- [ ] **Step 5: Execute a real single-window CP-SAT run**

Create a unique temporary output directory, then run: `python maintenance-milp-modeler/scripts/maintenance_product.py --project . plan --as-of 2026-10-23T18:00:00+08:00 --seconds 300 --output <unique-temp-output>`

Expected: the result contains exactly one target window, starts no earlier than `2026-10-30T18:00:00+08:00`, has a truthful solver status, passes independent review and produces the report artifacts.

- [ ] **Step 6: Verify deterministic cache and cold-run behavior**

Repeat the same request through the Agent API and compare canonical business results while excluding job IDs, message IDs, timestamps and transport metadata. Run a fresh output directory and compare target window, model fingerprint, status and objective layers; if both runs are `OPTIMAL + COMPLETED`, compare full schedule rows from the collision-free lexicographic decision vector.

- [ ] **Step 7: Smoke-test the running product**

Start backend and frontend, submit an ordinary algorithm question and a planning request, observe progress, open audit details, exercise the three-minute control through a shortened test configuration, and verify successful result cards and non-success state cards.

- [ ] **Step 8: Update operator documentation and test evidence**

Document the seven-day boundary, 90-day observation range, single-window behavior, status meanings, startup commands, DeepSeek configuration via local environment variables, and the exact verification results. Never include the API key value.
