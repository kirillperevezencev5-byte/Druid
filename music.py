#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Модуль для музыки (поиск через SoundCloud, Shazam, плейлисты) — без ffmpeg
"""

import re
import json
import asyncio
import tempfile
import secrets
from pathlib import Path
from datetime import datetime

import aiohttp
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

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
    """Безопасное форматирование длительности из int или float"""
    if not duration:
        return "?"
    try:
        dur = int(float(duration))
        return f"{dur//60}:{dur%60:02d}"
    except (ValueError, TypeError):
        return "?"

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
    except Exception:
        return None

# ---------- Поиск через SoundCloud (scsearch) ----------
async def search_tracks_soundcloud(query: str, max_results=5):
    """Ищет треки на SoundCloud через yt-dlp"""
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
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0 or not stdout:
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
            except:
                continue
        return results[:max_results]
    except Exception:
        return []

async def download_audio_from_url(url: str, output_path: Path):
    """Скачивает аудио без ffmpeg, только прямые форматы"""
    base_path = output_path.parent / output_path.stem
    cmd = [
        'yt-dlp',
        '-o', str(base_path),
        '--no-warnings',
        '--quiet',
        '--format', 'bestaudio[protocol!=m3u8][ext=m4a]/bestaudio[protocol!=m3u8][ext=webm]/bestaudio[protocol!=m3u8][ext=opus]/bestaudio[protocol!=m3u8][ext=mp3]',
        url
    ]
    try:
        proc = await asyncio.create_subprocess_exec(*cmd)
        await asyncio.wait_for(proc.wait(), timeout=120)
        if proc.returncode != 0:
            return None
        matches = list(output_path.parent.glob(f"{base_path.stem}.*"))
        if matches:
            return matches[0]
        return None
    except Exception:
        return None

# ---------- Плейлисты (JSON) ----------
PLAYLISTS_FILE = Path("playlists.json")

def load_playlists():
    if PLAYLISTS_FILE.exists():
        try:
            with open(PLAYLISTS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_playlists(data):
    try:
        with open(PLAYLISTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except:
        pass

async def add_to_playlist(user_id: int, track_info: dict):
    playlists = load_playlists()
    user_id_str = str(user_id)
    if user_id_str not in playlists:
        playlists[user_id_str] = []
    if not any(t['url'] == track_info['url'] for t in playlists[user_id_str]):
        track_info['added_at'] = datetime.now().isoformat()
        playlists[user_id_str].append(track_info)
        save_playlists(playlists)
        return True
    return False

async def remove_from_playlist(user_id: int, index: int):
    playlists = load_playlists()
    user_id_str = str(user_id)
    if user_id_str in playlists and 0 <= index < len(playlists[user_id_str]):
        removed = playlists[user_id_str].pop(index)
        save_playlists(playlists)
        return removed
    return None

async def get_playlist(user_id: int):
    playlists = load_playlists()
    return playlists.get(str(user_id), [])

# ---------- Отправка аудио с кнопкой "Добавить в плейлист" ----------
async def send_audio_with_add_button(update_or_query, context: ContextTypes.DEFAULT_TYPE,
                                      audio_file: Path, caption: str, track_info: dict):
    """Отправляет аудио с инлайн-кнопкой для добавления в плейлист"""
    # Определяем, откуда вызвано (из сообщения или из callback)
    if hasattr(update_or_query, 'message'):
        # Это Update
        target = update_or_query.message
    else:
        # Это CallbackQuery
        target = update_or_query.message
    
    if not target:
        return
    
    # Генерируем уникальный ID для этого трека
    track_id = secrets.token_hex(8)
    if 'temp_tracks' not in context.user_data:
        context.user_data['temp_tracks'] = {}
    context.user_data['temp_tracks'][track_id] = track_info

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить в плейлист", callback_data=f"add_track_{track_id}")]
    ])

    with open(audio_file, 'rb') as f:
        await target.reply_audio(
            audio=f,
            caption=caption,
            parse_mode='HTML',
            title=track_info.get('title', 'Аудио')[:50],
            reply_markup=keyboard
        )

# ---------- Обработчики команд ----------
async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = ' '.join(context.args).strip()
    if not query:
        query = context.user_data.get('pending_shazam_query', '')
        if not query:
            await update.message.reply_text("ℹ️ Используйте: /search <название трека>")
            return
    status_msg = await update.message.reply_text(f"🔎 Ищу на SoundCloud: {escape_html(query[:100])}...")
    results = await search_tracks_soundcloud(query, max_results=5)
    if not results:
        await status_msg.edit_text("❌ Ничего не найдено на SoundCloud. Попробуйте другой запрос.")
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
    user_id = update.effective_user.id
    playlist = await get_playlist(user_id)
    if not playlist:
        await update.message.reply_text("📭 Ваш плейлист пуст.")
        return
    text = "🎵 <b>Ваш плейлист</b>\n\n"
    for i, track in enumerate(playlist):
        title = escape_html(track.get('title', 'Без названия')[:60])
        dur_str = format_duration(track.get('duration'))
        text += f"{i+1}. {title} [{dur_str}]\n"
    text += "\nИспользуйте /play <номер> или /removefromplaylist <номер>"
    await update.message.reply_text(text, parse_mode='HTML')

async def add_to_playlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Эта команда оставлена для обратной совместимости, но рекомендуется использовать кнопку
    user_id = update.effective_user.id
    last_track = context.user_data.get('last_track')
    if not last_track:
        await update.message.reply_text("❌ Нет информации о последнем треке. Используйте кнопку под аудио.")
        return
    added = await add_to_playlist(user_id, last_track)
    if added:
        await update.message.reply_text(f"✅ Добавлено: {escape_html(last_track['title'][:50])}", parse_mode='HTML')
    else:
        await update.message.reply_text("⚠️ Трек уже в плейлисте.")

async def remove_from_playlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("ℹ️ /removefromplaylist 3")
        return
    index = int(args[0]) - 1
    user_id = update.effective_user.id
    removed = await remove_from_playlist(user_id, index)
    if removed:
        await update.message.reply_text(f"🗑️ Удалено: {escape_html(removed['title'][:50])}", parse_mode='HTML')
    else:
        await update.message.reply_text("❌ Неверный номер.")

async def play_from_playlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("ℹ️ /play 2")
        return
    index = int(args[0]) - 1
    user_id = update.effective_user.id
    playlist = await get_playlist(user_id)
    if index < 0 or index >= len(playlist):
        await update.message.reply_text("❌ Неверный номер.")
        return
    track = playlist[index]
    url = track['url']
    status_msg = await update.message.reply_text(f"🎵 Скачиваю: {escape_html(track['title'][:50])}...")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        out = tmp / f"{sanitize_filename(track['title'])}"
        result = await download_audio_from_url(url, out)
        if not result or not result.exists():
            await status_msg.edit_text("❌ Ошибка скачивания.")
            return
        caption = f"🎵 <b>{escape_html(track['title'][:100])}</b>\n📌 Из плейлиста"
        await send_audio_with_add_button(update, context, result, caption, track)
        await status_msg.delete()

async def select_track_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "cancel_search":
        await query.edit_message_text("❌ Поиск отменён.")
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
    await query.edit_message_text(f"⬇️ Скачиваю: {escape_html(selected['title'][:80])}...")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        out = tmp / f"{sanitize_filename(selected['title'])}"
        result = await download_audio_from_url(selected['url'], out)
        if not result or not result.exists():
            await query.edit_message_text("❌ Ошибка скачивания.")
            return
        # Сохраняем в last_track для обратной совместимости
        context.user_data['last_track'] = {
            'title': selected['title'],
            'url': selected['url'],
            'duration': selected['duration']
        }
        caption = f"🎵 <b>{escape_html(selected['title'][:100])}</b>"
        await send_audio_with_add_button(query, context, result, caption, selected)
        if query.message:
            try:
                await query.message.delete()
            except:
                pass

async def add_track_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик нажатия кнопки 'Добавить в плейлист'"""
    query = update.callback_query
    await query.answer()
    data = query.data
    if not data.startswith("add_track_"):
        return
    track_id = data.split("_", 2)[2]
    temp_tracks = context.user_data.get('temp_tracks')
    track_info = temp_tracks.get(track_id) if temp_tracks else None
    if not track_info:
        await query.edit_message_text("❌ Информация о треке устарела. Попробуйте скачать заново.")
        return
    user_id = update.effective_user.id
    added = await add_to_playlist(user_id, track_info)
    if added:
        await query.edit_message_text(f"✅ Добавлено в плейлист: {escape_html(track_info['title'][:50])}")
    else:
        await query.edit_message_text("⚠️ Трек уже есть в вашем плейлисте.")
    # Удаляем временные данные
    if temp_tracks:
        temp_tracks.pop(track_id, None)

async def handle_shazam_url(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, session):
    if not update.message:
        return
    status_msg = await update.message.reply_text("🔍 Анализирую Shazam...")
    shazam_info = await get_shazam_track_info(session, url)
    if not shazam_info:
        await status_msg.edit_text("❌ Не удалось распознать трек.")
        return
    title, artist = shazam_info
    query = f"{artist} {title}" if artist else title
    await status_msg.edit_text(f"🎵 Найден: {escape_html(title)} — {escape_html(artist)}\nИщу на SoundCloud...", parse_mode='HTML')
    results = await search_tracks_soundcloud(query, max_results=5)
    if not results:
        await status_msg.edit_text(f"❌ Ничего не найдено на SoundCloud. Попробуйте: /search {escape_html(query)}")
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
