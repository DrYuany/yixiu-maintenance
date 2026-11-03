from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.agent import CandidateWindow, WindowVersion


class DashboardResponse(BaseModel):
    conversationId: str
    sortMode: str = "latest_first"
    versions: list[WindowVersion] = Field(default_factory=list)
    candidateWindowPool: list[CandidateWindow] = Field(default_factory=list)
    topRecommendations: list[CandidateWindow] = Field(default_factory=list)
