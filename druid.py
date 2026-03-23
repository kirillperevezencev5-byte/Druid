#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram бот Druid (исправленная версия)
- TikTok: API + fallback на yt-dlp
- YouTube и другие: yt-dlp
- Разделение больших видео на части (без ffmpeg)
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
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from telegram.request import HTTPXRequest
from telegram.error import BadRequest

# ============ НАСТРОЙКИ ============
TOKEN = "8783056247:AAHGJF9vtDwuoCQBwfhdYOqQgFRsgfGAAp4"
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
MAX_MEDIA_GROUP = 10
CLEANUP_INTERVAL = 7200
YTDLP_TIMEOUT = 600
API_TIMEOUT = 30
RETRY_COUNT = 3
# ===================================

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

DOWNLOADS_DIR = Path("downloads")
DOWNLOADS_DIR.mkdir(exist_ok=True)

# ============ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ============

def is_ytdlp_available() -> bool:
    try:
        subprocess.run(['yt-dlp', '--version'], capture_output=True, check=True, timeout=5)
        return True
    except Exception:
        return False

def sanitize_filename(title: str) -> str:
    title = re.sub(r'[\\/*?:"<>|]', "", title)
    if len(title) > 80:
        title = title[:80]
    title = title.replace(' ', '_').strip('.')
    return title if title else "media"

def is_url(text: str) -> bool:
    pattern = re.compile(
        r'^https?://'
        r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|'
        r'localhost|\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'
        r'(?::\d+)?(?:/?|[/?]\S+)$', re.IGNORECASE)
    return pattern.match(text.strip()) is not None

def get_platform_name(url: str) -> str:
    domain = urlparse(url).netloc.lower()
    platforms = {
        'youtube.com': 'YouTube', 'youtu.be': 'YouTube',
        'tiktok.com': 'TikTok', 'instagram.com': 'Instagram',
        'twitter.com': 'Twitter/X', 'x.com': 'Twitter/X',
        'facebook.com': 'Facebook', 'vimeo.com': 'Vimeo',
        'reddit.com': 'Reddit', 'pinterest.com': 'Pinterest'
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
    return "".join(parts)

def check_file_size(file_path: Path, max_size: int = MAX_FILE_SIZE) -> bool:
    try:
        return file_path.exists() and file_path.stat().st_size <= max_size
    except Exception:
        return False

async def split_file(file_path: Path, chunk_size: int = MAX_FILE_SIZE) -> List[Path]:
    """Разделяет файл на части по chunk_size байт, возвращает список путей к частям."""
    parts = []
    file_size = file_path.stat().st_size
    if file_size <= chunk_size:
        return [file_path]
    
    base_name = file_path.stem
    ext = file_path.suffix
    dir_path = file_path.parent
    
    with open(file_path, 'rb') as f:
        part_num = 1
        while True:
            chunk_data = f.read(chunk_size)
            if not chunk_data:
                break
            part_name = f"{base_name}_part{part_num:03d}{ext}"
            part_path = dir_path / part_name
            with open(part_path, 'wb') as part_file:
                part_file.write(chunk_data)
            parts.append(part_path)
            part_num += 1
    return parts

# ============ TIKTOK ОБРАБОТКА (API + FALLBACK) ============

async def resolve_tiktok_url(session: aiohttp.ClientSession, short_url: str) -> str:
    """Асинхронно получает конечный URL из сокращённой ссылки."""
    try:
        async with session.head(short_url, allow_redirects=True, timeout=10) as resp:
            final_url = str(resp.url)
            logger.info(f"Resolved {short_url} -> {final_url}")
            return final_url
    except Exception as e:
        logger.error(f"Resolve error: {e}")
        return short_url

async def get_tiktok_data_api(session: aiohttp.ClientSession, url: str) -> Optional[Dict[str, Any]]:
    """Получает данные из TikTok через tikwm.com API (асинхронно)."""
    api_url = "https://tikwm.com/api/"
    params = {"url": url, "count": 12, "cursor": 0, "web": 1, "hd": 1}
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://www.tiktok.com/'
    }
    try:
        async with session.get(api_url, params=params, headers=headers, timeout=API_TIMEOUT) as resp:
            if resp.status != 200:
                logger.error(f"TikTok API status {resp.status}")
                return None
            data = await resp.json()
            if data.get('code') != 0:
                logger.error(f"TikTok API error: {data.get('msg')}")
                return None
            video_data = data.get('data', {})
            author = video_data.get('author', {})
            author_name = author.get('unique_id') or author.get('nickname', 'Неизвестный')
            description = video_data.get('title', '')[:200]
            
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
                    'duration': video_data.get('duration')
                }
            
            video_url = video_data.get('play')
            if video_url:
                return {
                    'type': 'video',
                    'video_url': video_url,
                    'author': author_name,
                    'description': description,
                    'url': url,
                    'duration': video_data.get('duration')
                }
            return None
    except Exception as e:
        logger.error(f"TikTok API request error: {e}")
        return None

async def download_tiktok_photos(session: aiohttp.ClientSession, images: List[str], output_dir: Path) -> List[Path]:
    """Скачивает фотографии TikTok."""
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
                        logger.info(f"Downloaded photo {i+1}/{len(images)}")
                        break
                    else:
                        logger.warning(f"Failed to download {img_url}, status {resp.status}, attempt {attempt+1}")
            except Exception as e:
                logger.error(f"Download photo error (attempt {attempt+1}): {e}")
            await asyncio.sleep(1)
    return photo_paths

async def download_tiktok_video(session: aiohttp.ClientSession, video_url: str, output_file: Path) -> bool:
    """Скачивает видео TikTok."""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://www.tiktok.com/'
    }
    try:
        async with session.get(video_url, headers=headers, timeout=API_TIMEOUT) as resp:
            if resp.status != 200:
                logger.error(f"Video download failed: status {resp.status}")
                return False
            with open(output_file, 'wb') as f:
                async for chunk in resp.content.iter_chunked(8192):
                    f.write(chunk)
            return check_file_size(output_file)
    except Exception as e:
        logger.error(f"Video download error: {e}")
        return False

# ============ YT-DLP ОБРАБОТКА (ДЛЯ ДРУГИХ ПЛАТФОРМ И FALLBACK) ============

async def get_media_info_ytdlp(url: str) -> Optional[Dict[str, Any]]:
    """Получает информацию через yt-dlp."""
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
            'url': url
        }
    except Exception as e:
        logger.error(f"get_media_info error: {e}")
        return None

async def download_media_ytdlp(url: str, output_file: Path, media_type: str) -> bool:
    """Скачивает медиа через yt-dlp."""
    if not is_ytdlp_available():
        return False
    cmd = ['yt-dlp', '-o', str(output_file), '--no-playlist', '--no-warnings']
    if media_type == 'video':
        cmd.extend(['-f', 'best[height<=720][ext=mp4]/best[height<=480][ext=mp4]/best[ext=mp4]'])
    elif media_type == 'image':
        cmd.extend(['-f', 'best'])
    elif media_type == 'audio':
        cmd.extend(['-f', 'bestaudio', '--extract-audio', '--audio-format', 'mp3'])
    cmd.append(url)
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=YTDLP_TIMEOUT)
        if process.returncode != 0:
            logger.error(f"yt-dlp download error: {stderr.decode()}")
            return False
        # Проверяем, создался ли файл (yt-dlp может изменить расширение)
        if not output_file.exists():
            possible = list(output_file.parent.glob(f"{output_file.stem}.*"))
            if possible:
                output_file = possible[0]
            else:
                return False
        return check_file_size(output_file)
    except Exception as e:
        logger.error(f"Download error: {e}")
        return False

# ============ ОТПРАВКА МЕДИА ============

async def send_photo_group(update: Update, photo_paths: List[Path], caption: str):
    """Отправляет группу фото, при ошибке отправляет по одному."""
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
            except Exception as send_err:
                logger.error(f"Single photo send error: {send_err}")
    except Exception as e:
        logger.error(f"Unexpected error sending media group: {e}")

async def send_with_split(update: Update, file_path: Path, caption: str, platform: str, media_type: str):
    """Отправляет файл, если он больше лимита — разбивает и отправляет частями."""
    if check_file_size(file_path, MAX_FILE_SIZE):
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

    # Файл слишком большой, разбиваем
    await update.message.reply_text(f"⚠️ Файл превышает {MAX_FILE_SIZE//(1024*1024)} МБ. Разделяю на части...")
    parts = await split_file(file_path, MAX_FILE_SIZE)
    for i, part_path in enumerate(parts):
        part_caption = f"{caption}\n📦 Часть {i+1}/{len(parts)}" if i == 0 else f"Часть {i+1}/{len(parts)}"
        with open(part_path, 'rb') as f:
            await update.message.reply_document(document=f, caption=part_caption, parse_mode='HTML')
        part_path.unlink()  # удаляем временную часть после отправки
    logger.info(f"Sent {len(parts)} parts for {file_path.name}")

# ============ ОСНОВНОЙ ОБРАБОТЧИК ============

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_msg = None
    try:
        text = update.message.text.strip()
        if not is_url(text):
            return

        status_msg = await update.message.reply_text("⏳ Обрабатываю ссылку...")

        async with aiohttp.ClientSession() as session:
            # ---------- TikTok ----------
            if 'tiktok.com' in text:
                await status_msg.edit_text("🎵 Обрабатываю TikTok...")
                final_url = await resolve_tiktok_url(session, text)

                # Пытаемся через API
                tiktok_data = await get_tiktok_data_api(session, final_url)

                # Fallback: если API не дал данных, пробуем yt-dlp
                if not tiktok_data and is_ytdlp_available():
                    await status_msg.edit_text("🔄 API TikTok недоступен, пробую yt-dlp...")
                    media_info = await get_media_info_ytdlp(final_url)
                    if media_info:
                        tiktok_data = {
                            'type': media_info['type'],
                            'url': final_url,
                            'author': media_info.get('author'),
                            'description': media_info.get('description'),
                            'duration': media_info.get('duration')
                        }
                        # Для видео используем yt-dlp
                        if media_info['type'] == 'video':
                            tiktok_data['type'] = 'video_fallback'

                if not tiktok_data:
                    await status_msg.edit_text("❌ Не удалось получить данные из TikTok.")
                    return

                # --- Обработка фото/карусели ---
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
                            await status_msg.delete()
                        else:
                            await status_msg.edit_text("❌ Не удалось скачать фото.")
                    return

                # --- Обработка видео через API ---
                if tiktok_data['type'] == 'video' and tiktok_data.get('video_url'):
                    video_url = tiktok_data['video_url']
                    await status_msg.edit_text("🎬 Скачиваю видео...")
                    with tempfile.TemporaryDirectory() as tmpdir:
                        tmp_path = Path(tmpdir)
                        output_file = tmp_path / "tiktok_video.mp4"
                        success = await download_tiktok_video(session, video_url, output_file)
                        if success:
                            caption = format_caption(tiktok_data, "TikTok", "видео")
                            await send_with_split(update, output_file, caption, "TikTok", "video")
                            await status_msg.delete()
                        else:
                            await status_msg.edit_text("❌ Не удалось скачать видео.")
                    return

                # --- Fallback через yt-dlp для видео TikTok ---
                if tiktok_data.get('type') == 'video_fallback':
                    await status_msg.edit_text("🎬 Скачиваю видео через yt-dlp...")
                    with tempfile.TemporaryDirectory() as tmpdir:
                        tmp_path = Path(tmpdir)
                        safe_title = sanitize_filename(tiktok_data.get('title', 'tiktok_video'))
                        output_file = tmp_path / f"{safe_title}.mp4"
                        success = await download_media_ytdlp(final_url, output_file, 'video')
                        if success:
                            caption = format_caption(tiktok_data, "TikTok", "видео")
                            await send_with_split(update, output_file, caption, "TikTok", "video")
                            await status_msg.delete()
                        else:
                            await status_msg.edit_text("❌ Не удалось скачать видео.")
                    return

                await status_msg.edit_text("❌ Неподдерживаемый тип контента TikTok.")
                return

            # ---------- Все остальные платформы (YouTube, Instagram и др.) ----------
            if not is_ytdlp_available():
                await status_msg.edit_text("❌ yt-dlp не установлен. Установите: pip install yt-dlp")
                return

            await status_msg.edit_text("🔍 Получаю информацию...")
            media_info = await get_media_info_ytdlp(text)
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

                success = await download_media_ytdlp(text, output_file, media_info['type'])
                if not success:
                    await status_msg.edit_text(f"❌ Не удалось скачать {media_type_rus}")
                    return

                caption = format_caption(
                    {'author': media_info.get('author'), 'description': media_info.get('description'),
                     'duration': media_info.get('duration'), 'url': text},
                    get_platform_name(text), media_type_rus
                )

                await send_with_split(update, output_file, caption, get_platform_name(text), media_info['type'])
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
        print("\n" + "="*50)
        print("🤖 Бот Druid запущен (исправленная версия)")
        print(f"✅ Токен: {TOKEN[:10]}...{TOKEN[-5:]}")
        print(f"✅ yt-dlp: установлен")
        print(f"✅ TikTok: API + fallback на yt-dlp")
        print(f"✅ Разделение файлов > {MAX_FILE_SIZE//(1024*1024)} МБ")
        print("="*50 + "\n")

    request = HTTPXRequest(connect_timeout=30, read_timeout=120, write_timeout=120, pool_timeout=30)
    application = Application.builder().token(TOKEN).request(request).build()
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
