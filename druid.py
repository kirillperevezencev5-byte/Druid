#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram бот Druid - улучшенная версия с полной поддержкой TikTok фото
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
from urllib.parse import urlparse, quote
from pathlib import Path

from telegram import Update, InputMediaPhoto
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from telegram.request import HTTPXRequest
from telegram.error import BadRequest

# ============ НАСТРОЙКИ ============
TOKEN = "8783056247:AAHGJF9vtDwuoCQBwfhdYOqQgFRsgfGAAp4"
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
MAX_MEDIA_GROUP = 10  # Максимум фото в карусели
CLEANUP_INTERVAL = 7200  # Очистка старых файлов каждые 2 часа (в секундах)
YTDLP_TIMEOUT = 600  # Таймаут для yt-dlp в секундах
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
    """Очищает название файла от недопустимых символов"""
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
    """Экранирует HTML символы для безопасной вставки"""
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
        'аудио': '🎵',
        'gif': '🎪'
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

# ============ УЛУЧШЕННАЯ TIKTOK ОБРАБОТКА ============

def clean_tiktok_url(url: str) -> str:
    """Очищает URL TikTok от лишних параметров"""
    # Убираем параметры отслеживания
    if '?' in url:
        base_url = url.split('?')[0]
        # Проверяем, что это ссылка на фото/видео
        if '/photo/' in base_url or '/video/' in base_url:
            return base_url
    return url

def get_tiktok_info_alternative(url: str) -> Optional[Dict[str, Any]]:
    """Альтернативный метод получения TikTok фото через другой API"""
    try:
        # Пробуем через API ssstik
        api_url = "https://www.ssstik.io/api"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'Referer': 'https://www.ssstik.io/'
        }
        
        data = {'url': url, 'lang': 'en'}
        response = requests.post(api_url, data=data, headers=headers, timeout=API_TIMEOUT)
        
        if response.status_code == 200:
            result = response.json()
            if result.get('status') == 'ok':
                images = []
                # Проверяем наличие фото
                if 'images' in result:
                    images = result['images']
                elif 'image' in result:
                    images = [result['image']]
                
                if images:
                    return {
                        'type': 'photo',
                        'images': images,
                        'author': result.get('author', {}).get('name', 'Неизвестный'),
                        'description': result.get('title', '')[:200],
                        'url': url
                    }
        
        return None
    except Exception as e:
        logger.error(f"Alternative TikTok API error: {e}")
        return None

def get_tiktok_info(url: str) -> Optional[Dict[str, Any]]:
    """Получает информацию о TikTok (фото/карусель) через несколько API"""
    
    # Очищаем URL
    clean_url = clean_tiktok_url(url)
    
    # Пробуем основной API (tikwm)
    try:
        api_url = "https://tikwm.com/api/"
        params = {"url": clean_url, "count": 12, "cursor": 0, "web": 1, "hd": 1}
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://www.tiktok.com/'
        }
        
        response = requests.get(api_url, params=params, headers=headers, timeout=API_TIMEOUT)
        
        if response.status_code == 200:
            data = response.json()
            
            if data.get('code') == 0:
                video_data = data.get('data', {})
                
                # Получаем изображения
                images = video_data.get('images', [])
                
                # Если нет массива images, но есть одиночное фото
                if not images and video_data.get('image'):
                    images = [video_data['image']]
                
                if images:
                    author = video_data.get('author', {})
                    author_name = author.get('unique_id') or author.get('nickname', 'Неизвестный')
                    description = video_data.get('title', '')[:200]
                    
                    logger.info(f"Successfully got TikTok info from tikwm.com, {len(images)} images")
                    return {
                        'type': 'photo',
                        'images': images,
                        'author': author_name,
                        'description': description,
                        'url': clean_url,
                        'duration': video_data.get('duration')
                    }
                    
    except Exception as e:
        logger.error(f"TikWM API error: {e}")
    
    # Если основной API не сработал, пробуем альтернативный
    logger.info("Trying alternative TikTok API...")
    alt_result = get_tiktok_info_alternative(clean_url)
    if alt_result:
        logger.info("Successfully got TikTok info from alternative API")
        return alt_result
    
    # Пробуем прямой парсинг HTML как последний вариант
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1'
        }
        
        response = requests.get(clean_url, headers=headers, timeout=API_TIMEOUT)
        
        if response.status_code == 200:
            # Ищем ссылки на изображения в HTML
            # Паттерн для поиска изображений TikTok
            img_patterns = [
                r'https://p\d{1,3}\.tikcdn\.com/[^"\']+\.(jpg|png|webp)',
                r'https://www\.tikcdn\.com/[^"\']+\.(jpg|png|webp)'
            ]
            
            images = []
            for pattern in img_patterns:
                found = re.findall(pattern, response.text)
                if found:
                    # Извлекаем полные URL
                    urls = re.findall(pattern, response.text)
                    for url in urls:
                        if url not in images:
                            images.append(url)
            
            if images:
                logger.info(f"Found {len(images)} images via HTML parsing")
                return {
                    'type': 'photo',
                    'images': images[:MAX_MEDIA_GROUP],
                    'author': 'TikTok',
                    'description': '',
                    'url': clean_url
                }
                
    except Exception as e:
        logger.error(f"HTML parsing error: {e}")
    
    return None

async def download_tiktok_photos(images: List[str], output_dir: Path) -> List[Path]:
    """Скачивает фото TikTok с правильными заголовками"""
    photo_paths = []
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://www.tiktok.com/',
        'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9'
    }
    
    for i, img_url in enumerate(images[:MAX_MEDIA_GROUP]):
        output_file = output_dir / f"photo_{i+1}.jpg"
        
        try:
            # Добавляем задержку между запросами чтобы не банили
            if i > 0:
                await asyncio.sleep(0.5)
            
            response = requests.get(img_url, headers=headers, timeout=API_TIMEOUT, stream=True)
            
            if response.status_code == 200:
                content_type = response.headers.get('content-type', '').lower()
                
                # Проверяем что это изображение
                if any(img_type in content_type for img_type in ['image', 'jpeg', 'png', 'webp']):
                    with open(output_file, 'wb') as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            f.write(chunk)
                    
                    # Проверяем размер файла
                    if output_file.stat().st_size > 0:
                        photo_paths.append(output_file)
                        logger.info(f"Downloaded photo {i+1}/{len(images[:MAX_MEDIA_GROUP])} ({output_file.stat().st_size} bytes)")
                    else:
                        logger.warning(f"Downloaded empty file for {img_url}")
                else:
                    logger.warning(f"Invalid content type for {img_url}: {content_type}")
            else:
                logger.warning(f"Failed to download {img_url}: status {response.status_code}")
                
        except Exception as e:
            logger.error(f"Download photo error for {img_url}: {e}")
    
    return photo_paths

# ============ YT-DLP ОБРАБОТКА ============

async def get_media_info_ytdlp(url: str) -> Optional[Dict[str, Any]]:
    """Получает информацию о медиа через yt-dlp"""
    try:
        # Для TikTok фото пропускаем yt-dlp, так как он их не поддерживает
        if 'tiktok.com' in url and ('/photo/' in url or 'photo' in url):
            logger.info("Skipping yt-dlp for TikTok photo")
            return None
            
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
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), 
                timeout=60
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            logger.error(f"yt-dlp info timeout for {url}")
            return None
        
        if process.returncode != 0:
            stderr_text = stderr.decode()
            if "Unsupported URL" in stderr_text:
                logger.info(f"URL not supported by yt-dlp: {url}")
            else:
                logger.error(f"yt-dlp info error: {stderr_text}")
            return None
            
        info = json.loads(stdout.decode())
        
        # Определяем тип медиа
        ext = info.get('ext', '')
        is_video = info.get('_type') == 'video' or ext in ['mp4', 'webm', 'mkv', 'avi', 'mov']
        is_audio = info.get('acodec') != 'none' and info.get('vcodec') == 'none'
        is_image = ext in ['jpg', 'jpeg', 'png', 'webp', 'gif', 'jfif', 'bmp']
        
        media_type = 'video'
        if is_audio:
            media_type = 'audio'
        elif is_image:
            media_type = 'image'
        
        return {
            'type': media_type,
            'title': info.get('title', 'media'),
            'ext': ext,
            'author': info.get('uploader') or info.get('channel') or info.get('creator'),
            'description': info.get('description', '')[:200],
            'duration': info.get('duration'),
            'url': url
        }
        
    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error: {e}")
        return None
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
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), 
                timeout=YTDLP_TIMEOUT
            )
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
        
        if not check_file_size(output_file):
            logger.warning(f"File too large: {output_file.stat().st_size}")
            return False
        
        return True
        
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
        logger.info(f"Sent {len(media_group)} photos as media group")
        
    except BadRequest as e:
        logger.error(f"BadRequest while sending media group: {e}")
        for photo_path in photo_paths:
            try:
                with open(photo_path, 'rb') as f:
                    await update.message.reply_photo(photo=f, caption=caption, parse_mode='HTML')
                break
            except Exception as send_error:
                logger.error(f"Error sending single photo: {send_error}")
                
    except Exception as e:
        logger.error(f"Error sending media group: {e}")
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
        
        # ========== 1. СПЕЦИАЛЬНАЯ ОБРАБОТКА TikTok ФОТО ==========
        if 'tiktok.com' in text and ('/photo/' in text or 'photo' in text):
            await status_msg.edit_text("📸 Обнаружено TikTok фото, получаю изображения...")
            
            tiktok_info = get_tiktok_info(text)
            
            if tiktok_info and tiktok_info.get('images'):
                with tempfile.TemporaryDirectory() as tmpdir:
                    tmp_path = Path(tmpdir)
                    photos = await download_tiktok_photos(tiktok_info['images'], tmp_path)
                    
                    if photos:
                        media_type = "карусель" if len(photos) > 1 else "фото"
                        caption = format_caption(tiktok_info, "TikTok", media_type)
                        
                        if len(photos) > 1:
                            await send_photo_group(update, photos, caption)
                        else:
                            with open(photos[0], 'rb') as f:
                                await update.message.reply_photo(
                                    photo=f, 
                                    caption=caption, 
                                    parse_mode='HTML'
                                )
                        
                        await status_msg.delete()
                        return
                    else:
                        await status_msg.edit_text("❌ Не удалось загрузить фото. Возможно, ссылка на видео или пост удален.")
                        return
            else:
                await status_msg.edit_text("❌ Не удалось получить информацию о фото. Проверьте ссылку.")
                return
        
        # ========== 2. ПРОВЕРКА НАЛИЧИЯ YT-DLP ==========
        if not is_ytdlp_available():
            await status_msg.edit_text(
                "❌ yt-dlp не установлен.\n"
                "Установите командой: pip install yt-dlp"
            )
            return
        
        # ========== 3. ОБРАБОТКА ЧЕРЕЗ YT-DLP ==========
        media_info = await get_media_info_ytdlp(text)
        
        if not media_info:
            await status_msg.edit_text(
                "❌ Не удалось получить информацию о медиа.\n"
                "Возможно, ссылка не поддерживается или требуется авторизация."
            )
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
                await status_msg.edit_text(f"❌ Не удалось скачать {media_type_rus}\n🔗 {text}")
                return
            
            if not output_file.exists():
                possible_files = list(tmp_path.glob(f"{safe_title}.*"))
                if possible_files:
                    output_file = possible_files[0]
                else:
                    await status_msg.edit_text(f"❌ Файл не найден после скачивания\n🔗 {text}")
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
                        await update.message.reply_photo(
                            photo=f,
                            caption=caption,
                            parse_mode='HTML'
                        )
                    elif media_info['type'] == 'audio':
                        await update.message.reply_audio(
                            audio=f,
                            caption=caption,
                            parse_mode='HTML',
                            title=safe_title,
                            performer=media_info.get('author', 'Неизвестный')
                        )
                    else:
                        await update.message.reply_video(
                            video=f,
                            caption=caption,
                            parse_mode='HTML',
                            supports_streaming=True
                        )
                
                logger.info(f"Successfully sent {media_info['type']} from {text}")
                
            except BadRequest as e:
                logger.error(f"BadRequest while sending: {e}")
                await status_msg.edit_text(f"⚠️ Ошибка отправки: {str(e)[:100]}")
            except Exception as e:
                logger.error(f"Send error: {e}")
                await status_msg.edit_text(f"⚠️ Ошибка отправки медиа: {str(e)[:100]}")
            
            await status_msg.delete()
            
    except Exception as e:
        logger.error(f"Handler error: {e}", exc_info=True)
        error_msg = f"⚠️ Произошла ошибка: {str(e)[:100]}"
        
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
                deleted_count = 0
                
                for file_path in DOWNLOADS_DIR.iterdir():
                    if file_path.is_file():
                        file_age = current_time - file_path.stat().st_mtime
                        if file_age > CLEANUP_INTERVAL:
                            file_path.unlink()
                            deleted_count += 1
                
                if deleted_count > 0:
                    logger.info(f"Cleaned up {deleted_count} old files")
                    
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
        
        await asyncio.sleep(CLEANUP_INTERVAL)

# ============ ЗАПУСК БОТА ============

def main():
    """Запуск бота"""
    
    if not is_ytdlp_available():
        print("\n" + "="*50)
        print("⚠️  ВНИМАНИЕ: yt-dlp не установлен!")
        print("="*50)
        print("Бот будет работать ограниченно. Для полной функциональности:")
        print("pip install yt-dlp")
        print("="*50 + "\n")
    else:
        print("\n" + "="*40)
        print("🤖 Бот Druid запущен")
        print("="*40)
        print(f"✅ Токен: {TOKEN[:10]}...{TOKEN[-5:]}")
        print(f"✅ yt-dlp: установлен")
        print(f"✅ Макс. размер: {MAX_FILE_SIZE // (1024*1024)} MB")
        print(f"✅ Очистка файлов: каждые {CLEANUP_INTERVAL // 3600} ч")
        print("="*40 + "\n")
    
    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=120.0,
        write_timeout=120.0,
        pool_timeout=30.0
    )
    
    application = Application.builder()\
        .token(TOKEN)\
        .request(request)\
        .build()
    
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(cleanup_old_files())
    
    try:
        application.run_polling(drop_pending_updates=True)
    except Exception as e:
        print(f"❌ Ошибка запуска бота: {e}")
        print("Проверьте подключение к интернету и токен бота")
    finally:
        loop.close()

if __name__ == '__main__':
    main()
