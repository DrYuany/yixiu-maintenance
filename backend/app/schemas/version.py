from __future__ import annotations

from pydantic import BaseModel


class VersionToggleRequest(BaseModel):
    includedInCandidatePool: bool


class CandidateReorderItem(BaseModel):
    id: str
    displayOrder: int


class CandidateReorderRequest(BaseModel):
    items: list[CandidateReorderItem]
