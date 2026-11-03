# Agent-first：工具化的读/建模能力取代语义门控

> 日期：2026-10-29　状态：设计 + 分阶段落地

## 1. 问题

对话里出现两类失败：

1. **「这两版本方案的数学模型区别是什么」** 被关键词表判成「模型是什么」，回答通用结构，而不是两版差异。
2. **「你没听懂我说的意思？」** 被 LLM 判成 `clarify`，运行时回「没法编译成约束」。

表面是两句话答错，根因是同一个：**语义判断权在硬编码门控手里，模型看不到原话、也缺工具。**

`_submit_message` 在对话 agent 之前有 10 道确定性判断，谁先命中谁返回。

## 2. 结论：能替代，但不能全替代

| 门控 | 性质 | 处理 |
|---|---|---|
| 版本指代解析 | 语义 | 交给 agent |
| 模型问答分诊 | 语义 | 交给 agent（已完成） |
| 场景批量 | 语义 | 交给 agent |
| 是否规划请求 / DSL 解析 | 语义 | 语义部分交给 agent，校验保留 |
| 数据查询分诊 | 语义 | 交给 agent |
| 确认 / 取消 | **状态机** | **保留确定性** |
| 版本切换 | **状态** | **保留确定性** |
| 授权（不得声称已求解） | **安全** | 保留确定性 |
| DSL 校验 / 编译 / 求解 | **内核** | 不动 |

**语义理解类门控工具化会超越规则**（规则补不全说法）；**状态机与安全类必须保留**（模型会幻觉「用户确认了」）。

## 3. 风险与缓解

| 风险 | 表现 | 缓解 |
|---|---|---|
| 状态机被模型接管 | 误判确认 → 未授权求解 | 确认/取消保持确定性；solve 只出预览 |
| 弱模型综合不足 | 读两版模型只讲一版（已实测） | **工具输出即答案**：差异在工具侧算好 |
| 非确定性 | 同话不同路 | 温度 0 + 结果缓存 + 评测集 |
| 延迟成本 | 每轮多一次工具调用（实测 3.9s→6.0s） | 只对读类走 agent；prompt caching |
| 上下文膨胀 | N 版 × 完整模型 | 工具侧投影裁剪（每维限 K 条） |
| 评测口径漂移 | 题库断言依赖内部路由 | 断言改成「最终行为」 |

## 4. 统一的读工具（替代 compare_versions）

删除 `compare_versions`，把能力并入 `read_versions`：

```
read_versions(
  versions=[...],          # 1..N，省略=当前选中
  include=[...],           # 维度：tasks/unscheduled/model/costs/parameters/constraints/status
  baseline=...,            # 差异相对基准
  focus=...                # 取舍提示
)
```

N 版 + `include=["model"]` 时返回**结构化投影**而非拒绝：

```
data.plans[i].model = {
  modelFingerprint,
  objective:  [{priority, name, sense, terms}],
  constraints:[{kind, count, meaning}],
  variables:  {total, groupCount}
}
data.modelDiffs = [
  {dimension, key, baselineValue, values:[...], changed}
]
```

于是「对比」只是 `view=table` 的一种视角，不再是特例工具：

| 用户说 | 调用 |
|---|---|
| 这两版数学模型区别 | `versions=[01,02], include=["model"]` |
| 方案02 模型长什么样 | `versions=["方案02"], include=["model"]` |
| 三版成本对比 | `versions=[01,02,03], include=["costs"]` |

## 5. 建模也工具化

`solve` 工具当前只走旧规划器（`build_change_plan`），DSL 约束表达不了。
第二步：`solve` 内部改为 **DSL 优先 → 旧规划器兜底**，与运行时的
`_change_plan_from_dsl` 共用同一段逻辑（下沉到 `change_planning.py`）。
模型判断「这是改排程」才调 `solve`，DSL 的校验/编译/预览/确认链不变。

## 6. 分阶段落地

| 阶段 | 内容 | 验证 |
|---|---|---|
| P1 | `read_versions` 支持 N 版模型投影；删除 `compare_versions` | 单测 + 实机 |
| P2 | `solve` 工具 DSL 优先；`_submit_message` 在 agent 可用时整体委派 | 各测试 + 实机 |
| P3 | 移除临时正则补丁（版本指代/对话修复） | 题库 + 对抗集 |
| P4 | 评测口径改为「最终行为」断言 | 221 题库 + 70 对抗集 |

**兜底原则**：agent 不可用（无 key / 调用失败）时，仍走原有确定性链路，离线与测试行为不变。

## 7. 落地结果（2026-10-29）

| 阶段 | 状态 | 说明 |
|---|---|---|
| P1 | ✅ | `read_versions(include=["model"])` 支持 N 版投影 + `modelDiffs`；删除 `compare_versions` |
| P2 | ✅ | 新增 `core/change_planning.py`；`solve` 工具 DSL 优先；`_submit_message` 整体委派 agent |
| P3 | ⚠️ 部分 | 语义门控已移出主链路；`_compare_versions_reply`、对话修复正则保留为**无 LLM 时的兜底**，删除只会降低离线可用性 |
| P4 | ✅ 初始 | 题库 99.5%（单条波动，复跑为 100%）、对抗集 94.3%、后端 293 测试通过 |

实测（总链路）：

| 输入 | 结果 |
|---|---|
| 你好 | `assistant_reply`，不调工具，3.5s |
| 任务001和任务002不能在同一天出海维修 | 调 `solve` → `change_preview`（DSL 待确认），6.2s |
| 这两版本方案的数学模型区别是什么 | 调 `read_versions(include=[model])` → 指纹/目标/约束/参数差异 |

顺带修复：`同一条线` 被旧编译器误当成「作业线数=1」；DSL 待确认缺 `request` 段导致落库 500。

