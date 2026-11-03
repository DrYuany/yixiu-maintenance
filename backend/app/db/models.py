from __future__ import annotations

from app.db.database import Database, dumps, loads
from app.schemas.agent import CandidateWindow, Message, WindowVersion, WorkingMemory
from app.schemas.conversation import Conversation


class ConversationRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, conversation: Conversation) -> Conversation:
        self.db.execute(
            """
            INSERT INTO conversations (id, title, created_at, updated_at, status, last_message_preview, active_view, summary)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conversation.id,
                conversation.title,
                conversation.createdAt,
                conversation.updatedAt,
                conversation.status,
                conversation.lastMessagePreview,
                conversation.activeView,
                conversation.summary,
            ),
        )
        return conversation

    def list_all(self) -> list[Conversation]:
        rows = self.db.fetch_all("SELECT * FROM conversations ORDER BY updated_at DESC")
        return [self._to_model(row) for row in rows]

    def get(self, conversation_id: str) -> Conversation | None:
        row = self.db.fetch_one("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
        return self._to_model(row) if row else None

    def update(self, conversation: Conversation) -> Conversation:
        self.db.execute(
            """
            UPDATE conversations
            SET title = ?, updated_at = ?, status = ?, last_message_preview = ?, active_view = ?, summary = ?
            WHERE id = ?
            """,
            (
                conversation.title,
                conversation.updatedAt,
                conversation.status,
                conversation.lastMessagePreview,
                conversation.activeView,
                conversation.summary,
                conversation.id,
            ),
        )
        return conversation

    def delete(self, conversation_id: str) -> None:
        self.db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        self.db.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
        self.db.execute("DELETE FROM working_memory WHERE conversation_id = ?", (conversation_id,))
        self.db.execute("DELETE FROM versions WHERE conversation_id = ?", (conversation_id,))
        self.db.execute("DELETE FROM model_artifacts WHERE conversation_id = ?", (conversation_id,))
        self.db.execute("DELETE FROM candidates WHERE conversation_id = ?", (conversation_id,))

    @staticmethod
    def _to_model(row) -> Conversation:
        return Conversation(
            id=row["id"],
            title=row["title"],
            createdAt=row["created_at"],
            updatedAt=row["updated_at"],
            status=row["status"],
            lastMessagePreview=row["last_message_preview"],
            activeView=row["active_view"],
            summary=row["summary"],
        )


class MessageRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, message: Message) -> Message:
        self.db.execute(
            """
            INSERT INTO messages (
                id, conversation_id, role, content, created_at, version_ids, job_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message.id,
                message.conversationId,
                message.role,
                message.content,
                message.createdAt,
                dumps(message.versionIds),
                message.jobId,
            ),
        )
        return message

    def list_by_conversation(self, conversation_id: str) -> list[Message]:
        rows = self.db.fetch_all(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at ASC",
            (conversation_id,),
        )
        return [
            Message(
                id=row["id"],
                conversationId=row["conversation_id"],
                role=row["role"],
                content=row["content"],
                createdAt=row["created_at"],
                versionIds=loads(row["version_ids"]) if row["version_ids"] else [],
                jobId=row["job_id"],
            )
            for row in rows
        ]


class WorkingMemoryRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, conversation_id: str) -> WorkingMemory | None:
        row = self.db.fetch_one("SELECT payload FROM working_memory WHERE conversation_id = ?", (conversation_id,))
        return WorkingMemory(**loads(row["payload"])) if row else None

    def upsert(self, memory: WorkingMemory) -> WorkingMemory:
        self.db.execute(
            """
            INSERT INTO working_memory (conversation_id, payload)
            VALUES (?, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET payload = excluded.payload
            """,
            (memory.conversationId, dumps(memory.model_dump())),
        )
        return memory


class VersionRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, version: WindowVersion) -> WindowVersion:
        self.db.execute(
            "INSERT INTO versions (id, conversation_id, payload, is_deleted) VALUES (?, ?, ?, 0)",
            (version.id, version.conversationId, dumps(version.model_dump())),
        )
        return version

    def list_by_conversation(self, conversation_id: str) -> list[WindowVersion]:
        rows = self.db.fetch_all(
            "SELECT payload FROM versions WHERE conversation_id = ? AND is_deleted = 0 ORDER BY rowid DESC",
            (conversation_id,),
        )
        return [WindowVersion(**loads(row["payload"])) for row in rows]

    def get(self, version_id: str) -> WindowVersion | None:
        row = self.db.fetch_one("SELECT payload FROM versions WHERE id = ? AND is_deleted = 0", (version_id,))
        return WindowVersion(**loads(row["payload"])) if row else None

    def update(self, version: WindowVersion) -> WindowVersion:
        self.db.execute("UPDATE versions SET payload = ? WHERE id = ?", (dumps(version.model_dump()), version.id))
        return version

    def delete(self, version_id: str) -> None:
        self.db.execute("UPDATE versions SET is_deleted = 1 WHERE id = ?", (version_id,))


class ModelArtifactRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, version_id: str, conversation_id: str, created_at: str, payload: dict) -> None:
        self.db.execute(
            """
            INSERT INTO model_artifacts (version_id, conversation_id, created_at, payload)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(version_id) DO UPDATE SET
                conversation_id = excluded.conversation_id,
                created_at = excluded.created_at,
                payload = excluded.payload
            """,
            (version_id, conversation_id, created_at, dumps(payload)),
        )

    def get(self, version_id: str) -> dict | None:
        row = self.db.fetch_one(
            "SELECT payload FROM model_artifacts WHERE version_id = ?",
            (version_id,),
        )
        return loads(row["payload"]) if row else None


class CandidateRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def replace_for_version(self, conversation_id: str, version_id: str, candidates: list[CandidateWindow]) -> None:
        self.db.execute(
            "UPDATE candidates SET is_deleted = 1 WHERE conversation_id = ? AND version_id = ?",
            (conversation_id, version_id),
        )
        if not candidates:
            return
        self.db.executemany(
            "INSERT INTO candidates (id, conversation_id, version_id, payload, is_deleted) VALUES (?, ?, ?, ?, 0)",
            [
                (candidate.id, conversation_id, version_id, dumps(candidate.model_dump()))
                for candidate in candidates
            ],
        )

    def list_by_conversation(self, conversation_id: str) -> list[CandidateWindow]:
        rows = self.db.fetch_all(
            "SELECT payload FROM candidates WHERE conversation_id = ? AND is_deleted = 0",
            (conversation_id,),
        )
        return [CandidateWindow(**loads(row["payload"])) for row in rows]

    def mark_deleted_by_version(self, version_id: str) -> None:
        self.db.execute("UPDATE candidates SET is_deleted = 1 WHERE version_id = ?", (version_id,))

    def delete(self, candidate_id: str) -> None:
        self.db.execute("UPDATE candidates SET is_deleted = 1 WHERE id = ?", (candidate_id,))

    def reorder(self, items: list[tuple[str, int]]) -> None:
        rows = self.db.fetch_all("SELECT id, payload FROM candidates WHERE is_deleted = 0")
        payloads = {row["id"]: loads(row["payload"]) for row in rows}
        for candidate_id, display_order in items:
            if candidate_id not in payloads:
                continue
            payloads[candidate_id]["displayOrder"] = display_order
            self.db.execute(
                "UPDATE candidates SET payload = ? WHERE id = ?",
                (dumps(payloads[candidate_id]), candidate_id),
            )
