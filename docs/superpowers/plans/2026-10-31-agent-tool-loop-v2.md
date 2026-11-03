# Agent Tool Loop V2 Implementation Plan

> **归档说明（2026-11-01 补记）**
>
> 这是一份**计划稿，未按原样实施**。后来选了更轻的路径：不重构工具注册表、不新建草稿仓库、不改循环预算，只在现有循环上补三个工具（`prepare_change` 落 llm-first 侧，`read_jobs` / `submit_change` 落 backend）。
>
> 与本文的偏差，据代码核对：
>
> | 本文要求 | 实际 |
> |---|---|
> | `tool_registry.py` 声明式注册表 | 未做，工具仍硬编码在 `agent_tools.py` |
> | `read_catalog` → `read_capabilities` | 未改名，`read_catalog` 保留 |
> | `solve` 拆成 prepare + submit | 未拆，`solve` 保留为确定性预览，另加 `submit_change` |
> | 不向模型暴露 `ledger` | 未执行，`ledger` 仍在工具表面（共 9 个） |
> | `change_drafts.py` 持久化草稿仓库 | 未做，沿用现有 `pending_change` 会话状态 |
> | `llm_first_adapter.py` 子进程适配器 | 未做，`prepare_change` 直接调 `validate_dsl` |
> | 循环预算 8 轮 / 12 次工具调用 | 未改，仍是 `max_rounds=4` |
> | `AGENT_TOOL_LOOP_V2` 分阶段开关 | 未做 |
>
> 为什么留档：这份稿子的价值不在"完成度"，而在它记录了当时考虑过的完整方案——持久化草稿、幂等提交、调用预算、分阶段开关。这些没做不等于不该做，只是当时代价收益不划算。
>
> 实际落地与归因见 `llm-first/docs/2026-10-31-工具循环落地记录.md`。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有对话产品收敛为一个可组合的 Agent 工具循环，并用持久化变更草稿把理解、授权和真实求解严格绑定。

**Architecture:** 保留现有查询、编译、验收和 CP-SAT 求解内核；新增声明式工具注册表、变更草稿仓库和 llm-first 适配器。对话模型只负责选择及组合工具，执行层负责参数校验、授权、幂等、预算、事件记录和真实作业生命周期。

**Tech Stack:** Python 3.12、FastAPI、Pydantic、SQLite、Anthropic tool calling、pytest、React/Vite。

---

### Task 1: 声明式工具注册表与八工具表面

**Files:**
- Create: `backend/app/core/tool_registry.py`
- Modify: `backend/app/core/agent_tools.py`
- Test: `backend/tests/test_tool_registry.py`
- Test: `backend/tests/test_conversation_agent.py`

- [ ] 先写测试，要求注册表恰好暴露 `read_versions`、`read_environment`、`read_site`、`read_alarms`、`read_capabilities`、`prepare_change`、`submit_change`、`read_jobs`，并记录读写性质、超时、缓存与授权要求。
- [ ] 运行测试，确认因注册表和新工具不存在而失败。
- [ ] 实现 `ToolDefinition` / `ToolRegistry`，把现有工具 schema 迁入注册表；`read_catalog` 改名为 `read_capabilities`，`solve` 拆成准备与提交，首版不再向模型暴露 `ledger`。
- [ ] 运行相关测试，确认通过。
- [ ] 提交 `feat(agent): add declarative eight-tool registry`。

### Task 2: 持久化变更草稿与 llm-first 准备工具

**Files:**
- Create: `backend/app/core/change_drafts.py`
- Create: `backend/app/core/llm_first_adapter.py`
- Create: `llm-first/scripts/prepare_change_bridge.py`
- Modify: `backend/app/db/database.py`
- Modify: `backend/app/db/models.py`
- Modify: `backend/app/core/conversation_agent.py`
- Modify: `backend/app/core/agent_tools.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_change_drafts.py`
- Test: `backend/tests/test_agent_tools.py`

- [ ] 先写测试，证明模型上下文能看到有界的 `{id, role, content}` 消息引用；工具按 `sourceMessageIds` 从服务端消息仓库读取原文，不能只信模型转述；缺失、重复、非用户及跨会话 ID 均拒绝；成功时保存带内容哈希的 `change_id`，澄清时不生成可提交草稿。
- [ ] 运行测试，确认失败原因是草稿仓库与适配器尚不存在。
- [ ] 新增 `change_drafts` 表和仓库，状态为 `PREPARED/AUTHORIZED/SUBMITTED/CANCELLED`，保存原始消息、基准版本与指纹、验收报告、DSL/request、预览和提交作业 ID；WorkingMemory 只保存一个显式 `activeChangeId`。
- [ ] 新增子进程适配器调用 `llm-first/src/pipeline.py`，将其 `claims/fills/dsl/report` 转为统一准备结果；`not_change/reject/guardrail/verification failure/compile failure/unassigned` 均不可提交。既做 fake interpreter 单测，也做真实子进程序列化契约测试。
- [ ] 实现 `prepare_change`，从 `MessageRepository` 校验消息归属并读取原文，调用 llm-first，保存草稿并返回 `changeId` 或精确澄清问题。
- [ ] 运行相关测试并提交 `feat(agent): persist verified change drafts`。

### Task 3: 授权绑定、幂等提交和作业读取

**Files:**
- Modify: `backend/app/core/change_drafts.py`
- Modify: `backend/app/core/agent_tools.py`
- Modify: `backend/app/core/agent_runtime.py`
- Modify: `backend/app/core/solve_jobs.py`
- Modify: `backend/app/db/database.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_change_drafts.py`
- Test: `backend/tests/test_agent_tools.py`
- Test: `backend/tests/test_agent_api.py`

- [ ] 先写测试：未授权、过期、取消或非活动草稿不能提交；授权绑定草稿哈希、基准版本指纹、授权消息 ID、actor 和时间；重复及并发提交返回同一个 job；跨会话 change/job ID 不可访问；`read_jobs` 能按 ID 或当前会话查询状态及失败原因。
- [ ] 运行测试并确认红灯。
- [ ] 给 `solve_jobs` 增加唯一 `change_id`；用同一 SQLite 事务创建 job 并将草稿转为 SUBMITTED，提交后再派发；注入崩溃测试证明重试仍返回同一 job。`submit_change` 只接收 `changeId`，`read_jobs` 的 ID 查询始终带 conversation scope。
- [ ] 修改确认/取消状态机：确认时授权当前草稿并提交其已保存内容，取消时标记草稿取消；旧 pending payload 仅保留离线兼容迁移。
- [ ] 运行相关测试并提交 `feat(agent): bind approval to idempotent job submission`。

### Task 4: 统一循环、事件上下文与调用预算

**Files:**
- Modify: `backend/app/core/conversation_agent.py`
- Modify: `backend/app/core/agent_trace.py`
- Modify: `backend/app/core/tool_runtime.py`
- Modify: `backend/app/core/conversation_memory.py`
- Modify: `backend/app/core/agent_runtime.py`
- Test: `backend/tests/test_conversation_agent.py`
- Test: `backend/tests/test_agent_trace.py`
- Test: `backend/tests/test_agent_memory.py`
- Test: `backend/tests/test_agent_tools.py`

- [ ] 先写测试：工具调用和关键结果保存为事件；后续轮次上下文包含草稿/作业引用；循环默认最多 8 个模型轮次、12 次工具调用、同一 `tool+canonical arguments+error code` 连续失败 2 次即终止，分别返回 `round_budget_exhausted/tool_budget_exhausted/repeated_tool_failure`。
- [ ] 运行测试并确认红灯。
- [ ] 扩展 trace 记录工具结果摘要、错误类型、产物引用；构建上下文时加入最近事件和活动草稿/作业引用，大表仍由工具按需读取。
- [ ] 在 Agent 循环中统一执行八个工具，加入可配置调用预算和重复失败终止；移除关键词强制工具选择。`ToolRuntime` 必须直接消费注册表定义完成 schema 校验、超时、缓存、重试、读写及授权判断，不保留硬编码工具集合。
- [ ] 运行相关测试并提交 `refactor(agent): unify loop context events and budgets`。

### Task 5: 修正两处现有行为

**Files:**
- Modify: `backend/app/core/conversation_agent.py`
- Modify: `backend/app/core/agent_runtime.py`
- Test: `backend/tests/test_conversation_agent.py`
- Test: `backend/tests/test_agent_api.py`

- [ ] 先写测试：精确 Markdown/公式逐字节保留，支持多份精确产物及同轮/后续轮的其他工具，不提前结束、不重复流式输出；两层模型的认证、超时、传输、5xx、坏输出及桥接非零退出均返回明确技术故障，不落入规则链重新解释用户语义；业务澄清仍作为正常工具结果交给模型。
- [ ] 运行测试并确认红灯。
- [ ] 删除 `verbatim_markdown` 提前返回；循环按调用顺序收集精确产物，模型只收到占位符，最终由执行层确定性替换/追加，保证字节不变。
- [ ] 将两层模型及桥接错误统一为 typed technical failure；Agent 已启用时技术故障返回 `assistant_unavailable`，不进入确定性语义路由。确定性路由仅允许显式 `AGENT_OFFLINE_MODE=1`，且 response action 与 trace 标明 offline。
- [ ] 运行相关测试并提交 `fix(agent): preserve compound tasks and surface model outages`。

### Task 6: 集成、兼容与证据审计

**Files:**
- Modify: `backend/app/core/agent_runtime.py`
- Modify: `backend/app/core/agent_tools.py`
- Modify: `backend/app/main.py`
- Modify: `docs/2026-10-31-agent-tool-loop-v2-evidence.md`
- Test: `backend/tests/test_agent_api.py`
- Test: `backend/tests/test_conversation_agent.py`
- Test: `backend/tests/test_agent_tools.py`

- [ ] 增加复合请求、跨轮修正、确认前解释、工具失败、幂等提交和模型故障集成测试。
- [ ] 增加 `AGENT_TOOL_LOOP_V2` 分阶段开关：`shadow` 只影子准备、`prepare` 开放八工具读取/准备、`submit` 开放授权提交、`on` 关闭旧语义路由；迁移时旧 pending payload 可读并转草稿，任何阶段均可回滚到前一阶段且不丢草稿/作业。
- [ ] 运行完整后端测试：`$env:PYTHONUTF8='1'; python -m pytest backend/tests -q`，要求退出码 0。
- [ ] 运行 llm-first 测试：`$env:PYTHONUTF8='1'; python -m pytest llm-first/tests -q`，要求退出码 0。
- [ ] 运行最终行为评测：221 条主集与 70 条对抗集，记录成功率、误执行率、澄清次数和回退次数；误执行必须为 0，回退次数必须为 0。
- [ ] 运行重启恢复测试，覆盖 PREPARED/AUTHORIZED/SUBMITTED/CANCELLED 四种草稿状态及已创建未派发 job。
- [ ] 运行前端测试与构建：`npm test -- --run`、`npm run build`，要求退出码 0。
- [ ] 逐项审计“一至四”和两处行为，证据文档列出实现文件、测试名称、命令结果和提交 ID。
- [ ] 提交 `docs: add agent tool loop verification evidence`。

