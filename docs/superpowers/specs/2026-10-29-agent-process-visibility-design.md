# Agent 过程可视化 + 后台运行从一开始

> 日期：2026-10-29　状态：已确认开工　关联：评测模块（阶段事件同源）

## 背景

网页端体验反馈三点，其中「慢」单独立项。本设计覆盖前两项：

1. 求解发起后要能「一开始就挂后台」，继续问参数 / 再安排任务。
2. 回答流式输出前，要把 agent 背后在干嘛（调用哪些解析层 / 工具 / 求解阶段）显示出来。

## 诊断结论（已实测）

- 求解器**已是异步 job**（秒回 `solve_started`），`/jobs/{id}/actions` 的 `background`
  动作已存在，前端输入框在求解期间**未被禁用**——机制齐全，缺的是「一开始就有入口」的 UX。
- 过程可见性：后端已有 `StreamSink.status/token`，但只有旧规划路径（toolbox 读版本/告警）
  发 `status`；DSL 解析新路径全程静默。
- 「慢」：LLM 解析一次 ~8.5s 且同步阻塞，另因「顺延/推迟」被误判 reject 而回退二次调用。
  **本设计不动求解器、不动 LLM 延迟**，仅埋点；慢的问题另立。

## 设计

### A. 过程可视化

- 新增 SSE 事件 `stage`，payload `{id, label, status: "running"|"done", detail?}`，
  与现有 `status`（短标签）/ `token`（正文）并存。
- `llm_parser.parse()` 增加可选 `on_stage` 回调，在各层切换处发事件：
  `route` → `parse.rule` / `parse.legacy` / `parse.retrieve` / `parse.llm`。
- `agent_runtime._try_dsl_parse` 透传 sink；DSL 分支在桥接+预览处发 `dsl.validate`；
  旧路径 `on_tool` 从发 `status` 升级为也发 `stage`（工具名 + 参数摘要）。
- 前端把「单行 thinking 横幅」升级为**可折叠过程面板**（步骤列表：spinner / ✓ / ✗ + 标签 + 明细）。

### B. 后台运行从一开始

- 后端不改（`background` 动作 RUNNING 态可用）。
- 前端 RUNNING 进度条旁加「后台运行」「停止」按钮，点击后台运行后横幅收成一条
  细的「后台求解中 · N%」，对话照常。180s 软阈值的 `SolveWaitDialog` 保留。

## 阶段事件词汇（最小集）

`route` / `parse.rule` / `parse.legacy` / `parse.retrieve` / `parse.llm` /
`dsl.validate` / `tool.{name}` / `solve`（job 阶段复用 progressPercent）。

## 复用

这套 stage 事件是评测模块「按阶段拆成本 / 延迟」的采集点，一次埋点两用。
