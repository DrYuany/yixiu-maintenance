# 源码检查清单（Source Check List）

> **历史存档**：本文是 2026-11-01 ~ 11-02 的走查记录，描述的是当时的架构与问题状态。
> 其中「关键词路由降级为起手提示」「`AGENT_ENGINE=rule` 双引擎回滚」等已在 `e0f60ca`
> 整链撤销；当前架构以 `README.md` 和 `docs/数学模型.md` 为准，旧口径逐条对照见
> `docs/槽位第一性判据复查.md` §6。**下方正文保持原样，未作修改。**

> 用途：对源码做系统性走查，逐条记录发现的问题。每条问题带状态、严重度、证据定位（file:line）与**问题来源**，最终**依据本清单做整体修改**。
>
> 维护规则：
> - 新问题按序号追加，不做匿名问题——每条必须落到具体文件与行号，写清「现象 / 根因 / 建议方向」。
> - **必须记录问题来源**：哪个文档、哪块源码、以什么方式引出了这个问题（例如「读系统提示词 18 条时对照 agent_tools.py 的 description 发现重复」）。问题是按阅读顺序浮现的，来源要还原这条线索，方便日后复盘「为什么想到这」。
> - 每次修改后更新「版本历史」，并把对应问题状态改为「已修复 / 已验证」。
> - 状态流转：`待修改 → 修改中 → 已修复 → 已验证`。整体修改阶段按本清单逐条推进，改完一条勾一条。

## 版本历史

| 版本 | 日期 | 变更 |
|------|------|------|
| v0.7 | 2026-11-02 | #1/#2/#3/#6 收口：#1 关键词路由降级为起手提示 + 新增数字溯源（`answer_grounding.py`）；#2 系统提示词 18 条压到 8 条；#3 产线默认理解引擎切 llm（`AGENT_ENGINE=rule` 回滚）；#6 核为已实现并补验收证据。全量 579 过 / 40 deselect / 0 失败 |
| v0.6 | 2026-11-02 | #4/#5 修复落地（workbuddy 主修 exclude+fallback，我补「解状态≠FEASIBLE」）；#6 仍待修改 |
| v0.5 | 2026-11-02 | 记录 #4-#6（现场排程对话暴露：exclude 约束链路中断、HEURISTIC_FALLBACK 误标、复核不验证意图） |
| v0.4 | 2026-11-01 | #3 结构分离落地：抽 `dsl_core/` 中立共享包，两产品解耦，两侧测试全绿 |
| v0.3 | 2026-11-01 | 记录问题 #3（llm-first 与 rule-first 两套理解层并存，产线未切换） |
| v0.2 | 2026-11-01 | 记录问题 #2（工具调用规则没有单一事实源）；为问题清单新增「问题来源」字段 |
| v0.1 | 2026-11-01 | 建立清单，记录问题 #1（关键词路由是补丁不是设计） |

## 状态总览（v0.7）

| # | 一句话 | 状态 |
|---|--------|------|
| 1 | 关键词路由是补丁，不是设计 | 已修复（降级为起手提示 + 数字溯源标注） |
| 2 | 工具调用规则没有单一事实源 | 已修复（提示词 18 条 → 8 条，权威归 description） |
| 3 | llm-first 与 rule-first 并存，产线未切换 | 已修复（产线默认 llm，`AGENT_ENGINE=rule` 回滚） |
| 4 | exclude 约束在 DSL→模型链路中断 | 已修复 |
| 5 | HEURISTIC_FALLBACK 误标 FEASIBLE | 已修复 |
| 6 | 独立复核不验证意图 | 已验证（两道对账门都已落地并会拦） |

## 问题列表

### #1 关键词路由是补丁，不是设计

- **状态**：已修复（2026-11-02）
- **严重度**：中（不碰正确性底线，但存在系统性漏判 / 误判，且词表会持续腐烂）
- **位置**：
  - `backend/app/core/conversation_agent.py:33-45` — `DATA_QUESTION` / `OPS_DATA` / `MATH_PASSTHROUGH` 三个关键词正则
  - `backend/app/core/conversation_agent.py:124` — `force_tool` 用 `DATA_QUESTION.search(message)` 做第 0 轮强制工具调用
  - `backend/app/core/conversation_agent.py:176` — `MATH_PASSTHROUGH.search(message)` 决定是否原样透传公式/参数表
- **问题来源**：在 `conversation_agent.py` 里选中第 33-45 行的三个关键词正则，追问「这三行是干吗的」，顺着 `force_tool` 的用法往下挖，发现关键词匹配承担了本该由工具决策完成的「何时调工具」判断。

- **现象**：用一行关键词 `search()` 判断「这句话是不是在问数据」，在第 0 轮把 `tool_choice` 从 `auto` 顶成 `any`，逼模型先去读工具而不是凭记忆编数字。

- **根因**：`tool_choice="auto"` 允许模型「选择不调工具」。叠加三个因素——system prompt 里塞了大量「产品事实」（`conversation_agent.py:320-330`）、模型是 deepseek-flash + temperature 0.2 偏懒、`max_rounds=4` 上限让第 0 轮被浪费代价高——使数据类问题特别容易在第 0 轮被模型凭记忆/常识直接回答，产出无来源的数字。

- **三个固有缺陷**：
  1. **会漏**：不命中词表的问法（如「这台风机要修吗」「还剩几个窗口」）不触发强制，该漏还是漏。
  2. **会误伤**：命中词表但本应直接答概念的问题（如「什么是目标函数」，规则 9/12 明确该直接回答）被强行拉去调工具，多此一举。
  3. **会腐烂**：词表随业务词汇扩张而漂移，永远追不上真实问法分布。

- **建议方向**（整体修改阶段二选一或组合）：
  - **A. 反向校验**（推荐）：信任 `tool_choice="auto"` 让模型自决，把防线从「猜用户意图」挪到「查模型输出」——数据类回答里出现的数字，校验是否来自某次工具调用结果；不是则判为「未经工具的事实」拒掉或强制重跑。
  - **B. 显式意图分类**：把关键词正则替换为一次轻量意图分类（LLM 或分类器），输出「问数据 / 问概念 / 要公式原文」三态，替代现在的词表启发式。

- **决策备注**：当前词表是「缓解」不是「解决」。保留它有价值（挡住 flash 第 0 轮偷懒编数字这个已被观测的故障），但要认清它注定有漏网之鱼，不应作为长期方案。

- **已做（2026-11-02，方向 A「反向校验」+ 词表降级）**：
  - `DATA_QUESTION` / `OPS_DATA` 改名 `DATA_HINT` / `OPS_HINT`（`conversation_agent.py:35-52`），
    注释写死它只是**起手提示**（决定第 0 轮 tool_choice 要不要顶成 any），不再承担「调不调工具」的判断。
  - 新增 `backend/app/core/answer_grounding.py`：不猜用户意图，只看模型最后写出来的东西——
    答案里「像业务数据」的数字（带单位 / 带小数 / 五位以上整数），能不能在**本轮工具返回的原始内容**里找到出处。
    找不到的用一行「注意：…这几个数字在本次的工具结果里没有找到出处，未经系统核实」标出来。
    依据池包含用户原话——用户自己报的数（「船租压到 8 万」）不算模型编的。
  - 接线：`conversation_agent.answer()` 在三条出口（无工具调用 / 原样快路径 / 循环结束）统一过 `_grounded()`
    （`:150-160`），工具 payload 逐条进 `sources`（`:246`）。
  - 只做字符串与浮点比较，不调模型、不联网，结果可复算；测试 `backend/tests/test_answer_grounding.py` 10 条。
- **刻意没做「拦截」，只做「标注」**，三个理由：
  1. 拦不住该拦的，反倒先拦了不该拦的——用户自己报的数和模型编的数在这条路上长得一样；
  2. 转述换单位是正常的（工具给 25000 元 → 人话 2.5 万元），一刀切会误判，所以比对带 0.5% 容差与万元/百分比换算；
  3. 状态诚实性要的是「没核实就不能说成已核实」，不是「宁可不说」。标出来，用户自己决定信不信。
- **遗留边界**：它只管**数字**，管不了「模型说了一句没有数字的错误结论」（比如「这版比上版省」但没给数）。
  这类要靠 #6 那条线上的意图对账，不在本条范围内。

### #2 工具调用规则没有单一事实源（系统提示词与工具 description 重复）

- **状态**：已修复（2026-11-02）
- **严重度**：中（不影响正确性，但规则漂移、重复维护、系统提示词臃肿挤占注意力）
- **位置**：
  - `backend/app/core/conversation_agent.py:332-384` — 系统提示词里的 18 条「工作方式」规则，其中 2/3/5/6/7 条是在复述「何时调用哪个工具」
  - `backend/app/core/agent_tools.py:191-535` — `tool_specs()` 里每个工具已经写了详尽的 `description`（read_jobs 写了「用户问『算完了吗』时用它」、submit_change 写了「必须先调过 solve」等）
- **问题来源**：读系统提示词的 18 条「工作方式」时发现绝大多数是「什么时候调用什么工具」，对照 `agent_tools.py` 的 `tool_specs()`，发现 description 里写得比系统提示词更细，遂追问「这不重复吗、Claude Code 怎么教的」。

- **现象**：同一份「何时调工具」的知识，同时写在两处——工具的 `description`（模型每轮都能看到、和工具强绑定）和系统提示词的规则条目里。两处会漂移，改了一处漏一处。

- **根因**：误把「何时调工具」当成系统级策略，实际它是工具级契约，Anthropic 的共识是放在 `description` / 参数 `description`。系统提示词真正该装的是三类 description 装不下的东西：**跨工具顺序/状态机**（solve→submit_change、dispatch→approve→feedback）、**产品级事实**（求解器是 CP-SAT、成本口径、四态）、**输出规范**（不倾倒 JSON、数字必须来自工具、公式原样保留）。

- **分类（18 条规则切分）**：
  - **纯复述 description，可删**：2（read_alarms/read_site）、3（read_versions）、5（模型 include=model）、6（成本/参数/状态 include）、7（not_available/not_found 处理）。
  - **跨工具状态机，必须留**：8（solve/submit_change 确认流程）。
  - **一半重复一半规范，拆**：4（「差异已算好」是 description 已有，删；「先结论后差异」是解读规范，留）。
  - **概览/输出规范/产品事实，留**：1（压成 3 行）、9、10、11、12、13、14-18。

- **建议方向**：
  - 把 2/3/5/6/7 从系统提示词删掉，让 description 成为「何时调工具」的唯一权威来源。
  - 只保留：工具地图（3 行，工具名以 `tool_specs()` 为准）、跨工具状态机（solve→submit_change、ledger 链路）、产品事实、输出规范。
  - 小模型（deepseek-flash）会欠读 description，「数字必须来自工具、先读再答」这类**底线**规则可保留一句重述作为强调——这是「强调」不是「重复」，但不应扩展到整段规则。

- **决策备注**：这个问题与 #1 同根——**「何时调工具」的逻辑没有单一权威来源**，一部分靠关键词正则、一部分靠系统提示词、一部分靠 description，三处各说各话。整体修改阶段应把这条统一收口：工具决策归模型 + description，跨工具顺序归系统提示词，兜底校验归后端。

- **已做（2026-11-02）**：三处收口，都落在 `conversation_agent.py` 的 `_system_prompt()`。
  1. **工作方式 18 条 → 8 条**。删掉纯复述 description 的 2/3/5/6/7（`include=["model"]`、`versions=["方案02"]`、
     `scope=`、`detail=` 这类参数级用法）；4 拆开——「差异已算好」删，「先结论后差异」并入新的第 6 条；
     13 的后半段（多版指代怎么传）删，由 read_versions 的 versions 参数说明承担。
     `7` 里「not_available/not_found 怎么说」不是「何时调哪个工具」，归入新的第 3 条（数字来源与取不到时的口径）。
  2. **保留的四类**：九工具地图（第 1 条）、跨工具状态机 solve→submit_change + ledger 先读后写（第 2 条）、
     产品事实与四态口径、输出规范（第 4 条：不倾倒 JSON、公式/参数表/契约原样输出）。
     第 1 条明写「**什么时候用哪个、参数怎么传，以每个工具的说明为准**」——把权威关系写进提示词本身。
     （原先这里写的是「六工具地图」，实际 `tool_specs()` 下发 9 个：5 个只读 + `read_jobs` + solve + submit_change + ledger。
     `read_jobs` 长期不在提示词名单里，模型不知道能查求解进度。已补上，并加守门测试数工具数。）
  3. **「产品内已有的现场数据」段瘦身**：原来按工具逐个列它返回什么（description 里都有），
     现在只留 domain 清单 + 两条不能错的口径（告警是计划快照不是实时 SCADA；空集合如实说「当前没有」，
     不说「没有这个数据域」）。
  4. **守门测试** `backend/tests/test_conversation_agent.py`：断言提示词里不再出现 `include=["model"]`
     这类参数配方（第 1 类），同时断言跨工具状态机、`comparability.savingsClaimAllowed`、`原样输出`、
     CP-SAT / UNKNOWN 这些 description 装不下的东西必须还在。改提示词时踩线会当场红。

### #3 llm-first 与 rule-first 两套理解层并存，产线仍走 rule-first，未切换

- **状态**：已修复（2026-11-02，产线默认引擎已切 llm-first）
- **严重度**：高（方向性问题——「LLM 优先」这个目标还没落地；且「两个独立产品」的架构愿望尚未达成）
- **位置**：
  - `backend/app/core/dsl/llm_parser.py:855-931` — 产线解析链顺序：批量正则 → `parse_by_rules` → not_change 规则 → legacy 桥接 → 非变更分类 → 向量检索 → **LLM（兜底，不是第一道）**
  - `llm-first/`（整目录）— LLM 主导版原型，独立目录；README 明说「新产品，不是升级」「并排对比」
  - `llm-first/src/paths.py` — 把 `../backend` 挂进 sys.path，复用 `app.core.dsl.*`（这是「混」的根源）
  - `backend/eval_cases/probe_llm_first.py` — 验证 llm-first 八路路由契约的探针，不接入产线
- **问题来源**：看 `backend/eval_cases/_probe_llm_first_result.json` 里 route/confidence 字段、追问字段含义 → 引出「probe 的八路词表（modify_plan/compare/…）与产线 llm_parser 的六路词表（dsl/batch/not_change/…）并存」→ 进一步确认 llm-first 是否接入产线 → 发现 llm-first 是独立原型、产线仍 rule-first。

- **现象**：用户目标「现在希望 llm 优先」，但产线仍跑 rule-first；llm-first 作为独立原型并排对比、未切换。两套路由词表并存且未对齐。

- **根因**：这是**刻意的「并排对比」阶段**（README §1-2 有意不合并、不设规则兜底、共用执行侧），所以 llm-first 依赖 backend（sys.path 复用），backend 对 llm-first 零引用。但这与用户「两个独立产品、不要混在一起」的最终愿望冲突。

- **依赖面（llm-first 伸手进 backend import 的模块）**：
  1. **执行侧/共享内核**（「其他都一样」该共享的部分）：`app.core.dsl.registry`（parameters/primitives/quantities）、`validator`、`compiler`、`runner`、`planning_bridge`
  2. **规则侧理解层**（仅 `rulebench.py` 对照用）：`llm_parser`、`planning_intent`、`model_change`

- **建议方向**（三选一，待用户定）：
  - **A. 复制进 llm-first**：registry/validator/compiler/runner 复制一份 → 真正自包含、独立版本；代价是执行侧两份会漂移，违背「其他都一样」。
  - **B. 抽出中立共享包**（倾向）：把执行侧抽成一个第三方目录（顶层 `dsl-core/` 或并入 `maintenance-milp-modeler/`），backend 与 llm-first 都 import 它、互不 import → 单一事实源 + 两个独立产品；代价是动 backend 自己的 import。
  - **C. 维持 sys.path 复用**（现状）：适合「并排对比」阶段，但不是「独立产品」。

- **决策备注**：这是 #2 的延续且更严重——不止「规则重复」，而是「两个产品 / 两套理解层 / 两套路由词表并存，且共享执行侧被 backend 独占」。切换前必须先定：执行侧怎么共享（复制 / 中立共享包 / 维持）。

- **已做（2026-11-01，方案 B）**：抽出 `dsl_core/` 中立共享包（registry、validator、compiler、generator、runner、planning_bridge、objective_catalog、part_catalog、planning_request）；`llm_parser.py` 留 backend（规则侧理解层）；`PlanningRequest`/`json_key`/`part_catalog` 抽进 dsl_core；两产品 import 全部改指 dsl_core，互不 import。验证：backend pytest 373 过、llm-first pytest 168 过。
- **未做**：~~产线从 rule-first 切到 llm-first~~（2026-11-02 已做，见下）。剩 rulebench 的规则侧对照 import 仍在 llm-first 内（README「遗留」），那是对比工具，不影响产线。

- **已做（2026-11-02·切换）**：
  - `agent_runtime.py` 新增 `default_engine()` / `resolve_engine()` 与 `ENGINE_ENV_VAR = "AGENT_ENGINE"`，
    请求没带 engine 时用产线默认 **llm**；带了以带的为准（前端切换控件照旧生效）。
  - `agent.py:30` 的 `engine` 默认从硬编码 `"rule"` 改成 `Field(default_factory=default_engine)`——
    服务端默认值跟着产线走，不再是「前端默认 llm、直接调 API 的走 rule」两套口径。
  - **回滚不用改代码**：设环境变量 `AGENT_ENGINE=rule` 重启进程即可回到规则主导。
  - **为什么敢默认切**：`_llm_first_parse()` 在没配 key、llmfirst 没接好、调用异常时都返回 `None`，
    上层拿到 `None` 就走原来的对话路径（`:1153-1155` 有 None 判断），最坏是慢一点，不是答错或崩。
    测试 `backend/tests/test_agent_engine.py::test_llm_engine_falls_back_when_llmfirst_is_unavailable` 盯着这个约定。
  - **仍未对齐**：llm-first 的八路路由词表与产线六路词表（见「问题来源」）本身没做合并——
    现在切过去后，`_llm_first_parse` 只认 `dsl / not_change / reject / clarify` 四种，其余一律判 clarify。
    这是切换后的下一个观察点，不在本条修复范围。

### #4 「排除（不安排）」约束在 DSL→模型链路中断

- **状态**：已修复（workbuddy 主修 + 我验证：`exclude` 走 `constraints_builder` 编出 `=0`，并切 `partial` 模式避免与完整模式 `sum(x)==1` 冲突；`test_exclude_partial.py` 3 过，真求解验证「exclude 009-011 → 恰好排 001-008」）
- **严重度**：高（用户的核心指令被静默丢弃，产出与意图不符的方案，且看起来「合规」）
- **位置**：
  - `dsl_core/planning_bridge.py:168-180` — `dsl_to_request` 把 `must` 映射到 `limits["必修任务"]`，但 `exclude` 落入 `else: residual.append(spec)`，未映射成可执行约束
  - `dsl_core/compiler.py:164` — 编译器**已支持** `exclude` → `model.Add(sum(variables)==0)`，但这条路没接到求解链路
  - `maintenance-milp-modeler/scripts/product_planner.py:588` — 求解器只「感知」exclude 用于切换部分排程模式，不把被排除任务加 `=0`
- **问题来源**：现场排程对话（2026-11-02）——用户「只需要修前 8 个，其余 009–013 不安排」，产出却 11/13（009/010/011 被排入）；agent 两轮自查定位到「模型里只有 8 条必做约束，没有任何一条把 009–013 锁在 0」。

- **现象**：「只修前 8 个」→ 生成了 8 条必做（=1，任务 001–008），但「其余不安排」没有生成 exclude（=0）；求解器在「成本最低优先 + 完成数次级优化」下把 009、010、011 一并排入 → 11/13。

- **根因**：`must` 有专属映射（`必修任务`），`exclude` 没有对应映射，归入 `residual` 后，这份 residual DSL 在求解时没有把 `=0` 约束编进模型。改措辞重跑没有用——不是措辞问题，是编译链路缺一条「排除」的映射 + 求解消费。

- **建议方向**：镜像 `must → 必修任务` 的路子——`planning_bridge` 加 `exclude → limits["排除任务"]` 映射，求解器据此对每个被排除任务加 `sum(x)==0`；并在 `format_dsl_preview` 与求解结果之间加对账（预览里的排除项，模型里必须有对应 =0）。

- **决策备注**：llm-first 的 README「八个真实缺口」其实登记过「exclude 是『这次不修』」，但那是 LLM 主导版的能力盘点，没回落到 rule-first 产线的编译链路。这条是 rule-first 产线的实际事故，优先级应高于能力盘点。

### #5 HEURISTIC_FALLBACK 误标解状态 FEASIBLE（「启发式兜底」与「CP-SAT FEASIBLE」自相矛盾）

- **状态**：已修复（workbuddy 删了 `selected=fallback` 起手 + `方案来源` 改判 `CP_SAT if selected else None`；我补了兜底分支 `解状态='FEASIBLE'` → `'UNKNOWN'`，消除来源与解状态的口径打架）
- **严重度**：中（不影响求解正确性，但状态表述自相矛盾，导致 agent 反复误报、自我纠正，用户无法判断真实状态）
- **位置**：
  - `maintenance-milp-modeler/scripts/product_planner.py:605-614` — 兜底分支写 `info['方案来源']='HEURISTIC_FALLBACK'` **同时** `info['解状态']='FEASIBLE'`（:610）
  - `backend/app/core/agent_runtime.py:2001` — `_format_product_reply` 按 `方案来源==HEURISTIC_FALLBACK` 输出「启发式基线兜底」，与 job 状态里显示的 FEASIBLE 对不上
- **问题来源**：同一段对话——agent 先说「启发式基线兜底」，后又纠正「CP-SAT 返回 FEASIBLE、独立复核通过、不是启发式兜底」；schedule card 显示「启发式兜底方案 · 未证明最优」。

- **现象**：同一个求解结果，两处口径打架：`方案来源=HEURISTIC_FALLBACK` 触发「启发式兜底」文案，`解状态=FEASIBLE` 触发「CP-SAT 有可行解」表述。

- **根因**：兜底分支（完整+部分排程都没在时限内找到可行解时）把启发式基线标成 `解状态=FEASIBLE`，复用了四态（OPTIMAL/FEASIBLE/UNKNOWN/INFEASIBLE）里 FEASIBLE 的语义——但 FEASIBLE 意味着「CP-SAT 找到的可行解」，启发式基线不是 CP-SAT 解，两者不能共用一个值。代码注释（:433-438）自述修过这个坑（「解状态 FEASIBLE、来源 HEURISTIC_FALLBACK… 永远降不了级」），但 :610 仍保留 `解状态='FEASIBLE'`，等于没修干净。

- **建议方向**：HEURISTIC_FALLBACK 的解状态不得复用 FEASIBLE，改为 UNKNOWN（或新增独立值），并在 `_format_product_reply` 与 job 状态展示两处统一口径；加一条测试断言「来源=HEURISTIC_FALLBACK 时解状态 ≠ FEASIBLE」。

### #6 独立复核不验证「约束是否符合用户意图」

- **状态**：已验证（2026-11-02 核对：两道门都已落地并会拦，清单此前漏标）
- **严重度**：高（复核通过 ≠ 正确：复核的是自洽但错误的模型，给用户「已复核」的假保障）
- **位置**：复核环节（`verified` 标记 + 独立复核流程）只验证约束自洽/模型可解，不对照用户原话验证意图是否实现
- **问题来源**：同一段对话——「独立复核通过」，但复核的是 13 条必做（错误模型），而非用户要的 8 必做 + 5 排除；agent 自己点破「复核的是错的模型」。

- **现象**：`scheduled=11/13`、约束全是 13 条 `=1`，与用户「只修 8 个、排除 009–013」完全不符，但复核照样通过。

- **根因**：复核只做「数学自洽」（约束无矛盾、能解出解），不做「意图对账」（用户点名的必做/排除任务，与模型里的 =1/=0 约束逐项是否一一对应）。#4 的 exclude 丢了，复核察觉不到，因为复核根本不知道用户说过「排除」。

- **建议方向**：在复核里加一道「意图↔约束」对账——把用户原话里点名的任务（必做/排除）与模型约束逐项核对；对不上的判复核不通过，而不是只看能解。可与 #4 的验收标准合并成同一条判据。

- **决策备注**：这条与 #4 是因果链：#4 让 exclude 丢进模型，本可由 #6 拦住但没拦。修复顺序上，先补 #4（让 exclude 能编译），再补 #6（让复核能拦住下次类似丢失）。

- **核对结论（2026-11-02）：两道门都已落地，清单此前标记为「待修改」是漏标。**
  1. **原话 ↔ DSL**（入口侧）：`maintenance_product_executor.py:101-110` 调 `utterance.audit_utterance()`，
     有违例直接抛「原话核对未通过，这份变更不是你要的那件事」。预览侧 `agent_tools.py:1633-1712`
     把对账结果渲进变更预览，用户批准前就能看见。
  2. **DSL ↔ 产物**（出口侧）：`product_planner.py:707-713` 在**写盘之前**跑 `reconcile()`，
     结果写进 `report['求解']['意图对账']`，进而进 `结果.json` 和版本摘要；
     `dsl_core/runner.py:226-232` 有违例直接 `DslError`；`maintenance_product_executor.py:194-202`
     发现「违例或未核对」就拒绝出方案（启发式兜底除外，它有专门的界面标注）。
     缓存复用也受这道门管：`:148-159` 对账脏的旧结果不拿来顶。
  3. **「未核对不算通过」**：`reconcile._summary()`（`:599-608`）与 `utterance.audit_utterance()`
     都是 `not violations and not unverified`——没有证据证明对得上，就不是通过。
- **验收证据**：`backend/tests/dsl/test_reconcile.py`、`test_utterance.py`、`test_exclude_partial.py`、
  `test_end_to_end.py` 共 **55 passed / 13 deselected**；其中 `test_exclude_partial.py` 是真求解出来的方案
  也要通过意图对账。
- **注意**：这两道门管的是「点名的任务/参数对不对得上」，管不了模型一句没数字的结论性表述。
  那条归 #1 的数字溯源和上面第 1 道门的原话断言，三条合起来才覆盖完整。
