# 单循环架构迁移计划（LLM 工具循环成为唯一理解者）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 删掉前置于工具循环的确定性词表路由（detect_operations / detect_model / 对比 / engine.say 直通），把全部消息交给 LLM 工具循环理解；原路由主体降格为「循环不可用时的降级险」；行为评测先行放宽查询桶判分作为兜底。

**架构：** 三层改动——① 评测判分支持「期望action备选」（查询桶接受 alarm_answer 与 assistant_reply 双归属）；② solve 工具补齐 engine.say 的结构化出口（场景→kind=batch、澄清→kind=clarify），runtime 循环出口把 pendingChange 转换成对应 actionType；③ `_yx_route` 重排为「pending 状态机 → LLM 工具循环 → 确定性降级险」。

**技术栈：** Python 3.12 / FastAPI / pytest / TestClient。后端测试必须带 `PLANNING_AS_OF=2026-10-23T00:00:00+08:00`。y高（LLM 在线评测需要现有环境 key）。

**仓库：** D:\文档\赋风交接\小一修产品（直接在 main 分支，延续现有做法）。

**纪律：**
- 每个任务结束即 `git commit`，**按文件选择性 add**（`git add -A` 会卷入用户未提交的 yxcore/prompt.py、reconcile.py、session.py——绝对不碰这三个文件）。
- 改代码后 live 服务（8022）必须杀掉重启才生效（最后任务统一做）。
- yxcore/ 与 backend/ 的测试基线：backend 120 passed；yxcore 6 条日期类既有失败与本次无关。

---

## 背景速查（给没有上下文的工程师）

### 现状消息流（`backend/app/core/agent_runtime.py` `_yx_route`，行 507-619）

```
pending 状态机（澄清多问循环 / 确认 / 取消）——保留
├─ detect_operations_question（正则词表）→ 确定性查询作答（alarm_answer 等）   ← 要删（降级险）
├─ detect_model_question（正则词表）→ capability/environment/structure/parameter/status    ← 要删
│        └─ formula → _answer_agent_question → LLM 工具循环
├─ 对比正则 → 确定性 version_compare ／ 版本对比                               ← 要删
└─ engine.say 直通（场景/澄清/变更/非变更→工具循环）                              ← 要删
└─ 兜底「这句没接：没有对应的旋钮。」
```

### 目标消息流

```
pending 状态机 —— 保留（流程状态不是理解）
├─ LLM 工具循环（唯一理解者）：conversation_agent.answer(...)
│     模型自主决定：读工具答查询 / 调 solve 改排程（solve 内部 = engine.say）
│     循环出口：toolbox 产出的 pendingChange → 转换为 change_preview / batch_preview / clarification
│     循环失败（ConversationAgentError / 未配 key）→ 降级险
└─ 降级险 `_deterministic_route`（原路由主体整体搬迁，只作兜底）：
      detect_operations / detect_model / compare / engine.say 直通 / 兜底文案
      （engine.say 直通里原本的「非变更→工具循环」子分支删掉，改为确定性 echo 回显）
```

### 已存在、直接复用的东西

- `AgentToolbox._solve_inner`（agent_tools.py:1613）：已调 `self.yx_engine.say(instruction, timeout=90)`，已处理「变更→needs_confirmation+pending(payload kind=yx)」和「错误/拒绝→clarify envelope」。**缺**：`turn.场景`、`turn.澄清` 两个结构化出口（现在落入 `not (新增 or 修订 or 撤销)` 分支被当成不可理解）。
- `AgentToolbox.pending_change` / `pending_touched` / `on_pending_change`（agent_tools.py:177-179，152 装配）：`on_pending_change` 就是 `self._persist_agent_pending(conversation_id, payload)`。
- 一修参照（AI窗口期预测产品 repo `backend/app/core/agent_runtime.py` 1753-1757）：循环后按 `toolbox.pending_touched` 做旧 pending 清理——小一修迁移时丢了这个，要补。
- 路由层 batch payload 形状：`{"kind": "batch", "scenarios": [{"标签": str, "patches": [p.to_json()]}], "sourcePrompt", "preview"(本次新增)}`；消费方 `_start_batch_solve`（agent_runtime.py:679-691）只读 `scenarios[].标签/patches`。
- 路由层 clarify payload 形状（kind=clarify）：`{"kind": "clarify", "groups": _serialize_clarify_groups(groups), "chosen": [], "nextIndex": 0, "sourcePrompt"}`；pending 状态机和 `_clarify_group_text(groups[0], 0, len(groups))`（agent_runtime.py 568-581）直接消费。
- 澄清序列化在 `AgentRuntime._serialize_clarify_groups`——工具箱侧要用同一份格式，须抽成模块级函数共享（见 Task 2）。
- 行为评测：`backend/app/evals/behavior_eval.py`（机械判分）、`backend/app/evals/run_behavior_eval.py`（TestClient 跑真实 agent，需要 key）、`backend/eval_cases/behavior_cases.json`（7 条）。
- `conversation_agent.py`：AUTHORITATIVE_TOOLS 里含 solve——工具结果带 `markdown+verbatim` 时独占本轮输出（模型闲聊被丢弃）。澄清/场景 envelope 走 `_as_contract`（`_solve` 的 `_as_contract` 包裹，agent_tools.py:1603-1611）会自动补 markdown+verbatim，所以结构化出口乘这趟车。

---

## Task 1: 评测兜底——判分放宽 + 查询桶扩用例

**Files:**
- Modify: `backend/app/evals/behavior_eval.py`
- Modify: `backend/eval_cases/behavior_cases.json`
- Test: `backend/tests/test_behavior_eval.py`

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_behavior_eval.py` 追加：

```python
def test_action_alternative_accepts_loop_reply():
    """查询桶双归属：路由删掉后同一句在线会变成 assistant_reply，判分必须认。"""
    case = {"id": "t", "期望action": "alarm_answer", "期望action备选": ["assistant_reply"]}
    result = evaluate_behavior(case, {"actionType": "assistant_reply",
                                      "assistantMessage": {"content": "ok"}})
    assert result.passed

def test_action_alternative_still_rejects_wrong():
    case = {"id": "t", "期望action": "alarm_answer", "期望action备选": ["assistant_reply"]}
    result = evaluate_behavior(case, {"actionType": "change_preview",
                                      "assistantMessage": {"content": "ok"}})
    assert not result.passed
```

- [ ] **Step 2: 跑测试确认失败**

```
$env:PLANNING_AS_OF="2026-10-23T00:00:00+08:00"; py -3.12 -m pytest backend/tests/test_behavior_eval.py -q
```
预期：`test_action_alternative_accepts_loop_reply` FAIL（KeyError 或 passed=False）。

- [ ] **Step 3: 实现「期望action备选」**

`behavior_eval.py` 的 action 判分改为：

```python
    expected_action = case.get("期望action")
    if expected_action:
        actual = response.get("actionType")
        # 备选：同一路由迁移到 LLM 工具循环后，同一句话的 actionType 会变
        #（如查询桶 alarm_answer → assistant_reply）。判分认「谁答的」，不认「谁想的」。
        allowed = {expected_action} | set(case.get("期望action备选") or [])
        result.add("action", actual in allowed,
                   f"期望 {expected_action} 或备选 {'、'.join(sorted(allowed - {expected_action})) or '无'}，得到 {actual}")
```

（若原实现里 add() 的 message 形参签名不同，按现有签名取齐。）

- [ ] **Step 4: 跑测试确认通过**（同 Step 2 命令，预期 PASS）

- [ ] **Step 5: 扩评测用例**

`backend/eval_cases/behavior_cases.json`：给两条查询用例加备选，新增两条词表注定漏接的话术（终态下由循环读 read_alarms / read_environment 答）：

```json
  {"id": "q-alarm-002", "桶": "查询", "话术": "1-10号风机有故障么", "期望action": "alarm_answer", "期望action备选": ["assistant_reply"], "期望关键词": ["告警"]},
  {"id": "q-alarm-003", "桶": "查询", "话术": "最近三天新收了哪些告警", "期望action": "alarm_answer", "期望action备选": ["assistant_reply"], "期望关键词": ["告警"]},
  {"id": "q-env-001", "桶": "查询", "话术": "未来一周风况怎么样，适不适合出海", "期望action": "environment_answer", "期望action备选": ["assistant_reply"], "期望关键词": ["风速"]}
```

（原有行保留原样，只把 q-alarm-002 整行替换。若 environment 确定性答案的 actionType 不是 `environment_answer`，以实际为准核对后写入。）

- [ ] **Step 6: 在线行为评测首跑**

```
py -3.12 -m app.evals.run_behavior_eval --cases backend/eval_cases/behavior_cases.json
```
预期：迁移前的基线全绿（offline 降级路径 = 原行为）。有失败就修用例口径（actionType 名对不上属预期内修正），不修产品代码。

- [ ] **Step 7: 提交**

```
git add backend/app/evals/behavior_eval.py backend/eval_cases/behavior_cases.json backend/tests/test_behavior_eval.py
git commit -m "test: behavior eval accepts alternative actionType (查询桶双归属) + 词表漏接用例"
```

---

## Task 2: solve 工具补齐场景/澄清结构化出口

**Files:**
- Modify: `backend/app/core/agent_tools.py`（`_solve_inner`、抽取 `_serialize_clarify_groups` 为模块级函数）
- Modify: `backend/app/core/agent_runtime.py`（`_serialize_clarify_groups` 改为委托模块级函数）
- Test: `backend/tests/test_agent_tools_solve_exits.py`（新建）

- [ ] **Step 1: 抽取共享序列化**

把 `AgentRuntime._serialize_clarify_groups` 的实现搬成 `agent_tools.py` 模块级函数 `serialize_clarify_groups(groups)`；`AgentRuntime._serialize_clarify_groups` 改为 `return serialize_clarify_groups(groups)`。先读现实现再搬运，不改逻辑。

**注**：backend/tests 里目前没有任何构造过 `AgentToolbox` 的先例——Task 2 Step 2 的测试要自己写最小 stub，例如：

```python
from types import SimpleNamespace
stub_versions = SimpleNamespace(list_by_conversation=lambda cid: [])
```

- [ ] **Step 2: 写失败测试**

新建 `backend/tests/test_agent_tools_solve_exits.py`：构造 AgentToolbox（fake yx_engine：`say()` 返回带 `.场景` / `.澄清` / `.echo()` 的对象），验证：

```python
def _toolbox(engine):
    persisted = []
    return AgentToolbox(conversation_id="c1", versions=None,
                        model_artifacts=None, yx_engine=engine,
                        on_pending_change=lambda p: persisted.append(p)), persisted
```

1. `turn.场景` 非空 → envelope `status == "needs_confirmation"`，`data.markdown` 含「将按这些方案分别排」，persisted[0] `kind=="batch"`，scenarios 各条有 `标签`+`patches`，且 payload 带 `preview`（runtime 转换要用）。
2. `turn.澄清` 非空 → envelope `status == "clarify"`，persisted[0] `kind=="clarify"`，groups 与 `serialize_clarify_groups(turn.澄清)` 同构。
3. `_solve_inner_cleared`：status 为 `clarify` 时 `pending_change` **不被清空**（澄清也要留待用户）。现有实现 `!= "needs_confirmation"` 会清掉，要改成 `not in {"needs_confirmation", "clarify"}`。

（版本/模型产物仓储 None 是否可构造 AgentToolbox，以 __init__ 实现「只存引用不校验」为准；`tool Specs` 构造不触库。不行就传现有测试已用的 fake。）

- [ ] **Step 3: 跑测试确认失败**

```
$env:PLANNING_AS_OF="2026-10-23T00:00:00+08:00"; py -3.12 -m pytest backend/tests/test_agent_tools_solve_exits.py -q
```

- [ ] **Step 4: 实现**

`_solve_inner` 在 `echo = turn.echo()` 后、错误/拒绝判断前插入（转载处保持 `self.pending_change` 并调 `on_pending_change`，与 kind=yx 同一套机制）：

```python
        # 复合需求 → N 套场景 → 批量预览 → 确认后并发求解（与路由直通版同构，
        # 对账口径唯一：pendingChange kind=batch，消费方 _start_batch_solve）。
        if turn.场景:
            scenarios = [
                {"标签": str(sc.get("标签") or f"方案{i + 1}"),
                 "patches": [p.to_json() for p in sc.get("patches") or []]}
                for i, sc in enumerate(turn.场景)
            ]
            labels = [sc["标签"] for sc in scenarios]
            preview = ("将按这些方案分别排：\n" + "\n".join(f"- {lab}" for lab in labels)
                       + "\n\n确认后并发求解，全部算完再对比。")
            payload = {"kind": "batch", "scenarios": scenarios,
                       "sourcePrompt": instruction,
                       "utterance": self.user_message or instruction,
                       "preview": preview}
            self.pending_change = payload
            if self.on_pending_change:
                self.on_pending_change(payload)
            return self._envelope(
                "needs_confirmation",
                {"preview": preview, "markdown": preview, "verbatim": True,
                 "requiresUserConfirmation": True},
                next_actions=["等待用户确认；确认后将并发求解全部场景并对比。"],
            )
        # 指代/范围/顺序不清 → 枚举候选让用户点选。pendingChange kind=clarify
        # 由 runtime 状态机接管（序号选择、多问循环、「都不是我重新说」出口）。
        if turn.澄清:
            payload = {"kind": "clarify",
                       "groups": serialize_clarify_groups(turn.澄清),
                       "chosen": [], "nextIndex": 0,
                       "sourcePrompt": instruction}
            self.pending_change = payload
            if self.on_pending_change:
                self.on_pending_change(payload)
            return self._envelope(
                "clarify",
                {"message": "这句要澄清几个问题，下面列出候选请你选。",
                 "markdown": "这句要澄清几个问题，下面列出候选请你选。",
                 "verbatim": True, "requiresUserConfirmation": False},
                next_actions=["等待用户点选澄清候选。"],
            )
```

并把 `_solve_inner_cleared` 的清理条件改为：`status not in {"needs_confirmation", "clarify"}` → `pending_change = None`。

注意顺序：错误/拒绝分支（原「`turn.错误 or turn.拒绝 or not (新增 or 修订 or 撤销)`」）保留在场景/澄清判断之后，作为「真不可理解」出口。

- [ ] **Step 5: 跑测试确认通过**（同 Step 3 命令 + 全量 backend 套件不回归）

- [ ] **Step 6: 提交**

```
git add backend/app/core/agent_tools.py backend/app/core/agent_runtime.py backend/tests/test_agent_tools_solve_exits.py
git commit -m "feat: solve tool handles yxcore structured exits (场景→batch, 澄清→clarify)"
```

---

## Task 3: 循环出口结构化转换 + 一修式 pending 清理

**Files:**
- Modify: `backend/app/core/agent_runtime.py`（`_answer_with_conversation_agent`、新增 `_finalize_loop_response`）
- Test: `backend/tests/test_loop_finalize.py`（新建）

- [ ] **Step 1: 写失败测试**

新建测试文件，直接构造 AgentRuntime（或最小 `object.__new__(AgentRuntime)` 绕过重构造）调用 `_finalize_loop_response`，按 memory.activeFilters 的 pendingChange 验证 actionType 转换：

```python
def test_loop_reply_to_change_preview(): ...
def test_loop_reply_to_batch_preview(): ...          # 用 pending["preview"] 当正文
def test_loop_reply_to_clarification(): ...           # actionType=clarification 且带 clarificationOptions
def test_loop_reply_plain(): ...                      # 无 pending → assistant_reply 原样
```

- [ ] **Step 2: 跑测试确认失败**

- [ ] **Step 3: 实现 `_finalize_loop_response` 并改造 `_answer_with_conversation_agent`**

```python
    def _finalize_loop_response(self, conversation_id, reply, memory):
        """循环出口：模型调过 solve 时按其结构化结果定 actionType。

        判定不重猜——pendingChange 是 solve 工具刚落库的事实（on_pending_change
        幂等：落库发生在工具里，这里只负责把 reply 与结构一起送进响应）。"""
        latest = self.memories.get_or_create(conversation_id)
        pending = (latest.activeFilters or {}).get("pendingChange")
        if isinstance(pending, dict):
            kind = pending.get("kind")
            if kind == "yx":
                return self._finalize_response(
                    conversation_id, reply, "change_preview", latest.activeView, [])
            if kind == "batch":
                return self._finalize_response(
                    conversation_id,
                    str(pending.get("preview") or reply),
                    "batch_preview", latest.activeView, [])
            if kind == "clarify":
                groups = pending.get("groups") or []
                if groups:
                    text, options = self._clarify_group_text(groups[0], 0, len(groups))
                    return self._finalize_response(
                        conversation_id, text, "clarification", latest.activeView,
                        [], clarification_options=options)
        return self._finalize_response(
            conversation_id, reply, "assistant_reply", latest.activeView, [])
```

`_answer_with_conversation_agent` 尾部补一修式清理（参照 AI窗口期预测产品/agent_runtime.py 1753-1757）：

```python
        if toolbox.pending_touched:
            if toolbox.pending_change:
                self._persist_agent_pending(conversation_id, toolbox.pending_change)
            else:
                self._clear_persisted_pending(conversation_id)  # 一修 cutoff：「没剩下就作废上轮遗留」
```

solution note：`_persist_agent_pending` 已有实现；`_clear_persisted_pending` 已有实现。

- [ ] **Step 4: 跑测试确认通过 + 全量 backend**

- [ ] **Step 5: 提交**

```
git add backend/app/core/agent_runtime.py backend/tests/test_loop_finalize.py
git commit -m "feat: loop exit converts pendingChange to change/batch/clarification actions; port stale-pending cleanup"
```

---

## Task 4: `_yx_route` 重排——循环唯一理解者 + 降级险

**Files:**
- Modify: `backend/app/core/agent_runtime.py`（`_yx_route` 主体）
- Test: 直接跑全量 backend 回归 + Task 5 在线行为评测

- [ ] **Step 1: 重排 `_yx_route`**

把 507-619 段（`detect_operations_question` 起、兜底文案止）抽成新方法 `_deterministic_route(conversation_id, content, selected_version, memory, sink)`，**原样保留**，只做两处删改：

1. 删掉「非排程约束 → 复用一修的 LLM 工具循环答」子分支（agent_runtime.py 588-602 那段 try/except ConversationAgentError）——降级险只确定性回显 echo（`change_rejected` / `assistant_reply`）。
2. 模型问句分支里 `_answer_agent_question` 的引用：降级险里 formula 没有循环可走，直接 `_answer_model_question`（现有函数）。

`_yx_route` 新主体（伪代码，保留 pending 状态机整段）：

```python
        # 0. 待确认变更：取消 / 确认（两步求解）—— 保留原文不动
        ...原 pending 状态机...

        # 1. LLM 工具循环：唯一理解者
        if self.conversation_agent.enabled:
            try:
                reply = self._answer_with_conversation_agent(
                    conversation_id=conversation_id,
                    content=content,
                    history=self.messages.list_by_conversation(conversation_id),
                    memory=memory,
                    selected_version=selected_version,
                    sink=sink,
                )
                return self._finalize_loop_response(conversation_id, reply, memory)
            except ConversationAgentError as exc:
                self._note_degradation(
                    "agent_unavailable",
                    "模型这一轮没答上来，下面是规则兜底生成的答复，可能不如模型答得全。",
                    str(exc),
                )

        # 2. 降级险：确定性路由（key 未配 / 循环挂了时才走到这）
        return self._deterministic_route(conversation_id, content, selected_version, memory, sink)
```

- [ ] **Step 2: 删除被替代的 `_answer_agent_question`**（我昨天为接回数学模型加的过渡入口；单循环下无调用方）。先 `Select-String` 全仓库确认无其它引用（含 tests），有引用的测试改走 `_finalize_loop_response` / 降级险语义。

- [ ] **Step 3: 回归**

```
$env:PLANNING_AS_OF="2026-10-23T00:00:00+08:00"; py -3.12 -m pytest backend/tests -q
```
预期 120 passed。Yxcore 全量（194s）失败集合与基线是否一致。

- [ ] **Step 4: 提交**

```
git add backend/app/core/agent_runtime.py
git commit -m "refactor: single loop — LLM tool loop is the only understander；deterministic routes demoted to fallback"
```

---

## Task 5: 在线行为评测 + 冒烟 + 服务重启

**Files:** 无新改动（验证与收尾）。失败的评测用例允许调整 `期望关键词/选项数` 口径（行为对但措辞变了的），不允许为过测评改产品语义。

- [ ] **Step 1: 在线行为评测**

```
py -3.12 -m app.evals.run_behavior_eval --cases backend/eval_cases/behavior_cases.json --baseline product-output/behavior-baseline.json
```
预期：全绿；尤其 q-alarm-003 / q-env-001 走循环给出关键词。失败则读明细，判断是模型行为差异（调口径）还是真退化（修代码）。

- [ ] **Step 2: 端到端冒烟（新会话，真实 HTTP 不用 TestClient）**

写临时脚本 `_diag_single_loop.py`（Python/urllib UTF-8，完结后删）按序：出方案→确认→数学模型问句→「1-10号风机有故障么」→「按经济优先和严重性优先分别排然后对比」→确认→「船租涨两成」→澄清链（「只完成前五个任务」→点选→确认）。核对重点：
1. 查询类不再被词表拦截（alarm/env 从循环走通）；
2. 数学模型问句仍出 formula LaTeX；
3. 批量场景走 batch_preview，单变更走 change_preview；
4. 澄清多问循环与「都不是，我重新说」出口仍在。

- [ ] **Step 3: 全量回归终跑**

backend 全量 + yxcore failure 集与基线对话一致。

- [ ] **Step 4: 重启 live 服务**

杀 8022 → 后台重启（PLANNING_AS_OF=2026-10-23T00:00:00+08:00, XYX_SOLVE_SECONDS=300）→ 一次浏览器级验证「能看下你用的数学模型么」。

- [ ] **Step 5: 清理与收尾**

删临时脚本；`git add -A` 禁止——只 add 实际改动过的评测/代码文件；最后一笔若有口径调整测试一并提交。

---

## 风险与回退

- **在线评测依赖真实 key 且行为有波动**：机械判分只看 actionType/关键词/选项数，漏了就加备选；不改产品语义。
- **离线（无 key）环境行为不变**：降级险完整保留原路由，pytest 全量必须仍然 120 passed。
- **向后兼容断点**：kind=yx / batch / clarify pending 的消费方（pending 状态机、`_start_batch_solve`、`_submit_pending_change`）一律不动。
- **回退方式**：单 commit 结构清晰（Task 4 一个 commit），`git revert` 即可回到双路由。
