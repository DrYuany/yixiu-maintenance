# 检修排程对话 Agent：五个读工具 + 一条受控求解链

日期：2026-10-27  
范围：对话 Agent 的事实读取、多方案对比、模型查询、变更预览与流式输出

## 1. 目标

把对话入口收敛成一个最小但完整的 Agent：

1. 普通聊天、概念解释、算法询问保持正常对话能力。
2. 涉及方案、成本、任务、窗口、模型、参数、状态的问题，先读事实再回答。
3. 多方案对比由确定性工具摆成一张指标表，解读交给大模型。
4. 模型变更只先生成预览，用户确认后才真正求解。
5. 没有事实时明确说缺失，不编造、不事后重建冒充原模型。

## 2. 设计原则

- **像人一样工作**：计划员对比多个方案，会先把各版指标拉进一张表，再看业务始终关心的指标和差异最大的一两项。`read_versions` 直接返回这张表，而不是让模型分别读多次再自己拼。
- **工具越少越好**：只有读、写两类。工具的职责是把确定性事实取回来，判断和取舍留给模型，避免用工具把结论写死。
- **一版和 N 版同一个入口**：读一版是单方案事实，读多版自动变成对比矩阵；不需要记“什么时候该用哪个工具”。
- **写侧必须留痕**：变更先编译成预览，确认后才求解；求解状态严格区分 OPTIMAL / FEASIBLE / UNKNOWN / INFEASIBLE。

## 3. 工具

落地为五个只读工具 + 一条受控求解链：

> **注（2026-11-02）**：本节记的是 10-27 当时的工具面。现在 `AgentToolbox.tool_specs()`
> 实际下发 **9 个**：下表五个只读加 `read_jobs`（查求解进度），再加 `solve` /
> `submit_change` / `ledger`。工具面以代码为准，本文档只保留当时的契约取舍。

| 工具 | 数据域 | 依赖方案 | 说明 |
|---|---|---|---|
| `read_versions` | 方案事实 | 是 | 1 个给单方案，N 个自动变对比矩阵；带 `baseline` 时给相对差值 |
| `read_environment` | 风速、电价、海况、可用小时 | 否 | 与求解同源，返回每日聚合、Markdown 表、`verbatim=true`；`detail="hourly"` 追加逐时序列 |
| `read_catalog` | 策略、可加权指标、默认权重 | 否 | 与系统提示里的目录同一来源 |
| `read_alarms` | 告警、待修任务、每日接收统计 | 否 | 只读规划时点之前的记录；支持 backlog / recent / summary、严重度、风机、部件、任务和历史状态筛选 |
| `read_site` | 风机台账、部件、窗口、库存、检修单、执行反馈、系统参数 | 否 | 按 `scope` 读取；空表如实返回“当前没有”，不把空表说成没有数据域 |
| `solve` | 变更预览 | 是 | 只出预览，确认后由系统求解；模型不直接写库、不审批、不导出 |

按数据域拆名的原因：早先把它们塞进一个 `read` 的 `scope`，参数互斥、合法性只能靠约定。
现在方案、环境、目标目录、告警、现场资源各自成域；参数只表达该域自己的筛选语义，
不把“读方案”和“读库存”的互斥参数放进同一个 schema。
`2026-10-27-agent-scenario-requirement-matrix.md` 里的 28 个名字是能力清点表，不是工具数量。
告警单独成一个工具，因为它是高频增量查询、筛选维度多；风机、部件、窗口、库存、
检修单、执行反馈和系统参数共用一个 `read_site`，用 `scope` 区分，避免为每个数据域各加一个工具。

统一返回：

```json
{
  "status": "ok | not_found | not_available | ambiguous | unknown | needs_confirmation | clarify",
  "data": {},
  "provenance": {"conversationId": "", "versionId": "", "modelFingerprint": "", "dataAsOf": ""},
  "warnings": [],
  "nextActions": []
}
```

### 3.1 `read_versions`

只读。输入：

| 字段 | 说明 |
|---|---|
| `versions` | 方案引用数组。省略或空数组＝列出本会话全部方案；1 个＝单方案事实；2 个及以上＝对比视图 |
| `baseline` | 多方案对比的基准方案；省略取列表第一个。差值一律由工具算，模型不做减法 |
| `include` | 追加内容：`tasks`、`unscheduled`、`model`、`costs`、`parameters`、`constraints`、`status` |
| `section` | `include=model` 时只取 `all / objective / constraints / variables / costs` |
| `focus` | 用户关心的侧重点，只影响解读，不影响事实 |

输出按 `data.mode` 分三种：

**index（不传 versions）**

- `versions[]`：序号、版本 ID、策略、状态、总成本、现金支出、停机损失、完成量、船天、窗口、复核。

**single（1 个方案）**

- `planNumber`、`label`、窗口、成本三项、完成量、船天、最晚完工、状态、复核、模型指纹、数据时点。
- 按 `include` 追加 `tasks`、`unscheduled`、`parameters`、`constraints`、`solve`。
- `include=model` 时给 `markdown`（Markdown + LaTeX）与 `latex`；缺模型产物时给 `modelUnavailable`，不重建。

**compare（2 个及以上）**

- `matrix`：统一指标表（每个指标一行，每个方案一列）。
- `deltasVsBaseline`：相对基准的绝对差与百分比，模型不得自行相减。
- `taskPresence`：共有任务、只在某版出现的任务、时间发生变化的任务。
- `comparability`：数据快照、任务全集、成本口径是否一致，以及是否允许宣称“节省”。

- `plans[]`：每个方案的同一组事实。
- `matrix.columns[]`：方案序号、标签、版本 ID。
- `matrix.rows[]`：建议窗口、总成本、现金支出、停机损失、完成任务、待修总数、船舶艘天、最晚完工、求解状态、独立复核、优化策略、目标模式；每行带 `values`、`changed`、`bestIndex`、`spreadPct`、`better`、`always`。
- `differences.changed[]`：按“业务始终关注优先、差异幅度其次”排序的前 8 项。
- `differences.identical[]`：完全一致的指标键。
- `comparability`：数据快照、任务全集、模型指纹、完成任务集合是否一致，以及 `costComparable`、`savingsClaimAllowed` 和限制说明。

`include=model` 在多方案时被拒绝，避免把 N 份完整模型塞进上下文；需要公式时单独读一版。

### 3.2 `solve`

写侧入口，但**只生成预览**。输入：

| 字段 | 说明 |
|---|---|
| `instruction` | 用户原话，保留关键数字和说法 |
| `baseVersion` | 基于哪个方案，省略时用当前选中方案 |

行为：

1. 用确定性编译器把变更编译成 `PlanningRequest`，与基准方案做差，得到 `operations`。
2. 返回 `status=needs_confirmation`、`preview`、`operations`、`requiresUserConfirmation=true`。
3. 预览写入会话待确认状态；用户回复确认后才启动 CP-SAT 求解。
4. 识别不出变更时返回 `status=clarify`，不猜参数。

## 4. 对比的三层如何保留

原来的三层对比仍完整保留，只是收进 `read_versions` 的多版视图：

| 层 | 现在的位置 |
|---|---|
| 事实层 | `plans[]` + `matrix` + `deltasVsBaseline` + `comparability` |
| 归因层 | `differences.changed[]`、`taskPresence`、`comparability.notes`，以及 `include=tasks/unscheduled/parameters/constraints/status` 的明细 |
| 下钻层 | 用 `read_versions` 带 `include` 再读单版，或 `include=["costs"]` / `["model"]` 取该版模型产物 |

硬规则不变：

- 完成任务集合不一致时 `savingsClaimAllowed=false`，不得说“节省”。
- 数据快照不一致时 `costComparable=false`，成本差异不能全部归因于参数。
- 模型指纹相同而结论不同，优先怀疑求解时间或随机性。

## 5. 行为规则

1. 数据问题先调对应读工具（方案 `read_versions`、环境 `read_environment`、能力 `read_catalog`、
   告警 `read_alarms`、现场资源 `read_site`）；模型不凭历史消息编造数字。
   纯查询问句由 `operations_query.py` 直接路由到读工具，不依赖模型是否记得数据域存在。
2. 用户点到的方案全部放进一次 `read_versions`，不要逐个读，也不要只读前两个。
3. 对比先给判断，再给 2 到 4 条关键差异；指标与任务集合完全一致时直接说等价。
4. 数学公式原样输出 `read_versions(include=["model"])` 的 `markdown`，不改写系数或约束；成本构成走 `include=["costs"]`。
5. 工具返回 `not_available / not_found` 时直接说明缺什么，不说“我去导出”。
6. `solve` 只产出预览；确认前不得声称已求解、已生成方案或已改数据。
7. 不展示内部字段名、`comparisonId` 或变量名。

## 6. 流式协议

`POST /api/agent/message/stream` 返回 SSE：

- `status`：工具读取进度。
- `token`：回答增量。
- `replace`：兜底替换全文。
- `done`：完整 `AgentResponse`。
- `error`：失败信息。

前端在临时助手气泡中增量渲染；收到 `done` 后用会话详情校准，避免本地状态与数据库分叉。

## 7. 模块

| 模块 | 作用 |
|---|---|
| `backend/app/core/agent_tools.py` | `read_versions` / `read_environment` / `read_catalog` / `read_alarms` / `read_site` / `solve`、方案解析、指标矩阵、基准差值、任务存在性 |
| `backend/app/core/conversation_agent.py` | 工具循环、系统提示、流式回答 |
| `backend/app/core/site_data.py` | 告警、风机、部件、窗口、库存、检修单、执行反馈、系统参数的只读渲染 |
| `backend/app/core/operations_query.py` | 纯数据查询的确定性路由与筛选参数提取 |
| `backend/app/core/objective_catalog.py` | 目标模板的唯一读取入口：策略、可加权指标、默认权重、能力目录文案 |
| `backend/app/core/environment_data.py` | 风速/电价/海况的读取与渲染，和求解共用 `load_project` 快照 |
| `backend/app/core/version_compare.py` | 两两对比的确定性计算与无 LLM 兜底文案 |
| `backend/app/core/agent_runtime.py` | 会话上下文、待确认变更、求解调度、SSE sink |
| `backend/app/api/agent.py` | `POST /api/agent/message/stream` |
| `src/components/agent/MessageBody.tsx` | Markdown、GFM 表格与 KaTeX 公式渲染 |
| `src/lib/api-client.ts` | SSE 解析与流式回调 |

## 7.1 能力问句与目标目录

问“有哪些目标函数可以加权”“支持哪些策略”“权重怎么设置”时，不需要先有方案。
这类问题走确定性回答，来源是 `maintenance-milp-modeler/references/四种目标模板.json`，
由 `objective_catalog.py` 读取后同一份内容喂给两处：

1. 对话 Agent 的系统提示（模型自己也能按目录回答更口语的变体）；
2. `model_query.py` 的 `capability` / `structure` 分支（高置信问法直接给结果，不花模型调用）。

目录不放进 `read_versions`：那会让模型先调一次工具才知道能设什么，且会与 `read_versions` 已有的
`versions/include/section/focus` 参数互相干扰。

可加权指标以模板为准：全部策略共有的只有 `总经济成本`、`最后完工时间`；`现金支出`、
各级 `完工时间和` 不在任何策略的加权项里，因此不能出现在能力回答中。

加权编译的三条规则：

- 用户只报关心的几项 → 该策略其余指标补 0（求解器要求权重覆盖全集）。
- 用户报了当前策略没有的指标 → 阻塞并说明它属于哪些策略，不静默丢弃。
- 更换策略后仍未给权重 → 改用新策略的默认权重，不出“权重集不匹配”的报错。

## 7.2 只保留一条执行链

旧的 `MiniMaxAgent` → `SkillRegistry`/`SkillExecutor` → `maintenance_window_runtime`
这条链路使用的策略名与权重语义（`loss/duration/severity` 三个乘数、英文策略名）与现行
中文四策略模板不一致，已删除。现在只有一条：

`ConversationAgent`（五个读工具 + solve）→ `MaintenanceProductExecutor` → `product_planner`
→ CP-SAT。`AgentRuntime.handle_message` 也直接复用同一入口，不再有第二套路由。

## 8. 已验证

- 后端 143 个测试通过；工具层覆盖列表、单版、三版矩阵、基准差值、任务存在性、成本构成视图、部分引用缺失、模型/参数/状态追加、多版拒绝模型、solve 预览与澄清；另有目标目录自检、能力问句直答、加权自动补 0、跨策略指标阻塞、策略切换后权重重置、环境域日聚合与逐时、指定窗口风况、告警增量/历史/筛选、库存与 115 项参数总表检索。
- 前端 20 个测试通过，生产构建包含 KaTeX 字体与读工具状态文案。
- 真实 DeepSeek 验收：多版对比返回矩阵且判定等价；公式问题走 `read_versions(include=["model"])` 输出 LaTeX；成本项问题给出精简表而不是整段模型；变更指令仍停在预览。

## 9. 验收清单

1. `read_versions` 不传参 → 方案索引。
2. “对比方案01、02、03，基准是01” → 一次 `read_versions`，返回三列矩阵、`deltasVsBaseline`、`taskPresence`。
3. “给我方案02的目标函数” → `read_versions(include=["model"], section="objective")`，公式可渲染。
4. “船租上涨 20%” → `solve` 只给预览，回复“确认”后才出现求解任务。
5. 无模型产物的旧方案 → 明确说明缺失，不重建成“原模型”。
6. “目标函数考虑了哪些成本” → `read_versions(include=["costs"])`，给五项金额和口径，不贴整段公式。
7. “接下来一周风速和电价” → `read_environment(days=7)`，返回带数据性质与输入哈希的表。

## 10. 已知边界

- `solve` 的自然语言覆盖仍以已注册说法为准，复杂组合需要补编译器和用例。
- 对比只自动分析成本、任务、目标、约束和状态；船期、海况、单任务归因需要扩展 `include`。
- 没有完整模型产物时只能说明缺失。
