from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from concurrent.futures import Future, ThreadPoolExecutor
from queue import Queue
from threading import Thread
from typing import Any
from uuid import uuid4

from app.core.memory_manager import MemoryManager
from app.core.agent_memory import (
    ConversationSummaryRepository,
    DecisionMemoryRepository,
    UserProfileRepository,
    build_summary,
)
from app.core.agent_trace import AgentTrace, AgentTraceRepository, ToolCallRecord, new_trace_id
from app.core.conversation_memory import (
    ConversationMemoryCache,
    ConversationMemoryRepository,
    build_memory_snapshot,
)
from app.core.demo_access import begin_llm_turn, end_llm_turn
from app.core.agent_tools import AgentToolbox, serialize_clarify_groups
from app.core.conversation_agent import ConversationAgent, ConversationAgentError
from app.core.yx_engine import YxEngine, _SOLVE_SECONDS
from app.core.model_query import (
    detect_model_question,
    render_capability_answer,
    render_missing_artifact,
    render_model_formula,
    render_parameter_answer,
    render_solution_answer,
    render_structure_answer,
    render_status_answer,
)
from app.core.operations_query import alarm_arguments, detect_operations_question
from app.core.result_normalizer import ResultNormalizer
from app.core.solve_jobs import SolveJobRepository
from app.core.version_manager import VersionManager
from app.core.version_compare import compare_versions, render_compare_summary
from app.core.version_reference import (
    chronological_versions,
    is_version_switch_request,
    resolve_version_reference,
)
from app.db.models import (
    CandidateRepository,
    ConversationRepository,
    MessageRepository,
    ModelArtifactRepository,
    VersionRepository,
)
from app.schemas.agent import AgentResponse, AssistantMessagePayload, ClarificationOptionPayload, DecisionArtifactPayload, DegradedPayload, Message, SolveEvidencePayload, SolveJobPayload
from app.schemas.dashboard import DashboardResponse


SHANGHAI_TZ = timezone(timedelta(hours=8))
SKILL_NAME = "maintenance-milp-modeler"

# --------------------------------------------------------------- 理解引擎
# llm = LLM 主导（llm-first），rule = 规则主导（旧路径）。
# 产线默认 llm：两套理解层并排跑了很久，规则侧该退了；前端默认也是 llm，
# 服务端跟着改，免得「前端发 llm、直接调 API 的走 rule」两套口径。
#
# 回滚不用改代码：设环境变量 AGENT_ENGINE=rule 即可（改完重启进程）。
# 之所以敢默认切过去：LLM 主导解析在没配 key、包没接好、调用异常时都会
# 自己返回 None 退回规则路径，不会把对话搞死——最坏情况是慢一点，不是答错。
ENGINE_LLM = "llm"
ENGINE_RULE = "rule"
ENGINE_ENV_VAR = "AGENT_ENGINE"


def default_engine() -> str:
    """产线默认引擎。环境变量 AGENT_ENGINE=rule 时回滚到规则主导。"""
    return (
        ENGINE_RULE
        if os.environ.get(ENGINE_ENV_VAR, "").strip().lower() == ENGINE_RULE
        else ENGINE_LLM
    )


def resolve_engine(engine: str | None) -> str:
    """请求里带了就用带的（前端切换控件），没带或带了不认识的值就用默认。"""
    value = (engine or "").strip().lower()
    return value if value in (ENGINE_LLM, ENGINE_RULE) else default_engine()

# 实体快照缓存：load_project 首次约 0.6s，之后内部缓存 0.01s；
# 这里再缓存一层，避免每次解析都走 import/路径拼接。
_ENTITY_SNAPSHOT_CACHE: dict[str, Any] = {}

# 修改类信号：只有像"要改排程"的话术才吃 DSL 澄清，
# 对话修复/元问题（"你没听懂""我是说"）交给对话 agent 处理。
_MODIFY_HINTS = re.compile(
    r"改|调|设|换|加|减|增|降|压|缩短|延长|提前|推迟|延后|顺延|不要|别|禁止|必须|只能"
    r"|最多|至少|不超过|预算|优先|权重|粒度|上限|下限|阈值|放宽|收紧"
)


def _looks_like_modification(text: str) -> bool:
    return bool(_MODIFY_HINTS.search(text or ""))


_VERSION_REF_RE = re.compile(
    r"这(?:两|2)版|这两个?方案|两版|最新两版|上一版|前一版|这版|这一版|方案\s*0*\d"
)


def _has_version_reference(text: str) -> bool:
    return bool(_VERSION_REF_RE.search(text or ""))


def _entity_snapshot() -> Any:
    """给 LLM 的极简快照（只有实体清单，不含数值）。失败返回 None。"""
    if "value" in _ENTITY_SNAPSHOT_CACHE:
        return _ENTITY_SNAPSHOT_CACHE["value"]
    snapshot = None
    try:
        from dsl_core.runner import PROJECT_ROOT, _solver_modules

        load_project = _solver_modules()[0]
        snapshot = load_project(PROJECT_ROOT, base_id="基座01", as_of=None)
    except Exception:  # noqa: BLE001 - 快照缺失不阻塞解析
        snapshot = None
    _ENTITY_SNAPSHOT_CACHE["value"] = snapshot
    return snapshot


class StreamSink:
    """Small adapter so one runtime can both return JSON and stream SSE."""

    def __init__(self, emit) -> None:
        self.emit = emit
        self.text = ""

    def token(self, text: str) -> None:
        if not text:
            return
        self.text += text
        self.emit({"event": "token", "data": {"text": text}})

    def status(self, label: str) -> None:
        self.emit({"event": "status", "data": {"label": label}})

    def stage(
        self,
        stage_id: str,
        label: str,
        status: str = "running",
        detail: str | None = None,
    ) -> None:
        """结构化步骤事件（过程可视化 + 评测阶段 span 同源）。"""
        data: dict[str, Any] = {"id": stage_id, "label": label, "status": status}
        if detail is not None:
            data["detail"] = detail
        self.emit({"event": "stage", "data": data})

    def replace(self, text: str) -> None:
        self.text = text
        self.emit({"event": "replace", "data": {"text": text}})

    def finish(self, content: str) -> None:
        if not self.text:
            self.token(content)
        elif self.text.strip() != content.strip():
            self.replace(content)


class AgentRuntime:
    def __init__(
        self,
        conversations: ConversationRepository,
        messages: MessageRepository,
        memories: MemoryManager,
        versions: VersionRepository,
        model_artifacts: ModelArtifactRepository | None,
        candidates: CandidateRepository,
        normalizer: ResultNormalizer,
        version_manager: VersionManager,
        jobs: SolveJobRepository | None = None,
        job_pool: ThreadPoolExecutor | None = None,
        conversation_agent: ConversationAgent | None = None,
        traces: AgentTraceRepository | None = None,
        summary_repo: ConversationSummaryRepository | None = None,
        profile_repo: UserProfileRepository | None = None,
        decision_repo: DecisionMemoryRepository | None = None,
        memory_repo: ConversationMemoryRepository | None = None,
    ) -> None:
        self.conversations = conversations
        self.messages = messages
        self.memories = memories
        self.versions = versions
        self.model_artifacts = model_artifacts
        self.candidates = candidates
        self.normalizer = normalizer
        self.version_manager = version_manager
        self.jobs = jobs
        self.job_pool = job_pool or ThreadPoolExecutor(max_workers=4, thread_name_prefix="maintenance-solve")
        self._futures: dict[str, Future] = {}
        self.conversation_agent = conversation_agent or ConversationAgent()
        self.traces = traces
        self.summary_repo = summary_repo
        self.profile_repo = profile_repo
        self.decision_repo = decision_repo
        self.memory_repo = memory_repo
        self.memory_cache = ConversationMemoryCache()
        self._tls = threading.local()
        # 小一修槽位引擎：每个会话一张旋钮表。与一修唯一的分叉点——理解层。
        self._yx_engines: dict[str, YxEngine] = {}
        self._batch_lock = threading.Lock()

    # ── trace ────────────────────────────────────────────────────────────────

    def _begin_trace(self, conversation_id: str, content: str) -> None:
        self._tls.trace = AgentTrace(
            requestId=new_trace_id(),
            conversationId=conversation_id,
            message=content,
            route="unknown",
        )
        self._tls.trace_started = time.monotonic()
        # 每个回合都从"没有降级"开始。少了这一行，上一轮的降级标记会跟着
        # 线程活下来，下一轮明明走了模型，界面上仍然挂着"规则兜底"的横幅。
        self._tls.degraded = None
        # 同理：上一轮派出去的求解响应不能留到这一轮。留着的话，下一轮
        # 模型只是闲聊，也会被那个旧响应顶掉，用户看到的是"求解已启动"。
        self._tls.submitted_response = None

    def _note_degradation(self, kind: str, message: str, detail: str | None = None) -> None:
        """记下"这一轮没走模型"。只记第一条——后面那些兜底是它的后果，
        把五个后果都摆给用户看，反而看不出最开始坏在哪。"""
        if getattr(self._tls, "degraded", None) is None:
            self._tls.degraded = DegradedPayload(
                kind=kind,
                message=message,
                detail=(detail or "")[:300] or None,
            )

    def _take_degradation(self) -> DegradedPayload | None:
        payload = getattr(self._tls, "degraded", None)
        self._tls.degraded = None
        return payload

    def _current_trace(self) -> AgentTrace | None:
        return getattr(self._tls, "trace", None)

    def _record_tool_call(self, record: dict[str, Any]) -> None:
        trace = self._current_trace()
        if trace is not None:
            trace.add_tool_call(ToolCallRecord(**record))

    def _finish_trace(
        self,
        conversation_id: str,
        action_type: str,
        solve_evidence: SolveEvidencePayload | None,
    ) -> str | None:
        trace = self._current_trace()
        if trace is None:
            return None
        trace.route = action_type
        trace.latencyMs = round((time.monotonic() - self._tls.trace_started) * 1000, 1)
        trace.model = "yxcore-slot"
        degraded = getattr(self._tls, "degraded", None)
        if degraded is not None:
            trace.degraded = degraded.kind
        if solve_evidence and solve_evidence.modelFingerprint:
            trace.modelFingerprint = solve_evidence.modelFingerprint
        trace_id = trace.requestId
        if self.traces is not None:
            self.traces.add(trace)
        self._tls.trace = None
        self._tls.trace_started = None
        return trace_id

    def _update_summary(self, conversation_id: str) -> None:
        """每个回合结束后更新会话摘要；失败不阻塞主流程。"""
        if self.summary_repo is None:
            return
        try:
            history = self.messages.list_by_conversation(conversation_id)
            memory = self.memories.get_or_create(conversation_id)
            summary = build_summary(conversation_id, history, memory.latestResultId)
            self.summary_repo.upsert(summary)
        except Exception:  # pragma: no cover - 摘要失败不阻断对话
            pass

    def submit_message(
        self,
        conversation_id: str,
        content: str,
        selected_version_id: str | None = None,
        selected_window_id: str | None = None,
        sink: StreamSink | None = None,
        engine: str | None = None,
        llm_api_key: str | None = None,
    ) -> AgentResponse:
        # 访客自带的密钥只在这一轮有效，退出时还原——线程会复用，
        # 忘了还原就等于把上一位访客的密钥借给了下一位。
        previous_key = begin_llm_turn(llm_api_key)
        try:
            response = self._submit_message(
                conversation_id,
                content,
                selected_version_id=selected_version_id,
                selected_window_id=selected_window_id,
                sink=sink,
                engine=resolve_engine(engine),
            )
        finally:
            end_llm_turn(previous_key)
        if sink:
            sink.finish(response.assistantMessage.content)
        return response

    def stream_message(
        self,
        conversation_id: str,
        content: str,
        selected_version_id: str | None = None,
        selected_window_id: str | None = None,
        engine: str | None = None,
        llm_api_key: str | None = None,
    ):
        queue: Queue = Queue()

        def run() -> None:
            try:
                response = self.submit_message(
                    conversation_id,
                    content,
                    selected_version_id=selected_version_id,
                    selected_window_id=selected_window_id,
                    sink=StreamSink(queue.put),
                    engine=resolve_engine(engine),
                    llm_api_key=llm_api_key,
                )
                queue.put({"event": "done", "data": response.model_dump()})
            except Exception as exc:  # pragma: no cover - transport boundary
                queue.put({"event": "error", "data": {"message": str(exc)}})

        Thread(target=run, name=f"agent-stream-{conversation_id}", daemon=True).start()
        while True:
            item = queue.get()
            yield item
            if item.get("event") in {"done", "error"}:
                break

    def _yx_engine(self, conversation_id: str) -> YxEngine:
        engine = self._yx_engines.get(conversation_id)
        if engine is None:
            engine = YxEngine()
            self._yx_engines[conversation_id] = engine
        return engine

    @staticmethod
    def _looks_like_question(content: str) -> bool:
        """开放问句（为什么/是不是/怎么…）——这类不套槽位，交给 LLM 答。"""
        text = content or ""
        return any(
            token in text
            for token in ("为什么", "怎么会", "是不是", "会吗", "吗", "？", "?",
                          "怎么", "是什么", "如何", "哪些", "哪个", "哪台", "谁")
        )

    def _build_toolbox(self, conversation_id, selected_version, sink, user_message=""):
        def on_tool(name, _result):
            if sink:
                labels = {
                    "read_versions": "已读取方案事实",
                    "read_environment": "已读取风况与电价",
                    "read_catalog": "已读取目标目录",
                    "read_alarms": "已读取告警清单",
                    "read_site": "已读取现场数据",
                    "read_jobs": "已查看求解进展",
                    "solve": "已准备变更预览",
                }
                label = labels.get(name, f"已执行 {name}")
                sink.status(label)
                sink.stage(f"tool.{name}", label, "done")

        return AgentToolbox(
            conversation_id=conversation_id,
            versions=self.versions,
            model_artifacts=self.model_artifacts,
            selected_version_id=selected_version.id if selected_version else None,
            on_tool=on_tool,
            on_tool_trace=self._record_tool_call,
            jobs=self.jobs,
            on_pending_change=lambda payload: self._persist_agent_pending(conversation_id, payload),
            on_submit_change=self._submit_pending_change,
            yx_engine=self._yx_engine(conversation_id),
            user_message=user_message,
        )

    def _answer_with_conversation_agent(self, *, conversation_id, content, history,
                                        memory, selected_version, sink):
        """开放问句：复用一修的 LLM 工具循环（读工具 + 产品事实），不硬套槽位。"""
        toolbox = self._build_toolbox(conversation_id, selected_version, sink, user_message=content)
        snapshot = self.memory_cache.get(conversation_id, len(history))
        if snapshot is None:
            snapshot = build_memory_snapshot(
                conversation_id, history, memory,
                self.versions.list_by_conversation(conversation_id),
                llm_summary=self.conversation_agent.summarize(history) or "",
            )
            self.memory_cache.put(snapshot)
        return self.conversation_agent.answer(
            message=content,
            history=history,
            memory=memory,
            toolbox=toolbox,
            selected_version=selected_version,
            snapshot=snapshot,
            on_token=sink.token if sink else None,
            on_status=sink.status if sink else None,
        )

    def _yx_route(self, conversation_id: str, content: str, selected_version_id: str | None, sink):
        """小一修入口：查询走一修的确定性路由，改排程走 yxcore 槽位引擎。

        一修和小一修唯一该有区别的是「理解层」——这里只替换理解层，
        查询（告警/现场/环境/模型问题/对比）、记忆、版本、结果归一化仍共用一修的。
        """
        conversation = self.conversations.get(conversation_id)
        if not conversation:
            raise ValueError("Conversation not found")

        now = datetime.now(SHANGHAI_TZ).isoformat()
        self.messages.add(Message(
            id=f"msg_{uuid4().hex[:8]}", conversationId=conversation_id,
            role="user", content=content, createdAt=now,
        ))
        memory = self.memories.get_or_create(conversation_id)
        selected_version, reference_kind = self._resolve_version(
            conversation_id, selected_version_id, memory, content,
        )
        if reference_kind in {"id", "ordinal", "previous"} and selected_version is None:
            return self._finalize_response(
                conversation_id,
                "没有找到你提到的方案。请确认方案序号或提供 ver_ 开头的版本号。",
                "context_not_found", memory.activeView, [],
            )

        # 0. 待确认变更：取消 / 确认（两步求解）
        pending = (memory.activeFilters or {}).get("pendingChange")
        if isinstance(pending, dict):
            if pending.get("kind") == "clarify":
                groups = pending.get("groups") or []
                chosen = list(pending.get("chosen") or [])
                idx = int(pending.get("nextIndex") or 0)
                if not groups:
                    self._clear_persisted_pending(conversation_id)
                    return self._finalize_response(conversation_id, "这句没接：没有可选的澄清项。", "change_rejected", memory.activeView, [])
                current = groups[idx] if 0 <= idx < len(groups) else groups[-1]
                choice = self._match_clarify_choice(content, current.get("候选") or [])
                if choice is not None:
                    # 匹配上了：累积这条候选的补丁
                    chosen.extend(choice.get("patches") or [])
                    if idx + 1 < len(groups):
                        # 还有下一问：问下一个问题
                        nxt = idx + 1
                        text, options = self._clarify_group_text(groups[nxt], nxt, len(groups))
                        self._persist_agent_pending(conversation_id, {
                            "kind": "clarify", "groups": groups, "chosen": chosen,
                            "nextIndex": nxt, "sourcePrompt": pending.get("sourcePrompt"),
                        })
                        return self._finalize_response(
                            conversation_id, text, "clarification", memory.activeView, [],
                            clarification_options=options,
                        )
                    # 全部答完：应用所有选中的补丁 → 预览 → 确认 → 求解
                    self._clear_persisted_pending(conversation_id)
                    engine = self._yx_engine(conversation_id)
                    patches = self._patches_from_payload(chosen)
                    applied = engine.session.apply(patches)
                    echo = applied.echo()
                    summary = engine.session.summary()
                    lines = [f"- {line}" for line in echo]
                    if summary:
                        lines.append("")
                        lines.append("现在按这些在排：")
                        lines.extend(f"- {line}" for line in summary)
                    preview = "\n".join(lines)
                    self._persist_agent_pending(conversation_id, {
                        "kind": "yx", "sourcePrompt": pending.get("sourcePrompt") or content,
                        "utterance": content, "preview": preview,
                    })
                    return self._finalize_response(conversation_id, preview, "change_preview", memory.activeView, [])
                # choice is None：用户没选序号/意图，而是重新表述了需求
                # → 清掉澄清、刷新 memory，落到下面的「查询 → 翻译」重新理解这句话，
                #   别死循环重问同一个问题。
                self._clear_persisted_pending(conversation_id)
                memory = self.memories.get_or_create(conversation_id)
            if "取消" in content or content.strip() in {"算了", "不要了", "放弃"}:
                self._clear_persisted_pending(conversation_id)
                return self._finalize_response(conversation_id, "已取消本次变更，没有生成新方案。", "change_cancelled", memory.activeView, [])
            if "确认" in content or "生成沙盘" in content or content.strip() in {"算吧", "可以", "开始", "确定", "好", "行"}:
                self._clear_persisted_pending(conversation_id)
                return self._start_planning_solve(conversation_id, pending, memory, sink)

        # 1. 查询：确定性路由（复用一修查询层）
        operations_question = detect_operations_question(content)
        if operations_question:
            kind, scope = operations_question
            return self._answer_operations_question(
                conversation_id, content, kind, scope, selected_version, memory, sink=sink,
            )
        model_question = detect_model_question(content)
        if model_question:
            if model_question == "capability":
                return self._answer_capability_question(conversation_id, content, memory)
            if model_question == "environment":
                return self._answer_environment_question(conversation_id, content, memory, selected_version)
            if model_question == "structure" and not selected_version:
                return self._answer_structure_question(conversation_id, content, memory)
            if not selected_version:
                if model_question == "parameter":
                    return self._answer_operations_question(
                        conversation_id, content, "site", "parameters", selected_version, memory, sink=sink,
                    )
                return self._finalize_response(
                    conversation_id,
                    "当前会话还没有可查询的排程方案。请先生成方案，之后我才能解释模型、参数或结果。",
                    "model_query_empty", memory.activeView, [],
                )
            # 有方案：结构问句升级成该版公式问句（一修同名语义）。
            if model_question == "structure":
                model_question = "formula"
            if model_question in {"parameter", "status"}:
                return self._answer_model_question(conversation_id, content, model_question, selected_version, memory)
            # formula：进 LLM 工具循环，让 read_versions(include=model) 给出真模型；
            # 循环答不上才降级到确定性 _answer_model_question。
            return self._answer_agent_question(conversation_id, content, model_question, selected_version, memory, sink=sink)
        compare_reply = self._compare_versions_reply(conversation_id, content, selected_version)
        if compare_reply is not None:
            return self._finalize_response(conversation_id, compare_reply, "version_compare", memory.activeView, [])

        # 2. 改排程优先：yxcore 槽位翻译（可靠、确定性填槽）。翻不出旋钮（=不是排程约束）→ LLM 问答。
        engine = self._yx_engine(conversation_id)
        if sink:
            sink.stage("translate", "理解：翻译成槽位补丁", "running")
        try:
            turn = engine.say(content, timeout=90)
        except Exception:  # noqa: BLE001
            turn = None
        if sink:
            sink.stage("translate", "理解：翻译成槽位补丁", "done")
        if turn is not None:
            echo = turn.echo()
            # 复合需求 → N 套场景 → 批量预览 → 确认后并发求解
            if turn.场景:
                scenarios = turn.场景
                labels = [str(sc.get("标签") or f"方案{i+1}") for i, sc in enumerate(scenarios)]
                preview = ("将按这些方案分别排：\n" + "\n".join(f"- {lab}" for lab in labels)
                           + "\n\n确认后并发求解，全部算完再对比。")
                payload = {
                    "kind": "batch",
                    "scenarios": [
                        {"标签": lab, "patches": [p.to_json() for p in sc["patches"]]}
                        for lab, sc in zip(labels, scenarios)
                    ],
                    "sourcePrompt": content,
                }
                self._persist_agent_pending(conversation_id, payload)
                return self._finalize_response(conversation_id, preview, "batch_preview", memory.activeView, [])
            # 指代/范围/顺序不清 → 枚举候选让用户点选，不落 LLM 工具循环、也不当改排程预览
            if turn.澄清:
                groups = turn.澄清
                text, options = self._clarify_group_text(groups[0], 0, len(groups))
                self._persist_agent_pending(conversation_id, {
                    "kind": "clarify",
                    "groups": self._serialize_clarify_groups(groups),
                    "chosen": [],
                    "nextIndex": 0,
                    "sourcePrompt": content,
                })
                return self._finalize_response(
                    conversation_id, text, "clarification", memory.activeView, [],
                    clarification_options=options,
                )
            is_change = bool(turn.新增 or turn.修订 or turn.撤销) and not turn.错误 and not turn.拒绝
            if not is_change:
                # 不是排程约束（问题/开放问句/越界）→ 复用一修的 LLM 工具循环答
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
                        return self._finalize_response(conversation_id, reply, "assistant_reply", memory.activeView, [])
                    except ConversationAgentError as exc:
                        self._note_degradation("agent_unavailable", "模型这一轮没答上来。", str(exc))
                text = "\n".join(f"- {line}" for line in echo)
                action = "change_rejected" if turn.拒绝 else "assistant_reply"
                return self._finalize_response(conversation_id, text, action, memory.activeView, [])
            # 是排程约束：出预览（预览 → 确认 → 求解）
            summary = engine.session.summary()
            lines = [f"- {line}" for line in echo]
            if summary:
                lines.append("")
                lines.append("现在按这些在排：")
                lines.extend(f"- {line}" for line in summary)
            preview = "\n".join(lines)
            payload = {"kind": "yx", "sourcePrompt": content, "utterance": content, "preview": preview}
            self._persist_agent_pending(conversation_id, payload)
            return self._finalize_response(conversation_id, preview, "change_preview", memory.activeView, [])

        # 3. 兜底：模型不可用，如实回显
        return self._finalize_response(conversation_id, "这句没接：没有对应的旋钮。", "change_rejected", memory.activeView, [])

    def batch_progress(self, conversation_id: str) -> dict[str, int] | None:
        """当前会话还有没有没算完的场景。前端靠它决定要不要继续轮询。

        小一修是单次填槽（没有多场景），pendingBatch 永不存在，恒返回 None；
        但 agent.py 的 /jobs 接口会调它，所以保留这个共享方法。
        """
        memory = self.memories.get_or_create(conversation_id)
        batch = (memory.activeFilters or {}).get("pendingBatch")
        if not isinstance(batch, dict):
            return None
        total = int(batch.get("total") or 0)
        if total <= 0:
            return None
        done = len(batch.get("completed") or [])
        return {"position": min(done + 1, total), "total": total}

    def _submit_pending_change(self, conversation_id, pending, reason):
        memory = self.memories.get_or_create(conversation_id)
        if pending.get("kind") != "yx":
            return {"ok": False, "message": "待确认的变更已经失效。"}
        self._clear_persisted_pending(conversation_id)
        resp = self._start_planning_solve(conversation_id, pending, memory, None)
        self._tls.submitted_response = resp
        return {"ok": True, "jobId": None, "message": "真实求解已完成。"}

    def _persist_agent_pending(self, conversation_id, pending):
        latest = self.memories.get_or_create(conversation_id)
        filters = dict(latest.activeFilters)
        filters["pendingChange"] = pending
        self.memories.apply_patch(latest, {"activeFilters": filters})

    def _clear_persisted_pending(self, conversation_id):
        latest = self.memories.get_or_create(conversation_id)
        filters = dict(latest.activeFilters)
        if "pendingChange" in filters:
            filters.pop("pendingChange", None)
            self.memories.apply_patch(latest, {"activeFilters": filters})

    def _start_planning_solve(self, conversation_id, pending, memory, sink):
        if pending.get("kind") == "batch":
            return self._start_batch_solve(conversation_id, pending, memory, sink)
        content = str(pending.get("sourcePrompt") or "")
        if self.jobs is None or self.job_pool is None:
            # 没有求解任务仓库（测试/降级）：同步求解
            return self._yx_run_sync(conversation_id, content, memory, sink)
        # 后台异步求解：建任务 → 丢线程池 → 立即返回，前端轮询进度
        job = self.jobs.create(conversation_id, {"kind": "yx", "message": content, "budgetSeconds": _SOLVE_SECONDS}, "")
        self._futures[job.id] = self.job_pool.submit(
            self._execute_job, job.id, conversation_id, content
        )
        return self._finalize_response(
            conversation_id,
            f"真实 CP-SAT 求解已启动，正在后台跑（任务 {job.id}）。算完会自动出方案卡。",
            "solve_started", "P01", [],
            solve_evidence=SolveEvidencePayload(status="UNKNOWN", running=True, phase="queued"),
            solve_job=SolveJobPayload(**job.model_dump()),
        )

    def _start_batch_solve(self, conversation_id, pending, memory, sink):
        """复合需求：并发 fork N 个求解任务，全部算完后汇合对比。"""
        scenarios = pending.get("scenarios") or []
        labels = [str(sc.get("标签") or f"方案{i+1}") for i, sc in enumerate(scenarios)]
        filters = dict(memory.activeFilters)
        filters["pendingBatch"] = {"total": len(scenarios), "completed": [], "labels": labels}
        self.memories.apply_patch(memory, {"activeFilters": filters})
        if self.jobs is None or self.job_pool is None:
            # 测试/降级：同步逐个跑，再对比
            versions = []
            for sc in scenarios:
                version = self._run_batch_scenario_sync(conversation_id, sc)
                if version is not None:
                    versions.append(version)
            if versions:
                self._emit_batch_compare(conversation_id, [v.id for v in versions])
            return self._finalize_response(conversation_id, "批量求解已完成。", "solve_completed", "P01", versions)
        # 并发 fork：每个场景一个独立任务
        for i, sc in enumerate(scenarios):
            job = self.jobs.create(
                conversation_id,
                {"kind": "batch", "label": labels[i], "patches": sc.get("patches") or [], "budgetSeconds": _SOLVE_SECONDS},
                "",
            )
            self._futures[job.id] = self.job_pool.submit(
                self._execute_batch_job, job.id, conversation_id, labels[i], sc.get("patches") or []
            )
        return self._finalize_response(
            conversation_id,
            f"已并发启动 {len(scenarios)} 个求解任务，全部算完会自动出方案卡和对比。",
            "solve_started", "P01", [],
            solve_evidence=SolveEvidencePayload(status="UNKNOWN", running=True, phase="queued"),
        )

    def _run_batch_scenario_sync(self, conversation_id, scenario):
        from yxcore.patch import parse_patch
        engine = YxEngine()
        patches = self._patches_from_payload(scenario.get("patches") or [])
        turn = engine.session.apply(patches)
        if turn.错误:
            return None
        try:
            result = engine.run()
        except Exception:  # noqa: BLE001
            return None
        version_id = f"ver_{uuid4().hex[:8]}"
        version = engine.build_version(conversation_id, scenario.get("标签") or "", result, version_id)
        self.versions.add(version)
        if self.candidates is not None:
            self.candidates.replace_for_version(conversation_id, version.id, version.windows)
        return version

    @staticmethod
    def _patches_from_payload(raw_patches):
        from yxcore.patch import normalize_patch, parse_patch
        patches = []
        for raw in raw_patches or []:
            patch, error = parse_patch(raw)
            if patch is None:
                raise ValueError(error or "补丁反序列化失败")
            patches.append(normalize_patch(patch))
        return patches

    @staticmethod
    def _match_clarify_choice(text: str, candidates: list[dict]) -> dict | None:
        """把用户回复（序号或意图）匹配到某条候选；匹配不上返回 None。"""
        text = (text or "").strip()
        n = len(candidates or [])
        # 空串不能当「匹配上了」：'' in 任意标签 恒真，会把没回答当成选了第一条
        if not text:
            return None
        # 序号：1 / 第1个 / 选1 / 选项1 / ①
        for i in range(n):
            if text in {str(i + 1), f"第{i + 1}个", f"第{i + 1}项", f"选{i + 1}",
                        f"选项{i + 1}", f"{i + 1}.", f"{i + 1}）", f"{i + 1})"}:
                return candidates[i]
        # 意图标签：用户说了意图里的字
        for cand in candidates:
            label = str(cand.get("意图") or "").strip()
            if label and (label in text or text in label):
                return cand
        # 用户直接说「硬约束」「相对偏好」也能选中对应候选；不然标签上看得见、
        # 手打却选不中，绝对优先那条还是点不到
        for cand in candidates:
            nature = str(cand.get("性质") or "").strip()
            if nature and (nature in text or text in nature):
                return cand
        return None

    @staticmethod
    def _serialize_clarify_groups(groups):
        """委托工具层的共享实现（与 solve 工具产出的 payload 必须同构）。"""
        return serialize_clarify_groups(groups)

    @staticmethod
    def _clarify_label(cand: dict, index: int) -> str:
        """选项标签要能看出「哪条是硬约束、哪条是推荐」。

        绝对优先被误做成加权偏好是这一版最贵的错，界面上不能只给两个光秃秃的意图名。
        """
        label = str(cand.get("意图") or f"选项{index + 1}")
        tags = []
        if cand.get("性质"):
            tags.append(str(cand["性质"]))
        if cand.get("推荐"):
            tags.append("推荐")
        return f"{label}（{'·'.join(tags)}）" if tags else label

    def _clarify_group_text(self, group, group_index, total):
        """把一个问题组格式成「问题 N：… 你是想：1…2…」的正文 + 可点击选项。"""
        cands = group.get("候选") or []
        labels = [self._clarify_label(c, i) for i, c in enumerate(cands)]
        head = f"问题{group_index + 1}" + (f"：{group.get('问')}" if group.get("问") else "")
        if total > 1:
            head += f"（共 {total} 个问题）"
        text = (head + "，你是想：\n"
                + "\n".join(f"{i+1}. {lab}" for i, lab in enumerate(labels))
                + "\n\n回复序号（如「1」），或直接说你的要求（比如「只做任务6-10」）。")
        options = [ClarificationOptionPayload(label=lab, value=str(i + 1))
                   for i, lab in enumerate(labels)]
        # 给一个「都不是」的出口：用户想说自己，就点它，前端切回自由输入
        options.append(ClarificationOptionPayload(label="都不是，我重新说", value="__custom__"))
        return text, options

    def _execute_batch_job(self, job_id, conversation_id, label, raw_patches):
        engine = YxEngine()
        patches = self._patches_from_payload(raw_patches)
        turn = engine.session.apply(patches)
        if self.jobs:
            self.jobs.mark_running(job_id)
        if turn.错误:
            if self.jobs:
                self.jobs.mark_failed(job_id, "；".join(turn.错误))
            self._batch_job_done(conversation_id, job_id, None)
            return
        try:
            result = engine.run()
        except Exception as exc:  # noqa: BLE001
            if self.jobs:
                self.jobs.mark_failed(job_id, str(exc))
            self._batch_job_done(conversation_id, job_id, None)
            return
        version_id = f"ver_{uuid4().hex[:8]}"
        version = engine.build_version(conversation_id, label, result, version_id, solve_job_id=job_id)
        self.versions.add(version)
        if self.candidates is not None:
            self.candidates.replace_for_version(conversation_id, version.id, version.windows)
        info = result.get("求解") or {}
        status = info.get("解状态") or "UNKNOWN"
        if self.jobs:
            self.jobs.mark_completed(
                job_id,
                {"versionId": version.id, "summary": version.resultSummary},
                status,
                has_incumbent=bool(info.get("有解")),
                status_message=f"方案「{label}」排程完成。",
            )
        self._batch_job_done(conversation_id, job_id, version)

    def _batch_job_done(self, conversation_id, job_id, version):
        """一个场景算完后推进批量；全部算完则汇合出对比。"""
        with self._batch_lock:
            memory = self.memories.get_or_create(conversation_id)
            batch = (memory.activeFilters or {}).get("pendingBatch")
            if not isinstance(batch, dict):
                return
            completed = list(batch.get("completed") or [])
            if version is not None and version.id not in completed:
                completed.append(version.id)
            total = int(batch.get("total") or 0)
            if len(completed) >= total:
                filters = dict(memory.activeFilters)
                filters.pop("pendingBatch", None)
                self.memories.apply_patch(memory, {"activeFilters": filters})
                self._emit_batch_compare(conversation_id, completed)
            else:
                filters = dict(memory.activeFilters)
                filters["pendingBatch"] = {**batch, "completed": completed}
                self.memories.apply_patch(memory, {"activeFilters": filters})

    def _emit_batch_compare(self, conversation_id, version_ids):
        """多个方案都算完了，直接出对比，不再多一次模型调用。"""
        pairs: list[tuple] = []
        unique: list[str] = []
        for version_id in version_ids:
            if version_id not in unique:
                unique.append(version_id)
        for index in range(len(unique) - 1):
            left = self.versions.get(unique[index])
            right = self.versions.get(unique[index + 1])
            if left and right:
                pairs.append((left, right))
        if not pairs:
            return
        toolbox = self._build_toolbox(conversation_id, None, None)
        blocks = []
        for left, right in pairs:
            blocks.append(render_compare_summary(toolbox.compare_pair(left, right)))
        memory = self.memories.get_or_create(conversation_id)
        self._finalize_response(
            conversation_id,
            f"这些方案都算完了，下面是它们的对比：\n\n" + "\n\n".join(blocks),
            "batch_compare",
            memory.activeView,
            [item for pair in pairs for item in pair],
        )

    def _yx_run_sync(self, conversation_id, content, memory, sink, solve_job_id=None):
        engine = self._yx_engine(conversation_id)
        if sink:
            sink.stage("solve", "CP-SAT 求解", "running")
        try:
            result = engine.run()
        except Exception as exc:  # noqa: BLE001
            return self._finalize_response(conversation_id, f"求解没成功：{exc}", "solve_failed", memory.activeView, [])
        if sink:
            sink.stage("solve", "CP-SAT 求解", "done")
        return self._yx_build_reply(conversation_id, content, memory, result, solve_job_id=solve_job_id)

    def _yx_build_reply(self, conversation_id, content, memory, result, solve_job_id=None):
        engine = self._yx_engine(conversation_id)
        version_id = f"ver_{uuid4().hex[:8]}"
        version = engine.build_version(conversation_id, content, result, version_id, solve_job_id=solve_job_id)
        self.versions.add(version)
        # 模型产物跟着版本落库：没有它，「看数学模型」只剩下诚实地说没保存。
        # yxcore 的求解器本来就把 model_artifact.json 写到 output 目录，
        # pipeline.run 已经把它带进 result["模型产物"]；这里只差最后一根线。
        artifact = result.get("模型产物")
        if self.model_artifacts is not None and isinstance(artifact, dict) and artifact:
            self.model_artifacts.add(version.id, version.conversationId, version.generatedAt, artifact)
        if self.candidates is not None:
            self.candidates.replace_for_version(conversation_id, version.id, version.windows)
        self.memories.apply_patch(memory, {
            "latestResultId": version.id,
            "selectedVersionIds": [version.id],
            "activeView": "P01",
        })
        info = result.get("求解") or {}
        status = info.get("解状态") or "UNKNOWN"
        body = self._format_product_reply(status, version.resultSummary, version)
        return self._finalize_response(
            conversation_id, body, "solve_completed", "P01", [version],
            solve_evidence=self._product_solve_evidence(result, version.modelFingerprint or "", status),
            solve_job_id=solve_job_id,
        )

    def _execute_job(self, job_id, conversation_id, content):
        engine = self._yx_engine(conversation_id)
        if self.jobs:
            self.jobs.mark_running(job_id)
        try:
            result = engine.run()
        except Exception as exc:  # noqa: BLE001
            if self.jobs:
                self.jobs.mark_failed(job_id, str(exc))
            self._finalize_response(conversation_id, f"求解任务执行失败：{exc}", "solve_failed", "P01", [])
            return
        if self.jobs:
            self.jobs.mark_reviewing(job_id)
        memory = self.memories.get_or_create(conversation_id)
        resp = self._yx_build_reply(conversation_id, content, memory, result, solve_job_id=job_id)
        info = result.get("求解") or {}
        status = info.get("解状态") or "UNKNOWN"
        body = resp.assistantMessage.content
        stored_result = {"versionId": resp.newVersions[0].id if resp.newVersions else None, "summary": {}}
        if resp.newVersions:
            stored_result["summary"] = resp.newVersions[0].resultSummary
        if self.jobs:
            self.jobs.mark_completed(
                job_id, stored_result, status,
                has_incumbent=bool(info.get("有解")),
                status_message=str(body).strip()[:300],
            )

    def _yx_solve(self, conversation_id: str, content: str, memory, sink):
        engine = self._yx_engine(conversation_id)
        if sink:
            sink.stage("translate", "翻译成槽位补丁", "running")
        try:
            turn = engine.say(content, timeout=90)
        except Exception as exc:  # noqa: BLE001
            return self._finalize_response(
                conversation_id, f"这句没接：{exc}", "change_rejected", memory.activeView, [],
            )
        echo = turn.echo()
        if sink:
            sink.stage("translate", "翻译成槽位补丁", "done")
        if turn.错误:
            text = "\n".join(f"- {line}" for line in echo)
            return self._finalize_response(conversation_id, text, "change_rejected", memory.activeView, [])

        # 拒绝（不在边界内，如「均衡一下」）或没改任何槽位：不求解，直接回显。
        if turn.拒绝 or not (turn.新增 or turn.修订 or turn.撤销):
            text = "\n".join(f"- {line}" for line in echo)
            action = "change_rejected" if turn.拒绝 else "assistant_reply"
            return self._finalize_response(conversation_id, text, action, memory.activeView, [])

        if sink:
            sink.stage("solve", "CP-SAT 求解", "running")
        try:
            result = engine.run()
        except Exception as exc:  # noqa: BLE001
            text = "\n".join(f"- {line}" for line in echo) + f"\n\n- 求解没成功：{exc}"
            return self._finalize_response(conversation_id, text, "solve_failed", memory.activeView, [])
        if sink:
            sink.stage("solve", "CP-SAT 求解", "done")

        version_id = f"ver_{uuid4().hex[:8]}"
        version = engine.build_version(conversation_id, content, result, version_id)
        self.versions.add(version)
        if self.candidates is not None:
            self.candidates.replace_for_version(conversation_id, version.id, version.windows)
        self.memories.apply_patch(memory, {
            "latestResultId": version.id,
            "selectedVersionIds": [version.id],
            "activeView": "P01",
        })

        info = result.get("求解") or {}
        status = info.get("解状态") or "UNKNOWN"
        summary = engine.session.summary()
        head = "\n".join(f"- {line}" for line in echo)
        if summary:
            head += "\n\n**现在按这些在排：**\n" + "\n".join(f"- {line}" for line in summary)
        body = self._format_product_reply(status, version.resultSummary, version)
        reply = f"{head}\n\n{body}" if head else body
        return self._finalize_response(
            conversation_id, reply, "solve_completed", "P01", [version],
            solve_evidence=self._product_solve_evidence(result, version.modelFingerprint or "", status),
        )

    def _submit_message(
        self,
        conversation_id: str,
        content: str,
        selected_version_id: str | None = None,
        selected_window_id: str | None = None,
        sink: StreamSink | None = None,
        engine: str | None = None,
    ) -> AgentResponse:
        self._begin_trace(conversation_id, content)
        self._tls.engine = resolve_engine(engine)
        # 小一修：理解层是槽位引擎（yxcore），不走一修的 LLM 工具循环 / DSL 构造。
        return self._yx_route(conversation_id, content, selected_version_id, sink)

    def _resolve_version(
        self,
        conversation_id: str,
        selected_version_id: str | None,
        memory,
        content: str = "",
    ):
        conversation_versions = self.versions.list_by_conversation(conversation_id)
        base_version = None
        if selected_version_id:
            version = self.versions.get(selected_version_id)
            if version and version.conversationId == conversation_id:
                base_version = version
        latest_id = memory.latestResultId
        if base_version is None and latest_id:
            version = self.versions.get(latest_id)
            if version and version.conversationId == conversation_id:
                base_version = version
        if base_version is None and conversation_versions:
            base_version = conversation_versions[0]
        explicit, reference_kind = resolve_version_reference(
            content,
            conversation_versions,
            selected_version=base_version,
        )
        if explicit:
            return explicit, reference_kind
        if reference_kind in {"id", "ordinal", "previous"}:
            return None, reference_kind
        return base_version, reference_kind

    def _answer_capability_question(
        self,
        conversation_id: str,
        content: str,
        memory,
    ) -> AgentResponse:
        """能力问句：有哪些目标、权重、策略可以直接答，不依赖任何方案。"""
        memory = self.memories.apply_patch(
            memory,
            {"currentGoal": content, "currentIntent": "capability"},
        )
        return self._finalize_response(
            conversation_id,
            render_capability_answer(),
            "capability_answer",
            memory.activeView,
            [],
        )

    def _answer_environment_question(
        self,
        conversation_id: str,
        content: str,
        memory,
        version,
    ) -> AgentResponse:
        """风况/电价属确定性数据：直接取基座逐时表，不经过模型转抄。"""
        from app.core import environment_data

        window = self._environment_window(content)
        try:
            if window:
                summary = environment_data.environment_summary(window=window)
                markdown = environment_data.render_window_markdown(summary)
            else:
                summary = environment_data.environment_summary(days=7)
                markdown = environment_data.render_markdown(summary)
        except Exception:  # noqa: BLE001 - 数据缺失不阻断
            markdown = ""
        if not markdown:
            reply = "这次没有取到风况或电价序列，不能给出具体数值。"
            action = "environment_unavailable"
        else:
            reply = markdown
            action = "environment_answer"
        memory = self.memories.apply_patch(
            memory,
            {"currentGoal": content, "currentIntent": "environment"},
        )
        return self._finalize_response(
            conversation_id,
            reply,
            action,
            memory.activeView,
            [],
        )

    def _answer_operations_question(
        self,
        conversation_id: str,
        content: str,
        kind: str,
        scope: str | None,
        selected_version,
        memory,
        sink: StreamSink | None = None,
    ) -> AgentResponse:
        """Pure site-data lookups go straight to the read tool.

        This keeps “现在有哪些告警 / 库存还有多少 / 下一个窗口” from depending
        on whether the model remembers that the domain exists.
        """
        from app.core import site_data

        snapshot = site_data.load_site_snapshot()
        if kind == "alarms":
            args = alarm_arguments(content)
            report = site_data.alarm_report(
                snapshot,
                scope=str(args.get("scope") or "backlog"),
                days=int(args.get("days") or 7),
                severity=args.get("severity"),
                status=args.get("status"),
                turbine_id=args.get("turbineId"),
                component=args.get("component"),
                task_id=args.get("taskId"),
                include_closed=bool(args.get("includeClosed")),
                date=args.get("date"),
            )
            action = "alarm_answer"
        else:
            scope_name = scope or "overview"
            report = site_data.site_report(
                snapshot,
                scope=scope_name,
                query=content if scope_name == "parameters" else None,
            )
            action = "site_answer"
        markdown = report.get("markdown")
        if not markdown:
            reply = "这次没有取到现场数据，不能给出具体清单或数值。"
            action = f"{action}_unavailable"
        else:
            reply = markdown
        memory = self.memories.apply_patch(
            memory,
            {"currentGoal": content, "currentIntent": action},
        )
        return self._finalize_response(
            conversation_id,
            reply,
            action,
            memory.activeView,
            [],
        )

    @staticmethod
    def _environment_days(content: str) -> int:
        text = "".join(content.split())
        if any(token in text for token in ("一个月", "30天", "四周", "4周")):
            return 30
        if any(token in text for token in ("两周", "14天", "半个月")):
            return 14
        if any(token in text for token in ("三天", "3天")):
            return 3
        match = re.search(r"(\d+)\s*天", text)
        if match:
            return max(1, min(int(match.group(1)), 31))
        return 7

    @classmethod
    def _environment_chart_range(cls, content: str) -> int:
        requested = cls._environment_days(content)
        return min((3, 7, 14, 30), key=lambda value: (abs(value - requested), value))

    @staticmethod
    def _environment_window(content: str) -> str | None:
        text = "".join(content.split())
        match = re.search(r"窗口\s*0*(\d+)", text)
        if match:
            return f"窗口{int(match.group(1)):02d}"
        if any(token in text for token in ("目标窗口", "本次窗口", "这个窗口")):
            return "target"
        return None

    def _answer_structure_question(
        self,
        conversation_id: str,
        content: str,
        memory,
    ) -> AgentResponse:
        """结构问句：还没有方案时，讲模板结构，不假装读过某次求解。"""
        memory = self.memories.apply_patch(
            memory,
            {"currentGoal": content, "currentIntent": "structure"},
        )
        return self._finalize_response(
            conversation_id,
            render_structure_answer(),
            "structure_answer",
            memory.activeView,
            [],
        )

    def _answer_model_question(
        self,
        conversation_id: str,
        content: str,
        question_kind: str,
        version,
        memory,
    ) -> AgentResponse:
        artifact = self.get_model_artifact(version.id)
        patch: dict[str, Any] = {
            "latestResultId": version.id,
            "selectedVersionIds": [version.id],
        }
        if question_kind in {"formula", "parameter"} and not artifact:
            reply = (
                f"{render_missing_artifact(version)}\n"
                "我已按该版本的参数准备好一次可审计重跑；确认后会生成新版本并保存模型产物。"
            )
            filters = dict(memory.activeFilters)
            filters["pendingChange"] = {
                "baseVersionId": version.id,
                "request": self._planning_request_from_context(memory, version).to_payload(),
                "operations": [{
                    "kind": "artifact_rebuild",
                    "key": "模型产物",
                    "before": "缺失",
                    "after": "重新运行并保存完整模型产物",
                }],
                "preview": (
                    f"将按方案 {version.id} 的原始参数重新运行一次，"
                    "生成独立的新版本并保存 ModelArtifact；不覆盖旧版本。"
                ),
                "sourcePrompt": f"重新运行方案 {version.id} 以保存数学模型产物",
            }
            patch["activeFilters"] = filters
        elif question_kind == "formula":
            reply = render_model_formula(version, artifact or {})
        elif question_kind == "parameter":
            reply = render_parameter_answer(version, artifact or {}, content)
        elif question_kind == "status":
            reply = render_status_answer(version)
        else:
            reply = render_solution_answer(version, content)
        self.memories.apply_patch(memory, patch)
        return self._finalize_response(
            conversation_id,
            reply,
            f"model_{question_kind}",
            memory.activeView,
            [],
        )

    def _compare_versions_reply(
        self,
        conversation_id: str,
        content: str,
        selected_version,
    ) -> str | None:
        """版本对比：解析"哪两版"，给具体差异而不是通用模型说明。

        支持两种指代：
          - 显式序号："方案01和方案02的区别"
          - 相对指代："这两版本/两个方案/最新两版/上一版和这版" → 最近两版
        """
        if not any(token in content for token in ("区别", "差异", "对比", "比较", "不同", "差别")):
            return None
        try:
            ordered = chronological_versions(self.versions.list_by_conversation(conversation_id))
        except Exception:  # noqa: BLE001
            return None
        if len(ordered) < 2:
            if len(ordered) == 1 and _has_version_reference(content):
                return (
                    "当前会话只有 1 版方案，没有可比较的两版。\n"
                    "先再排一版（例如改个约束或策略后生成沙盘），我再按模型层和结果层逐项对比。"
                )
            return None

        toolbox = None  # 已移除 agent_tools；下面直接按序号/相对指代解析版本
        resolved = []
        ordinals = re.findall(r"方案\s*0*(\d{1,3})", content)
        if len(ordinals) >= 2:
            for ordinal in ordinals[:2]:
                index = int(ordinal) - 1
                if 0 <= index < len(ordered) and ordered[index].id not in {item.id for item in resolved}:
                    resolved.append(ordered[index])
        elif _has_version_reference(content):
            resolved = ordered[-2:]
        if len(resolved) < 2:
            return None

        left, right = resolved[0], resolved[1]
        numbers = {item.id: index for index, item in enumerate(ordered, start=1)}
        left_label = f"方案{numbers.get(left.id, 0):02d}"
        right_label = f"方案{numbers.get(right.id, 0):02d}"
        if not ordinals:
            left_label = f"{left_label}（{left.id}）"
            right_label = f"{right_label}（{right.id}）"
        # 说"哪两版"时把目标模式带上：分层和加权算出来的是两套目标，
        # 只说"方案01 与 方案02"，读者无法知道差异来自目标还是来自数据。
        left_mode = (left.resultSummary or {}).get("objectiveMode")
        right_mode = (right.resultSummary or {}).get("objectiveMode")
        if left_mode:
            left_label = f"{left_label}·{left_mode}"
        if right_mode:
            right_label = f"{right_label}·{right_mode}"

        lines = [f"我按最近的两版来比：{left_label} → {right_label}。"]
        left_fp = getattr(left, "modelFingerprint", None)
        right_fp = getattr(right, "modelFingerprint", None)
        if left_fp and right_fp:
            if left_fp == right_fp:
                lines.append(
                    "**模型层**：两版模型指纹相同——决策变量、约束和目标函数结构完全一致，"
                    "差异来自输入数据或求解结果，不是模型本身。"
                )
            else:
                lines.append(
                    "**模型层**：两版模型指纹不同——约束、目标或数据假设至少有一项变了，"
                    "具体差异见下面对照。"
                )
        else:
            lines.append("**模型层**：至少一版缺少完整模型产物，我只能对比结果层事实。")

        try:
            payload = compare_versions(
                left, right,
                left_number=numbers.get(left.id, 0),
                right_number=numbers.get(right.id, 0),
            )
            lines.append(render_compare_summary(payload))
        except Exception as exc:  # noqa: BLE001
            lines.append(f"（结果层对比失败：{exc}）")
        return "\n\n".join(lines)

    def _answer_agent_question(
        self,
        conversation_id: str,
        content: str,
        question_kind: str,
        version,
        memory,
        sink=None,
    ) -> AgentResponse:
        """模型问句（formula）：走 LLM 工具循环，用真产物回答数学模型。

        与一修同名同语义：先看产物在不在；在就进循环让 read_versions(include=model)
        透传 markdown/latex；循环答不上降级到确定性 _answer_model_question，
        并如实标注这次是规则兜底。
        """
        artifact = self.get_model_artifact(version.id)
        if question_kind in {"formula", "parameter"} and not artifact:
            return self._answer_model_question(
                conversation_id, content, question_kind, version, memory,
            )
        history = self.messages.list_by_conversation(conversation_id)
        try:
            reply = self._answer_with_conversation_agent(
                conversation_id=conversation_id,
                content=content,
                history=history,
                memory=memory,
                selected_version=version,
                sink=sink,
            )
        except ConversationAgentError as exc:
            self._note_degradation(
                "model_question_fallback",
                "模型没能解释这个问题，下面是从模型产物直接读出来的答案。",
                str(exc),
            )
            return self._answer_model_question(
                conversation_id, content, question_kind, version, memory,
            )
        memory = self.memories.get_or_create(conversation_id)
        self.memories.apply_patch(
            memory,
            {
                "currentGoal": content,
                "currentIntent": f"model_{question_kind}",
                "latestResultId": version.id,
                "selectedVersionIds": [version.id],
            },
        )
        return self._finalize_response(
            conversation_id,
            reply,
            f"model_{question_kind}",
            memory.activeView,
            [],
        )

    def get_job(self, job_id: str):
        if not self.jobs:
            return None
        return self.jobs.get(job_id)

    def get_latest_job(self, conversation_id: str):
        if not self.jobs:
            return None
        return self.jobs.latest_for_conversation(conversation_id)

    def list_jobs(self, conversation_id: str):
        if not self.jobs:
            return []
        return self.jobs.list_by_conversation(conversation_id)

    def control_job(self, job_id: str, action: str):
        if not self.jobs:
            raise ValueError("Solve jobs are not configured")
        job = self.jobs.get(job_id)
        if not job:
            raise ValueError("Solve job not found")
        if action == "background":
            return self.jobs.set_backgrounded(job_id, True)
        if action == "wait":
            return self.jobs.defer_decision(job_id)
        if action == "stop":
            future = self._futures.get(job_id)
            if future:
                future.cancel()
            return self.jobs.mark_cancelled(job_id)
        if action == "use_incumbent" and not job.hasIncumbent:
            raise ValueError("当前尚无可用方案")
        if action == "use_incumbent":
            return job
        raise ValueError("Unknown solve action")

    @staticmethod
    def _format_product_reply(status: str, summary: dict, version) -> str:
        total_cost = float(summary.get("totalCost") or 0)
        completed = summary.get("scheduledTaskCount", 0)
        total = summary.get("totalTaskCount", 0)
        windows = "、".join(
            f"{item.windowId}（{item.metrics.startTime} 至 {item.metrics.endTime}）"
            for item in version.windows
        )
        proof = "已证明最优" if status == "OPTIMAL" else "已找到并复核可行方案，尚未证明最优"
        solver = summary.get("solver") or {}
        fallback = str(solver.get("方案来源") or "") == "HEURISTIC_FALLBACK"
        if fallback:
            # 求解器没产出排程时，给出的是启发式基线的部分排程，不能按“可行方案”汇报。
            opening = (
                f"本次未得到求解器排程：现在这版是启发式基线兜底，"
                f"只完成 {completed}/{total} 个任务，尚未证明最优。"
            )
        else:
            opening = f"排程完成：{proof}。"

        # 「尚未证明最优」必须说清卡在哪一步；兜底 / 没解也要给得出的事实。
        # 理由只从求解记录里取，不许模型或模板自己补一个「可能是工时不够」。
        from app.core.solve_explanation import explain_no_solution, explain_not_optimal

        if fallback:
            reason_line = explain_no_solution("UNKNOWN", solver)
        elif status in ("UNKNOWN", "INFEASIBLE"):
            reason_line = explain_no_solution(status, solver)
        elif status == "FEASIBLE":
            reason_line = explain_not_optimal(solver)
        else:
            reason_line = ""

        savings = summary.get("baselineSavings", {})
        savings_text = "；".join(
            f"较{name}节省 {value / 10000:.2f} 万元"
            for name, value in savings.items()
            if value is not None and value >= 0
        )
        return (
            f"{opening}\n"
            + (f"{reason_line}\n" if reason_line else "")
            + f"建议窗口：{windows or '暂无'}\n"
            + f"完成任务：{completed}/{total}；总成本：{total_cost / 10000:.2f} 万元"
            + (f"；{savings_text}" if savings_text else "")
            + "。点击下方方案卡可查看完整排程和审计证据。"
        )

    @staticmethod
    def _product_solve_evidence(raw_result: dict, model_fingerprint: str, status: str) -> SolveEvidencePayload:
        solving = raw_result.get("求解", {})
        stages = solving.get("阶段", [])
        last = stages[-1] if stages else {}
        # UNKNOWN 的原因不止"到时限"一种：兜底方案也是 UNKNOWN，但它不是
        # "还在搜"，而是"两种模式都没解出来、这份是启发式基线"。原因字段
        # 要分开写，否则审计里会把兜底误读成"只是还没算完"。
        reason = None
        if status == "UNKNOWN":
            reason = (
                "heuristic_fallback"
                if str(solving.get("方案来源") or "") == "HEURISTIC_FALLBACK"
                else "time_limit_or_search_incomplete"
            )
        return SolveEvidencePayload(
            status=status,
            hasIncumbent=bool(solving.get("有解")),
            proof=status in {"OPTIMAL", "INFEASIBLE"},
            elapsedSeconds=float(solving.get("总耗时秒", 0)),
            phase="solve",
            reason=reason,
            running=False,
            objectiveValue=last.get("目标值") or solving.get("模型成本"),
            bestBound=last.get("界"),
            modelFingerprint=model_fingerprint,
        )

    def handle_message(self, conversation_id: str, content: str) -> AgentResponse:
        """单一入口：与 submit_message 走同一条编排，不再有第二套 skill 链路。"""
        return self._submit_message(conversation_id, content)

    def get_dashboard(
        self,
        conversation_id: str,
        sort_mode: str = "latest_first",
    ) -> DashboardResponse:
        versions = self.versions.list_by_conversation(conversation_id)
        candidates = self.candidates.list_by_conversation(conversation_id)
        included_versions = {
            item.id for item in versions if item.includedInCandidatePool
        }
        visible_candidates = [
            item
            for item in candidates
            if item.versionId in included_versions and not item.isDeleted
        ]
        sorted_candidates = self.version_manager.sort_candidates(
            visible_candidates,
            sort_mode,
        )
        recommendations = self.version_manager.compute_top_recommendations(
            sorted_candidates,
            versions,
            sort_mode,
        )
        return DashboardResponse(
            conversationId=conversation_id,
            sortMode=sort_mode,
            versions=versions,
            candidateWindowPool=sorted_candidates,
            topRecommendations=recommendations,
        )

    def include_version(self, version_id: str, include: bool) -> None:
        version = self.versions.get(version_id)
        if not version:
            raise ValueError("Version not found")
        version = version.model_copy(update={"includedInCandidatePool": include})
        self.versions.update(version)

    def get_model_artifact(self, version_id: str) -> dict | None:
        if not self.model_artifacts:
            return None
        return self.model_artifacts.get(version_id)

    def get_trace(self, request_id: str) -> AgentTrace | None:
        if not self.traces:
            return None
        return self.traces.get(request_id)

    def list_traces(self, conversation_id: str, limit: int = 50) -> list[AgentTrace]:
        if not self.traces:
            return []
        return self.traces.list_by_conversation(conversation_id, limit)

    def delete_version(self, version_id: str) -> None:
        self.versions.delete(version_id)
        self.candidates.mark_deleted_by_version(version_id)

    def delete_candidate(self, candidate_id: str) -> None:
        self.candidates.delete(candidate_id)

    def reorder_candidates(self, items: list[tuple[str, int]]) -> None:
        self.candidates.reorder(items)

    def _finalize_response(
        self,
        conversation_id: str,
        content: str,
        action_type: str,
        recommended_view: str,
        new_versions,
        memory_patch: dict | None = None,
        decision_artifact: DecisionArtifactPayload | None = None,
        solve_evidence: SolveEvidencePayload | None = None,
        solve_job: SolveJobPayload | None = None,
        solve_job_id: str | None = None,
        clarification_options: list[ClarificationOptionPayload] | None = None,
    ) -> AgentResponse:
        now = datetime.now(SHANGHAI_TZ).isoformat()
        trace_id = self._finish_trace(conversation_id, action_type, solve_evidence)
        assistant_message = Message(
            id=f"msg_{uuid4().hex[:8]}",
            conversationId=conversation_id,
            role="assistant",
            content=content,
            createdAt=now,
            versionIds=[item.id for item in new_versions],
            jobId=solve_job.id if solve_job else solve_job_id,
        )
        self.messages.add(assistant_message)
        self._update_summary(conversation_id)

        conversation = self.conversations.get(conversation_id)
        if conversation:
            self.conversations.update(
                conversation.model_copy(
                    update={
                        "updatedAt": now,
                        "lastMessagePreview": content,
                        "activeView": recommended_view,
                    }
                )
            )

        dashboard = self.get_dashboard(conversation_id)
        return AgentResponse(
            assistantMessage=AssistantMessagePayload(
                id=assistant_message.id,
                content=assistant_message.content,
                versionIds=assistant_message.versionIds,
            ),
            actionType=action_type,
            recommendedView=recommended_view,
            memoryPatch=memory_patch or {},
            newVersions=new_versions,
            candidateWindowPool=dashboard.candidateWindowPool,
            topRecommendations=dashboard.topRecommendations,
            dashboardState={"sortMode": dashboard.sortMode},
            decisionArtifact=decision_artifact,
            solveEvidence=solve_evidence,
            solveJob=solve_job,
            clarificationOptions=clarification_options,
            traceId=trace_id,
            degraded=self._take_degradation(),
        )
