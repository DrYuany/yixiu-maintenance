from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.agent_runtime import AgentRuntime
from app.schemas.dashboard import DashboardResponse


router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


def get_runtime() -> AgentRuntime:
    raise RuntimeError("Dependency override not configured")


@router.get("/{conversation_id}", response_model=DashboardResponse)
def get_dashboard(
    conversation_id: str,
    sortMode: str = "latest_first",
    runtime: AgentRuntime = Depends(get_runtime),
) -> DashboardResponse:
    return runtime.get_dashboard(conversation_id, sort_mode=sortMode)
