import aiosqlite
import json
import os
from pathlib import Path

# Путь к базе данных
DB_PATH = os.path.join(os.path.dirname(__file__), "bot.db")

# Папка для кэширования аудиофайлов
CACHE_DIR = Path(__file__).parent / "cached_audio"
CACHE_DIR.mkdir(exist_ok=True)

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
                file_path TEXT NOT NULL,
                file_size INTEGER,
                added_at TEXT,
                UNIQUE(user_id, url)
            );
            
            CREATE INDEX IF NOT EXISTS idx_playlists_user ON playlists(user_id);
            CREATE INDEX IF NOT EXISTS idx_playlists_url ON playlists(url);
        """)
        await db.commit()
    
    print(f"✅ База данных инициализирована: {DB_PATH}")
    print(f"📁 Папка для аудио: {CACHE_DIR}")

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

async def add_track_to_playlist(user_id: int, track_info: dict, local_file_path: str, file_size: int) -> bool:
    """
    Добавляет трек в плейлист с указанием локального пути к файлу.
    Возвращает True, если добавлен, False если уже был.
    """
    if not track_info.get('url'):
        print(f"❌ Ошибка: нет url в track_info: {track_info}")
        return False
    
    if not track_info.get('title'):
        track_info['title'] = 'Без названия'
    
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("""
                INSERT OR IGNORE INTO playlists 
                (user_id, url, title, duration, uploader, file_path, file_size, added_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """, (user_id, track_info['url'], track_info.get('title', 'Без названия')[:200],
                  track_info.get('duration', 0), track_info.get('uploader', '')[:100], 
                  local_file_path, file_size))
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

async def get_user_playlist(user_id: int) -> list[dict]:
    """Возвращает плейлист пользователя (список словарей) с id и file_path"""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, url, title, duration, uploader, file_path, file_size, added_at FROM playlists WHERE user_id=? ORDER BY added_at",
            (user_id,)
        )
        rows = await cursor.fetchall()
        playlist = [dict(r) for r in rows]
        print(f"📋 Получен плейлист для user {user_id}: {len(playlist)} треков")
        return playlist

async def remove_track_from_playlist_by_id(user_id: int, track_id: int):
    """Удаляет трек по id. Возвращает удалённый трек или None."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, url, title, duration, uploader, file_path FROM playlists WHERE user_id=? AND id=?",
            (user_id, track_id)
        )
        track = await cursor.fetchone()
        if track:
            await db.execute("DELETE FROM playlists WHERE id=?", (track_id,))
            await db.commit()
            print(f"🗑️ Трек удалён из плейлиста: {track[2]} для user {user_id}")
            return {
                'id': track[0],
                'url': track[1],
                'title': track[2],
                'duration': track[3],
                'uploader': track[4],
                'file_path': track[5]
            }
        print(f"⚠️ Трек с id {track_id} не найден для user {user_id}")
        return None

async def update_track_file_path(track_id: int, new_file_path: str):
    """Обновляет путь к файлу в БД"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE playlists SET file_path=? WHERE id=?",
            (new_file_path, track_id)
        )
        await db.commit()
        print(f"📝 Обновлён путь к файлу для трека {track_id}: {new_file_path}")

async def get_playlist_stats(user_id: int) -> dict:
    """Возвращает статистику плейлиста: количество треков и общий размер"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) as count, SUM(file_size) as total_size FROM playlists WHERE user_id=?",
            (user_id,)
        )
        row = await cursor.fetchone()
        return {
            'count': row[0] or 0,
            'total_size': row[1] or 0
        }

async def clear_user_cache(user_id: int):
    """Очищает кэш пользователя (удаляет все его файлы)"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, file_path FROM playlists WHERE user_id=?",
            (user_id,)
        )
        tracks = await cursor.fetchall()
        
        for track in tracks:
            file_path = Path(track[1])
            if file_path.exists():
                file_path.unlink()
                print(f"🗑️ Удалён файл: {file_path}")
        
        await db.execute("DELETE FROM playlists WHERE user_id=?", (user_id,))
        await db.commit()
        print(f"🧹 Очищен кэш пользователя {user_id}: {len(tracks)} треков удалено")
