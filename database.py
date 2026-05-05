import aiosqlite
import json

DB_PATH = "bot.db"

async def init_db():
    """Создаёт таблицы, если их нет"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT,
                username TEXT,
                last_activity TEXT,
                groups TEXT DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS playlists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                url TEXT NOT NULL,
                title TEXT,
                duration INTEGER,
                uploader TEXT,
                added_at TEXT,
                UNIQUE(user_id, url)
            );
        """)
        await db.commit()

async def log_user_activity(user_id: int, first_name: str, username: str, chat_id: int):
    """Фиксирует активность пользователя и группу (если сообщение из группы)"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT groups FROM users WHERE user_id=?", (user_id,))
        row = await cursor.fetchone()
        groups = []
        if row:
            try:
                groups = json.loads(row[0])
            except:
                groups = []
        if chat_id < 0 and chat_id not in groups:
            groups.append(chat_id)
        groups_json = json.dumps(groups)

        await db.execute("""
            INSERT OR REPLACE INTO users (user_id, first_name, username, last_activity, groups)
            VALUES (?, ?, ?, datetime('now'), ?)
        """, (user_id, first_name, username, groups_json))
        await db.commit()

async def get_all_users():
    """Возвращает список всех пользователей, упорядоченных по последней активности"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM users ORDER BY last_activity DESC")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

async def add_track_to_playlist(user_id: int, track_info: dict) -> bool:
    """Добавляет трек в плейлист. Возвращает True, если добавлен, False если уже был."""
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("""
                INSERT OR IGNORE INTO playlists (user_id, url, title, duration, uploader, added_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
            """, (user_id, track_info['url'], track_info.get('title', ''),
                  track_info.get('duration', 0), track_info.get('uploader', '')))
            await db.commit()
            return db.total_changes > 0
        except Exception:
            return False

async def remove_track_from_playlist(user_id: int, index: int):
    """Удаляет трек по индексу (0-based). Возвращает удалённый трек или None."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, url, title, duration, uploader FROM playlists WHERE user_id=? ORDER BY added_at",
            (user_id,))
        tracks = await cursor.fetchall()
        if 0 <= index < len(tracks):
            track_id = tracks[index][0]
            removed = tracks[index]
            await db.execute("DELETE FROM playlists WHERE id=?", (track_id,))
            await db.commit()
            return {
                'url': removed[1],
                'title': removed[2],
                'duration': removed[3],
                'uploader': removed[4]
            }
        return None

async def get_user_playlist(user_id: int) -> list[dict]:
    """Возвращает плейлист пользователя (список словарей)"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT url, title, duration, uploader, added_at FROM playlists WHERE user_id=? ORDER BY added_at",
            (user_id,))
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
