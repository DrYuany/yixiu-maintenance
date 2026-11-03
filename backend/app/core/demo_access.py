"""公开演示的额度闸门：免费若干轮，用完要求访客自带模型密钥。

两条约束，各自解决一件事：

1. **每访客免费轮次**（``DEMO_FREE_TURNS``）。默认 0 = 不启用，本地开发与
   自动化测试行为不变；演示部署显式设成 10。
2. **全局每日上限**（``DEMO_DAILY_TURNS``）。防止有人反复刷新访客身份刷额度，
   把一把钥匙的余额烧光。默认 300 轮/天。

访客身份优先取前端 localStorage 里的 ``X-Visitor-Id``，退回 cookie，再退回
IP。它只用来计数，不做鉴权——换浏览器就能重新拿一份免费额度，这是有意的：
这东西是"劝退"，不是"门锁"，真正兜底的是每日上限。

密钥本身不落库、不写日志。它是随每次请求带进来的，只在当前这一轮对话里
生效，用完即弃。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

SHANGHAI_TZ = timezone(timedelta(hours=8))

USER_KEY_HEADER = "x-llm-api-key"
VISITOR_HEADER = "x-visitor-id"
VISITOR_COOKIE = "mwa_visitor"

FREE_TURNS_ENV = "DEMO_FREE_TURNS"
DAILY_CAP_ENV = "DEMO_DAILY_TURNS"
DEFAULT_DAILY_CAP = 300

MAX_KEY_CHARS = 256


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(0, int(raw.strip()))
    except ValueError:
        return default


def free_turn_limit() -> int:
    """免费轮次。0 表示不限额（本地开发/测试默认）。"""
    return _env_int(FREE_TURNS_ENV, 0)


def daily_turn_cap() -> int:
    """全局每日上限。免费轮次没开时它也不生效。"""
    if not free_turn_limit():
        return 0
    return _env_int(DAILY_CAP_ENV, DEFAULT_DAILY_CAP)


# ── 请求级密钥 ───────────────────────────────────────────────────────────────
# 用线程本地而不是 contextvar：一轮对话可能跨函数调用，也可能跑在
# stream_message 自己开的线程里，两者都落在同一个线程上，线程本地最直白。


_local = threading.local()


def normalize_key(raw: str | None) -> str | None:
    if not raw:
        return None
    key = raw.strip()
    if not key or len(key) > MAX_KEY_CHARS:
        return None
    return key


def begin_llm_turn(api_key: str | None) -> str | None:
    """记录这一轮要用的访客密钥，返回上一轮的值供还原。"""
    previous = getattr(_local, "api_key", None)
    _local.api_key = normalize_key(api_key)
    return previous


def end_llm_turn(previous: str | None) -> None:
    _local.api_key = previous


def resolve_api_key(default: str | None = None) -> str | None:
    """访客自带密钥优先，否则回落到服务端配置的那把。"""
    return getattr(_local, "api_key", None) or default


# ── 额度账本 ─────────────────────────────────────────────────────────────────

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS demo_turn_usage (
        visitor_id TEXT PRIMARY KEY,
        turns INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS demo_turn_daily (
        day TEXT PRIMARY KEY,
        turns INTEGER NOT NULL DEFAULT 0
    )
    """,
)


def _today() -> str:
    return datetime.now(SHANGHAI_TZ).date().isoformat()


def _now() -> str:
    return datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")


class TurnQuota:
    """按访客计轮次，SQLite 落盘，重启不清零。"""

    def __init__(
        self,
        db_path: Path,
        *,
        limit: int | None = None,
        daily_cap: int | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self._limit = limit
        self._daily_cap = daily_cap
        self._schema_ready = False
        self._schema_lock = threading.Lock()

    # 每次读环境变量，便于测试中途改配置；显式传入的值优先。
    @property
    def limit(self) -> int:
        return self._limit if self._limit is not None else free_turn_limit()

    @property
    def daily_cap(self) -> int:
        if self._daily_cap is not None:
            return self._daily_cap
        return daily_turn_cap()

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            for statement in _SCHEMA:
                connection.execute(statement)
            connection.commit()
            self._schema_ready = True

    def used(self, visitor_id: str) -> int:
        connection = self._connect()
        try:
            self._ensure_schema(connection)
            row = connection.execute(
                "SELECT turns FROM demo_turn_usage WHERE visitor_id = ?",
                (visitor_id,),
            ).fetchone()
            return int(row["turns"]) if row else 0
        finally:
            connection.close()

    def daily_used(self) -> int:
        connection = self._connect()
        try:
            self._ensure_schema(connection)
            row = connection.execute(
                "SELECT turns FROM demo_turn_daily WHERE day = ?",
                (_today(),),
            ).fetchone()
            return int(row["turns"]) if row else 0
        finally:
            connection.close()

    def snapshot(self, visitor_id: str, *, own_key: bool = False) -> dict[str, object]:
        limit = self.limit
        daily = self.daily_cap
        used = self.used(visitor_id)
        daily_used = self.daily_used()
        return {
            "enabled": self.enabled,
            "limit": limit,
            "used": used,
            "remaining": max(0, limit - used),
            "dailyCap": daily,
            "dailyUsed": daily_used,
            "dailyRemaining": max(0, daily - daily_used) if daily else 0,
            "ownKey": own_key,
        }

    def consume(self, visitor_id: str) -> dict[str, object]:
        """扣一轮。返回是否放行以及剩余额度。"""
        limit = self.limit
        daily = self.daily_cap
        if limit <= 0:
            return {"allowed": True, "reason": None, "used": 0, "limit": 0, "remaining": 0}

        connection = self._connect()
        try:
            self._ensure_schema(connection)
            row = connection.execute(
                "SELECT turns FROM demo_turn_usage WHERE visitor_id = ?",
                (visitor_id,),
            ).fetchone()
            used = int(row["turns"]) if row else 0
            if used >= limit:
                return {
                    "allowed": False,
                    "reason": "visitor",
                    "used": used,
                    "limit": limit,
                    "remaining": 0,
                }

            day = _today()
            if daily:
                daily_row = connection.execute(
                    "SELECT turns FROM demo_turn_daily WHERE day = ?",
                    (day,),
                ).fetchone()
                daily_used = int(daily_row["turns"]) if daily_row else 0
                if daily_used >= daily:
                    return {
                        "allowed": False,
                        "reason": "daily",
                        "used": used,
                        "limit": limit,
                        "remaining": max(0, limit - used),
                        "dailyRemaining": 0,
                    }
                connection.execute(
                    "INSERT INTO demo_turn_daily (day, turns) VALUES (?, 1) "
                    "ON CONFLICT(day) DO UPDATE SET turns = turns + 1",
                    (day,),
                )

            connection.execute(
                "INSERT INTO demo_turn_usage (visitor_id, turns, updated_at) VALUES (?, 1, ?) "
                "ON CONFLICT(visitor_id) DO UPDATE SET turns = turns + 1, updated_at = excluded.updated_at",
                (visitor_id, _now()),
            )
            connection.commit()
            return {
                "allowed": True,
                "reason": None,
                "used": used + 1,
                "limit": limit,
                "remaining": max(0, limit - used - 1),
            }
        finally:
            connection.close()
