from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.core.agent_runtime import AgentRuntime
from app.db.models import VersionRepository
from app.schemas.agent import WindowVersion
from app.schemas.version import CandidateReorderRequest, VersionToggleRequest


router = APIRouter(prefix="/api", tags=["versions"])


def get_runtime() -> AgentRuntime:
    raise RuntimeError("Dependency override not configured")


def get_version_repo() -> VersionRepository:
    raise RuntimeError("Dependency override not configured")


@router.get("/conversations/{conversation_id}/versions", response_model=list[WindowVersion])
def list_versions(
    conversation_id: str,
    repository: VersionRepository = Depends(get_version_repo),
) -> list[WindowVersion]:
    return repository.list_by_conversation(conversation_id)


@router.get("/versions/{version_id}/model")
def get_model_artifact(
    version_id: str,
    runtime: AgentRuntime = Depends(get_runtime),
) -> dict:
    artifact = runtime.get_model_artifact(version_id)
    if not artifact:
        raise HTTPException(
            status_code=404,
            detail="该版本未保存完整模型产物。旧版本需要按同一输入重新运行并保存模型。",
        )
    return artifact


@router.post("/versions/{version_id}/include", status_code=status.HTTP_204_NO_CONTENT)
def include_version(
    version_id: str,
    payload: VersionToggleRequest,
    runtime: AgentRuntime = Depends(get_runtime),
) -> Response:
    try:
        runtime.include_version(version_id, payload.includedInCandidatePool)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/versions/{version_id}/exclude", status_code=status.HTTP_204_NO_CONTENT)
def exclude_version(
    version_id: str,
    payload: VersionToggleRequest,
    runtime: AgentRuntime = Depends(get_runtime),
) -> Response:
    try:
        runtime.include_version(version_id, payload.includedInCandidatePool)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/versions/{version_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_version(version_id: str, runtime: AgentRuntime = Depends(get_runtime)) -> Response:
    runtime.delete_version(version_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/candidates/reorder", status_code=status.HTTP_204_NO_CONTENT)
def reorder_candidates(
    payload: CandidateReorderRequest,
    runtime: AgentRuntime = Depends(get_runtime),
) -> Response:
    runtime.reorder_candidates([(item.id, item.displayOrder) for item in payload.items])
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/candidates/{candidate_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_candidate(candidate_id: str, runtime: AgentRuntime = Depends(get_runtime)) -> Response:
    runtime.delete_candidate(candidate_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
