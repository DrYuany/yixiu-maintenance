# 环境数据可视化设计

## 目标

在检修排程 Agent 的回答消息中，把风速、电价、海况和目标检修窗口展示为可交互图表。默认展示 7 天，用户可在同一张图中切换 3、7、14、30 天，不新增 Agent 工具，也不依赖大模型重算或重新组织数字。

## 数据契约

`read_environment` 增加 `detail="chart"`。该模式一次返回：

- 最多 30 天的逐日聚合；
- 前 7 天的逐时风速、电价和功率；
- 规划时点、规划边界、目标窗口、输入哈希和数据性质；
- `chartSpec`，包含 `schemaVersion`、`defaultRange`、`availableRanges`、`hourly`、`daily` 和标注区间。

协议固定为 `environment-chart.v1`：

```json
{
  "schemaVersion": "environment-chart.v1",
  "timezone": "Asia/Shanghai",
  "defaultRange": 7,
  "availableRanges": [3, 7, 14, 30],
  "units": {"wind": "m/s", "price": "元/MWh", "power": "MW", "usableHours": "小时"},
  "hourly": [{"time": "ISO-8601+08:00", "wind": 8.2, "price": 430.0, "powerMw": 6.1}],
  "daily": [{"date": "YYYY-MM-DD", "windMean": 8.2, "priceMean": 430.0, "seaState": "良好", "usableHours": 8.0}],
  "annotations": {
    "planningBoundary": "ISO-8601+08:00|null",
    "targetWindow": "object|null；非空时为 {id,start,end,approval}"
  },
  "dataNature": "合成|真实|其他来源标签",
  "source": "string|null",
  "inputHash": "string|null"
}
```

数组按时间升序稳定排列。数值缺测允许为 `null`，前端不得插值。逐日风速和电价取日均值，可用小时取当日可作业时长。3/7 天分别截取规划时点之后 72/168 个逐时点；14/30 天截取从规划时点所在自然日开始的 14/30 个日桶，首尾可为部分日。`availableRanges` 只列数据完整支持的范围；`defaultRange` 不可用时回退到不超过请求值的最大可用范围。

默认范围由确定性规则解析：`三天/3天 → 3`、`一周/7天 → 7`、`两周/14天/半个月 → 14`、`一个月/30天/四周 → 30`；其他天数映射到距离最近的受支持范围，超出上限映射到 30，未指定时为 7。同一句出现多个范围时按 30、14、3、其他数字、默认 7 的固定顺序处理，不交给大模型决定。

后端按 ```` ```chart\n{合法 JSON}\n``` ```` 序列化。前端只解析 `environment-chart.v1`；非法 JSON、未知版本和普通代码块原样作为代码显示。该代码块是消息内容的一部分，因此会随聊天历史保存、滚动和回放。

## 图表交互

- 默认范围由用户问句决定；未指定时为 7 天。
- 顶部提供 3 天、7 天、14 天、30 天切换。
- 3 天和 7 天使用逐时数据；14 天和 30 天使用逐日聚合。
- 风速使用左轴 `m/s`，电价使用右轴 `元/MWh`。
- 图内显示规划边界竖线、目标窗口区间和海况/可用小时提示。
- 合成数据必须显示不可隐藏的“合成测试数据”标签。
- 缺测值断线，不做插值；无目标窗口时显示说明，不伪造色带。

## 组件边界

- `environment_data.py`：只负责生成可信数据与版本化 `chartSpec`。
- `agent_runtime.py`：识别用户要求的默认天数，并请求 30 天 chart 数据。
- `MessageBody.tsx`：只识别 `chart` code block并交给图表组件；其他 Markdown 行为不变。
- `EnvironmentChart.tsx`：负责范围切换、粒度选择、双轴绘图、标注与无障碍文本。

## 错误与回退

- `chartSpec` 无法解析时，显示普通代码块，不让整条消息崩溃。
- 数据不足某个范围时禁用对应按钮并说明可用范围。
- 图表下保留一段简短的文字摘要和数据来源；即使图表不可渲染，用户仍能理解核心信息。

## 验收

- 同一条回答内可切换 3/7/14/30 天，且不产生网络请求。
- 3/7 天为逐时数据，14/30 天为逐日数据。
- 双轴单位、合成数据标签、规划边界和目标窗口均可见。
- 历史消息重新加载后图表仍可交互。
- 键盘可切换范围，选中状态有 `aria-pressed`，图表有文字摘要。

