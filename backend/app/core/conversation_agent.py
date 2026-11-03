from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from anthropic import Anthropic

from app.core.agent_tools import AgentToolbox
from app.core.demo_access import resolve_api_key
from app.core.conversation_memory import (
    RECENT_MESSAGES,
    SUMMARY_MAX_CHARS,
    ConversationMemory,
    render_memory_context,
)
from app.schemas.agent import Message, WorkingMemory, WindowVersion


SHANGHAI_TZ = timezone(timedelta(hours=8))

TOOL_LABELS = {
    "read_versions": "读取方案事实",
    "read_environment": "读取风况与电价",
    "read_catalog": "读取目标目录",
    "read_alarms": "读取告警清单",
    "read_site": "读取现场数据",
    "solve": "准备排程变更",
    "submit_change": "提交排程变更",
}

# 下面两个是**起手提示**，不是判断：它们只决定第 0 轮要不要把 tool_choice
# 从 auto 顶成 any（逼模型先去读工具，而不是凭记忆编数字）。
#
# 为什么不是判断——一句话像不像在问数据，靠词表是判不准的：会漏（换个说法就
# 不命中）、会误伤（问概念却被拉去调工具）、会腐烂（业务词一扩张词表就追不上）。
# 所以调不调工具最终由模型 + 工具说明决定；漏掉的那一半由 answer_grounding
# 在**输出端**兜住：答案里的数字追不到工具结果就当场标出来。
DATA_HINT = re.compile(
    r"方案|版本|模型|参数|系数|约束|目标函数|公式|成本|费用|任务|窗口|船天|"
    r"对比|比较|差异|区别|最优|可行|状态|进度"
)

OPS_HINT = re.compile(
    r"告警|报警|缺陷|库存|备件|风机台账|部件|海事窗口|检修单|工单|"
    r"执行反馈|船租|人工单价|系统参数|风速|电价|海况"
)

MATH_PASSTHROUGH = re.compile(
    r"数学模型|模型公式|模型结构|数学表达|公式|LaTeX|写出来|贴出来|导出模型"
)

# Tool markdown that must be copied verbatim: formulas, cost tables, the
# environment table, and the change contract (solve/submit_change).  Alarm/site
# tables are exact too, but the agent may add analysis around them, so they are
# not forced through this path.
PASSTHROUGH_TOOLS = {"read_versions", "read_environment", "solve", "submit_change"}

# 这几个工具的结果是"契约"：用户据此决定批不批、要不要等求解。
# 让模型自由转述，就会出现"预览说排除了 5 个、模型里一条排除都没有"——
# 用户批准的是模型的话，执行的是另一份 DSL，中间没有任何绑定。
# 所以它们必须独占输出：连模型这一轮已经说出来的话也丢掉，只留系统那份。
AUTHORITATIVE_TOOLS = {"solve", "submit_change"}


class ConversationAgentError(RuntimeError):
    """Raised when the conversational agent cannot complete a turn."""


class ConversationAgent:
    """A deliberately small tool-calling loop: plan tools, read facts, answer."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        max_rounds: int = 4,
    ) -> None:
        self.api_key = api_key or os.environ.get("MINIMAX_API_KEY")
        self.model = model or os.environ.get("MINIMAX_MODEL", "deepseek-flash")
        self.base_url = (
            base_url
            or os.environ.get("MINIMAX_BASE_URL", "https://api.deepseek.com/anthropic")
        )
        self.max_rounds = max_rounds

    @property
    def enabled(self) -> bool:
        return bool(resolve_api_key(self.api_key))

    def answer(
        self,
        *,
        message: str,
        history: list[Message],
        memory: WorkingMemory,
        toolbox: AgentToolbox,
        selected_version: WindowVersion | None,
        snapshot: ConversationMemory | None = None,
        on_token: Callable[[str], None] | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> str:
        if not self.enabled:
            raise ConversationAgentError("MINIMAX_API_KEY is not configured")

        # 本机代理环境里的 ANTHROPIC_AUTH_TOKEN 会劫持 api_key（SDK 在
        # auth_token=None 时读环境变量且优先发 Bearer 头）：构造传空串跳过
        # 环境读取，构造后置回 None 让 SDK 走 x-api-key 头。
        client = Anthropic(
            api_key=resolve_api_key(self.api_key),
            auth_token="",
            base_url=self.base_url,
            timeout=120.0,
        )
        client.auth_token = None
        messages = self._build_messages(message, history)
        system = self._system_prompt(memory, toolbox, selected_version, snapshot)
        tools = toolbox.tool_specs()
        collected: list[str] = []
        # 必须原样输出的内容（公式、参数表、变更契约）。攒着，等模型把这一轮
        # 说完再拼上去——拿到就 return 会把同一句话里还没做完的事一起丢掉。
        # 第二项标记它是不是"契约"（见 AUTHORITATIVE_TOOLS）。
        pending_verbatim: list[tuple[str, bool]] = []
        tool_used = False
        # 数字溯源的依据：本轮所有工具返回的原始内容，外加用户原话。
        # 用户自己报的数（「船租压到 8 万」）不算模型编的，所以原话也算出处。
        sources: list[str] = [message]

        def _grounded(text: str) -> str:
            """答案里的数字追不到工具结果时，当场标出来——不替模型背书。"""
            from app.core.answer_grounding import caveat_line, grounding_report

            note = caveat_line(grounding_report(text, sources))
            if not note:
                return text
            if on_token:
                on_token("\n\n" + note)
            return f"{text}\n\n{note}" if text.strip() else note

        def _finish() -> str:
            if any(authoritative for _, authoritative in pending_verbatim):
                # 有契约类结果时只交契约：模型的话是转述，可能已经失真，
                # 两份摆在一起用户没法判断哪份算数。
                return "\n\n".join(block for block, _ in pending_verbatim)
            text = "".join(collected).strip()
            for block, _ in pending_verbatim:
                text = f"{text}\n\n{block}" if text else block
            return text

        for round_index in range(self.max_rounds):
            # 思考型模型不支持 tool_choice=any，统一用 auto，让模型自己决定是否调工具。
            force_tool = False
            try:
                with client.messages.stream(
                    model=self.model,
                    max_tokens=3000,
                    temperature=0.2,
                    system=system,
                    tools=tools,
                    tool_choice={"type": "any"} if force_tool else {"type": "auto"},
                    messages=messages,
                ) as stream:
                    for text in stream.text_stream:
                        if text:
                            collected.append(text)
                            if on_token:
                                on_token(text)
                    final = stream.get_final_message()
            except Exception as exc:  # pragma: no cover - network boundary
                raise ConversationAgentError(str(exc)) from exc

            tool_calls = [
                block for block in final.content if getattr(block, "type", "") == "tool_use"
            ]
            if not tool_calls:
                text = _finish()
                if not text:
                    raise ConversationAgentError("模型没有返回文本")
                return _grounded(text)

            tool_used = True
            assistant_blocks = [
                block
                for block in final.content
                if getattr(block, "type", "") in {"text", "tool_use"}
            ]
            messages.append({"role": "assistant", "content": assistant_blocks})
            results = []
            verbatim_blocks: list[tuple[str, bool]] = []
            for block in tool_calls:
                label = TOOL_LABELS.get(block.name, block.name)
                if on_status:
                    on_status(f"{label}…")
                result = toolbox.execute(block.name, dict(block.input or {}))
                data = result.get("data")
                if (
                    block.name in PASSTHROUGH_TOOLS
                    # 只读工具成功才透传；契约类工具本来就不是 "ok"
                    # （solve 给 needs_confirmation，submit_change 给 submitted），
                    # 它们只要带 markdown 就得原样送到用户面前。
                    and (
                        result.get("status") == "ok"
                        or block.name in AUTHORITATIVE_TOOLS
                    )
                    and isinstance(data, dict)
                    and isinstance(data.get("markdown"), str)
                    and data["markdown"].strip()
                    and (data.get("verbatim") is True or MATH_PASSTHROUGH.search(message))
                ):
                    verbatim_blocks.append(
                        (data["markdown"], block.name in AUTHORITATIVE_TOOLS)
                    )
                payload = json.dumps(result, ensure_ascii=False, default=str)
                sources.append(payload)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": payload,
                    }
                )
            if verbatim_blocks and all(b.name in PASSTHROUGH_TOOLS for b in tool_calls):
                # 这一轮**只**在取原样内容（公式、参数表、环境表、变更契约）。
                # 用户问的就是这个，再让模型转一轮只会拖慢并增加改写公式的风险，
                # 所以直接交卷——这是快路径，也是绝大多数情况。
                # 一次调多个工具时逐块保留，不能只留最后一块（会把公式挤掉）。
                body = "\n\n".join(text for text, _ in verbatim_blocks)
                authoritative = any(auth for _, auth in verbatim_blocks)
                # 契约类结果独占输出：模型这轮说的话是它的转述，不是系统契约。
                prefix = "" if authoritative else "".join(collected).strip()
                return _grounded(f"{prefix}\n\n{body}" if prefix else body)
            if verbatim_blocks:
                # 这一轮除了取原样内容，**还夹着别的诉求**（比如"贴出这版公式，
                # 再把船租压到8万"）。原来这里也是直接 return，于是后半句当场
                # 消失，而且没有任何提示——用户以为系统听懂了，只是没做。
                # 现在存起来，把 tool_result 回喂，等模型把这一轮说完再拼上去。
                pending_verbatim.append((
                    "\n\n".join(text for text, _ in verbatim_blocks),
                    any(auth for _, auth in verbatim_blocks),
                ))
            if collected and not collected[-1].endswith("\n\n"):
                collected.append("\n\n")
                if on_token:
                    on_token("\n\n")
            messages.append({"role": "user", "content": results})

        return _grounded(_finish())

    def summarize(self, messages: list[Message]) -> str | None:
        """用 LLM 把旧消息压缩成自由文本摘要；未启用/失败返回 None，绝不抛。"""
        if not self.enabled or len(messages) <= RECENT_MESSAGES:
            return None
        old = messages[:-RECENT_MESSAGES]
        text = "\n".join(f"{item.role}: {item.content[:200]}" for item in old)
        try:
            client = Anthropic(
                api_key=resolve_api_key(self.api_key),
                auth_token="",
                base_url=self.base_url,
                timeout=30.0,
            )
            client.auth_token = None
            response = client.messages.create(
                model=self.model,
                max_tokens=400,
                temperature=0.0,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "用 2-3 句话总结这段对话里已确认的约束、已否决的方案、待办和关键结论；"
                            "不要编造数字，不确定就不写：\n" + text
                        ),
                    }
                ],
            )
            content = "".join(
                block.text
                for block in response.content
                if getattr(block, "type", "") == "text"
            ).strip()
            return content[:SUMMARY_MAX_CHARS] or None
        except Exception:  # pragma: no cover - LLM 摘要失败不阻断
            return None

    @staticmethod
    def _build_messages(message: str, history: list[Message]) -> list[dict[str, Any]]:
        recent = history[-8:]
        if recent and recent[-1].role == "user" and recent[-1].content == message:
            recent = recent[:-1]
        messages: list[dict[str, Any]] = []
        for item in recent:
            if not item.content.strip():
                continue
            messages.append({"role": item.role, "content": item.content})
        messages.append({"role": "user", "content": message})
        return messages

    def _system_prompt(
        self,
        memory: WorkingMemory,
        toolbox: AgentToolbox,
        selected_version: WindowVersion | None,
        snapshot: ConversationMemory | None = None,
    ) -> str:
        plans = []
        for index, item in enumerate(toolbox.ordered, start=1):
            summary = item.resultSummary or {}
            plans.append(
                f"方案{index:02d} {item.id} | {summary.get('strategy') or item.focusTag} | "
                f"{summary.get('scheduledTaskCount')}/{summary.get('totalTaskCount')} 项 | "
                f"{_wan(summary.get('totalCost'))} 万元 | 状态 {_status(item)}"
            )
        selected = "无"
        if selected_version:
            selected = (
                f"方案{toolbox.plan_numbers.get(selected_version.id, 1):02d} "
                f"（{selected_version.id}）"
            )
        plan_block = "\n".join(plans) if plans else "本会话还没有方案。"
        catalog_block = _slot_catalog()
        memory_block = render_memory_context(snapshot) if snapshot else ""
        return f"""你是海上风电检修排程产品的对话 Agent。你正在和生产环境中的检修计划员对话。

当前时间：{datetime.now(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M")}（Asia/Shanghai）
当前选中方案：{selected}
会话最新方案：{memory.latestResultId or "无"}

本会话方案：
{plan_block}

本会话背景记忆（只读背景，具体数字仍以工具为准）：
{memory_block}

能改的旋钮（形状冻结，只填数；唯一来源是小一修槽位注册表）：
{catalog_block}

产品内已有的现场数据（都有，不要跟用户说“没有数据源”）：
告警与待修任务、风机台账与部件、海事窗口、备件库存与收发计划、检修单与出海清单、
执行反馈、船租人工等系统参数、风况电价海况时间序列、方案事实与目标目录。
哪个数据在哪、怎么筛，以每个工具的说明为准，这里不重复一遍。
两条不能错的口径：
告警是计划快照，不是实时 SCADA 推送，必须带数据时点和性质，不能把未来批次说成当前告警；
检修单、出海清单、执行反馈为空时如实说“当前没有”，不要说“没有这个数据域”。

本产品没有接入（被问到时直接说明，不要用通用经验补造现场状态）：
- 实时 SCADA、设备在线/离线、振动温度等测点；
- 船舶 AIS、船员排班、船舶实时可用性；
- 采购在途实时状态、供应商交期；
- 外部审批系统、财务实账、发票。
能替代回答的现有数据是：风机台账、已确认的库存收发计划、窗口审批状态、船租与人工参数、
检修单和执行反馈（当前可能为空）。

产品事实（可以直接回答，不需要先有方案）：
- 求解器是 Google OR-Tools CP-SAT，解的是有界 0-1 整数调度模型，目标与约束都是线性的。
- 求解器参数固定：单工作线程、随机种子 17、时限计入模型指纹；同一份契约同一时限应得到同一结论。
- 流程是：自然语言 → 固定的模型契约 → 真实求解 → 独立复核 → 版本入库。
- 求解任务是并发、互不覆盖的：每次「确认生成沙盘」都新建一个独立任务，各自写入独立版本；
  一次可以同时运行多个求解任务，新任务不会替代或挤掉仍在运行的任务。
- 求解状态四态：OPTIMAL 已证明最优、FEASIBLE 有可行解但未证明最优、
  UNKNOWN 时限内没有结论（不等于无解）、INFEASIBLE 已证明不可行。
- 成本口径：总经济成本 = 船租 + 人工 + 备件 + 其他现场费用 + 发电损失；
  现金支出 = 总经济成本 − 发电损失（发电损失是机会成本，不是当期支出）。
- 每个方案都可以回看它的模型产物、参数来源和审计证据。

工作方式：
1. 九个工具：只读六个（read_versions 方案事实、read_environment 风况电价海况、
   read_catalog 旋钮目录、read_alarms 告警待修、read_site 现场数据、
   read_jobs 求解任务进度）；改排程两个（solve 出预览、submit_change 提交）；
   写盘一个（ledger 检修单 / 出海清单 / 审批 / 现场回填）。
   **什么时候用哪个、参数怎么传，以每个工具的说明为准**——那是唯一权威，这里不重复。
2. 跨工具的顺序不能乱（这条工具说明装不下，所以留在这里）：
   要改排程 → solve 出预览（把话翻译成上面清单里的旋钮）→ 等用户确认 → submit_change 提交；
   没有 solve 过的预览不能提交。
   ledger 的下发 / 审批 / 回填同理，先读后写。
   你不能声称已求解、已生成方案、已修改数据或已导出文件。
3. 数字必须来自工具：工具没返回前不要断言“没有数据”，也不要凭记忆或历史消息补造、重算。
   返回 not_available / not_found 时说清缺的是哪个域、哪次运行缺、可选补救，
   不要先说“我去导出”，也不要事后重建冒充原模型。
4. 输出用 Markdown，简洁可扫读；公式用 LaTeX；普通回答控制在 2 到 6 句。
   不要把工具 JSON、字段名或内部变量名倒给用户，要转成中文业务词、列表或表格。
   公式、参数表、环境表、变更契约**原样输出**，不得改写公式、变量、系数或约束。
5. 普通聊天、解释概念、询问算法原理，直接回答，不必调用工具。
   能力类问题（有哪些旋钮、strategy 四选一是哪四个）
   按上面的目录回答，不调用工具，也不需要先有方案；目录里没有的旋钮一律不能说"支持"。
   上面“产品事实”那几条是产品级常识，可以直接讲，不要因为“本会话还没有方案”就拒答；
   只有具体某次运行的成本、系数、耗时、任务安排才必须读方案。
6. 对比：工具会给同一张指标矩阵、相对基准的差值和可比性限制，你只做解读——
   先给结论，再给 2 到 4 条关键差异，不要自己做减法。
   只有 comparability.savingsClaimAllowed 为 true 才能说“节省”；
   costCertainty 是 inconclusive / unverified 时必须明说不能据此判定谁更省。
7. 用户问模型结构/目标怎么设时，讲旋钮形式（strategy 是四选一的整体排程风格），
   并说明具体取值要读某一版的方案事实；不要把旋钮当成某次求解的实际取值。
8. 风况、电价、海况这类时间序列：原样输出工具返回的 Markdown 或图表协议，不要自己重排数字。
   聊天框能渲染 Markdown 表格，不能渲染真正的图表；用户要“可视化”时先给表，
   并把关键峰值、低谷、窗口位置说清楚；不要因为画不了图就拒答，也不要把表格说成图。
"""


def _slot_catalog() -> str:
    """小一修能改的旋钮清单，从 yxcore 注册表反向生成（唯一事实来源）。"""
    try:
        from yxcore import slots as S
        from yxcore.prompt import _boundary_text, _disambiguation, _slot_lines

        head = (
            "strategy 四选一：" + "、".join(S.STRATEGY_NAMES)
        )
        return "\n\n".join([head, _slot_lines(), _disambiguation(), _boundary_text()])
    except Exception:  # pragma: no cover
        return "（旋钮注册表暂不可读）"


def _wan(value: Any) -> str:
    try:
        return f"{float(value) / 10_000:.2f}"
    except (TypeError, ValueError):
        return "未记录"


def _status(version: WindowVersion) -> str:
    solver = (version.resultSummary or {}).get("solver")
    if isinstance(solver, dict):
        return str(
            solver.get("解状态")
            or solver.get("求解状态")
            or solver.get("状态")
            or "UNKNOWN"
        )
    return "UNKNOWN"
