import aiosqlite
import json
import os

# Используем абсолютный путь к БД
DB_PATH = os.path.join(os.path.dirname(__file__), "bot.db")

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
            CREATE INDEX IF NOT EXISTS idx_playlists_user ON playlists(user_id);
            CREATE INDEX IF NOT EXISTS idx_playlists_url ON playlists(url);
        """)
        await db.commit()
    print("✅ База данных инициализирована")

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
    # Проверяем обязательные поля
    if not track_info.get('url'):
        print(f"❌ Ошибка: нет url в track_info: {track_info}")
        return False
    
    if not track_info.get('title'):
        print(f"⚠️ Предупреждение: нет title в track_info, использую 'Без названия'")
        track_info['title'] = 'Без названия'
    
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("""
                INSERT OR IGNORE INTO playlists (user_id, url, title, duration, uploader, added_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
            """, (user_id, track_info['url'], track_info.get('title', 'Без названия')[:200],
                  track_info.get('duration', 0), track_info.get('uploader', '')[:100]))
            await db.commit()
            
            # Проверяем, добавилась ли запись
            cursor = await db.execute(
                "SELECT COUNT(*) FROM playlists WHERE user_id=? AND url=?", 
                (user_id, track_info['url'])
            )
            count = await cursor.fetchone()
            added = count[0] > 0
            
            if added:
                print(f"✅ Трек добавлен в плейлист: {track_info['title'][:50]} для user {user_id}")
            else:
                print(f"⚠️ Трек уже существует в плейлисте: {track_info['title'][:50]}")
            
            return added
        except Exception as e:
            print(f"❌ Ошибка добавления в плейлист: {e}")
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
            print(f"🗑️ Трек удалён из плейлиста: {removed[2]} для user {user_id}")
            return {
                'url': removed[1],
                'title': removed[2],
                'duration': removed[3],
                'uploader': removed[4]
            }
        print(f"⚠️ Неверный индекс {index} для user {user_id}")
        return None

async def get_user_playlist(user_id: int) -> list[dict]:
    """Возвращает плейлист пользователя (список словарей)"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT url, title, duration, uploader, added_at FROM playlists WHERE user_id=? ORDER BY added_at",
            (user_id,))
        rows = await cursor.fetchall()
        playlist = [dict(r) for r in rows]
        print(f"📋 Получен плейлист для user {user_id}: {len(playlist)} треков")
        return playlist
