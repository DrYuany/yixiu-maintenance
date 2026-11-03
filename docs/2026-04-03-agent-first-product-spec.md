# 检修窗口期预测 Agent First 产品方案

**日期：** 2026-04-03

**目标：** 将当前“左侧对话 + 右侧业务页”的前端雏形落地为一个真正可用的检修窗口期预测产品。左侧为核心 agent，右侧为版本化窗口结果工作台与专题展示层。

## 1. 产品核心判断

当前产品不能再按“右侧天然有窗口大盘，左侧只是解释入口”来定义。

正确关系应为：

- 左侧 agent 负责理解用户意图、维护会话记忆、调用预测 skill、生成窗口版本
- 右侧页面负责展示会话中已经生成过的窗口版本、候选窗口池和专题分析结果

一句话定义：

`左侧持续生成和管理窗口版本，右侧将这些版本资产化为候选窗口池与推荐结果。`

## 2. 关键对象定义

### 2.1 会话 Conversation

一次连续的用户工作上下文。会话是一级对象，不再只是消息数组。

会话最少包含：

- `id`
- `title`
- `createdAt`
- `updatedAt`
- `status`
- `lastMessagePreview`
- `activeView`
- `summary`

### 2.2 窗口版本 Window Version

每次对话触发一次有效计算，就生成一个窗口版本。窗口版本是右侧大盘的来源，不允许被最新结果简单覆盖。

窗口版本最少包含：

- `id`
- `conversationId`
- `sourceType`
- `generatedAt`
- `userPrompt`
- `focusTag`
- `intentType`
- `parameterSnapshot`
- `resultSummary`
- `windows`
- `isIncludedInCandidatePool`

其中：

- `sourceType` 先支持 `user`
- 未来预留 `system`，用于后台定时主动计算

### 2.3 候选窗口 Candidate Window

右侧列表真正展示的对象。每个候选窗口必须能够追溯到来源版本。

候选窗口最少包含：

- `id`
- `windowId`
- `versionId`
- `conversationId`
- `generatedAt`
- `focusTag`
- `generatedFromPrompt`
- `metrics`
- `displayOrder`
- `isPinned`
- `isDeleted`

### 2.4 Top3 推荐 Recommendation Set

Top3 不是独立数据源，而是候选窗口池在当前排序规则下的派生视图。

计算规则：

- 候选窗口少于 3 个时，有几个推荐几个
- 候选窗口多于 3 个时，默认按“最新有效版本优先”取前三
- 用户切换优化目标时，Top3 与候选池同时按新规则重新排序

## 3. P01 大盘的新定位

P01 不再叫“公共窗口大盘”，而是更接近“窗口工作台”。

建议功能拆分为 4 个区块：

### 3.1 顶部排序/优化目标区

作用：对当前候选窗口池重新排序，而不是触发新的计算。

第一阶段保留 4 类排序：

- 综合成本最优
- 低机会成本优先
- 低直接成本优先
- 高风险抢修优先

### 3.2 Top3 推荐区

从当前候选窗口池派生而来。

每个推荐卡片必须显示：

- 窗口基础信息
- 来自哪个版本
- 该版本的侧重点
- 推荐原因摘要

### 3.3 候选窗口列表区

展示当前会话下所有已纳入候选池的窗口。

第一阶段必须支持：

- 删除窗口
- 调整展示顺序
- 按时间段筛选
- 按版本来源筛选
- 按优化目标排序

### 3.4 版本来源区

展示当前会话生成过的所有窗口版本。

每个版本卡片显示：

- 版本名
- 生成时间
- 用户当时的提问
- 侧重点标签
- 参数快照摘要
- 窗口数量
- 是否纳入候选池

第一阶段必须支持：

- 将版本加入候选池
- 将版本移出候选池
- 删除版本
- 查看版本详情

## 4. 左侧 Agent 的正确职责

左侧不是聊天框，而是产品核心控制台。

它需要承担 3 类动作：

### 4.1 生成新版本

例如：

- “未来 7 天按经济优先重算”
- “只看 1 号和 2 号风机”
- “去掉变频器”
- “比较经济优先和风险优先”

结果：产生一个或多个新窗口版本。

### 4.2 管理已生成版本

例如：

- “把上一版也加入候选”
- “删掉刚才那版”
- “只保留风险优先那组”

结果：修改右侧候选窗口池与展示状态。

### 4.3 解释版本和推荐

例如：

- “为什么这个窗口排第一”
- “这版和上一版差在哪里”
- “这个版本更偏成本还是更偏风险”

结果：不新增版本，只解释已有结构化结果。

## 5. Skill 在产品中的角色

`maintenance-window-forecast` skill 是左侧 agent 的核心执行底座，不是附属功能。

### 5.1 Skill 当前适合处理的能力

- 权重调整
- 约束参数调整
- 对象范围调整
- 组件参数调整
- 预置策略对比

### 5.2 Skill 当前不适合处理的能力

- 新数学约束发明
- 动态改写模型结构
- 用户没有参数载体的泛化要求

### 5.3 技术集成原则

对话层职责：

- 理解自然语言
- 判断是否需要澄清
- 生成结构化 `updates`
- 决定生成一个版本还是多个版本
- 用业务语言解释结果

Skill/执行器职责：

- 校验 `updates`
- 真正执行计算
- 返回真实结果

右侧页面职责：

- 将版本和窗口结果结构化呈现

## 6. 前端架构方案

## 6.1 当前前端技术栈

- React 18
- Vite
- Tailwind 样式
- Radix UI 组件
- Recharts 图表

## 6.2 前端重构原则

- 右侧页面不再维护业务真相源
- 所有核心状态统一收束到一个 Agent Store
- 页面只从 Store 读取结构化结果并展示

## 6.3 建议的前端模块

### A. Shell 层

- `src/App.tsx`
- `src/layouts/ProductShell.tsx`

职责：

- 左右布局
- 顶部导航
- 全局状态提供

### B. Agent 状态层

建议新增：

- `src/store/agent-store.ts`
- `src/store/types.ts`
- `src/store/selectors.ts`

职责：

- 会话管理
- 记忆管理
- 版本池管理
- 候选池管理
- 当前页面视图管理
- 执行状态管理

### C. Agent UI 层

建议拆分：

- `src/components/agent/AgentPanel.tsx`
- `src/components/agent/ConversationList.tsx`
- `src/components/agent/MessageThread.tsx`
- `src/components/agent/ChatComposer.tsx`
- `src/components/agent/MemoryDrawer.tsx`

职责：

- 展示会话列表
- 展示消息线程
- 发送消息
- 展示当前会话工作记忆与版本摘要

### D. 页面展示层

现有页面保留，但角色调整：

- `P01Dashboard.tsx` 改为窗口工作台
- `P02VoyageAnalysis.tsx` 读候选窗口和版本结果
- `P03TurbineCheck.tsx` 读单机校验结果
- `P04RuleManagement.tsx` 成为对话和表单双入口规则中心
- `P05Analytics.tsx` 展示历史统计和会话级运营指标

### E. API 调用层

建议新增：

- `src/lib/api-client.ts`
- `src/lib/agent-service.ts`
- `src/lib/result-normalizer.ts`

职责：

- 调用后端 agent 接口
- 将 skill 返回结果归一化为前端统一结构

## 7. 后端架构方案

## 7.1 推荐技术栈

- Python 3.10+
- FastAPI
- Pydantic
- SQLite 作为第一阶段持久化存储

原因：

- 现有 skill 是 Python
- 直接复用 skill 和运行时最顺手
- 前后端职责清晰，后续也方便接真正模型服务

## 7.2 后端核心模块

建议新增：

- `backend/app/main.py`
- `backend/app/api/agent.py`
- `backend/app/api/conversations.py`
- `backend/app/api/versions.py`
- `backend/app/api/dashboard.py`
- `backend/app/core/agent_runtime.py`
- `backend/app/core/memory_manager.py`
- `backend/app/core/version_manager.py`
- `backend/app/core/result_normalizer.py`
- `backend/app/core/skill_executor.py`
- `backend/app/models/*.py`
- `backend/app/db/*.py`

## 7.3 后端职责划分

### A. Agent Runtime

负责：

- 处理用户消息
- 读取会话上下文
- 判断是生成版本、管理版本还是解释结果
- 调用 skill 执行器
- 将结果写入数据库
- 返回前端所需的结构化响应

### B. Memory Manager

负责：

- 维护工作记忆
- 压缩长会话摘要
- 读取用户偏好

### C. Version Manager

负责：

- 创建窗口版本
- 展开版本中的窗口到候选池
- 删除/归档版本
- 计算 Top3 推荐

### D. Skill Executor

负责：

- 调用 `maintenance-window-forecast` 的运行时
- 处理 `updates` 和 `compare`
- 统一封装异常和输出格式

说明：

- skill 内已经存在可直接复用的执行入口：
  - `execute_parameter_updates(...)`
  - `execute_named_strategy_comparison(...)`
- 后端这里的 `skill_executor` 不应重新实现优化逻辑，只负责适配和调用现有 skill 运行时

## 8. 记忆与会话设计

这里直接借鉴 `learn-claude-code` 的核心思想：消息历史不是唯一记忆，结构化状态才是产品级记忆。

同时要注意与现有 skill 的职责边界：

- skill 已经定义了“领域内支持哪些意图类别、如何把自然语言映射到 updates、何时拒绝”
- 产品后端不应再做一套独立的领域参数解释器
- 产品后端只需要做“产品级动作路由”，例如：
  - 这是要新建版本
  - 这是要管理版本
  - 这是要解释已有结果
  - 这是要切换页面
- 一旦进入“生成预测版本”分支，领域参数映射应尽量复用 skill 的规则与 few-shot 约束

## 8.1 记忆分层

### 第一层：消息记忆

存原始对话消息。

用途：

- 聊天展示
- 回放上下文

### 第二层：工作记忆

存会话当前活跃状态。

建议字段：

- `currentGoal`
- `currentIntent`
- `activeView`
- `activeFocusTag`
- `selectedVersionIds`
- `selectedWindowIds`
- `activeFilters`
- `latestResultId`
- `pendingClarification`

### 第三层：摘要记忆

当会话变长时，对老消息压缩摘要。

摘要固定保留：

- 已经生成过哪些版本
- 当前候选池有哪些窗口
- 用户最新的偏好方向
- 最近一次关键调整是什么

### 第四层：用户偏好记忆

跨会话保存。

建议字段：

- `defaultFarm`
- `defaultTimeHorizon`
- `preferredObjective`
- `preferredComponents`
- `recentStrategies`

## 8.2 会话状态机

会话建议至少有以下状态：

- `idle`
- `collecting_input`
- `running_prediction`
- `awaiting_clarification`
- `showing_results`
- `error`

## 9. P01 的默认规则

这部分作为产品显式规则固化，避免后期实现发散。

### 9.1 无对话时

- 不主动显示窗口
- 展示空状态
- 引导用户从左侧发起预测

### 9.2 每次有效预测后

- 新建一个版本
- 默认将该版本加入候选池
- 将该版本窗口插入候选池顶部

### 9.3 Top3 默认形成规则

- 候选池不足 3 个时，有几个推几个
- 默认优先最新生成的版本
- 若用户点击优化目标切换，则对候选池重排后再取 Top3

### 9.4 侧重点识别规则

每个版本必须标注 focus tag，例如：

- 综合成本
- 经济优先
- 风险优先
- 快修优先
- 缩小对象范围
- 组件排除
- 参数对比

## 10. 系统 API 草案

第一阶段建议提供以下接口：

### 会话相关

- `POST /api/conversations`
- `GET /api/conversations`
- `GET /api/conversations/{id}`
- `DELETE /api/conversations/{id}`

### 消息与 agent

- `POST /api/agent/message`

返回：

- 新消息
- 新版本列表
- 候选窗口池
- Top3 推荐
- 推荐展示页

### 版本与候选池

- `GET /api/conversations/{id}/versions`
- `POST /api/versions/{id}/include`
- `POST /api/versions/{id}/exclude`
- `DELETE /api/versions/{id}`
- `PATCH /api/candidates/reorder`
- `DELETE /api/candidates/{id}`

### 大盘数据

- `GET /api/dashboard/{conversationId}`

## 11. 后续主动计算能力预留

本期不做后台定时主动生成窗口，但必须在模型上预留。

方式：

- `WindowVersion.sourceType = user | system`
- 未来后台调度器可每天生成系统版本
- 系统版本进入同一版本池与候选池逻辑

这样后续增加主动计算时，不会推翻现有前后端架构。

## 12. 第一阶段范围建议

第一阶段只做能闭环的最小产品，不追求所有页面一次到位。

### 必做

- 左侧真实会话管理
- 左侧调用 skill 生成版本
- 统一 Agent Store
- 后端最小 agent 服务
- P01 改造成版本驱动窗口工作台
- P04 读写统一规则与策略状态

### 暂缓

- 系统定时主动算版本
- P02/P03 全量真实化
- 复杂权限体系
- 多用户协作
- 实时流式输出和长任务队列

## 13. 成功标准

如果以下 6 项全部成立，说明第一阶段产品成立：

- 用户能新建和切换会话
- 每次有效对话能生成一个窗口版本
- 右侧候选窗口列表由版本展开而来
- Top3 推荐会随候选池和排序规则变化
- 用户能删除窗口/版本并调整展示逻辑
- 多轮对话不会覆盖旧结果，而是沉淀为版本资产

## 14. 风险与控制

### 风险 1：右侧仍残留本地模拟数据逻辑

控制：

- 明确右侧只读 Store，不再自己生成核心业务数据

### 风险 2：聊天和页面操作走两套逻辑

控制：

- 所有参数变化最终都落到同一 agent/action 管道

### 风险 3：长会话状态失控

控制：

- 使用工作记忆 + 摘要记忆，而不是只堆历史消息

### 风险 4：Skill 输出与前端展示结构不匹配

控制：

- 在后端增加 `result_normalizer`

## 15. 本文档结论

本产品第一阶段不应该继续强化“静态大盘”，而应该优先完成：

- 左侧 agent 化
- 版本池与候选池建模
- 右侧 P01 工作台改造
- 会话、记忆、状态管理落地

只有这样，后续无论接更多 skill、做后台主动计算，还是扩展 P02/P03/P05，产品都不会返工。
