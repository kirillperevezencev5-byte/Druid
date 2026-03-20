#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram бот для скачивания медиа (YouTube, TikTok, Instagram и др.)
Поддержка каруселей, отправка фото как фото.
Добавлено: описание источника, реакция только на ссылки.
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

from telegram import Update, InputMediaPhoto
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from telegram.request import HTTPXRequest

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Константы
MAX_FILE_SIZE = 50 * 1024 * 1024
MAX_MEDIA_GROUP = 10
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
        # Убираем www.
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

def get_tiktok_info(url: str) -> Optional[Dict[str, Any]]:
    """
    Получает информацию о контенте TikTok через API.
    Возвращает словарь с ключами:
        - 'type': 'photo' или 'video'
        - 'images': список URL фото (для фото)
        - 'video_url': URL видео (для видео)
        - 'author': автор контента
        - 'description': описание
    В случае ошибки возвращает None.
    """
    try:
        api_url = "https://tikwm.com/api/"
        params = {
            "url": url,
            "count": 12,
            "cursor": 0,
            "web": 1,
            "hd": 1
        }
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json'
        }
        
        logger.info(f"Запрос к API TikTok: {url}")
        resp = requests.get(api_url, params=params, headers=headers, timeout=30)
        
        if resp.status_code != 200:
            logger.error(f"Ошибка API: {resp.status_code}")
            return None
            
        data = resp.json()
        if data.get('code') != 0:
            logger.error(f"API вернул ошибку: {data.get('msg')}")
            return None
            
        video_data = data.get('data', {})
        images = video_data.get('images')
        video_url = video_data.get('play')
        
        # Извлекаем метаданные
        author = video_data.get('author', {})
        author_name = author.get('unique_id', author.get('nickname', 'Неизвестный автор'))
        description = video_data.get('title', '')
        if description:
            # Ограничиваем длину описания
            if len(description) > 200:
                description = description[:200] + "..."
        
        if images and isinstance(images, list) and len(images) > 0:
            # Это фото (карусель или одиночное)
            return {
                'type': 'photo',
                'images': images,
                'video_url': None,
                'author': author_name,
                'description': description,
                'url': url
            }
        elif video_url:
            # Это видео
            return {
                'type': 'video',
                'images': [],
                'video_url': video_url,
                'author': author_name,
                'description': description,
                'url': url
            }
        else:
            logger.error("Не удалось определить тип контента")
            return None
            
    except Exception as e:
        logger.error(f"Ошибка получения информации TikTok: {e}")
        return None

async def download_tiktok_photos(images: List[str], output_dir: str) -> List[str]:
    """
    Скачивает список фото из TikTok.
    Возвращает список путей к скачанным файлам.
    """
    photo_paths = []
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    
    for i, img_url in enumerate(images):
        output_file = os.path.join(output_dir, f"tiktok_photo_{i+1}.jpg")
        try:
            img_resp = requests.get(img_url, headers=headers, timeout=30)
            if img_resp.status_code == 200:
                content_type = img_resp.headers.get('content-type', '')
                if 'image' in content_type:
                    with open(output_file, 'wb') as f:
                        f.write(img_resp.content)
                    photo_paths.append(output_file)
                    logger.info(f"Скачано фото {i+1}: {output_file}")
                else:
                    logger.error(f"Фото {i+1} не является изображением: {content_type}")
            else:
                logger.error(f"Ошибка скачивания фото {i+1}: {img_resp.status_code}")
        except Exception as e:
            logger.error(f"Ошибка при скачивании фото {i+1}: {e}")
    
    return photo_paths

def format_caption(info: Dict[str, Any], platform: str, media_type: str) -> str:
    """
    Формирует подпись для медиафайла с информацией об источнике.
    
    Args:
        info: словарь с информацией о медиа
        platform: название платформы
        media_type: тип медиа ('фото', 'видео', 'карусель')
    
    Returns:
        отформатированная подпись
    """
    caption_parts = []
    
    # Добавляем эмодзи в зависимости от типа
    if media_type == 'фото':
        caption_parts.append("📸")
    elif media_type == 'карусель':
        caption_parts.append("🖼️")
    elif media_type == 'видео':
        caption_parts.append("🎬")
    
    caption_parts.append(f"**{platform}**\n")
    
    # Информация об авторе
    if info.get('author'):
        caption_parts.append(f"👤 **Автор:** {info['author']}\n")
    
    # Описание (если есть)
    if info.get('description'):
        caption_parts.append(f"📝 **Описание:** {info['description']}\n")
    
    # Исходная ссылка
    if info.get('url'):
        caption_parts.append(f"🔗 **Источник:** {info['url']}")
    
    return "".join(caption_parts)

def is_url(text: str) -> bool:
    """Проверяет, является ли текст ссылкой"""
    url_pattern = re.compile(
        r'^https?://'  # http:// или https://
        r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|'  # домен
        r'localhost|'  # localhost
        r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'  # или IP
        r'(?::\d+)?'  # опциональный порт
        r'(?:/?|[/?]\S+)$', re.IGNORECASE)
    return url_pattern.match(text.strip()) is not None

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений. Реагирует только на ссылки."""
    text = update.message.text.strip()
    
    # Проверяем, является ли сообщение ссылкой
    if not is_url(text):
        # Игнорируем обычный текст
        logger.info(f"Игнорируем сообщение (не ссылка): {text[:50]}")
        return
    
    # Это ссылка - обрабатываем
    status_msg = await update.message.reply_text("⏳ Обрабатываю ссылку...")

    try:
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        
        # Специальная обработка для TikTok
        if 'tiktok.com' in text:
            logger.info(f"Обнаружена ссылка TikTok: {text}")
            tiktok_info = get_tiktok_info(text)
            
            if tiktok_info and tiktok_info['type'] == 'photo' and tiktok_info['images']:
                # Это фото/карусель
                with tempfile.TemporaryDirectory() as tmpdir:
                    photo_paths = await download_tiktok_photos(tiktok_info['images'], tmpdir)
                    if photo_paths:
                        caption = format_caption(tiktok_info, "TikTok", "карусель" if len(photo_paths) > 1 else "фото")
                        
                        if len(photo_paths) > 1:
                            # Отправляем как альбом (карусель)
                            logger.info(f"Отправляем {len(photo_paths)} фото как альбом")
                            media = []
                            for i, path in enumerate(photo_paths[:MAX_MEDIA_GROUP]):
                                with open(path, 'rb') as f:
                                    # Подпись добавляем только к первому фото
                                    cap = caption if i == 0 else None
                                    media.append(InputMediaPhoto(media=f, caption=cap, parse_mode='Markdown'))
                            await update.message.reply_media_group(media)
                        else:
                            # Одиночное фото
                            logger.info("Отправляем одиночное фото")
                            with open(photo_paths[0], 'rb') as f:
                                await update.message.reply_photo(
                                    photo=f, 
                                    caption=caption,
                                    parse_mode='Markdown'
                                )
                        return
                    else:
                        await update.message.reply_text("❌ Не удалось скачать фото из TikTok.")
                        return
                        
            elif tiktok_info and tiktok_info['type'] == 'video':
                # Это видео - используем yt-dlp с метаданными
                logger.info("Обрабатываем TikTok видео")
                # Сохраняем метаданные для подписи
                context.user_data['current_info'] = tiktok_info
                context.user_data['platform'] = "TikTok"
        
        # Для всех остальных платформ используем yt-dlp
        try:
            # Получаем информацию через yt-dlp
            cmd_info = ['yt-dlp', '--dump-json', '--no-playlist', text]
            result = subprocess.run(cmd_info, capture_output=True, text=True, timeout=30)
            
            if result.returncode == 0:
                info = json.loads(result.stdout)
                ext = info.get('ext', '')
                title = info.get('title', 'media')
                safe_title = sanitize_filename(title)
                
                # Формируем информацию для подписи
                media_info = {
                    'author': info.get('uploader', info.get('channel', 'Неизвестный автор')),
                    'description': title if title != 'media' else None,
                    'url': text
                }
                
                # Если нет информации о платформе из TikTok, определяем
                platform = context.user_data.get('platform', get_platform_name(text))
                
                # Если это фото
                if ext in ('jpg', 'jpeg', 'png', 'webp', 'gif'):
                    output_file = os.path.join(DOWNLOADS_DIR, f"{safe_title}.{ext}")
                    cmd = [
                        'yt-dlp',
                        '-f', 'best[ext=jpg]/best[ext=png]/best[ext=webp]/best',
                        '-o', output_file,
                        '--no-playlist',
                        text
                    ]
                    subprocess.run(cmd, capture_output=True, timeout=120)
                    if os.path.exists(output_file):
                        caption = format_caption(media_info, platform, "фото")
                        with open(output_file, 'rb') as f:
                            await update.message.reply_photo(
                                photo=f, 
                                caption=caption,
                                parse_mode='Markdown'
                            )
                        try:
                            os.remove(output_file)
                        except:
                            pass
                        return
                
                # Если это видео
                output_file = os.path.join(DOWNLOADS_DIR, f"{safe_title}.mp4")
                cmd = [
                    'yt-dlp',
                    '-f', 'best[ext=mp4]/best',
                    '-o', output_file,
                    '--no-playlist',
                    text
                ]
                subprocess.run(cmd, capture_output=True, timeout=300)
                if os.path.exists(output_file):
                    caption = format_caption(media_info, platform, "видео")
                    with open(output_file, 'rb') as f:
                        await update.message.reply_video(
                            video=f, 
                            caption=caption,
                            parse_mode='Markdown'
                        )
                    try:
                        os.remove(output_file)
                    except:
                        pass
                    return
                        
            else:
                logger.error(f"yt-dlp ошибка: {result.stderr}")
                
        except Exception as e:
            logger.error(f"Ошибка обработки: {e}")
        
        await update.message.reply_text("❌ Не удалось скачать файл.")
        
    except Exception as e:
        logger.exception("Ошибка в handle_message")
        await update.message.reply_text(f"⚠️ Ошибка: {str(e)[:100]}")
    finally:
        try:
            await status_msg.delete()
        except:
            pass
        # Очищаем временные данные
        if 'current_info' in context.user_data:
            del context.user_data['current_info']
        if 'platform' in context.user_data:
            del context.user_data['platform']

def cleanup_old_files(max_age_seconds: int = 3600):
    """Очищает старые файлы."""
    try:
        if not os.path.exists(DOWNLOADS_DIR):
            return
        current = time.time()
        for fname in os.listdir(DOWNLOADS_DIR):
            fpath = os.path.join(DOWNLOADS_DIR, fname)
            if os.path.isfile(fpath):
                if current - os.path.getmtime(fpath) > max_age_seconds:
                    os.remove(fpath)
    except Exception as e:
        logger.error(f"Ошибка очистки: {e}")

def main():
    token = "8783056247:AAHGJF9vtDwuoCQBwfhdYOqQgFRsgfGAAp4"
    cleanup_old_files()
    
    # Устанавливаем библиотеки
    try:
        import requests
    except ImportError:
        os.system("pip install requests")
    
    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0
    )
    application = Application.builder().token(token).request(request).build()
    # Реагируем только на текстовые сообщения (без команд)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("🤖 Бот Druid запущен!")
    print("✅ Бот реагирует ТОЛЬКО на ссылки")
    print("✅ Под каждым файлом будет указан источник и описание")
    print("✅ Поддержка TikTok фото (карусели и одиночные)")
    print("📱 Фото отправляются как фото, карусели — как альбомы")
    print("🎬 Для видео используется yt-dlp")
    application.run_polling(drop_pending_updates=True, allowed_updates=['message'])

if __name__ == '__main__':
    main()
