#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram бот Druid - исправленная версия (с приоритетом TikTok фото)
"""

import os
import logging
import subprocess
import re
import json
import time
import requests
import tempfile
from typing import Optional, List, Dict, Any
from urllib.parse import urlparse

from telegram import Update, InputMediaPhoto
from telegram.ext import Application, MessageHandler, filters, ContextTypes
from telegram.request import HTTPXRequest

# ============ НАСТРОЙКИ ============
TOKEN = "8783056247:AAHGJF9vtDwuoCQBwfhdYOqQgFRsgfGAAp4"
MAX_FILE_SIZE = 50 * 1024 * 1024
MAX_MEDIA_GROUP = 10
DOWNLOADS_DIR = "downloads"
CLEANUP_HOURS = 2
# ===================================

# Настройка логирования - только ошибки
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.ERROR
)
logger = logging.getLogger(__name__)

def is_ytdlp_available() -> bool:
    """Проверяет доступность yt-dlp"""
    try:
        subprocess.run(['yt-dlp', '--version'], capture_output=True, check=True, timeout=5)
        return True
    except:
        return False

def sanitize_filename(title: str) -> str:
    """Очищает название файла"""
    title = re.sub(r'[\\/*?:"<>|]', "", title)
    if len(title) > 80:
        title = title[:80]
    title = title.replace(' ', '_')
    return title.strip()

def is_url(text: str) -> bool:
    """Проверка на ссылку"""
    url_pattern = re.compile(
        r'^https?://'
        r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|'
        r'localhost|'
        r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'
        r'(?::\d+)?'
        r'(?:/?|[/?]\S+)$', re.IGNORECASE)
    return url_pattern.match(text.strip()) is not None

def get_platform_name(url: str) -> str:
    """Определяет платформу"""
    domain = urlparse(url).netloc.lower()
    if 'youtube.com' in domain or 'youtu.be' in domain:
        return "YouTube"
    elif 'tiktok.com' in domain:
        return "TikTok"
    elif 'instagram.com' in domain:
        return "Instagram"
    elif 'twitter.com' in domain or 'x.com' in domain:
        return "Twitter/X"
    else:
        return domain.replace('www.', '').split('.')[0].capitalize()

def escape_html(text: str) -> str:
    """Экранирует HTML символы"""
    if not text:
        return text
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

def format_caption(info: Dict[str, Any], platform: str, media_type: str) -> str:
    """Формирует подпись"""
    caption_parts = []
    
    emojis = {'фото': '📸', 'видео': '🎬', 'карусель': '🖼️'}
    caption_parts.append(f"{emojis.get(media_type, '📎')} ")
    caption_parts.append(f"<b>{escape_html(platform)}</b>\n")
    
    if info.get('author'):
        caption_parts.append(f"👤 <b>Автор:</b> {escape_html(info['author'][:50])}\n")
    
    if info.get('description'):
        desc = escape_html(info['description'][:150])
        caption_parts.append(f"📝 <b>Описание:</b> {desc}\n")
    
    if info.get('url'):
        caption_parts.append(f"🔗 <b>Источник:</b> <a href='{info['url']}'>ссылка</a>")
    
    return "".join(caption_parts)

def get_tiktok_info(url: str) -> Optional[Dict[str, Any]]:
    """Получает информацию о TikTok (фото/карусель)"""
    try:
        api_url = "https://tikwm.com/api/"
        params = {"url": url, "count": 12, "cursor": 0, "web": 1, "hd": 1}
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
        
        resp = requests.get(api_url, params=params, headers=headers, timeout=20)
        if resp.status_code != 200:
            logger.error(f"TikTok API returned {resp.status_code}")
            return None
            
        data = resp.json()
        if data.get('code') != 0:
            logger.error(f"TikTok API error code {data.get('code')}: {data.get('msg')}")
            return None
            
        video_data = data.get('data', {})
        images = video_data.get('images')
        
        if images and len(images) > 0:
            author = video_data.get('author', {})
            author_name = author.get('unique_id', author.get('nickname', 'Неизвестный'))
            description = video_data.get('title', '')[:150]
            
            return {
                'type': 'photo',
                'images': images,
                'author': author_name,
                'description': description,
                'url': url
            }
        return None
    except Exception as e:
        logger.error(f"TikTok info error: {e}")
        return None

async def download_tiktok_photos(images: List[str], output_dir: str) -> List[str]:
    """Скачивает фото TikTok"""
    photo_paths = []
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
    
    for i, img_url in enumerate(images[:MAX_MEDIA_GROUP]):
        output_file = os.path.join(output_dir, f"photo_{i+1}.jpg")
        try:
            img_resp = requests.get(img_url, headers=headers, timeout=20)
            if img_resp.status_code == 200 and 'image' in img_resp.headers.get('content-type', ''):
                with open(output_file, 'wb') as f:
                    f.write(img_resp.content)
                photo_paths.append(output_file)
            else:
                logger.warning(f"Failed to download {img_url}: status {img_resp.status_code}")
        except Exception as e:
            logger.error(f"Download photo error: {e}")
    
    return photo_paths

async def download_video_ytdlp(url: str, output_file: str) -> bool:
    """Скачивает видео через yt-dlp"""
    if not is_ytdlp_available():
        return False
        
    try:
        cmd = [
            'yt-dlp',
            '-f', 'best[height<=720][ext=mp4]/best[height<=480][ext=mp4]/best',
            '-o', output_file,
            '--no-playlist',
            '--no-warnings',
            url
        ]
        
        result = subprocess.run(cmd, capture_output=True, timeout=600)
        
        if result.returncode != 0:
            logger.error(f"yt-dlp error: {result.stderr}")
            return False
            
        return os.path.exists(output_file) and os.path.getsize(output_file) <= MAX_FILE_SIZE
        
    except Exception as e:
        logger.error(f"Download error: {e}")
        return False

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Главный обработчик сообщений"""
    try:
        text = update.message.text.strip()
        
        if not is_url(text):
            return
        
        if not is_ytdlp_available():
            await update.message.reply_text(
                "❌ yt-dlp не установлен.\n"
                "Установите командой: pip install yt-dlp"
            )
            return
        
        status_msg = await update.message.reply_text("⏳ Обрабатываю...")
        
        try:
            os.makedirs(DOWNLOADS_DIR, exist_ok=True)
            
            # ========== 1. СПЕЦИАЛЬНАЯ ОБРАБОТКА TikTok ФОТО (карусель) ==========
            if 'tiktok.com' in text and '/photo/' in text:
                tiktok_info = get_tiktok_info(text)
                if tiktok_info and tiktok_info.get('images'):
                    with tempfile.TemporaryDirectory() as tmpdir:
                        photos = await download_tiktok_photos(tiktok_info['images'], tmpdir)
                        if photos:
                            caption = format_caption(tiktok_info, "TikTok", "карусель" if len(photos) > 1 else "фото")
                            
                            if len(photos) > 1:
                                media = []
                                for i, path in enumerate(photos):
                                    with open(path, 'rb') as f:
                                        cap = caption if i == 0 else None
                                        media.append(InputMediaPhoto(media=f, caption=cap, parse_mode='HTML'))
                                await update.message.reply_media_group(media)
                            else:
                                with open(photos[0], 'rb') as f:
                                    await update.message.reply_photo(photo=f, caption=caption, parse_mode='HTML')
                            await status_msg.delete()
                            return
                    # Если не удалось получить фото, выводим ошибку и завершаем
                    await update.message.reply_text("❌ Не удалось обработать TikTok фото. Возможно, ссылка недействительна или контент скрыт.")
                    await status_msg.delete()
                    return
            
            # ========== 2. ОБРАБОТКА ЧЕРЕЗ YT-DLP (видео, фото, аудио) ==========
            # Получаем информацию через yt-dlp
            cmd_info = ['yt-dlp', '--dump-json', '--no-playlist', '--no-warnings', text]
            result = subprocess.run(cmd_info, capture_output=True, text=True, timeout=30)
            
            if result.returncode != 0:
                # yt-dlp не смог получить информацию, возможно, ссылка не поддерживается
                logger.error(f"yt-dlp info error: {result.stderr}")
                await update.message.reply_text("❌ Не удалось получить информацию о видео. Возможно, ссылка не поддерживается или требуется логин.")
                await status_msg.delete()
                return
                
            info = json.loads(result.stdout)
            title = info.get('title', 'media')
            ext = info.get('ext', '')
            
            # Обработка фото (расширения изображений)
            if ext in ('jpg', 'jpeg', 'png', 'webp', 'gif', 'jfif', 'bmp'):
                safe_title = sanitize_filename(title)
                output_file = os.path.join(DOWNLOADS_DIR, f"{safe_title}.{ext}")
                
                cmd = ['yt-dlp', '-o', output_file, '--no-playlist', '--no-warnings', text]
                proc = subprocess.run(cmd, capture_output=True, timeout=120)
                
                if proc.returncode != 0:
                    logger.error(f"yt-dlp download photo error: {proc.stderr}")
                    await update.message.reply_text(f"❌ Не удалось скачать фото\n🔗 {text}")
                elif os.path.exists(output_file) and os.path.getsize(output_file) <= MAX_FILE_SIZE:
                    media_info = {
                        'author': info.get('uploader', info.get('channel', 'Неизвестный')),
                        'description': title,
                        'url': text
                    }
                    caption = format_caption(media_info, get_platform_name(text), "фото")
                    try:
                        with open(output_file, 'rb') as f:
                            await update.message.reply_photo(photo=f, caption=caption, parse_mode='HTML')
                    except Exception as e:
                        logger.error(f"Send photo error: {e}")
                        await update.message.reply_text(f"⚠️ Ошибка отправки фото: {str(e)[:100]}")
                    finally:
                        if os.path.exists(output_file):
                            os.remove(output_file)
                else:
                    await update.message.reply_text(f"❌ Не удалось скачать фото (возможно, большой размер)\n🔗 {text}")
                await status_msg.delete()
                return
            
            # Обработка видео
            safe_title = sanitize_filename(title)
            output_file = os.path.join(DOWNLOADS_DIR, f"{safe_title}.mp4")
            
            await status_msg.edit_text("⏳ Скачиваю видео...")
            
            if await download_video_ytdlp(text, output_file):
                media_info = {
                    'author': info.get('uploader', info.get('channel', 'Неизвестный')),
                    'description': title,
                    'url': text
                }
                caption = format_caption(media_info, get_platform_name(text), "видео")
                
                try:
                    with open(output_file, 'rb') as f:
                        await update.message.reply_video(
                            video=f,
                            caption=caption,
                            parse_mode='HTML',
                            supports_streaming=True
                        )
                except Exception as e:
                    logger.error(f"Send video error: {e}")
                    await update.message.reply_text(f"⚠️ Ошибка отправки видео: {str(e)[:100]}")
                finally:
                    if os.path.exists(output_file):
                        os.remove(output_file)
            else:
                await update.message.reply_text(f"❌ Не удалось скачать видео\n🔗 {text}")
            
            await status_msg.delete()
            
        except subprocess.TimeoutExpired:
            await update.message.reply_text("⏰ Превышено время ожидания")
            if 'status_msg' in locals():
                await status_msg.delete()
        except json.JSONDecodeError:
            await update.message.reply_text("❌ Ошибка обработки данных")
            if 'status_msg' in locals():
                await status_msg.delete()
        except Exception as e:
            logger.error(f"Processing error: {e}")
            await update.message.reply_text(f"⚠️ Ошибка: {str(e)[:100]}")
            if 'status_msg' in locals():
                await status_msg.delete()
                
    except Exception as e:
        logger.error(f"Handler error: {e}")
        await update.message.reply_text("⚠️ Произошла ошибка. Попробуйте позже.")

def cleanup_old_files():
    """Очистка старых файлов"""
    try:
        if not os.path.exists(DOWNLOADS_DIR):
            return
        current = time.time()
        for fname in os.listdir(DOWNLOADS_DIR):
            fpath = os.path.join(DOWNLOADS_DIR, fname)
            if os.path.isfile(fpath) and current - os.path.getmtime(fpath) > CLEANUP_HOURS * 3600:
                try:
                    os.remove(fpath)
                except:
                    pass
    except Exception:
        pass

def main():
    """Запуск бота"""
    # Создаём директорию для загрузок
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    cleanup_old_files()
    
    # Проверка yt-dlp (без выхода)
    if not is_ytdlp_available():
        print("\n" + "="*50)
        print("⚠️  ВНИМАНИЕ: yt-dlp не установлен!")
        print("="*50)
        print("Бот будет работать, но для скачивания видео нужно установить yt-dlp:")
        print("pip install yt-dlp")
        print("="*50 + "\n")
    else:
        print("\n" + "="*40)
        print("🤖 Бот Druid запущен")
        print("="*40)
        print(f"✅ Токен: {TOKEN[:10]}...")
        print(f"✅ yt-dlp: установлен")
        print(f"✅ Готов к работе")
        print("="*40 + "\n")
    
    # Настройка запросов
    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=60.0,
        write_timeout=60.0,
        pool_timeout=30.0
    )
    
    # Запуск бота
    application = Application.builder().token(TOKEN).request(request).build()
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Запуск с обработкой ошибок
    try:
        application.run_polling(drop_pending_updates=True)
    except Exception as e:
        print(f"❌ Ошибка запуска бота: {e}")
        print("Проверьте подключение к интернету и токен бота")

if __name__ == '__main__':
    main()
