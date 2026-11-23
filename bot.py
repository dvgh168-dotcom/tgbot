import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from collections import OrderedDict
from datetime import date

import aiohttp
import yt_dlp
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputMediaAudio,
    InputMediaPhoto,
    InputMediaVideo,
    InputTextMessageContent,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
STORAGE_CHAT_ID = int(os.getenv("STORAGE_CHAT_ID", "0"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

DATA_DIR = "data"
STATE_FILE = os.path.join(DATA_DIR, "state.json")

# Ссылки: TikTok, Pinterest, X (Twitter), SoundCloud
MEDIA_RE = re.compile(
    r"https?://(?:www\.|m\.|vm\.|vt\.|mobile\.|on\.)?"
    r"(?:tiktok\.com|pinterest\.[a-z.]+|pin\.it|twitter\.com|x\.com|soundcloud\.com)/\S+",
    re.IGNORECASE,
)

DOWNLOAD_SEMAPHORE = asyncio.Semaphore(4)
SEARCH_SEMAPHORE = asyncio.Semaphore(5)
STATE_LOCK = asyncio.Lock()
BOT_USERNAME = ""

# Кэш найденных треков SoundCloud: key -> url (храним последние 1000 запросов)
SEARCH_CACHE: OrderedDict[str, str] = OrderedDict()

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())


class AdminStates(StatesGroup):
    waiting_ad_text = State()
    waiting_ad_button = State()
    waiting_broadcast = State()
    waiting_custom_text = State()
    waiting_custom_icon = State()


DEFAULT_TEXTS = {
    "start_text": (
        "👋 Привет! Я скачиваю видео, фото и музыку без водяных знаков.\n\n"
        "<b>Поддерживаются:</b> TikTok, Pinterest (видео и фото), X (Twitter) и SoundCloud.\n\n"
        "• Отправь ссылку или название трека прямо сюда\n"
        "• Или введи <code>@{username} ссылка / название трека</code> в любом чате!"
    ),
    "loading_text": "⏳ Загрузка...",
    "hint_title": "👋 Вставьте ссылку или название трека",
    "hint_desc": "TikTok, Pinterest, X и музыка из SoundCloud",
    "hint_click_text": "Вставьте ссылку на видео/фото или напишите название трека после @{username}",
    "title_tiktok": "💌 Поделиться видео из TikTok",
    "title_pinterest": "📌 Поделиться из Pinterest",
    "title_x": "🎬 Поделиться видео из X",
    "title_soundcloud": "🎵 Поделиться треком из SoundCloud",
}

DEFAULT_ICONS = {
    "tiktok": "https://www.google.com/s2/favicons?domain=tiktok.com&sz=256",
    "pinterest": "https://www.google.com/s2/favicons?domain=pinterest.com&sz=256",
    "x": "https://www.google.com/s2/favicons?domain=x.com&sz=256",
    "soundcloud": "https://www.google.com/s2/favicons?domain=soundcloud.com&sz=256",
    "hint": "https://cdn-icons-png.flaticon.com/512/1828/1828919.png",
}

TEXT_LABELS = {
    "start_text": "👋 Приветствие (/start)",
    "loading_text": "⏳ Заглушка загрузки",
    "hint_title": "💡 Подсказка: заголовок",
    "hint_desc": "💡 Подсказка: описание",
    "hint_click_text": "💡 Подсказка: при нажатии",
    "title_tiktok": "💌 Карточка TikTok",
    "title_pinterest": "📌 Карточка Pinterest",
    "title_x": "🎬 Карточка X (Twitter)",
    "title_soundcloud": "🎵 Карточка SoundCloud",
}

ICON_LABELS = {
    "tiktok": "TikTok",
    "pinterest": "Pinterest",
    "x": "X (Twitter)",
    "soundcloud": "SoundCloud",
    "hint": "Подсказка (пустой ввод)",
}

DEFAULT_STATE = {
    "ad_enabled": True,
    "ad_text": "🔥 Скачано через нашего бота",
    "ad_btn_text": "",
    "ad_btn_url": "",
    "texts": dict(DEFAULT_TEXTS),
    "icons": dict(DEFAULT_ICONS),
    "stats": {
        "total_inline": 0,
        "total_dm": 0,
        "today_date": str(date.today()),
        "today_inline": 0,
        "today_dm": 0,
        "today_users": [],
        "ad_impressions": 0,
    },
    "users": [],
    "all_users": [],
}


def load_state_sync() -> dict:
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(STATE_FILE):
        return json.loads(json.dumps(DEFAULT_STATE))
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in DEFAULT_STATE.items():
            if k not in data:
                data[k] = json.loads(json.dumps(v))
        for k, v in DEFAULT_STATE["stats"].items():
            if k not in data["stats"]:
                data["stats"][k] = json.loads(json.dumps(v))
        for k, v in DEFAULT_TEXTS.items():
            if k not in data["texts"]:
                data["texts"][k] = v
        for k, v in DEFAULT_ICONS.items():
            if k not in data["icons"]:
                data["icons"][k] = v
        if not data["all_users"] and data["users"]:
            data["all_users"] = list(data["users"])
        return data
    except Exception:
        return json.loads(json.dumps(DEFAULT_STATE))


def save_state_sync(data: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp_path = STATE_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, STATE_FILE)


STATE = load_state_sync()


def _refresh_today():
    today = str(date.today())
    if STATE["stats"]["today_date"] != today:
        STATE["stats"]["today_date"] = today
        STATE["stats"]["today_inline"] = 0
        STATE["stats"]["today_dm"] = 0
        STATE["stats"]["today_users"] = []


async def save_state():
    async with STATE_LOCK:
        _refresh_today()
        await asyncio.to_thread(save_state_sync, STATE)


async def register_user(user_id: int, is_dm: bool = False):
    _refresh_today()
    changed = False
    if is_dm and user_id not in STATE["users"]:
        STATE["users"].append(user_id)
        changed = True
    if user_id not in STATE["all_users"]:
        STATE["all_users"].append(user_id)
        changed = True
    if user_id not in STATE["stats"]["today_users"]:
        STATE["stats"]["today_users"].append(user_id)
        changed = True
    if changed:
        await save_state()


async def record_download(source: str, ad_shown: bool, user_id: int | None = None):
    _refresh_today()
    if source == "inline":
        STATE["stats"]["total_inline"] += 1
        STATE["stats"]["today_inline"] += 1
    else:
        STATE["stats"]["total_dm"] += 1
        STATE["stats"]["today_dm"] += 1
    if ad_shown:
        STATE["stats"]["ad_impressions"] += 1
    if user_id:
        if user_id not in STATE["all_users"]:
            STATE["all_users"].append(user_id)
        if user_id not in STATE["stats"]["today_users"]:
            STATE["stats"]["today_users"].append(user_id)
    await save_state()


def cache_search_url(url: str) -> str:
    key = uuid.uuid4().hex[:12]
    SEARCH_CACHE[key] = url
    if len(SEARCH_CACHE) > 1000:
        SEARCH_CACHE.popitem(last=False)
    return key


def format_duration(seconds) -> str:
    if not seconds:
        return ""
    try:
        total = int(seconds)
        m, s = divmod(total, 60)
        return f"{m}:{s:02d}"
    except Exception:
        return ""


def get_ad_payload() -> tuple[str | None, InlineKeyboardMarkup | None, bool]:
    if not STATE["ad_enabled"]:
        return None, None, False
    caption = STATE["ad_text"] or None
    kb = None
    if STATE["ad_btn_text"] and STATE["ad_btn_url"]:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=STATE["ad_btn_text"], url=STATE["ad_btn_url"])]
            ]
        )
    ad_shown = bool(caption or kb)
    return caption, kb, ad_shown


def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID


def is_soundcloud_url(url: str) -> bool:
    return "soundcloud.com" in url.lower()


def is_pinterest_url(url: str) -> bool:
    lower = url.lower()
    return "pinterest." in lower or "pin.it" in lower


def detect_platform_ui(url: str) -> tuple[str, str]:
    lower = url.lower()
    texts = STATE["texts"]
    icons = STATE["icons"]
    if "soundcloud.com" in lower:
        return texts["title_soundcloud"], icons["soundcloud"]
    if "pinterest." in lower or "pin.it" in lower:
        return texts["title_pinterest"], icons["pinterest"]
    if "x.com" in lower or "twitter.com" in lower:
        return texts["title_x"], icons["x"]
    return texts["title_tiktok"], icons["tiktok"]


# ==================== МЕНЮ АДМИН-ПАНЕЛИ ====================

def admin_menu_text() -> str:
    _refresh_today()
    st = STATE["stats"]
    today_total = st["today_inline"] + st["today_dm"]
    all_total = st["total_inline"] + st["total_dm"]
    status = "🟢 ВКЛ" if STATE["ad_enabled"] else "🔴 ВЫКЛ"
    btn_info = (
        f"{STATE['ad_btn_text']} ({STATE['ad_btn_url']})"
        if STATE["ad_btn_text"] and STATE["ad_btn_url"]
        else "отключена"
    )
    return (
        f"⚙️ <b>Админ-панель</b>\n\n"
        f"👥 <b>Всего уникальных пользователей:</b> {len(STATE['all_users'])}\n"
        f"🔥 <b>Активных за сегодня:</b> {len(st['today_users'])}\n"
        f"📬 <b>В личке (для рассылки):</b> {len(STATE['users'])}\n\n"
        f"📊 <b>Скачиваний сегодня:</b> {today_total} "
        f"<i>(чаты: {st['today_inline']} | личка: {st['today_dm']})</i>\n"
        f"📈 <b>За всё время:</b> {all_total} "
        f"<i>(чаты: {st['total_inline']} | личка: {st['total_dm']})</i>\n"
        f"👁 <b>Показов рекламы:</b> {st['ad_impressions']}\n\n"
        f"📢 <b>Реклама:</b> {status}\n"
        f"🔗 <b>Кнопка:</b> {btn_info}"
    )


def admin_menu_kb() -> InlineKeyboardMarkup:
    toggle_text = "🔴 Выключить рекламу" if STATE["ad_enabled"] else "🟢 Включить рекламу"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=toggle_text, callback_data="adm:toggle")],
            [
                InlineKeyboardButton(text="✏️ Текст рекламы", callback_data="adm:text"),
                InlineKeyboardButton(text="🔗 Кнопка", callback_data="adm:btn"),
            ],
            [
                InlineKeyboardButton(text="📝 Тексты бота", callback_data="adm:texts_menu"),
                InlineKeyboardButton(text="🖼 Иконки", callback_data="adm:icons_menu"),
            ],
            [
                InlineKeyboardButton(text="👁 Предпросмотр", callback_data="adm:preview"),
                InlineKeyboardButton(text="🔄 Сбросить показы", callback_data="adm:reset_imp"),
            ],
            [InlineKeyboardButton(text="📢 Сделать рассылку", callback_data="adm:broadcast")],
            [InlineKeyboardButton(text="🔁 Обновить статистику", callback_data="adm:refresh")],
        ]
    )


def texts_menu_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"adm_txt:{key}")]
        for key, label in TEXT_LABELS.items()
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def icons_menu_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"🖼 {label}", callback_data=f"adm_ico:{key}")]
        for key, label in ICON_LABELS.items()
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cancel_kb(back_to: str = "adm:cancel") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=back_to)]]
    )


async def upload_to_catbox(file_bytes: bytes, filename: str = "icon.jpg") -> str:
    data = aiohttp.FormData()
    data.add_field("reqtype", "fileupload")
    data.add_field("fileToUpload", file_bytes, filename=filename, content_type="image/jpeg")
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post("https://catbox.moe/user/api.php", data=data) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Ошибка загрузки фото (код {resp.status})")
            url = (await resp.text()).strip()
            if not url.startswith("http"):
                raise RuntimeError(f"Неожиданный ответ фотохостинга: {url}")
            return url


# ==================== СКАЧИВАНИЕ И ПОИСК МЕДИА ====================

def search_soundcloud_sync(query_text: str, limit: int = 5) -> list[dict]:
    ydl_opts = {
        "extract_flat": True,
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"scsearch{limit}:{query_text}", download=False) or {}

    entries = info.get("entries") or []
    results = []
    for entry in entries:
        if not entry:
            continue
        track_url = entry.get("url") or entry.get("webpage_url")
        if not track_url or not str(track_url).startswith("http"):
            continue
        thumb = entry.get("thumbnail")
        if not thumb and entry.get("thumbnails"):
            thumb = entry["thumbnails"][-1].get("url")
        results.append(
            {
                "url": track_url,
                "title": entry.get("title") or "Без названия",
                "uploader": entry.get("uploader") or entry.get("channel") or "SoundCloud",
                "duration": format_duration(entry.get("duration")),
                "thumbnail": thumb,
            }
        )
    return results


def download_ytdlp_sync(url: str, audio_mode: bool) -> dict:
    temp_dir = tempfile.mkdtemp()
    out_template = os.path.join(temp_dir, "media.%(ext)s")

    if audio_mode:
        ydl_opts = {
            "outtmpl": out_template,
            "format": "bestaudio[filesize<48M]/bestaudio/best",
            "noplaylist": True,
            "writethumbnail": True,
            "quiet": True,
            "no_warnings": True,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        }
    else:
        ydl_opts = {
            "outtmpl": out_template,
            "format": "bv*[ext=mp4][filesize<45M]+ba[ext=m4a]/b[ext=mp4][filesize<48M]/best[filesize<48M]/best",
            "merge_output_format": "mp4",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
        }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True) or {}

        image_exts = (".jpg", ".jpeg", ".png", ".webp")
        all_files = [f for f in os.listdir(temp_dir) if not f.endswith(".part")]
        media_files = [f for f in all_files if not f.lower().endswith(image_exts)]
        if not media_files:
            raise RuntimeError("Файл не был скачан")

        preferred_ext = ".mp3" if audio_mode else ".mp4"
        chosen = next((f for f in media_files if f.lower().endswith(preferred_ext)), media_files[0])

        thumb_path = None
        if audio_mode:
            img_files = [f for f in all_files if f.lower().endswith(image_exts)]
            if img_files:
                raw_thumb = os.path.join(temp_dir, img_files[0])
                converted_thumb = os.path.join(temp_dir, "thumb.jpg")
                cmd = [
                    "ffmpeg", "-y", "-i", raw_thumb,
                    "-vf", "scale=320:320:force_original_aspect_ratio=decrease",
                    "-q:v", "3", converted_thumb,
                ]
                subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if os.path.exists(converted_thumb):
                    thumb_path = converted_thumb

        duration = info.get("duration")
        return {
            "path": os.path.join(temp_dir, chosen),
            "thumb_path": thumb_path,
            "temp_dir": temp_dir,
            "media_type": "audio" if audio_mode else "video",
            "title": info.get("track") or info.get("title"),
            "performer": info.get("artist") or info.get("uploader"),
            "duration": int(duration) if duration else None,
        }
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


async def download_pinterest_image(url: str) -> dict:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
    }
    temp_dir = tempfile.mkdtemp()
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url, allow_redirects=True) as resp:
                html_text = await resp.text(errors="ignore")

            img_url = None
            m = re.search(
                r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
                html_text,
                re.IGNORECASE,
            )
            if not m:
                m = re.search(
                    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
                    html_text,
                    re.IGNORECASE,
                )
            if m:
                img_url = m.group(1)
            else:
                m2 = re.search(
                    r"https://i\.pinimg\.com/(?:originals|736x|564x)/[a-zA-Z0-9/_-]+\.(?:jpg|jpeg|png|webp)",
                    html_text,
                )
                if m2:
                    img_url = m2.group(0)

            if not img_url:
                raise RuntimeError("Не удалось найти картинку на странице Pinterest")

            # Пробуем забрать оригинал в максимальном качестве
            orig_url = re.sub(r"i\.pinimg\.com/\d+x/", "i.pinimg.com/originals/", img_url)
            chosen_bytes = None
            for candidate in (orig_url, img_url):
                async with session.get(candidate) as img_resp:
                    if img_resp.status == 200:
                        chosen_bytes = await img_resp.read()
                        break

            if not chosen_bytes:
                raise RuntimeError("Не удалось скачать изображение с Pinterest")

            out_path = os.path.join(temp_dir, "photo.jpg")
            with open(out_path, "wb") as f:
                f.write(chosen_bytes)

            return {
                "path": out_path,
                "thumb_path": None,
                "temp_dir": temp_dir,
                "media_type": "photo",
                "title": None,
                "performer": None,
                "duration": None,
            }
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


async def download_media(url: str) -> dict:
    audio_mode = is_soundcloud_url(url)
    try:
        return await asyncio.to_thread(download_ytdlp_sync, url, audio_mode)
    except Exception:
        if is_pinterest_url(url):
            return await download_pinterest_image(url)
        raise


async def notify_admin(url: str, error: Exception):
    if ADMIN_ID:
        try:
            await bot.send_message(
                ADMIN_ID,
                f"⚠️ <b>Ошибка скачивания</b>\n\n"
                f"Ссылка: {url}\n"
                f"Ошибка: <code>{str(error)[:500]}</code>",
            )
        except Exception:
            pass


# ==================== АДМИН-ПАНЕЛЬ ====================

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer(admin_menu_text(), reply_markup=admin_menu_kb(), disable_web_page_preview=True)


@dp.callback_query(F.data.startswith("adm:"))
async def handle_admin_callbacks(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    action = callback.data.split(":")[1]

    if action in ("cancel", "back"):
        await state.clear()
        await callback.message.edit_text(
            admin_menu_text(), reply_markup=admin_menu_kb(), disable_web_page_preview=True
        )
        await callback.answer()

    elif action == "refresh":
        try:
            await callback.message.edit_text(
                admin_menu_text(), reply_markup=admin_menu_kb(), disable_web_page_preview=True
            )
        except Exception:
            pass
        await callback.answer("Обновлено")

    elif action == "toggle":
        STATE["ad_enabled"] = not STATE["ad_enabled"]
        await save_state()
        await callback.message.edit_text(
            admin_menu_text(), reply_markup=admin_menu_kb(), disable_web_page_preview=True
        )
        await callback.answer("Статус рекламы изменён")

    elif action == "reset_imp":
        STATE["stats"]["ad_impressions"] = 0
        await save_state()
        await callback.message.edit_text(
            admin_menu_text(), reply_markup=admin_menu_kb(), disable_web_page_preview=True
        )
        await callback.answer("Счётчик показов сброшен")

    elif action == "preview":
        caption, kb, ad_shown = get_ad_payload()
        if not ad_shown:
            await callback.answer("Сейчас реклама выключена или пустая", show_alert=True)
            return
        await callback.message.answer(
            caption or "<i>(только кнопка, без текста)</i>",
            reply_markup=kb,
            disable_web_page_preview=True,
        )
        await callback.answer()

    elif action == "text":
        await state.set_state(AdminStates.waiting_ad_text)
        await callback.message.edit_text(
            "✏️ <b>Отправь новый текст рекламы</b>\n\n"
            "Можно использовать жирность, эмодзи и вшитые ссылки Telegram.\n"
            "Чтобы убрать текст (оставить только кнопку), отправь <code>0</code>.",
            reply_markup=cancel_kb(),
        )
        await callback.answer()

    elif action == "btn":
        await state.set_state(AdminStates.waiting_ad_button)
        await callback.message.edit_text(
            "🔗 <b>Настройка кнопки под медиа</b>\n\n"
            "Отправь текст и ссылку через вертикальную черту:\n"
            "<code>⚡ ВСЕ СКИДКИ ТУТ | https://t.me/твой_канал</code>\n\n"
            "Чтобы отключить кнопку, отправь <code>0</code>.",
            reply_markup=cancel_kb(),
        )
        await callback.answer()

    elif action == "texts_menu":
        await state.clear()
        await callback.message.edit_text(
            "📝 <b>Редактирование текстов бота</b>\n\nВыбери, какой текст хочешь изменить:",
            reply_markup=texts_menu_kb(),
        )
        await callback.answer()

    elif action == "icons_menu":
        await state.clear()
        await callback.message.edit_text(
            "🖼 <b>Иконки карточек в инлайн-режиме</b>\n\nВыбери иконку для замены:",
            reply_markup=icons_menu_kb(),
        )
        await callback.answer()

    elif action == "broadcast":
        await state.set_state(AdminStates.waiting_broadcast)
        await callback.message.edit_text(
            f"📢 <b>Рассылка ({len(STATE['users'])} чел.)</b>\n\n"
            "Отправь сюда готовый пост (текст, фото или видео). "
            "Он сразу будет разослан всем пользователям из лички.",
            reply_markup=cancel_kb(),
        )
        await callback.answer()


@dp.callback_query(F.data.startswith("adm_txt:"))
async def handle_edit_text_pick(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    key = callback.data.split(":", 1)[1]
    if key not in DEFAULT_TEXTS:
        return
    await state.set_state(AdminStates.waiting_custom_text)
    await state.update_data(edit_key=key)
    current = STATE["texts"].get(key, "")
    hint_note = "\n<i>(Можно вставить {username} — бот подставит свой юзернейм)</i>" if key in ("start_text", "hint_click_text") else ""
    await callback.message.edit_text(
        f"📝 <b>{TEXT_LABELS[key]}</b>\n\n"
        f"<b>Сейчас установлено:</b>\n{current}\n\n"
        f"Отправь новый текст (или <code>0</code>, чтобы вернуть стандартный).{hint_note}",
        reply_markup=cancel_kb("adm:texts_menu"),
        disable_web_page_preview=True,
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("adm_ico:"))
async def handle_edit_icon_pick(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    key = callback.data.split(":", 1)[1]
    if key not in DEFAULT_ICONS:
        return
    await state.set_state(AdminStates.waiting_custom_icon)
    await state.update_data(edit_key=key)
    current = STATE["icons"].get(key, "")
    await callback.message.edit_text(
        f"🖼 <b>Иконка: {ICON_LABELS[key]}</b>\n\n"
        f"<b>Текущая ссылка:</b>\n<code>{current}</code>\n\n"
        f"Отправь сюда <b>картинку</b> (как фото) или прямую <b>ссылку</b> (<code>https://...</code>).\n"
        f"Чтобы вернуть стандартную иконку, отправь <code>0</code>.",
        reply_markup=cancel_kb("adm:icons_menu"),
        disable_web_page_preview=True,
    )
    await callback.answer()


@dp.message(AdminStates.waiting_custom_text)
async def process_custom_text(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    key = data.get("edit_key")
    if key not in DEFAULT_TEXTS:
        await state.clear()
        return

    raw = (message.text or "").strip()
    if raw == "0":
        STATE["texts"][key] = DEFAULT_TEXTS[key]
    else:
        STATE["texts"][key] = message.html_text if key == "start_text" else raw

    await save_state()
    await state.clear()
    await message.answer(
        f"✅ Текст «{TEXT_LABELS[key]}» обновлён!",
        reply_markup=texts_menu_kb(),
    )


@dp.message(AdminStates.waiting_custom_icon)
async def process_custom_icon(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    key = data.get("edit_key")
    if key not in DEFAULT_ICONS:
        await state.clear()
        return

    if message.photo:
        wait_msg = await message.answer("⏳ Загружаю иконку...")
        try:
            file_io = await bot.download(message.photo[-1])
            new_url = await upload_to_catbox(file_io.read(), f"{key}.jpg")
            STATE["icons"][key] = new_url
            await save_state()
            await state.clear()
            await wait_msg.edit_text(
                f"✅ Иконка «{ICON_LABELS[key]}» обновлена!\n<code>{new_url}</code>",
                reply_markup=icons_menu_kb(),
            )
        except Exception as e:
            await wait_msg.edit_text(
                f"❌ Не удалось загрузить картинку: <code>{str(e)[:200]}</code>\n"
                "Попробуй отправить прямую ссылку на PNG/JPG.",
                reply_markup=cancel_kb("adm:icons_menu"),
            )
        return

    text = (message.text or "").strip()
    if text == "0":
        STATE["icons"][key] = DEFAULT_ICONS[key]
        await save_state()
        await state.clear()
        await message.answer(
            f"✅ Иконка «{ICON_LABELS[key]}» сброшена на стандартную.",
            reply_markup=icons_menu_kb(),
        )
        return

    if not text.startswith(("http://", "https://")):
        await message.answer(
            "❌ Отправь картинку (как фото) или ссылку, начинающуюся с <code>https://</code>",
            reply_markup=cancel_kb("adm:icons_menu"),
        )
        return

    STATE["icons"][key] = text
    await save_state()
    await state.clear()
    await message.answer(
        f"✅ Иконка «{ICON_LABELS[key]}» обновлена!",
        reply_markup=icons_menu_kb(),
    )


@dp.message(AdminStates.waiting_ad_text)
async def process_ad_text(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if message.text and message.text.strip() == "0":
        STATE["ad_text"] = ""
    else:
        STATE["ad_text"] = message.html_text or ""
    await save_state()
    await state.clear()
    await message.answer(
        "✅ Текст рекламы обновлён!\n\n" + admin_menu_text(),
        reply_markup=admin_menu_kb(),
        disable_web_page_preview=True,
    )


@dp.message(AdminStates.waiting_ad_button)
async def process_ad_button(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    text = (message.text or "").strip()
    if text == "0":
        STATE["ad_btn_text"] = ""
        STATE["ad_btn_url"] = ""
        await save_state()
        await state.clear()
        await message.answer(
            "✅ Кнопка отключена.\n\n" + admin_menu_text(),
            reply_markup=admin_menu_kb(),
            disable_web_page_preview=True,
        )
        return

    if "|" not in text:
        await message.answer("❌ Неверный формат. Напиши через черту: <code>Текст | https://ссылка</code>", reply_markup=cancel_kb())
        return

    btn_text, btn_url = [part.strip() for part in text.split("|", 1)]
    if not btn_url.startswith(("http://", "https://", "tg://")):
        await message.answer("❌ Ссылка должна начинаться с <code>https://</code>", reply_markup=cancel_kb())
        return

    STATE["ad_btn_text"] = btn_text
    STATE["ad_btn_url"] = btn_url
    await save_state()
    await state.clear()
    await message.answer(
        "✅ Кнопка обновлена!\n\n" + admin_menu_text(),
        reply_markup=admin_menu_kb(),
        disable_web_page_preview=True,
    )


@dp.message(AdminStates.waiting_broadcast)
async def process_broadcast(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    users = list(STATE["users"])
    status_msg = await message.answer(f"⏳ Начинаю рассылку по {len(users)} пользователям...")
    ok, fail = 0, 0
    for uid in users:
        try:
            await bot.copy_message(
                chat_id=uid,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    await status_msg.edit_text(
        f"✅ <b>Рассылка завершена</b>\n\nДоставлено: {ok}\nНе доставлено (заблокировали бота): {fail}"
    )


# ==================== ПОЛЬЗОВАТЕЛЬСКИЙ РЕЖИМ (ЛИЧКА) ====================

async def send_downloaded_to_dm(message: types.Message, url: str, user_id: int):
    status_msg = await message.answer(STATE["texts"]["loading_text"])
    media = None
    try:
        async with DOWNLOAD_SEMAPHORE:
            media = await download_media(url)

        caption, kb, ad_shown = get_ad_payload()
        mtype = media["media_type"]

        if mtype == "audio":
            thumb = FSInputFile(media["thumb_path"]) if media.get("thumb_path") else None
            await message.answer_audio(
                audio=FSInputFile(media["path"]),
                thumbnail=thumb,
                title=media["title"],
                performer=media["performer"],
                duration=media["duration"],
                caption=caption,
                reply_markup=kb,
            )
        elif mtype == "photo":
            await message.answer_photo(
                photo=FSInputFile(media["path"]),
                caption=caption,
                reply_markup=kb,
            )
        else:
            await message.answer_video(
                video=FSInputFile(media["path"]),
                caption=caption,
                reply_markup=kb,
            )

        await record_download("dm", ad_shown, user_id=user_id)
        await status_msg.delete()
    except Exception as e:
        await status_msg.edit_text("❌ Не удалось скачать по этой ссылке.")
        await notify_admin(url, e)
    finally:
        if media and media.get("temp_dir"):
            shutil.rmtree(media["temp_dir"], ignore_errors=True)


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await register_user(message.from_user.id, is_dm=True)
    text = STATE["texts"]["start_text"].replace("{username}", BOT_USERNAME)
    await message.answer(text)


@dp.callback_query(F.data.startswith("scdl:"))
async def handle_dm_soundcloud_pick(callback: types.CallbackQuery):
    key = callback.data.split(":", 1)[1]
    url = SEARCH_CACHE.get(key)
    if not url:
        await callback.answer("Поиск устарел, напиши название ещё раз.", show_alert=True)
        return
    await callback.answer("Загружаю трек...")
    await send_downloaded_to_dm(callback.message, url, user_id=callback.from_user.id)


@dp.message(F.text)
async def handle_direct_message(message: types.Message):
    await register_user(message.from_user.id, is_dm=True)
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return

    match = MEDIA_RE.search(text)
    if match:
        await send_downloaded_to_dm(message, match.group(0), user_id=message.from_user.id)
        return

    # Если прислали не ссылку — ищем трек в SoundCloud
    if len(text) < 2 or "http://" in text or "https://" in text:
        return

    wait_msg = await message.answer("🔎 Ищу в SoundCloud...")
    try:
        async with SEARCH_SEMAPHORE:
            tracks = await asyncio.to_thread(search_soundcloud_sync, text, 5)
        if not tracks:
            await wait_msg.edit_text("❌ Ничего не найдено в SoundCloud.")
            return

        buttons = []
        for tr in tracks:
            key = cache_search_url(tr["url"])
            dur = f" ({tr['duration']})" if tr["duration"] else ""
            label = f"🎵 {tr['title']} — {tr['uploader']}{dur}"[:60]
            buttons.append([InlineKeyboardButton(text=label, callback_data=f"scdl:{key}")])

        await wait_msg.edit_text(
            f"🎧 <b>Результаты поиска:</b> {text}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        )
    except Exception:
        await wait_msg.edit_text("❌ Ошибка поиска, попробуй ещё раз.")


# ==================== ИНЛАЙН-РЕЖИМ В ЛЮБЫХ ЧАТАХ ====================

@dp.inline_query()
async def handle_inline_query(query: types.InlineQuery):
    await register_user(query.from_user.id, is_dm=False)
    q = (query.query or "").strip()
    loading_text = STATE["texts"]["loading_text"]

    # 1. Пустой ввод — показываем карточку-подсказку
    if not q:
        click_text = STATE["texts"]["hint_click_text"].replace("{username}", BOT_USERNAME)
        hint_result = InlineQueryResultArticle(
            id="hint:" + uuid.uuid4().hex[:8],
            title=STATE["texts"]["hint_title"],
            description=STATE["texts"]["hint_desc"],
            thumbnail_url=STATE["icons"]["hint"],
            input_message_content=InputTextMessageContent(message_text=click_text),
        )
        await query.answer([hint_result], cache_time=5, is_personal=True)
        return

    loading_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=loading_text, callback_data="loading")]
        ]
    )

    # 2. Введена ссылка на поддерживаемую площадку
    match = MEDIA_RE.search(q)
    if match:
        url = match.group(0)
        title, icon_url = detect_platform_ui(url)
        result = InlineQueryResultArticle(
            id="url:" + uuid.uuid4().hex[:12],
            title=title,
            thumbnail_url=icon_url,
            input_message_content=InputTextMessageContent(message_text=loading_text),
            reply_markup=loading_kb,
        )
        await query.answer([result], cache_time=1, is_personal=True)
        return

    # 3. Введён текст (не ссылка) — поиск треков в SoundCloud
    if len(q) < 2 or "http://" in q or "https://" in q:
        await query.answer([], cache_time=1)
        return

    try:
        async with SEARCH_SEMAPHORE:
            tracks = await asyncio.to_thread(search_soundcloud_sync, q, 5)
    except Exception:
        tracks = []

    results = []
    for tr in tracks:
        key = cache_search_url(tr["url"])
        desc = f"{tr['uploader']} • {tr['duration']}" if tr["duration"] else tr["uploader"]
        results.append(
            InlineQueryResultArticle(
                id=f"sc:{key}",
                title=tr["title"],
                description=desc,
                thumbnail_url=tr["thumbnail"] or STATE["icons"]["soundcloud"],
                input_message_content=InputTextMessageContent(message_text=loading_text),
                reply_markup=loading_kb,
            )
        )

    await query.answer(results, cache_time=10, is_personal=True)


@dp.chosen_inline_result()
async def handle_chosen_inline(result: types.ChosenInlineResult):
    if not result.inline_message_id or result.result_id.startswith("hint:"):
        return

    url = None
    if result.result_id.startswith("sc:"):
        key = result.result_id.split(":", 1)[1]
        url = SEARCH_CACHE.get(key)
    else:
        match = MEDIA_RE.search(result.query or "")
        if match:
            url = match.group(0)

    if not url:
        return

    media = None
    try:
        async with DOWNLOAD_SEMAPHORE:
            media = await download_media(url)

        caption, kb, ad_shown = get_ad_payload()
        mtype = media["media_type"]

        if mtype == "audio":
            thumb = FSInputFile(media["thumb_path"]) if media.get("thumb_path") else None
            sent = await bot.send_audio(
                chat_id=STORAGE_CHAT_ID,
                audio=FSInputFile(media["path"]),
                thumbnail=thumb,
                title=media["title"],
                performer=media["performer"],
                duration=media["duration"],
            )
            input_media = InputMediaAudio(
                media=sent.audio.file_id,
                caption=caption,
                title=media["title"],
                performer=media["performer"],
                duration=media["duration"],
            )
        elif mtype == "photo":
            sent = await bot.send_photo(
                chat_id=STORAGE_CHAT_ID,
                photo=FSInputFile(media["path"]),
            )
            input_media = InputMediaPhoto(media=sent.photo[-1].file_id, caption=caption)
        else:
            sent = await bot.send_video(
                chat_id=STORAGE_CHAT_ID,
                video=FSInputFile(media["path"]),
            )
            input_media = InputMediaVideo(media=sent.video.file_id, caption=caption)

        await bot.edit_message_media(
            inline_message_id=result.inline_message_id,
            media=input_media,
            reply_markup=kb,
        )
        await bot.delete_message(chat_id=STORAGE_CHAT_ID, message_id=sent.message_id)
        await record_download("inline", ad_shown, user_id=result.from_user.id)
    except Exception as e:
        await notify_admin(url, e)
        try:
            await bot.edit_message_text(
                inline_message_id=result.inline_message_id,
                text="❌ Не удалось загрузить файл.",
            )
        except Exception:
            pass
    finally:
        if media and media.get("temp_dir"):
            shutil.rmtree(media["temp_dir"], ignore_errors=True)


@dp.callback_query(F.data == "loading")
async def handle_loading_click(callback: types.CallbackQuery):
    await callback.answer("Загружаю файл, подождите пару секунд...", show_alert=False)


async def main():
    global BOT_USERNAME
    me = await bot.get_me()
    BOT_USERNAME = me.username or ""
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
