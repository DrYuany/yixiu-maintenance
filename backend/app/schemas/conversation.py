from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.agent import Message, WorkingMemory


class Conversation(BaseModel):
    id: str
    title: str
    createdAt: str
    updatedAt: str
    status: Literal["idle", "active", "archived"] = "active"
    lastMessagePreview: str = ""
    activeView: str = "P01"
    summary: str = ""


class ConversationCreateRequest(BaseModel):
    title: str = "新会话"


class ConversationDetail(Conversation):
    messages: list[Message] = Field(default_factory=list)
    workingMemory: WorkingMemory
