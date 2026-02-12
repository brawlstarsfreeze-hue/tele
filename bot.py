import asyncio
import json
import re
from datetime import datetime

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.default import DefaultBotProperties

from config import BOT_TOKEN, ADMIN_IDS, ADMIN_USERNAME, DB_PATH


# ----------------- DB -----------------
async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        PRAGMA foreign_keys = ON;

        CREATE TABLE IF NOT EXISTS products(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            price INTEGER NOT NULL,
            description TEXT NOT NULL,
            photo_file_id TEXT NOT NULL,
            variants_json TEXT NOT NULL DEFAULT '[]',
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS cart(
            user_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            variant TEXT NOT NULL DEFAULT '',
            qty INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(user_id, product_id, variant),
            FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            full_name TEXT NOT NULL,
            phone TEXT NOT NULL,
            city TEXT NOT NULL,
            np_type TEXT NOT NULL,
            np_point TEXT NOT NULL,
            payment TEXT NOT NULL,
            comment TEXT NOT NULL,
            total INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS order_items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            variant TEXT NOT NULL,
            price INTEGER NOT NULL,
            qty INTEGER NOT NULL,
            FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
        );
        """)
        await db.commit()


async def db_fetchone(query, args=()):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(query, args)
        row = await cur.fetchone()
        await cur.close()
        return row


async def db_fetchall(query, args=()):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(query, args)
        rows = await cur.fetchall()
        await cur.close()
        return rows


async def db_execute(query, args=()):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(query, args)
        await db.commit()


# ----------------- Helpers -----------------
def is_admin_user(user_id: int, username: str | None) -> bool:
    if user_id in ADMIN_IDS:
        return True
    if username and username.lower() == ADMIN_USERNAME.lower():
        return True
    return False


def money(uah: int) -> str:
    return f"{uah} грн"


def parse_variants(text: str) -> list[str]:
    t = text.strip()
    if t in ("-", ""):
        return []
    parts = re.split(r"[,; ]+", t)
    variants = [p.strip() for p in parts if p.strip()]
    seen = set()
    res = []
    for v in variants:
        if v not in seen:
            seen.add(v)
            res.append(v)
    return res


def main_kb(is_admin: bool) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="🛍 Каталог", callback_data="cat:0")],
        [InlineKeyboardButton(text="🧺 Кошик", callback_data="cart:view")],
        [InlineKeyboardButton(text="ℹ️ Оплата/Доставка", callback_data="info")],
        [InlineKeyboardButton(text="📞 Контакти", callback_data="contacts")],
    ]
    if is_admin:
        buttons.append([InlineKeyboardButton(text="🔧 Адмін-меню", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Додати товар", callback_data="admin:add")],
        [InlineKeyboardButton(text="📦 Мої товари", callback_data="admin:products:0")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="home")],
    ])


def back_home_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ На головну", callback_data="home")]
    ])


async def safe_edit_text(c: CallbackQuery, text: str, reply_markup=None):
    """
    Не падає, якщо повідомлення було фото/без тексту.
    """
    try:
        if c.message and c.message.text is not None:
            await c.message.edit_text(text, reply_markup=reply_markup)
        else:
            await c.message.answer(text, reply_markup=reply_markup)
    except Exception:
        await c.message.answer(text, reply_markup=reply_markup)


async def send_photo_or_document(m: Message, file_id: str, caption: str, reply_markup=None):
    """
    Фікс "unsupported file type":
    - пробуємо відправити як фото
    - якщо Telegram відмовив — відправляємо як документ
    """
    try:
        await m.answer_photo(photo=file_id, caption=caption, reply_markup=reply_markup)
    except Exception as e:
        print(f"⚠️ answer_photo failed, fallback to document: {e}")
        await m.answer_document(document=file_id, caption=caption, reply_markup=reply_markup)


# ----------------- FSM -----------------
class AddProduct(StatesGroup):
    photo = State()
    title = State()
    price = State()
    description = State()
    variants = State()


class Checkout(StatesGroup):
    full_name = State()
    phone = State()
    city = State()
    np_type = State()
    np_point = State()
    payment = State()
    comment = State()
    confirm = State()


# ----------------- Router -----------------
router = Router()
PAGE_SIZE = 5


# ----------------- Start + /myid -----------------
@router.message(CommandStart())
async def start(m: Message):
    admin = is_admin_user(m.from_user.id, m.from_user.username)
    await m.answer(
        "👋 Привіт! Це наш магазин у Telegram.\n\n"
        "Обирай товари в каталозі та оформлюй замовлення 💙",
        reply_markup=main_kb(admin)
    )


@router.message(F.text == "/myid")
async def myid(m: Message):
    await m.answer(f"✅ Твій Telegram ID: {m.from_user.id}\nUsername: @{m.from_user.username}")


# ----------------- Home/Info/Contacts -----------------
@router.callback_query(F.data == "home")
async def home(c: CallbackQuery):
    admin = is_admin_user(c.from_user.id, c.from_user.username)
    await safe_edit_text(c, "🏠 Головне меню", reply_markup=main_kb(admin))
    await c.answer()


@router.callback_query(F.data == "info")
async def info(c: CallbackQuery):
    await safe_edit_text(
        c,
        "Оплата/Доставка\n\n"
        "• Доставка: Нова Пошта\n"
        "• Оплата: накладений платіж або передоплата (за домовленістю)\n\n"
        "Після оформлення замовлення ми зв’яжемось, якщо потрібно уточнити деталі.",
        reply_markup=back_home_kb()
    )
    await c.answer()


@router.callback_query(F.data == "contacts")
async def contacts(c: CallbackQuery):
    await safe_edit_text(
        c,
        "Контакти\n\n"
        "Напиши нам у Telegram або відповідай тут у боті після замовлення.\n"
        f"Адмін: @{ADMIN_USERNAME}",
        reply_markup=back_home_kb()
    )
    await c.answer()


# ----------------- Catalog -----------------
async def send_product_card(chat_msg: Message, prod):
    pid, title, price, desc, photo_id, variants_json = prod
    variants = json.loads(variants_json or "[]")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧺 Додати в кошик", callback_data=f"cart:add:{pid}")],
        [InlineKeyboardButton(text="🧺 Відкрити кошик", callback_data="cart:view")],
        [InlineKeyboardButton(text="⬅️ Назад до каталогу", callback_data="cat:0")],
        [InlineKeyboardButton(text="🏠 На головну", callback_data="home")],
    ])

    cap = f"{title}\n💰 {money(price)}\n\n{desc}"
    if variants:
        cap += "\n\n📏 Розміри/варіанти: " + ", ".join(variants)

    await send_photo_or_document(chat_msg, photo_id, cap, reply_markup=kb)


@router.callback_query(F.data.startswith("cat:"))
async def catalog(c: CallbackQuery):
    page = int(c.data.split(":")[1])
    offset = page * PAGE_SIZE

    rows = await db_fetchall(
        "SELECT id, title, price FROM products WHERE active=1 ORDER BY id DESC LIMIT ? OFFSET ?",
        (PAGE_SIZE, offset)
    )

    if not rows:
        await safe_edit_text(c, "Каталог порожній 😕", reply_markup=back_home_kb())
        await c.answer()
        return

    text = "🛍 Каталог товарів\n\n"
    kb_rows = []

    for pid, title, price in rows:
        text += f"• {title} — {money(price)}\n"
        kb_rows.append([InlineKeyboardButton(text=f"🔎 {title}", callback_data=f"prod:{pid}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"cat:{page-1}"))
    nav.append(InlineKeyboardButton(text="🧺 Кошик", callback_data="cart:view"))
    nav.append(InlineKeyboardButton(text="🏠", callback_data="home"))
    nav.append(InlineKeyboardButton(text="➡️", callback_data=f"cat:{page+1}"))
    kb_rows.append(nav)

    await safe_edit_text(c, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await c.answer()


@router.callback_query(F.data.startswith("prod:"))
async def product_view(c: CallbackQuery):
    pid = int(c.data.split(":")[1])
    prod = await db_fetchone(
        "SELECT id, title, price, description, photo_file_id, variants_json FROM products WHERE id=? AND active=1",
        (pid,)
    )
    if not prod:
        await c.answer("Товар не знайдено", show_alert=True)
        return

    await send_product_card(chat_msg=c.message, prod=prod)
    await c.answer()


# ----------------- Cart -----------------
async def cart_total(user_id: int) -> int:
    rows = await db_fetchall("""
        SELECT c.qty, p.price
        FROM cart c
        JOIN products p ON p.id=c.product_id
        WHERE c.user_id=? AND p.active=1
    """, (user_id,))
    return sum(qty * price for qty, price in rows)


async def cart_items(user_id: int):
    return await db_fetchall("""
        SELECT c.product_id, p.title, p.price, c.qty, c.variant
        FROM cart c
        JOIN products p ON p.id=c.product_id
        WHERE c.user_id=? AND p.active=1
        ORDER BY p.id DESC
    """, (user_id,))


@router.callback_query(F.data == "cart:view")
async def cart_view(c: CallbackQuery):
    items = await cart_items(c.from_user.id)

    if not items:
        await safe_edit_text(
            c,
            "🧺 Кошик порожній.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🛍 Перейти в каталог", callback_data="cat:0")],
                [InlineKeyboardButton(text="🏠 На головну", callback_data="home")],
            ])
        )
        await c.answer()
        return

    total = await cart_total(c.from_user.id)

    text = "🧺 Твій кошик:\n\n"
    kb_rows = []

    for pid, title, price, qty, variant in items:
        vtxt = f" ({variant})" if variant else ""
        text += f"• {title}{vtxt} — {money(price)} × {qty} = {money(price * qty)}\n"
        kb_rows.append([
            InlineKeyboardButton(text="➖", callback_data=f"cart:dec:{pid}:{variant}"),
            InlineKeyboardButton(text=f"{qty}", callback_data="noop"),
            InlineKeyboardButton(text="➕", callback_data=f"cart:inc:{pid}:{variant}"),
            InlineKeyboardButton(text="🗑", callback_data=f"cart:del:{pid}:{variant}"),
        ])

    text += f"\nРазом: {money(total)}"

    kb_rows.append([InlineKeyboardButton(text="✅ Оформити", callback_data="checkout:start")])
    kb_rows.append([
        InlineKeyboardButton(text="🛍 Каталог", callback_data="cat:0"),
        InlineKeyboardButton(text="🏠", callback_data="home")
    ])
    kb_rows.append([InlineKeyboardButton(text="🧹 Очистити", callback_data="cart:clear")])

    await safe_edit_text(c, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await c.answer()


@router.callback_query(F.data == "noop")
async def noop(c: CallbackQuery):
    await c.answer()


@router.callback_query(F.data == "cart:clear")
async def cart_clear(c: CallbackQuery):
    await db_execute("DELETE FROM cart WHERE user_id=?", (c.from_user.id,))
    await c.answer("Кошик очищено ✅")
    await cart_view(c)


@router.callback_query(F.data.startswith("cart:add:"))
async def cart_add(c: CallbackQuery):
    pid = int(c.data.split(":")[2])
    prod = await db_fetchone("SELECT variants_json FROM products WHERE id=? AND active=1", (pid,))
    if not prod:
        await c.answer("Товар не знайдено", show_alert=True)
        return

    variants = json.loads(prod[0] or "[]")

    if variants:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=v, callback_data=f"cart:addv:{pid}:{v}")] for v in variants
        ] + [[InlineKeyboardButton(text="⬅️ Назад", callback_data=f"prod:{pid}")]])
        await c.message.answer("Обери розмір/варіант:", reply_markup=kb)
        await c.answer()
        return

    await db_execute("""
        INSERT INTO cart(user_id, product_id, variant, qty)
        VALUES(?,?,?,1)
        ON CONFLICT(user_id, product_id, variant)
        DO UPDATE SET qty=qty+1
    """, (c.from_user.id, pid, ""))
    await c.answer("Додано в кошик ✅")


@router.callback_query(F.data.startswith("cart:addv:"))
async def cart_add_variant(c: CallbackQuery):
    _, _, pid_s, variant = c.data.split(":", 3)
    pid = int(pid_s)

    await db_execute("""
        INSERT INTO cart(user_id, product_id, variant, qty)
        VALUES(?,?,?,1)
        ON CONFLICT(user_id, product_id, variant)
        DO UPDATE SET qty=qty+1
    """, (c.from_user.id, pid, variant))

    await c.answer(f"Додано ({variant}) ✅")
    await cart_view(c)


@router.callback_query(F.data.startswith("cart:inc:"))
async def cart_inc(c: CallbackQuery):
    _, _, pid_s, variant = c.data.split(":", 3)
    pid = int(pid_s)
    await db_execute("UPDATE cart SET qty=qty+1 WHERE user_id=? AND product_id=? AND variant=?",
                     (c.from_user.id, pid, variant))
    await c.answer()
    await cart_view(c)


@router.callback_query(F.data.startswith("cart:dec:"))
async def cart_dec(c: CallbackQuery):
    _, _, pid_s, variant = c.data.split(":", 3)
    pid = int(pid_s)

    row = await db_fetchone("SELECT qty FROM cart WHERE user_id=? AND product_id=? AND variant=?",
                            (c.from_user.id, pid, variant))
    if not row:
        await c.answer()
        return

    qty = row[0]
    if qty <= 1:
        await db_execute("DELETE FROM cart WHERE user_id=? AND product_id=? AND variant=?",
                         (c.from_user.id, pid, variant))
    else:
        await db_execute("UPDATE cart SET qty=qty-1 WHERE user_id=? AND product_id=? AND variant=?",
                         (c.from_user.id, pid, variant))

    await c.answer()
    await cart_view(c)


@router.callback_query(F.data.startswith("cart:del:"))
async def cart_del(c: CallbackQuery):
    _, _, pid_s, variant = c.data.split(":", 3)
    pid = int(pid_s)
    await db_execute("DELETE FROM cart WHERE user_id=? AND product_id=? AND variant=?",
                     (c.from_user.id, pid, variant))
    await c.answer("Видалено 🗑")
    await cart_view(c)


# ----------------- Checkout -----------------
class Checkout(StatesGroup):
    full_name = State()
    phone = State()
    city = State()
    np_type = State()
    np_point = State()
    payment = State()
    comment = State()
    confirm = State()


@router.callback_query(F.data == "checkout:start")
async def checkout_start(c: CallbackQuery, state: FSMContext):
    items = await cart_items(c.from_user.id)
    if not items:
        await c.answer("Кошик порожній", show_alert=True)
        return

    await state.clear()
    await state.set_state(Checkout.full_name)
    await c.message.answer("✍️ Введи Ім’я та Прізвище:")
    await c.answer()


@router.message(Checkout.full_name)
async def co_full_name(m: Message, state: FSMContext):
    await state.update_data(full_name=m.text.strip())
    await state.set_state(Checkout.phone)
    await m.answer("📞 Введи номер телефону (наприклад: 0981234567):")


@router.message(Checkout.phone)
async def co_phone(m: Message, state: FSMContext):
    phone = re.sub(r"[^\d+]", "", m.text.strip())
    if len(re.sub(r"\D", "", phone)) < 9:
        await m.answer("❗️Схоже на неправильний номер. Спробуй ще раз.")
        return
    await state.update_data(phone=phone)
    await state.set_state(Checkout.city)
    await m.answer("🏙 Введи місто:")


@router.message(Checkout.city)
async def co_city(m: Message, state: FSMContext):
    await state.update_data(city=m.text.strip())
    await state.set_state(Checkout.np_type)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏤 Відділення", callback_data="np:type:branch")],
        [InlineKeyboardButton(text="📦 Поштомат", callback_data="np:type:locker")],
    ])
    await m.answer("Нова Пошта: обери тип доставки:", reply_markup=kb)


@router.callback_query(Checkout.np_type, F.data.startswith("np:type:"))
async def co_np_type(c: CallbackQuery, state: FSMContext):
    np_type = c.data.split(":")[2]
    await state.update_data(np_type=np_type)
    await state.set_state(Checkout.np_point)

    if np_type == "branch":
        await c.message.answer("🏤 Введи номер відділення або адресу відділення НП:")
    else:
        await c.message.answer("📦 Введи номер поштомату або адресу поштомату НП:")

    await c.answer()


@router.message(Checkout.np_point)
async def co_np_point(m: Message, state: FSMContext):
    await state.update_data(np_point=m.text.strip())
    await state.set_state(Checkout.payment)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💵 Накладений платіж", callback_data="pay:cod")],
        [InlineKeyboardButton(text="💳 Передоплата", callback_data="pay:prepay")],
    ])
    await m.answer("💳 Обери оплату:", reply_markup=kb)


@router.callback_query(Checkout.payment, F.data.startswith("pay:"))
async def co_payment(c: CallbackQuery, state: FSMContext):
    p = c.data.split(":")[1]
    payment = "Накладений платіж" if p == "cod" else "Передоплата"
    await state.update_data(payment=payment)
    await state.set_state(Checkout.comment)
    await c.message.answer("📝 Коментар (якщо нема — напиши - ):")
    await c.answer()


@router.message(Checkout.comment)
async def co_comment(m: Message, state: FSMContext):
    await state.update_data(comment=m.text.strip())
    data = await state.get_data()

    items = await cart_items(m.from_user.id)
    total = await cart_total(m.from_user.id)

    lines = []
    for pid, title, price, qty, variant in items:
        vtxt = f" ({variant})" if variant else ""
        lines.append(f"• {title}{vtxt} — {money(price)} × {qty}")

    np_type = "Відділення" if data["np_type"] == "branch" else "Поштомат"

    preview = (
        "✅ Підтверди замовлення\n\n"
        "🧺 Товари:\n" + "\n".join(lines) + "\n\n"
        f"💰 Разом: {money(total)}\n\n"
        f"👤 ПІБ: {data['full_name']}\n"
        f"📞 Телефон: {data['phone']}\n"
        f"🏙 Місто: {data['city']}\n"
        f"🚚 НП: {np_type} — {data['np_point']}\n"
        f"💳 Оплата: {data['payment']}\n"
        f"📝 Коментар: {data['comment']}"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Підтвердити", callback_data="checkout:confirm")],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data="checkout:cancel")],
    ])
    await state.set_state(Checkout.confirm)
    await m.answer(preview, reply_markup=kb)


@router.callback_query(Checkout.confirm, F.data == "checkout:cancel")
async def checkout_cancel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.answer("Скасовано ✅", reply_markup=back_home_kb())
    await c.answer()


@router.callback_query(Checkout.confirm, F.data == "checkout:confirm")
async def checkout_confirm(c: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    items = await cart_items(c.from_user.id)
    total = await cart_total(c.from_user.id)

    if not items:
        await c.answer("Кошик порожній", show_alert=True)
        return

    created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    username = c.from_user.username or ""
    np_type_text = "Відділення" if data["np_type"] == "branch" else "Поштомат"

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            INSERT INTO orders(user_id, username, full_name, phone, city, np_type, np_point, payment, comment, total, created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
        """, (
            c.from_user.id, username, data["full_name"], data["phone"], data["city"],
            np_type_text, data["np_point"], data["payment"], data["comment"], total, created
        ))
        order_id = cur.lastrowid

        for pid, title, price, qty, variant in items:
            await db.execute("""
                INSERT INTO order_items(order_id, product_id, title, variant, price, qty)
                VALUES(?,?,?,?,?,?)
            """, (order_id, pid, title, variant or "", price, qty))

        await db.execute("DELETE FROM cart WHERE user_id=?", (c.from_user.id,))
        await db.commit()

    lines = []
    for pid, title, price, qty, variant in items:
        vtxt = f" ({variant})" if variant else ""
        lines.append(f"• {title}{vtxt} — {money(price)} × {qty}")

    admin_text = (
        f"🛒 НОВЕ ЗАМОВЛЕННЯ #{order_id}\n\n"
        "🧺 Товари:\n" + "\n".join(lines) + "\n\n"
        f"💰 Разом: {money(total)}\n\n"
        f"👤 ПІБ: {data['full_name']}\n"
        f"📞 Телефон: {data['phone']}\n"
        f"🏙 Місто: {data['city']}\n"
        f"🚚 НП: {np_type_text} — {data['np_point']}\n"
        f"💳 Оплата: {data['payment']}\n"
        f"📝 Коментар: {data['comment']}\n"
    )
    if username:
        admin_text += f"\n👤 Telegram: @{username}"

    sent = False
    for admin_id in ADMIN_IDS:
        if admin_id and admin_id != 0:
            try:
                await bot.send_message(admin_id, admin_text)
                print(f"✅ Sent to admin_id={admin_id}")
                sent = True
                break
            except Exception as e:
                print(f"❌ Failed to send to admin_id={admin_id}: {e}")

    if not sent:
        try:
            await bot.send_message(c.message.chat.id, admin_text)
            print(f"✅ Fallback sent to current chat_id={c.message.chat.id}")
        except Exception as e:
            print(f"❌ Fallback failed: {e}")

    await state.clear()
    await c.message.answer(f"✅ Замовлення оформлено! Номер: #{order_id}", reply_markup=back_home_kb())
    await c.answer("Готово ✅")


# ----------------- Admin -----------------
@router.callback_query(F.data == "admin:menu")
async def admin_menu(c: CallbackQuery):
    if not is_admin_user(c.from_user.id, c.from_user.username):
        await c.answer("Нема доступу", show_alert=True)
        return
    await safe_edit_text(c, "🔧 Адмін-меню", reply_markup=admin_kb())
    await c.answer()


@router.callback_query(F.data == "admin:add")
async def admin_add_start(c: CallbackQuery, state: FSMContext):
    if not is_admin_user(c.from_user.id, c.from_user.username):
        await c.answer("Нема доступу", show_alert=True)
        return
    await state.clear()
    await state.set_state(AddProduct.photo)
    await c.message.answer("➕ Надішли фото товару (можна як Фото або як Файл):")
    await c.answer()


# ✅ ВИПРАВЛЕНО: приймає і Photo і Document(image/*)
@router.message(AddProduct.photo)
async def admin_add_photo_any(m: Message, state: FSMContext):
    # 1) якщо надіслали як фото
    if m.photo:
        photo_id = m.photo[-1].file_id
        await state.update_data(photo_file_id=photo_id)
        await state.set_state(AddProduct.title)
        await m.answer("Введи назву товару:")
        return

    # 2) якщо надіслали як файл (document)
    if m.document:
        mt = (m.document.mime_type or "").lower()
        if mt.startswith("image/"):
            file_id = m.document.file_id
            await state.update_data(photo_file_id=file_id)
            await state.set_state(AddProduct.title)
            await m.answer("Введи назву товару:")
            return

    await m.answer("❗️Надішли картинку як Фото або як Файл-зображення (jpg/png).")


@router.message(AddProduct.title)
async def admin_add_title(m: Message, state: FSMContext):
    await state.update_data(title=m.text.strip())
    await state.set_state(AddProduct.price)
    await m.answer("Введи ціну (тільки число, грн):")


@router.message(AddProduct.price)
async def admin_add_price(m: Message, state: FSMContext):
    t = re.sub(r"\D", "", m.text.strip())
    if not t:
        await m.answer("❗️Ціна має бути числом. Спробуй ще раз.")
        return
    price = int(t)
    await state.update_data(price=price)
    await state.set_state(AddProduct.description)
    await m.answer("Введи опис товару:")


@router.message(AddProduct.description)
async def admin_add_desc(m: Message, state: FSMContext):
    await state.update_data(description=m.text.strip())
    await state.set_state(AddProduct.variants)
    await m.answer("Введи розміри/варіанти через кому (S,M,L) або - якщо немає:")


@router.message(AddProduct.variants)
async def admin_add_variants(m: Message, state: FSMContext):
    variants = parse_variants(m.text)
    data = await state.get_data()
    created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    await db_execute("""
        INSERT INTO products(title, price, description, photo_file_id, variants_json, active, created_at)
        VALUES(?,?,?,?,?,?,?)
    """, (
        data["title"], data["price"], data["description"], data["photo_file_id"],
        json.dumps(variants, ensure_ascii=False), 1, created
    ))

    await state.clear()
    await m.answer("✅ Товар додано!", reply_markup=admin_kb())


@router.callback_query(F.data.startswith("admin:products:"))
async def admin_products(c: CallbackQuery):
    if not is_admin_user(c.from_user.id, c.from_user.username):
        await c.answer("Нема доступу", show_alert=True)
        return

    page = int(c.data.split(":")[2])
    offset = page * PAGE_SIZE

    rows = await db_fetchall(
        "SELECT id, title, price, active FROM products ORDER BY id DESC LIMIT ? OFFSET ?",
        (PAGE_SIZE, offset)
    )

    if not rows:
        await safe_edit_text(c, "Товарів поки немає.", reply_markup=admin_kb())
        await c.answer()
        return

    text = "📦 Товари\n\n"
    kb_rows = []
    for pid, title, price, active in rows:
        status = "✅" if active else "⛔️"
        text += f"{status} #{pid} — {title} — {money(price)}\n"
        kb_rows.append([
            InlineKeyboardButton(text=f"{status} {title}", callback_data=f"admin:toggle:{pid}"),
            InlineKeyboardButton(text="🗑", callback_data=f"admin:del:{pid}")
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin:products:{page-1}"))
    nav.append(InlineKeyboardButton(text="🔧 Меню", callback_data="admin:menu"))
    nav.append(InlineKeyboardButton(text="➡️", callback_data=f"admin:products:{page+1}"))
    kb_rows.append(nav)

    await safe_edit_text(c, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await c.answer()


@router.callback_query(F.data.startswith("admin:toggle:"))
async def admin_toggle(c: CallbackQuery):
    if not is_admin_user(c.from_user.id, c.from_user.username):
        await c.answer("Нема доступу", show_alert=True)
        return
    pid = int(c.data.split(":")[2])
    row = await db_fetchone("SELECT active FROM products WHERE id=?", (pid,))
    if not row:
        await c.answer("Не знайдено", show_alert=True)
        return
    new_active = 0 if row[0] == 1 else 1
    await db_execute("UPDATE products SET active=? WHERE id=?", (new_active, pid))
    await c.answer("Оновлено ✅")
    await admin_products(c)


@router.callback_query(F.data.startswith("admin:del:"))
async def admin_del(c: CallbackQuery):
    if not is_admin_user(c.from_user.id, c.from_user.username):
        await c.answer("Нема доступу", show_alert=True)
        return
    pid = int(c.data.split(":")[2])
    await db_execute("DELETE FROM products WHERE id=?", (pid,))
    await c.answer("Видалено 🗑")
    await admin_products(c)


# ----------------- Main -----------------
async def start_polling_with_retries(dp: Dispatcher, bot: Bot):
    delay = 3
    while True:
        try:
            await dp.start_polling(bot)
        except Exception as e:
            print(f"❌ Polling crashed: {e}")
            print(f"⏳ Retry in {delay}s...")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


async def main():
    await db_init()

    session = AiohttpSession(timeout=60)
    bot = Bot(token=BOT_TOKEN, session=session, default=DefaultBotProperties())

    dp = Dispatcher()
    dp.include_router(router)

    print("✅ Bot started")
    await start_polling_with_retries(dp, bot)


if __name__ == "__main__":
    asyncio.run(main())
