from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


def resolve_db_path() -> Path:
    raw = os.environ.get("MWA_DB_PATH")
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parents[3] / "data" / "agent.db"


class Database:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or resolve_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connection() as connection:
            cursor = connection.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    last_message_preview TEXT NOT NULL,
                    active_view TEXT NOT NULL,
                    summary TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    version_ids TEXT NOT NULL DEFAULT '[]',
                    job_id TEXT
                )
                """
            )
            message_columns = {
                row["name"]
                for row in cursor.execute("PRAGMA table_info(messages)").fetchall()
            }
            if "version_ids" not in message_columns:
                cursor.execute(
                    "ALTER TABLE messages ADD COLUMN version_ids TEXT NOT NULL DEFAULT '[]'"
                )
            if "job_id" not in message_columns:
                cursor.execute("ALTER TABLE messages ADD COLUMN job_id TEXT")
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS working_memory (
                    conversation_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS versions (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    is_deleted INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS model_artifacts (
                    version_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS candidates (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    version_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    is_deleted INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS solve_jobs (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_solve_jobs_conversation ON solve_jobs(conversation_id, submitted_at)"
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_traces (
                    request_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_traces_conversation ON agent_traces(conversation_id)"
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_summary (
                    conversation_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS user_profile (
                    user_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS decision_memories (
                    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_decision_memories_conversation ON decision_memories(conversation_id)"
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_memory (
                    conversation_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                )
                """
            )

    def fetch_all(self, query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self.connection() as connection:
            return list(connection.execute(query, params).fetchall())

    def fetch_one(self, query: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self.connection() as connection:
            return connection.execute(query, params).fetchone()

    def execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
        with self.connection() as connection:
            connection.execute(query, params)

    def executemany(self, query: str, params: list[tuple[Any, ...]]) -> None:
        with self.connection() as connection:
            connection.executemany(query, params)


def dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)


def loads(payload: str) -> Any:
    return json.loads(payload)
