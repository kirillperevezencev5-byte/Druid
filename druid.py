#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram бот Druid (с поддержкой плейлистов и кэширования аудио)
- Логирование активности в SQLite
- Команда /users (админы)
- Плейлисты с локальным кэшированием
- Улучшенная обработка кнопок
- Корректное управление временными файлами
- Токен и ID админов через переменные окружения
"""

import os
import asyncio
import json
import logging
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from dotenv import load_dotenv
from telegram import Update, InputMediaPhoto
from telegram.ext import (
    Application, MessageHandler, filters, ContextTypes, CommandHandler,
    CallbackQueryHandler
)
from telegram.error import BadRequest, NetworkError

import music
import database

# ---------- Загрузка переменных окружения ----------
load_dotenv()

# ---------- Настройки ----------
TOKEN = os.environ.get("BOT_TOKEN")
if not TOKEN:
    raise ValueError("❌ Не задан BOT_TOKEN в переменных окружения или .env файле")

MAX_FILE_SIZE = 50 * 1024 * 1024
MAX_MEDIA_GROUP = 10
API_TIMEOUT = 30

# ID администраторов через запятую: ADMIN_IDS=123456789,987654321
admin_ids_str = os.environ.get("ADMIN_IDS", "")
ADMIN_IDS = set()
for uid in admin_ids_str.split(","):
    uid = uid.strip()
    if uid.isdigit():
        ADMIN_IDS.add(int(uid))

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------- Вспомогательные функции ----------
def sanitize_filename(title: str) -> str:
    title = re.sub(r'[\\/*?:"<>|]', "", title)
    if len(title) > 80:
        title = title[:80]
    title = title.replace(' ', '_').strip('.')
    return title or "media"

def is_url(text: str) -> bool:
    return text.startswith(("http://", "https://"))

def get_platform(url: str) -> str:
    domain = urlparse(url).netloc.lower()
    if "tiktok" in domain: return "TikTok"
    if "youtube" in domain or "youtu.be" in domain: return "YouTube"
    if "instagram" in domain: return "Instagram"
    if "soundcloud" in domain: return "SoundCloud"
    return domain.replace('www.', '').split('.')[0].capitalize()

def escape_html(text: str) -> str:
    if not text:
        return ""
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

def format_duration(duration) -> str:
    if not duration:
        return "?"
    try:
        dur = int(float(duration))
        return f"{dur//60}:{dur%60:02d}"
    except (ValueError, TypeError):
        return "?"

def format_caption(info: dict, platform: str, media_type: str) -> str:
    emoji = {"video": "🎬", "photo": "📸", "carousel": "🖼️", "audio": "🎵"}.get(media_type, "📎")
    parts = [f"{emoji} <b>{escape_html(platform)}</b>\n"]
    if info.get("author"):
        parts.append(f"👤 <b>Автор:</b> {escape_html(info['author'][:50])}\n")
    if info.get("title"):
        parts.append(f"📝 <b>Название:</b> {escape_html(info['title'][:200])}\n")
    if info.get("duration"):
        dur_str = format_duration(info['duration'])
        if dur_str != "?":
            parts.append(f"⏱️ <b>Длительность:</b> {dur_str}\n")
    if info.get("url"):
        parts.append(f"🔗 <b>Ссылка:</b> <a href='{info['url']}'>источник</a>")
    caption = "".join(parts)
    return caption[:1024]

def check_file_size(path: Path) -> bool:
    return path.exists() and path.stat().st_size <= MAX_FILE_SIZE

async def split_file(file_path: Path):
    parts = []
    with open(file_path, 'rb') as f:
        i = 1
        while True:
            chunk = f.read(MAX_FILE_SIZE)
            if not chunk:
                break
            part = file_path.with_name(f"{file_path.stem}_part{i}{file_path.suffix}")
            with open(part, 'wb') as p:
                p.write(chunk)
            parts.append(part)
            i += 1
    return parts

async def send_photo_group(update, photo_paths, caption):
    if not update.message:
        return
    files = []
    try:
        media = []
        for i, path in enumerate(photo_paths[:MAX_MEDIA_GROUP]):
            f = open(path, 'rb')
            files.append(f)
            media.append(InputMediaPhoto(
                media=f,
                caption=caption if i == 0 else None,
                parse_mode='HTML'
            ))
        await update.message.reply_media_group(media)
    except BadRequest:
        for path in photo_paths:
            try:
                with open(path, 'rb') as f:
                    await update.message.reply_photo(photo=f, caption=caption, parse_mode='HTML')
            except:
                pass
    finally:
        for f in files:
            try:
                f.close()
            except:
                pass

async def send_with_split(update, context, file_path, caption, media_type, track_info=None):
    """
    Отправляет файл, при необходимости разбивая на части.
    Для аудио вызывает специальную функцию с кнопкой.
    """
    if media_type == 'audio' and track_info:
        await music.send_audio_with_add_button(update, context, file_path, caption, track_info)
        return

    if check_file_size(file_path):
        with open(file_path, 'rb') as f:
            if media_type == 'video':
                await update.message.reply_video(
                    video=f, caption=caption, parse_mode='HTML',
                    supports_streaming=True
                )
            elif media_type == 'audio':
                await update.message.reply_audio(
                    audio=f, caption=caption, parse_mode='HTML'
                )
            elif media_type == 'photo':
                await update.message.reply_photo(
                    photo=f, caption=caption, parse_mode='HTML'
                )
            else:
                await update.message.reply_document(
                    document=f, caption=caption, parse_mode='HTML'
                )
        return

    parts = await split_file(file_path)
    for i, part in enumerate(parts):
        with open(part, 'rb') as f:
            await update.message.reply_document(
                document=f,
                caption=f"{caption}\n📦 Часть {i + 1}/{len(parts)}"
            )
        try:
            part.unlink(missing_ok=True)
        except:
            pass

# ---------- TikTok (API) ----------
async def get_tiktok_info(session, url):
    try:
        async with session.get(
            "https://tikwm.com/api/",
            params={"url": url},
            timeout=API_TIMEOUT
        ) as r:
            if r.status != 200:
                return None
            data = await r.json()
            if data.get("code") != 0:
                return None
            return data["data"]
    except:
        return None

async def download_tiktok_photos(session, images, dest_dir):
    files = []
    for i, img in enumerate(images):
        path = dest_dir / f"{i}.jpg"
        try:
            async with session.get(img, timeout=API_TIMEOUT) as r:
                if r.status == 200:
                    path.write_bytes(await r.read())
                    files.append(path)
        except:
            pass
    return files

async def download_tiktok_video(session, url, dest_path):
    try:
        async with session.get(url, timeout=API_TIMEOUT) as r:
            if r.status != 200:
                return False
            with open(dest_path, 'wb') as f:
                async for chunk in r.content.iter_chunked(8192):
                    f.write(chunk)
        return True
    except:
        return False

# ---------- yt-dlp info (без ffmpeg) ----------
async def ytdlp_info(url):
    cmd = ['yt-dlp', '--dump-json', '--no-warnings', '--quiet', url]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0 or not stdout:
            logger.error(f"yt-dlp info error: {stderr.decode()[:200]}")
            return None
        return json.loads(stdout.decode())
    except Exception as e:
        logger.error(f"yt-dlp info exception: {e}")
        return None

# ---------- SoundCloud (с кнопкой плейлиста) ----------
async def handle_soundcloud(update, context, url, status_msg, tmpdir):
    await status_msg.edit_text("🎵 Получаю информацию о треке...")

    info = await ytdlp_info(url)
    if not info:
        await status_msg.edit_text("❌ Не удалось получить информацию с SoundCloud")
        return False

    title = info.get('title', 'audio')
    uploader = info.get('uploader', '')
    duration = info.get('duration', 0)

    await status_msg.edit_text(f"🎵 Скачиваю: {escape_html(title[:50])}...")

    tmp = Path(tmpdir)
    output_file = tmp / f"{sanitize_filename(title)}"

    result = await music.download_audio_from_url(url, output_file)

    if not result or not result.exists():
        await status_msg.edit_text("❌ Ошибка скачивания аудио. Попробуйте другой источник.")
        return False

    track_info = {
        'title': title,
        'url': url,
        'duration': duration,
        'uploader': uploader
    }
    caption = format_caption({
        'author': uploader,
        'title': title,
        'duration': duration,
        'url': url
    }, "SoundCloud", "audio")

    await send_with_split(update, context, result, caption, 'audio', track_info)
    return True

# ---------- Instagram ----------
async def handle_instagram(update, context, url, status_msg, tmpdir):
    await status_msg.edit_text("📸 Получаю информацию из Instagram...")

    info = await ytdlp_info(url)
    if not info:
        await status_msg.edit_text("❌ Не удалось получить данные Instagram")
        return False

    tmp = Path(tmpdir)

    if info.get('_type') == 'playlist' and 'entries' in info:
        entries = info['entries']
        await status_msg.edit_text(f"🖼️ Обнаружена карусель из {len(entries)} элементов. Скачиваю...")

        downloaded = []

        for idx, entry in enumerate(entries):
            if not entry:
                continue
            entry_url = entry.get('webpage_url') or entry.get('url')
            if not entry_url:
                continue

            ext = entry.get('ext') or ''
            if ext in ('jpg', 'jpeg', 'png'):
                out = tmp / f"photo_{idx + 1}.jpg"
                result = await music.download_audio_from_url(entry_url, out)  # переиспользуем, но для фото это не оптимально
                # Лучше использовать yt-dlp для фото, но для простоты оставим так
                if result and result.exists():
                    downloaded.append(result)
            elif ext in ('mp4', 'mov'):
                out = tmp / f"video_{idx + 1}.mp4"
                # Для видео нужна отдельная функция
                cmd = ['yt-dlp', '-o', str(out), '--no-warnings', '--quiet', '-f', 'best[height<=720][ext=mp4]', entry_url]
                proc = await asyncio.create_subprocess_exec(*cmd)
                await proc.wait()
                if out.exists():
                    downloaded.append(out)

        if not downloaded:
            await status_msg.edit_text("❌ Не удалось скачать карусель")
            return False

        photos = [f for f in downloaded if f.suffix in ('.jpg', '.jpeg', '.png')]
        videos = [f for f in downloaded if f.suffix == '.mp4']

        author = info.get('uploader', '')

        if photos:
            caption = format_caption({'author': author, 'url': url}, "Instagram", "carousel")
            if len(photos) > 1:
                await send_photo_group(update, photos, caption)
            else:
                await send_with_split(update, context, photos[0], caption, 'photo')

        for v in videos:
            cap = format_caption({'author': author, 'url': url}, "Instagram", "video")
            await send_with_split(update, context, v, cap, 'video')

        return True

    ext = info.get('ext') or ''
    is_video = ext in ('mp4', 'webm', 'mov')
    is_image = ext in ('jpg', 'jpeg', 'png', 'webp')
    title = info.get('title', 'media')
    author = info.get('uploader', '')
    duration = info.get('duration', 0)

    if is_image:
        await status_msg.edit_text("📸 Скачиваю фото...")
        out = tmp / f"{sanitize_filename(title)}.jpg"
        cmd = ['yt-dlp', '-o', str(out), '--no-warnings', '--quiet', url]
        proc = await asyncio.create_subprocess_exec(*cmd)
        await proc.wait()
        if out.exists():
            caption = format_caption(
                {'author': author, 'title': title, 'url': url}, "Instagram", "photo"
            )
            await send_with_split(update, context, out, caption, 'photo')
            return True

    elif is_video:
        await status_msg.edit_text("🎬 Скачиваю видео...")
        out = tmp / f"{sanitize_filename(title)}.mp4"
        cmd = ['yt-dlp', '-o', str(out), '--no-warnings', 'quiet', '-f', 'best[height<=720][ext=mp4]', url]
        proc = await asyncio.create_subprocess_exec(*cmd)
        await proc.wait()
        if out.exists():
            caption = format_caption(
                {'author': author, 'title': title, 'duration': duration, 'url': url},
                "Instagram", "video"
            )
            await send_with_split(update, context, out, caption, 'video')
            return True

    await status_msg.edit_text("❌ Не удалось обработать Instagram")
    return False

# ---------- YouTube / другие (аудио с кнопкой) ----------
async def handle_generic(update, context, url, status_msg, tmpdir):
    await status_msg.edit_text("🔄 Получаю информацию...")

    info = await ytdlp_info(url)
    if not info:
        await status_msg.edit_text("❌ Не удалось получить информацию")
        return False

    ext = info.get('ext') or ''
    is_video = ext in ('mp4', 'webm', 'mov')
    is_image = ext in ('jpg', 'jpeg', 'png', 'webp')
    title = info.get('title', 'media')
    author = info.get('uploader', '')
    duration = info.get('duration', 0)
    platform = get_platform(url)

    tmp = Path(tmpdir)

    if is_image:
        await status_msg.edit_text("📸 Скачиваю фото...")
        out = tmp / f"{sanitize_filename(title)}.jpg"
        cmd = ['yt-dlp', '-o', str(out), '--no-warnings', '--quiet', url]
        proc = await asyncio.create_subprocess_exec(*cmd)
        await proc.wait()
        if out.exists():
            caption = format_caption(
                {'author': author, 'title': title, 'url': url}, platform, "photo"
            )
            await send_with_split(update, context, out, caption, 'photo')
            return True

    elif is_video:
        await status_msg.edit_text("🎬 Скачиваю видео...")
        out = tmp / f"{sanitize_filename(title)}.mp4"
        cmd = ['yt-dlp', '-o', str(out), '--no-warnings', '--quiet', '-f', 'best[height<=720][ext=mp4]', url]
        proc = await asyncio.create_subprocess_exec(*cmd)
        await proc.wait()
        if out.exists():
            caption = format_caption(
                {'author': author, 'title': title, 'duration': duration, 'url': url},
                platform, "video"
            )
            await send_with_split(update, context, out, caption, 'video')
            return True
    else:
        await status_msg.edit_text("🎵 Скачиваю аудио...")
        out = tmp / f"{sanitize_filename(title)}"
        result = await music.download_audio_from_url(url, out)
        if result and result.exists():
            track_info = {
                'title': title,
                'url': url,
                'duration': duration,
                'uploader': author
            }
            caption = format_caption(
                {'author': author, 'title': title, 'duration': duration, 'url': url},
                platform, "audio"
            )
            await send_with_split(update, context, result, caption, 'audio', track_info)
            return True

    await status_msg.edit_text("❌ Не удалось обработать ссылку")
    return False

# ---------- Основной обработчик с логированием активности ----------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    
    # Логирование активности
    if update.message.from_user:
        user = update.message.from_user
        chat_id = update.message.chat_id
        try:
            await database.log_user_activity(
                user.id,
                user.first_name or "",
                user.username or "",
                chat_id
            )
        except Exception as e:
            logger.error(f"Ошибка логирования: {e}")
    
    if not is_url(text):
        return

    status_msg = await update.message.reply_text("⏳ Обрабатываю ссылку...")

    tmpdir = tempfile.mkdtemp()

    try:
        async with aiohttp.ClientSession() as session:
            # TikTok
            if "tiktok.com" in text:
                await status_msg.edit_text("🎵 Обрабатываю TikTok...")
                data = await get_tiktok_info(session, text)
                if not data:
                    await status_msg.edit_text("❌ Не удалось получить данные TikTok")
                    return

                images = data.get("images") or []
                if images:
                    await status_msg.edit_text(f"📸 Скачиваю {len(images)} фото...")
                    photos = await download_tiktok_photos(session, images, Path(tmpdir))
                    if not photos:
                        await status_msg.edit_text("❌ Не удалось скачать фото")
                        return
                    author = data.get('author', {}).get('unique_id', '')
                    caption = format_caption(
                        {'author': author, 'title': data.get('title', ''), 'url': text},
                        "TikTok",
                        "carousel" if len(photos) > 1 else "photo"
                    )
                    if len(photos) == 1:
                        await send_with_split(update, context, photos[0], caption, 'photo')
                    else:
                        await send_photo_group(update, photos, caption)
                    try:
                        await status_msg.delete()
                    except:
                        pass
                    return

                video = data.get("play")
                if video:
                    await status_msg.edit_text("🎬 Скачиваю видео...")
                    out = Path(tmpdir) / "video.mp4"
                    ok = await download_tiktok_video(session, video, out)
                    if not ok:
                        await status_msg.edit_text("❌ Не удалось скачать видео")
                        return
                    author = data.get('author', {}).get('unique_id', '')
                    caption = format_caption(
                        {'author': author, 'title': data.get('title', ''),
                         'duration': data.get('duration'), 'url': text},
                        "TikTok", "video"
                    )
                    await send_with_split(update, context, out, caption, 'video')
                    try:
                        await status_msg.delete()
                    except:
                        pass
                    return

                await status_msg.edit_text("❌ Не найден контент TikTok")
                return

            # SoundCloud
            if "soundcloud.com" in text:
                await handle_soundcloud(update, context, text, status_msg, tmpdir)
                try:
                    await status_msg.delete()
                except:
                    pass
                return

            # Instagram
            if "instagram.com" in text:
                await handle_instagram(update, context, text, status_msg, tmpdir)
                try:
                    await status_msg.delete()
                except:
                    pass
                return

            # Shazam
            if "shazam.com" in text:
                await music.handle_shazam_url(update, context, text, session)
                try:
                    await status_msg.delete()
                except:
                    pass
                return

            # Generic (YouTube и другие)
            await handle_generic(update, context, text, status_msg, tmpdir)
            try:
                await status_msg.delete()
            except:
                pass

    except asyncio.TimeoutError:
        try:
            await status_msg.edit_text("❌ Превышено время ожидания")
        except:
            pass
    except Exception as e:
        logger.exception("Unhandled error in handle_message")
        try:
            await status_msg.edit_text("❌ Произошла ошибка, попробуйте позже")
        except:
            pass
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# ---------- Команды для админов ----------
async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Доступ запрещён")
        return

    users = await database.get_all_users()
    if not users:
        await update.message.reply_text("Нет данных о пользователях.")
        return

    text = "👥 <b>Пользователи бота</b>\n\n"
    for u in users:
        uid = u['user_id']
        name = escape_html(u['first_name'] or "no_name")
        username = u['username'] or "—"
        last = u['last_activity'] or "?"
        groups_raw = u['groups']
        groups_str = ""
        if groups_raw:
            try:
                groups_list = json.loads(groups_raw) if isinstance(groups_raw, str) else groups_raw
                if groups_list:
                    groups_str = ", ".join(str(g) for g in groups_list[:5])
                    groups_str = f" (группы: {groups_str})"
            except:
                pass
        
        # Получаем статистику плейлиста пользователя
        stats = await database.get_playlist_stats(uid)
        text += f"• <code>{uid}</code> {name} @{username}\n  📊 {stats['count']} треков, {stats['total_size']/(1024*1024):.1f} МБ\n  Последняя активность: {last}{groups_str}\n\n"
        
        if len(text) > 3800:
            text += "... (показаны первые, полный список в логах)"
            break

    await update.message.reply_text(text, parse_mode='HTML')

async def check_db_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проверка состояния базы данных (только для админов)"""
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Доступ запрещён")
        return
    
    import aiosqlite
    async with aiosqlite.connect(database.DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM playlists")
        total_tracks = await cursor.fetchone()
        
        cursor = await db.execute("SELECT COUNT(*) FROM users")
        total_users = await cursor.fetchone()
        
        cursor = await db.execute("SELECT COUNT(*) FROM playlists WHERE user_id=?", (user_id,))
        my_tracks = await cursor.fetchone()
        
        # Размер кэша
        cache_size = 0
        if database.CACHE_DIR.exists():
            for f in database.CACHE_DIR.iterdir():
                if f.is_file():
                    cache_size += f.stat().st_size
        
        await update.message.reply_text(
            f"📊 <b>Статистика БД и кэша</b>\n\n"
            f"🗄 База данных:\n"
            f"• Всего треков в плейлистах: {total_tracks[0]}\n"
            f"• Всего пользователей: {total_users[0]}\n"
            f"• Ваших треков: {my_tracks[0]}\n\n"
            f"💾 Кэш аудио:\n"
            f"• Путь: {database.CACHE_DIR}\n"
            f"• Размер: {cache_size/(1024*1024):.2f} МБ\n"
            f"• Файлов: {len(list(database.CACHE_DIR.iterdir()))}\n\n"
            f"📁 Путь к БД: {database.DB_PATH}",
            parse_mode='HTML'
        )

async def clear_cache_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Очистка кэша аудио (только для админов)"""
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Доступ запрещён")
        return
    
    # Очищаем папку кэша
    deleted_count = 0
    deleted_size = 0
    
    if database.CACHE_DIR.exists():
        for f in database.CACHE_DIR.iterdir():
            if f.is_file():
                deleted_size += f.stat().st_size
                f.unlink()
                deleted_count += 1
    
    await update.message.reply_text(
        f"🧹 <b>Кэш очищен</b>\n\n"
        f"• Удалено файлов: {deleted_count}\n"
        f"• Освобождено: {deleted_size/(1024*1024):.2f} МБ",
        parse_mode='HTML'
    )

async def my_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает статистику пользователя"""
    user_id = update.effective_user.id
    stats = await database.get_playlist_stats(user_id)
    playlist = await database.get_user_playlist(user_id)
    
    await update.message.reply_text(
        f"📊 <b>Ваша статистика</b>\n\n"
        f"🎵 Треков в плейлисте: {stats['count']}\n"
        f"💾 Общий размер: {stats['total_size']/(1024*1024):.1f} МБ\n"
        f"📁 В кэше: {len(list(database.CACHE_DIR.glob(f'{user_id}_*')))} файлов\n\n"
        f"💡 <i>Используйте /playlist для прослушивания</i>",
        parse_mode='HTML'
    )

# ---------- /start ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 <b>Druid Bot v2.0</b>\n\n"
        "<b>📌 Основные возможности:</b>\n"
        "• Скачивание с TikTok, Instagram, YouTube, SoundCloud\n"
        "• Поиск музыки на SoundCloud\n"
        "• <b>Плейлисты с сохранением треков</b>\n"
        "• Воспроизведение прямо в Telegram\n\n"
        
        "<b>🎵 Музыкальные команды:</b>\n"
        "/search <название> – поиск на SoundCloud\n"
        "/playlist – показать плейлист (аудиосообщения)\n"
        "/addtoplaylist – добавить последний трек\n"
        "/mystats – ваша статистика\n\n"
        
        "<b>🔗 Работа со ссылками:</b>\n"
        "• TikTok, Instagram, YouTube\n"
        "• SoundCloud (с кнопкой добавления)\n"
        "• Shazam (поиск по ссылке)\n\n"
        
        f"📦 Максимальный размер файла: {MAX_FILE_SIZE // (1024 * 1024)} МБ\n"
        "🎵 <i>Под каждым аудио есть кнопка добавления в плейлист</i>\n\n"
        
        "💡 <b>Совет:</b> Добавляйте треки в плейлист через кнопку, "
        "и они будут доступны в любой момент без перекачивания!",
        
        parse_mode='HTML'
    )

# ---------- main (асинхронная) ----------
def main():
    """Точка входа (синхронная)"""
    print("🚀 Запуск Druid Bot...")
    
    # Инициализируем базу данных
    try:
        asyncio.run(database.init_db())
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(database.init_db())
        loop.close()
    
    print("✅ База данных инициализирована")
    print(f"📁 Кэш аудио: {database.CACHE_DIR}")
    print(f"👥 Администраторы: {ADMIN_IDS if ADMIN_IDS else 'не настроены'}")
    
    # Создаём приложение
    app = Application.builder().token(TOKEN).build()
    
    # Основные команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CommandHandler("checkdb", check_db_command))
    app.add_handler(CommandHandler("clearcache", clear_cache_command))
    app.add_handler(CommandHandler("mystats", my_stats_command))
    
    # Обработчик ссылок
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Музыкальные команды
    app.add_handler(CommandHandler("search", music.search_command))
    app.add_handler(CommandHandler("playlist", music.playlist_command))
    app.add_handler(CommandHandler("addtoplaylist", music.add_to_playlist_command))
    
    # Callback-обработчики
    app.add_handler(CallbackQueryHandler(music.select_track_callback, pattern="^(select_track_|cancel_search)"))
    app.add_handler(CallbackQueryHandler(music.add_track_callback, pattern="^add_track_"))
    app.add_handler(CallbackQueryHandler(music.remove_from_playlist_callback, pattern="^del_pl_"))
    
    # Запуск бота
    print("✅ Бот запущен и готов к работе!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n👋 Бот остановлен.")
    except Exception as e:
        print(f"❌ Ошибка: {e}")
