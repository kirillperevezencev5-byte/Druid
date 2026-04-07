#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram бот Druid (расширенная версия)
- TikTok: API + fallback на yt-dlp
- YouTube, Instagram, SoundCloud и другие: yt-dlp
- Разделение видео/аудио через ffmpeg (по длительности)
- Отправка отдельного MP3 для видео и фото (если есть аудио)
- Защита от множества запросов (семафор + блокировка по пользователю)
"""

import os
import logging
import subprocess
import re
import json
import time
import tempfile
import asyncio
from typing import Optional, List, Dict, Any
from urllib.parse import urlparse
from pathlib import Path

import aiohttp
from telegram import Update, InputMediaPhoto, InputMediaDocument
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler
from telegram.request import HTTPXRequest
from telegram.error import BadRequest

# ============ НАСТРОЙКИ ============
TOKEN = "8783056247:AAHGJF9vtDwuoCQBwfhdYOqQgFRsgfGAAp4"   # <-- Токен оставлен в коде по вашей просьбе
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
MAX_MEDIA_GROUP = 10
CLEANUP_INTERVAL = 7200
YTDLP_TIMEOUT = 600
API_TIMEOUT = 30
RETRY_COUNT = 3
CHUNK_DURATION = 300  # 5 минут для разделения видео/аудио
MAX_CONCURRENT_DOWNLOADS = 3   # одновременных загрузок
# ===================================

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

DOWNLOADS_DIR = Path("downloads")
DOWNLOADS_DIR.mkdir(exist_ok=True)

# Глобальные ограничители
download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
user_locks: Dict[int, asyncio.Lock] = {}

# ============ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ============

def is_ytdlp_available() -> bool:
    try:
        subprocess.run(['yt-dlp', '--version'], capture_output=True, check=True, timeout=5)
        return True
    except Exception:
        return False

def is_ffmpeg_available() -> bool:
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True, timeout=5)
        return True
    except Exception:
        return False

def sanitize_filename(title: str) -> str:
    title = re.sub(r'[\\/*?:"<>|]', "", title)
    if len(title) > 80:
        title = title[:80]
    title = title.replace(' ', '_').strip('.')
    return title if title else "media"

def is_url(text: str) -> str:
    """Извлекает первую ссылку из текста или возвращает пустую строку."""
    pattern = re.compile(
        r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+(?:/[^\s]*)?'
    )
    match = pattern.search(text.strip())
    return match.group(0) if match else ""

def get_platform_name(url: str) -> str:
    domain = urlparse(url).netloc.lower()
    platforms = {
        'youtube.com': 'YouTube', 'youtu.be': 'YouTube',
        'tiktok.com': 'TikTok', 'instagram.com': 'Instagram',
        'twitter.com': 'Twitter/X', 'x.com': 'Twitter/X',
        'facebook.com': 'Facebook', 'vimeo.com': 'Vimeo',
        'reddit.com': 'Reddit', 'pinterest.com': 'Pinterest',
        'soundcloud.com': 'SoundCloud'
    }
    for key, value in platforms.items():
        if key in domain:
            return value
    return domain.replace('www.', '').split('.')[0].capitalize()

def escape_html(text: str) -> str:
    if not text:
        return text
    return (text.replace('&', '&amp;').replace('<', '&lt;')
                .replace('>', '&gt;').replace('"', '&quot;'))

def format_caption(info: Dict[str, Any], platform: str, media_type: str) -> str:
    """Формирует подпись, обрезая до 1024 символов."""
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
    # Telegram ограничение 1024 символа
    if len(caption) > 1024:
        caption = caption[:1020] + "..."
    return caption

def check_file_size(file_path: Path, max_size: int = MAX_FILE_SIZE) -> bool:
    try:
        return file_path.exists() and file_path.stat().st_size <= max_size
    except Exception:
        return False

# ============ РАЗДЕЛЕНИЕ ФАЙЛОВ ЧЕРЕЗ FFMPEG ============

async def split_video_ffmpeg(input_path: Path, chunk_duration: int = CHUNK_DURATION) -> List[Path]:
    """
    Нарезает видео на сегменты по chunk_duration секунд.
    Возвращает список путей к сегментам.
    """
    parts = []
    base_name = input_path.stem
    output_pattern = str(input_path.parent / f"{base_name}_part%03d.mp4")
    
    cmd = [
        'ffmpeg', '-i', str(input_path),
        '-c', 'copy',
        '-map', '0',
        '-segment_time', str(chunk_duration),
        '-f', 'segment',
        '-reset_timestamps', '1',
        output_pattern
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await process.communicate()
        
        # Собираем созданные файлы
        for i in range(1, 100):
            part_path = input_path.parent / f"{base_name}_part{i:03d}.mp4"
            if part_path.exists():
                parts.append(part_path)
            else:
                break
    except Exception as e:
        logger.error(f"FFmpeg split error: {e}")
    return parts

async def split_audio_ffmpeg(input_path: Path, chunk_duration: int = CHUNK_DURATION) -> List[Path]:
    """
    Нарезает аудио на сегменты по chunk_duration секунд.
    Выходной формат MP3.
    """
    parts = []
    base_name = input_path.stem
    output_pattern = str(input_path.parent / f"{base_name}_part%03d.mp3")
    
    cmd = [
        'ffmpeg', '-i', str(input_path),
        '-c', 'copy',
        '-map', '0',
        '-segment_time', str(chunk_duration),
        '-f', 'segment',
        '-reset_timestamps', '1',
        output_pattern
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await process.communicate()
        
        for i in range(1, 100):
            part_path = input_path.parent / f"{base_name}_part{i:03d}.mp3"
            if part_path.exists():
                parts.append(part_path)
            else:
                break
    except Exception as e:
        logger.error(f"FFmpeg split audio error: {e}")
    return parts

# ============ ИЗВЛЕЧЕНИЕ АУДИО ============

async def extract_audio_from_video(video_path: Path, output_mp3: Path) -> bool:
    """Извлекает аудиодорожку из видео в MP3."""
    try:
        cmd = ['ffmpeg', '-i', str(video_path), '-q:a', '0', '-map', 'a', str(output_mp3)]
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await process.communicate()
        return output_mp3.exists() and output_mp3.stat().st_size > 0
    except Exception as e:
        logger.error(f"Audio extraction failed: {e}")
        return False

# ============ TIKTOK ОБРАБОТКА (API + FALLBACK) ============

async def resolve_tiktok_url(session: aiohttp.ClientSession, short_url: str) -> str:
    try:
        async with session.head(short_url, allow_redirects=True, timeout=10) as resp:
            final_url = str(resp.url)
            logger.info(f"Resolved {short_url} -> {final_url}")
            return final_url
    except Exception:
        return short_url

async def get_tiktok_data_api(session: aiohttp.ClientSession, url: str) -> Optional[Dict[str, Any]]:
    api_url = "https://tikwm.com/api/"
    params = {"url": url, "count": 12, "cursor": 0, "web": 1, "hd": 1}
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://www.tiktok.com/'
    }
    try:
        async with session.get(api_url, params=params, headers=headers, timeout=API_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            if data.get('code') != 0:
                return None
            video_data = data.get('data', {})
            author = video_data.get('author', {})
            author_name = author.get('unique_id') or author.get('nickname', 'Неизвестный')
            description = video_data.get('title', '')[:200]
            
            # Музыка
            music_url = None
            music_info = video_data.get('music', {})
            if music_info and music_info.get('play'):
                music_url = music_info['play']
            
            images = video_data.get('images', [])
            if not images and video_data.get('image'):
                images = [video_data['image']]
            
            if images:
                return {
                    'type': 'photo',
                    'images': images,
                    'author': author_name,
                    'description': description,
                    'url': url,
                    'duration': video_data.get('duration'),
                    'music_url': music_url
                }
            
            video_url = video_data.get('play')
            if video_url:
                if video_url.startswith('/'):
                    video_url = f"https://tikwm.com{video_url}"
                return {
                    'type': 'video',
                    'video_url': video_url,
                    'author': author_name,
                    'description': description,
                    'url': url,
                    'duration': video_data.get('duration'),
                    'music_url': music_url
                }
            return None
    except Exception as e:
        logger.error(f"TikTok API error: {e}")
        return None

async def download_tiktok_photos(session: aiohttp.ClientSession, images: List[str], output_dir: Path) -> List[Path]:
    photo_paths = []
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://www.tiktok.com/'
    }
    for i, img_url in enumerate(images[:MAX_MEDIA_GROUP]):
        output_file = output_dir / f"photo_{i+1}.jpg"
        for attempt in range(RETRY_COUNT):
            try:
                async with session.get(img_url, headers=headers, timeout=API_TIMEOUT) as resp:
                    if resp.status == 200:
                        content = await resp.read()
                        with open(output_file, 'wb') as f:
                            f.write(content)
                        photo_paths.append(output_file)
                        break
            except Exception:
                await asyncio.sleep(1)
    return photo_paths

async def download_tiktok_video(session: aiohttp.ClientSession, video_url: str, output_file: Path) -> bool:
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://www.tiktok.com/'
    }
    try:
        async with session.get(video_url, headers=headers, timeout=API_TIMEOUT) as resp:
            if resp.status != 200:
                return False
            with open(output_file, 'wb') as f:
                async for chunk in resp.content.iter_chunked(8192):
                    f.write(chunk)
            return check_file_size(output_file)
    except Exception:
        return False

async def download_audio_from_url(session: aiohttp.ClientSession, audio_url: str, output_file: Path) -> bool:
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    try:
        async with session.get(audio_url, headers=headers, timeout=API_TIMEOUT) as resp:
            if resp.status != 200:
                return False
            with open(output_file, 'wb') as f:
                async for chunk in resp.content.iter_chunked(8192):
                    f.write(chunk)
            return output_file.exists()
    except Exception:
        return False

# ============ YT-DLP ОБРАБОТКА (ДЛЯ ВСЕХ ПЛАТФОРМ, ВКЛЮЧАЯ INSTAGRAM И SOUNDCLOUD) ============

async def get_media_info_ytdlp(url: str) -> Optional[Dict[str, Any]]:
    """Получает информацию через yt-dlp. Для каруселей Instagram возвращает список entries."""
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
        
        # Если это карусель Instagram (поле _type == "playlist" или есть entries)
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
            else:
                return None
        
        # Одиночное медиа
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

async def download_media_ytdlp(url: str, output_file: Path, media_type: str, extract_audio: bool = False) -> bool:
    """Скачивает медиа через yt-dlp. Для аудио отдельный флаг."""
    if not is_ytdlp_available():
        return False
    
    cmd = ['yt-dlp', '-o', str(output_file), '--no-playlist', '--no-warnings']
    if media_type == 'video':
        cmd.extend(['-f', 'best[height<=720][ext=mp4]/best[height<=480][ext=mp4]/best[ext=mp4]'])
    elif media_type == 'image':
        cmd.extend(['-f', 'best'])
    elif media_type == 'audio' or extract_audio:
        # Для SoundCloud и извлечения аудио из видео
        cmd.extend(['-f', 'bestaudio', '--extract-audio', '--audio-format', 'mp3'])
        # yt-dlp сам изменит расширение на .mp3, поэтому переопределим output_file
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
        # Проверяем существование файла (с учётом возможного изменения расширения)
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

async def download_instagram_carousel(entries: List[Dict], tmp_path: Path) -> List[Path]:
    """Скачивает все элементы карусели Instagram (фото/видео)."""
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
        success = await download_media_ytdlp(entry['url'], out_file, media_type)
        if success:
            downloaded.append(out_file)
        else:
            logger.warning(f"Failed to download carousel item {idx+1}")
    return downloaded

# ============ ОТПРАВКА МЕДИА ============

async def send_photo_group(update: Update, photo_paths: List[Path], caption: str):
    try:
        media_group = []
        for i, path in enumerate(photo_paths[:MAX_MEDIA_GROUP]):
            with open(path, 'rb') as f:
                cap = caption if i == 0 else None
                media_group.append(InputMediaPhoto(media=f, caption=cap, parse_mode='HTML'))
        await update.message.reply_media_group(media_group)
    except BadRequest as e:
        logger.error(f"Media group error: {e}, sending individually")
        for path in photo_paths:
            try:
                with open(path, 'rb') as f:
                    await update.message.reply_photo(photo=f, caption=caption, parse_mode='HTML')
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Unexpected error sending media group: {e}")

async def send_with_split(update: Update, file_path: Path, caption: str, platform: str, media_type: str, is_audio_separate: bool = False):
    """
    Отправляет файл. Если размер > MAX_FILE_SIZE и есть ffmpeg, разделяет на части.
    Для видео и аудио разделение по длительности, для фото - предупреждение.
    """
    if media_type == 'photo' or media_type == 'image':
        if check_file_size(file_path, MAX_FILE_SIZE):
            with open(file_path, 'rb') as f:
                await update.message.reply_photo(photo=f, caption=caption, parse_mode='HTML')
        else:
            await update.message.reply_text("⚠️ Файл слишком большой для отправки в Telegram (более 50 МБ).")
        return
    
    if not check_file_size(file_path, MAX_FILE_SIZE):
        if not is_ffmpeg_available():
            await update.message.reply_text("⚠️ Файл превышает 50 МБ, а ffmpeg не установлен. Невозможно разделить. Установите ffmpeg или используйте меньший файл.")
            return
        
        await update.message.reply_text(f"⏳ Файл превышает 50 МБ. Разделяю на части (до {CHUNK_DURATION//60} мин каждая)...")
        
        if media_type == 'video':
            parts = await split_video_ffmpeg(file_path)
        elif media_type == 'audio':
            parts = await split_audio_ffmpeg(file_path)
        else:
            parts = []
        
        if not parts:
            await update.message.reply_text("❌ Не удалось разделить файл.")
            return
        
        for i, part_path in enumerate(parts):
            part_caption = f"{caption}\n📦 Часть {i+1}/{len(parts)}" if i == 0 else f"Часть {i+1}/{len(parts)}"
            with open(part_path, 'rb') as f:
                if media_type == 'video':
                    await update.message.reply_video(video=f, caption=part_caption, parse_mode='HTML', supports_streaming=True)
                else:  # audio
                    await update.message.reply_audio(audio=f, caption=part_caption, parse_mode='HTML')
            part_path.unlink()  # удаляем после отправки
        logger.info(f"Sent {len(parts)} parts for {file_path.name}")
        return
    
    # Файл в пределах лимита – отправляем как есть
    with open(file_path, 'rb') as f:
        if media_type == 'video':
            await update.message.reply_video(video=f, caption=caption, parse_mode='HTML', supports_streaming=True)
        elif media_type == 'audio':
            await update.message.reply_audio(audio=f, caption=caption, parse_mode='HTML')
        elif media_type == 'photo':
            await update.message.reply_photo(photo=f, caption=caption, parse_mode='HTML')
        else:
            await update.message.reply_document(document=f, caption=caption, parse_mode='HTML')

# ============ ОСНОВНОЙ ОБРАБОТЧИК ============

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 <b>Druid Bot</b>\n\n"
        "Отправьте мне ссылку на видео/аудио/фото из:\n"
        "• TikTok (видео/карусель)\n"
        "• YouTube\n"
        "• Instagram (посты, Reels, карусели)\n"
        "• SoundCloud\n"
        "• Twitter/X, Facebook, Vimeo, Reddit и другие\n\n"
        "Бот скачает медиа, при необходимости разделит на части (через ffmpeg) и отправит вам.\n"
        "Для видео и каруселей также будет отправлено отдельное аудио (если доступно).\n\n"
        "⚙️ Ограничения:\n"
        f"• Максимальный размер части: {MAX_FILE_SIZE//(1024*1024)} МБ\n"
        f"• Длительность сегмента: {CHUNK_DURATION//60} мин\n"
        "• Одновременно обрабатывается не более 3 запросов.",
        parse_mode='HTML'
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = None
    user_id = update.effective_user.id
    
    # Защита от множества запросов от одного пользователя
    if user_id not in user_locks:
        user_locks[user_id] = asyncio.Lock()
    async with user_locks[user_id]:
        async with download_semaphore:
            try:
                text = update.message.text.strip()
                url = is_url(text)
                if not url:
                    return
                
                status_msg = await update.message.reply_text("⏳ Обрабатываю ссылку...")
                
                async with aiohttp.ClientSession() as session:
                    # ---------- TikTok ----------
                    if 'tiktok.com' in url:
                        await status_msg.edit_text("🎵 Обрабатываю TikTok...")
                        final_url = await resolve_tiktok_url(session, url)
                        
                        tiktok_data = await get_tiktok_data_api(session, final_url)
                        
                        if not tiktok_data and is_ytdlp_available():
                            await status_msg.edit_text("🔄 API TikTok недоступен, пробую yt-dlp...")
                            media_info = await get_media_info_ytdlp(final_url)
                            if media_info and media_info.get('type'):
                                tiktok_data = {
                                    'type': media_info['type'],
                                    'url': final_url,
                                    'author': media_info.get('author'),
                                    'description': media_info.get('description'),
                                    'duration': media_info.get('duration')
                                }
                                if media_info['type'] == 'video':
                                    tiktok_data['type'] = 'video_fallback'
                        
                        if not tiktok_data:
                            await status_msg.edit_text("❌ Не удалось получить данные из TikTok.")
                            return
                        
                        # Обработка фото/карусель
                        if tiktok_data['type'] == 'photo':
                            images = tiktok_data.get('images', [])
                            if not images:
                                await status_msg.edit_text("❌ Нет изображений.")
                                return
                            await status_msg.edit_text(f"📸 Скачиваю {len(images)} изображений...")
                            with tempfile.TemporaryDirectory() as tmpdir:
                                tmp_path = Path(tmpdir)
                                photos = await download_tiktok_photos(session, images, tmp_path)
                                if photos:
                                    media_type = "карусель" if len(photos) > 1 else "фото"
                                    caption = format_caption(tiktok_data, "TikTok", media_type)
                                    if len(photos) > 1:
                                        await send_photo_group(update, photos, caption)
                                    else:
                                        await send_with_split(update, photos[0], caption, "TikTok", "photo")
                                    
                                    # Отправляем аудио, если есть
                                    if tiktok_data.get('music_url'):
                                        await status_msg.edit_text("🎵 Скачиваю аудиодорожку...")
                                        audio_file = tmp_path / "music.mp3"
                                        if await download_audio_from_url(session, tiktok_data['music_url'], audio_file):
                                            audio_caption = f"🎧 <b>Аудио из TikTok</b>\n👤 {escape_html(tiktok_data['author'][:50])}"
                                            await send_with_split(update, audio_file, audio_caption, "TikTok", "audio")
                                    await status_msg.delete()
                                else:
                                    await status_msg.edit_text("❌ Не удалось скачать фото.")
                            return
                        
                        # Обработка видео через API
                        if tiktok_data['type'] == 'video' and tiktok_data.get('video_url'):
                            await status_msg.edit_text("🎬 Скачиваю видео...")
                            with tempfile.TemporaryDirectory() as tmpdir:
                                tmp_path = Path(tmpdir)
                                video_file = tmp_path / "tiktok_video.mp4"
                                success = await download_tiktok_video(session, tiktok_data['video_url'], video_file)
                                if success:
                                    caption = format_caption(tiktok_data, "TikTok", "видео")
                                    await send_with_split(update, video_file, caption, "TikTok", "video")
                                    # Отправляем аудио
                                    if tiktok_data.get('music_url'):
                                        await status_msg.edit_text("🎵 Скачиваю аудиодорожку...")
                                        audio_file = tmp_path / "music.mp3"
                                        if await download_audio_from_url(session, tiktok_data['music_url'], audio_file):
                                            audio_caption = f"🎧 <b>Аудио из TikTok</b>\n👤 {escape_html(tiktok_data['author'][:50])}"
                                            await send_with_split(update, audio_file, audio_caption, "TikTok", "audio")
                                    await status_msg.delete()
                                else:
                                    await status_msg.edit_text("❌ Не удалось скачать видео.")
                            return
                        
                        # Fallback через yt-dlp
                        if tiktok_data.get('type') == 'video_fallback':
                            await status_msg.edit_text("🎬 Скачиваю видео через yt-dlp...")
                            with tempfile.TemporaryDirectory() as tmpdir:
                                tmp_path = Path(tmpdir)
                                safe_title = sanitize_filename(tiktok_data.get('title', 'tiktok_video'))
                                video_file = tmp_path / f"{safe_title}.mp4"
                                success = await download_media_ytdlp(final_url, video_file, 'video')
                                if success:
                                    caption = format_caption(tiktok_data, "TikTok", "видео")
                                    await send_with_split(update, video_file, caption, "TikTok", "video")
                                    # Пытаемся извлечь аудио из скачанного видео
                                    audio_file = tmp_path / f"{safe_title}_audio.mp3"
                                    if await extract_audio_from_video(video_file, audio_file):
                                        audio_caption = f"🎧 <b>Аудио из видео</b>\n👤 {escape_html(tiktok_data['author'][:50])}"
                                        await send_with_split(update, audio_file, audio_caption, "TikTok", "audio")
                                    await status_msg.delete()
                                else:
                                    await status_msg.edit_text("❌ Не удалось скачать видео.")
                            return
                        
                        await status_msg.edit_text("❌ Неподдерживаемый тип контента TikTok.")
                        return
                    
                    # ---------- Instagram (особая обработка каруселей) ----------
                    if 'instagram.com' in url:
                        await status_msg.edit_text("📸 Обрабатываю Instagram...")
                        media_info = await get_media_info_ytdlp(url)
                        if not media_info:
                            await status_msg.edit_text("❌ Не удалось получить информацию. Возможно, ссылка закрыта или неверна.")
                            return
                        
                        # Карусель
                        if media_info.get('_type') == 'playlist':
                            entries = media_info['entries']
                            await status_msg.edit_text(f"🖼️ Обнаружена карусель из {len(entries)} элементов. Скачиваю...")
                            with tempfile.TemporaryDirectory() as tmpdir:
                                tmp_path = Path(tmpdir)
                                downloaded_files = await download_instagram_carousel(entries, tmp_path)
                                if not downloaded_files:
                                    await status_msg.edit_text("❌ Не удалось скачать элементы карусели.")
                                    return
                                # Отправляем все медиа (фото группой, видео по одному)
                                photos = [f for f in downloaded_files if f.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp')]
                                videos = [f for f in downloaded_files if f.suffix.lower() == '.mp4']
                                
                                if photos:
                                    caption = format_caption({'author': entries[0].get('author'), 'url': url}, "Instagram", "карусель")
                                    if len(photos) > 1:
                                        await send_photo_group(update, photos, caption)
                                    else:
                                        await send_with_split(update, photos[0], caption, "Instagram", "photo")
                                for video_path in videos:
                                    cap = format_caption({'author': entries[0].get('author'), 'url': url}, "Instagram", "видео")
                                    await send_with_split(update, video_path, cap, "Instagram", "video")
                                    # Извлекаем аудио из видео
                                    audio_path = video_path.with_suffix('.mp3')
                                    if await extract_audio_from_video(video_path, audio_path):
                                        audio_caption = f"🎧 <b>Аудио из Instagram</b>\n👤 {escape_html(entries[0].get('author', '')[:50])}"
                                        await send_with_split(update, audio_path, audio_caption, "Instagram", "audio")
                            await status_msg.delete()
                            return
                        else:
                            # Одиночное фото или видео
                            media_type = media_info['type']
                            with tempfile.TemporaryDirectory() as tmpdir:
                                tmp_path = Path(tmpdir)
                                safe_title = sanitize_filename(media_info.get('title', 'instagram_media'))
                                if media_type == 'image':
                                    out_file = tmp_path / f"{safe_title}.jpg"
                                else:
                                    out_file = tmp_path / f"{safe_title}.mp4"
                                success = await download_media_ytdlp(url, out_file, media_type)
                                if not success:
                                    await status_msg.edit_text("❌ Не удалось скачать.")
                                    return
                                caption = format_caption(media_info, "Instagram", media_type)
                                await send_with_split(update, out_file, caption, "Instagram", media_type)
                                # Для видео извлекаем аудио
                                if media_type == 'video':
                                    audio_file = tmp_path / f"{safe_title}_audio.mp3"
                                    if await extract_audio_from_video(out_file, audio_file):
                                        audio_caption = f"🎧 <b>Аудио из Instagram</b>\n👤 {escape_html(media_info.get('author', '')[:50])}"
                                        await send_with_split(update, audio_file, audio_caption, "Instagram", "audio")
                            await status_msg.delete()
                            return
                    
                    # ---------- SoundCloud (особая обработка как аудио) ----------
                    if 'soundcloud.com' in url:
                        await status_msg.edit_text("🎵 Обрабатываю SoundCloud...")
                        media_info = await get_media_info_ytdlp(url)
                        if not media_info or media_info['type'] != 'audio':
                            await status_msg.edit_text("❌ Не удалось получить аудио с SoundCloud.")
                            return
                        with tempfile.TemporaryDirectory() as tmpdir:
                            tmp_path = Path(tmpdir)
                            safe_title = sanitize_filename(media_info['title'])
                            out_file = tmp_path / f"{safe_title}.mp3"
                            success = await download_media_ytdlp(url, out_file, 'audio')
                            if not success:
                                await status_msg.edit_text("❌ Не удалось скачать аудио.")
                                return
                            caption = format_caption(media_info, "SoundCloud", "аудио")
                            await send_with_split(update, out_file, caption, "SoundCloud", "audio")
                            await status_msg.delete()
                        return
                    
                    # ---------- Все остальные платформы (YouTube, Twitter, Vimeo и т.д.) ----------
                    if not is_ytdlp_available():
                        await status_msg.edit_text("❌ yt-dlp не установлен. Установите: pip install yt-dlp")
                        return
                    
                    await status_msg.edit_text("🔍 Получаю информацию...")
                    media_info = await get_media_info_ytdlp(url)
                    if not media_info:
                        await status_msg.edit_text("❌ Не удалось получить информацию. Ссылка не поддерживается.")
                        return
                    
                    with tempfile.TemporaryDirectory() as tmpdir:
                        tmp_path = Path(tmpdir)
                        safe_title = sanitize_filename(media_info['title'])
                        ext = media_info.get('ext', 'mp4')
                        if media_info['type'] == 'image':
                            output_file = tmp_path / f"{safe_title}.{ext}"
                        elif media_info['type'] == 'audio':
                            output_file = tmp_path / f"{safe_title}.mp3"
                        else:
                            output_file = tmp_path / f"{safe_title}.mp4"
                        
                        media_type_rus = {'video': 'видео', 'image': 'фото', 'audio': 'аудио'}.get(media_info['type'], 'медиа')
                        await status_msg.edit_text(f"⏳ Скачиваю {media_type_rus}...")
                        
                        success = await download_media_ytdlp(url, output_file, media_info['type'])
                        if not success:
                            await status_msg.edit_text(f"❌ Не удалось скачать {media_type_rus}")
                            return
                        
                        caption = format_caption(
                            {'author': media_info.get('author'), 'description': media_info.get('description'),
                             'duration': media_info.get('duration'), 'url': url},
                            get_platform_name(url), media_type_rus
                        )
                        
                        await send_with_split(update, output_file, caption, get_platform_name(url), media_info['type'])
                        
                        # Для видео дополнительно извлекаем и отправляем аудио
                        if media_info['type'] == 'video':
                            audio_file = tmp_path / f"{safe_title}_audio.mp3"
                            if await extract_audio_from_video(output_file, audio_file):
                                audio_caption = f"🎧 <b>Аудио из видео</b>\n👤 {escape_html(media_info.get('author', '')[:50])}"
                                await send_with_split(update, audio_file, audio_caption, get_platform_name(url), "audio")
                        
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

# ============ ОЧИСТКА ФАЙЛОВ ============

async def cleanup_old_files():
    while True:
        try:
            if DOWNLOADS_DIR.exists():
                now = time.time()
                for f in DOWNLOADS_DIR.iterdir():
                    if f.is_file() and now - f.stat().st_mtime > CLEANUP_INTERVAL:
                        f.unlink()
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
        await asyncio.sleep(CLEANUP_INTERVAL)

# ============ ЗАПУСК ============

def main():
    if not is_ytdlp_available():
        print("\n" + "="*50)
        print("⚠️  ВНИМАНИЕ: yt-dlp не установлен!")
        print("Установите: pip install yt-dlp")
        print("="*50 + "\n")
    else:
        ffmpeg_status = "✅ установлен" if is_ffmpeg_available() else "❌ не установлен (разделение файлов недоступно)"
        print("\n" + "="*50)
        print("🤖 Бот Druid запущен (расширенная версия)")
        print(f"✅ Токен: {TOKEN[:10]}...{TOKEN[-5:]}")
        print(f"✅ yt-dlp: установлен")
        print(f"✅ ffmpeg: {ffmpeg_status}")
        print(f"✅ TikTok: API + fallback на yt-dlp")
        print(f"✅ Instagram: поддержка каруселей")
        print(f"✅ SoundCloud: аудио в MP3")
        print(f"✅ Разделение файлов > {MAX_FILE_SIZE//(1024*1024)} МБ через ffmpeg")
        print(f"✅ Одновременных загрузок: {MAX_CONCURRENT_DOWNLOADS}")
        print("="*50 + "\n")
    
    request = HTTPXRequest(connect_timeout=30, read_timeout=120, write_timeout=120, pool_timeout=30)
    application = Application.builder().token(TOKEN).request(request).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(cleanup_old_files())
    try:
        application.run_polling(drop_pending_updates=True)
    except Exception as e:
        print(f"❌ Ошибка: {e}")
    finally:
        loop.close()

if __name__ == '__main__':
    main()
