#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram бот Druid - улучшенная версия
Поддерживает: TikTok (фото/карусель), YouTube, Instagram, Twitter/X и другие
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
from urllib.parse import urlparse
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
    # Удаляем недопустимые символы для файловой системы
    title = re.sub(r'[\\/*?:"<>|]', "", title)
    # Ограничиваем длину
    if len(title) > 80:
        title = title[:80]
    # Заменяем пробелы на подчеркивания
    title = title.replace(' ', '_')
    # Удаляем лишние точки в начале и конце
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
    
    # Эмодзи для разных типов медиа
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

def get_file_mime_type(file_path: Path) -> Optional[str]:
    """Определяет MIME тип файла"""
    try:
        import magic
        return magic.from_file(str(file_path), mime=True)
    except ImportError:
        # Если python-magic не установлен, определяем по расширению
        ext = file_path.suffix.lower()
        mime_map = {
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.png': 'image/png',
            '.gif': 'image/gif',
            '.webp': 'image/webp',
            '.mp4': 'video/mp4',
            '.webm': 'video/webm',
            '.mp3': 'audio/mpeg'
        }
        return mime_map.get(ext, 'application/octet-stream')
    except Exception:
        return None

# ============ TIKTOK ОБРАБОТКА ============

def get_tiktok_info(url: str) -> Optional[Dict[str, Any]]:
    """Получает информацию о TikTok (фото/карусель) через API"""
    try:
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
        
        # Получаем изображения
        images = video_data.get('images', [])
        
        # Если нет массива images, но есть одиночное фото
        if not images and video_data.get('image'):
            images = [video_data['image']]
        
        if images:
            author = video_data.get('author', {})
            author_name = author.get('unique_id') or author.get('nickname', 'Неизвестный')
            description = video_data.get('title', '')[:200]
            
            return {
                'type': 'photo',
                'images': images,
                'author': author_name,
                'description': description,
                'url': url,
                'duration': video_data.get('duration')
            }
        
        return None
        
    except requests.Timeout:
        logger.error("TikTok API timeout")
        return None
    except Exception as e:
        logger.error(f"TikTok info error: {e}")
        return None

async def download_tiktok_photos(images: List[str], output_dir: Path) -> List[Path]:
    """Скачивает фото TikTok с правильными заголовками"""
    photo_paths = []
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://www.tiktok.com/'
    }
    
    for i, img_url in enumerate(images[:MAX_MEDIA_GROUP]):
        output_file = output_dir / f"photo_{i+1}.jpg"
        
        try:
            response = requests.get(img_url, headers=headers, timeout=API_TIMEOUT)
            
            if response.status_code == 200:
                content_type = response.headers.get('content-type', '')
                if 'image' in content_type:
                    with open(output_file, 'wb') as f:
                        f.write(response.content)
                    photo_paths.append(output_file)
                    logger.info(f"Downloaded photo {i+1}/{len(images[:MAX_MEDIA_GROUP])}")
                else:
                    logger.warning(f"Invalid content type for {img_url}: {content_type}")
            else:
                logger.warning(f"Failed to download {img_url}: status {response.status_code}")
                
        except Exception as e:
            logger.error(f"Download photo error: {e}")
    
    return photo_paths

# ============ YT-DLP ОБРАБОТКА ============

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
        
        # Добавляем таймаут для медленных сайтов
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
            logger.error(f"yt-dlp info error: {stderr.decode()}")
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
        
        # Выбираем формат в зависимости от типа
        if media_type == 'video':
            # Сначала пытаемся скачать MP4 720p, затем 480p, затем любой MP4
            cmd.extend(['-f', 'best[height<=720][ext=mp4]/best[height<=480][ext=mp4]/best[ext=mp4]'])
        elif media_type == 'image':
            # Для фото скачиваем в лучшем качестве
            cmd.extend(['-f', 'best[ext=jpg]/best[ext=png]/best[ext=webp]'])
        elif media_type == 'audio':
            # Для аудио конвертируем в MP3
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
        
        # Проверяем существование файла
        if not output_file.exists():
            # Если файл с другим расширением (например, для аудио)
            possible_files = list(output_file.parent.glob(f"{output_file.stem}.*"))
            if possible_files:
                output_file = possible_files[0]
            else:
                return False
        
        # Проверяем размер
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
                # Подпись добавляем только к первому фото
                cap = caption if i == 0 else None
                media_group.append(InputMediaPhoto(media=f, caption=cap, parse_mode='HTML'))
        
        await update.message.reply_media_group(media_group)
        logger.info(f"Sent {len(media_group)} photos as media group")
        
    except BadRequest as e:
        logger.error(f"BadRequest while sending media group: {e}")
        # Если не удалось отправить группой, отправляем по одному
        for photo_path in photo_paths:
            try:
                with open(photo_path, 'rb') as f:
                    await update.message.reply_photo(photo=f, caption=caption, parse_mode='HTML')
                break  # Отправляем подпись только с первым фото
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
        
        # Проверяем, является ли текст ссылкой
        if not is_url(text):
            return
        
        # Проверяем наличие yt-dlp
        if not is_ytdlp_available():
            await update.message.reply_text(
                "❌ yt-dlp не установлен.\n"
                "Установите командой: pip install yt-dlp"
            )
            return
        
        # Отправляем сообщение о начале обработки
        status_msg = await update.message.reply_text("⏳ Обрабатываю ссылку...")
        
        # ========== 1. СПЕЦИАЛЬНАЯ ОБРАБОТКА TikTok ФОТО ==========
        if 'tiktok.com' in text and '/photo/' in text:
            await status_msg.edit_text("📸 Определено TikTok фото, получаю изображения...")
            
            tiktok_info = get_tiktok_info(text)
            
            if tiktok_info and tiktok_info.get('images'):
                # Создаем временную директорию
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
                        logger.warning("No photos downloaded from TikTok")
                        await status_msg.edit_text("⚠️ Не удалось загрузить фото, пробую другой способ...")
            else:
                logger.info("TikTok photo API failed, falling back to yt-dlp")
                await status_msg.edit_text("🔄 Пробую другой способ загрузки...")
        
        # ========== 2. ОБРАБОТКА ЧЕРЕЗ YT-DLP ==========
        # Получаем информацию о медиа
        media_info = await get_media_info_ytdlp(text)
        
        if not media_info:
            await status_msg.edit_text(
                "❌ Не удалось получить информацию о медиа.\n"
                "Возможно, ссылка не поддерживается или требуется авторизация."
            )
            return
        
        # Создаем временную директорию для скачивания
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            
            # Формируем имя файла
            safe_title = sanitize_filename(media_info['title'])
            ext = media_info.get('ext', 'mp4')
            
            if media_info['type'] == 'image':
                output_file = tmp_path / f"{safe_title}.{ext}"
            elif media_info['type'] == 'audio':
                output_file = tmp_path / f"{safe_title}.mp3"
            else:
                output_file = tmp_path / f"{safe_title}.mp4"
            
            # Обновляем статус
            media_type_rus = {
                'video': 'видео',
                'image': 'фото',
                'audio': 'аудио'
            }.get(media_info['type'], 'медиа')
            
            await status_msg.edit_text(f"⏳ Скачиваю {media_type_rus}...")
            
            # Скачиваем медиа
            success = await download_media_ytdlp(text, output_file, media_info['type'])
            
            if not success:
                await status_msg.edit_text(f"❌ Не удалось скачать {media_type_rus}\n🔗 {text}")
                return
            
            # Проверяем, что файл существует
            if not output_file.exists():
                # Ищем файл с другим расширением
                possible_files = list(tmp_path.glob(f"{safe_title}.*"))
                if possible_files:
                    output_file = possible_files[0]
                else:
                    await status_msg.edit_text(f"❌ Файл не найден после скачивания\n🔗 {text}")
                    return
            
            # Формируем подпись
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
            
            # Отправляем медиа
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
                    else:  # video
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
        
        # Ждем CLEANUP_INTERVAL секунд перед следующей очисткой
        await asyncio.sleep(CLEANUP_INTERVAL)

# ============ ЗАПУСК БОТА ============

def main():
    """Запуск бота"""
    
    # Проверка yt-dlp
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
    
    # Настройка HTTPX запросов
    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=120.0,
        write_timeout=120.0,
        pool_timeout=30.0
    )
    
    # Создаем приложение
    application = Application.builder()\
        .token(TOKEN)\
        .request(request)\
        .build()
    
    # Добавляем обработчик сообщений
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    
    # Запускаем фоновую задачу очистки файлов
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(cleanup_old_files())
    
    # Запускаем бота
    try:
        application.run_polling(drop_pending_updates=True)
    except Exception as e:
        print(f"❌ Ошибка запуска бота: {e}")
        print("Проверьте подключение к интернету и токен бота")
    finally:
        loop.close()

if __name__ == '__main__':
    main()
