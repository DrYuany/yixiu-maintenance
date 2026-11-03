# Maintenance Window Forecast Skill

一个面向 OpenClaw / Codex 的检修窗口期预测 skill。它把自然语言层和优化求解层分开：

- 大模型负责理解用户话术，并把它转换成结构化 `updates`
- skill 自带脚本负责校验 `updates`、执行计算、返回真实结果

这个 skill 适合做检修窗口期预测 agent 的第一版能力底座。

## 1. 能做什么

当前 skill 支持的能力：

- 调整目标权重
  - 例如：经济性调高点、风险优先、尽量快修
- 调整已注册的约束参数
  - 例如：时间窗改48小时、最多同时修2个部件
- 调整对象范围
  - 例如：只看1号和2号风机、去掉变频器
- 调整部件参数
  - 例如：发电机维修时长改成10小时、齿轮箱严重性提高到12
- 做方案对比
  - 例如：对比经济优先和风险优先

当前 skill 不支持：

- 临时新增一种新的数学约束
- 让模型自动改写 MILP 结构
- 没有已注册参数承载的要求

## 2. 目录结构

```text
maintenance-window-forecast/
  SKILL.md
  README.md
  rules.md
  examples.md
  scripts/
    execute_updates.py
    maintenance_window_runtime.py
```

说明：

- `SKILL.md`
  skill 主入口，告诉模型什么时候触发、怎么执行
- `rules.md`
  能力规则，定义支持边界、模糊词解释、拒绝条件
- `examples.md`
  few-shot 示例，帮助模型稳定理解真实业务话术
- `scripts/maintenance_window_runtime.py`
  可移植运行时
- `scripts/execute_updates.py`
  命令行入口

## 3. 安装方法

把整个目录复制到：

```text
%USERPROFILE%\.agents\skills\maintenance-window-forecast
```

PowerShell 安装命令：

```powershell
New-Item -ItemType Directory -Force "$HOME\.agents\skills\maintenance-window-forecast" | Out-Null
Copy-Item ".\maintenance-window-forecast\*" "$HOME\.agents\skills\maintenance-window-forecast\" -Recurse -Force
```

如果你是从当前项目安装，可用：

```powershell
New-Item -ItemType Directory -Force "$HOME\.agents\skills\maintenance-window-forecast" | Out-Null
Copy-Item "d:\claw试验2\.agents\skills\maintenance-window-forecast\*" "$HOME\.agents\skills\maintenance-window-forecast\" -Recurse -Force
```

## 4. 依赖

建议环境：

- Python 3.10+
- `ortools`

安装：

```powershell
pip install ortools
```

说明：

- 安装了 `ortools`，skill 会优先尝试 MILP 求解
- 如果 `ortools` 不可用，当前脚本会退回启发式近似求解

## 5. 如何调用

### 5.1 参数更新

通过标准输入传入 JSON：

```powershell
'{"weights":{"loss":1.1}}' | python "%USERPROFILE%\.agents\skills\maintenance-window-forecast\scripts\execute_updates.py" updates -
```

### 5.2 对比预置策略

```powershell
'["economic_priority","risk_priority"]' | python "%USERPROFILE%\.agents\skills\maintenance-window-forecast\scripts\execute_updates.py" compare -
```

### 5.3 返回结果

脚本返回结构化 JSON，包含：

- `applied_updates`
- `applied_config`
- `component_library`
- `metrics`
- `assignments`
- `summary`

其中：

- `summary` 适合直接给用户解释
- 其余字段适合继续被 agent 读取、比较、二次推理

## 6. 自然语言使用场景

适合触发这个 skill 的话术包括：

- 经济性调高点
- 风险优先
- 尽量快修
- 时间窗改48小时
- 最多同时修2个部件
- 只看1号和2号风机
- 去掉变频器
- 发电机维修时长改成10小时
- 齿轮箱严重性提高到12
- 对比经济优先和风险优先

## 7. 模型该怎么理解这些话

不要在脚本里穷举自然语言。推荐的职责分工是：

- 大模型：
  读 `SKILL.md`、`rules.md`、`examples.md`
  把自然语言翻译成结构化 `updates`
- 脚本：
  校验 `updates`
  执行计算
  返回结果

例如：

用户说：

> 经济性调高点

大模型应生成：

```json
{
  "weights": {
    "loss": 1.1
  }
}
```

再调用执行脚本。

## 8. 模糊词解释规则

推荐统一解释：

- “一点” / “稍微” -> 约 10%
- “多一点” -> 约 20%
- “明显提高” -> 约 30%

默认基于当前配置值调整；如果当前没有上下文状态，则基于默认配置。

## 9. 不支持的请求

下面这些当前不能直接做：

- “齿轮箱和主轴不能同时修”
- “每天18点以后必须收工”
- “第一台风机必须先修发电机再修齿轮箱”
- “自动帮我改数学模型”

这些属于：

- 新增约束
- 新增模型能力

正确做法是明确告诉用户：当前 skill 只支持参数调整，不支持新增约束逻辑。

## 10. 适合开源的方式

建议开源时至少包含：

- 整个 `maintenance-window-forecast/` skill 目录
- 一个示例对话说明
- 一个安装说明
- 一个依赖说明

如果你希望别人直接即装即用，这一套目录已经是可分发的最小闭环版本。
