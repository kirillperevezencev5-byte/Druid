#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Модуль для работы с музыкой: поиск, Shazam, плейлисты
"""

import re
import json
import asyncio
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

# Импортируем вспомогательные функции из основного бота (они уже есть в druid.py)
# Чтобы избежать циклического импорта, мы продублируем некоторые утилиты или передадим их через параметры.
# Проще скопировать сюда необходимые функции: sanitize_filename, escape_html, send_with_split, format_caption
# Но чтобы не дублировать код, мы можем импортировать их из druid.py после того, как модуль будет подключён.
# Однако druid.py будет импортировать music.py, и если music.py попытается импортировать druid.py – будет цикл.
# Поэтому лучше вынести общие утилиты в отдельный файл utils.py, но для простоты продублируем здесь необходимые мини-функции.

def sanitize_filename(title: str) -> str:
    title = re.sub(r'[\\/*?:"<>|]', "", title)
    if len(title) > 80:
        title = title[:80]
    title = title.replace(' ', '_').strip('.')
    return title or "media"

def escape_html(text: str) -> str:
    if not text:
        return ""
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

# Эта функция будет передана из основного бота, чтобы не дублировать код отправки.
# Но мы пока оставим заглушку, а в основном боте будем передавать ссылку на функцию.
# Проще сделать так: в music.py определим класс MusicManager, который при инициализации получит ссылку на send_with_split.
# Но для быстрой демонстрации я продублирую простую версию, использующую update.message.reply_audio.

async def send_audio_simple(update, file_path, caption):
    """Отправка аудио с разделением, если файл большой"""
    # Здесь можно использовать ту же логику, что и в druid.py: send_with_split
    # Но чтобы не копировать много кода, вызовем функцию из глобального контекста.
    # Лучше передать функцию через менеджер.
    pass

# ---------------------- Shazam ----------------------
async def get_shazam_track_info(session, url: str):
    """Парсит страницу Shazam, возвращает (название, исполнитель)"""
    try:
        async with session.get(url, timeout=10) as resp:
            if resp.status != 200:
                return None
            html = await resp.text()
            soup = BeautifulSoup(html, 'html.parser')
            # Пробуем несколько способов
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
            # Альтернативный вариант: ищем в JSON-LD
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

# ---------------------- Поиск через yt-dlp ----------------------
async def search_tracks(query: str, max_results=5):
    """
    Ищет треки на YouTube по запросу.
    Возвращает список словарей: [{'id': 'video_id', 'title': '...', 'duration': секунды, 'url': '...'}]
    """
    search_query = f"ytsearch{max_results}:{query}"
    cmd = ['yt-dlp', '--dump-json', '--no-warnings', '--quiet', '--skip-download', search_query]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
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
                    'url': info.get('webpage_url') or f"https://youtube.com/watch?v={info.get('id')}"
                })
            except:
                continue
        return results[:max_results]
    except Exception:
        return []

async def download_audio_from_url(url: str, output_path: Path):
    """Скачивает аудио с YouTube или другого сайта через yt-dlp"""
    cmd = ['yt-dlp', '-o', str(output_path), '--no-warnings', '--quiet', '-f', 'bestaudio', '--extract-audio', '--audio-format', 'mp3', url]
    try:
        proc = await asyncio.create_subprocess_exec(*cmd)
        await asyncio.wait_for(proc.wait(), timeout=120)
        if proc.returncode != 0:
            return None
        # yt-dlp может добавить расширение .mp3
        if output_path.exists():
            return output_path
        # Ищем файл с таким же именем, но с .mp3
        mp3_path = output_path.with_suffix('.mp3')
        if mp3_path.exists():
            return mp3_path
        return None
    except Exception:
        return None

# ---------------------- Плейлисты (хранилище) ----------------------
# Для простоты используем JSON-файл: playlists.json
# Структура: { user_id: [ {'title': str, 'url': str, 'duration': int, 'added_at': str} ] }

PLAYLISTS_FILE = Path("playlists.json")

def load_playlists():
    if PLAYLISTS_FILE.exists():
        with open(PLAYLISTS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_playlists(data):
    with open(PLAYLISTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

async def add_to_playlist(user_id: int, track_info: dict):
    playlists = load_playlists()
    user_id_str = str(user_id)
    if user_id_str not in playlists:
        playlists[user_id_str] = []
    # Добавляем, если ещё нет такого url
    if not any(t['url'] == track_info['url'] for t in playlists[user_id_str]):
        track_info['added_at'] = asyncio.get_event_loop().time()  # или datetime
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

# ---------------------- Обработчики команд ----------------------
async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик /search <запрос>"""
    query = ' '.join(context.args).strip()
    if not query:
        # Возможно, есть сохранённый запрос из Shazam
        query = context.user_data.get('pending_shazam_query', '')
        if not query:
            await update.message.reply_text("ℹ️ Используйте: /search <название трека> [исполнитель]")
            return
    # Выполняем поиск
    status_msg = await update.message.reply_text(f"🔎 Ищу: {query[:100]}...")
    results = await search_tracks(query, max_results=5)
    if not results:
        await status_msg.edit_text("❌ Ничего не найдено. Попробуйте другой запрос.")
        return
    # Сохраняем результаты в user_data для последующего выбора
    context.user_data['search_results'] = results
    # Формируем клавиатуру с вариантами
    keyboard = []
    for idx, track in enumerate(results):
        title = track['title'][:60]
        dur = track['duration']
        dur_str = f"{dur//60}:{dur%60:02d}" if dur else "?"
        button_text = f"{idx+1}. {title} [{dur_str}]"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"select_track_{idx}")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_search")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await status_msg.edit_text("🎵 Найдено несколько вариантов. Выберите:", reply_markup=reply_markup)

async def playlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать плейлист пользователя"""
    user_id = update.effective_user.id
    playlist = await get_playlist(user_id)
    if not playlist:
        await update.message.reply_text("📭 Ваш плейлист пуст. Добавьте треки через /search или /addtoplaylist после загрузки.")
        return
    text = "🎵 <b>Ваш плейлист</b>\n\n"
    for i, track in enumerate(playlist):
        title = track.get('title', 'Без названия')
        dur = track.get('duration', 0)
        dur_str = f"{dur//60}:{dur%60:02d}" if dur else "?"
        text += f"{i+1}. {escape_html(title[:60])} [{dur_str}]\n"
    text += "\nИспользуйте /play <номер> чтобы прослушать трек.\n/removefromplaylist <номер> чтобы удалить."
    await update.message.reply_text(text, parse_mode='HTML')

async def add_to_playlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Добавить текущий трек в плейлист (должен быть сохранён в user_data['last_track'])"""
    user_id = update.effective_user.id
    last_track = context.user_data.get('last_track')
    if not last_track:
        await update.message.reply_text("❌ Нет информации о последнем треке. Сначала скачайте что-нибудь через /search или ссылку.")
        return
    added = await add_to_playlist(user_id, last_track)
    if added:
        await update.message.reply_text(f"✅ Добавлено в плейлист: {escape_html(last_track['title'][:50])}", parse_mode='HTML')
    else:
        await update.message.reply_text("⚠️ Этот трек уже есть в вашем плейлисте.")

async def remove_from_playlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/removefromplaylist <номер>"""
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("ℹ️ Укажите номер трека: /removefromplaylist 3")
        return
    index = int(args[0]) - 1
    user_id = update.effective_user.id
    removed = await remove_from_playlist(user_id, index)
    if removed:
        await update.message.reply_text(f"🗑️ Удалено: {escape_html(removed['title'][:50])}", parse_mode='HTML')
    else:
        await update.message.reply_text("❌ Неверный номер.")

async def play_from_playlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/play <номер> - скачать и отправить трек из плейлиста"""
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("ℹ️ Укажите номер трека: /play 2")
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
        out = tmp / f"{sanitize_filename(track['title'])}.mp3"
        result = await download_audio_from_url(url, out)
        if not result or not result.exists():
            await status_msg.edit_text("❌ Ошибка скачивания трека.")
            return
        # Отправляем аудио
        caption = f"🎵 <b>{escape_html(track['title'][:100])}</b>\n📌 Из вашего плейлиста"
        with open(result, 'rb') as f:
            await update.message.reply_audio(audio=f, caption=caption, parse_mode='HTML', title=track['title'][:50])
        await status_msg.delete()

async def handle_shazam_url(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, session):
    """Обработка Shazam ссылки: парсим, ищем, предлагаем результаты"""
    status_msg = await update.message.reply_text("🔍 Анализирую ссылку Shazam...")
    shazam_info = await get_shazam_track_info(session, url)
    if not shazam_info:
        await status_msg.edit_text("❌ Не удалось распознать трек. Попробуйте вручную через /search")
        return
    title, artist = shazam_info
    query = f"{artist} {title}" if artist else title
    context.user_data['pending_shazam_query'] = query
    await status_msg.edit_text(f"🎵 Найден трек: <b>{escape_html(title)}</b> — {escape_html(artist)}\nИщу варианты...", parse_mode='HTML')
    # Запускаем поиск
    results = await search_tracks(query, max_results=5)
    if not results:
        await status_msg.edit_text("❌ Ничего не найдено. Попробуйте поискать вручную: /search " + query)
        return
    context.user_data['search_results'] = results
    keyboard = []
    for idx, track in enumerate(results):
        title_short = track['title'][:60]
        dur = track['duration']
        dur_str = f"{dur//60}:{dur%60:02d}" if dur else "?"
        keyboard.append([InlineKeyboardButton(f"{idx+1}. {title_short} [{dur_str}]", callback_data=f"select_track_{idx}")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_search")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await status_msg.edit_text("🔽 Выберите вариант для скачивания:", reply_markup=reply_markup)

async def select_track_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатия кнопки выбора трека"""
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "cancel_search":
        await query.edit_message_text("❌ Поиск отменён.")
        return
    if not data.startswith("select_track_"):
        return
    idx = int(data.split("_")[-1])
    results = context.user_data.get('search_results', [])
    if idx >= len(results):
        await query.edit_message_text("❌ Ошибка: вариант не найден.")
        return
    selected = results[idx]
    # Скачиваем
    await query.edit_message_text(f"⬇️ Скачиваю: {escape_html(selected['title'][:80])}...")
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        out = tmp / f"{sanitize_filename(selected['title'])}.mp3"
        result = await download_audio_from_url(selected['url'], out)
        if not result or not result.exists():
            await query.edit_message_text("❌ Ошибка скачивания.")
            return
        # Сохраняем информацию о треке для возможного добавления в плейлист
        context.user_data['last_track'] = {
            'title': selected['title'],
            'url': selected['url'],
            'duration': selected['duration']
        }
        caption = f"🎵 <b>{escape_html(selected['title'][:100])}</b>\n\n"
        caption += "➕ Чтобы добавить в плейлист, введите /addtoplaylist"
        with open(result, 'rb') as f:
            await query.message.reply_audio(audio=f, caption=caption, parse_mode='HTML', title=selected['title'][:50])
        await query.message.delete()  # удаляем сообщение с кнопками
