---
name: maintenance-window-forecast
description: Use when users ask to adjust maintenance optimization priorities, time windows, turbine scope, component scope, or component parameters for the maintenance window forecasting script, or ask to compare supported preset strategies.
---

# Maintenance Window Forecast

## Overview
Use this skill when the user is interacting with the maintenance window forecasting script as a decision engine. The model must translate business language into supported parameter updates, execute the solver, and explain results without inventing new optimization logic.

Core rule: only map requests into already-supported parameters. If the user is actually asking for a new mathematical constraint or a new objective, say the current script cannot implement that directly.

## When To Use

Use this skill for requests like:

- "经济性调高点"
- "风险优先"
- "尽量快修"
- "时间窗改48小时"
- "最多同时修2个部件"
- "只看1号和2号风机"
- "去掉变频器"
- "发电机维修时长改成10小时"
- "齿轮箱严重性提高到12"
- "对比经济优先和风险优先"

Do not use this skill for:

- asking the model to invent a new constraint
- asking the model to rewrite the MILP
- asking the model to optimize on unsupported concepts

## Required References

Before interpreting user language, read:

- `rules.md`
- `examples.md`

This rules file defines:

- supported intent classes
- allowed parameter fields
- fuzzy wording interpretation
- unsupported requests and refusal behavior

## Supported Intent Classes

Map user requests only into these categories:

- weight adjustments
- supported constraint-parameter adjustments
- turbine/component scope adjustments
- supported component parameter adjustments
- preset strategy comparisons

If the request does not fit one of these, do not force a mapping.

## Execution Flow

1. Read the user request carefully.
2. Classify it using the rules document.
3. If it is supported, build structured `updates`.
4. Run the validated executor:
   - Repo runtime: `maintenance_update_executor.execute_parameter_updates(...)`
   - Portable skill runtime: `scripts/execute_updates.py`
   - Strategy comparison: `maintenance_update_executor.execute_named_strategy_comparison(...)`
5. Explain the returned result in business language.
6. If unsupported, say so clearly and briefly explain why.

## Bundled Scripts

This skill ships with a portable runtime under `scripts/` so it can be distributed independently of a specific repo:

- `scripts/maintenance_window_runtime.py`
- `scripts/execute_updates.py`

Use the bundled scripts when the host environment does not already have the repo-level executor available.

## Fuzzy Language Handling

Follow the rules document, not ad hoc intuition.

Default interpretation:

- "一点" / "稍微": about 10%
- "多一点": about 20%
- "明显提高": about 30%

Interpret these relative to the current active configuration when available. If no prior active configuration is available, use the default scenario as the baseline.

## Clarification Rules

Do not ask unnecessary questions when the request can be mapped safely.

Execute directly for requests like:

- "经济性调高点"
- "时间窗改48小时"
- "只看1号和2号风机"
- "去掉变频器"
- "发电机维修时长改成10小时"

Ask a concise clarification only when:

- the request could map to more than one supported parameter
- the user mentions "严重性" but does not say whether it means global risk weight or a specific component severity
- the request combines conflicting directions

## Refusal Boundary

Refuse cleanly when the user is really asking for new model structure, such as:

- "齿轮箱和主轴不能同时修"
- "每天18点以后必须收工"
- "发电机必须先于齿轮箱"

Good refusal pattern:

- say this is a new model constraint, not a supported parameter adjustment
- say the current forecasting script cannot do it directly
- if possible, suggest a nearby supported parameter alternative

## Output Style

When execution succeeds, report:

- what you changed
- which supported parameter(s) it mapped to
- key result deltas
- repaired vs pending components when useful

Do not pretend you solved a request that the executor did not support.
Do not fabricate optimization results.

## Quick Examples

User: "经济性调高点"

Action:

- map to `updates = {"weights": {"loss": 1.1}}`
- run `execute_parameter_updates`

User: "对比经济优先和风险优先"

Action:

- map to `["economic_priority", "risk_priority"]`
- run `execute_named_strategy_comparison`

User: "让齿轮箱和主轴不能同时修"

Action:

- do not execute parameter update
- explain that this is a new constraint type outside current script support
