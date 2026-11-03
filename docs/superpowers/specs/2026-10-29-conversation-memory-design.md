# 会话记忆机制设计：结构化快照 + 滚动摘要 + 增量缓存

日期：2026-10-29
状态：设计已评审（第 2 版，已修复评审问题），待实现

## 背景与问题

当前 agent 的会话记忆（[agent_memory.py](../../../backend/app/core/agent_memory.py)、
[memory_manager.py](../../../backend/app/core/memory_manager.py)）已有三层：
会话摘要（正则抽取约束/否决/待办）、用户画像、决策记忆。但存在两个问题：

1. **长多轮丢背景**：每轮把完整 `messages` 历史塞给 LLM
   （[agent_runtime.py:622](../../../backend/app/core/agent_runtime.py#L622) 的
   `_submit_message`、以及 [agent_runtime.py:1481](../../../backend/app/core/agent_runtime.py#L1481)
   的 `_answer_agent_question`，二者都是 `list_by_conversation` 全量读后交给
   `conversation_agent.answer`），长对话超过上下文窗口后，早期背景（约束、方案、动作）
   被截断丢弃。
2. **摘要太窄**：`build_summary` 只靠 4 种正则抽取约束，覆盖不了问答、产物、动作。

数据库（[database.py](../../../backend/app/db/database.py)）里其实已经落库了
`messages`（对话）、`versions`（产物）、`agent_traces` / `solve_jobs`（动作）、
`working_memory`（状态）。问题不是「没存」，而是「没用对」——上下文组装绕开了这些
结构化数据，直接塞全量原文。

## 目标与非目标

**目标**：单个会话内，多轮对话不再遗忘背景（约束/产物/动作/问答），上下文有界、
可回查、增量缓存。

**非目标**（本轮明确不做）：

- 跨会话检索（不引入向量库 / embedding）。
- 用户画像（不提炼长期偏好）。
- 全量 LLM 摘要（保持确定性为主，LLM 可选增强）。

## 方案：结构化快照 + 滚动摘要 + 增量缓存

从三个备选方案中选定方案 A（备选见文末附录）。

### 1. 数据模型 —— 会话记忆快照

**新建 `conversation_memory` 表**（`conversation_id` 主键，payload 为 JSON）。
旧的 `conversation_summary` 表与 `ConversationSummaryRepository` 废弃：读路径移除，
不再写入。快照缺失时从现有表（messages / versions / traces / solve_jobs）冷启动重建，
因此**不需要迁移旧摘要数据**。

```
{
  "messageCount": 42,                 // 增量判断与缓存失效锚点
  "state": {                          // 来自 working_memory（沿用真实字段名）
    "currentGoal": "...", "activeView": "P01",
    "activeFilters": {...}, "pendingChange": {...}
  },
  "constraints": [                    // 已确认约束（确定性抽取）
    "船租上涨 20%", "最多 2 条作业线"
  ],
  "artifacts": [                      // 产物引用（来自 versions）
    { "versionId": "...", "strategy": "成本最低优先", "cost": 2821043, "scheduled": 11 }
  ],
  "actions": [                        // 动作时间线（来自 agent_traces / solve_jobs）
    { "type": "solve", "versionId": "...", "status": "FEASIBLE", "at": "..." },
    { "type": "preview_change", "change": "船租上涨 20%", "at": "..." },
    { "type": "approve", "versionId": "...", "at": "..." }
  ],
  "qa": [                             // 关键问答（来源与判定见 §3）
    { "q": "为什么任务018没排上", "a": "...", "versionId": "...", "at": "..." }
  ],
  "summary": "……",                     // 滚动摘要（旧消息压缩，≤800 字）
  "recentMessageIds": ["msg-39", "msg-40", "msg-41", "msg-42"]  // 最近 N 条原文指针
}
```

关键性质：

- **增量更新**：每轮只 append 新事件、更新变化字段，不重扫全量历史。
- **快照即缓存**：上下文组装直接读快照，不再每轮 `list_by_conversation` 全量读 + 重算摘要。
- 各字段来源明确（state ← working_memory、artifacts ← versions、
  actions ← agent_traces/solve_jobs、constraints/qa ← 抽取），回查可溯源。

### 2. 上下文组装（根治「遗忘」的核心）

每轮给 LLM 的上下文从**快照**组装，不再塞全量历史：

```
[会话状态] currentGoal / activeView / activeFilters / pendingChange
[已确认约束] 来自快照，而不是靠 LLM 回忆
[最近方案] versionId / strategy / cost / scheduled
[动作时间线] 最近 K 条
[问答] 最近 M 条
[滚动摘要] …
[最近对话] 最近 N 条原文（按 recentMessageIds 取）
```

有界参数（默认值，可调）：**N = 8**（最近对话原文）、**K = 10**（动作）、
**M = 5**（问答）、**摘要 ≤ 800 字**。上下文**有界**，不随对话变长而膨胀；早期背景
（约束/产物/动作）永远可回查。

### 3. 摘要机制（确定性 + 可选 LLM）

- **确定性为主**：扩展现有正则（约束/否决/待办），并直接从 traces / jobs / versions
  的结构化字段取动作与产物，不靠正则猜语义。**否决/待办不单独设结构化字段，折叠进
  `summary` 文本**（§1 快照里不含 `rejectedPlans`/`openTodos` 字段）。
- **问答（qa）的来源与判定**：从 `messages` 表扫描；凡用户消息命中问句判定
  （复用 `_QUESTION_HINTS` 正则 + `detect_model_question`，与路由层一致），就把它与
  紧随其后的助手回答配对成 `{q, a}`，`a` 取助手消息原文（超长截断到 200 字）。
  `qa.versionId` 取自该消息的 `messages.version_ids`，无则省略。
  只归档「超出最近 N 条窗口」的早期问答；窗口内由「最近对话」原文覆盖，避免重复。
- **可选 LLM**：配置了 key 时用 LLM 压缩旧消息写自由文本摘要；失败或无 key 回退确定性，
  绝不阻塞主流程。
- **更新时机**：快照（含确定性摘要）每轮**增量更新**（成本低）；「压缩」是上下文组装
  层面的策略——「最近对话」只取最近 N 条，更早的靠 summary + constraints + qa + actions
  覆盖。不设单独的「超阈值才压缩」开关。
- 摘要永远标注「压缩记录，非完整对话」，具体数字以方案与工具为准。

### 4. 缓存策略

- 快照 = 持久缓存（落库、增量更新）。
- 进程内再加一层 LRU（按 `conversation_id`），`messageCount` 变化时失效对应条目；
  避免每轮读 DB。

### 5. 错误处理与测试

- LLM 摘要失败 → 确定性兜底。
- 快照缺失/损坏 → 从现有表（messages / versions / traces / solve_jobs）重建。
- 测试：
  - 20+ 轮长对话仍能正确回答早期约束（断言早期约束出现在组装后的上下文中）；
  - 增量更新后的快照与全量重算一致；
  - LLM 缺失时摘要仍可用；
  - 快照重建（冷启动）与增量维护结果一致。

## 实施边界

- 只改后端记忆层与上下文组装；前端无需改动（除非要展示记忆，本轮不做）。
- 复用现有 `WorkingMemory` 仓储与 `Database` 迁移模式，不重写数据库层。
- 废弃 `ConversationSummaryRepository` 会让现有测试
  `tests/test_agent_memory.py::test_conversation_summary_repository_roundtrip` 失去对象，
  实现计划需包含删除/更新该测试。
- 与「agent 过程可视化」分支（`feat/registry-driven-schema-prompt`）都改
  `agent_runtime.py`，合并时需注意冲突。

## 附录：备选方案（未选）

- **方案 B（纯结构化）**：不加摘要，只靠结构化字段承载记忆。最简，但自由文本里的
  背景（"你上次说的那个方案"）仍会丢。
- **方案 C（LLM 摘要压缩）**：Echomind 式，消息超阈值就 LLM 压缩。依赖 LLM 质量、
  确定性弱，与证据链/可复现原则冲突。
