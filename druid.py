#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram бот Druid - новая архитектура
- TikTok: отдельная обработка через API (фото и видео)
- Остальные платформы: yt-dlp
"""

import os
import logging
import subprocess
import re
import json
import time
import requests
import tempfile
import asyncio
from typing import Optional, List, Dict, Any, Tuple
from urllib.parse import urlparse, unquote
from pathlib import Path

from telegram import Update, InputMediaPhoto
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from telegram.request import HTTPXRequest
from telegram.error import BadRequest

# ============ НАСТРОЙКИ ============
TOKEN = "8783056247:AAHGJF9vtDwuoCQBwfhdYOqQgFRsgfGAAp4"
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
MAX_MEDIA_GROUP = 10  # Максимум фото в карусели
CLEANUP_INTERVAL = 7200  # Очистка старых файлов каждые 2 часа
YTDLP_TIMEOUT = 600  # Таймаут для yt-dlp
API_TIMEOUT = 30  # Таймаут для API запросов
# ===================================

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Создаем директорию для загрузок
DOWNLOADS_DIR = Path("downloads")
DOWNLOADS_DIR.mkdir(exist_ok=True)

# ============ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ============

def is_ytdlp_available() -> bool:
    """Проверяет доступность yt-dlp"""
    try:
        result = subprocess.run(
            ['yt-dlp', '--version'], 
            capture_output=True, 
            check=True, 
            timeout=5
        )
        logger.info(f"yt-dlp version: {result.stdout.decode().strip()}")
        return True
    except Exception as e:
        logger.warning(f"yt-dlp not available: {e}")
        return False

def sanitize_filename(title: str) -> str:
    """Очищает название файла"""
    title = re.sub(r'[\\/*?:"<>|]', "", title)
    if len(title) > 80:
        title = title[:80]
    title = title.replace(' ', '_')
    title = title.strip('.')
    return title if title else "media"

def is_url(text: str) -> bool:
    """Проверка, является ли текст ссылкой"""
    url_pattern = re.compile(
        r'^https?://'
        r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|'
        r'localhost|'
        r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'
        r'(?::\d+)?'
        r'(?:/?|[/?]\S+)$', re.IGNORECASE)
    return url_pattern.match(text.strip()) is not None

def get_platform_name(url: str) -> str:
    """Определяет платформу по URL"""
    domain = urlparse(url).netloc.lower()
    
    platforms = {
        'youtube.com': 'YouTube',
        'youtu.be': 'YouTube',
        'tiktok.com': 'TikTok',
        'instagram.com': 'Instagram',
        'twitter.com': 'Twitter/X',
        'x.com': 'Twitter/X',
        'facebook.com': 'Facebook',
        'vimeo.com': 'Vimeo',
        'reddit.com': 'Reddit',
        'pinterest.com': 'Pinterest'
    }
    
    for key, value in platforms.items():
        if key in domain:
            return value
    
    return domain.replace('www.', '').split('.')[0].capitalize()

def escape_html(text: str) -> str:
    """Экранирует HTML символы"""
    if not text:
        return text
    return (text.replace('&', '&amp;')
                .replace('<', '&lt;')
                .replace('>', '&gt;')
                .replace('"', '&quot;'))

def format_caption(info: Dict[str, Any], platform: str, media_type: str) -> str:
    """Формирует подпись для медиафайла"""
    caption_parts = []
    
    emojis = {
        'фото': '📸',
        'видео': '🎬',
        'карусель': '🖼️',
        'аудио': '🎵'
    }
    
    caption_parts.append(f"{emojis.get(media_type, '📎')} ")
    caption_parts.append(f"<b>{escape_html(platform)}</b>\n")
    
    if info.get('author'):
        author = escape_html(info['author'][:50])
        caption_parts.append(f"👤 <b>Автор:</b> {author}\n")
    
    if info.get('description'):
        desc = escape_html(info['description'][:200])
        if len(info.get('description', '')) > 200:
            desc += "..."
        caption_parts.append(f"📝 <b>Описание:</b> {desc}\n")
    
    if info.get('duration'):
        duration = info['duration']
        minutes = duration // 60
        seconds = duration % 60
        caption_parts.append(f"⏱️ <b>Длительность:</b> {minutes}:{seconds:02d}\n")
    
    if info.get('url'):
        caption_parts.append(f"🔗 <b>Источник:</b> <a href='{info['url']}'>ссылка</a>")
    
    return "".join(caption_parts)

def check_file_size(file_path: Path, max_size: int = MAX_FILE_SIZE) -> bool:
    """Проверяет размер файла"""
    try:
        return file_path.exists() and file_path.stat().st_size <= max_size
    except Exception:
        return False

# ============ TIKTOK ОБРАБОТКА (ТОЛЬКО API) ============

async def resolve_tiktok_url(short_url: str) -> str:
    """Получает конечный URL из сокращённой ссылки vm.tiktok.com"""
    try:
        response = requests.head(short_url, allow_redirects=True, timeout=10)
        final_url = response.url
        logger.info(f"Resolved TikTok URL: {short_url} -> {final_url}")
        return final_url
    except Exception as e:
        logger.error(f"Error resolving TikTok URL: {e}")
        return short_url

def get_tiktok_data(url: str) -> Optional[Dict[str, Any]]:
    """
    Универсальная функция для получения данных из TikTok
    Возвращает информацию как для фото, так и для видео
    """
    try:
        # Используем tikwm.com API
        api_url = "https://tikwm.com/api/"
        params = {"url": url, "count": 12, "cursor": 0, "web": 1, "hd": 1}
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://www.tiktok.com/'
        }
        
        response = requests.get(api_url, params=params, headers=headers, timeout=API_TIMEOUT)
        
        if response.status_code != 200:
            logger.error(f"TikTok API error: status {response.status_code}")
            return None
            
        data = response.json()
        
        if data.get('code') != 0:
            logger.error(f"TikTok API error: {data.get('msg')}")
            return None
            
        video_data = data.get('data', {})
        
        # Получаем информацию об авторе
        author = video_data.get('author', {})
        author_name = author.get('unique_id') or author.get('nickname', 'Неизвестный')
        description = video_data.get('title', '')[:200]
        
        # Проверяем наличие фото (карусель)
        images = video_data.get('images', [])
        if not images and video_data.get('image'):
            images = [video_data['image']]
        
        if images:
            # Это фото/карусель
            logger.info(f"Detected TikTok photo/carousel with {len(images)} images")
            return {
                'type': 'photo',
                'images': images,
                'author': author_name,
                'description': description,
                'url': url,
                'duration': video_data.get('duration')
            }
        
        # Проверяем наличие видео
        video_url = video_data.get('play')
        if video_url:
            logger.info("Detected TikTok video")
            return {
                'type': 'video',
                'video_url': video_url,
                'author': author_name,
                'description': description,
                'url': url,
                'duration': video_data.get('duration')
            }
        
        logger.warning("No images or video found in TikTok data")
        return None
        
    except Exception as e:
        logger.error(f"TikTok API error: {e}")
        return None

async def download_tiktok_photos(images: List[str], output_dir: Path) -> List[Path]:
    """Скачивает фото TikTok"""
    photo_paths = []
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://www.tiktok.com/'
    }
    
    for i, img_url in enumerate(images[:MAX_MEDIA_GROUP]):
        output_file = output_dir / f"photo_{i+1}.jpg"
        
        try:
            if i > 0:
                await asyncio.sleep(0.3)
            
            response = requests.get(img_url, headers=headers, timeout=API_TIMEOUT)
            
            if response.status_code == 200:
                with open(output_file, 'wb') as f:
                    f.write(response.content)
                photo_paths.append(output_file)
                logger.info(f"Downloaded photo {i+1}/{len(images[:MAX_MEDIA_GROUP])}")
            else:
                logger.warning(f"Failed to download {img_url}: status {response.status_code}")
                
        except Exception as e:
            logger.error(f"Download photo error: {e}")
    
    return photo_paths

async def download_tiktok_video(video_url: str, output_file: Path) -> bool:
    """Скачивает видео TikTok"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://www.tiktok.com/'
        }
        
        response = requests.get(video_url, headers=headers, timeout=API_TIMEOUT, stream=True)
        
        if response.status_code != 200:
            logger.error(f"Failed to download video: status {response.status_code}")
            return False
        
        with open(output_file, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        
        return check_file_size(output_file)
        
    except Exception as e:
        logger.error(f"Download video error: {e}")
        return False

# ============ YT-DLP ОБРАБОТКА (ДЛЯ ДРУГИХ ПЛАТФОРМ) ============

async def get_media_info_ytdlp(url: str) -> Optional[Dict[str, Any]]:
    """Получает информацию о медиа через yt-dlp"""
    try:
        cmd = [
            'yt-dlp',
            '--dump-json',
            '--no-playlist',
            '--no-warnings',
            '--quiet'
        ]
        
        process = await asyncio.create_subprocess_exec(
            *cmd, url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            logger.error(f"yt-dlp info timeout for {url}")
            return None
        
        if process.returncode != 0:
            stderr_text = stderr.decode()
            logger.error(f"yt-dlp info error: {stderr_text}")
            return None
            
        info = json.loads(stdout.decode())
        
        ext = info.get('ext', '')
        is_video = ext in ['mp4', 'webm', 'mkv', 'avi', 'mov']
        is_audio = info.get('acodec') != 'none' and info.get('vcodec') == 'none'
        is_image = ext in ['jpg', 'jpeg', 'png', 'webp', 'gif']
        
        media_type = 'video'
        if is_audio:
            media_type = 'audio'
        elif is_image:
            media_type = 'image'
        
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
    """Скачивает медиа через yt-dlp"""
    if not is_ytdlp_available():
        return False
    
    try:
        cmd = ['yt-dlp', '-o', str(output_file), '--no-playlist', '--no-warnings']
        
        if media_type == 'video':
            cmd.extend(['-f', 'best[height<=720][ext=mp4]/best[height<=480][ext=mp4]/best[ext=mp4]'])
        elif media_type == 'image':
            cmd.extend(['-f', 'best[ext=jpg]/best[ext=png]/best[ext=webp]'])
        elif media_type == 'audio':
            cmd.extend(['-f', 'bestaudio', '--extract-audio', '--audio-format', 'mp3'])
        
        cmd.append(url)
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=YTDLP_TIMEOUT)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            logger.error(f"Download timeout for {url}")
            return False
        
        if process.returncode != 0:
            logger.error(f"yt-dlp download error: {stderr.decode()}")
            return False
        
        if not output_file.exists():
            possible_files = list(output_file.parent.glob(f"{output_file.stem}.*"))
            if possible_files:
                output_file = possible_files[0]
            else:
                return False
        
        return check_file_size(output_file)
        
    except Exception as e:
        logger.error(f"Download error: {e}")
        return False

# ============ ОТПРАВКА МЕДИА ============

async def send_photo_group(update: Update, photo_paths: List[Path], caption: str):
    """Отправляет группу фотографий"""
    try:
        media_group = []
        
        for i, photo_path in enumerate(photo_paths[:MAX_MEDIA_GROUP]):
            with open(photo_path, 'rb') as f:
                cap = caption if i == 0 else None
                media_group.append(InputMediaPhoto(media=f, caption=cap, parse_mode='HTML'))
        
        await update.message.reply_media_group(media_group)
        logger.info(f"Sent {len(media_group)} photos")
        
    except BadRequest as e:
        logger.error(f"BadRequest: {e}")
        for photo_path in photo_paths:
            try:
                with open(photo_path, 'rb') as f:
                    await update.message.reply_photo(photo=f, caption=caption, parse_mode='HTML')
                break
            except Exception as send_error:
                logger.error(f"Single photo error: {send_error}")
    except Exception as e:
        logger.error(f"Media group error: {e}")
        raise

# ============ ОСНОВНОЙ ОБРАБОТЧИК ============

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Главный обработчик сообщений"""
    status_msg = None
    
    try:
        text = update.message.text.strip()
        
        if not is_url(text):
            return
        
        status_msg = await update.message.reply_text("⏳ Обрабатываю ссылку...")
        
        # ========== 1. TIKTOK (ТОЛЬКО API, БЕЗ YT-DLP) ==========
        if 'tiktok.com' in text:
            await status_msg.edit_text("🎵 Обрабатываю TikTok...")
            
            # Разрешаем сокращённую ссылку
            final_url = await resolve_tiktok_url(text)
            
            # Получаем данные через API
            tiktok_data = get_tiktok_data(final_url)
            
            if not tiktok_data:
                await status_msg.edit_text("❌ Не удалось получить данные из TikTok. Проверьте ссылку.")
                return
            
            # Обработка фото/карусели
            if tiktok_data['type'] == 'photo':
                images = tiktok_data.get('images', [])
                if not images:
                    await status_msg.edit_text("❌ Не найдены изображения в посте.")
                    return
                
                await status_msg.edit_text(f"📸 Найдено {len(images)} изображений, скачиваю...")
                
                with tempfile.TemporaryDirectory() as tmpdir:
                    tmp_path = Path(tmpdir)
                    photos = await download_tiktok_photos(images, tmp_path)
                    
                    if photos:
                        media_type = "карусель" if len(photos) > 1 else "фото"
                        caption = format_caption(tiktok_data, "TikTok", media_type)
                        
                        if len(photos) > 1:
                            await send_photo_group(update, photos, caption)
                        else:
                            with open(photos[0], 'rb') as f:
                                await update.message.reply_photo(photo=f, caption=caption, parse_mode='HTML')
                        
                        await status_msg.delete()
                    else:
                        await status_msg.edit_text("❌ Не удалось скачать изображения.")
                
                return
            
            # Обработка видео
            elif tiktok_data['type'] == 'video':
                video_url = tiktok_data.get('video_url')
                if not video_url:
                    await status_msg.edit_text("❌ Не найдена ссылка на видео.")
                    return
                
                await status_msg.edit_text("🎬 Скачиваю видео...")
                
                with tempfile.TemporaryDirectory() as tmpdir:
                    tmp_path = Path(tmpdir)
                    output_file = tmp_path / "tiktok_video.mp4"
                    
                    success = await download_tiktok_video(video_url, output_file)
                    
                    if success:
                        caption = format_caption(tiktok_data, "TikTok", "видео")
                        
                        with open(output_file, 'rb') as f:
                            await update.message.reply_video(
                                video=f,
                                caption=caption,
                                parse_mode='HTML',
                                supports_streaming=True
                            )
                        
                        await status_msg.delete()
                    else:
                        await status_msg.edit_text("❌ Не удалось скачать видео.")
                
                return
        
        # ========== 2. ВСЕ ОСТАЛЬНЫЕ ПЛАТФОРМЫ (YT-DLP) ==========
        
        # Проверяем yt-dlp
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
            
            media_type_rus = {
                'video': 'видео',
                'image': 'фото',
                'audio': 'аудио'
            }.get(media_info['type'], 'медиа')
            
            await status_msg.edit_text(f"⏳ Скачиваю {media_type_rus}...")
            
            success = await download_media_ytdlp(text, output_file, media_info['type'])
            
            if not success:
                await status_msg.edit_text(f"❌ Не удалось скачать {media_type_rus}")
                return
            
            if not output_file.exists():
                possible = list(tmp_path.glob(f"{safe_title}.*"))
                if possible:
                    output_file = possible[0]
                else:
                    await status_msg.edit_text("❌ Файл не найден")
                    return
            
            caption = format_caption(
                {
                    'author': media_info.get('author'),
                    'description': media_info.get('description'),
                    'duration': media_info.get('duration'),
                    'url': text
                },
                get_platform_name(text),
                media_type_rus
            )
            
            try:
                with open(output_file, 'rb') as f:
                    if media_info['type'] == 'image':
                        await update.message.reply_photo(photo=f, caption=caption, parse_mode='HTML')
                    elif media_info['type'] == 'audio':
                        await update.message.reply_audio(
                            audio=f, caption=caption, parse_mode='HTML',
                            title=safe_title, performer=media_info.get('author', 'Неизвестный')
                        )
                    else:
                        await update.message.reply_video(video=f, caption=caption, parse_mode='HTML', supports_streaming=True)
                
                logger.info(f"Successfully sent {media_info['type']}")
                
            except Exception as e:
                logger.error(f"Send error: {e}")
                await status_msg.edit_text(f"⚠️ Ошибка отправки: {str(e)[:100]}")
            
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
    """Периодическая очистка старых файлов"""
    while True:
        try:
            if DOWNLOADS_DIR.exists():
                current_time = time.time()
                deleted = 0
                for file_path in DOWNLOADS_DIR.iterdir():
                    if file_path.is_file() and current_time - file_path.stat().st_mtime > CLEANUP_INTERVAL:
                        file_path.unlink()
                        deleted += 1
                if deleted:
                    logger.info(f"Cleaned up {deleted} files")
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
        
        await asyncio.sleep(CLEANUP_INTERVAL)

# ============ ЗАПУСК ============

def main():
    """Запуск бота"""
    
    if not is_ytdlp_available():
        print("\n" + "="*50)
        print("⚠️  ВНИМАНИЕ: yt-dlp не установлен!")
        print("="*50)
        print("Для YouTube, Instagram и других платформ нужен yt-dlp:")
        print("pip install yt-dlp")
        print("="*50 + "\n")
    else:
        print("\n" + "="*50)
        print("🤖 Бот Druid запущен (новая архитектура)")
        print("="*50)
        print(f"✅ Токен: {TOKEN[:10]}...{TOKEN[-5:]}")
        print(f"✅ yt-dlp: установлен (для YouTube, Instagram и др.)")
        print(f"✅ TikTok: через API (фото и видео)")
        print(f"✅ Макс. размер: {MAX_FILE_SIZE // (1024*1024)} MB")
        print("="*50 + "\n")
    
    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=120.0,
        write_timeout=120.0,
        pool_timeout=30.0
    )
    
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
