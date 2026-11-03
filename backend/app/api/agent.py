from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import Literal

from app.core.agent_runtime import AgentRuntime, default_engine
from app.core.demo_access import USER_KEY_HEADER, normalize_key
from app.schemas.agent import AgentResponse, SolveJobPayload


router = APIRouter(prefix="/api/agent", tags=["agent"])


def _visitor_key(request: Request) -> str | None:
    """访客自带的模型密钥。没有就用服务端配置的那把（免费额度内）。"""
    return normalize_key(request.headers.get(USER_KEY_HEADER))


class AgentMessageRequest(BaseModel):
    conversationId: str
    message: str
    selectedVersionId: str | None = None
    selectedWindowId: str | None = None
    # 理解引擎：llm = LLM 主导，rule = 规则主导。
    # 默认跟产线走（default_engine，见 agent_runtime）；前端切换控件会显式带上。
    # 回滚：设环境变量 AGENT_ENGINE=rule，不用改代码也不用重新发版。
    engine: Literal["llm", "rule"] = Field(default_factory=default_engine)


def get_runtime() -> AgentRuntime:
    raise RuntimeError("Dependency override not configured")


@router.post("/message", response_model=AgentResponse)
def post_message(
    payload: AgentMessageRequest,
    request: Request,
    runtime: AgentRuntime = Depends(get_runtime),
) -> AgentResponse:
    try:
        return runtime.submit_message(
            payload.conversationId,
            payload.message,
            selected_version_id=payload.selectedVersionId,
            selected_window_id=payload.selectedWindowId,
            engine=payload.engine,
            llm_api_key=_visitor_key(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/message/stream")
def post_message_stream(
    payload: AgentMessageRequest,
    request: Request,
    runtime: AgentRuntime = Depends(get_runtime),
) -> StreamingResponse:
    api_key = _visitor_key(request)

    def events():
        for item in runtime.stream_message(
            payload.conversationId,
            payload.message,
            selected_version_id=payload.selectedVersionId,
            selected_window_id=payload.selectedWindowId,
            engine=payload.engine,
            llm_api_key=api_key,
        ):
            data = json.dumps(item.get("data", {}), ensure_ascii=False, default=str)
            yield f"event: {item.get('event', 'message')}\ndata: {data}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


class SolveJobActionRequest(BaseModel):
    action: Literal["wait", "background", "use_incumbent", "stop"]


def _with_batch_progress(runtime: AgentRuntime, job, payload: SolveJobPayload) -> SolveJobPayload:
    progress = runtime.batch_progress(job.conversationId)
    if progress:
        payload.batchPosition = progress["position"]
        payload.batchTotal = progress["total"]
    return payload


@router.get("/jobs/{job_id}", response_model=SolveJobPayload)
def get_solve_job(job_id: str, runtime: AgentRuntime = Depends(get_runtime)) -> SolveJobPayload:
    job = runtime.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Solve job not found")
    return _with_batch_progress(runtime, job, SolveJobPayload(**job.model_dump()))


@router.get("/trace/{request_id}")
def get_trace(request_id: str, runtime: AgentRuntime = Depends(get_runtime)):
    trace = runtime.get_trace(request_id)
    if not trace:
        raise HTTPException(status_code=404, detail="Trace not found")
    return trace.model_dump()


@router.get("/conversations/{conversation_id}/traces")
def list_traces(
    conversation_id: str,
    limit: int = 50,
    runtime: AgentRuntime = Depends(get_runtime),
):
    return [trace.model_dump() for trace in runtime.list_traces(conversation_id, limit)]


@router.get("/conversations/{conversation_id}/jobs/latest", response_model=SolveJobPayload | None)
def get_latest_solve_job(conversation_id: str, runtime: AgentRuntime = Depends(get_runtime)) -> SolveJobPayload | None:
    job = runtime.get_latest_job(conversation_id)
    return _with_batch_progress(runtime, job, SolveJobPayload(**job.model_dump())) if job else None


@router.get("/conversations/{conversation_id}/jobs", response_model=list[SolveJobPayload])
def list_solve_jobs(conversation_id: str, runtime: AgentRuntime = Depends(get_runtime)) -> list[SolveJobPayload]:
    return [
        _with_batch_progress(runtime, job, SolveJobPayload(**job.model_dump()))
        for job in runtime.list_jobs(conversation_id)
    ]


@router.post("/jobs/{job_id}/actions", response_model=SolveJobPayload)
def control_solve_job(
    job_id: str,
    payload: SolveJobActionRequest,
    runtime: AgentRuntime = Depends(get_runtime),
) -> SolveJobPayload:
    try:
        job = runtime.control_job(job_id, payload.action)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return SolveJobPayload(**job.model_dump())
