#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Модуль для музыки (поиск через SoundCloud, Shazam, плейлисты)
С поддержкой локального кэширования треков
"""

import re
import json
import asyncio
import tempfile
import shutil
import secrets
from pathlib import Path
from typing import Optional, Dict, Any

import aiohttp
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
import logging

import database

logger = logging.getLogger(__name__)

# ---------- общие утилиты ----------
def escape_html(text: str) -> str:
    if not text:
        return ""
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

def sanitize_filename(title: str) -> str:
    title = re.sub(r'[\\/*?:"<>|]', "", title)
    if len(title) > 80:
        title = title[:80]
    title = title.replace(' ', '_').strip('.')
    return title or "media"

def format_duration(duration) -> str:
    if not duration:
        return "?"
    try:
        dur = int(float(duration))
        return f"{dur//60}:{dur%60:02d}"
    except (ValueError, TypeError):
        return "?"

def get_user_track_path(user_id: int, track_title: str, ext: str = ".mp3") -> Path:
    """Генерирует путь для сохранения трека пользователя"""
    safe_title = sanitize_filename(track_title)
    # Ограничиваем длину имени файла
    if len(safe_title) > 100:
        safe_title = safe_title[:100]
    filename = f"{user_id}_{safe_title}{ext}"
    return database.CACHE_DIR / filename

# ---------- Shazam ----------
async def get_shazam_track_info(session, url: str):
    try:
        async with session.get(url, timeout=10) as resp:
            if resp.status != 200:
                return None
            html = await resp.text()
            soup = BeautifulSoup(html, 'html.parser')
            title_tag = soup.find('meta', property='og:title')
            if title_tag:
                full_title = title_tag.get('content', '')
                if '·' in full_title:
                    parts = full_title.split('·')
                    title = parts[0].strip()
                    artist = parts[1].strip()
                else:
                    title = full_title
                    artist = ''
                return title, artist
            script = soup.find('script', type='application/ld+json')
            if script:
                data = json.loads(script.string)
                if isinstance(data, dict) and 'name' in data and 'byArtist' in data:
                    title = data['name']
                    artist = data['byArtist']['name'] if isinstance(data['byArtist'], dict) else str(data['byArtist'])
                    return title, artist
            return None
    except Exception as e:
        logger.error(f"Shazam parsing error: {e}")
        return None

# ---------- Поиск через SoundCloud ----------
async def search_tracks_soundcloud(query: str, max_results=5):
    search_query = f"scsearch{max_results}:{query}"
    cmd = [
        'yt-dlp',
        '--dump-json',
        '--no-warnings',
        '--quiet',
        '--skip-download',
        search_query
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0 or not stdout:
            stderr_decoded = stderr.decode() if stderr else ''
            logger.error(f"yt-dlp search error (code {proc.returncode}): {stderr_decoded[:200]}")
            return []
        results = []
        for line in stdout.decode().strip().split('\n'):
            if not line:
                continue
            try:
                info = json.loads(line)
                results.append({
                    'id': info.get('id'),
                    'title': info.get('title'),
                    'duration': info.get('duration', 0),
                    'url': info.get('webpage_url') or info.get('url'),
                    'uploader': info.get('uploader', '')
                })
            except json.JSONDecodeError as e:
                logger.error(f"JSON decode error: {e}")
                continue
        logger.info(f"Search for '{query}' returned {len(results)} results")
        return results[:max_results]
    except Exception as e:
        logger.error(f"Search error: {e}")
        return []

# ---------- Скачивание аудио ----------
async def download_audio_from_url(url: str, output_path: Path) -> Optional[Path]:
    """
    Скачивание аудио в форматах, поддерживаемых Telegram:
    m4a, ogg, mp3, opus (webm исключён)
    """
    base_path = output_path.parent / output_path.stem
    
    # Приоритет форматов: m4a -> ogg -> mp3 -> opus -> aac
    format_spec = (
        'bestaudio[protocol!=m3u8][ext=m4a]/'
        'bestaudio[protocol!=m3u8][ext=ogg]/'
        'bestaudio[protocol!=m3u8][ext=mp3]/'
        'bestaudio[protocol!=m3u8][ext=opus]/'
        'bestaudio[protocol!=m3u8][ext=aac]/'
        'bestaudio[protocol!=m3u8]'
    )
    
    cmd = [
        'yt-dlp',
        '-o', str(base_path),
        '--no-warnings',
        '--quiet',
        '--format', format_spec,
        url
    ]
    
    try:
        logger.info(f"Downloading audio from {url[:100]}...")
        proc = await asyncio.create_subprocess_exec(*cmd)
        await asyncio.wait_for(proc.wait(), timeout=120)
        
        if proc.returncode != 0:
            logger.error(f"Download failed with code {proc.returncode}")
            return None
        
        # Ищем скачанный файл
        matches = list(output_path.parent.glob(f"{base_path.stem}.*"))
        if not matches:
            logger.error(f"No file found for {base_path.stem}")
            return None
        
        downloaded_path = matches[0]
        
        # Проверка: если скачался webm - пробуем с другим форматом
        if downloaded_path.suffix.lower() == '.webm':
            logger.warning(f"Downloaded webm format, trying alternative...")
            
            # Альтернативный формат без webm
            alt_format_spec = (
                'bestaudio[protocol!=m3u8][ext=m4a]/'
                'bestaudio[protocol!=m3u8][ext=ogg]/'
                'bestaudio[protocol!=m3u8][ext=mp3]/'
                'bestaudio[protocol!=m3u8][ext=opus]'
            )
            
            cmd_alt = [
                'yt-dlp',
                '-o', str(base_path),
                '--no-warnings',
                '--quiet',
                '--format', alt_format_spec,
                url
            ]
            
            proc_alt = await asyncio.create_subprocess_exec(*cmd_alt)
            await asyncio.wait_for(proc_alt.wait(), timeout=120)
            
            if proc_alt.returncode == 0:
                matches_alt = list(output_path.parent.glob(f"{base_path.stem}.*"))
                if matches_alt and matches_alt[0].suffix.lower() != '.webm':
                    downloaded_path = matches_alt[0]
                    logger.info(f"Alternative download successful: {downloaded_path}")
                else:
                    logger.error("Alternative download failed or still webm")
                    return None
            else:
                logger.error("Alternative download failed")
                return None
        
        logger.info(f"Download successful: {downloaded_path} ({downloaded_path.stat().st_size} bytes)")
        return downloaded_path
        
    except asyncio.TimeoutError:
        logger.error(f"Download timeout for {url[:100]}")
        return None
    except Exception as e:
        logger.error(f"Download error: {e}")
        return None

# ---------- Сохранение трека в кэш ----------
async def download_and_cache_track(url: str, user_id: int, track_info: dict) -> Optional[Dict[str, Any]]:
    """
    Скачивает трек, сохраняет в кэш пользователя и возвращает информацию о файле.
    Если файл уже существует, возвращает его.
    """
    # Определяем расширение (пока mp3, потом можно определить из скачанного файла)
    ext = ".mp3"
    out_path = get_user_track_path(user_id, track_info['title'], ext)
    
    # Проверяем, есть ли уже файл в кэше
    if out_path.exists():
        logger.info(f"Track already cached: {out_path}")
        return {
            'path': out_path,
            'size': out_path.stat().st_size,
            'is_cached': True
        }
    
    # Скачиваем во временную папку
    tmpdir = tempfile.mkdtemp()
    try:
        tmp_out = Path(tmpdir) / f"{sanitize_filename(track_info['title'])}"
        downloaded = await download_audio_from_url(url, tmp_out)
        
        if not downloaded or not downloaded.exists():
            logger.error(f"Failed to download track: {track_info.get('title')}")
            return None
        
        # Определяем реальное расширение
        actual_ext = downloaded.suffix
        if actual_ext != ext:
            # Обновляем путь с правильным расширением
            out_path = get_user_track_path(user_id, track_info['title'], actual_ext)
        
        # Копируем в постоянное место
        shutil.copy2(downloaded, out_path)
        
        file_size = out_path.stat().st_size
        
        # Проверка лимита Telegram (50 MB)
        if file_size > 50 * 1024 * 1024:
            logger.warning(f"File too large: {file_size} bytes, removing")
            out_path.unlink()
            return None
        
        logger.info(f"Saved track to {out_path} ({file_size} bytes)")
        
        return {
            'path': out_path,
            'size': file_size,
            'is_cached': False
        }
        
    except Exception as e:
        logger.error(f"Error caching track: {e}")
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# ---------- Отправка аудио с кнопкой "Добавить в плейлист" ----------
SUPPORTED_AUDIO_EXT = {'.mp3', '.m4a', '.ogg', '.opus', '.wav', '.flac', '.aac'}

async def send_audio_with_add_button(update_or_query, context: ContextTypes.DEFAULT_TYPE,
                                      audio_path: Path, caption: str, track_info: dict):
    """Отправляет аудио с кнопкой для добавления в плейлист"""
    if hasattr(update_or_query, 'message'):
        target = update_or_query.message
    else:
        target = update_or_query.message
    
    if not target:
        logger.error("No target for send_audio_with_add_button")
        return

    # Проверка поддерживаемого расширения
    if audio_path.suffix.lower() not in SUPPORTED_AUDIO_EXT:
        logger.warning(f"Unsupported audio format {audio_path.suffix}, sending as document")
        try:
            with open(audio_path, 'rb') as f:
                await target.reply_document(
                    document=f,
                    filename=f"{track_info.get('title', 'audio')[:50]}{audio_path.suffix}",
                    caption=f"{caption}\n⚠️ Формат не поддерживается для встроенного воспроизведения",
                    parse_mode='HTML'
                )
            return
        except Exception as e:
            logger.error(f"Error sending as document: {e}")
            return

    # Генерируем временный ID для этого трека
    track_id = secrets.token_hex(8)
    if 'temp_tracks' not in context.user_data:
        context.user_data['temp_tracks'] = {}
    
    # Сохраняем информацию о треке для последующего добавления в плейлист
    context.user_data['temp_tracks'][track_id] = {
        'title': track_info.get('title'),
        'url': track_info.get('url'),
        'duration': track_info.get('duration'),
        'uploader': track_info.get('uploader', '')
    }
    
    # Сохраняем как last_track для команды /addtoplaylist
    context.user_data['last_track'] = track_info

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить в плейлист", callback_data=f"add_track_{track_id}")]
    ])

    try:
        with open(audio_path, 'rb') as f:
            await target.reply_audio(
                audio=f,
                caption=caption,
                parse_mode='HTML',
                title=track_info.get('title', 'Аудио')[:50],
                performer=track_info.get('uploader', '')[:50],
                reply_markup=keyboard
            )
        logger.info(f"Audio sent with add button: {track_info.get('title')[:50]}")
    except Exception as e:
        logger.error(f"Error sending audio: {e}")
        # Fallback: отправляем как документ
        try:
            with open(audio_path, 'rb') as f:
                await target.reply_document(
                    document=f,
                    filename=f"{track_info.get('title', 'audio')[:50]}{audio_path.suffix}",
                    caption=caption,
                    parse_mode='HTML'
                )
        except Exception as e2:
            logger.error(f"Error sending as fallback document: {e2}")

# ---------- Обработчики команд ----------
async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Поиск треков на SoundCloud"""
    query = ' '.join(context.args).strip()
    if not query:
        await update.message.reply_text("ℹ️ Используйте: /search <название трека>")
        return

    status_msg = await update.message.reply_text(f"🔎 Ищу на SoundCloud: {escape_html(query[:100])}...")
    results = await search_tracks_soundcloud(query, max_results=5)
    
    if not results:
        await status_msg.edit_text("❌ Ничего не найдено на SoundCloud.")
        return
    
    context.user_data['search_results'] = results
    keyboard = []
    for idx, track in enumerate(results):
        title = escape_html(track['title'][:60])
        dur_str = format_duration(track.get('duration'))
        button_text = f"{idx+1}. {title} [{dur_str}]"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"select_track_{idx}")])
    
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_search")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await status_msg.edit_text("🎵 Найдено несколько вариантов. Выберите:", reply_markup=reply_markup)

async def playlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет плейлист как набор аудиосообщений"""
    user_id = update.effective_user.id
    playlist = await database.get_user_playlist(user_id)
    
    if not playlist:
        await update.message.reply_text(
            "📭 Ваш плейлист пуст.\n\n"
            "Чтобы добавить трек:\n"
            "1. Найдите трек через /search\n"
            "2. Нажмите кнопку «Добавить в плейлист» под аудио"
        )
        return
    
    # Отправляем статистику
    stats = await database.get_playlist_stats(user_id)
    size_mb = stats['total_size'] / (1024 * 1024)
    
    await update.message.reply_text(
        f"🎵 <b>Ваш плейлист</b>\n"
        f"📊 Треков: {stats['count']}\n"
        f"💾 Общий размер: {size_mb:.1f} МБ\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"⬇️ Список треков:",
        parse_mode='HTML'
    )
    
    # Отправляем каждый трек
    for track in playlist:
        file_path = Path(track['file_path'])
        
        # Проверяем, существует ли файл
        if not file_path.exists():
            await update.message.reply_text(
                f"🔄 Файл для <b>{escape_html(track['title'][:50])}</b> потерян, пробую восстановить...",
                parse_mode='HTML'
            )
            
            # Пробуем восстановить
            cached = await download_and_cache_track(track['url'], user_id, track)
            if cached and cached['path'].exists():
                file_path = cached['path']
                # Обновляем путь в БД
                await database.update_track_file_path(track['id'], str(file_path))
            else:
                await update.message.reply_text(
                    f"❌ Не удалось восстановить <b>{escape_html(track['title'][:50])}</b>\n"
                    f"Рекомендуется удалить его из плейлиста",
                    parse_mode='HTML'
                )
                continue
        
        # Формируем подпись
        caption = (
            f"🎵 <b>{escape_html(track['title'][:100])}</b>\n"
            f"👤 {escape_html(track.get('uploader', 'Неизвестен')[:50])}\n"
            f"⏱ {format_duration(track.get('duration'))}"
        )
        
        # Кнопка удаления
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑 Удалить из плейлиста", callback_data=f"del_pl_{track['id']}")]
        ])
        
        try:
            with open(file_path, 'rb') as f:
                await update.message.reply_audio(
                    audio=f,
                    caption=caption,
                    parse_mode='HTML',
                    title=track['title'][:50],
                    performer=track.get('uploader', '')[:50],
                    reply_markup=keyboard
                )
            # Небольшая задержка между отправками
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Error sending playlist track {track['title']}: {e}")
            await update.message.reply_text(
                f"❌ Не удалось отправить: {escape_html(track['title'][:50])}",
                parse_mode='HTML'
            )

async def add_to_playlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Добавляет последний прослушанный трек в плейлист"""
    user_id = update.effective_user.id
    last_track = context.user_data.get('last_track')
    
    if not last_track:
        await update.message.reply_text(
            "❌ Нет информации о последнем треке.\n"
            "Используйте кнопку «Добавить в плейлист» под аудио."
        )
        return
    
    status_msg = await update.message.reply_text(f"💾 Сохраняю: {escape_html(last_track['title'][:50])}...")
    
    # Скачиваем и кэшируем трек
    cached = await download_and_cache_track(last_track['url'], user_id, last_track)
    
    if not cached:
        await status_msg.edit_text("❌ Не удалось сохранить трек. Попробуйте позже.")
        return
    
    # Добавляем в БД
    added = await database.add_track_to_playlist(
        user_id, 
        last_track, 
        str(cached['path']), 
        cached['size']
    )
    
    if added:
        await status_msg.edit_text(f"✅ Добавлено в плейлист: {escape_html(last_track['title'][:50])}")
    else:
        await status_msg.edit_text("⚠️ Трек уже есть в вашем плейлисте")

# ---------- Callback обработчики ----------
async def select_track_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик выбора трека из результатов поиска"""
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == "cancel_search":
        await query.edit_message_text("❌ Поиск отменён.")
        context.user_data.pop('search_results', None)
        return
    
    if not data.startswith("select_track_"):
        return
    
    try:
        idx = int(data.split("_")[-1])
    except:
        await query.edit_message_text("❌ Ошибка: неверный формат.")
        return
    
    results = context.user_data.get('search_results', [])
    if idx >= len(results):
        await query.edit_message_text("❌ Ошибка: вариант не найден.")
        return
    
    selected = results[idx]
    
    logger.info(f"Selected track from search: {selected.get('title')}")
    
    await query.edit_message_text(f"⬇️ Скачиваю: {escape_html(selected['title'][:80])}...")
    
    # Скачиваем во временную папку
    tmpdir = tempfile.mkdtemp()
    try:
        out = Path(tmpdir) / f"{sanitize_filename(selected['title'])}"
        result = await download_audio_from_url(selected['url'], out)
        
        if not result or not result.exists():
            logger.error(f"Download failed for selected track: {selected.get('title')}")
            await query.edit_message_text("❌ Ошибка скачивания.")
            return
        
        logger.info(f"Download complete: {result} ({result.stat().st_size} bytes)")
        
        # Сохраняем информацию о треке
        track_info = {
            'title': selected['title'],
            'url': selected['url'],
            'duration': selected['duration'],
            'uploader': selected.get('uploader', '')
        }
        
        context.user_data['last_track'] = track_info
        
        caption = f"🎵 <b>{escape_html(selected['title'][:100])}</b>"
        await send_audio_with_add_button(query, context, result, caption, track_info)
        
        # Удаляем сообщение с выбором
        if query.message:
            try:
                await query.message.delete()
            except:
                pass
                
    except Exception as e:
        logger.error(f"Error in select_track_callback: {e}")
        await query.edit_message_text("❌ Произошла ошибка.")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    
    # Очищаем временные данные поиска
    context.user_data.pop('search_results', None)

async def add_track_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback при нажатии 'Добавить в плейлист' — скачиваем и сохраняем файл"""
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if not data.startswith("add_track_"):
        return
    
    track_id = data.split("_", 2)[2]
    
    # Проверяем наличие временных треков
    if 'temp_tracks' not in context.user_data:
        await query.answer("❌ Ошибка: нет данных о треке", show_alert=True)
        return
    
    temp_tracks = context.user_data.get('temp_tracks', {})
    track_info = temp_tracks.get(track_id)
    
    if not track_info:
        await query.answer("❌ Информация о треке устарела. Найдите трек заново.", show_alert=True)
        return
    
    if not track_info.get('url'):
        await query.answer("❌ Ошибка: у трека нет URL", show_alert=True)
        return
    
    user_id = update.effective_user.id
    
    # Показываем статус
    status_msg = await query.message.reply_text(f"💾 Сохраняю в плейлист: {escape_html(track_info['title'][:50])}...")
    
    # Скачиваем и кэшируем трек
    cached = await download_and_cache_track(track_info['url'], user_id, track_info)
    
    if not cached:
        await status_msg.edit_text("❌ Не удалось сохранить трек. Попробуйте позже.")
        return
    
    # Добавляем в базу данных
    added = await database.add_track_to_playlist(user_id, track_info, str(cached['path']), cached['size'])
    
    if added:
        await status_msg.edit_text(f"✅ Добавлено в плейлист: {track_info['title'][:50]}")
        # Убираем кнопку у сообщения с аудио
        try:
            new_markup = InlineKeyboardMarkup([])
            await query.edit_message_reply_markup(reply_markup=new_markup)
        except Exception as e:
            logger.error(f"Error removing button: {e}")
    else:
        await status_msg.edit_text("⚠️ Трек уже есть в вашем плейлисте")
    
    # Удаляем временный трек
    temp_tracks.pop(track_id, None)
    if not temp_tracks:
        context.user_data.pop('temp_tracks', None)

async def remove_from_playlist_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаляет трек из плейлиста и стирает файл"""
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if not data.startswith("del_pl_"):
        return
    
    track_id = int(data.split("_")[2])
    user_id = update.effective_user.id
    
    # Удаляем из БД
    removed = await database.remove_track_from_playlist_by_id(user_id, track_id)
    
    if removed:
        # Удаляем локальный файл
        file_path = Path(removed['file_path'])
        if file_path.exists():
            file_path.unlink()
            logger.info(f"Deleted file: {file_path}")
        
        # Обновляем сообщение
        try:
            await query.edit_message_caption(
                caption=f"🗑 <b>Удалено из плейлиста:</b>\n{escape_html(removed['title'][:100])}",
                parse_mode='HTML'
            )
        except:
            pass
        
        await query.answer("Трек удалён из плейлиста")
    else:
        await query.answer("Ошибка: трек не найден", show_alert=True)

async def handle_shazam_url(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, session):
    """Обработка Shazam ссылок"""
    if not update.message:
        return
    
    status_msg = await update.message.reply_text("🔍 Анализирую Shazam...")
    shazam_info = await get_shazam_track_info(session, url)
    
    if not shazam_info:
        await status_msg.edit_text("❌ Не удалось распознать трек.")
        return
    
    title, artist = shazam_info
    query = f"{artist} {title}" if artist else title
    
    await status_msg.edit_text(
        f"🎵 Найден: {escape_html(title)} — {escape_html(artist)}\nИщу на SoundCloud...",
        parse_mode='HTML'
    )
    
    results = await search_tracks_soundcloud(query, max_results=5)
    
    if not results:
        await status_msg.edit_text(
            f"❌ Ничего не найдено на SoundCloud.\nПопробуйте: /search {escape_html(query)}"
        )
        return
    
    context.user_data['search_results'] = results
    keyboard = []
    for idx, track in enumerate(results):
        title_short = escape_html(track['title'][:60])
        dur_str = format_duration(track.get('duration'))
        keyboard.append([InlineKeyboardButton(f"{idx+1}. {title_short} [{dur_str}]", callback_data=f"select_track_{idx}")])
    
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_search")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await status_msg.edit_text("🔽 Выберите вариант на SoundCloud:", reply_markup=reply_markup)
