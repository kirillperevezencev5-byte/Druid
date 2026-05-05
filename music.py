#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Модуль для музыки (поиск через SoundCloud, Shazam, плейлисты) – без ffmpeg
Хранилище плейлистов перенесено в SQLite (database)
"""

import re
import json
import asyncio
import tempfile
import secrets
from pathlib import Path

import aiohttp
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
import logging

import database

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
    base_path = output_path.parent / output_path.stem
    cmd = [
        'yt-dlp',
        '-o', str(base_path),
        '--no-warnings',
        '--quiet',
        '--format',
        'bestaudio[protocol!=m3u8][ext=m4a]/bestaudio[protocol!=m3u8][ext=webm]/bestaudio[protocol!=m3u8][ext=opus]/bestaudio[protocol!=m3u8][ext=mp3]',
        url
    ]
    try:
        proc = await asyncio.create_subprocess_exec(*cmd)
        await asyncio.wait_for(proc.wait(), timeout=120)
        if proc.returncode != 0:
            return None
        matches = list(output_path.parent.glob(f"{base_path.stem}.*"))
        return matches[0] if matches else None
    except Exception:
        return None

# ---------- Отправка аудио с кнопкой "Добавить в плейлист" ----------
async def send_audio_with_add_button(update_or_query, context: ContextTypes.DEFAULT_TYPE,
                                      audio_path: Path, caption: str, track_info: dict):
    """Отправляет аудио с кнопкой и временным токеном для callback"""
    if hasattr(update_or_query, 'message'):
        target = update_or_query.message
    else:
        target = update_or_query.message
    if not target:
        return

    track_id = secrets.token_hex(8)
    if 'temp_tracks' not in context.user_data:
        context.user_data['temp_tracks'] = {}
    context.user_data['temp_tracks'][track_id] = track_info

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить в плейлист", callback_data=f"add_track_{track_id}")]
    ])

    with open(audio_path, 'rb') as f:
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
    user_id = update.effective_user.id
    playlist = await database.get_user_playlist(user_id)
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
    user_id = update.effective_user.id
    last_track = context.user_data.get('last_track')
    if not last_track:
        await update.message.reply_text("❌ Нет информации о последнем треке. Используйте кнопку под аудио.")
        return
    added = await database.add_track_to_playlist(user_id, last_track)
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
    removed = await database.remove_track_from_playlist(user_id, index)
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
    playlist = await database.get_user_playlist(user_id)
    if index < 0 or index >= len(playlist):
        await update.message.reply_text("❌ Неверный номер.")
        return
    track = playlist[index]
    url = track['url']
    status_msg = await update.message.reply_text(f"🎵 Скачиваю: {escape_html(track['title'][:50])}...")
    tmpdir = tempfile.mkdtemp()
    try:
        out = Path(tmpdir) / f"{sanitize_filename(track['title'])}"
        result = await download_audio_from_url(url, out)
        if not result or not result.exists():
            await status_msg.edit_text("❌ Ошибка скачивания.")
            return
        caption = f"🎵 <b>{escape_html(track['title'][:100])}</b>\n📌 Из плейлиста"
        await send_audio_with_add_button(update, context, result, caption, track)
        await status_msg.delete()
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

async def select_track_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    await query.edit_message_text(f"⬇️ Скачиваю: {escape_html(selected['title'][:80])}...")
    tmpdir = tempfile.mkdtemp()
    try:
        out = Path(tmpdir) / f"{sanitize_filename(selected['title'])}"
        result = await download_audio_from_url(selected['url'], out)
        if not result or not result.exists():
            await query.edit_message_text("❌ Ошибка скачивания.")
            return
        context.user_data['last_track'] = {
            'title': selected['title'],
            'url': selected['url'],
            'duration': selected['duration'],
            'uploader': selected.get('uploader', '')
        }
        caption = f"🎵 <b>{escape_html(selected['title'][:100])}</b>"
        await send_audio_with_add_button(query, context, result, caption, selected)
        if query.message:
            try:
                await query.message.delete()
            except:
                pass
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
    # Очищаем временные данные поиска
    context.user_data.pop('search_results', None)

async def add_track_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if not data.startswith("add_track_"):
        return
    track_id = data.split("_", 2)[2]
    temp_tracks = context.user_data.get('temp_tracks')
    track_info = temp_tracks.get(track_id) if temp_tracks else None
    if not track_info:
        await query.answer("Информация о треке устарела. Скачайте заново.", show_alert=True)
        return
    user_id = update.effective_user.id
    added = await database.add_track_to_playlist(user_id, track_info)
    if added:
        # Меняем только клавиатуру, оставляя оригинальное аудиосообщение
        try:
            # Убираем кнопку
            new_markup = InlineKeyboardMarkup([])  # пустая клавиатура
            await query.edit_message_reply_markup(reply_markup=new_markup)
            await query.answer(f"✅ Добавлено: {track_info['title'][:50]}", show_alert=True)
        except Exception:
            await query.answer(f"✅ Добавлено (но кнопка осталась)", show_alert=True)
    else:
        await query.answer("⚠️ Трек уже в вашем плейлисте.", show_alert=True)
    # Удаляем временный трек
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
