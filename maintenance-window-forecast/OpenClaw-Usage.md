# OpenClaw 实战调用说明

这份说明面向使用 OpenClaw 的人，解释安装 skill 后，OpenClaw 如何和这个检修窗口期预测 skill 串联工作。

## 1. 工作分工

OpenClaw 负责：

- 读 `SKILL.md`
- 读 `rules.md`
- 读 `examples.md`
- 理解用户自然语言
- 生成结构化 `updates`
- 调用 skill 自带脚本
- 再把真实结果解释给用户

skill 自带脚本负责：

- 校验 `updates`
- 运行预测计算
- 返回结构化 JSON 结果

## 2. 标准链路

用户提问：

> 经济性调高点

OpenClaw 应执行：

1. 识别为“改权重”
2. 生成：

```json
{
  "weights": {
    "loss": 1.1
  }
}
```

3. 调用：

```powershell
'{"weights":{"loss":1.1}}' | python "%USERPROFILE%\.agents\skills\maintenance-window-forecast\scripts\execute_updates.py" updates -
```

4. 读取脚本返回的 JSON
5. 把 `summary` 和 `metrics` 用业务语言解释给用户

## 3. 常见话术与动作

### 3.1 改权重

用户：

> 风险优先

OpenClaw 应：

- 识别为预置策略
- 使用 `risk_priority`

### 3.2 改约束参数

用户：

> 时间窗改48小时

OpenClaw 应：

```json
{
  "constraints": {
    "time_horizon": 48
  }
}
```

### 3.3 改对象

用户：

> 只看1号和2号风机

OpenClaw 应：

```json
{
  "objects": {
    "turbine_ids": [1, 2]
  }
}
```

### 3.4 改部件参数

用户：

> 发电机维修时长改成10小时

OpenClaw 应：

```json
{
  "component_params": {
    "发电机": {
      "duration_hours": 10
    }
  }
}
```

### 3.5 对比方案

用户：

> 对比经济优先和风险优先

OpenClaw 应调用：

```powershell
'["economic_priority","risk_priority"]' | python "%USERPROFILE%\.agents\skills\maintenance-window-forecast\scripts\execute_updates.py" compare -
```

## 4. 什么时候不要执行

下面这些话术，不应硬翻译成 `updates`：

- “齿轮箱和主轴不能同时修”
- “每天18点以后必须收工”
- “第一台风机必须先修发电机再修齿轮箱”

这些属于新增模型约束，不是当前参数调整能力。

正确做法：

- 明确告诉用户当前 skill 不支持直接实现
- 说明这是“新增约束逻辑”

## 5. VSCode 直接运行

如果你直接在 VSCode 里点击运行：

- `scripts/execute_updates.py`
  现在会自动跑一个 demo
- `scripts/maintenance_window_runtime.py`
  也会自动输出一个 demo 摘要

这样就算不走命令行传参，也能看到脚本确实在工作。

## 6. 多轮会话建议

如果 OpenClaw 具备会话状态，建议保存“最近一次已应用配置”。

这样用户说：

1. “时间窗改48小时”
2. “经济性再调高一点”

第二句就能基于第一句继续调整，而不是回到默认配置。
