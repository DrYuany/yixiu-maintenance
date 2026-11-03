# 检修窗口期预测左侧 Agent 材料清单

**日期：** 2026-04-03

本文档用于约束左侧对话框升级为真正 agent 时所需的材料，包括系统提示、工具定义、状态结构、版本规则、接口契约和异常边界。

## 1. Agent 的产品角色

左侧 agent 不是问答机器人，而是：

- 会话管理器
- 预测请求编排器
- skill 调用入口
- 版本管理入口
- 结果解释器

它的任务不是“答一句话”，而是“维护一个持续演进的窗口版本工作区”。

## 2. Agent 的输入类型

左侧 agent 需要识别 5 类输入：

### 2.1 预测生成类

用户希望生成新的窗口结果。

示例：

- “未来 7 天给我算一版综合成本最优”
- “只看 1 号和 2 号风机”
- “把时间窗改成 48 小时”

### 2.2 策略比较类

用户希望对多个策略并排比较。

示例：

- “比较经济优先和风险优先”
- “看看快修优先会不会更好”

### 2.3 版本管理类

用户不一定要新算，而是要管理已有版本。

示例：

- “把上一版也加入候选”
- “删掉刚才那版”
- “只保留风险优先那组”

### 2.4 结果解释类

用户想理解已有结果。

示例：

- “为什么这个窗口排第一”
- “这一版比上一版好在哪”

### 2.5 导航展示类

用户想跳去某个专题页看结构化结果。

示例：

- “打开规则页”
- “看这个窗口的航次分析”

## 3. Agent 的最小工具集

左侧 agent 第一阶段至少需要 6 类工具能力。

### 3.1 会话工具

- 新建会话
- 切换会话
- 重命名会话
- 删除会话

### 3.2 记忆工具

- 读取当前会话工作记忆
- 更新当前会话工作记忆
- 读取会话摘要
- 写入会话摘要

### 3.3 Skill 执行工具

- 参数更新执行
- 预置策略对比执行

### 3.4 版本工具

- 创建窗口版本
- 查询版本列表
- 删除版本
- 将版本加入/移出候选池

### 3.5 候选池工具

- 展开版本为候选窗口
- 删除候选窗口
- 调整候选窗口顺序
- 计算 Top3 推荐

### 3.6 导航工具

- 设置右侧当前展示页
- 设置当前选中的版本/窗口

## 4. Agent 系统提示词骨架

下面是第一阶段可落地的系统提示词骨架。

```text
你是检修窗口期预测产品中的核心 Agent。

你的职责不是单纯回答问题，而是帮助用户管理一个持续演进的窗口版本工作台。

你的核心任务包括：
1. 理解用户是否要生成新版本、比较策略、管理版本、解释结果或导航页面
2. 在需要时将自然语言映射为受支持的参数更新或策略比较
3. 调用预测执行器并返回真实结果
4. 每次有效计算后生成新的窗口版本，不覆盖旧版本
5. 维护候选窗口池和 Top3 推荐
6. 在回答时始终说明本次结果来自哪次意图、偏向什么目标

约束：
1. 不要伪造预测结果
2. 不要把不受支持的请求硬翻译成参数更新
3. 如果请求属于新约束或新模型能力，明确说明当前不能直接完成
4. 如果用户是在管理已有版本，不要重复计算
5. 默认认为用户最新一轮有效计算代表当前最重要偏好
```

## 5. Agent 工作记忆结构

推荐结构如下：

```json
{
  "conversationId": "conv_001",
  "currentGoal": "生成未来7天窗口并比较不同策略",
  "currentIntent": "compare_versions",
  "activeView": "P01",
  "activeFocusTag": "risk_priority",
  "selectedVersionIds": ["ver_003"],
  "selectedWindowIds": ["cand_018"],
  "activeFilters": {
    "timeRange": null,
    "versionIds": [],
    "operationType": null
  },
  "latestResultId": "ver_003",
  "pendingClarification": null
}
```

## 6. 会话摘要结构

推荐结构如下：

```json
{
  "conversationId": "conv_001",
  "summary": {
    "completedActions": [
      "已生成综合成本版本",
      "已生成风险优先版本"
    ],
    "currentCandidatePool": [
      "来自 ver_001 的 3 个窗口",
      "来自 ver_002 的 2 个窗口"
    ],
    "currentUserPreference": "用户最近更偏向风险优先",
    "latestImportantChange": "将时间窗调整为48小时"
  }
}
```

## 7. 窗口版本结构

推荐结构如下：

```json
{
  "id": "ver_003",
  "conversationId": "conv_001",
  "sourceType": "user",
  "generatedAt": "2026-04-03T18:00:00+08:00",
  "userPrompt": "比较经济优先和风险优先",
  "focusTag": "strategy_compare",
  "intentType": "compare",
  "parameterSnapshot": {
    "strategies": ["economic_priority", "risk_priority"]
  },
  "resultSummary": {
    "windowCount": 5,
    "topWindowId": "WIN-20260204-001"
  },
  "windows": [],
  "isIncludedInCandidatePool": true
}
```

## 8. 候选窗口结构

推荐结构如下：

```json
{
  "id": "cand_018",
  "windowId": "WIN-20260204-001",
  "versionId": "ver_003",
  "conversationId": "conv_001",
  "generatedAt": "2026-04-03T18:00:00+08:00",
  "focusTag": "risk_priority",
  "generatedFromPrompt": "比较经济优先和风险优先",
  "metrics": {
    "duration": 6.5,
    "opportunityCost": 12800,
    "directCost": 45000,
    "riskLevel": "low"
  },
  "displayOrder": 1,
  "isPinned": false,
  "isDeleted": false
}
```

## 9. Agent 返回给前端的标准响应

推荐统一成一个 `AgentResponse`：

```json
{
  "assistantMessage": {
    "id": "msg_021",
    "role": "assistant",
    "content": "已为你生成一版风险优先窗口，并加入候选列表。"
  },
  "actionType": "create_version",
  "recommendedView": "P01",
  "memoryPatch": {},
  "newVersions": [],
  "candidateWindowPool": [],
  "topRecommendations": [],
  "dashboardState": {
    "sortMode": "latest_first"
  }
}
```

## 10. Skill 接入材料

`maintenance-window-forecast` 目录已经具备：

- `rules.md`
- `examples.md`
- `scripts/execute_updates.py`
- `scripts/maintenance_window_runtime.py`

左侧 agent 的 skill 调用流程建议固定为：

1. 读当前会话工作记忆
2. 识别用户输入属于哪种意图
3. 若是 skill 支持的请求，则映射成 `updates` 或 `strategy_names`
4. 调用 skill 执行器
5. 结果归一化成窗口版本结构
6. 写入版本池和候选池
7. 返回解释文本和右侧展示状态

补充边界说明：

- skill 已经自带领域能力边界说明：`rules.md`、`examples.md`、`SKILL.md`
- skill 已经自带执行器入口，不需要产品后端重新写求解器
- 产品后端新增的 `skill_executor` 只是一个适配层，不是新的领域执行器
- 产品后端的 `intent_parser` 不应重做 skill 里的“参数意图分类”，只应做产品层动作分类

产品层动作分类建议仅保留：

- `generate_version`
- `manage_version`
- `explain_result`
- `navigate_view`
- `conversation_control`

其中 `generate_version` 再细分为 skill 支持的领域参数映射时，应复用 skill 规则，而不是重新发明一套

## 11. 版本管理规则

### 11.1 新版本生成规则

- 每次有效计算都必须生成新版本
- 旧版本默认保留
- 不允许直接覆盖上次版本

### 11.2 候选池纳入规则

- 用户新生成版本时，默认加入候选池
- 用户也可手动移出

### 11.3 删除规则

- 删除版本时，要同步处理其候选窗口
- 删除候选窗口不等于删除版本

### 11.4 排序规则

默认规则：

- 优先最新版本
- 再按候选窗口生成顺序

用户切换排序目标时：

- 对候选池重新排序
- Top3 重新派生

## 12. 异常边界

### 12.1 不支持的新约束

例如：

- “齿轮箱和主轴不能同时修”
- “每天 18 点后必须收工”

处理原则：

- 不生成伪造结果
- 明确说明当前 skill 不支持该类新约束

### 12.2 技术执行失败

例如：

- skill 运行时异常
- 参数不合法
- 模型解析失败

处理原则：

- 左侧明确告诉用户失败原因
- 不污染候选池
- 保留消息与错误日志

### 12.3 多轮会话过长

处理原则：

- 历史消息做摘要压缩
- 结构化版本池和候选池不丢

## 13. 第一阶段必须实现的 UI 材料

左侧至少需要：

- 会话列表
- 新建会话按钮
- 当前消息线程
- 输入框
- 快捷动作区
- 当前工作记忆摘要区

建议快捷动作包括：

- 生成一版综合成本最优
- 比较经济优先和风险优先
- 只看指定风机
- 管理候选窗口

## 14. 第一阶段不必实现的材料

- 多模型切换
- 复杂人格系统
- 主动 cron 调度
- 多用户协同
- 实时语音

## 15. 结论

左侧 agent 的落地不只是“接一个模型接口”，而是要同时具备：

- 会话容器
- 结构化记忆
- skill 编排
- 版本池管理
- 候选池管理
- 页面驱动能力

只有这些材料都齐了，左侧才算真正变成产品核心。
