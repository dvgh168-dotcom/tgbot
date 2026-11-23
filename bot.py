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
from datetime import date, datetime
from io import BytesIO

import aiohttp
import yt_dlp
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputMediaAudio,
    InputMediaPhoto,
    InputMediaVideo,
    InputSticker,
    InputTextMessageContent,
)
from dotenv import load_dotenv
from PIL import Image, ImageOps

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
STORAGE_CHAT_ID = int(os.getenv("STORAGE_CHAT_ID", "0"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

DATA_DIR = "data"
STATE_FILE = os.path.join(DATA_DIR, "state.json")

# Ссылки: TikTok, Pinterest (любые региональные поддомены и pin.it), X (Twitter), SoundCloud
MEDIA_RE = re.compile(
    r"https?://(?:[a-z0-9-]+\.)*"
    r"(?:tiktok\.com|pinterest\.[a-z.]+|pin\.it|twitter\.com|x\.com|soundcloud\.com)/\S+",
    re.IGNORECASE,
)

DOWNLOAD_SEMAPHORE = asyncio.Semaphore(4)
SEARCH_SEMAPHORE = asyncio.Semaphore(5)
STATE_LOCK = asyncio.Lock()
BOT_USERNAME = ""

# Кэш найденных треков SoundCloud: key -> url
SEARCH_CACHE: OrderedDict[str, str] = OrderedDict()

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())


class AdminStates(StatesGroup):
    waiting_ad_text = State()
    waiting_broadcast = State()
    waiting_custom_text = State()
    waiting_custom_icon = State()


class StickerStates(StatesGroup):
    waiting_pack_title = State()
    waiting_first_item = State()
    waiting_add_item = State()
    waiting_grid_size = State()
    waiting_grid_photo = State()
    waiting_rename = State()
    waiting_delete_sticker = State()


DEFAULT_TEXTS = {
    "start_text": (
        "👋 Привет! Я скачиваю видео, фото и музыку без водяных знаков, "
        "а ещё помогаю собирать свои стикерпаки и сетки премиум-эмодзи.\n\n"
        "<b>Загрузка медиа:</b> TikTok, Pinterest (видео и фото), X (Twitter) и SoundCloud.\n\n"
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
    "src_btn_tiktok": "🎬 Посмотреть в TikTok",
    "src_btn_pinterest": "📌 Посмотреть на Pinterest",
    "src_btn_x": "🔗 Посмотреть в X",
    "src_btn_soundcloud": "🎵 Посмотреть на SoundCloud",
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
    "src_btn_tiktok": "🔗 Кнопка: TikTok",
    "src_btn_pinterest": "🔗 Кнопка: Pinterest",
    "src_btn_x": "🔗 Кнопка: X (Twitter)",
    "src_btn_soundcloud": "🔗 Кнопка: SoundCloud",
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
    "source_btn_enabled": True,
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
        "packs_created": 0,
    },
    "users": [],
    "all_users": [],
    "sticker_packs": {},
    "premium_users": {},
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


def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID


def has_premium(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    exp_str = STATE.get("premium_users", {}).get(str(user_id))
    if not exp_str:
        return False
    try:
        return datetime.fromisoformat(exp_str) > datetime.utcnow()
    except Exception:
        return False


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


def detect_platform_key(url: str) -> str:
    lower = url.lower()
    if "soundcloud.com" in lower:
        return "soundcloud"
    if "pinterest." in lower or "pin.it" in lower:
        return "pinterest"
    if "x.com" in lower or "twitter.com" in lower:
        return "x"
    return "tiktok"


def get_media_payload(source_url: str, user_id: int | None = None) -> tuple[str | None, InlineKeyboardMarkup | None, bool]:
    is_prem = has_premium(user_id) if user_id else False
    caption = None if is_prem else (STATE["ad_text"] if (STATE["ad_enabled"] and STATE["ad_text"]) else None)
    kb = None
    if STATE.get("source_btn_enabled", True) and source_url:
        pkey = detect_platform_key(source_url)
        btn_text = STATE["texts"].get(f"src_btn_{pkey}") or DEFAULT_TEXTS[f"src_btn_{pkey}"]
        if btn_text:
            kb = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text=btn_text, url=source_url)]]
            )
    ad_shown = bool(caption)
    return caption, kb, ad_shown


def is_soundcloud_url(url: str) -> bool:
    return "soundcloud.com" in url.lower()


def is_pinterest_url(url: str) -> bool:
    lower = url.lower()
    return "pinterest." in lower or "pin.it" in lower


def detect_platform_ui(url: str) -> tuple[str, str]:
    pkey = detect_platform_key(url)
    return STATE["texts"][f"title_{pkey}"], STATE["icons"][pkey]


def start_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🎨 Стикеры и Эмодзи", callback_data="stk:menu")]
        ]
    )


# ==================== ОБРАБОТКА КАРТИНОК ДЛЯ СТИКЕРОВ И ЭМОДЗИ ====================

def format_pack_title(raw_title: str, is_premium: bool) -> str:
    clean = raw_title.strip()
    if is_premium or not BOT_USERNAME:
        return clean[:64]
    suffix = f" • @{BOT_USERNAME}"
    max_len = 64 - len(suffix)
    return (clean[:max_len].strip() or "Pack") + suffix


def make_pack_short_name() -> str:
    return f"p{uuid.uuid4().hex[:10]}_by_{BOT_USERNAME}"


def get_user_packs(user_id: int) -> list[dict]:
    return STATE.setdefault("sticker_packs", {}).setdefault(str(user_id), [])


def find_user_pack(user_id: int, pack_name: str) -> dict | None:
    for p in get_user_packs(user_id):
        if p["name"] == pack_name:
            return p
    return None


def image_to_sticker_png_sync(raw_bytes: bytes, is_emoji: bool) -> bytes:
    img = Image.open(BytesIO(raw_bytes)).convert("RGBA")
    if is_emoji:
        resized = ImageOps.contain(img, (100, 100), Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
        offset = ((100 - resized.width) // 2, (100 - resized.height) // 2)
        canvas.paste(resized, offset, resized)
        out_img = canvas
    else:
        # Для обычного стикера одна сторона должна быть ровно 512px, вторая <= 512px
        out_img = ImageOps.contain(img, (512, 512), Image.Resampling.LANCZOS)

    buf = BytesIO()
    out_img.save(buf, format="PNG")
    return buf.getvalue()


def slice_image_grid_sync(raw_bytes: bytes, cols: int, rows: int, is_emoji: bool) -> list[bytes]:
    tile_size = 100 if is_emoji else 512
    target_w = cols * tile_size
    target_h = rows * tile_size

    img = Image.open(BytesIO(raw_bytes)).convert("RGBA")
    fitted = ImageOps.contain(img, (target_w, target_h), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
    offset = ((target_w - fitted.width) // 2, (target_h - fitted.height) // 2)
    canvas.paste(fitted, offset, fitted)

    tiles = []
    for r in range(rows):
        for c in range(cols):
            box = (c * tile_size, r * tile_size, (c + 1) * tile_size, (r + 1) * tile_size)
            tile = canvas.crop(box)
            buf = BytesIO()
            tile.save(buf, format="PNG")
            tiles.append(buf.getvalue())
    return tiles


async def extract_images_from_message(message: types.Message) -> tuple[list[tuple[bytes, str]], str | None]:
    """
    Возвращает ([(bytes, emoji), ...], error_text_or_None).
    Поддерживает фото, картинки-документы, статичные стикеры и статичные кастомные эмодзи.
    """
    items: list[tuple[bytes, str]] = []

    if message.photo:
        io_obj = await bot.download(message.photo[-1])
        items.append((io_obj.read(), "😀"))
        return items, None

    if message.document:
        mime = (message.document.mime_type or "").lower()
        if mime.startswith("image/"):
            io_obj = await bot.download(message.document)
            items.append((io_obj.read(), "😀"))
            return items, None
        return [], "❌ Отправь картинку (PNG/JPG/WEBP), а не другой файл."

    if message.sticker:
        if message.sticker.is_animated or message.sticker.is_video:
            return [], "❌ Пока поддерживаются только статичные стикеры и картинки (без анимации и видео)."
        io_obj = await bot.download(message.sticker)
        items.append((io_obj.read(), message.sticker.emoji or "😀"))
        return items, None

    if message.entities:
        emoji_ids = [
            e.custom_emoji_id
            for e in message.entities
            if e.type == "custom_emoji" and e.custom_emoji_id
        ]
        if emoji_ids:
            stickers = await bot.get_custom_emoji_stickers(emoji_ids[:20])
            skipped = 0
            for st in stickers:
                if st.is_animated or st.is_video:
                    skipped += 1
                    continue
                io_obj = await bot.download(st)
                items.append((io_obj.read(), st.emoji or "😀"))
            if not items:
                return [], "❌ Все отправленные эмодзи анимированные. Пока поддерживаются только статичные."
            return items, None

    return [], "❌ Отправь картинку (фото/файл), статичный стикер или статичный премиум-эмодзи."


# ==================== МЕНЮ КОНСТРУКТОРА СТИКЕРОВ ====================

def sticker_main_menu_text() -> str:
    return (
        "🎨 <b>Конструктор стикеров и эмодзи</b>\n\n"
        "Создавай свои стикерпаки, копируй чужие стикеры и нарезай картинки "
        "на цельные мозаики для <b>Telegram Premium</b>."
    )


def sticker_main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🆕 Создать новый набор", callback_data="stk:new")],
            [
                InlineKeyboardButton(text="🧩 Мои стикеры", callback_data="stk:list:regular"),
                InlineKeyboardButton(text="😀 Мои эмодзи", callback_data="stk:list:custom_emoji"),
            ],
            [InlineKeyboardButton(text="⬅️ В начало", callback_data="stk:home")],
        ]
    )


def pack_list_kb(user_id: int, ptype: str) -> InlineKeyboardMarkup:
    packs = [p for p in get_user_packs(user_id) if p["type"] == ptype]
    rows = []
    for p in packs:
        badge = "💎 " if p.get("is_premium") else ""
        rows.append([InlineKeyboardButton(text=f"{badge}{p['title']}", callback_data=f"stkp:{p['name']}")])
    rows.append([InlineKeyboardButton(text="🆕 Создать набор", callback_data=f"stk:new_type:{ptype}")])
    rows.append([InlineKeyboardButton(text="⬅️ Вернуться в меню", callback_data="stk:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pack_card_text(pack: dict) -> str:
    type_label = "Премиум-эмодзи" if pack["type"] == "custom_emoji" else "Стикеры"
    prem_line = "💎 <b>Это премиум набор</b> <i>(без подписи бота)</i>" if pack.get("is_premium") else "🔴 <b>Премиум не подключён</b> <i>(в названии указан бот)</i>"
    return (
        f"🧿 <b>Название:</b> {pack['title']}\n"
        f"📂 <b>Тип:</b> {type_label}\n\n"
        f"{prem_line}"
    )


def pack_card_kb(pack: dict) -> InlineKeyboardMarkup:
    name = pack["name"]
    rows = []
    if not pack.get("is_premium"):
        rows.append([InlineKeyboardButton(text="🆙 Улучшить набор (скоро за ⭐)", callback_data=f"stka:prem:{name}")])
    rows.append([InlineKeyboardButton(text="👀 Посмотреть набор", url=f"https://t.me/addstickers/{name}")])
    rows.append([InlineKeyboardButton(text="✏️ Изменить название", callback_data=f"stka:rename:{name}")])
    rows.append(
        [
            InlineKeyboardButton(text="➕ Добавить", callback_data=f"stka:add:{name}"),
            InlineKeyboardButton(text="➖ Удалить", callback_data=f"stka:del_one:{name}"),
        ]
    )
    if pack["type"] == "custom_emoji":
        rows.append([InlineKeyboardButton(text="🧩 Нарезать фото в сетку", callback_data=f"stka:grid:{name}")])
    rows.append([InlineKeyboardButton(text="❌ Удалить набор", callback_data=f"stka:del_pack:{name}")])
    back_cb = f"stk:list:{pack['type']}"
    rows.append([InlineKeyboardButton(text="⬅️ Назад к списку", callback_data=back_cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def grid_presets_kb(back_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="4×2 (8 шт)", callback_data="stkgrid:4x2"),
                InlineKeyboardButton(text="5×2 (10 шт)", callback_data="stkgrid:5x2"),
                InlineKeyboardButton(text="6×3 (18 шт)", callback_data="stkgrid:6x3"),
            ],
            [
                InlineKeyboardButton(text="8×2 (16 шт)", callback_data="stkgrid:8x2"),
                InlineKeyboardButton(text="8×3 (24 шт)", callback_data="stkgrid:8x3"),
                InlineKeyboardButton(text="8×4 (32 шт)", callback_data="stkgrid:8x4"),
            ],
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data=back_cb)],
        ]
    )


# ==================== ОБРАБОТЧИКИ КОНСТРУКТОРА ====================

@dp.message(Command("stickers"))
async def cmd_stickers(message: types.Message, state: FSMContext):
    await register_user(message.from_user.id, is_dm=True)
    await state.clear()
    await message.answer(sticker_main_menu_text(), reply_markup=sticker_main_menu_kb())


@dp.callback_query(F.data == "stk:home")
async def cb_stk_home(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text = STATE["texts"]["start_text"].replace("{username}", BOT_USERNAME)
    await callback.message.edit_text(text, reply_markup=start_kb())
    await callback.answer()


@dp.callback_query(F.data == "stk:menu")
async def cb_stk_menu(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(sticker_main_menu_text(), reply_markup=sticker_main_menu_kb())
    await callback.answer()


@dp.callback_query(F.data == "stk:new")
async def cb_stk_new(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🧩 Обычные стикеры", callback_data="stk:new_type:regular")],
            [InlineKeyboardButton(text="😀 Премиум-эмодзи (и нарезка сеток)", callback_data="stk:new_type:custom_emoji")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="stk:menu")],
        ]
    )
    await callback.message.edit_text(
        "🆕 <b>Выбери тип нового набора:</b>\n\n"
        "• <b>Обычные стикеры</b> — крупные стикеры `512×512` для чатов\n"
        "• <b>Премиум-эмодзи</b> — мини-иконки `100×100` и нарезка картинок в мозаику",
        reply_markup=kb,
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("stk:new_type:"))
async def cb_stk_new_type(callback: types.CallbackQuery, state: FSMContext):
    ptype = callback.data.split(":", 2)[2]
    await state.set_state(StickerStates.waiting_pack_title)
    await state.update_data(new_pack_type=ptype)
    label = "эмодзи-набора" if ptype == "custom_emoji" else "стикерпака"
    await callback.message.edit_text(
        f"✏️ <b>Введи название для нового {label}:</b>\n\n"
        "<i>Например: Мои мемы или Эстетика</i>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="stk:menu")]]
        ),
    )
    await callback.answer()


@dp.message(StickerStates.waiting_pack_title, F.text)
async def msg_pack_title(message: types.Message, state: FSMContext):
    raw_title = (message.text or "").strip()
    if not raw_title or raw_title.startswith("/"):
        await message.answer("❌ Напиши обычное текстовое название (до 45 символов).")
        return

    data = await state.get_data()
    ptype = data.get("new_pack_type", "regular")
    await state.update_data(new_pack_title=raw_title[:45])
    await state.set_state(StickerStates.waiting_first_item)

    rows = []
    if ptype == "custom_emoji":
        rows.append([InlineKeyboardButton(text="🧩 Нарезать фото в сетку", callback_data="stk:first_grid")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="stk:menu")])

    extra = "\n\nИли нажми <b>«🧩 Нарезать фото в сетку»</b>, чтобы сразу разбить картинку на мозаику." if ptype == "custom_emoji" else ""
    await message.answer(
        f"📸 <b>Отправь первую картинку или стикер</b>, чтобы создать набор «{raw_title[:45]}».\n"
        f"Можно прислать фото, картинку файлом, чужой статичный стикер или статичный премиум-эмодзи.{extra}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@dp.callback_query(F.data == "stk:first_grid")
async def cb_first_grid(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("new_pack_title"):
        await callback.answer("Сначала выбери создание набора", show_alert=True)
        return
    await state.set_state(StickerStates.waiting_grid_size)
    await state.update_data(grid_mode="create_new")
    await callback.message.edit_text(
        "🧩 <b>Выбери размер сетки (ширина × высота):</b>\n\n"
        "В Telegram панель эмодзи вмещает <b>8 штук в ряд</b> — для широких баннеров бери <code>8×2</code>, <code>8×3</code> или <code>8×4</code>.\n"
        "Можешь нажать кнопку ниже или написать свой размер, например <code>7x3</code> (от 2×1 до 8×6).",
        reply_markup=grid_presets_kb("stk:menu"),
    )
    await callback.answer()


@dp.message(StickerStates.waiting_first_item)
async def msg_first_item(message: types.Message, state: FSMContext):
    items, err = await extract_images_from_message(message)
    if err:
        await message.answer(err)
        return

    data = await state.get_data()
    ptype = data.get("new_pack_type", "regular")
    raw_title = data.get("new_pack_title", "My Pack")
    is_emoji = ptype == "custom_emoji"
    is_prem = has_premium(message.from_user.id)

    short_name = make_pack_short_name()
    full_title = format_pack_title(raw_title, is_prem)

    wait_msg = await message.answer("⏳ Создаю набор...")
    try:
        input_stickers = []
        for idx, (raw_bytes, emoji) in enumerate(items[:50]):
            png_bytes = await asyncio.to_thread(image_to_sticker_png_sync, raw_bytes, is_emoji)
            input_stickers.append(
                InputSticker(
                    sticker=BufferedInputFile(png_bytes, filename=f"s{idx}.png"),
                    emoji_list=[emoji],
                    format="static",
                )
            )

        await bot.create_new_sticker_set(
            user_id=message.from_user.id,
            name=short_name,
            title=full_title,
            stickers=input_stickers,
            sticker_type=ptype,
        )

        pack_obj = {
            "name": short_name,
            "title": raw_title,
            "type": ptype,
            "is_premium": is_prem,
        }
        get_user_packs(message.from_user.id).append(pack_obj)
        STATE["stats"]["packs_created"] = STATE["stats"].get("packs_created", 0) + 1
        await save_state()
        await state.clear()

        await wait_msg.edit_text(
            f"✅ <b>Набор создан!</b>\n\n{pack_card_text(pack_obj)}",
            reply_markup=pack_card_kb(pack_obj),
        )
    except Exception as e:
        await wait_msg.edit_text(f"❌ Не удалось создать набор: <code>{str(e)[:300]}</code>")


@dp.callback_query(F.data.startswith("stk:list:"))
async def cb_stk_list(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    ptype = callback.data.split(":", 2)[2]
    header = "😀 <b>Мои эмодзи</b>" if ptype == "custom_emoji" else "🧩 <b>Мои стикеры</b>"
    packs = [p for p in get_user_packs(callback.from_user.id) if p["type"] == ptype]
    body = f"{header}\n\nВыбери набор для управления:" if packs else f"{header}\n\nУ тебя пока нет созданных наборов."
    await callback.message.edit_text(body, reply_markup=pack_list_kb(callback.from_user.id, ptype))
    await callback.answer()


@dp.callback_query(F.data.startswith("stkp:"))
async def cb_open_pack(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    pack_name = callback.data.split(":", 1)[1]
    pack = find_user_pack(callback.from_user.id, pack_name)
    if not pack:
        await callback.answer("Набор не найден", show_alert=True)
        return
    await callback.message.edit_text(pack_card_text(pack), reply_markup=pack_card_kb(pack))
    await callback.answer()


@dp.callback_query(F.data.startswith("stka:"))
async def cb_pack_action(callback: types.CallbackQuery, state: FSMContext):
    _, action, pack_name = callback.data.split(":", 2)
    pack = find_user_pack(callback.from_user.id, pack_name)
    if not pack:
        await callback.answer("Набор не найден", show_alert=True)
        return

    back_to_pack = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад к набору", callback_data=f"stkp:{pack_name}")]]
    )

    if action == "prem":
        await callback.answer(
            "Оплата улучшения набора через Telegram Stars появится в следующем обновлении!",
            show_alert=True,
        )
        return

    elif action == "rename":
        await state.set_state(StickerStates.waiting_rename)
        await state.update_data(active_pack=pack_name)
        await callback.message.edit_text(
            f"✏️ <b>Введи новое название для набора «{pack['title']}»:</b>",
            reply_markup=back_to_pack,
        )
        await callback.answer()

    elif action == "add":
        await state.set_state(StickerStates.waiting_add_item)
        await state.update_data(active_pack=pack_name)
        await callback.message.edit_text(
            f"➕ <b>Добавление в набор «{pack['title']}»</b>\n\n"
            "Отправляй картинки (фото/файлы), чужие статичные стикеры или статичные эмодзи — "
            "можно сразу несколько подряд.\n\nКогда закончишь, нажми кнопку ниже.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="✅ Готово", callback_data=f"stkp:{pack_name}")]]
            ),
        )
        await callback.answer()

    elif action == "grid":
        await state.set_state(StickerStates.waiting_grid_size)
        await state.update_data(grid_mode="add_existing", active_pack=pack_name)
        await callback.message.edit_text(
            f"🧩 <b>Нарезка фото в сетку для «{pack['title']}»</b>\n\n"
            "Выбери размер сетки (ширина × высота) кнопкой или напиши вручную, например <code>7x3</code> (макс. 8×6):",
            reply_markup=grid_presets_kb(f"stkp:{pack_name}"),
        )
        await callback.answer()

    elif action == "del_one":
        await state.set_state(StickerStates.waiting_delete_sticker)
        await state.update_data(active_pack=pack_name)
        await callback.message.edit_text(
            f"➖ <b>Удаление из набора «{pack['title']}»</b>\n\n"
            "Отправь сюда стикер или эмодзи <b>из этого набора</b>, который нужно удалить.",
            reply_markup=back_to_pack,
        )
        await callback.answer()

    elif action == "del_pack":
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🗑 Да, удалить навсегда", callback_data=f"stka:del_confirm:{pack_name}")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data=f"stkp:{pack_name}")],
            ]
        )
        await callback.message.edit_text(
            f"⚠️ Точно удалить набор <b>«{pack['title']}»</b>? Это действие нельзя отменить.",
            reply_markup=kb,
        )
        await callback.answer()

    elif action == "del_confirm":
        try:
            await bot.delete_sticker_set(name=pack_name)
        except Exception:
            pass
        packs = get_user_packs(callback.from_user.id)
        STATE["sticker_packs"][str(callback.from_user.id)] = [p for p in packs if p["name"] != pack_name]
        await save_state()
        await callback.message.edit_text(
            f"🗑 Набор <b>«{pack['title']}»</b> удалён.",
            reply_markup=pack_list_kb(callback.from_user.id, pack["type"]),
        )
        await callback.answer()


@dp.callback_query(F.data.startswith("stkgrid:"), StickerStates.waiting_grid_size)
async def cb_grid_preset(callback: types.CallbackQuery, state: FSMContext):
    size_str = callback.data.split(":", 1)[1]
    cols, rows = [int(x) for x in size_str.split("x")]
    await state.update_data(grid_cols=cols, grid_rows=rows)
    await state.set_state(StickerStates.waiting_grid_photo)
    data = await state.get_data()
    back_cb = f"stkp:{data['active_pack']}" if data.get("grid_mode") == "add_existing" else "stk:menu"
    await callback.message.edit_text(
        f"📸 <b>Сетка {cols}×{rows} ({cols * rows} шт.) выбрана.</b>\n\n"
        "Теперь отправь картинку (как фото или файл PNG/JPG) — я аккуратно нарежу её и залью в набор.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data=back_cb)]]
        ),
    )
    await callback.answer()


@dp.message(StickerStates.waiting_grid_size, F.text)
async def msg_grid_custom_size(message: types.Message, state: FSMContext):
    txt = (message.text or "").strip().lower().replace("х", "x").replace("*", "x").replace(" ", "")
    m = re.fullmatch(r"(\d+)x(\d+)", txt)
    if not m:
        await message.answer("❌ Напиши размер в формате <code>8x3</code> (ширина×высота) или выбери кнопку выше.")
        return
    cols, rows = int(m.group(1)), int(m.group(2))
    if not (1 <= cols <= 8 and 1 <= rows <= 6) or (cols * rows < 2):
        await message.answer("❌ Допустимый размер: ширина от 1 до 8, высота от 1 до 6 (минимум 2 плитки).")
        return

    await state.update_data(grid_cols=cols, grid_rows=rows)
    await state.set_state(StickerStates.waiting_grid_photo)
    await message.answer(
        f"📸 <b>Сетка {cols}×{rows} ({cols * rows} шт.) выбрана.</b>\n\n"
        "Теперь отправь картинку (как фото или файл PNG/JPG)."
    )


@dp.message(StickerStates.waiting_grid_photo)
async def msg_grid_photo(message: types.Message, state: FSMContext):
    items, err = await extract_images_from_message(message)
    if err or not items:
        await message.answer(err or "❌ Отправь картинку (фото или файл).")
        return

    data = await state.get_data()
    cols = data.get("grid_cols", 8)
    rows = data.get("grid_rows", 2)
    mode = data.get("grid_mode", "create_new")
    raw_bytes = items[0][0]

    wait_msg = await message.answer(f"⏳ Нарезаю картинку на {cols * rows} фрагментов и загружаю в Telegram...")
    try:
        tiles = await asyncio.to_thread(slice_image_grid_sync, raw_bytes, cols, rows, True)

        if mode == "create_new":
            raw_title = data.get("new_pack_title", "Emoji Grid")
            is_prem = has_premium(message.from_user.id)
            short_name = make_pack_short_name()
            full_title = format_pack_title(raw_title, is_prem)

            input_stickers = [
                InputSticker(
                    sticker=BufferedInputFile(tile_bytes, filename=f"t{i}.png"),
                    emoji_list=["✨"],
                    format="static",
                )
                for i, tile_bytes in enumerate(tiles)
            ]
            await bot.create_new_sticker_set(
                user_id=message.from_user.id,
                name=short_name,
                title=full_title,
                stickers=input_stickers,
                sticker_type="custom_emoji",
            )
            pack_obj = {
                "name": short_name,
                "title": raw_title,
                "type": "custom_emoji",
                "is_premium": is_prem,
            }
            get_user_packs(message.from_user.id).append(pack_obj)
            STATE["stats"]["packs_created"] = STATE["stats"].get("packs_created", 0) + 1
            await save_state()
        else:
            pack_name = data["active_pack"]
            pack_obj = find_user_pack(message.from_user.id, pack_name)
            if not pack_obj:
                await wait_msg.edit_text("❌ Набор не найден.")
                await state.clear()
                return
            for i, tile_bytes in enumerate(tiles):
                st = InputSticker(
                    sticker=BufferedInputFile(tile_bytes, filename=f"t{i}.png"),
                    emoji_list=["✨"],
                    format="static",
                )
                await bot.add_sticker_to_set(
                    user_id=message.from_user.id,
                    name=pack_name,
                    sticker=st,
                )
                await asyncio.sleep(0.05)

        await state.clear()
        await wait_msg.edit_text(
            f"✅ <b>Готово! Добавлено {len(tiles)} эмодзи ({cols}×{rows}).</b>\n\n{pack_card_text(pack_obj)}",
            reply_markup=pack_card_kb(pack_obj),
        )
    except Exception as e:
        await wait_msg.edit_text(f"❌ Ошибка при нарезке/загрузке: <code>{str(e)[:300]}</code>")


@dp.message(StickerStates.waiting_add_item)
async def msg_add_item(message: types.Message, state: FSMContext):
    data = await state.get_data()
    pack_name = data.get("active_pack")
    pack = find_user_pack(message.from_user.id, pack_name) if pack_name else None
    if not pack:
        await state.clear()
        return

    items, err = await extract_images_from_message(message)
    if err:
        await message.answer(err)
        return

    is_emoji = pack["type"] == "custom_emoji"
    added = 0
    try:
        for idx, (raw_bytes, emoji) in enumerate(items):
            png_bytes = await asyncio.to_thread(image_to_sticker_png_sync, raw_bytes, is_emoji)
            st = InputSticker(
                sticker=BufferedInputFile(png_bytes, filename=f"a{idx}.png"),
                emoji_list=[emoji],
                format="static",
            )
            await bot.add_sticker_to_set(
                user_id=message.from_user.id,
                name=pack_name,
                sticker=st,
            )
            added += 1
            if len(items) > 1:
                await asyncio.sleep(0.05)

        await message.answer(
            f"✅ Добавлено: {added} шт. Отправь ещё или нажми <b>«Готово»</b>.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="✅ Готово", callback_data=f"stkp:{pack_name}")]]
            ),
        )
    except Exception as e:
        await message.answer(f"❌ Не удалось добавить: <code>{str(e)[:250]}</code>")


@dp.message(StickerStates.waiting_rename, F.text)
async def msg_rename_pack(message: types.Message, state: FSMContext):
    new_raw = (message.text or "").strip()[:45]
    if not new_raw or new_raw.startswith("/"):
        await message.answer("❌ Введи обычное название.")
        return

    data = await state.get_data()
    pack_name = data.get("active_pack")
    pack = find_user_pack(message.from_user.id, pack_name) if pack_name else None
    if not pack:
        await state.clear()
        return

    new_full = format_pack_title(new_raw, pack.get("is_premium", False))
    try:
        await bot.set_sticker_set_title(name=pack_name, title=new_full)
        pack["title"] = new_raw
        await save_state()
        await state.clear()
        await message.answer(
            f"✅ Название обновлено!\n\n{pack_card_text(pack)}",
            reply_markup=pack_card_kb(pack),
        )
    except Exception as e:
        await message.answer(f"❌ Не удалось изменить название: <code>{str(e)[:250]}</code>")


@dp.message(StickerStates.waiting_delete_sticker)
async def msg_delete_sticker(message: types.Message, state: FSMContext):
    data = await state.get_data()
    pack_name = data.get("active_pack")
    pack = find_user_pack(message.from_user.id, pack_name) if pack_name else None
    if not pack:
        await state.clear()
        return

    target_file_id = None
    if message.sticker:
        if message.sticker.set_name != pack_name:
            await message.answer("❌ Этот стикер из другого набора. Отправь стикер именно из выбранного пака.")
            return
        target_file_id = message.sticker.file_id
    elif message.entities:
        emoji_ids = [
            e.custom_emoji_id
            for e in message.entities
            if e.type == "custom_emoji" and e.custom_emoji_id
        ]
        if emoji_ids:
            stickers = await bot.get_custom_emoji_stickers([emoji_ids[0]])
            if stickers and stickers[0].set_name == pack_name:
                target_file_id = stickers[0].file_id
            else:
                await message.answer("❌ Этот эмодзи не из выбранного набора.")
                return

    if not target_file_id:
        await message.answer("❌ Отправь сам стикер или премиум-эмодзи из этого набора.")
        return

    try:
        await bot.delete_sticker_from_set(sticker=target_file_id)
        await state.clear()
        await message.answer(
            f"✅ Удалено из набора!\n\n{pack_card_text(pack)}",
            reply_markup=pack_card_kb(pack),
        )
    except Exception as e:
        await message.answer(f"❌ Не удалось удалить: <code>{str(e)[:250]}</code>")


# ==================== МЕНЮ АДМИН-ПАНЕЛИ ====================

def admin_menu_text() -> str:
    _refresh_today()
    st = STATE["stats"]
    today_total = st["today_inline"] + st["today_dm"]
    all_total = st["total_inline"] + st["total_dm"]
    ad_status = "🟢 ВКЛ" if STATE["ad_enabled"] else "🔴 ВЫКЛ"
    src_status = "🟢 ВКЛ" if STATE.get("source_btn_enabled", True) else "🔴 ВЫКЛ"
    return (
        f"⚙️ <b>Админ-панель</b>\n\n"
        f"👥 <b>Всего уникальных пользователей:</b> {len(STATE['all_users'])}\n"
        f"🔥 <b>Активных за сегодня:</b> {len(st['today_users'])}\n"
        f"📬 <b>В личке (для рассылки):</b> {len(STATE['users'])}\n\n"
        f"📊 <b>Скачиваний сегодня:</b> {today_total} "
        f"<i>(чаты: {st['today_inline']} | личка: {st['today_dm']})</i>\n"
        f"📈 <b>За всё время:</b> {all_total} "
        f"<i>(чаты: {st['total_inline']} | личка: {st['total_dm']})</i>\n"
        f"🎨 <b>Создано стикерпаков:</b> {st.get('packs_created', 0)}\n"
        f"👁 <b>Показов рекламы:</b> {st['ad_impressions']}\n\n"
        f"📢 <b>Подпись (реклама):</b> {ad_status}\n"
        f"🔗 <b>Кнопка-источник:</b> {src_status} <i>(текст меняется в «📝 Тексты бота»)</i>"
    )


def admin_menu_kb() -> InlineKeyboardMarkup:
    ad_toggle = "🔴 Выкл. подпись" if STATE["ad_enabled"] else "🟢 Вкл. подпись"
    src_toggle = "🔴 Выкл. кнопку-источник" if STATE.get("source_btn_enabled", True) else "🟢 Вкл. кнопку-источник"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=ad_toggle, callback_data="adm:toggle"),
                InlineKeyboardButton(text="✏️ Текст подписи", callback_data="adm:text"),
            ],
            [InlineKeyboardButton(text=src_toggle, callback_data="adm:toggle_src")],
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


def normalize_pinterest_url_sync(url: str) -> str:
    return re.sub(
        r"^https?://(?:[a-z0-9-]+\.)?pinterest\.[a-z.]+",
        "https://www.pinterest.com",
        url,
        flags=re.IGNORECASE,
    )


async def resolve_pinterest_url(url: str) -> str:
    if "pin.it" in url.lower():
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        }
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(url, allow_redirects=True) as resp:
                    url = str(resp.url)
        except Exception:
            pass
    return normalize_pinterest_url_sync(url)


def remux_hls_to_mp4_sync(m3u8_url: str, out_path: str) -> bool:
    cmd_copy = [
        "ffmpeg", "-y", "-i", m3u8_url,
        "-c", "copy", "-bsf:a", "aac_adtstoasc",
        "-movflags", "+faststart", out_path,
    ]
    res = subprocess.run(cmd_copy, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if res.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 10000:
        return True

    cmd_reencode = [
        "ffmpeg", "-y", "-i", m3u8_url,
        "-c:v", "libx264", "-preset", "fast", "-c:a", "aac",
        "-movflags", "+faststart", out_path,
    ]
    res2 = subprocess.run(cmd_reencode, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return res2.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 10000


async def download_pinterest_fallback(url: str) -> dict:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    temp_dir = tempfile.mkdtemp()
    try:
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url, allow_redirects=True) as resp:
                html_text = await resp.text(errors="ignore")

            cleaned = html_text.replace("\\/", "/").replace("\\u002F", "/")

            mp4_urls = re.findall(
                r"https://v[0-9]*\.pinimg\.com/videos/[^\s\"'<>\\]+\.mp4",
                cleaned,
                re.IGNORECASE,
            )
            if mp4_urls:
                mp4_urls.sort(key=lambda u: ("720p" in u or "expMp4" in u), reverse=True)
                for video_url in mp4_urls:
                    async with session.get(video_url) as vresp:
                        if vresp.status == 200:
                            vbytes = await vresp.read()
                            if len(vbytes) > 10000:
                                out_path = os.path.join(temp_dir, "video.mp4")
                                with open(out_path, "wb") as f:
                                    f.write(vbytes)
                                return {
                                    "path": out_path,
                                    "thumb_path": None,
                                    "temp_dir": temp_dir,
                                    "media_type": "video",
                                    "title": None,
                                    "performer": None,
                                    "duration": None,
                                }

            m3u8_urls = re.findall(
                r"https://v[0-9]*\.pinimg\.com/videos/[^\s\"'<>\\]+\.m3u8",
                cleaned,
                re.IGNORECASE,
            )
            if m3u8_urls:
                out_path = os.path.join(temp_dir, "video.mp4")
                for hls_url in m3u8_urls:
                    ok = await asyncio.to_thread(remux_hls_to_mp4_sync, hls_url, out_path)
                    if ok:
                        return {
                            "path": out_path,
                            "thumb_path": None,
                            "temp_dir": temp_dir,
                            "media_type": "video",
                            "title": None,
                            "performer": None,
                            "duration": None,
                        }

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
                    cleaned,
                )
                if m2:
                    img_url = m2.group(0)

            if not img_url:
                raise RuntimeError("Не удалось найти видео или картинку на странице Pinterest")

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
    target_url = await resolve_pinterest_url(url) if is_pinterest_url(url) else url
    try:
        return await asyncio.to_thread(download_ytdlp_sync, target_url, audio_mode)
    except Exception:
        if is_pinterest_url(target_url):
            return await download_pinterest_fallback(target_url)
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
        await callback.answer("Статус подписи изменён")

    elif action == "toggle_src":
        STATE["source_btn_enabled"] = not STATE.get("source_btn_enabled", True)
        await save_state()
        await callback.message.edit_text(
            admin_menu_text(), reply_markup=admin_menu_kb(), disable_web_page_preview=True
        )
        await callback.answer("Статус кнопки-источника изменён")

    elif action == "reset_imp":
        STATE["stats"]["ad_impressions"] = 0
        await save_state()
        await callback.message.edit_text(
            admin_menu_text(), reply_markup=admin_menu_kb(), disable_web_page_preview=True
        )
        await callback.answer("Счётчик показов сброшен")

    elif action == "preview":
        caption, kb, _ = get_media_payload("https://www.pinterest.com/", user_id=None)
        if not caption and not kb:
            await callback.answer("Сейчас и подпись, и кнопка-источник выключены", show_alert=True)
            return
        await callback.message.answer(
            caption or "<i>(подпись выключена, показана только кнопка-источник)</i>",
            reply_markup=kb,
            disable_web_page_preview=True,
        )
        await callback.answer()

    elif action == "text":
        await state.set_state(AdminStates.waiting_ad_text)
        await callback.message.edit_text(
            "✏️ <b>Отправь новый текст подписи (рекламы) под медиа</b>\n\n"
            "Можно использовать жирность, эмодзи и вшитые ссылки Telegram.\n"
            "Чтобы полностью убрать подпись, отправь <code>0</code>.",
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
        "✅ Текст подписи обновлён!\n\n" + admin_menu_text(),
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

        caption, kb, ad_shown = get_media_payload(url, user_id=user_id)
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
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await register_user(message.from_user.id, is_dm=True)
    text = STATE["texts"]["start_text"].replace("{username}", BOT_USERNAME)
    await message.answer(text, reply_markup=start_kb())


@dp.callback_query(F.data.startswith("scdl:"))
async def handle_dm_soundcloud_pick(callback: types.CallbackQuery):
    key = callback.data.split(":", 1)[1]
    url = SEARCH_CACHE.get(key)
    if not url:
        await callback.answer("Поиск устарел, напиши название ещё раз.", show_alert=True)
        return
    await callback.answer("Загружаю трек...")
    await send_downloaded_to_dm(callback.message, url, user_id=callback.from_user.id)


@dp.message(StateFilter(None), F.text)
async def handle_direct_message(message: types.Message):
    await register_user(message.from_user.id, is_dm=True)
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return

    match = MEDIA_RE.search(text)
    if match:
        await send_downloaded_to_dm(message, match.group(0), user_id=message.from_user.id)
        return

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

        caption, kb, ad_shown = get_media_payload(url, user_id=result.from_user.id)
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
