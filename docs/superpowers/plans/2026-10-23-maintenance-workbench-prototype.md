# Maintenance Workbench Prototype Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an Ant Design-inspired HTML prototype that demonstrates recommendation selection, plan review, and work-order progression.

**Architecture:** Keep the existing React/Vite shell and data store, but replace the current page hierarchy with focused prototype views. Isolate deterministic prototype data and formatting helpers from visual components so key business display rules can be tested.

**Tech Stack:** React 18, TypeScript, Vite, Vitest, Testing Library, Lucide icons, existing Tailwind utilities and custom CSS.

---

### Task 1: Prototype data model

**Files:**
- Create: `src/product/prototype-data.ts`
- Test: `src/product/__tests__/prototype-data.test.ts`

- [ ] Write failing tests for strategy labels, total economic cost and independent baseline-difference display，including positive and negative signs.
- [ ] Run the focused test and confirm the expected failure.
- [ ] Implement the minimal typed prototype model and formatters.
- [ ] Run the focused test and confirm it passes.

### Task 2: Application shell and navigation

**Files:**
- Modify: `src/App.tsx`
- Modify: `src/store/types.ts`
- Modify: `src/index.css`

- [ ] Replace the top-only navigation with a compact enterprise shell.
- [ ] Map the four product entrances explicitly: P01 workbench, P04 alert tasks, P06 maintenance list, and P05 resources/rules. P02 plan detail and P03 work-order detail are drill-down views with contextual back actions.
- [ ] Add responsive rules for desktop and narrow screens.

### Task 3: Decision-focused workbench

**Files:**
- Modify: `src/components/P01Dashboard.tsx`
- Create: `src/components/AttentionPanel.tsx`

- [ ] Implement strategy switching and a condition drawer.
- [ ] Render recommendation cards led by time and total cost.
- [ ] Render baseline differences separately and add clear detail actions.
- [ ] Add a compact attention list with contextual navigation.
- [ ] Add UI tests proving time and total cost lead each card, both baselines are visible, task/resource breakdowns are absent, and actionable attention items are ordered by urgency.

### Task 4: Plan detail and work-order flow

**Files:**
- Modify: `src/components/P02VoyageAnalysis.tsx`
- Modify: `src/components/P03TurbineCheck.tsx`

- [ ] Rebuild P02 as a plan detail with summary, window timeline, and the named tabs: tasks, people/vessels, spares, cost comparison, and unscheduled tasks.
- [ ] Keep calculation basis and version history collapsed by default.
- [ ] Make “选用并生成检修单” create a draft `WO-20261023-01` and open its detail, with a back action preserving plan context.
- [ ] Rebuild P03 as a six-stage work-order detail with phase-specific work areas and independent internal-review, maritime-permit, inventory-reservation, and actual-issue statuses.

### Task 5: Supporting list pages and verification

**Files:**
- Modify: `src/components/P04RuleManagement.tsx`
- Modify: `src/components/P05Analytics.tsx`
- Create: `src/components/P06MaintenanceList.tsx`
- Modify: `src/App.tsx`

- [ ] Reframe supporting pages as task and resource views.
- [ ] Add the P06 maintenance list, wire it to the primary navigation, and route “继续处理” into P03 with contextual back-navigation.
- [ ] Keep the “演示数据” label visible on prototype recommendation and plan-detail views; do not imply live-data integration.
- [ ] Run focused tests and the full test suite.
- [ ] Run the production build and inspect the generated HTML bundle.
