from __future__ import annotations

from app.db.models import WorkingMemoryRepository
from app.schemas.agent import WorkingMemory


class MemoryManager:
    def __init__(self, repository: WorkingMemoryRepository) -> None:
        self.repository = repository

    def get_or_create(self, conversation_id: str) -> WorkingMemory:
        memory = self.repository.get(conversation_id)
        if memory:
            return memory
        memory = WorkingMemory(
            conversationId=conversation_id,
            currentGoal="生成并管理窗口版本",
            activeView="P01",
            activeFilters={"timeRange": None, "versionIds": [], "operationType": None},
        )
        return self.repository.upsert(memory)

    def apply_patch(self, memory: WorkingMemory, patch: dict) -> WorkingMemory:
        merged = memory.model_copy(update=patch)
        return self.repository.upsert(merged)
