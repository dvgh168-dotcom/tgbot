import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from datetime import date

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
    InlineQueryResultCachedVideo,
    InputMediaVideo,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
STORAGE_CHAT_ID = int(os.getenv("STORAGE_CHAT_ID", "0"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

DATA_DIR = "data"
STATE_FILE = os.path.join(DATA_DIR, "state.json")
TIKTOK_RE = re.compile(r"https?://(?:www\.|vm\.|vt\.|m\.)?tiktok\.com/\S+")

# Максимум 4 одновременных скачивания, чтобы не перегружать VPS
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(4)
STATE_LOCK = asyncio.Lock()
LOADING_VIDEO_ID: str | None = None

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())


class AdminStates(StatesGroup):
    waiting_ad_text = State()
    waiting_ad_button = State()
    waiting_broadcast = State()


DEFAULT_STATE = {
    "ad_enabled": True,
    "ad_text": "🔥 Скачано через нашего бота",
    "ad_btn_text": "",
    "ad_btn_url": "",
    "stats": {
        "total_inline": 0,
        "total_dm": 0,
        "today_date": str(date.today()),
        "today_inline": 0,
        "today_dm": 0,
        "ad_impressions": 0,
    },
    "users": [],
}


def load_state_sync() -> dict:
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(STATE_FILE):
        return json.loads(json.dumps(DEFAULT_STATE))
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Дополняем недостающие ключи, если файл старый
        for k, v in DEFAULT_STATE.items():
            if k not in data:
                data[k] = json.loads(json.dumps(v))
        for k, v in DEFAULT_STATE["stats"].items():
            if k not in data["stats"]:
                data["stats"][k] = v
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


async def save_state():
    async with STATE_LOCK:
        _refresh_today()
        await asyncio.to_thread(save_state_sync, STATE)


async def register_user(user_id: int):
    if user_id not in STATE["users"]:
        STATE["users"].append(user_id)
        await save_state()


async def record_download(source: str, ad_shown: bool):
    _refresh_today()
    if source == "inline":
        STATE["stats"]["total_inline"] += 1
        STATE["stats"]["today_inline"] += 1
    else:
        STATE["stats"]["total_dm"] += 1
        STATE["stats"]["today_dm"] += 1
    if ad_shown:
        STATE["stats"]["ad_impressions"] += 1
    await save_state()


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
        f"📊 <b>Скачиваний сегодня:</b> {today_total} "
        f"<i>(чаты: {st['today_inline']} | личка: {st['today_dm']})</i>\n"
        f"📈 <b>За всё время:</b> {all_total} "
        f"<i>(чаты: {st['total_inline']} | личка: {st['total_dm']})</i>\n"
        f"👁 <b>Показов текущей рекламы:</b> {st['ad_impressions']}\n"
        f"👥 <b>Пользователей в личке (для рассылки):</b> {len(STATE['users'])}\n\n"
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
                InlineKeyboardButton(text="👁 Предпросмотр", callback_data="adm:preview"),
                InlineKeyboardButton(text="🔄 Сбросить показы", callback_data="adm:reset_imp"),
            ],
            [InlineKeyboardButton(text="📢 Сделать рассылку", callback_data="adm:broadcast")],
            [InlineKeyboardButton(text="🔁 Обновить статистику", callback_data="adm:refresh")],
        ]
    )


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="adm:cancel")]]
    )


async def init_loading_video():
    global LOADING_VIDEO_ID
    temp_dir = tempfile.mkdtemp()
    out_path = os.path.join(temp_dir, "loading.mp4")
    try:
        if os.path.exists("tiktok.png"):
            cmd = [
                "ffmpeg", "-y", "-loop", "1", "-i", "tiktok.png",
                "-t", "1", "-vf",
                "scale=320:320:force_original_aspect_ratio=decrease,pad=320:320:(ow-iw)/2:(oh-ih)/2",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", out_path,
            ]
        else:
            cmd = [
                "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=#2AABEE:s=320x320:d=1",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", out_path,
            ]
        await asyncio.to_thread(
            subprocess.run, cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        sent = await bot.send_video(chat_id=STORAGE_CHAT_ID, video=FSInputFile(out_path))
        LOADING_VIDEO_ID = sent.video.file_id
        await bot.delete_message(chat_id=STORAGE_CHAT_ID, message_id=sent.message_id)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def download_tiktok_sync(url: str) -> tuple[str, str]:
    temp_dir = tempfile.mkdtemp()
    out_template = os.path.join(temp_dir, "video.%(ext)s")
    ydl_opts = {
        "outtmpl": out_template,
        "format": "mp4[filesize<50M]/best[ext=mp4][filesize<50M]/best[filesize<50M]/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        files = os.listdir(temp_dir)
        if not files:
            raise RuntimeError("Файл не был скачан")
        return os.path.join(temp_dir, files[0]), temp_dir
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


async def notify_admin(url: str, error: Exception):
    if ADMIN_ID:
        try:
            await bot.send_message(
                ADMIN_ID,
                f"⚠️ <b>Ошибка скачивания TikTok</b>\n\n"
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

    if action == "cancel":
        await state.clear()
        await callback.message.edit_text(
            admin_menu_text(), reply_markup=admin_menu_kb(), disable_web_page_preview=True
        )
        await callback.answer("Отменено")

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
            "Можно использовать жирность, эмодзи и вшитые ссылки Telegram — всё сохранится.\n"
            "Чтобы полностью убрать текст (оставить только кнопку), отправь <code>0</code>.",
            reply_markup=cancel_kb(),
        )
        await callback.answer()

    elif action == "btn":
        await state.set_state(AdminStates.waiting_ad_button)
        await callback.message.edit_text(
            "🔗 <b>Настройка кнопки под видео</b>\n\n"
            "Отправь текст и ссылку через вертикальную черту:\n"
            "<code>⚡ ВСЕ СКИДКИ ТУТ | https://t.me/твой_канал</code>\n\n"
            "Чтобы полностью отключить кнопку, отправь <code>0</code>.",
            reply_markup=cancel_kb(),
        )
        await callback.answer()

    elif action == "broadcast":
        await state.set_state(AdminStates.waiting_broadcast)
        await callback.message.edit_text(
            f"📢 <b>Рассылка ({len(STATE['users'])} чел.)</b>\n\n"
            "Отправь сюда готовый пост (текст, фото или видео). "
            "Он сразу будет разослан всем пользователям, которые запускали бота в личке.",
            reply_markup=cancel_kb(),
        )
        await callback.answer()


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

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await register_user(message.from_user.id)
    me = await bot.get_me()
    await message.answer(
        f"👋 Привет! Я качаю видео из TikTok без водяного знака.\n\n"
        f"• Отправь ссылку прямо сюда\n"
        f"• Или введи <code>@{me.username} ссылка</code> в любом чате!"
    )


@dp.message(F.text)
async def handle_direct_link(message: types.Message):
    await register_user(message.from_user.id)
    match = TIKTOK_RE.search(message.text or "")
    if not match:
        return

    url = match.group(0)
    status_msg = await message.answer("⏳ Загружаю видео...")
    temp_dir = None
    try:
        async with DOWNLOAD_SEMAPHORE:
            file_path, temp_dir = await asyncio.to_thread(download_tiktok_sync, url)
        caption, kb, ad_shown = get_ad_payload()
        await message.answer_video(
            video=FSInputFile(file_path),
            caption=caption,
            reply_markup=kb,
        )
        await record_download("dm", ad_shown)
        await status_msg.delete()
    except Exception as e:
        await status_msg.edit_text("❌ Не удалось скачать это видео.")
        await notify_admin(url, e)
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


# ==================== ИНЛАЙН-РЕЖИМ В ЛЮБЫХ ЧАТАХ ====================

@dp.inline_query()
async def handle_inline_query(query: types.InlineQuery):
    match = TIKTOK_RE.search(query.query or "")
    if not match or not LOADING_VIDEO_ID:
        await query.answer([], cache_time=1)
        return

    loading_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏳ Загрузка видео...", callback_data="loading")]
        ]
    )
    result = InlineQueryResultCachedVideo(
        id=uuid.uuid4().hex[:16],
        video_file_id=LOADING_VIDEO_ID,
        title="💌 Поделиться видео из TikTok",
        caption="⏳ Загружаю видео...",
        reply_markup=loading_kb,
    )
    await query.answer([result], cache_time=1, is_personal=True)


@dp.chosen_inline_result()
async def handle_chosen_inline(result: types.ChosenInlineResult):
    if not result.inline_message_id:
        return

    match = TIKTOK_RE.search(result.query or "")
    if not match:
        return

    url = match.group(0)
    temp_dir = None
    try:
        async with DOWNLOAD_SEMAPHORE:
            file_path, temp_dir = await asyncio.to_thread(download_tiktok_sync, url)

        sent = await bot.send_video(
            chat_id=STORAGE_CHAT_ID, video=FSInputFile(file_path)
        )
        file_id = sent.video.file_id

        caption, kb, ad_shown = get_ad_payload()
        await bot.edit_message_media(
            inline_message_id=result.inline_message_id,
            media=InputMediaVideo(media=file_id, caption=caption),
            reply_markup=kb,
        )
        await bot.delete_message(chat_id=STORAGE_CHAT_ID, message_id=sent.message_id)
        await record_download("inline", ad_shown)
    except Exception as e:
        await notify_admin(url, e)
        try:
            await bot.edit_message_caption(
                inline_message_id=result.inline_message_id,
                caption="❌ Не удалось загрузить видео.",
            )
        except Exception:
            pass
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


@dp.callback_query(F.data == "loading")
async def handle_loading_click(callback: types.CallbackQuery):
    await callback.answer("Видео загружается, подождите 2–3 секунды...", show_alert=False)


async def main():
    await init_loading_video()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
