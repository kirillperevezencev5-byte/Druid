#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram бот Druid (упрощённая версия)
"""

import os
import logging
import subprocess
import re
import json
import tempfile
import asyncio
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse
from pathlib import Path

import aiohttp
from telegram import Update, InputMediaPhoto
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler
from telegram.request import HTTPXRequest
from telegram.error import BadRequest

# ============ НАСТРОЙКИ ============
TOKEN = "8783056247:AAHGJF9vtDwuoCQBwfhdYOqQgFRsgfGAAp4"
MAX_FILE_SIZE = 50 * 1024 * 1024
MAX_MEDIA_GROUP = 10
CLEANUP_INTERVAL = 7200
YTDLP_TIMEOUT = 600
MAX_CONCURRENT_DOWNLOADS = 3
# ===================================

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

DOWNLOADS_DIR = Path("downloads")
DOWNLOADS_DIR.mkdir(exist_ok=True)

download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
user_locks: Dict[int, asyncio.Lock] = {}

# Простая проверка при запуске (один раз)
YTDLP_AVAILABLE = None

def check_ytdlp():
    global YTDLP_AVAILABLE
    if YTDLP_AVAILABLE is None:
        try:
            subprocess.run(['yt-dlp', '--version'], capture_output=True, check=True, timeout=5)
            YTDLP_AVAILABLE = True
        except Exception:
            YTDLP_AVAILABLE = False
    return YTDLP_AVAILABLE

def is_url(text: str) -> str:
    pattern = re.compile(r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+(?:/[^\s]*)?')
    match = pattern.search(text.strip())
    return match.group(0) if match else ""

def sanitize_filename(title: str) -> str:
    title = re.sub(r'[\\/*?:"<>|]', "", title)
    if len(title) > 80:
        title = title[:80]
    title = title.replace(' ', '_').strip('.')
    return title if title else "media"

def escape_html(text: str) -> str:
    if not text:
        return text
    return (text.replace('&', '&amp;').replace('<', '&lt;')
                .replace('>', '&gt;').replace('"', '&quot;'))

def format_caption(info: Dict[str, Any], platform: str, media_type: str) -> str:
    emojis = {'фото': '📸', 'видео': '🎬', 'карусель': '🖼️', 'аудио': '🎵'}
    parts = [f"{emojis.get(media_type, '📎')} <b>{escape_html(platform)}</b>\n"]
    if info.get('author'):
        parts.append(f"👤 <b>Автор:</b> {escape_html(info['author'][:50])}\n")
    if info.get('description'):
        desc = escape_html(info['description'][:200])
        if len(info.get('description', '')) > 200:
            desc += "..."
        parts.append(f"📝 <b>Описание:</b> {desc}\n")
    if info.get('duration'):
        minutes = info['duration'] // 60
        seconds = info['duration'] % 60
        parts.append(f"⏱️ <b>Длительность:</b> {minutes}:{seconds:02d}\n")
    if info.get('url'):
        parts.append(f"🔗 <b>Источник:</b> <a href='{info['url']}'>ссылка</a>")
    caption = "".join(parts)
    if len(caption) > 1024:
        caption = caption[:1020] + "..."
    return caption

def check_file_size(file_path: Path, max_size: int = MAX_FILE_SIZE) -> bool:
    try:
        return file_path.exists() and file_path.stat().st_size <= max_size
    except Exception:
        return False

async def get_media_info_ytdlp(url: str) -> Optional[Dict[str, Any]]:
    if not check_ytdlp():
        return None
    
    cmd = ['yt-dlp', '--dump-json', '--no-playlist', '--no-warnings', '--quiet', url]
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        if process.returncode != 0:
            logger.error(f"yt-dlp info error: {stderr.decode()}")
            return None
        info = json.loads(stdout.decode())
        
        if info.get('_type') == 'playlist' and 'entries' in info:
            entries = []
            for entry in info['entries']:
                if entry is None:
                    continue
                ext = entry.get('ext', '')
                is_video = ext in ['mp4', 'webm', 'mkv', 'avi', 'mov']
                is_audio = entry.get('acodec') != 'none' and entry.get('vcodec') == 'none'
                is_image = ext in ['jpg', 'jpeg', 'png', 'webp', 'gif']
                media_type = 'video' if is_video else ('audio' if is_audio else ('image' if is_image else 'video'))
                entries.append({
                    'type': media_type,
                    'url': entry.get('webpage_url') or entry.get('url'),
                    'title': entry.get('title', 'media'),
                    'author': entry.get('uploader') or entry.get('channel'),
                    'description': entry.get('description', '')[:200],
                    'duration': entry.get('duration'),
                })
            if entries:
                return {'_type': 'playlist', 'entries': entries, 'playlist_title': info.get('title', 'Карусель')}
            return None
        
        ext = info.get('ext', '')
        is_video = ext in ['mp4', 'webm', 'mkv', 'avi', 'mov']
        is_audio = info.get('acodec') != 'none' and info.get('vcodec') == 'none'
        is_image = ext in ['jpg', 'jpeg', 'png', 'webp', 'gif']
        media_type = 'video' if is_video else ('audio' if is_audio else ('image' if is_image else 'video'))
        return {
            'type': media_type,
            'title': info.get('title', 'media'),
            'ext': ext,
            'author': info.get('uploader') or info.get('channel'),
            'description': info.get('description', '')[:200],
            'duration': info.get('duration'),
            'url': url,
            'webpage_url': info.get('webpage_url', url)
        }
    except Exception as e:
        logger.error(f"get_media_info error: {e}")
        return None

async def download_media_ytdlp(url: str, output_file: Path, media_type: str) -> bool:
    if not check_ytdlp():
        return False
    
    cmd = ['yt-dlp', '-o', str(output_file), '--no-playlist', '--no-warnings']
    
    if media_type == 'video':
        cmd.extend(['-f', 'best[height<=720][ext=mp4]/best[height<=480][ext=mp4]/best[ext=mp4]'])
    elif media_type == 'image':
        cmd.extend(['-f', 'best'])
    elif media_type == 'audio':
        cmd.extend(['-f', 'bestaudio', '--extract-audio', '--audio-format', 'mp3'])
        output_file = output_file.with_suffix('.mp3')
    
    cmd.append(url)
    
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=YTDLP_TIMEOUT)
        if process.returncode != 0:
            logger.error(f"yt-dlp download error: {stderr.decode()}")
            return False
        
        if not output_file.exists():
            possible = list(output_file.parent.glob(f"{output_file.stem}.*"))
            if possible:
                output_file = possible[0]
            else:
                return False
        return True
    except Exception as e:
        logger.error(f"Download error: {e}")
        return False

async def download_audio_separate(video_url: str, output_mp3: Path) -> bool:
    if not check_ytdlp():
        return False
    
    cmd = [
        'yt-dlp', '-f', 'bestaudio', '--extract-audio', 
        '--audio-format', 'mp3', '-o', str(output_mp3), video_url
    ]
    try:
        process = await asyncio.create_subprocess_exec(*cmd)
        await process.wait()
        return output_mp3.exists()
    except Exception as e:
        logger.error(f"Audio extraction error: {e}")
        return False

async def send_photo_group(update: Update, photo_paths: List[Path], caption: str):
    try:
        media_group = []
        for i, path in enumerate(photo_paths[:MAX_MEDIA_GROUP]):
            with open(path, 'rb') as f:
                cap = caption if i == 0 else None
                media_group.append(InputMediaPhoto(media=f, caption=cap, parse_mode='HTML'))
        await update.message.reply_media_group(media_group)
    except BadRequest as e:
        logger.error(f"Media group error: {e}")
        for path in photo_paths:
            try:
                with open(path, 'rb') as f:
                    await update.message.reply_photo(photo=f, caption=caption, parse_mode='HTML')
            except Exception:
                pass

async def send_media(update: Update, file_path: Path, caption: str, media_type: str):
    if not check_file_size(file_path):
        await update.message.reply_text(
            f"⚠️ Файл превышает {MAX_FILE_SIZE//(1024*1024)} МБ.\n"
            f"Telegram не позволяет отправить такой большой файл."
        )
        return
    
    with open(file_path, 'rb') as f:
        if media_type == 'video':
            await update.message.reply_video(video=f, caption=caption, parse_mode='HTML', supports_streaming=True)
        elif media_type == 'audio':
            await update.message.reply_audio(audio=f, caption=caption, parse_mode='HTML')
        elif media_type == 'photo' or media_type == 'image':
            await update.message.reply_photo(photo=f, caption=caption, parse_mode='HTML')
        else:
            await update.message.reply_document(document=f, caption=caption, parse_mode='HTML')

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 <b>Druid Bot</b>\n\n"
        "Отправьте мне ссылку на видео/аудио/фото.\n"
        f"⚠️ Ограничение: файл до {MAX_FILE_SIZE//(1024*1024)} МБ.",
        parse_mode='HTML'
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = None
    user_id = update.effective_user.id
    
    if user_id not in user_locks:
        user_locks[user_id] = asyncio.Lock()
    
    async with user_locks[user_id]:
        async with download_semaphore:
            try:
                text = update.message.text.strip()
                url = is_url(text)
                if not url:
                    return
                
                status_msg = await update.message.reply_text("⏳ Обрабатываю...")
                
                if not check_ytdlp():
                    await status_msg.edit_text("❌ Ошибка: yt-dlp не установлен")
                    return
                
                media_info = await get_media_info_ytdlp(url)
                if not media_info:
                    await status_msg.edit_text("❌ Не удалось получить информацию. Ссылка не поддерживается.")
                    return
                
                # Карусель Instagram
                if media_info.get('_type') == 'playlist':
                    entries = media_info['entries']
                    await status_msg.edit_text(f"🖼️ Карусель из {len(entries)} элементов. Скачиваю...")
                    
                    with tempfile.TemporaryDirectory() as tmpdir:
                        tmp_path = Path(tmpdir)
                        downloaded = []
                        
                        for idx, entry in enumerate(entries):
                            media_type = entry['type']
                            safe_title = sanitize_filename(entry.get('title', f'item_{idx+1}'))
                            if media_type == 'image':
                                out_file = tmp_path / f"{safe_title}.jpg"
                            elif media_type == 'video':
                                out_file = tmp_path / f"{safe_title}.mp4"
                            else:
                                continue
                            if await download_media_ytdlp(entry['url'], out_file, media_type):
                                downloaded.append(out_file)
                        
                        if not downloaded:
                            await status_msg.edit_text("❌ Не удалось скачать карусель.")
                            return
                        
                        photos = [f for f in downloaded if f.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp')]
                        videos = [f for f in downloaded if f.suffix.lower() == '.mp4']
                        
                        if photos:
                            caption = format_caption({'author': entries[0].get('author'), 'url': url}, "Instagram", "карусель")
                            if len(photos) > 1:
                                await send_photo_group(update, photos, caption)
                            else:
                                await send_media(update, photos[0], caption, 'photo')
                        
                        for video_path in videos:
                            cap = format_caption({'author': entries[0].get('author'), 'url': url}, "Instagram", "видео")
                            await send_media(update, video_path, cap, 'video')
                            
                            audio_path = tmp_path / f"{video_path.stem}_audio.mp3"
                            if await download_audio_separate(url, audio_path):
                                audio_caption = f"🎧 <b>Аудио из Instagram</b>\n👤 {escape_html(entries[0].get('author', '')[:50])}"
                                await send_media(update, audio_path, audio_caption, 'audio')
                    
                    await status_msg.delete()
                    return
                
                # Обычное медиа
                media_type = media_info['type']
                platform = urlparse(url).netloc.lower().replace('www.', '').split('.')[0].capitalize()
                media_type_rus = {'video': 'видео', 'image': 'фото', 'audio': 'аудио'}.get(media_type, 'медиа')
                
                await status_msg.edit_text(f"⏳ Скачиваю {media_type_rus}...")
                
                with tempfile.TemporaryDirectory() as tmpdir:
                    tmp_path = Path(tmpdir)
                    safe_title = sanitize_filename(media_info['title'])
                    
                    if media_type == 'image':
                        output_file = tmp_path / f"{safe_title}.jpg"
                    elif media_type == 'audio':
                        output_file = tmp_path / f"{safe_title}.mp3"
                    else:
                        output_file = tmp_path / f"{safe_title}.mp4"
                    
                    if not await download_media_ytdlp(url, output_file, media_type):
                        await status_msg.edit_text(f"❌ Не удалось скачать {media_type_rus}")
                        return
                    
                    caption = format_caption(media_info, platform, media_type_rus)
                    await send_media(update, output_file, caption, media_type)
                    
                    if media_type == 'video':
                        await status_msg.edit_text("🎵 Извлекаю аудио...")
                        audio_file = tmp_path / f"{safe_title}_audio.mp3"
                        if await download_audio_separate(url, audio_file):
                            audio_caption = f"🎧 <b>Аудио из видео</b>\n👤 {escape_html(media_info.get('author', '')[:50])}"
                            await send_media(update, audio_file, audio_caption, 'audio')
                    
                    await status_msg.delete()
            
            except Exception as e:
                logger.error(f"Handler error: {e}", exc_info=True)
                error_msg = f"⚠️ Ошибка: {str(e)[:100]}"
                if status_msg:
                    try:
                        await status_msg.edit_text(error_msg)
                    except:
                        await update.message.reply_text(error_msg)
                else:
                    await update.message.reply_text(error_msg)

async def cleanup_old_files():
    while True:
        try:
            if DOWNLOADS_DIR.exists():
                import time
                now = time.time()
                for f in DOWNLOADS_DIR.iterdir():
                    if f.is_file() and now - f.stat().st_mtime > CLEANUP_INTERVAL:
                        f.unlink()
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
        await asyncio.sleep(CLEANUP_INTERVAL)

def main():
    # Тихая проверка при запуске
    if not check_ytdlp():
        print("\n⚠️  yt-dlp не установлен. Бот будет работать только с ссылками, которые не требуют yt-dlp.")
        print("   Установите: pip install yt-dlp\n")
    else:
        print("\n✅ Druid Bot запущен")
        print(f"   Токен: {TOKEN[:10]}...{TOKEN[-5:]}")
        print(f"   yt-dlp: установлен")
        print(f"   Одновременных загрузок: {MAX_CONCURRENT_DOWNLOADS}\n")
    
    request = HTTPXRequest(connect_timeout=30, read_timeout=120, write_timeout=120, pool_timeout=30)
    application = Application.builder().token(TOKEN).request(request).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(cleanup_old_files())
    
    try:
        application.run_polling(drop_pending_updates=True)
    except KeyboardInterrupt:
        print("\n👋 Бот остановлен")
    except Exception as e:
        print(f"❌ Ошибка: {e}")
    finally:
        loop.close()

if __name__ == '__main__':
    main()
