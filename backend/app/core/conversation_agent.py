from __future__ import annotations

import json
import os
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
    "read_jobs": "查看求解进展",
    "solve": "准备排程变更",
    "submit_change": "提交排程变更",
    "ledger": "查看检修单台账",
}

# 过程面板里解释「这个工具是干什么的」。只写给用户可以核对的一句话，
# 不写内部实现；工具名另由 tool 字段单独传，前端排在标签旁边。
TOOL_PURPOSE = {
    "read_versions": "看已经排过的方案里有哪些事实和数字",
    "read_environment": "查风况、电价与海事窗口",
    "read_catalog": "查目标、权重和约束的目录",
    "read_alarms": "查台账里还没结案的告警",
    "read_site": "查现场风机、部件、库存与窗口",
    "read_jobs": "看求解任务跑到哪一步",
    "solve": "把这句话翻译成排程改动，先出预览",
    "submit_change": "确认变更并启动真实求解",
    "ledger": "查检修单台账与送审状态",
}

STATUS_LABELS = {
    "ok": "已读到数据",
    "needs_confirmation": "已生成待确认的变更预览",
    "clarify": "这一步需要你澄清",
    "not_available": "数据没取到",
    "error": "这一步失败了",
}


def _tool_result_brief(result: Any, toolbox: AgentToolbox) -> str:
    """把工具返回压成一句人能核实的摘要，不编内容、不猜字段含义。"""
    status = result.get("status") if isinstance(result, dict) else None
    bits = [STATUS_LABELS.get(str(status), f"状态 {status or '未知'}")]
    data = result.get("data") if isinstance(result, dict) else None
    count: int | None = None
    if isinstance(data, dict):
        # 只认语义明确的数量字段。以前随便挑 data 里第一个 list 数长度，
        # 会把「窗口列表 3 项」显示成「读到 3 条」，让人以为现场只有 3 条。
        counts = data.get("counts")
        if isinstance(counts, dict) and isinstance(counts.get("total"), (int, float)):
            count = int(counts["total"])
        else:
            for key in ("总数", "任务数", "告警数", "方案数", "条数", "count", "total"):
                value = data.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    count = int(value)
                    break
    elif isinstance(data, list):
        # 整个返回就是一份清单，长度才是可信的。
        count = len(data)
    if count is not None:
        bits.append(f"共 {count} 条")
    try:
        trace = toolbox.trace()
        if trace:
            bits.append(f"{round(float(trace[-1].get('latencyMs', 0)))} ms")
    except Exception:  # noqa: BLE001 - 耗时只是装饰，缺了不影响过程展示
        pass
    return " · ".join(bits)

# 「哪些字不许改写」和「本轮只交哪一份」由工具自己在返回里声明：
# data["verbatim"]=True  → 这段 markdown 由系统原样附在回答末尾；
# data["exclusive"]=True → 本轮只交系统这份，模型的转述不算数。
# 循环既不按工具名猜，也不按用户话里的关键词猜：判断绑在数据流上，
# 而不是绑在某句具体的话上。


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
        on_stage: Callable[[dict[str, Any]], None] | None = None,
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
        # 工具声明要原样附上的内容，攒到最后统一拼。不提前 return：同一句话
        # 里还夹着别的诉求时，拿到第一段就交卷会把后半句丢掉。
        # 第二项标记「独占输出」——变更契约不许和模型转述摆在一起。
        pending_verbatim: list[tuple[str, bool]] = []
        # 数字溯源的依据：本轮所有工具返回的原始内容，外加用户原话。
        # 用户自己报的数（「船租压到 8 万」）不算模型编的，所以原话也算出处。
        sources: list[str] = [message]

        def _stage(*, stage_id: str, label: str, status: str,
                   detail: str | None = None, tool: str | None = None) -> None:
            """一条结构化过程事件。同一个 id 的 running→done 会被前端就地替换。

            过程面板要回答三件事：现在到哪个环节、调用了哪个工具、那一步
            拿回了什么。所以事件必须跟着真实循环发，不能事后按文案补。
            """
            if not on_stage:
                return
            payload: dict[str, Any] = {
                "stage_id": stage_id,
                "label": label,
                "status": status,
            }
            if detail:
                payload["detail"] = detail
            if tool:
                payload["tool"] = tool
            on_stage(payload)

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
            # 同一份原样内容可能被多轮工具调用重复返回，只交一次。
            exclusive = [block for block, flag in pending_verbatim if flag]
            if exclusive:
                # 有契约类结果时只交契约：模型的话是转述，可能已经失真，
                # 两份摆在一起用户没法判断哪份算数。
                return "\n\n".join(dict.fromkeys(exclusive))
            text = "".join(collected).strip()
            for block in dict.fromkeys(block for block, _ in pending_verbatim):
                text = f"{text}\n\n{block}" if text else block
            return text

        for round_index in range(self.max_rounds):
            # 思考型模型不支持 tool_choice=any，统一用 auto，让模型自己决定是否调工具。
            think_id = f"think.{round_index}"
            think_label = (
                "理解这句话，决定下一步" if round_index == 0
                else "结合读到的事实继续推理"
            )
            _stage(
                stage_id=think_id, label=think_label, status="running",
                detail="模型在判断要不要查数据、查哪份数据",
            )
            try:
                with client.messages.stream(
                    model=self.model,
                    max_tokens=3000,
                    temperature=0.2,
                    system=system,
                    tools=tools,
                    tool_choice={"type": "auto"},
                    messages=messages,
                ) as stream:
                    for text in stream.text_stream:
                        if text:
                            collected.append(text)
                            if on_token:
                                on_token(text)
                    final = stream.get_final_message()
            except Exception as exc:  # pragma: no cover - network boundary
                _stage(
                    stage_id=think_id, label=think_label, status="error",
                    detail="这一轮没拿到模型回复",
                )
                raise ConversationAgentError(str(exc)) from exc

            tool_calls = [
                block for block in final.content if getattr(block, "type", "") == "tool_use"
            ]
            if not tool_calls:
                _stage(
                    stage_id=think_id, label=think_label, status="done",
                    detail="不再调工具，直接给回答",
                )
                _stage(
                    stage_id="answer", label="把结果整理成回答", status="running",
                    detail="把工具读到的数字和模型的说明合成一段回答",
                )
                text = _finish()
                if not text:
                    raise ConversationAgentError("模型没有返回文本")
                answer = _grounded(text)
                _stage(
                    stage_id="answer", label="把结果整理成回答", status="done",
                    detail="回答已整理好",
                )
                return answer

            _stage(
                stage_id=think_id, label=think_label, status="done",
                detail="决定调用 "
                + "、".join(TOOL_LABELS.get(b.name, b.name) for b in tool_calls),
            )

            assistant_blocks = [
                block
                for block in final.content
                if getattr(block, "type", "") in {"text", "tool_use"}
            ]
            messages.append({"role": "assistant", "content": assistant_blocks})
            results = []
            for call_index, block in enumerate(tool_calls):
                name = block.name
                label = TOOL_LABELS.get(name, name)
                tool_id = f"tool.{round_index}.{call_index}"
                _stage(
                    stage_id=tool_id, label=f"调用工具 · {label}", status="running",
                    tool=name,
                    detail=TOOL_PURPOSE.get(name, "读取或变更这一步需要的数据"),
                )
                if on_status:
                    on_status(f"{label}…")
                result = toolbox.execute(name, dict(block.input or {}))
                _stage(
                    stage_id=tool_id, label=f"调用工具 · {label}", status="done",
                    tool=name, detail=_tool_result_brief(result, toolbox),
                )
                data = result.get("data")
                verbatim = (
                    isinstance(data, dict)
                    and data.get("verbatim") is True
                    and isinstance(data.get("markdown"), str)
                    and data["markdown"].strip()
                )
                # 数字溯源要拿完整原文（含表格里的数），先记下来再剥离。
                sources.append(json.dumps(result, ensure_ascii=False, default=str))
                if verbatim:
                    pending_verbatim.append(
                        (data["markdown"], data.get("exclusive") is True)
                    )
                    # 原样内容由系统在末尾附一次，回喂模型时把 markdown 全文
                    # 摘掉：否则模型复述一遍、系统又附一遍，用户看到两遍，
                    # 且复述那份可能把公式系数改了。结构化字段照给。
                    result = {
                        **result,
                        "data": {k: v for k, v in data.items() if k != "markdown"},
                    }
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, ensure_ascii=False, default=str),
                    }
                )
            # 工具结果一律回喂模型，本轮不提前交卷。原样内容由系统在末尾附上，
            # 所以一句话里既有公式又要改排程时，后半句不会被快路径吞掉。
            if collected and not collected[-1].endswith("\n\n"):
                collected.append("\n\n")
                if on_token:
                    on_token("\n\n")
            messages.append({"role": "user", "content": results})

        # 四轮用完仍未交卷：也要把「整理回答」这一步标完，别把过程停在半路。
        _stage(
            stage_id="answer", label="把结果整理成回答", status="running",
            detail="把工具读到的数字和模型的说明合成一段回答",
        )
        answer = _grounded(_finish())
        _stage(
            stage_id="answer", label="把结果整理成回答", status="done",
            detail="回答已整理好",
        )
        return answer

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
- 求解器是 Google OR-Tools CP-SAT，解的是有界 0-1 整数调度模型。目标固定为**一段
  标准化加权目标**（具体权重见方案详情「数学模型」页），约束是线性的；用户能改的是
  权重和业务约束，不能改目标形状，也不存在「分层求解」。
- 求解器默认多线程并行搜索（可设 MWA_SOLVER_WORKERS=1 切单线程归档），时限计入模型指纹；
  正常证到最优的结论可复现，时间被截断时不同次运行可能取到不同中间解，
  此时解状态会如实写「未证明最优」，不要说成「一定一样」。
- 流程是：自然语言 → 固定的模型契约 → 真实求解 → 独立复核 → 版本入库。
- 求解任务是并发、互不覆盖的：每次「确认生成沙盘」都新建一个独立任务，各自写入独立版本；
  一次可以同时运行多个求解任务，新任务不会替代或挤掉仍在运行的任务。
- 求解状态四态：OPTIMAL 已证明最优、FEASIBLE 有可行解但未证明最优、
  UNKNOWN 时限内没有结论（不等于无解）、INFEASIBLE 已证明不可行。
- 成本口径：总经济成本 = 船租 + 人工 + 备件 + 其他现场费用 + 发电损失；
  现金支出 = 总经济成本 − 发电损失（发电损失是机会成本，不是当期支出）。
- 默认不要求全部任务都修：每项任务都有「本轮不修」分支，按固定展望期（默认 14 天）
  计一笔发电机会损失 P_i = 展望窗内预计发电收入 × (1 − 剩余出力系数 α_i)，
  α_i 来自部件库业务表、按部件×严重度取值；由目标函数权衡「修的成本」与「不修的损失」，
  资源不够时自然少修，不再用硬约束逼全修。已批准检修单锁定的任务不允许不修。
- 每个方案都可以回看它的模型产物、参数来源和审计证据；模型产物里逐任务记录了 P_i、
  α 出处和实际计价窗（末端超出逐时预测覆盖期时会诚实截断）。

工作方式：
1. 九个工具：只读六个（read_versions 方案事实、read_environment 风况电价海况、
   read_catalog 旋钮目录、read_alarms 告警待修、read_site 现场数据、
   read_jobs 求解任务进度）；改排程两个（solve 出预览、submit_change 提交）；
   写盘一个（ledger 检修单 / 出海清单 / 审批 / 现场回填）。
   **什么时候用哪个、参数怎么传，以每个工具的说明为准**——那是唯一权威，这里不重复。
2. 跨工具的顺序不能乱（这条工具说明装不下，所以留在这里）：
   要改排程 → solve 出预览（把话翻译成上面清单里的旋钮）→ 等用户确认 → submit_change 提交；
   没有 solve 过的预览不能提交。
   限定数量/范围这类**含糊的变更**（「只完成前五个」「从下下周开始」）也交给 solve：
   它会给出给用户点选的结构化澄清，你不要在聊天里自己问「你是指…吗」，
   也不要替用户挑一种含义直接往下走。
   ledger 的下发 / 审批 / 回填同理，先读后写。
   你不能声称已求解、已生成方案、已修改数据或已导出文件。
3. 数字必须来自工具：工具没返回前不要断言“没有数据”，也不要凭记忆或历史消息补造、重算。
   返回 not_available / not_found 时说清缺的是哪个域、哪次运行缺、可选补救，
   不要先说“我去导出”，也不要事后重建冒充原模型。
4. 输出用 Markdown，简洁可扫读；公式用 LaTeX；普通回答控制在 2 到 6 句。
   不要把工具 JSON、字段名或内部变量名倒给用户，要转成中文业务词、列表或表格。
   工具声明「原样输出」的公式/参数表/环境表由系统一字不改附在末尾，工具结果里
   不给你这些原文：你只补解读，不要复述、改写、重排，也不要凭记忆补数字。
   变更预览与提交契约由系统独占输出，你不要转述、不要另写一份承诺。
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
9. 海事窗口的起止时间是已批准的业务事实，不是可调旋钮，不能改。
   用户说「窗口03 缩短到 19 号结束」「窗口03 推迟两天开始」时：先明确说窗口事实不能改，
   再给出真正可办的说法——如果只是想让检修在 19 号前收尾，可以提「窗口03 的任务
   最晚 11 月 19 号完成」这类任务级结束时间要求；如果是想晚点开工，可以提「最早开工」
   要求。窗口记录本身仍是原来的区间。不要甩「旋钮注册表」「part_hours」这类内部术语，
   也不要拿「把某天新开工数压到 0」去近似用户的要求。
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
