# Environment Chart Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Agent 消息中提供可切换 3/7/14/30 天的风速、电价与检修窗口图表。

**Architecture:** 后端生成版本化 chart spec 并嵌入 Markdown fenced code block；前端 `MessageBody` 拦截该代码块并交给独立 Recharts 组件渲染。所有范围数据一次随消息返回，切换完全在浏览器本地完成。

**Tech Stack:** FastAPI/Python、React/TypeScript、React Markdown、Recharts、pytest、Vitest/Testing Library。

---

### Task 1: 定义环境图表契约

**Files:**
- Modify: `backend/app/core/environment_data.py`
- Test: `backend/tests/test_agent_tools.py`

- [ ] 写失败测试，断言 chart spec 包含 30 天日数据、7 天逐时数据、范围和标注。
- [ ] 运行 `python -m pytest backend/tests/test_agent_tools.py::test_read_environment_chart_returns_all_switchable_ranges -q`，确认因 `chartSpec` 缺失而失败。
- [ ] 实现版本化 chart spec 生成函数。
- [ ] 运行目标 pytest，确认通过。

### Task 2: 将图表写入回答消息

**Files:**
- Modify: `backend/app/core/agent_tools.py`
- Modify: `backend/app/core/agent_runtime.py`
- Test: `backend/tests/test_agent_api.py`

- [ ] 写失败测试，断言环境回答包含 `chart` code block 和用户要求的默认范围。
- [ ] 增加消息重新读取测试，断言 chart block 逐字持久化。
- [ ] 运行 `python -m pytest backend/tests/test_agent_api.py -k environment -q`，确认因 chart block 缺失而失败。
- [ ] 增加 `detail="chart"` 并在直答链路请求 30 天数据。
- [ ] 运行目标 pytest，确认通过。

### Task 3: 实现消息内图表组件

**Files:**
- Create: `src/components/agent/EnvironmentChart.tsx`
- Modify: `src/components/agent/MessageBody.tsx`
- Modify: `src/index.css`
- Test: `src/components/agent/__tests__/EnvironmentChart.test.tsx`
- Test: `src/components/agent/__tests__/MessageBodyChart.test.tsx`

- [ ] 写失败测试，覆盖四个范围、粒度、单位、标注、合成标签、禁用范围、无目标窗口、缺测断线说明、数据来源、无网络请求和 ARIA。
- [ ] 写失败测试，覆盖合法 block、非法 JSON、未知版本、普通 Markdown 和多个 code block。
- [ ] 增加消息 hydration 测试，重新渲染持久化内容后仍可切换范围。
- [ ] 运行 `npx vitest run src/components/agent/__tests__/EnvironmentChart.test.tsx src/components/agent/__tests__/MessageBodyChart.test.tsx`，确认组件尚不存在而失败。
- [ ] 用 Recharts 实现双轴折线、范围按钮、窗口与边界标注。
- [ ] 在 Markdown code renderer 中解析 `language-chart`。
- [ ] 运行目标 Vitest，确认通过。

### Task 4: 集成验证

**Files:**
- Modify: `backend/tests/test_agent_api.py`
- Modify: `src/components/agent/__tests__/EnvironmentChart.test.tsx`

- [ ] 运行 `python -m pytest backend/tests/test_agent_tools.py -k environment backend/tests/test_agent_api.py -k environment -q`，预期全部通过。
- [ ] 运行 `npx vitest run src/components/agent/__tests__/EnvironmentChart.test.tsx src/components/agent/__tests__/MessageBodyChart.test.tsx`，预期全部通过。
- [ ] 运行 `python -m pytest backend/tests -q` 和 `npx vitest run`，预期零失败。
- [ ] 运行 `npm run build`，预期 Vite 构建和 standalone HTML 生成成功。

