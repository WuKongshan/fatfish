"""提醒的持久化：SQLite 单文件，挂载到宿主机。

为什么不用主项目那套 Postgres：这个 bot 是独立服务，
为了存十几条提醒去连一个数据库，部署复杂度翻倍，收益为零。
SQLite 够用，备份就是复制一个文件。
"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path


class ReminderStore:
    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_key TEXT NOT NULL,
                    target TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    sent INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_due ON reminders (sent, due_at)")

    def add(
        self, *, user_key: str, target: str, target_id: str, content: str, due_at: datetime
    ) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO reminders (user_key, target, target_id, content, due_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    user_key,
                    target,
                    target_id,
                    content,
                    due_at.astimezone(UTC).isoformat(),
                    datetime.now(UTC).isoformat(),
                ),
            )
            return int(cur.lastrowid or 0)

    def due(self, now: datetime | None = None) -> list[sqlite3.Row]:
        moment = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self._conn() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM reminders WHERE sent = 0 AND due_at <= ? ORDER BY due_at",
                    (moment,),
                )
            )

    def mark_sent(self, reminder_id: int) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE reminders SET sent = 1 WHERE id = ?", (reminder_id,))

    def pending_of(self, user_key: str) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM reminders WHERE user_key = ? AND sent = 0 ORDER BY due_at",
                    (user_key,),
                )
            )
