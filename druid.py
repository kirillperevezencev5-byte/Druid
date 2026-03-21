#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram бот для скачивания медиа с поддержкой разбивки больших видео
Поддержка отправки частей в одном альбоме
"""

import os
import logging
import subprocess
import re
import json
import time
import requests
import tempfile
from typing import Optional, List, Tuple, Dict, Any
from urllib.parse import urlparse

from telegram import Update, InputMediaVideo, InputMediaPhoto
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from telegram.request import HTTPXRequest

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Константы
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB - лимит Telegram
MAX_MEDIA_GROUP = 10  # Максимум файлов в одном альбоме
DOWNLOADS_DIR = "downloads"

def sanitize_filename(title: str) -> str:
    """Очищает название файла от недопустимых символов"""
    title = re.sub(r'[\\/*?:"<>|]', "", title)
    if len(title) > 100:
        title = title[:100]
    title = re.sub(r'[#@$%&]', '', title)
    title = title.replace(' ', '_')
    return title.strip()

def extract_domain(url: str) -> str:
    """Извлекает домен из URL"""
    try:
        parsed = urlparse(url)
        domain = parsed.netloc
        if domain.startswith('www.'):
            domain = domain[4:]
        return domain
    except:
        return "неизвестный источник"

def get_platform_name(url: str) -> str:
    """Определяет название платформы по URL"""
    domain = extract_domain(url).lower()
    if 'youtube.com' in domain or 'youtu.be' in domain:
        return "YouTube"
    elif 'tiktok.com' in domain:
        return "TikTok"
    elif 'instagram.com' in domain:
        return "Instagram"
    elif 'twitter.com' in domain or 'x.com' in domain:
        return "Twitter/X"
    elif 'facebook.com' in domain or 'fb.com' in domain:
        return "Facebook"
    elif 'reddit.com' in domain:
        return "Reddit"
    elif 'pinterest.com' in domain:
        return "Pinterest"
    else:
        return domain.capitalize()

def get_video_duration(file_path: str) -> float:
    """Получает длительность видео в секундах через ffprobe"""
    try:
        cmd = [
            'ffprobe',
            '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            file_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception as e:
        logger.error(f"Ошибка получения длительности: {e}")
    return 0

def split_video_into_chunks(input_file: str, output_dir: str, max_size_mb: int = 50) -> List[str]:
    """
    Разбивает видео на части по размеру с сохранением качества 1080p
    Возвращает список путей к частям
    """
    chunks = []
    max_size_bytes = max_size_mb * 1024 * 1024
    
    # Получаем длительность видео
    duration = get_video_duration(input_file)
    if duration == 0:
        logger.error("Не удалось получить длительность видео")
        return chunks
    
    # Получаем размер файла
    file_size = os.path.getsize(input_file)
    
    # Если файл меньше лимита, возвращаем его как есть
    if file_size <= max_size_bytes:
        return [input_file]
    
    # Рассчитываем количество частей
    num_chunks = (file_size + max_size_bytes - 1) // max_size_bytes
    if num_chunks > MAX_MEDIA_GROUP:
        num_chunks = MAX_MEDIA_GROUP
    
    # Длительность каждой части
    chunk_duration = duration / num_chunks
    
    logger.info(f"Разбиваем видео {file_size / 1024 / 1024:.1f} MB на {num_chunks} частей")
    
    base_name = os.path.splitext(os.path.basename(input_file))[0]
    
    for i in range(num_chunks):
        start_time = i * chunk_duration
        # Последняя часть может быть короче
        if i == num_chunks - 1:
            end_time = duration
        else:
            end_time = (i + 1) * chunk_duration
        
        output_file = os.path.join(output_dir, f"{base_name}_part_{i+1:02d}.mp4")
        
        # Разбиваем видео без перекодирования (сохраняем качество)
        # Используем copy кодек для сохранения оригинального качества
        cmd = [
            'ffmpeg',
            '-i', input_file,
            '-ss', str(start_time),
            '-to', str(end_time),
            '-c', 'copy',  # Копируем потоки без перекодирования
            '-avoid_negative_ts', 'make_zero',
            output_file
        ]
        
        try:
            logger.info(f"Создаем часть {i+1}/{num_chunks} ({start_time:.1f}s - {end_time:.1f}s)")
            subprocess.run(cmd, capture_output=True, check=True, timeout=300)
            
            if os.path.exists(output_file):
                chunk_size = os.path.getsize(output_file)
                logger.info(f"Часть {i+1}: {chunk_size / 1024 / 1024:.1f} MB")
                chunks.append(output_file)
            else:
                logger.error(f"Не удалось создать часть {i+1}")
                
        except subprocess.CalledProcessError as e:
            logger.error(f"Ошибка при создании части {i+1}: {e.stderr}")
        except Exception as e:
            logger.error(f"Ошибка: {e}")
    
    return chunks

def escape_html(text: str) -> str:
    """Экранирует HTML спецсимволы"""
    if not text:
        return text
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

def format_caption(info: Dict[str, Any], platform: str, media_type: str, part_info: str = "") -> str:
    """
    Формирует подпись для медиафайла с информацией об источнике.
    Использует HTML для надежности.
    """
    caption_parts = []
    
    # Добавляем эмодзи в зависимости от типа
    if media_type == 'фото':
        caption_parts.append("📸 ")
    elif media_type == 'карусель':
        caption_parts.append("🖼️ ")
    elif media_type == 'видео':
        caption_parts.append("🎬 ")
    
    caption_parts.append(f"<b>{escape_html(platform)}</b>\n")
    
    # Информация о частях видео
    if part_info:
        caption_parts.append(f"📀 {part_info}\n")
    
    # Информация об авторе
    if info.get('author'):
        author = escape_html(info['author'])
        caption_parts.append(f"👤 <b>Автор:</b> {author}\n")
    
    # Описание (если есть)
    if info.get('description'):
        description = escape_html(info['description'])
        if len(description) > 200:
            description = description[:200] + "..."
        caption_parts.append(f"📝 <b>Описание:</b> {description}\n")
    
    # Исходная ссылка
    if info.get('url'):
        url = info['url']
        caption_parts.append(f"🔗 <b>Источник:</b> <a href='{url}'>ссылка</a>")
    
    return "".join(caption_parts)

def is_url(text: str) -> bool:
    """Проверяет, является ли текст ссылкой"""
    url_pattern = re.compile(
        r'^https?://'
        r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|'
        r'localhost|'
        r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'
        r'(?::\d+)?'
        r'(?:/?|[/?]\S+)$', re.IGNORECASE)
    return url_pattern.match(text.strip()) is not None

async def download_video_with_ytdlp(url: str, output_file: str) -> bool:
    """Скачивает видео через yt-dlp в лучшем качестве 1080p"""
    try:
        # Скачиваем в лучшем качестве 1080p
        cmd = [
            'yt-dlp',
            '-f', 'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best',
            '-o', output_file,
            '--no-playlist',
            '--merge-output-format', 'mp4',
            url
        ]
        
        logger.info(f"Скачиваем видео: {url}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        
        if result.returncode != 0:
            logger.error(f"Ошибка yt-dlp: {result.stderr}")
            return False
            
        return os.path.exists(output_file) and os.path.getsize(output_file) > 0
        
    except Exception as e:
        logger.error(f"Ошибка скачивания: {e}")
        return False

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений. Реагирует только на ссылки."""
    text = update.message.text.strip()
    
    # Проверяем, является ли сообщение ссылкой
    if not is_url(text):
        logger.info(f"Игнорируем сообщение (не ссылка): {text[:50]}")
        return
    
    # Это ссылка - обрабатываем
    status_msg = await update.message.reply_text("⏳ Обрабатываю ссылку...")

    try:
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        os.makedirs(os.path.join(DOWNLOADS_DIR, "chunks"), exist_ok=True)
        
        # Получаем информацию о видео
        try:
            cmd_info = ['yt-dlp', '--dump-json', '--no-playlist', text]
            result = subprocess.run(cmd_info, capture_output=True, text=True, timeout=30)
            
            if result.returncode != 0:
                await update.message.reply_text("❌ Не удалось получить информацию о видео.")
                return
                
            info = json.loads(result.stdout)
            title = info.get('title', 'video')
            safe_title = sanitize_filename(title)
            
            # Формируем метаданные
            media_info = {
                'author': info.get('uploader', info.get('channel', 'Неизвестный автор')),
                'description': title if title != 'video' else None,
                'url': text
            }
            
            platform = get_platform_name(text)
            
            # Проверяем, является ли это видео
            ext = info.get('ext', '')
            if ext in ('jpg', 'jpeg', 'png', 'webp', 'gif'):
                # Обработка фото (как в предыдущей версии)
                await update.message.reply_text("❌ Пока поддерживаются только видео.")
                return
            
            # Скачиваем видео
            await status_msg.edit_text("⏳ Скачиваю видео в 1080p...")
            temp_video = os.path.join(DOWNLOADS_DIR, f"{safe_title}.mp4")
            
            if not await download_video_with_ytdlp(text, temp_video):
                await update.message.reply_text("❌ Не удалось скачать видео.")
                return
            
            file_size = os.path.getsize(temp_video)
            logger.info(f"Видео скачано: {file_size / 1024 / 1024:.1f} MB")
            
            # Если видео меньше лимита - отправляем как есть
            if file_size <= MAX_FILE_SIZE:
                await status_msg.edit_text("⏳ Отправляю видео...")
                caption = format_caption(media_info, platform, "видео")
                with open(temp_video, 'rb') as f:
                    await update.message.reply_video(
                        video=f,
                        caption=caption,
                        parse_mode='HTML',
                        supports_streaming=True
                    )
                os.remove(temp_video)
                return
            
            # Если видео больше лимита - разбиваем на части
            await status_msg.edit_text(f"⏳ Видео {file_size / 1024 / 1024:.1f} MB, разбиваю на части...")
            
            chunks_dir = os.path.join(DOWNLOADS_DIR, "chunks", safe_title)
            os.makedirs(chunks_dir, exist_ok=True)
            
            # Разбиваем видео на части
            chunks = split_video_into_chunks(temp_video, chunks_dir, 50)
            
            if not chunks:
                await update.message.reply_text("❌ Не удалось разбить видео на части.")
                return
            
            if len(chunks) == 1:
                # Если после разбивки получился 1 файл (ошибка разбивки)
                with open(chunks[0], 'rb') as f:
                    await update.message.reply_video(
                        video=f,
                        caption=format_caption(media_info, platform, "видео"),
                        parse_mode='HTML'
                    )
            else:
                # Отправляем все части одним альбомом
                await status_msg.edit_text(f"⏳ Отправляю {len(chunks)} частей одним альбомом...")
                
                media_group = []
                for i, chunk_path in enumerate(chunks[:MAX_MEDIA_GROUP]):
                    part_text = f"Часть {i+1}/{len(chunks)}"
                    caption = format_caption(media_info, platform, "видео", part_text) if i == 0 else None
                    
                    with open(chunk_path, 'rb') as f:
                        media_group.append(
                            InputMediaVideo(
                                media=f,
                                caption=caption,
                                parse_mode='HTML',
                                supports_streaming=True
                            )
                        )
                
                # Отправляем альбом
                await update.message.reply_media_group(media_group)
                
                # Если частей больше 10, отправляем остальные отдельно
                if len(chunks) > MAX_MEDIA_GROUP:
                    await update.message.reply_text(
                        f"⚠️ Видео разбито на {len(chunks)} частей, "
                        f"но Telegram позволяет отправить только {MAX_MEDIA_GROUP} в одном альбоме.\n"
                        f"Остальные части отправлены отдельно:"
                    )
                    
                    for i, chunk_path in enumerate(chunks[MAX_MEDIA_GROUP:]):
                        with open(chunk_path, 'rb') as f:
                            await update.message.reply_video(
                                video=f,
                                caption=f"📀 Часть {i + MAX_MEDIA_GROUP + 1}/{len(chunks)}",
                                supports_streaming=True
                            )
            
            # Очистка временных файлов
            os.remove(temp_video)
            for chunk in chunks:
                try:
                    os.remove(chunk)
                except:
                    pass
            try:
                os.rmdir(chunks_dir)
            except:
                pass
                
        except Exception as e:
            logger.error(f"Ошибка обработки: {e}")
            await update.message.reply_text(f"⚠️ Ошибка: {str(e)[:100]}")
            
    except Exception as e:
        logger.exception("Ошибка в handle_message")
        await update.message.reply_text(f"⚠️ Ошибка: {str(e)[:100]}")
    finally:
        try:
            await status_msg.delete()
        except:
            pass

def cleanup_old_files(max_age_seconds: int = 3600):
    """Очищает старые файлы."""
    try:
        if not os.path.exists(DOWNLOADS_DIR):
            return
        current = time.time()
        for root, dirs, files in os.walk(DOWNLOADS_DIR):
            for fname in files:
                fpath = os.path.join(root, fname)
                if os.path.isfile(fpath):
                    if current - os.path.getmtime(fpath) > max_age_seconds:
                        os.remove(fpath)
    except Exception as e:
        logger.error(f"Ошибка очистки: {e}")

def main():
    token = "8783056247:AAHGJF9vtDwuoCQBwfhdYOqQgFRsgfGAAp4"
    cleanup_old_files()
    
    # Проверяем наличие необходимых программ
    try:
        subprocess.run(['yt-dlp', '--version'], capture_output=True, check=True)
        logger.info("✅ yt-dlp найден")
    except:
        logger.error("❌ yt-dlp не установлен!")
        print("⚠️ Установите: pip install yt-dlp")
    
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
        logger.info("✅ ffmpeg найден")
    except:
        logger.error("❌ ffmpeg не установлен!")
        print("⚠️ Установите: sudo apt install ffmpeg")
    
    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0
    )
    application = Application.builder().token(token).request(request).build()
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("🤖 Бот Druid запущен!")
    print("✅ Реагирует ТОЛЬКО на ссылки")
    print("🎬 Поддержка видео 1080p")
    print("📦 Автоматическая разбивка видео > 50 MB")
    print("🖼️ Отправка частей в одном альбоме (до 10 частей)")
    print("⚡ Сохранение оригинального качества (без перекодирования)")
    application.run_polling(drop_pending_updates=True, allowed_updates=['message'])

if __name__ == '__main__':
    main()
