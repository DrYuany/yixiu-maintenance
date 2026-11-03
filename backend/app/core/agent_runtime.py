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
from app.core.yx_engine import YxEngine
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
        job_pool: ThreadPoolExecutor | None = None,
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
        self.job_pool = job_pool or ThreadPoolExecutor(max_workers=2, thread_name_prefix="maintenance-solve")
        self._futures: dict[str, Future] = {}
        self.traces = traces
        self.summary_repo = summary_repo
        self.profile_repo = profile_repo
        self.decision_repo = decision_repo
        self.memory_repo = memory_repo
        self.memory_cache = ConversationMemoryCache()
        self._tls = threading.local()
        # 小一修槽位引擎：每个会话一张旋钮表。与一修唯一的分叉点——理解层。
        self._yx_engines: dict[str, YxEngine] = {}

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
            if selected_version:
                if model_question in {"parameter", "status"}:
                    return self._answer_model_question(conversation_id, content, model_question, selected_version, memory)
                return self._finalize_response(
                    conversation_id,
                    "小一修是槽位填表模型，不生成自定义公式或约束；当前策略、目标与排程见方案卡。",
                    "model_query", memory.activeView, [],
                )
            return self._finalize_response(
                conversation_id,
                "当前会话还没有可查询的排程方案。请先生成方案，之后我才能解释模型、参数或结果。",
                "model_query_empty", memory.activeView, [],
            )
        compare_reply = self._compare_versions_reply(conversation_id, content, selected_version)
        if compare_reply is not None:
            return self._finalize_response(conversation_id, compare_reply, "version_compare", memory.activeView, [])

        # 2. 改排程：槽位引擎（翻译 → 求解）
        return self._yx_solve(conversation_id, content, memory, sink)

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
            result = engine.run(seconds=60)
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

    @staticmethod
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
