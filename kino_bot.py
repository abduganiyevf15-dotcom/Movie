"""
Kino Bot — Telegram kino tarqatish boti (BITTA faylda, aiogram 3 + SQLite)

ISHGA TUSHIRISH:
    pip install -r requirements.txt
    python kino_bot.py

SOZLASH:
    Pastdagi "SOZLAMALAR" bo'limida BOT_TOKEN va SUPER_ADMIN_ID ni to'g'ridan-to'g'ri
    yozib qo'yishingiz MUMKIN, yoki shu nomlar bilan muhit o'zgaruvchi (environment
    variable) sifatida bersangiz, ular avtomatik ustunlik qiladi (masalan Render'da).
"""

import asyncio
import logging
import os
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Union

import aiosqlite
from aiohttp import web
from dotenv import load_dotenv

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import BaseFilter, Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    KeyboardButton,
    Message,
    ReplyKeyboardRemove,
    TelegramObject,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

load_dotenv()


# ========================================================================
# SOZLAMALAR
# ========================================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
SUPER_ADMIN_ID = int(os.getenv("SUPER_ADMIN_ID", "123456789"))
DB_PATH = os.getenv("DB_PATH", "kino_bot.db")
MOVIES_PER_PAGE = 8
USERS_PER_PAGE = 10


# ========================================================================
# MA'LUMOTLAR BAZASI (SQLite)
# ========================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    full_name TEXT,
    joined_at TEXT,
    is_banned INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS movies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    file_id TEXT NOT NULL,
    added_by INTEGER,
    added_at TEXT,
    views INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS admins (
    user_id INTEGER PRIMARY KEY,
    added_by INTEGER,
    added_at TEXT
);

CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER UNIQUE NOT NULL,
    username TEXT,
    title TEXT,
    added_at TEXT
);
"""


async def init_db():
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.executescript(SCHEMA)
        await conn.commit()


# ---- users ----

async def add_user(user_id: int, username, full_name: str):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO users (user_id, username, full_name, joined_at) VALUES (?, ?, ?, ?)",
            (user_id, username, full_name, datetime.now().isoformat()),
        )
        await conn.commit()


async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return await cur.fetchone()


async def is_user_banned(user_id: int) -> bool:
    user = await get_user(user_id)
    return bool(user["is_banned"]) if user else False


async def ban_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("UPDATE users SET is_banned = 1 WHERE user_id = ?", (user_id,))
        await conn.commit()


async def unban_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("UPDATE users SET is_banned = 0 WHERE user_id = ?", (user_id,))
        await conn.commit()


async def count_users() -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM users")
        row = await cur.fetchone()
        return row[0]


async def get_all_user_ids() -> list:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("SELECT user_id FROM users WHERE is_banned = 0")
        rows = await cur.fetchall()
        return [r[0] for r in rows]


async def get_users_page(page: int, per_page: int):
    offset = page * per_page
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM users ORDER BY joined_at DESC LIMIT ? OFFSET ?",
            (per_page, offset),
        )
        return await cur.fetchall()


# ---- movies ----

async def add_movie(code: str, title: str, description, file_id: str, added_by: int) -> bool:
    try:
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute(
                "INSERT INTO movies (code, title, description, file_id, added_by, added_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (code, title, description, file_id, added_by, datetime.now().isoformat()),
            )
            await conn.commit()
        return True
    except aiosqlite.IntegrityError:
        return False


async def get_movie_by_code(code: str):
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM movies WHERE code = ?", (code,))
        return await cur.fetchone()


async def delete_movie(code: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("DELETE FROM movies WHERE code = ?", (code,))
        await conn.commit()
        return cur.rowcount > 0


async def update_movie_title(code: str, title: str):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("UPDATE movies SET title = ? WHERE code = ?", (title, code))
        await conn.commit()


async def update_movie_description(code: str, description: str):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("UPDATE movies SET description = ? WHERE code = ?", (description, code))
        await conn.commit()


async def increment_views(code: str):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("UPDATE movies SET views = views + 1 WHERE code = ?", (code,))
        await conn.commit()


async def count_movies() -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM movies")
        row = await cur.fetchone()
        return row[0]


async def get_movies_page(page: int, per_page: int):
    offset = page * per_page
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM movies ORDER BY id DESC LIMIT ? OFFSET ?",
            (per_page, offset),
        )
        return await cur.fetchall()


async def get_top_movies(limit: int = 5):
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM movies ORDER BY views DESC LIMIT ?", (limit,))
        return await cur.fetchall()


async def total_views() -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("SELECT COALESCE(SUM(views), 0) FROM movies")
        row = await cur.fetchone()
        return row[0]


# ---- admins ----

async def add_admin(user_id: int, added_by: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO admins (user_id, added_by, added_at) VALUES (?, ?, ?)",
            (user_id, added_by, datetime.now().isoformat()),
        )
        await conn.commit()


async def remove_admin(user_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
        await conn.commit()


async def is_admin(user_id: int) -> bool:
    if user_id == SUPER_ADMIN_ID:
        return True
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return row is not None


async def list_admins():
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM admins ORDER BY added_at")
        return await cur.fetchall()


# ---- channels (majburiy obuna) ----

async def add_channel(chat_id: int, username, title: str):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO channels (chat_id, username, title, added_at) VALUES (?, ?, ?, ?)",
            (chat_id, username, title, datetime.now().isoformat()),
        )
        await conn.commit()


async def remove_channel(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("DELETE FROM channels WHERE chat_id = ?", (chat_id,))
        await conn.commit()


async def list_channels():
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM channels")
        return await cur.fetchall()


# ========================================================================
# FSM HOLATLARI
# ========================================================================

class AddMovie(StatesGroup):
    waiting_for_video = State()
    waiting_for_code = State()
    waiting_for_title = State()
    waiting_for_description = State()


class EditMovie(StatesGroup):
    waiting_for_new_title = State()
    waiting_for_new_description = State()


class DeleteMovie(StatesGroup):
    waiting_for_code = State()


class Broadcast(StatesGroup):
    waiting_for_message = State()
    waiting_for_confirm = State()


class AddChannel(StatesGroup):
    waiting_for_channel = State()


class AddAdmin(StatesGroup):
    waiting_for_id = State()


# ========================================================================
# FILTER — faqat adminlar uchun
# ========================================================================

class IsAdmin(BaseFilter):
    async def __call__(self, event: Union[Message, CallbackQuery]) -> bool:
        return await is_admin(event.from_user.id)


# ========================================================================
# KLAVIATURALAR
# ========================================================================

def subscription_keyboard(channels):
    builder = InlineKeyboardBuilder()
    for ch in channels:
        if ch["username"]:
            builder.row(InlineKeyboardButton(text=f"📢 {ch['title']}", url=f"https://t.me/{ch['username']}"))
    builder.row(InlineKeyboardButton(text="✅ Tekshirish", callback_data="check_subscription"))
    return builder.as_markup()


def admin_main_menu():
    builder = ReplyKeyboardBuilder()
    builder.row(KeyboardButton(text="🎬 Kino qo'shish"), KeyboardButton(text="📋 Kinolar ro'yxati"))
    builder.row(KeyboardButton(text="🗑 Kino o'chirish"), KeyboardButton(text="📊 Statistika"))
    builder.row(KeyboardButton(text="📣 Xabar yuborish"), KeyboardButton(text="📡 Majburiy obuna"))
    builder.row(KeyboardButton(text="👮 Adminlar"), KeyboardButton(text="👥 Foydalanuvchilar"))
    builder.row(KeyboardButton(text="❌ Admin panelni yopish"))
    return builder.as_markup(resize_keyboard=True)


def cancel_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.row(KeyboardButton(text="◀️ Bekor qilish"))
    return builder.as_markup(resize_keyboard=True)


def skip_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.row(KeyboardButton(text="➡️ O'tkazib yuborish"))
    builder.row(KeyboardButton(text="◀️ Bekor qilish"))
    return builder.as_markup(resize_keyboard=True)


def confirm_broadcast_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Ha, yuborish", callback_data="broadcast_confirm"),
        InlineKeyboardButton(text="❌ Bekor qilish", callback_data="broadcast_cancel"),
    )
    return builder.as_markup()


def movies_list_keyboard(movies, page: int, total_pages: int):
    builder = InlineKeyboardBuilder()
    for m in movies:
        builder.row(InlineKeyboardButton(text=f"🎬 {m['title']} ({m['code']})", callback_data=f"movie_view_{m['code']}"))
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"movies_page_{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{max(total_pages, 1)}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"movies_page_{page + 1}"))
    if nav:
        builder.row(*nav)
    return builder.as_markup()


def movie_detail_keyboard(code: str):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✏️ Nomi", callback_data=f"movie_edit_title_{code}"),
        InlineKeyboardButton(text="📝 Tavsifi", callback_data=f"movie_edit_desc_{code}"),
    )
    builder.row(InlineKeyboardButton(text="🗑 O'chirish", callback_data=f"movie_delete_{code}"))
    builder.row(InlineKeyboardButton(text="⬅️ Ro'yxatga qaytish", callback_data="movies_page_0"))
    return builder.as_markup()


def confirm_delete_keyboard(code: str):
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Ha, o'chirish", callback_data=f"movie_delete_confirm_{code}"),
        InlineKeyboardButton(text="❌ Yo'q", callback_data=f"movie_view_{code}"),
    )
    return builder.as_markup()


def channels_list_keyboard(channels):
    builder = InlineKeyboardBuilder()
    for ch in channels:
        builder.row(InlineKeyboardButton(text=f"📢 {ch['title']} ❌ o'chirish", callback_data=f"channel_remove_{ch['chat_id']}"))
    builder.row(InlineKeyboardButton(text="➕ Kanal qo'shish", callback_data="channel_add"))
    return builder.as_markup()


def admins_list_keyboard(admins, super_admin_id: int):
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=f"👑 Super Admin: {super_admin_id}", callback_data="noop"))
    for a in admins:
        builder.row(InlineKeyboardButton(text=f"👮 {a['user_id']} ❌ o'chirish", callback_data=f"admin_remove_{a['user_id']}"))
    builder.row(InlineKeyboardButton(text="➕ Admin qo'shish", callback_data="admin_add"))
    return builder.as_markup()


def users_list_keyboard(users, page: int, total_pages: int):
    builder = InlineKeyboardBuilder()
    for u in users:
        status = "🚫" if u["is_banned"] else "✅"
        name = u["full_name"] or "Noma'lum"
        builder.row(InlineKeyboardButton(text=f"{status} {name} ({u['user_id']})", callback_data=f"user_view_{u['user_id']}"))
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"users_page_{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{max(total_pages, 1)}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"users_page_{page + 1}"))
    if nav:
        builder.row(*nav)
    return builder.as_markup()


def user_detail_keyboard(user_id: int, is_banned: bool):
    builder = InlineKeyboardBuilder()
    if is_banned:
        builder.row(InlineKeyboardButton(text="✅ Blokdan chiqarish", callback_data=f"user_unban_{user_id}"))
    else:
        builder.row(InlineKeyboardButton(text="🚫 Bloklash", callback_data=f"user_ban_{user_id}"))
    builder.row(InlineKeyboardButton(text="⬅️ Ro'yxatga qaytish", callback_data="users_page_0"))
    return builder.as_markup()


# ========================================================================
# MAJBURIY OBUNA MIDDLEWARE
# ========================================================================

class SubscriptionMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any],
    ) -> Any:
        user_id = event.from_user.id
        bot = data["bot"]

        if await is_admin(user_id):
            return await handler(event, data)

        channels = await list_channels()
        if not channels:
            return await handler(event, data)

        not_subscribed = []
        for ch in channels:
            try:
                member = await bot.get_chat_member(chat_id=ch["chat_id"], user_id=user_id)
                if member.status in ("left", "kicked"):
                    not_subscribed.append(ch)
            except TelegramBadRequest:
                continue

        if not_subscribed:
            await event.answer(
                "📛 Botdan foydalanish uchun quyidagi kanal(lar)ga obuna bo'ling, so'ng "
                '"✅ Tekshirish" tugmasini bosing:',
                reply_markup=subscription_keyboard(not_subscribed),
            )
            return

        return await handler(event, data)


# ========================================================================
# FOYDALANUVCHI ROUTERI
# ========================================================================

user_router = Router()


async def not_subscribed_channels(bot: Bot, user_id: int) -> list:
    channels = await list_channels()
    not_subscribed = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(chat_id=ch["chat_id"], user_id=user_id)
            if member.status in ("left", "kicked"):
                not_subscribed.append(ch)
        except TelegramBadRequest:
            continue
    return not_subscribed


@user_router.message(CommandStart())
async def cmd_start(message: Message):
    await add_user(message.from_user.id, message.from_user.username, message.from_user.full_name)

    if await is_user_banned(message.from_user.id):
        await message.answer("🚫 Siz botdan foydalanishdan bloklangansiz.")
        return

    await message.answer(
        f"🎬 Assalomu alaykum, {message.from_user.full_name}!\n\n"
        "Kerakli kino kodini yuboring, men sizga kinoni topib beraman.\n\n"
        "Masalan: <code>1001</code>"
    )


@user_router.callback_query(F.data == "check_subscription")
async def check_subscription(callback: CallbackQuery, bot: Bot):
    still_missing = await not_subscribed_channels(bot, callback.from_user.id)
    if still_missing:
        await callback.answer("❌ Siz hali barcha kanallarga obuna bo'lmagansiz!", show_alert=True)
        return
    await callback.message.delete()
    await callback.message.answer("✅ Rahmat! Endi kino kodini yuborishingiz mumkin.\n\nMasalan: <code>1001</code>")


@user_router.callback_query(F.data == "noop")
async def noop_callback(callback: CallbackQuery):
    await callback.answer()


@user_router.message(F.text)
async def resolve_movie_code(message: Message):
    if await is_user_banned(message.from_user.id):
        await message.answer("🚫 Siz botdan foydalanishdan bloklangansiz.")
        return

    code = message.text.strip()
    movie = await get_movie_by_code(code)
    if not movie:
        await message.answer("❌ Bunday kodga ega kino topilmadi. Kodni tekshirib qaytadan yuboring.")
        return

    caption = f"🎬 <b>{movie['title']}</b>\n\n"
    if movie["description"]:
        caption += f"{movie['description']}\n\n"
    caption += f"🔢 Kod: <code>{movie['code']}</code>\n👁 Ko'rishlar: {movie['views'] + 1}"

    await message.answer_video(video=movie["file_id"], caption=caption)
    await increment_views(code)


# ========================================================================
# ADMIN ROUTERI
# ========================================================================

admin_router = Router()
admin_router.message.filter(IsAdmin())
admin_router.callback_query.filter(IsAdmin())


# ---- admin panelga kirish / chiqish / bekor qilish ----

@admin_router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🛠 Admin panelga xush kelibsiz!", reply_markup=admin_main_menu())


@admin_router.message(F.text == "❌ Admin panelni yopish")
async def close_admin_panel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Admin panel yopildi.", reply_markup=ReplyKeyboardRemove())


@admin_router.message(F.text == "◀️ Bekor qilish", StateFilter("*"))
async def cancel_action(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Bekor qilindi.", reply_markup=admin_main_menu())


# ---- kino qo'shish ----

@admin_router.message(F.text == "🎬 Kino qo'shish")
async def add_movie_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(AddMovie.waiting_for_video)
    await message.answer("🎥 Kino videosini yuboring (video fayl sifatida):", reply_markup=cancel_keyboard())


@admin_router.message(AddMovie.waiting_for_video, F.video)
async def add_movie_video(message: Message, state: FSMContext):
    await state.update_data(file_id=message.video.file_id)
    await state.set_state(AddMovie.waiting_for_code)
    await message.answer("🔢 Kino uchun noyob kod kiriting (masalan: 1001):")


@admin_router.message(AddMovie.waiting_for_video)
async def add_movie_video_invalid(message: Message):
    await message.answer("⚠️ Iltimos, video fayl yuboring (yoki ◀️ Bekor qilish tugmasini bosing).")


@admin_router.message(AddMovie.waiting_for_code, F.text)
async def add_movie_code(message: Message, state: FSMContext):
    code = message.text.strip()
    if await get_movie_by_code(code):
        await message.answer("⚠️ Bu kod band. Boshqa kod kiriting:")
        return
    await state.update_data(code=code)
    await state.set_state(AddMovie.waiting_for_title)
    await message.answer("📝 Kino nomini kiriting:")


@admin_router.message(AddMovie.waiting_for_title, F.text)
async def add_movie_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await state.set_state(AddMovie.waiting_for_description)
    await message.answer(
        "ℹ️ Kino haqida qisqacha ma'lumot kiriting (yoki o'tkazib yuboring):",
        reply_markup=skip_keyboard(),
    )


@admin_router.message(AddMovie.waiting_for_description, F.text == "➡️ O'tkazib yuborish")
async def add_movie_desc_skip(message: Message, state: FSMContext):
    await _finish_add_movie(message, state, description=None)


@admin_router.message(AddMovie.waiting_for_description, F.text)
async def add_movie_desc(message: Message, state: FSMContext):
    await _finish_add_movie(message, state, description=message.text.strip())


async def _finish_add_movie(message: Message, state: FSMContext, description):
    data = await state.get_data()
    ok = await add_movie(
        code=data["code"],
        title=data["title"],
        description=description,
        file_id=data["file_id"],
        added_by=message.from_user.id,
    )
    await state.clear()
    if ok:
        await message.answer(
            f"✅ \"{data['title']}\" kino muvaffaqiyatli qo'shildi!\nKod: <code>{data['code']}</code>",
            reply_markup=admin_main_menu(),
        )
    else:
        await message.answer("❌ Xatolik yuz berdi, kod band bo'lib qolgan bo'lishi mumkin.", reply_markup=admin_main_menu())


# ---- kinolar ro'yxati ----

@admin_router.message(F.text == "📋 Kinolar ro'yxati")
async def movies_list(message: Message, state: FSMContext):
    await state.clear()
    total = await count_movies()
    if total == 0:
        await message.answer("📭 Hozircha kinolar yo'q.")
        return
    total_pages = max((total + MOVIES_PER_PAGE - 1) // MOVIES_PER_PAGE, 1)
    movies = await get_movies_page(0, MOVIES_PER_PAGE)
    await message.answer(f"🎞 Jami kinolar: {total}", reply_markup=movies_list_keyboard(movies, 0, total_pages))


@admin_router.callback_query(F.data.startswith("movies_page_"))
async def movies_page_callback(callback: CallbackQuery):
    page = int(callback.data.split("_")[-1])
    total = await count_movies()
    total_pages = max((total + MOVIES_PER_PAGE - 1) // MOVIES_PER_PAGE, 1)
    movies = await get_movies_page(page, MOVIES_PER_PAGE)
    try:
        await callback.message.edit_text(
            f"🎞 Jami kinolar: {total}", reply_markup=movies_list_keyboard(movies, page, total_pages)
        )
    except TelegramBadRequest:
        pass
    await callback.answer()


@admin_router.callback_query(F.data.startswith("movie_view_"))
async def movie_view_callback(callback: CallbackQuery):
    code = callback.data.removeprefix("movie_view_")
    movie = await get_movie_by_code(code)
    if not movie:
        await callback.answer("Kino topilmadi.", show_alert=True)
        return
    text = f"🎬 <b>{movie['title']}</b>\n🔢 Kod: <code>{movie['code']}</code>\n👁 Ko'rishlar: {movie['views']}"
    if movie["description"]:
        text += f"\n\n{movie['description']}"
    await callback.message.edit_text(text, reply_markup=movie_detail_keyboard(code))
    await callback.answer()


@admin_router.callback_query(F.data.startswith("movie_delete_confirm_"))
async def movie_delete_confirm(callback: CallbackQuery):
    code = callback.data.removeprefix("movie_delete_confirm_")
    await delete_movie(code)
    await callback.message.edit_text(f"🗑 Kino (kod: {code}) o'chirildi.")
    await callback.answer("O'chirildi")


@admin_router.callback_query(F.data.startswith("movie_delete_"))
async def movie_delete_ask(callback: CallbackQuery):
    code = callback.data.removeprefix("movie_delete_")
    await callback.message.edit_text(
        "❗️ Rostdan ham ushbu kinoni o'chirmoqchimisiz?", reply_markup=confirm_delete_keyboard(code)
    )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("movie_edit_title_"))
async def movie_edit_title_start(callback: CallbackQuery, state: FSMContext):
    code = callback.data.removeprefix("movie_edit_title_")
    await state.update_data(code=code)
    await state.set_state(EditMovie.waiting_for_new_title)
    await callback.message.answer("✏️ Yangi nomni kiriting:", reply_markup=cancel_keyboard())
    await callback.answer()


@admin_router.message(EditMovie.waiting_for_new_title, F.text)
async def movie_edit_title_save(message: Message, state: FSMContext):
    data = await state.get_data()
    await update_movie_title(data["code"], message.text.strip())
    await state.clear()
    await message.answer("✅ Nom yangilandi.", reply_markup=admin_main_menu())


@admin_router.callback_query(F.data.startswith("movie_edit_desc_"))
async def movie_edit_desc_start(callback: CallbackQuery, state: FSMContext):
    code = callback.data.removeprefix("movie_edit_desc_")
    await state.update_data(code=code)
    await state.set_state(EditMovie.waiting_for_new_description)
    await callback.message.answer("📝 Yangi tavsifni kiriting:", reply_markup=cancel_keyboard())
    await callback.answer()


@admin_router.message(EditMovie.waiting_for_new_description, F.text)
async def movie_edit_desc_save(message: Message, state: FSMContext):
    data = await state.get_data()
    await update_movie_description(data["code"], message.text.strip())
    await state.clear()
    await message.answer("✅ Tavsif yangilandi.", reply_markup=admin_main_menu())


# ---- tezkor o'chirish (kod orqali) ----

@admin_router.message(F.text == "🗑 Kino o'chirish")
async def delete_movie_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(DeleteMovie.waiting_for_code)
    await message.answer("🔢 O'chirmoqchi bo'lgan kino kodini kiriting:", reply_markup=cancel_keyboard())


@admin_router.message(DeleteMovie.waiting_for_code, F.text)
async def delete_movie_code(message: Message, state: FSMContext):
    code = message.text.strip()
    ok = await delete_movie(code)
    await state.clear()
    if ok:
        await message.answer(f"🗑 Kino (kod: {code}) o'chirildi.", reply_markup=admin_main_menu())
    else:
        await message.answer("❌ Bunday kodli kino topilmadi.", reply_markup=admin_main_menu())


# ---- statistika ----

@admin_router.message(F.text == "📊 Statistika")
async def statistics(message: Message, state: FSMContext):
    await state.clear()
    users_count = await count_users()
    movies_count = await count_movies()
    views = await total_views()
    top = await get_top_movies(5)

    text = (
        "📊 <b>Bot statistikasi</b>\n\n"
        f"👥 Foydalanuvchilar: {users_count}\n"
        f"🎬 Kinolar soni: {movies_count}\n"
        f"👁 Jami ko'rishlar: {views}\n"
    )
    if top:
        text += "\n🏆 <b>Eng ko'p ko'rilgan kinolar:</b>\n"
        for i, m in enumerate(top, 1):
            text += f"{i}. {m['title']} — {m['views']} ta ko'rish\n"

    await message.answer(text)


# ---- xabar yuborish (broadcast) ----

@admin_router.message(F.text == "📣 Xabar yuborish")
async def broadcast_start(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(Broadcast.waiting_for_message)
    await message.answer(
        "✉️ Barcha foydalanuvchilarga yubormoqchi bo'lgan xabaringizni yuboring "
        "(matn, rasm, video — istalgan turda bo'lishi mumkin):",
        reply_markup=cancel_keyboard(),
    )


@admin_router.message(Broadcast.waiting_for_message)
async def broadcast_preview(message: Message, state: FSMContext):
    await state.update_data(chat_id=message.chat.id, message_id=message.message_id)
    await state.set_state(Broadcast.waiting_for_confirm)
    await message.answer("⬆️ Xabar shu ko'rinishda barchaga yuboriladi. Tasdiqlaysizmi?", reply_markup=confirm_broadcast_keyboard())


@admin_router.callback_query(F.data == "broadcast_cancel", Broadcast.waiting_for_confirm)
async def broadcast_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Xabar yuborish bekor qilindi.")
    await callback.answer()


@admin_router.callback_query(F.data == "broadcast_confirm", Broadcast.waiting_for_confirm)
async def broadcast_confirm(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    await callback.message.edit_text("⏳ Xabar yuborilmoqda...")

    user_ids = await get_all_user_ids()
    sent, failed = 0, 0
    for uid in user_ids:
        try:
            await bot.copy_message(chat_id=uid, from_chat_id=data["chat_id"], message_id=data["message_id"])
            sent += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            failed += 1

    await callback.message.answer(
        f"✅ Xabar yuborildi!\n📨 Yetib bordi: {sent}\n🚫 Yetib bormadi: {failed}",
        reply_markup=admin_main_menu(),
    )
    await callback.answer()


# ---- majburiy obuna (kanallar) ----

@admin_router.message(F.text == "📡 Majburiy obuna")
async def channels_menu(message: Message, state: FSMContext):
    await state.clear()
    channels = await list_channels()
    text = "📡 Majburiy obuna kanallari:" if channels else "📡 Hozircha majburiy obuna kanallari qo'shilmagan."
    await message.answer(text, reply_markup=channels_list_keyboard(channels))


@admin_router.callback_query(F.data == "channel_add")
async def channel_add_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AddChannel.waiting_for_channel)
    await callback.message.answer(
        "➕ Botni kerakli kanalga <b>admin</b> qilib qo'shing, so'ngra kanal usernamesini "
        "(masalan: @mychannel) yoki ID raqamini yuboring:",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()


@admin_router.message(AddChannel.waiting_for_channel, F.text)
async def channel_add_save(message: Message, state: FSMContext, bot: Bot):
    value = message.text.strip()
    try:
        chat = await bot.get_chat(value)
    except TelegramBadRequest:
        await message.answer("❌ Kanal topilmadi. Username yoki ID to'g'riligini tekshiring.")
        return

    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(chat.id, me.id)
        if member.status not in ("administrator", "creator"):
            await message.answer("⚠️ Bot ushbu kanalda admin emas. Avval botni kanalga admin qiling.")
            return
    except TelegramBadRequest:
        await message.answer("⚠️ Botni kanalga admin sifatida qo'shing va qaytadan urinib ko'ring.")
        return

    await add_channel(chat.id, chat.username, chat.title)
    await state.clear()
    await message.answer(f"✅ \"{chat.title}\" kanali majburiy obunaga qo'shildi.", reply_markup=admin_main_menu())


@admin_router.callback_query(F.data.startswith("channel_remove_"))
async def channel_remove(callback: CallbackQuery):
    chat_id = int(callback.data.removeprefix("channel_remove_"))
    await remove_channel(chat_id)
    channels = await list_channels()
    text = "📡 Majburiy obuna kanallari:" if channels else "📡 Hozircha majburiy obuna kanallari qo'shilmagan."
    await callback.message.edit_text(text, reply_markup=channels_list_keyboard(channels))
    await callback.answer("O'chirildi")


# ---- adminlar boshqaruvi (faqat super admin) ----

@admin_router.message(F.text == "👮 Adminlar")
async def admins_menu(message: Message, state: FSMContext):
    await state.clear()
    if message.from_user.id != SUPER_ADMIN_ID:
        await message.answer("⛔️ Faqat super admin adminlarni boshqara oladi.")
        return
    admins = await list_admins()
    await message.answer("👮 Adminlar ro'yxati:", reply_markup=admins_list_keyboard(admins, SUPER_ADMIN_ID))


@admin_router.callback_query(F.data == "admin_add")
async def admin_add_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != SUPER_ADMIN_ID:
        await callback.answer("⛔️ Ruxsat yo'q.", show_alert=True)
        return
    await state.set_state(AddAdmin.waiting_for_id)
    await callback.message.answer(
        "➕ Yangi admin qilib tayinlamoqchi bo'lgan foydalanuvchining Telegram ID raqamini yuboring:",
        reply_markup=cancel_keyboard(),
    )
    await callback.answer()


@admin_router.message(AddAdmin.waiting_for_id, F.text)
async def admin_add_save(message: Message, state: FSMContext):
    try:
        new_admin_id = int(message.text.strip())
    except ValueError:
        await message.answer("⚠️ Iltimos, faqat ID raqam yuboring.")
        return
    await add_admin(new_admin_id, message.from_user.id)
    await state.clear()
    await message.answer(f"✅ {new_admin_id} endi admin.", reply_markup=admin_main_menu())


@admin_router.callback_query(F.data.startswith("admin_remove_"))
async def admin_remove(callback: CallbackQuery):
    if callback.from_user.id != SUPER_ADMIN_ID:
        await callback.answer("⛔️ Ruxsat yo'q.", show_alert=True)
        return
    user_id = int(callback.data.removeprefix("admin_remove_"))
    await remove_admin(user_id)
    admins = await list_admins()
    await callback.message.edit_text("👮 Adminlar ro'yxati:", reply_markup=admins_list_keyboard(admins, SUPER_ADMIN_ID))
    await callback.answer("O'chirildi")


# ---- foydalanuvchilar boshqaruvi ----

@admin_router.message(F.text == "👥 Foydalanuvchilar")
async def users_menu(message: Message, state: FSMContext):
    await state.clear()
    total = await count_users()
    total_pages = max((total + USERS_PER_PAGE - 1) // USERS_PER_PAGE, 1)
    users = await get_users_page(0, USERS_PER_PAGE)
    await message.answer(f"👥 Jami foydalanuvchilar: {total}", reply_markup=users_list_keyboard(users, 0, total_pages))


@admin_router.callback_query(F.data.startswith("users_page_"))
async def users_page_callback(callback: CallbackQuery):
    page = int(callback.data.split("_")[-1])
    total = await count_users()
    total_pages = max((total + USERS_PER_PAGE - 1) // USERS_PER_PAGE, 1)
    users = await get_users_page(page, USERS_PER_PAGE)
    try:
        await callback.message.edit_text(
            f"👥 Jami foydalanuvchilar: {total}", reply_markup=users_list_keyboard(users, page, total_pages)
        )
    except TelegramBadRequest:
        pass
    await callback.answer()


@admin_router.callback_query(F.data.startswith("user_view_"))
async def user_view(callback: CallbackQuery):
    user_id = int(callback.data.removeprefix("user_view_"))
    user = await get_user(user_id)
    if not user:
        await callback.answer("Topilmadi.", show_alert=True)
        return
    status = "🚫 Bloklangan" if user["is_banned"] else "✅ Faol"
    text = (
        f"👤 <b>{user['full_name']}</b>\n"
        f"🆔 ID: <code>{user['user_id']}</code>\n"
        f"📛 Username: @{user['username'] if user['username'] else '—'}\n"
        f"📅 Qo'shilgan: {user['joined_at'][:10]}\n"
        f"Holati: {status}"
    )
    await callback.message.edit_text(text, reply_markup=user_detail_keyboard(user_id, bool(user["is_banned"])))
    await callback.answer()


@admin_router.callback_query(F.data.startswith("user_ban_"))
async def user_ban(callback: CallbackQuery):
    user_id = int(callback.data.removeprefix("user_ban_"))
    await ban_user(user_id)
    user = await get_user(user_id)
    text = f"👤 <b>{user['full_name']}</b>\n🆔 ID: <code>{user['user_id']}</code>\nHolati: 🚫 Bloklangan"
    await callback.message.edit_text(text, reply_markup=user_detail_keyboard(user_id, True))
    await callback.answer("🚫 Bloklandi")


@admin_router.callback_query(F.data.startswith("user_unban_"))
async def user_unban(callback: CallbackQuery):
    user_id = int(callback.data.removeprefix("user_unban_"))
    await unban_user(user_id)
    user = await get_user(user_id)
    text = f"👤 <b>{user['full_name']}</b>\n🆔 ID: <code>{user['user_id']}</code>\nHolati: ✅ Faol"
    await callback.message.edit_text(text, reply_markup=user_detail_keyboard(user_id, False))
    await callback.answer("✅ Blokdan chiqarildi")


# ========================================================================
# KEEP-ALIVE SERVER — Render "Web Service" turida ishlaganda kerak bo'ladi.
# PORT muhit o'zgaruvchisi bo'lmasa (masalan kompyuteringizda), bu shunchaki
# ishga tushmay o'tib ketadi.
# ========================================================================

async def _ping(request: web.Request) -> web.Response:
    return web.Response(text="Kino bot ishlayapti ✅")


async def start_keepalive_server():
    port = os.environ.get("PORT")
    if not port:
        return
    app = web.Application()
    app.router.add_get("/", _ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=int(port))
    await site.start()
    logging.info(f"Keep-alive server {port}-portda ishga tushdi.")


# ========================================================================
# ISHGA TUSHIRISH
# ========================================================================

async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    await init_db()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())

    dp.message.middleware(SubscriptionMiddleware())

    # admin_router BIRINCHI bo'lishi SHART, aks holda admin menyu tugmalari
    # oddiy "kino kodi qidirish" handleriga tushib qoladi.
    dp.include_router(admin_router)
    dp.include_router(user_router)

    await start_keepalive_server()

    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Bot ishga tushdi...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot to'xtatildi.")
