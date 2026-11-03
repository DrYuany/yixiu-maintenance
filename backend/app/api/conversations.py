from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.memory_manager import MemoryManager
from app.db.models import ConversationRepository, MessageRepository
from app.schemas.conversation import Conversation, ConversationCreateRequest, ConversationDetail


router = APIRouter(prefix="/api/conversations", tags=["conversations"])
SHANGHAI_TZ = timezone(timedelta(hours=8))


def get_conversation_repo() -> ConversationRepository:
    raise RuntimeError("Dependency override not configured")


def get_memory_manager() -> MemoryManager:
    raise RuntimeError("Dependency override not configured")


def get_message_repo() -> MessageRepository:
    raise RuntimeError("Dependency override not configured")


@router.post("", response_model=Conversation, status_code=status.HTTP_201_CREATED)
def create_conversation(
    payload: ConversationCreateRequest,
    repository: ConversationRepository = Depends(get_conversation_repo),
    memory_manager: MemoryManager = Depends(get_memory_manager),
) -> Conversation:
    now = datetime.now(SHANGHAI_TZ).isoformat()
    conversation = Conversation(
        id=f"conv_{uuid4().hex[:8]}",
        title=payload.title,
        createdAt=now,
        updatedAt=now,
        status="active",
        activeView="P01",
    )
    repository.create(conversation)
    memory_manager.get_or_create(conversation.id)
    return conversation


@router.get("", response_model=list[Conversation])
def list_conversations(repository: ConversationRepository = Depends(get_conversation_repo)) -> list[Conversation]:
    return repository.list_all()


@router.get("/{conversation_id}", response_model=ConversationDetail)
def get_conversation(
    conversation_id: str,
    repository: ConversationRepository = Depends(get_conversation_repo),
    memory_manager: MemoryManager = Depends(get_memory_manager),
    message_repo: MessageRepository = Depends(get_message_repo),
) -> ConversationDetail:
    conversation = repository.get(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return ConversationDetail(
        **conversation.model_dump(),
        messages=message_repo.list_by_conversation(conversation_id),
        workingMemory=memory_manager.get_or_create(conversation_id),
    )


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(
    conversation_id: str,
    repository: ConversationRepository = Depends(get_conversation_repo),
) -> None:
    if not repository.get(conversation_id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    repository.delete(conversation_id)
