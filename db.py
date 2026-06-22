from pathlib import Path

import aiosqlite


CREATE_DANMAKU_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS danmaku (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  room_id TEXT NOT NULL,
  text TEXT NOT NULL,
  video_time REAL NOT NULL,
  sender_id TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
"""

CREATE_DANMAKU_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_danmaku_room
ON danmaku(room_id, video_time);
"""


async def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA synchronous=NORMAL;")
        await db.execute(CREATE_DANMAKU_TABLE_SQL)
        await db.execute(CREATE_DANMAKU_INDEX_SQL)
        await db.commit()


async def add_danmaku(
    db_path: Path,
    *,
    room_id: str,
    text: str,
    video_time: float,
    sender_id: str,
    created_at: int,
) -> int:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            """
            INSERT INTO danmaku (room_id, text, video_time, sender_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (room_id, text, video_time, sender_id, created_at),
        )
        await db.commit()
        return int(cursor.lastrowid)


async def get_danmaku_for_room(db_path: Path, room_id: str) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id, room_id, text, video_time, sender_id, created_at
            FROM danmaku
            WHERE room_id = ?
            ORDER BY video_time ASC, id ASC
            """,
            (room_id,),
        )
        rows = await cursor.fetchall()

    return [dict(row) for row in rows]
