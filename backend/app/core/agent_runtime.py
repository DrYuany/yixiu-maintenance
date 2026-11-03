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
from app.core.agent_tools import AgentToolbox
from app.core.agent_memory import (
    ConversationSummaryRepository,
    DecisionMemoryRepository,
    UserProfileRepository,
    build_summary,
)
from app.core.agent_trace import AgentTrace, AgentTraceRepository, ToolCallRecord, new_trace_id
from app.core.conversation_agent import ConversationAgent, ConversationAgentError
from app.core.conversation_memory import (
    ConversationMemoryCache,
    ConversationMemoryRepository,
    build_memory_snapshot,
)
from app.core.decision_contract import DecisionCompiler
from app.core.demo_access import begin_llm_turn, end_llm_turn
from app.core.maintenance_product_executor import MaintenanceProductExecutor, solver_status
from app.core.model_change import (
    build_change_plan,
    build_scenario_batch,
    is_cancellation,
    is_confirmation,
    is_scenario_batch,
    is_solve_execution_command,
)
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
from app.core.planning_intent import PlanningIntentCompiler, PlanningRequest
from app.core.result_normalizer import ResultNormalizer
from app.core.solve_jobs import SolveJobRepository
from app.core.version_manager import VersionManager
from app.core.version_compare import render_compare_summary
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
from app.schemas.agent import AgentResponse, AssistantMessagePayload, DecisionArtifactPayload, DegradedPayload, Message, SolveEvidencePayload, SolveJobPayload
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
        product_executor: MaintenanceProductExecutor | None = None,
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
        self.product_executor = product_executor or MaintenanceProductExecutor()
        self.intent_compiler = PlanningIntentCompiler()
        self.job_pool = job_pool or ThreadPoolExecutor(max_workers=2, thread_name_prefix="maintenance-solve")
        self._futures: dict[str, Future] = {}
        self.conversation_agent = conversation_agent or ConversationAgent()
        self.traces = traces
        self.summary_repo = summary_repo
        self.profile_repo = profile_repo
        self.decision_repo = decision_repo
        self.memory_repo = memory_repo
        self.memory_cache = ConversationMemoryCache()
        self._tls = threading.local()

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
        trace.model = self.conversation_agent.model
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

    def _submit_message(
        self,
        conversation_id: str,
        content: str,
        selected_version_id: str | None = None,
        selected_window_id: str | None = None,
        sink: StreamSink | None = None,
        engine: str | None = None,
    ) -> AgentResponse:
        conversation = self.conversations.get(conversation_id)
        if not conversation:
            raise ValueError("Conversation not found")

        self._begin_trace(conversation_id, content)
        self._tls.engine = resolve_engine(engine)

        now = datetime.now(SHANGHAI_TZ).isoformat()
        self.messages.add(Message(
            id=f"msg_{uuid4().hex[:8]}",
            conversationId=conversation_id,
            role="user",
            content=content,
            createdAt=now,
        ))
        memory = self.memories.get_or_create(conversation_id)
        selected_version, reference_kind = self._resolve_version(
            conversation_id,
            selected_version_id,
            memory,
            content,
        )
        if reference_kind in {"id", "ordinal", "previous"} and selected_version is None:
            return self._finalize_response(
                conversation_id,
                "没有找到你提到的方案。请确认方案序号或提供 ver_ 开头的版本号。",
                "context_not_found",
                memory.activeView,
                [],
            )
        filters = dict(memory.activeFilters)
        pending = filters.get("pendingChange")
        if isinstance(pending, dict) and (
            selected_version_id or reference_kind in {"id", "ordinal", "latest", "previous"}
        ):
            pending_base = pending.get("baseVersionId")
            if pending_base and selected_version and pending_base != selected_version.id:
                filters.pop("pendingChange", None)
                pending = None
                memory = self.memories.apply_patch(memory, {"activeFilters": filters})

        if isinstance(pending, dict) and is_cancellation(content):
            filters.pop("pendingChange", None)
            self.memories.apply_patch(memory, {"activeFilters": filters})
            return self._finalize_response(
                conversation_id,
                "已取消本次模型变更，没有生成新的沙盘方案。",
                "change_cancelled",
                memory.activeView,
                [],
            )

        if isinstance(pending, dict) and is_confirmation(content):
            filters.pop("pendingChange", None)
            memory = self.memories.apply_patch(memory, {"activeFilters": filters})
            if pending.get("kind") == "batch":
                return self._start_batch_solve(
                    conversation_id=conversation_id,
                    batch=pending,
                    memory=memory,
                )
            if pending.get("kind") == "dsl":
                return self._start_dsl_solve(
                    conversation_id=conversation_id,
                    dsl=pending.get("dsl") or {},
                    content=str(pending.get("sourcePrompt") or content),
                    memory=memory,
                    base_version_id=pending.get("baseVersionId"),
                    utterance=pending.get("utterance"),
                )
            try:
                request = PlanningRequest(**pending["request"])
            except (KeyError, TypeError, ValueError):
                return self._finalize_response(
                    conversation_id,
                    "待确认的模型变更已经失效，请重新描述变更。",
                    "change_expired",
                    memory.activeView,
                    [],
                )
            return self._start_planning_solve(
                conversation_id=conversation_id,
                content=str(pending.get("sourcePrompt") or content),
                request=request,
                memory=memory,
                utterance=pending.get("utterance"),
            )

        if is_solve_execution_command(content):
            return self._start_planning_solve(
                conversation_id=conversation_id,
                content=content,
                request=self._planning_request_from_context(memory, selected_version),
                memory=memory,
            )

        # ── Agent-first：语义判断交给模型 ────────────────────────────────
        # 读类/解释类/纠错类/改排程都由模型自己决定调哪个工具（read_* 或 solve）。
        # 场景批量也交给模型：solve 支持 scenarios[]，一句里几个方案由它拆。
        # 确定性批量分支（下面 is_scenario_batch 那段）只在模型不可用时兜底——
        # 它的正则只认动词并列，认不出「加权和分层，都…」，不能当主路径。
        if (
            self.conversation_agent.enabled
            and not (is_version_switch_request(content) and selected_version)
        ):
            try:
                reply = self._answer_with_conversation_agent(
                    conversation_id=conversation_id,
                    content=content,
                    history=self.messages.list_by_conversation(conversation_id),
                    memory=memory,
                    selected_version=selected_version,
                    sink=sink,
                )
                # 这一轮模型真的把活派出去了。那份响应带着 jobId、求解时限，
                # 还有"超时不会说成无解"那句话——比让模型再转述一遍准，
                # 所以直接用它，模型的话不再贴一遍（否则用户会看到两段"已启动"）。
                submitted = getattr(self._tls, "submitted_response", None)
                if submitted is not None:
                    return submitted

                latest = self.memories.get_or_create(conversation_id)
                pending_now = (latest.activeFilters or {}).get("pendingChange")
                created_now = (
                    isinstance(pending_now, dict)
                    and pending_now.get("sourcePrompt") == content
                )
                return self._finalize_response(
                    conversation_id,
                    reply,
                    "change_preview" if created_now else "assistant_reply",
                    latest.activeView,
                    [],
                )
            except ConversationAgentError as exc:
                # 以前这里是裸 pass：调用失败静默滑到下面的确定性兜底，
                # 界面上一模一样，用户分不出"模型听懂了"和"正则猜的"。
                self._note_degradation(
                    "agent_unavailable",
                    "模型这一轮没答上来，下面是规则兜底生成的答复，可能不如模型答得全。",
                    str(exc),
                )

        compare_reply = self._compare_versions_reply(conversation_id, content, selected_version)
        model_question = detect_model_question(content)

        if compare_reply is not None:
            return self._finalize_response(
                conversation_id,
                compare_reply,
                "version_compare",
                memory.activeView,
                [],
            )

        if model_question:
            if model_question == "capability":
                return self._answer_capability_question(
                    conversation_id,
                    content,
                    memory,
                )
            if model_question == "environment":
                return self._answer_environment_question(
                    conversation_id,
                    content,
                    memory,
                    selected_version,
                )
            if model_question == "structure" and not selected_version:
                return self._answer_structure_question(
                    conversation_id,
                    content,
                    memory,
                )
            if not selected_version:
                if model_question == "parameter":
                    return self._answer_operations_question(
                        conversation_id,
                        content,
                        "site",
                        "parameters",
                        selected_version,
                        memory,
                        sink=sink,
                    )
                return self._finalize_response(
                    conversation_id,
                    "当前会话还没有可查询的排程方案。请先生成方案，之后我才能解释模型、参数或结果。",
                    "model_query_empty",
                    memory.activeView,
                    [],
                )
            if model_question == "structure":
                model_question = "formula"
            if model_question in {"parameter", "status"}:
                return self._answer_model_question(
                    conversation_id,
                    content,
                    model_question,
                    selected_version,
                    memory,
                )
            return self._answer_agent_question(
                conversation_id,
                content,
                model_question,
                selected_version,
                memory,
                sink=sink,
            )

        if is_version_switch_request(content) and selected_version:
            ordered = chronological_versions(
                self.versions.list_by_conversation(conversation_id)
            )
            plan_number = next(
                (index for index, item in enumerate(ordered, start=1) if item.id == selected_version.id),
                None,
            )
            plan_label = f"{plan_number:02d}" if plan_number else selected_version.id
            filters.pop("pendingChange", None)
            self.memories.apply_patch(memory, {
                "selectedVersionIds": [selected_version.id],
                "activeFilters": filters,
            })
            return self._finalize_response(
                conversation_id,
                (
                    f"已切换到方案 {plan_label}"
                    f"（{selected_version.id}）。后续“这版/这个模型”会绑定该版本；"
                    "只查看和讨论不会触发新求解。"
                ),
                "context_switch",
                memory.activeView,
                [],
            )

        # 一句话里同时提了多个场景时，先按场景拆开，再逐个预览。
        if is_scenario_batch(content):
            current_batch = self._planning_request_from_context(memory, selected_version)
            batch = build_scenario_batch(
                content,
                current_batch,
                selected_version.id if selected_version else memory.latestResultId,
                self.intent_compiler,
            )
            filters["pendingChange"] = {
                "kind": "batch",
                "baseVersionId": batch.base_version_id,
                "scenarios": [
                    {
                        "label": item.label,
                        "request": item.request.to_payload(),
                        "operations": item.operations,
                        "notes": item.notes,
                        "sourcePrompt": item.source_prompt,
                    }
                    for item in batch.scenarios
                ],
                "compare": batch.compare,
                "preview": batch.preview,
                "sourcePrompt": content,
            }
            self.memories.apply_patch(
                memory,
                {
                    "currentGoal": content,
                    "currentIntent": "scenario_batch",
                    "activeFilters": filters,
                },
            )
            return self._finalize_response(
                conversation_id,
                batch.preview,
                "change_preview",
                memory.activeView,
                [],
            )

        planning_request = self.intent_compiler.is_planning_request(content)
        if not planning_request:
            operations_question = detect_operations_question(content)
            if operations_question:
                kind, scope = operations_question
                return self._answer_operations_question(
                    conversation_id,
                    content,
                    kind,
                    scope,
                    selected_version,
                    memory,
                    sink=sink,
                )
            # DSL 增量（第一期）：约束/自定义目标类修改，走预览→确认→求解。
            dsl_result = self._try_dsl_parse(
                content, sink=sink,
                context_block=self._dsl_context_block(conversation_id, memory, selected_version),
            )
            if dsl_result is not None and dsl_result.route == "reject":
                # 越界诉求：明确拒绝，不再回退旧 conversation_agent（避免二次 LLM）。
                if sink:
                    sink.stage("route", "判断意图路由", "done", "reject")
                return self._finalize_response(
                    conversation_id,
                    (
                        "这类诉求超出了当前排程模型的能力范围（比如非线性/无意义的修改），"
                        "我没法把它编译成约束，也不会假装能排。\n\n"
                        "可以试试这些我能处理的：具体任务的时间（最早/期限）、互斥或先后、"
                        "固定作业线、预算、目标策略或参数调整。"
                    ),
                    "change_rejected",
                    memory.activeView,
                    [],
                )
            if dsl_result is not None and dsl_result.route == "clarify" and _looks_like_modification(content):
                # 低置信/歧义诉求：直接澄清，不再回退旧 conversation_agent（避免二次 LLM）。
                if sink:
                    sink.stage("route", "判断意图路由", "done", "clarify")
                return self._finalize_response(
                    conversation_id,
                    (
                        "这句话我有点拿不准，没法确定性地编译成约束。请说得更明确一点，比如：\n"
                        "1. 具体任务编号（如 任务001）或风机/部件；\n"
                        "2. 约束类型（最早/期限/互斥/先后/作业线/预算）或要改的参数；\n"
                        "3. 相对基准（例如“顺延”相对哪一版方案）。"
                    ),
                    "change_clarification",
                    memory.activeView,
                    [],
                )
            if (
                dsl_result is not None
                and dsl_result.route == "dsl"
                and dsl_result.dsl is not None
            ):
                current = self._planning_request_from_context(memory, selected_version)
                pending_for_merge = filters.get("pendingChange")
                if isinstance(pending_for_merge, dict) and pending_for_merge.get("request"):
                    try:
                        current = PlanningRequest(**pending_for_merge["request"])
                    except (KeyError, TypeError, ValueError):
                        pass
                base_version_id = selected_version.id if selected_version else memory.latestResultId
                from dsl_core.planning_bridge import (
                    custom_objectives,
                    dsl_to_request,
                    merge_dsl,
                    request_to_dsl,
                )
                from app.core.model_change import _diff_requests, _format_preview

                if sink:
                    sink.stage("dsl.validate", "校验并编译 DSL", "running")
                updated, residual, timeline = dsl_to_request(dsl_result.dsl, current)
                if sink:
                    sink.stage("dsl.validate", "校验并编译 DSL", "done")
                expressible = not (residual or timeline or custom_objectives(dsl_result.dsl))
                operations = _diff_requests(current, updated)
                pending_dsl = (
                    pending_for_merge.get("dsl")
                    if isinstance(pending_for_merge, dict)
                    and pending_for_merge.get("kind") == "dsl"
                    else None
                )
                if pending_dsl:
                    # 已有未确认的 DSL 约束：把新修改并进来，而不是静默覆盖
                    merged = merge_dsl(pending_dsl, request_to_dsl(updated), dsl_result.dsl)
                    filters["pendingChange"] = {
                        "kind": "dsl",
                        "dsl": merged,
                        "source": dsl_result.source,
                        "baseVersionId": base_version_id,
                        "sourcePrompt": content,
                        "preview": self._format_dsl_preview(merged),
                    }
                    self.memories.apply_patch(memory, {
                        "currentIntent": "dsl_change",
                        "activeFilters": filters,
                    })
                    return self._finalize_response(
                        conversation_id,
                        filters["pendingChange"]["preview"],
                        "dsl_preview",
                        memory.activeView,
                        [],
                    )
                if expressible and operations:
                    filters["pendingChange"] = {
                        "baseVersionId": base_version_id,
                        "request": updated.to_payload(),
                        "operations": operations,
                        "preview": _format_preview(operations, base_version_id),
                        "sourcePrompt": content,
                    }
                    self.memories.apply_patch(memory, {
                        "activeFocusTag": updated.strategy,
                        "activeFilters": filters,
                    })
                    return self._finalize_response(
                        conversation_id,
                        filters["pendingChange"]["preview"],
                        "change_preview",
                        memory.activeView,
                        [],
                    )
                filters["pendingChange"] = {
                    "kind": "dsl",
                    "dsl": dsl_result.dsl,
                    "source": dsl_result.source,
                    "baseVersionId": base_version_id,
                    "sourcePrompt": content,
                    "preview": self._format_dsl_preview(dsl_result.dsl),
                }
                self.memories.apply_patch(
                    memory,
                    {
                        "currentIntent": "dsl_change",
                        "activeFilters": filters,
                    },
                )
                return self._finalize_response(
                    conversation_id,
                    filters["pendingChange"]["preview"],
                    "dsl_preview",
                    memory.activeView,
                    [],
                )
            history = self.messages.list_by_conversation(conversation_id)
            try:
                reply = self._answer_with_conversation_agent(
                    conversation_id=conversation_id,
                    content=content,
                    history=history,
                    memory=memory,
                    selected_version=selected_version,
                    sink=sink,
                )
            except ConversationAgentError as exc:
                self._note_degradation(
                    "compare_fallback",
                    "对比问题没能走模型，下面是按版本记录逐项比出来的结果。",
                    str(exc),
                )
                reply = (
                    self._fallback_compare_reply(
                        conversation_id,
                        content,
                        selected_version,
                    )
                    or (
                        "正式排程使用 OR-Tools CP-SAT 求解有界整数调度模型。"
                        "自然语言会先编译成固定模型契约，再进行求解和独立复核；"
                        "OPTIMAL、FEASIBLE、UNKNOWN 与 INFEASIBLE 会严格区分。"
                    )
                )
            memory = self.memories.get_or_create(conversation_id)
            self.memories.apply_patch(
                memory,
                {
                    "currentGoal": content,
                    "currentIntent": "agent_answer",
                },
            )
            return self._finalize_response(
                conversation_id,
                reply,
                "assistant_reply",
                memory.activeView,
                [],
            )

        current = self._planning_request_from_context(memory, selected_version)
        base_version_id = selected_version.id if selected_version else memory.latestResultId
        # 多轮上下文：未确认的变更继续作为基准累加（"船租涨20%"后再补"最多两条线"）
        if isinstance(pending, dict) and pending.get("request"):
            try:
                current = PlanningRequest(**pending["request"])
                base_version_id = pending.get("baseVersionId") or base_version_id
            except (KeyError, TypeError, ValueError):
                pass
        # 第三期：统一解析器优先（旧正则作为其规则层），DSL 表达不了的再回退旧规划器。
        change_plan = self._change_plan_from_dsl(
            content, current, base_version_id, sink=sink,
            context_block=self._dsl_context_block(conversation_id, memory, selected_version),
        )
        dsl_pending: dict[str, Any] | None = None
        if change_plan is None:
            change_plan = build_change_plan(
                content,
                current,
                base_version_id,
                self.intent_compiler,
            )
        elif change_plan.operations:
            # DSL 里含 PlanningRequest 表达不了的约束/目标（co_occur 等）→ 走 DSL 待确认
            from dsl_core.planning_bridge import (
                custom_objectives,
                dsl_to_request,
                merge_dsl,
                request_to_dsl,
            )

            dsl = self._try_dsl_parse(content, sink=sink)
            if dsl is not None and dsl.dsl:
                residual, timeline = dsl_to_request(dsl.dsl, current)[1:]
                pending_dsl = (
                    pending.get("dsl")
                    if isinstance(pending, dict) and pending.get("kind") == "dsl"
                    else None
                )
                if residual or timeline or custom_objectives(dsl.dsl) or pending_dsl:
                    merged = merge_dsl(
                        pending_dsl,
                        request_to_dsl(change_plan.request),
                        dsl.dsl,
                    )
                    dsl_pending = {
                        "kind": "dsl",
                        "dsl": merged,
                        "baseVersionId": base_version_id,
                        "sourcePrompt": content,
                        "preview": self._format_dsl_preview(merged),
                    }
        if dsl_pending is not None:
            filters["pendingChange"] = dsl_pending
            self.memories.apply_patch(memory, {
                "currentIntent": "dsl_change",
                "activeFilters": filters,
            })
            return self._finalize_response(
                conversation_id,
                dsl_pending["preview"],
                "dsl_preview",
                memory.activeView,
                [],
            )
        if not change_plan.operations:
            return self._finalize_response(
                conversation_id,
                change_plan.preview,
                "change_clarification",
                memory.activeView,
                [],
            )
        filters["pendingChange"] = {
            "baseVersionId": base_version_id,
            "request": change_plan.request.to_payload(),
            "operations": change_plan.operations,
            "preview": change_plan.preview,
            "sourcePrompt": change_plan.source_prompt,
        }
        self.memories.apply_patch(memory, {
            "activeFocusTag": change_plan.request.strategy,
            "activeFilters": filters,
        })
        return self._finalize_response(
            conversation_id,
            change_plan.preview,
            "change_preview",
            memory.activeView,
            [],
        )

    def _start_batch_solve(
        self,
        *,
        conversation_id: str,
        batch: dict[str, Any],
        memory,
    ) -> AgentResponse:
        """一句话里提了多个场景时，一次确认后按场景顺序逐个求解。"""
        scenarios = list(batch.get("scenarios") or [])
        if not scenarios:
            return self._finalize_response(
                conversation_id,
                "这批场景已经失效，请重新描述要算的几个方案。",
                "change_expired",
                memory.activeView,
                [],
            )
        first = scenarios[0]
        try:
            request = PlanningRequest(**first["request"])
        except (KeyError, TypeError, ValueError):
            return self._finalize_response(
                conversation_id,
                "这批场景已经失效，请重新描述要算的几个方案。",
                "change_expired",
                memory.activeView,
                [],
            )
        pending_batch = {
            "remaining": scenarios[1:],
            "total": len(scenarios),
            "completed": [],
            "compare": bool(batch.get("compare")),
            "sourcePrompt": batch.get("sourcePrompt") or "",
            "baseVersionId": batch.get("baseVersionId"),
        }
        return self._start_planning_solve(
            conversation_id=conversation_id,
            content=batch.get("sourcePrompt") or "",
            request=request,
            memory=memory,
            pending_batch=pending_batch,
        )

    def _continue_batch(self, conversation_id: str, job_id: str, version_id: str | None) -> None:
        """一个场景算完后，接着算下一个；全部算完则自动对比。"""
        memory = self.memories.get_or_create(conversation_id)
        batch = (memory.activeFilters or {}).get("pendingBatch")
        if not isinstance(batch, dict):
            return
        completed = list(batch.get("completed") or [])
        if version_id:
            completed.append(version_id)
        remaining = list(batch.get("remaining") or [])
        filters = dict(memory.activeFilters)
        if not remaining:
            filters.pop("pendingBatch", None)
            self.memories.apply_patch(memory, {"activeFilters": filters})
            if batch.get("compare"):
                self._emit_batch_compare(conversation_id, completed)
            return
        nxt = remaining[0]
        filters["pendingBatch"] = {
            **batch,
            "remaining": remaining[1:],
            "completed": completed,
        }
        self.memories.apply_patch(memory, {"activeFilters": filters})
        try:
            request = PlanningRequest(**nxt["request"])
        except (KeyError, TypeError, ValueError):
            self._continue_batch(conversation_id, job_id, None)
            return
        self._start_planning_solve(
            conversation_id=conversation_id,
            content=batch.get("sourcePrompt") or "",
            request=request,
            memory=self.memories.get_or_create(conversation_id),
            pending_batch={
                **batch,
                "remaining": remaining[1:],
                "completed": completed,
            },
        )

    def batch_progress(self, conversation_id: str) -> dict[str, int] | None:
        """当前会话还有没有没算完的场景。前端靠它决定要不要继续轮询。"""
        memory = self.memories.get_or_create(conversation_id)
        batch = (memory.activeFilters or {}).get("pendingBatch")
        if not isinstance(batch, dict):
            return None
        total = int(batch.get("total") or 0)
        if total <= 0:
            return None
        done = len(batch.get("completed") or [])
        return {"position": min(done + 1, total), "total": total}

    def _emit_batch_compare(self, conversation_id: str, version_ids: list[str]) -> None:
        """两个以上场景都算完后，用现成的比较器直接出对比，不再额外问一轮模型。"""
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
            f"这两个场景都算完了，下面是它们的对比：\n\n" + "\n\n".join(blocks),
            "batch_compare",
            memory.activeView,
            [item for pair in pairs for item in pair],
        )

    _DSL_LABELS = None  # 兼容旧引用；渲染已下沉到 change_planning

    @classmethod
    def _format_dsl_preview(cls, dsl: dict[str, Any]) -> str:
        from app.core.change_planning import format_dsl_preview

        return format_dsl_preview(dsl)

    def _llm_first_parse(self, content: str, sink: StreamSink | None = None):
        """LLM 主导引擎：一句话直接进 llmfirst.pipeline.understand，产出 (route, dsl)。

        未配置 LLM key 或 llmfirst 包不可用时返回 None，回退规则主导路径（旧路径）。
        """
        try:
            import llmfirst.client as llm_client
            import llmfirst.pipeline as llm_pipeline
            from app.core.llm_parser import ParseResult
        except Exception:  # pragma: no cover - llmfirst 未接好时静默回退
            return None

        if not llm_client.configured():
            return None

        try:
            if sink:
                sink.stage("route", "判断意图路由", "done", "llm-first")
            outcome = llm_pipeline.understand(content)
            route = outcome.route if outcome.route in ("dsl", "not_change", "reject", "clarify") else "clarify"
            dsl = (outcome.as_dict().get("dsl") or None) if route == "dsl" else None
            return ParseResult(
                dsl=dsl,
                source="llm",
                route=route,
                confidence=0.0 if outcome.error else 1.0,
                latency_ms=float(outcome.latency_ms or 0),
            )
        except Exception:  # noqa: BLE001 - LLM 引擎失败 → 回退规则主导，不二次 LLM
            return None

    def _try_dsl_parse(self, content: str, sink: StreamSink | None = None, context_block: str | None = None):
        """DSL 三层解析（增量能力）。解析不了/LLM 不可用返回 None，回退现有路径。

        规则层与门控是确定性的，不产生 API 调用；只有真正的新话术才走到 LLM。
        sink 用于把解析各阶段（规则/桥接/检索/LLM）作为 stage 事件推给前端。
        context_block 是每轮动态上下文（快照清单 + 当前方案 + 待确认变更）。

        LLM 失败（超时/网络/坏响应）降级为 clarify，不再回退旧 conversation_agent
        二次调 LLM；只有「未配置 key」（ParseUnavailable）才返回 None 走旧路径。
        """
        if resolve_engine(getattr(self._tls, "engine", None)) == "llm":
            return self._llm_first_parse(content, sink=sink)

        try:
            from app.core.llm_parser import ParseResult, ParseUnavailable, parse as dsl_parse
        except Exception:  # pragma: no cover - DSL 包缺失时静默回退
            return None

        def on_stage(stage_id: str, label: str, status: str = "running", detail: str | None = None) -> None:
            if sink:
                sink.stage(stage_id, label, status, detail)

        try:
            return dsl_parse(content, on_stage=on_stage, context_block=context_block)
        except ParseUnavailable:
            return None
        except Exception:  # noqa: BLE001 - LLM 调用失败 → 澄清，不二次 LLM
            if sink:
                sink.stage("parse.llm", "调用大模型解析", "done", "失败，降级澄清")
            return ParseResult(
                dsl=None, source="llm", route="clarify", confidence=0.0,
                source_scores={"llm": 0.0},
            )

    def _dsl_context_block(self, conversation_id: str, memory, selected_version) -> str:
        """把「现在是什么状态」拼成上下文：快照清单 + 当前方案 + 待确认变更 + 最近对话。"""
        try:
            from dsl_core.registry import schema_prompt as sp
        except Exception:  # pragma: no cover
            return ""

        blocks = [sp.snapshot_block(_entity_snapshot())]
        context: dict[str, Any] = {}
        if selected_version is not None:
            label = getattr(selected_version, "id", None)
            try:
                ordered = chronological_versions(
                    self.versions.list_by_conversation(conversation_id)
                )
                for index, item in enumerate(ordered, start=1):
                    if item.id == selected_version.id:
                        label = f"方案{index:02d}（{selected_version.id}）"
                        break
            except Exception:  # noqa: BLE001
                pass
            context["plan_label"] = label
        filters = getattr(memory, "activeFilters", None) or {}
        pending = filters.get("pendingChange") if isinstance(filters, dict) else None
        if isinstance(pending, dict):
            context["pending"] = str(pending.get("preview") or pending.get("request") or pending)[:300]
        try:
            history = self.messages.list_by_conversation(conversation_id)[-3:]
            turns, user_text = [], None
            for item in history:
                if item.role == "user":
                    user_text = item.content[:120]
                elif user_text is not None:
                    turns.append({"user": user_text, "assistant": item.content[:120]})
                    user_text = None
            context["history"] = turns
        except Exception:  # noqa: BLE001
            pass
        ctx_block = sp.context_block(context)
        if ctx_block:
            blocks.append(ctx_block)
        return "\n\n".join(blocks)

    def _change_plan_from_dsl(self, content: str, current, base_version_id,
                              sink: StreamSink | None = None,
                              context_block: str | None = None):
        """统一解析器 → 变更计划（第三期）。DSL 表达不了或没有变化时返回 None 走旧路径。"""
        parsed = self._try_dsl_parse(content, sink=sink, context_block=context_block)
        if parsed is None or parsed.route != "dsl" or not parsed.dsl:
            return None
        try:
            from dsl_core.planning_bridge import dsl_to_request
            from app.core.model_change import ModelChangePlan, _diff_requests, _format_preview

            updated, residual, timeline = dsl_to_request(parsed.dsl, current)
        except Exception:  # pragma: no cover - 桥接失败回退旧路径
            return None
        operations = _diff_requests(current, updated)
        if not operations and not residual and not timeline:
            return None
        preview = (
            self._format_dsl_preview(parsed.dsl)
            if (residual or timeline)
            else _format_preview(operations, base_version_id)
        )
        return ModelChangePlan(
            request=updated,
            operations=operations,
            preview=preview,
            source_prompt=content,
            base_version_id=base_version_id,
            notes=[],
            errors=[],
        )

    def _start_dsl_solve(
        self,
        *,
        conversation_id: str,
        dsl: dict[str, Any],
        content: str,
        memory,
        base_version_id: str | None = None,
        utterance: str | None = None,
    ) -> AgentResponse:
        """DSL 变更确认后的求解：绑定预览时的基版，注入 DSL 约束/目标。"""
        base_version = self.versions.get(base_version_id) if base_version_id else None
        if base_version is None and memory.latestResultId:
            base_version = self.versions.get(memory.latestResultId)
        request = self._planning_request_from_context(memory, base_version)
        return self._start_planning_solve(
            conversation_id=conversation_id,
            content=content,
            request=request,
            memory=memory,
            dsl=dsl,
            utterance=utterance,
        )

    def _start_planning_solve(
        self,
        *,
        conversation_id: str,
        content: str,
        request: PlanningRequest,
        memory,
        pending_batch: dict[str, Any] | None = None,
        dsl: dict[str, Any] | None = None,
        utterance: str | None = None,
    ) -> AgentResponse:
        if not self.jobs:
            return self._finalize_response(
                conversation_id,
                "求解服务未配置，这次无法发起真实求解；没有生成任何方案。",
                "solve_unavailable",
                memory.activeView,
                [],
            )
        try:
            request, data_snapshot = self.product_executor.prepare(request)
        except MaintenanceProductExecutionError as exc:
            # 数据改动没通过校验时，如实说明，不把它说成无解。
            return self._finalize_response(
                conversation_id,
                f"这次没有开始求解，因为数据改动没有通过校验：{exc}",
                "change_rejected",
                memory.activeView,
                [],
            )
        request_payload = request.to_payload()
        budget = dict((data_snapshot or {}).get("budget") or {})
        budget_seconds = int(budget.get("seconds") or 60)
        budget_gap = budget.get("gapLimit")
        budget_note = f"{budget_seconds} 秒（{budget.get('secondsSource') or '参数总表'}）"
        if isinstance(budget_gap, (int, float)) and budget_gap > 0:
            budget_note += f"，成本阶段容差 {budget_gap:.0%}"
        history = self.messages.list_by_conversation(conversation_id)
        artifact = DecisionCompiler().compile(
            {
                "prompt": content,
                "history": [item.model_dump() for item in history],
                "working_memory": memory.model_dump(),
                "business_state": data_snapshot,
                "schema_version": "maintenance-plan-v1",
                "compiler_version": "deterministic-intent-v1",
                "model_version": "registered-language",
                "prompt_version": "none",
                "solver_profile": {"name": "ortools-cp-sat", "random_seed": 17, "workers": 1, "max_seconds": budget_seconds},
            },
            {"intent": "maintenance_plan", "request": request_payload, "dsl": dsl or {}},
            {"skill": SKILL_NAME, "request": request_payload, "data_snapshot": data_snapshot, "dsl": dsl or {}},
        )
        # job.request 保持 request 字段在顶层（兼容现有断言），DSL 附加在其上。
        # budgetSeconds 是本次真正会用的时限（已过参数总表与部署上限），
        # 进度条和"已超时限"提示都读它，不再各自猜一个默认值。
        job_payload = {
            **request_payload,
            "budgetSeconds": budget_seconds,
            "budget": budget,
        }
        if dsl is not None:
            job_payload.update({"kind": "dsl", "dsl": dsl})
        job = self.jobs.create(conversation_id, job_payload, artifact.model_fingerprint, snapshot=data_snapshot)
        filters = dict(memory.activeFilters)
        filters["planningRequest"] = request_payload
        self.memories.apply_patch(memory, {
            "currentIntent": "maintenance_plan",
            "activeFocusTag": request.strategy,
            "activeFilters": filters,
        })

        if pending_batch is not None:
            latest = self.memories.get_or_create(conversation_id)
            batch_filters = dict(latest.activeFilters)
            batch_filters["pendingBatch"] = pending_batch
            self.memories.apply_patch(latest, {"activeFilters": batch_filters})

        if pending_batch is not None:
            position = len(pending_batch.get("completed") or []) + 1
            total = pending_batch.get("total") or 1
            batch_message = (
                f"这是第 {position}/{total} 个场景；"
                f"全部算完后我会把 {total} 个场景放在一起对比。"
                if total > 1
                else ""
            )
        else:
            batch_message = ""
        response = self._finalize_response(
            conversation_id,
            (
                f"已按“{request.strategy}”编译为固定模型契约，"
                f"{'附带自定义约束/目标，' if dsl else ''}真实 CP-SAT 求解已启动。"
                f"{batch_message}任务 {job.id}，本次求解预算 {budget_note}；"
                "到时限我会如实说明是已证明最优、还是只找到可行解，不会把超时说成无解。"
            ),
            "solve_started",
            "P01",
            [],
            decision_artifact=DecisionArtifactPayload(
                canonicalContext=artifact.canonical_context,
                canonicalDecision=artifact.canonical_decision,
                canonicalModel=artifact.canonical_model,
                contextFingerprint=artifact.context_fingerprint,
                decisionFingerprint=artifact.decision_fingerprint,
                modelFingerprint=artifact.model_fingerprint,
            ),
            solve_evidence=SolveEvidencePayload(
                status="UNKNOWN",
                running=True,
                phase="queued",
                modelFingerprint=artifact.model_fingerprint,
            ),
            solve_job=SolveJobPayload(**job.model_dump()),
        )
        self._futures[job.id] = self.job_pool.submit(
            self._execute_job,
            job.id,
            content,
            request,
            artifact,
            dsl,
            utterance,
        )
        return response

    def _submit_pending_change(
        self,
        conversation_id: str,
        pending: dict[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        """模型在循环里请求"把这份变更派出去"。

        走的是和用户点确认**同一套分派**（_start_dsl_solve / _start_planning_solve）。
        不另写一条：上一轮 DSL 双路径已经证明，同一件事两份实现迟早给出两个答案。

        派成功的那份 AgentResponse 存在 thread-local 上。外层拿到它就直接用——
        它带着 jobId、求解时限和"超时不会说成无解"那句话，比让模型转述一遍准。
        """
        memory = self.memories.get_or_create(conversation_id)
        content = str(pending.get("sourcePrompt") or "")
        if reason:
            # 授权来源进 trace，事后能回答"这次是谁让派的"。
            trace = getattr(self._tls, "trace", None)
            if trace is not None:
                trace.message = f"{trace.message}｜提交依据：{reason}"
        try:
            if pending.get("kind") == "batch":
                response = self._start_batch_solve(
                    conversation_id=conversation_id,
                    batch=pending,
                    memory=memory,
                )
            elif pending.get("kind") == "dsl":
                response = self._start_dsl_solve(
                    conversation_id=conversation_id,
                    dsl=pending.get("dsl") or {},
                    content=content,
                    memory=memory,
                    base_version_id=pending.get("baseVersionId"),
                    utterance=pending.get("utterance"),
                )
            else:
                request = PlanningRequest(**pending["request"])
                response = self._start_planning_solve(
                    conversation_id=conversation_id,
                    content=content,
                    request=request,
                    memory=memory,
                    utterance=pending.get("utterance"),
                )
        except (KeyError, TypeError, ValueError) as exc:
            return {"ok": False, "message": f"待确认的变更已经失效：{exc}"}

        self._tls.submitted_response = response
        job = getattr(response, "solveJob", None)
        queued = len(pending.get("scenarios") or []) if pending.get("kind") == "batch" else 1
        return {
            "ok": True,
            "jobId": getattr(job, "id", None),
            "message": (
                f"{queued} 个场景已排队，会按顺序逐个求解；全部算完后自动出对比。"
                if queued > 1
                else "真实求解已启动，正在后台跑。"
            ),
            "authorizedBy": reason,
        }

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

    @staticmethod
    def _planning_request_from_context(memory, version) -> PlanningRequest:
        if version and isinstance(version.parameterSnapshot, dict):
            known = {
                key: value
                for key, value in version.parameterSnapshot.items()
                if key in PlanningRequest.__dataclass_fields__
            }
            if known:
                return PlanningRequest(**known)
        payload = (memory.activeFilters or {}).get("planningRequest")
        if isinstance(payload, dict):
            known = {
                key: value
                for key, value in payload.items()
                if key in PlanningRequest.__dataclass_fields__
            }
            return PlanningRequest(**known)
        return PlanningRequest()

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
        toolbox = self._build_toolbox(conversation_id, version, None)
        window = self._environment_window(content)
        arguments: dict[str, Any] = (
            {"detail": "window", "window": window}
            if window
            else {
                "days": 30,
                "detail": "chart",
                "defaultRange": self._environment_chart_range(content),
            }
        )
        result = toolbox.execute("read_environment", arguments)
        markdown = (result.get("data") or {}).get("markdown")
        if result.get("status") != "ok" or not markdown:
            reply = (
                (result.get("data") or {}).get("message")
                or "这次没有取到风况或电价序列，不能给出具体数值。"
            )
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
        toolbox = self._build_toolbox(conversation_id, selected_version, sink)
        if kind == "alarms":
            result = toolbox.execute("read_alarms", alarm_arguments(content))
            action = "alarm_answer"
        else:
            arguments: dict[str, Any] = {"scope": scope or "overview"}
            if scope == "parameters":
                arguments["query"] = content
            result = toolbox.execute("read_site", arguments)
            action = "site_answer"
        markdown = (result.get("data") or {}).get("markdown")
        if result.get("status") != "ok" or not markdown:
            reply = (
                (result.get("data") or {}).get("message")
                or "这次没有取到现场数据，不能给出具体清单或数值。"
            )
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

    def _build_toolbox(
        self,
        conversation_id: str,
        selected_version,
        sink: StreamSink | None,
        user_message: str = "",
    ) -> AgentToolbox:
        def on_tool(name: str, _result: dict[str, Any]) -> None:
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
            on_submit_change=self._submit_pending_change,
            user_message=user_message,
        )

    def _answer_with_conversation_agent(
        self,
        *,
        conversation_id: str,
        content: str,
        history,
        memory,
        selected_version,
        sink: StreamSink | None,
    ) -> str:
        # 原话一起交给工具盒：solve 要用它跟解析出的 DSL 对账，
        # 不能拿模型转写的 instruction 当原话。
        toolbox = self._build_toolbox(conversation_id, selected_version, sink, user_message=content)
        snapshot = self.memory_cache.get(conversation_id, len(history))
        if snapshot is None:
            snapshot = build_memory_snapshot(
                conversation_id,
                history,
                memory,
                self.versions.list_by_conversation(conversation_id),
                llm_summary=self.conversation_agent.summarize(history) or "",
            )
            self.memory_cache.put(snapshot)
            if self.memory_repo is not None:
                try:
                    self.memory_repo.upsert(snapshot)
                except Exception:  # pragma: no cover - 记忆落库失败不阻断对话
                    pass
        reply = self.conversation_agent.answer(
            message=content,
            history=history,
            memory=memory,
            toolbox=toolbox,
            selected_version=selected_version,
            snapshot=snapshot,
            on_token=sink.token if sink else None,
            on_status=sink.status if sink else None,
        )
        if toolbox.pending_touched:
            if toolbox.pending_change:
                self._persist_agent_pending(conversation_id, toolbox.pending_change)
            else:
                self._clear_persisted_pending(conversation_id)
        return reply

    def _clear_persisted_pending(self, conversation_id: str) -> None:
        """这一轮没能给出预览，就把上一轮遗留的待确认一起作废。

        只清 Toolbox 里的字段不够：下一句“确认”读的是 working memory，
        不清的话派出去的还是上一句留下的变更。
        """
        latest = self.memories.get_or_create(conversation_id)
        filters = dict(latest.activeFilters)
        if "pendingChange" not in filters:
            return
        filters.pop("pendingChange", None)
        self.memories.apply_patch(latest, {"activeFilters": filters})

    def _persist_agent_pending(self, conversation_id: str, pending: dict[str, Any]) -> None:
        """agent 路径产出的待确认变更落库。

        DSL 待确认（kind=dsl）没有 request 段，旧实现硬取 request["strategy"] 会 500。
        """
        latest = self.memories.get_or_create(conversation_id)
        filters = dict(latest.activeFilters)
        filters["pendingChange"] = pending
        patch: dict[str, Any] = {"activeFilters": filters}
        request_payload = pending.get("request")
        if isinstance(request_payload, dict) and request_payload.get("strategy"):
            patch["activeFocusTag"] = request_payload["strategy"]
        self.memories.apply_patch(latest, patch)

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

        toolbox = self._build_toolbox(conversation_id, selected_version, None)
        resolved = []
        ordinals = re.findall(r"方案\s*0*(\d{1,3})", content)
        if len(ordinals) >= 2:
            for ordinal in ordinals[:2]:
                version = toolbox.resolve_version(f"方案{ordinal}")
                if version and version.id not in {item.id for item in resolved}:
                    resolved.append(version)
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
            lines.append(render_compare_summary(toolbox.compare_pair(left, right)))
        except Exception as exc:  # noqa: BLE001
            lines.append(f"（结果层对比失败：{exc}）")
        return "\n\n".join(lines)

    def _fallback_compare_reply(
        self,
        conversation_id: str,
        content: str,
        selected_version,
    ) -> str | None:
        if not any(token in content for token in ("对比", "比较", "差异", "区别")):
            return None
        ordinals = re.findall(r"方案\s*0*(\d{1,3})", content)
        if len(ordinals) < 2:
            return None
        toolbox = self._build_toolbox(conversation_id, selected_version, None)
        resolved = []
        for ordinal in ordinals[:2]:
            version = toolbox.resolve_version(f"方案{ordinal}")
            if version and version.id not in {item.id for item in resolved}:
                resolved.append(version)
        if len(resolved) < 2:
            return None
        return render_compare_summary(toolbox.compare_pair(resolved[0], resolved[1]))

    def _answer_agent_question(
        self,
        conversation_id: str,
        content: str,
        question_kind: str,
        version,
        memory,
        sink: StreamSink | None = None,
    ) -> AgentResponse:
        artifact = self.get_model_artifact(version.id)
        if question_kind in {"formula", "parameter"} and not artifact:
            return self._answer_model_question(
                conversation_id,
                content,
                question_kind,
                version,
                memory,
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
                "模型没能解释这个问题，下面是按模型档案直接读出来的答案。",
                str(exc),
            )
            return self._answer_model_question(
                conversation_id,
                content,
                question_kind,
                version,
                memory,
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
            self.product_executor.cancel(job_id)
            future = self._futures.get(job_id)
            if future:
                future.cancel()
            return self.jobs.mark_cancelled(job_id)
        if action == "use_incumbent" and not job.hasIncumbent:
            raise ValueError("当前尚无可用方案")
        if action == "use_incumbent":
            return job
        raise ValueError("Unknown solve action")

    def _execute_job(
        self,
        job_id: str,
        content: str,
        request: PlanningRequest,
        artifact,
        dsl: dict[str, Any] | None = None,
        utterance: str | None = None,
    ) -> None:
        assert self.jobs is not None
        produced_version_id: str | None = None
        try:
            self.jobs.mark_running(job_id)
            raw_result = self.product_executor.execute(
                request,
                job_id,
                artifact.model_fingerprint,
                dsl=dsl,
                utterance=utterance,
            )
            current = self.jobs.get(job_id)
            if not current or current.status == "CANCELLED":
                return
            self.jobs.mark_reviewing(job_id)
            status, has_incumbent = solver_status(raw_result)
            if has_incumbent:
                version = self.normalizer.build_version_from_product_result(
                    conversation_id=current.conversationId,
                    user_prompt=content,
                    focus_tag=request.strategy,
                    parameter_snapshot=(
                        {**request.to_payload(), "dsl": dsl}
                        if dsl
                        else request.to_payload()
                    ),
                    payload=raw_result,
                    solve_job_id=job_id,
                    model_fingerprint=artifact.model_fingerprint,
                )
                self.versions.add(version)
                produced_version_id = version.id
                if self.model_artifacts and isinstance(raw_result.get("模型产物"), dict):
                    self.model_artifacts.add(
                        version.id,
                        version.conversationId,
                        version.generatedAt,
                        raw_result["模型产物"],
                    )
                self.candidates.replace_for_version(current.conversationId, version.id, version.windows)
                memory = self.memories.get_or_create(current.conversationId)
                self.memories.apply_patch(memory, {
                    "latestResultId": version.id,
                    "selectedVersionIds": [version.id],
                    "activeView": "P01",
                })
                summary = version.resultSummary
                reply = self._format_product_reply(status, summary, version)
                self._finalize_response(
                    current.conversationId,
                    reply,
                    "solve_completed",
                    "P01",
                    [version],
                    solve_evidence=self._product_solve_evidence(raw_result, artifact.model_fingerprint, status),
                    solve_job_id=job_id,
                )
                artifact_payload = DecisionArtifactPayload(
                    canonicalContext=artifact.canonical_context,
                    canonicalDecision=artifact.canonical_decision,
                    canonicalModel=artifact.canonical_model,
                    contextFingerprint=artifact.context_fingerprint,
                    decisionFingerprint=artifact.decision_fingerprint,
                    modelFingerprint=artifact.model_fingerprint,
                )
                stored_result = {
                    "versionId": version.id,
                    "summary": summary,
                    "decisionArtifact": artifact_payload.model_dump(),
                }
            else:
                # 失败原因只能来自系统算出来的诊断，不能是模板话术。
                # 以前这里是一句固定的"时限内没找到 / 已证明不可行"——没错，
                # 但用户拿不到任何可行动的信息，模型还会自己补一个"可能是工时不够"。
                solving = raw_result.get("求解") or {}
                diagnosis = solving.get("失败诊断") or {}
                if diagnosis:
                    from dsl_core.diagnose import describe as describe_diagnosis

                    reason = describe_diagnosis(diagnosis)
                else:
                    # 诊断缺席时也不能退回空话：按求解记录里的时间、时限、终止原因说话。
                    from app.core.solve_explanation import explain_no_solution

                    reason = explain_no_solution(status, solving)
                self._finalize_response(
                    current.conversationId,
                    reason,
                    "solve_completed",
                    "P01",
                    [],
                    solve_evidence=self._product_solve_evidence(raw_result, artifact.model_fingerprint, status),
                    solve_job_id=job_id,
                )
                stored_result = {
                    "summary": reason,
                    "decisionArtifact": DecisionArtifactPayload(
                        canonicalContext=artifact.canonical_context,
                        canonicalDecision=artifact.canonical_decision,
                        canonicalModel=artifact.canonical_model,
                        contextFingerprint=artifact.context_fingerprint,
                        decisionFingerprint=artifact.decision_fingerprint,
                        modelFingerprint=artifact.model_fingerprint,
                    ).model_dump(),
                }
            if has_incumbent:
                # 有方案但没证到最优时，状态卡也要说清卡在哪，别只写"已完成"。
                from app.core.solve_explanation import explain_no_solution, explain_not_optimal

                incumbent_solver = (stored_result.get("summary") or {}).get("solver") or {}
                if str(incumbent_solver.get("方案来源") or "") == "HEURISTIC_FALLBACK":
                    status_message = explain_no_solution("UNKNOWN", incumbent_solver)
                elif status == "FEASIBLE":
                    status_message = explain_not_optimal(incumbent_solver)
                else:
                    status_message = None
            else:
                status_message = str(stored_result.get("summary") or "").strip() or None
            self.jobs.mark_completed(
                job_id,
                stored_result,
                status,
                has_incumbent=has_incumbent,
                # 状态卡直接写原因，别只说"已完成"。
                status_message=str(status_message).strip()[:300] if status_message else None,
            )
            current = self.jobs.get(job_id)
            if current:
                self._continue_batch(current.conversationId, job_id, produced_version_id)
        except Exception as exc:
            current = self.jobs.get(job_id)
            if current and current.status != "CANCELLED":
                message = f"求解任务执行失败：{exc}。这不是无解，当前状态记为 UNKNOWN。"
                self._finalize_response(current.conversationId, message, "solve_failed", "P01", [])
                self.jobs.mark_failed(job_id, str(exc))
                self._continue_batch(current.conversationId, job_id, None)

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
            traceId=trace_id,
            degraded=self._take_degradation(),
        )
