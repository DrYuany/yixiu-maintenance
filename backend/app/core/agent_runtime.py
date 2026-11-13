from __future__ import annotations

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
from app.core.solve_jobs import SolveJobRepository
from app.core.version_manager import VersionManager
from app.core.version_compare import render_compare_summary
from app.core.version_reference import resolve_version_reference
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


def _solve_failure_text(exc: Exception, session) -> str:
    """把求解失败的原因和「要求撤回到哪一版」合成一句人话。

    回滚动作在 Session.run() 里已经做完，这里只把说明带进回复。否则界面上
    只有一句「求解没成功」，用户不知道自己刚说的那几条要求还算不算数。
    """
    note = getattr(session, "last_rollback", "")
    return f"{exc}；{note}" if note else str(exc)


# 理解层只有一条：LLM 工具循环。旧的两套引擎（llm/rule）切换与
# AGENT_ENGINE 回滚开关随规则兜底链一起撤销，别留一个点不动的假旋钮。

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
        *,
        tool: str | None = None,
    ) -> None:
        """结构化步骤事件（过程可视化 + 评测阶段 span 同源）。

        tool 是这一步实际调用的工具名，前端单独排成标签；没调工具的步骤
        （理解、整理回答）不带这个字段。
        """
        data: dict[str, Any] = {"id": stage_id, "label": label, "status": status}
        if detail is not None:
            data["detail"] = detail
        if tool is not None:
            data["tool"] = tool
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
                # 结构化过程事件由 conversation_agent 的工具循环统一发
                # （含 running 与结果摘要），这里不再补一条同名 done，
                # 否则前端会按 id 收成两条互相打架的步骤。

        # 待确认变更的唯一事实源是持久化的 working memory；工具箱拿它的
        # 本轮快照，跨轮「确认」才读得到上一轮那份预览。
        pending = (self.memories.get_or_create(conversation_id).activeFilters or {}).get(
            "pendingChange"
        )
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
            pending_change=pending if isinstance(pending, dict) else None,
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
            # 过程事件由工具循环按真实进展发：理解 → 调工具 → 整理回答。
            on_stage=(lambda payload: sink.stage(**payload)) if sink else None,
        )

    def _yx_route(self, conversation_id: str, content: str, selected_version_id: str | None, sink):
        """小一修入口（单循环）：LLM 工具循环是唯一理解者。

        一修和小一修唯一该有区别的是「理解层」——所有消息先进循环，
        模型自主决定：读工具答查询，调 solve 出预览，调 submit_change 提交。
        词表路由、槽位直通、规则兜底答案已整块删除；模型没答上来就如实说，
        不换一套口径冒充。
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
        # 1. LLM 工具循环：唯一理解者。改排程 = 模型决定调 solve（solve 内部
        #    就是 yxcore 填槽）；查询 = 模型按工具说明自主读数据。确认 =
        #    模型调 submit_change，由工具核对那份待确认预览。主路不再拿
        #    「确认 / 可以 / 开始」这类字样抢在模型前面判意图。
        if not self.conversation_agent.enabled:
            self._note_degradation(
                "llm_unavailable", "这一轮没能调用大模型：请配置 DeepSeek API Key 后重试。")
            return self._finalize_response(
                conversation_id,
                "这一轮没能调用大模型：请填入自己的 DeepSeek API Key 后重试。",
                "llm_unavailable", memory.activeView, [],
            )
        try:
            reply = self._answer_with_conversation_agent(
                conversation_id=conversation_id,
                content=content,
                history=self.messages.list_by_conversation(conversation_id),
                memory=memory,
                selected_version=selected_version,
                sink=sink,
            )
        except ConversationAgentError as exc:
            # 模型没答上来就如实说，不再换一套规则答案冒充。两套口径曾把
            # 「能看下数学模型么」顶成兜底文案，也把成本表当成过公式。
            self._note_degradation(
                "agent_unavailable",
                "模型这一轮没答上来，请重试或填入自己的 DeepSeek API Key。",
                str(exc),
            )
            return self._finalize_response(
                conversation_id,
                "这一轮大模型没答上来，请重试；也可以填入自己的 DeepSeek API Key 继续。",
                "llm_unavailable", memory.activeView, [],
            )
        return self._finalize_loop_response(conversation_id, reply, memory)

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
        kind = pending.get("kind")
        # 澄清中的变更不是「失效」，是「还没答完」。这两种话必须分开说：
        # 说失效会让用户以为要重说一遍需求，于是再翻译一遍，越走越远。
        if kind == "clarify":
            return {"ok": False,
                    "message": "这句还没定下来：先点选上面的选项（或重新说一句），再确认。"}
        # yx 与 batch 都是「已出过预览、等用户点头」的形态，批量只是换成 N 份
        # 补丁各算一版。之前只认 yx，导致确认多方案时恒返回「已经失效」，
        # 模型只能反复重译重预览，1 个方案都出不来。
        if kind not in {"yx", "batch"}:
            return {"ok": False, "message": "待确认的变更已经失效。"}
        self._clear_persisted_pending(conversation_id)
        resp = self._start_planning_solve(conversation_id, pending, memory, None)
        self._tls.submitted_response = resp
        # 工具层这句只给模型看；用户看到的是 resp 里那份系统口径，别在这
        # 另写一句「已完成」——异步求解这时其实才刚启动。
        return {"ok": True, "jobId": None,
                "message": str(resp.assistantMessage.content).strip()[:200] or "求解已发起。"}

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
            "真实 CP-SAT 求解已启动，正在后台跑。算完会自动出方案卡。",
            "solve_started", "P01", [],
            solve_evidence=SolveEvidencePayload(status="UNKNOWN", running=True, phase="queued"),
            solve_job=SolveJobPayload(**job.model_dump()),
        )

    def _start_batch_solve(self, conversation_id, pending, memory, sink):
        """复合需求：并发 fork N 个求解任务，全部算完后汇合对比。"""
        scenarios = pending.get("scenarios") or []
        labels = [str(sc.get("标签") or f"方案{i+1}") for i, sc in enumerate(scenarios)]
        # 场景补丁只描述这一句要换的那个维度，不含本会话此前已确认的要求。
        # 把当前生效的补丁一起带进每个场景（先铺底、再叠本场景），否则批量
        # 一确认就把「船租、最多修几个」这类前几轮的要求全丢了。
        base_patches = [p.to_json() for p in self._yx_engine(conversation_id).session.patches()]
        # 重新取一份最新记忆再写：调用方刚把 pendingChange 清掉，而
        # apply_patch 不修改传入对象，拿旧的 activeFilters 往上写会把
        # 刚清掉的待确认原样复活——算完再点一次「确认」就又能触发一轮，
        # 正是那种「一直在循环」的观感。
        memory = self.memories.get_or_create(conversation_id)
        filters = dict(memory.activeFilters)
        filters["pendingBatch"] = {"total": len(scenarios), "completed": [], "labels": labels}
        self.memories.apply_patch(memory, {"activeFilters": filters})
        if self.jobs is None or self.job_pool is None:
            # 测试/降级：同步逐个跑，再对比
            versions = []
            for sc in scenarios:
                version = self._run_batch_scenario_sync(conversation_id, sc, base_patches)
                if version is not None:
                    versions.append(version)
            if versions:
                self._emit_batch_compare(conversation_id, [v.id for v in versions])
            return self._finalize_response(conversation_id, "批量求解已完成。", "solve_completed", "P01", versions)
        # 并发 fork：每个场景一个独立任务
        jobs = []
        for i, sc in enumerate(scenarios):
            job = self.jobs.create(
                conversation_id,
                {"kind": "batch", "label": labels[i], "patches": sc.get("patches") or [],
                 "basePatches": base_patches, "budgetSeconds": _SOLVE_SECONDS},
                "",
            )
            jobs.append(job)
            self._futures[job.id] = self.job_pool.submit(
                self._execute_batch_job, job.id, conversation_id, labels[i],
                sc.get("patches") or [], base_patches
            )
        return self._finalize_response(
            conversation_id,
            f"已并发启动 {len(scenarios)} 个求解任务，全部算完会自动出方案卡和对比。",
            "solve_started", "P01", [],
            solve_evidence=SolveEvidencePayload(status="UNKNOWN", running=True, phase="queued"),
            # 带一个真实任务号：前端靠它开始轮询，否则批量求解算完也不会刷新。
            # 其余任务由 /jobs 列表随每一次轮询补齐。
            solve_job=SolveJobPayload(**jobs[0].model_dump()) if jobs else None,
        )

    @staticmethod
    def _fresh_session_engine() -> YxEngine:
        """批量场景用的独立引擎。

        它不经过 ``say()``（没有翻译、直接套补丁），所以会话里的基础快照还
        没加载；不先装上，后面的 ``build_version`` 读 ``session.base_data``
        会拿到 None 直接抛错，每个场景都算失败、0 个方案。
        """
        engine = YxEngine()
        engine.session._ensure_data()
        return engine

    def _run_batch_scenario_sync(self, conversation_id, scenario, base_patches=()):
        from yxcore.patch import parse_patch
        engine = self._fresh_session_engine()
        patches = self._patches_from_payload(scenario.get("patches") or [])
        base = self._patches_from_payload(list(base_patches))
        if base:
            base_turn = engine.session.apply(base)
            if base_turn.错误:
                return None
        turn = engine.session.apply(patches)
        if turn.错误:
            return None
        try:
            result = engine.session.run(seconds=_SOLVE_SECONDS)
            version_id = f"ver_{uuid4().hex[:8]}"
            version = engine.build_version(conversation_id, scenario.get("标签") or "",
                                           result, version_id)
        except Exception:  # noqa: BLE001
            # 这个引擎是本场景 fork 出来的一次性会话，失败即丢弃；撤回到
            # 「哪份要求算数」由 Session.run() 统一处理，这里不必再回滚一次。
            # 建版本失败也归这里：少一版好过整批抛出去一版都没有。
            return None
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

    def _execute_batch_job(self, job_id, conversation_id, label, raw_patches, base_patches=()):
        engine = self._fresh_session_engine()
        patches = self._patches_from_payload(raw_patches)
        base = self._patches_from_payload(list(base_patches))
        if base:
            # 铺底：本会话此前已确认的要求。套不上就直接失败，不要静默退化成
            # 「只按这一维度排」——那样算出来的方案看着有、其实少了约束。
            base_turn = engine.session.apply(base)
            if base_turn.错误:
                if self.jobs:
                    self.jobs.mark_failed(job_id, "；".join(base_turn.错误))
                self._batch_job_done(conversation_id, job_id, None)
                return
        turn = engine.session.apply(patches)
        if self.jobs:
            self.jobs.mark_running(job_id)
        if turn.错误:
            if self.jobs:
                self.jobs.mark_failed(job_id, "；".join(turn.错误))
            self._batch_job_done(conversation_id, job_id, None)
            return
        try:
            result = engine.session.run(seconds=_SOLVE_SECONDS)
            version_id = f"ver_{uuid4().hex[:8]}"
            version = engine.build_version(conversation_id, label, result, version_id,
                                           solve_job_id=job_id)
        except Exception as exc:  # noqa: BLE001
            # 建版本这一步也算在本任务里：它抛出去的话任务永远停在 RUNNING，
            # 前端会一直轮询、批量永远不汇合——用户看到的就是「一直在循环」。
            if self.jobs:
                self.jobs.mark_failed(job_id, _solve_failure_text(exc, engine.session))
            self._batch_job_done(conversation_id, job_id, None)
            return
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
            result = engine.session.run(seconds=_SOLVE_SECONDS)
        except Exception as exc:  # noqa: BLE001
            return self._finalize_response(
                conversation_id,
                f"求解没成功：{_solve_failure_text(exc, engine.session)}",
                "solve_failed", memory.activeView, [])
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
            result = engine.session.run(seconds=_SOLVE_SECONDS)
        except Exception as exc:  # noqa: BLE001
            detail = _solve_failure_text(exc, engine.session)
            if self.jobs:
                self.jobs.mark_failed(job_id, detail)
            self._finalize_response(conversation_id, f"求解任务执行失败：{detail}", "solve_failed", "P01", [])
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

    def _submit_message(
        self,
        conversation_id: str,
        content: str,
        selected_version_id: str | None = None,
        selected_window_id: str | None = None,
        sink: StreamSink | None = None,
    ) -> AgentResponse:
        self._begin_trace(conversation_id, content)
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

    def _finalize_loop_response(self, conversation_id, reply, memory):
        """循环出口：模型这轮调过 solve 时按其结构化结果定 actionType。

        判定不重猜——pendingChange 是 solve 工具刚落库的事实（on_pending_change
        幂等：落库发生在工具里，这里只把 reply 与结构一起送进响应）。
        不转换的话，改排程预览/澄清/批量场景都会顶着 assistant_reply 混掉区别。
        """
        # 本轮模型调过 submit_change：求解由 runtime 派出，它已经造好完整的
        # 结构化响应（solve_started + solveJob，或同步方案卡）。直接交那份，
        # 别用模型后来的转述把任务状态和版本号盖掉。
        tls = getattr(self, "_tls", None)
        submitted = getattr(tls, "submitted_response", None) if tls is not None else None
        if submitted is not None:
            return submitted
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
