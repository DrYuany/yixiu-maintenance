# 检修窗口期预测 Agent First Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将当前前端雏形升级为“左侧 agent 生成版本、右侧展示版本与候选窗口池”的第一阶段可用产品。

**Architecture:** 前端继续使用 Vite + React，后端新增 Python FastAPI 服务作为 agent 运行时与 skill 执行层。前端通过统一 Agent Store 管理会话、版本池、候选池和右侧页面状态，右侧页面从 Store 中读取结构化结果，不再自行生成核心业务数据。

**Tech Stack:** React 18, Vite, TypeScript, Tailwind, Python 3.10+, FastAPI, Pydantic, SQLite, existing `maintenance-window-forecast` skill runtime

---

## 文件结构规划

### 前端修改/新增

- Modify: `AI窗口期预测产品/src/App.tsx`
- Modify: `AI窗口期预测产品/src/components/AgentPanel.tsx`
- Modify: `AI窗口期预测产品/src/components/P01Dashboard.tsx`
- Modify: `AI窗口期预测产品/src/components/P04RuleManagement.tsx`
- Create: `AI窗口期预测产品/src/store/types.ts`
- Create: `AI窗口期预测产品/src/store/agent-store.ts`
- Create: `AI窗口期预测产品/src/store/selectors.ts`
- Create: `AI窗口期预测产品/src/lib/api-client.ts`
- Create: `AI窗口期预测产品/src/lib/result-normalizer.ts`
- Create: `AI窗口期预测产品/src/components/agent/ConversationList.tsx`
- Create: `AI窗口期预测产品/src/components/agent/MessageThread.tsx`
- Create: `AI窗口期预测产品/src/components/agent/ChatComposer.tsx`
- Create: `AI窗口期预测产品/src/components/agent/WorkingMemoryPanel.tsx`
- Create: `AI窗口期预测产品/src/components/dashboard/VersionSourcePanel.tsx`
- Create: `AI窗口期预测产品/src/components/dashboard/RecommendationBoard.tsx`
- Create: `AI窗口期预测产品/src/components/dashboard/CandidateWindowTable.tsx`
- Create: `AI窗口期预测产品/src/components/dashboard/EmptyWorkspaceState.tsx`

### 后端新增

- Create: `AI窗口期预测产品/backend/requirements.txt`
- Create: `AI窗口期预测产品/backend/app/main.py`
- Create: `AI窗口期预测产品/backend/app/api/agent.py`
- Create: `AI窗口期预测产品/backend/app/api/conversations.py`
- Create: `AI窗口期预测产品/backend/app/api/dashboard.py`
- Create: `AI窗口期预测产品/backend/app/api/versions.py`
- Create: `AI窗口期预测产品/backend/app/core/agent_runtime.py`
- Create: `AI窗口期预测产品/backend/app/core/intent_parser.py`
- Create: `AI窗口期预测产品/backend/app/core/memory_manager.py`
- Create: `AI窗口期预测产品/backend/app/core/version_manager.py`
- Create: `AI窗口期预测产品/backend/app/core/result_normalizer.py`
- Create: `AI窗口期预测产品/backend/app/core/skill_executor.py`
- Create: `AI窗口期预测产品/backend/app/db/database.py`
- Create: `AI窗口期预测产品/backend/app/db/models.py`
- Create: `AI窗口期预测产品/backend/app/schemas/agent.py`
- Create: `AI窗口期预测产品/backend/app/schemas/conversation.py`
- Create: `AI窗口期预测产品/backend/app/schemas/dashboard.py`
- Create: `AI窗口期预测产品/backend/app/schemas/version.py`
- Create: `AI窗口期预测产品/backend/tests/test_agent_api.py`
- Create: `AI窗口期预测产品/backend/tests/test_version_manager.py`
- Create: `AI窗口期预测产品/backend/tests/test_skill_executor.py`

### 文档

- Create: `AI窗口期预测产品/docs/backend-api.md`
- Modify: `AI窗口期预测产品/README.md`

## 任务拆分

### Task 1: 定义前后端共享的数据结构

**Files:**

- Create: `AI窗口期预测产品/src/store/types.ts`
- Create: `AI窗口期预测产品/backend/app/schemas/agent.py`
- Create: `AI窗口期预测产品/backend/app/schemas/conversation.py`
- Create: `AI窗口期预测产品/backend/app/schemas/dashboard.py`
- Create: `AI窗口期预测产品/backend/app/schemas/version.py`

- [ ] Step 1: 明确定义核心对象
- [ ] Step 2: 统一前后端对象名称
- [ ] Step 3: 定义 `Conversation`
- [ ] Step 4: 定义 `Message`
- [ ] Step 5: 定义 `WorkingMemory`
- [ ] Step 6: 定义 `WindowVersion`
- [ ] Step 7: 定义 `CandidateWindow`
- [ ] Step 8: 定义 `RecommendationSet`
- [ ] Step 9: 定义 `AgentResponse`
- [ ] Step 10: 手动校对字段是否能覆盖 P01/P04 第一阶段需求

### Task 2: 建立后端基础骨架

**Files:**

- Create: `AI窗口期预测产品/backend/requirements.txt`
- Create: `AI窗口期预测产品/backend/app/main.py`
- Create: `AI窗口期预测产品/backend/app/db/database.py`
- Create: `AI窗口期预测产品/backend/app/db/models.py`

- [ ] Step 1: 新建 `backend` 目录结构
- [ ] Step 2: 写 `requirements.txt`
- [ ] Step 3: 初始化 FastAPI 应用
- [ ] Step 4: 接入 SQLite
- [ ] Step 5: 建立会话、消息、版本、候选窗口基础表
- [ ] Step 6: 本地启动后端并确认服务可访问

### Task 3: 封装 skill 执行层

**Files:**

- Create: `AI窗口期预测产品/backend/app/core/skill_executor.py`
- Create: `AI窗口期预测产品/backend/tests/test_skill_executor.py`

- [ ] Step 1: 编写 skill 执行层测试用例
- [ ] Step 2: 测试 `updates` 模式调用
- [ ] Step 3: 测试 `compare` 模式调用
- [ ] Step 4: 实现 skill 适配层，优先直接 import 现有运行时函数 `execute_parameter_updates(...)` 与 `execute_named_strategy_comparison(...)`
- [ ] Step 5: 为异常和非法输入补测试
- [ ] Step 6: 验证执行结果结构稳定

### Task 4: 实现后端结果归一化与版本管理

**Files:**

- Create: `AI窗口期预测产品/backend/app/core/result_normalizer.py`
- Create: `AI窗口期预测产品/backend/app/core/version_manager.py`
- Create: `AI窗口期预测产品/backend/tests/test_version_manager.py`

- [ ] Step 1: 写测试，给 skill 原始结果输入样本
- [ ] Step 2: 设计版本生成规则
- [ ] Step 3: 实现原始结果转 `WindowVersion`
- [ ] Step 4: 实现版本展开为候选窗口
- [ ] Step 5: 实现 Top3 派生逻辑
- [ ] Step 6: 实现候选池排序逻辑
- [ ] Step 7: 验证“版本不覆盖旧版本”

### Task 5: 实现后端记忆与 agent 运行时

**Files:**

- Create: `AI窗口期预测产品/backend/app/core/intent_parser.py`
- Create: `AI窗口期预测产品/backend/app/core/memory_manager.py`
- Create: `AI窗口期预测产品/backend/app/core/agent_runtime.py`

- [ ] Step 1: 定义第一阶段支持的意图枚举
- [ ] Step 2: 实现简单意图路由
- [ ] Step 3: 实现工作记忆读取与更新
- [ ] Step 4: 实现会话摘要生成接口
- [ ] Step 5: 在 agent runtime 中编排 “消息 -> skill -> 版本 -> 候选池”
- [ ] Step 6: 处理非 skill 类输入，如删除版本、加入候选池、解释结果

Task 5 说明：

- 这里的“意图枚举”和“意图路由”是产品层动作路由，不是重做 skill 内的领域意图分类
- 第一阶段产品层枚举应控制在：
  - `generate_version`
  - `manage_version`
  - `explain_result`
  - `navigate_view`
  - `conversation_control`
- 当路由进入 `generate_version` 后，领域参数解释优先复用 skill 现有规则边界，而不是在产品后端再实现一套新的自然语言到 `updates` 映射器

### Task 6: 暴露后端 API

**Files:**

- Create: `AI窗口期预测产品/backend/app/api/agent.py`
- Create: `AI窗口期预测产品/backend/app/api/conversations.py`
- Create: `AI窗口期预测产品/backend/app/api/dashboard.py`
- Create: `AI窗口期预测产品/backend/app/api/versions.py`
- Create: `AI窗口期预测产品/backend/tests/test_agent_api.py`

- [ ] Step 1: 写 API 行为测试
- [ ] Step 2: 实现新建/查询会话接口
- [ ] Step 3: 实现发送消息接口
- [ ] Step 4: 实现版本查询/加入/移出/删除接口
- [ ] Step 5: 实现 dashboard 聚合接口
- [ ] Step 6: 验证接口返回能直接驱动前端

### Task 7: 建立前端 Agent Store

**Files:**

- Create: `AI窗口期预测产品/src/store/agent-store.ts`
- Create: `AI窗口期预测产品/src/store/selectors.ts`
- Modify: `AI窗口期预测产品/src/App.tsx`

- [ ] Step 1: 选择状态管理实现
- [ ] Step 2: 建立全局 store
- [ ] Step 3: 存入会话列表、当前会话、消息、版本池、候选池、Top3、当前视图
- [ ] Step 4: 改造 `App.tsx`，让页面从 Store 取状态
- [ ] Step 5: 移除 `App.tsx` 中分散的核心业务状态

### Task 8: 重构左侧 AgentPanel 为真正会话中心

**Files:**

- Modify: `AI窗口期预测产品/src/components/AgentPanel.tsx`
- Create: `AI窗口期预测产品/src/components/agent/ConversationList.tsx`
- Create: `AI窗口期预测产品/src/components/agent/MessageThread.tsx`
- Create: `AI窗口期预测产品/src/components/agent/ChatComposer.tsx`
- Create: `AI窗口期预测产品/src/components/agent/WorkingMemoryPanel.tsx`
- Create: `AI窗口期预测产品/src/lib/api-client.ts`

- [ ] Step 1: 拆分现有 `AgentPanel`
- [ ] Step 2: 接入会话列表
- [ ] Step 3: 接入消息线程展示
- [ ] Step 4: 接入发送消息到后端
- [ ] Step 5: 接入 loading/error 状态
- [ ] Step 6: 展示当前工作记忆摘要
- [ ] Step 7: 去掉现在的前端硬编码分支回复

### Task 9: 重构 P01 为版本驱动窗口工作台

**Files:**

- Modify: `AI窗口期预测产品/src/components/P01Dashboard.tsx`
- Create: `AI窗口期预测产品/src/components/dashboard/VersionSourcePanel.tsx`
- Create: `AI窗口期预测产品/src/components/dashboard/RecommendationBoard.tsx`
- Create: `AI窗口期预测产品/src/components/dashboard/CandidateWindowTable.tsx`
- Create: `AI窗口期预测产品/src/components/dashboard/EmptyWorkspaceState.tsx`

- [ ] Step 1: 去除 P01 内部模拟窗口生成逻辑
- [ ] Step 2: 加入无会话/无版本空状态
- [ ] Step 3: 接入候选池列表
- [ ] Step 4: 接入 Top3 推荐区
- [ ] Step 5: 接入版本来源区
- [ ] Step 6: 接入排序模式切换
- [ ] Step 7: 接入删除窗口/版本操作
- [ ] Step 8: 验证“全部窗口来自对话生成”

### Task 10: 改造 P04 作为规则与策略展示页

**Files:**

- Modify: `AI窗口期预测产品/src/components/P04RuleManagement.tsx`

- [ ] Step 1: 区分“可编辑规则”与“当前会话参数快照”
- [ ] Step 2: 接入从版本中读取 focus tag 和参数快照
- [ ] Step 3: 让页面操作最终走统一 action 管道
- [ ] Step 4: 去除仅前端演示性质的假 AI 提示

### Task 11: 补齐 README 与接口说明

**Files:**

- Create: `AI窗口期预测产品/docs/backend-api.md`
- Modify: `AI窗口期预测产品/README.md`

- [ ] Step 1: 补充前后端启动说明
- [ ] Step 2: 说明 skill 依赖和后端位置
- [ ] Step 3: 说明第一阶段已支持的能力
- [ ] Step 4: 记录暂未支持的主动后台计算

### Task 12: 联调与验收

**Files:**

- Modify: `AI窗口期预测产品/src/App.tsx`
- Modify: `AI窗口期预测产品/src/components/AgentPanel.tsx`
- Modify: `AI窗口期预测产品/src/components/P01Dashboard.tsx`
- Modify: `AI窗口期预测产品/src/components/P04RuleManagement.tsx`

- [ ] Step 1: 跑通前端开发服务器
- [ ] Step 2: 跑通后端开发服务器
- [ ] Step 3: 手工测试新建会话
- [ ] Step 4: 手工测试一次版本生成
- [ ] Step 5: 手工测试多轮对话累计多个版本
- [ ] Step 6: 手工测试加入/移出候选池
- [ ] Step 7: 手工测试删除候选窗口
- [ ] Step 8: 手工测试 Top3 随排序规则变化
- [ ] Step 9: 修复联调问题

## 测试策略

### 后端

- `pytest backend/tests/test_skill_executor.py -v`
- `pytest backend/tests/test_version_manager.py -v`
- `pytest backend/tests/test_agent_api.py -v`

### 前端

第一阶段以前端手工回归为主。

重点场景：

- 无对话空状态
- 单次生成版本
- 多轮生成多个版本
- 删除版本
- 删除候选窗口
- 切换排序模式
- 左侧切会话后右侧同步切换

## 第一阶段验收口径

满足以下条件才进入下一阶段编码扩展：

- 左侧不再依赖硬编码关键词回复
- 每次有效计算都能生成新版本
- P01 只展示对话生成出来的窗口
- 候选池和 Top3 逻辑成立
- 多轮对话不会覆盖旧版本
- 前后端数据结构稳定

## 备注

- 第二阶段再接入 P02/P03/P05 的真实结构化结果
- 后台定时主动计算作为 V2，当前只预留 `sourceType=system`
- 本计划默认先完成最小闭环，不追求一次性把全部页面做真
