#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram бот Druid (полностью рабочий) + интеграция с музыкой (Shazam, поиск, плейлисты)
"""

import re
import json
import asyncio
import tempfile
import logging
from pathlib import Path
from urllib.parse import urlparse

# ОТКЛЮЧАЕМ ВСЕ ЛОГИ И СПАМ В КОНСОЛИ
logging.basicConfig(level=logging.ERROR)
logging.getLogger("httpx").setLevel(logging.ERROR)
logging.getLogger("httpcore").setLevel(logging.ERROR)
logging.getLogger("telegram").setLevel(logging.ERROR)
logging.getLogger("telegram.ext").setLevel(logging.ERROR)
logging.getLogger("asyncio").setLevel(logging.ERROR)

import aiohttp
from telegram import Update, InputMediaPhoto
from telegram.ext import (
    Application, MessageHandler, filters, ContextTypes, CommandHandler,
    CallbackQueryHandler
)
from telegram.error import BadRequest, NetworkError

import music

TOKEN = "8783056247:AAHGJF9vtDwuoCQBwfhdYOqQgFRsgfGAAp4"
MAX_FILE_SIZE = 50 * 1024 * 1024
MAX_MEDIA_GROUP = 10
API_TIMEOUT = 30

# ---------------------- helpers ----------------------
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
    if not text: return ""
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

def format_caption(info: dict, platform: str, media_type: str) -> str:
    emoji = {"video": "🎬", "photo": "📸", "carousel": "🖼️", "audio": "🎵"}.get(media_type, "📎")
    parts = [f"{emoji} <b>{escape_html(platform)}</b>\n"]
    if info.get("author"):
        parts.append(f"👤 <b>Автор:</b> {escape_html(info['author'][:50])}\n")
    if info.get("title"):
        parts.append(f"📝 <b>Название:</b> {escape_html(info['title'][:200])}\n")
    if info.get("duration"):
        dur = info['duration']
        m = int(dur // 60)
        s = int(dur % 60)
        parts.append(f"⏱️ <b>Длительность:</b> {m}:{s:02d}\n")
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
            except Exception:
                pass
    finally:
        for f in files:
            try: f.close()
            except: pass

async def send_with_split(update, file_path, caption, media_type):
    if check_file_size(file_path):
        with open(file_path, 'rb') as f:
            if media_type == 'video':
                await update.message.reply_video(video=f, caption=caption, parse_mode='HTML', supports_streaming=True)
            elif media_type == 'audio':
                await update.message.reply_audio(audio=f, caption=caption, parse_mode='HTML')
            elif media_type == 'photo':
                await update.message.reply_photo(photo=f, caption=caption, parse_mode='HTML')
            else:
                await update.message.reply_document(document=f, caption=caption, parse_mode='HTML')
        return
    parts = await split_file(file_path)
    for i, part in enumerate(parts):
        with open(part, 'rb') as f:
            await update.message.reply_document(document=f, caption=f"{caption}\n📦 Часть {i+1}/{len(parts)}")

# ---------------------- TikTok (API) ----------------------
async def get_tiktok_info(session, url):
    try:
        async with session.get("https://tikwm.com/api/", params={"url": url}, timeout=API_TIMEOUT) as r:
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

# ---------------------- yt-dlp core ----------------------
async def ytdlp_info(url):
    cmd = ['yt-dlp', '--dump-json', '--no-warnings', '--quiet', url]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, 
            stdout=asyncio.subprocess.PIPE, 
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            return None
        return json.loads(stdout.decode())
    except:
        return None

async def ytdlp_download(url, output_path, format_spec):
    cmd = ['yt-dlp', '-o', str(output_path), '--no-warnings', '--quiet']
    if format_spec:
        cmd += ['-f', format_spec]
    cmd.append(url)
    try:
        proc = await asyncio.create_subprocess_exec(*cmd)
        await asyncio.wait_for(proc.wait(), timeout=120)
        if proc.returncode != 0:
            return None
        if not output_path.exists():
            matches = list(output_path.parent.glob(f"{output_path.stem}.*"))
            return matches[0] if matches else None
        return output_path
    except:
        return None

# ---------------------- SoundCloud ----------------------
async def handle_soundcloud(update, url, status_msg):
    await status_msg.edit_text("🎵 Получаю информацию о треке...")
    
    info = await ytdlp_info(url)
    if not info:
        await status_msg.edit_text("❌ Не удалось получить информацию с SoundCloud")
        return False
    
    title = info.get('title', 'audio')
    uploader = info.get('uploader', '')
    duration = info.get('duration', 0)
    
    await status_msg.edit_text(f"🎵 Скачиваю: {title[:50]}...")
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        output_file = tmp / f"{sanitize_filename(title)}.%(ext)s"
        
        result = await ytdlp_download(url, output_file, 'bestaudio')
        
        if not result or not result.exists():
            await status_msg.edit_text("❌ Ошибка скачивания аудио")
            return False
        
        caption = format_caption({
            'author': uploader,
            'title': title,
            'duration': duration,
            'url': url
        }, "SoundCloud", "audio")
        
        await send_with_split(update, result, caption, 'audio')
        return True

# ---------------------- Instagram ----------------------
async def handle_instagram(update, url, status_msg):
    await status_msg.edit_text("📸 Получаю информацию из Instagram...")
    
    info = await ytdlp_info(url)
    if not info:
        await status_msg.edit_text("❌ Не удалось получить данные Instagram")
        return False
    
    if info.get('_type') == 'playlist' and 'entries' in info:
        entries = info['entries']
        await status_msg.edit_text(f"🖼️ Обнаружена карусель из {len(entries)} элементов. Скачиваю...")
        
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            downloaded = []
            
            for idx, entry in enumerate(entries):
                if not entry:
                    continue
                entry_url = entry.get('webpage_url') or entry.get('url')
                if not entry_url:
                    continue
                
                ext = entry.get('ext', '')
                if ext in ('jpg', 'jpeg', 'png'):
                    out = tmp / f"photo_{idx+1}.jpg"
                    result = await ytdlp_download(entry_url, out, 'best')
                elif ext in ('mp4', 'mov'):
                    out = tmp / f"video_{idx+1}.mp4"
                    result = await ytdlp_download(entry_url, out, 'best[height<=720][ext=mp4]')
                else:
                    continue
                    
                if result and result.exists():
                    downloaded.append(result)
            
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
                    await send_with_split(update, photos[0], caption, 'photo')
            
            for v in videos:
                cap = format_caption({'author': author, 'url': url}, "Instagram", "video")
                await send_with_split(update, v, cap, 'video')
        
        return True
    
    ext = info.get('ext', '')
    is_video = ext in ('mp4', 'webm', 'mov')
    is_image = ext in ('jpg', 'jpeg', 'png', 'webp')
    title = info.get('title', 'media')
    author = info.get('uploader', '')
    duration = info.get('duration', 0)
    
    if is_image:
        await status_msg.edit_text("📸 Скачиваю фото...")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            out = tmp / f"{sanitize_filename(title)}.jpg"
            result = await ytdlp_download(url, out, 'best')
            if result and result.exists():
                caption = format_caption({'author': author, 'title': title, 'url': url}, "Instagram", "photo")
                await send_with_split(update, result, caption, 'photo')
                return True
                
    elif is_video:
        await status_msg.edit_text("🎬 Скачиваю видео...")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            out = tmp / f"{sanitize_filename(title)}.mp4"
            result = await ytdlp_download(url, out, 'best[height<=720][ext=mp4]')
            if result and result.exists():
                caption = format_caption({'author': author, 'title': title, 'duration': duration, 'url': url}, "Instagram", "video")
                await send_with_split(update, result, caption, 'video')
                return True
    
    await status_msg.edit_text("❌ Не удалось обработать Instagram")
    return False

# ---------------------- YouTube / другие ----------------------
async def handle_generic(update, url, status_msg):
    await status_msg.edit_text("🔄 Получаю информацию...")
    
    info = await ytdlp_info(url)
    if not info:
        await status_msg.edit_text("❌ Не удалось получить информацию")
        return False
    
    ext = info.get('ext', '')
    is_video = ext in ('mp4', 'webm', 'mov')
    is_image = ext in ('jpg', 'jpeg', 'png', 'webp')
    title = info.get('title', 'media')
    author = info.get('uploader', '')
    duration = info.get('duration', 0)
    platform = get_platform(url)
    
    if is_image:
        await status_msg.edit_text("📸 Скачиваю фото...")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            out = tmp / f"{sanitize_filename(title)}.jpg"
            result = await ytdlp_download(url, out, 'best')
            if result and result.exists():
                caption = format_caption({'author': author, 'title': title, 'url': url}, platform, "photo")
                await send_with_split(update, result, caption, 'photo')
                return True
                
    elif is_video:
        await status_msg.edit_text("🎬 Скачиваю видео...")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            out = tmp / f"{sanitize_filename(title)}.mp4"
            result = await ytdlp_download(url, out, 'best[height<=720][ext=mp4]')
            if result and result.exists():
                caption = format_caption({'author': author, 'title': title, 'duration': duration, 'url': url}, platform, "video")
                await send_with_split(update, result, caption, 'video')
                return True
    else:
        await status_msg.edit_text("🎵 Скачиваю аудио...")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            out = tmp / f"{sanitize_filename(title)}.%(ext)s"
            result = await ytdlp_download(url, out, 'bestaudio')
            if result and result.exists():
                caption = format_caption({'author': author, 'title': title, 'duration': duration, 'url': url}, platform, "audio")
                await send_with_split(update, result, caption, 'audio')
                return True
    
    await status_msg.edit_text("❌ Не удалось обработать ссылку")
    return False

# ---------------------- основной обработчик ----------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not is_url(text):
        return

    status_msg = await update.message.reply_text("⏳ Обрабатываю ссылку...")
    
    try:
        async with aiohttp.ClientSession() as session:
            if "tiktok.com" in text:
                await status_msg.edit_text("🎵 Обрабатываю TikTok...")
                
                data = await get_tiktok_info(session, text)
                if not data:
                    await status_msg.edit_text("❌ Не удалось получить данные TikTok")
                    return
                
                images = data.get("images") or []
                if images:
                    await status_msg.edit_text(f"📸 Скачиваю {len(images)} фото...")
                    with tempfile.TemporaryDirectory() as tmpdir:
                        tmp = Path(tmpdir)
                        photos = await download_tiktok_photos(session, images, tmp)
                        if not photos:
                            await status_msg.edit_text("❌ Не удалось скачать фото")
                            return
                        
                        author = data.get('author', {}).get('unique_id', '')
                        caption = format_caption({
                            'author': author, 
                            'title': data.get('title', ''),
                            'url': text
                        }, "TikTok", "carousel" if len(photos) > 1 else "photo")
                        
                        if len(photos) == 1:
                            await send_with_split(update, photos[0], caption, 'photo')
                        else:
                            await send_photo_group(update, photos, caption)
                        await status_msg.delete()
                    return
                
                video = data.get("play")
                if video:
                    await status_msg.edit_text("🎬 Скачиваю видео...")
                    with tempfile.TemporaryDirectory() as tmpdir:
                        tmp = Path(tmpdir)
                        out = tmp / "video.mp4"
                        ok = await download_tiktok_video(session, video, out)
                        if not ok:
                            await status_msg.edit_text("❌ Не удалось скачать видео")
                            return
                        
                        author = data.get('author', {}).get('unique_id', '')
                        caption = format_caption({
                            'author': author,
                            'title': data.get('title', ''),
                            'duration': data.get('duration'),
                            'url': text
                        }, "TikTok", "video")
                        
                        await send_with_split(update, out, caption, 'video')
                        await status_msg.delete()
                    return
                
                await status_msg.edit_text("❌ Не найден контент TikTok")
                return

            if "soundcloud.com" in text:
                await handle_soundcloud(update, text, status_msg)
                await status_msg.delete()
                return

            if "instagram.com" in text:
                await handle_instagram(update, text, status_msg)
                await status_msg.delete()
                return

            if "shazam.com" in text:
                await music.handle_shazam_url(update, context, text, session)
                await status_msg.delete()
                return

            await handle_generic(update, text, status_msg)
            await status_msg.delete()

    except asyncio.TimeoutError:
        try:
            await status_msg.edit_text("❌ Превышено время ожидания")
        except:
            pass
    except Exception as e:
        try:
            await status_msg.edit_text(f"❌ Ошибка: {str(e)[:100]}")
        except:
            pass

# ---------------------- start ----------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 <b>Druid Bot</b>\n\n"
        "Отправьте ссылку на видео/аудио/фото с:\n"
        "• TikTok (фото-карусели и видео)\n"
        "• SoundCloud (аудио с названием трека)\n"
        "• Instagram (фото, видео, карусели)\n"
        "• Shazam (ссылка на трек)\n"
        "• YouTube и другие\n\n"
        "🎵 <b>Музыкальные команды:</b>\n"
        "/search <название> – поиск и скачивание трека\n"
        "/playlist – показать ваш плейлист\n"
        "/addtoplaylist – добавить последний скачанный трек в плейлист\n"
        "/play <номер> – прослушать трек из плейлиста\n"
        "/removefromplaylist <номер> – удалить трек\n\n"
        f"📦 Максимальный размер файла: {MAX_FILE_SIZE//(1024*1024)} МБ\n"
        "📎 Большие файлы автоматически разделяются на части\n"
        "🎵 Аудио сохраняется с оригинальным названием",
        parse_mode='HTML'
    )

# ---------------------- main ----------------------
def main():
    # Только одно сообщение при запуске
    print("🚀 Бот запущен. Нажмите Ctrl+C для остановки.")
    
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    app.add_handler(CommandHandler("search", music.search_command))
    app.add_handler(CommandHandler("playlist", music.playlist_command))
    app.add_handler(CommandHandler("addtoplaylist", music.add_to_playlist_command))
    app.add_handler(CommandHandler("removefromplaylist", music.remove_from_playlist_command))
    app.add_handler(CommandHandler("play", music.play_from_playlist))
    app.add_handler(CallbackQueryHandler(music.select_track_callback, pattern="^(select_track_|cancel_search)"))
    
    # Запускаем с подавлением ошибок сети
    try:
        app.run_polling(drop_pending_updates=True)
    except NetworkError:
        # Просто игнорируем ошибки сети, не спамим в консоль
        pass
    except KeyboardInterrupt:
        print("\n👋 Бот остановлен.")

if __name__ == "__main__":
    main()
