import asyncio
import logging
import re
import os
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import asyncpg

# ---------- Конфиг ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL не задан")
if not ADMIN_ID:
    raise ValueError("ADMIN_ID не задан")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

db_pool = None
product_cache = {}

# ---------- FSM ----------
class OrderForm(StatesGroup):
    address = State()
    phone = State()

class BroadcastForm(StatesGroup):
    waiting_for_confirm = State()

class AdminProductForm(StatesGroup):
    waiting_for_name = State()
    waiting_for_price = State()
    waiting_for_category = State()
    waiting_for_photo = State()
    waiting_for_description = State()

class ReportForm(StatesGroup):
    waiting_for_custom_start = State()
    waiting_for_custom_end = State()

# ---------- База данных ----------
async def init_db():
    global db_pool, product_cache
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

    async with db_pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS products (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                price INTEGER NOT NULL,
                photo TEXT,
                category TEXT NOT NULL
            )
        ''')
        await conn.execute('''
            ALTER TABLE products ADD COLUMN IF NOT EXISTS description TEXT DEFAULT 'Отличный выбор!'
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS carts (
                user_id BIGINT NOT NULL,
                product_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (user_id, product_id)
            )
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                address TEXT NOT NULL,
                phone TEXT NOT NULL,
                total INTEGER NOT NULL,
                status TEXT DEFAULT 'новый',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await conn.execute('''
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'новый'
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        count = await conn.fetchval("SELECT COUNT(*) FROM users")
        if count == 0:
            await conn.execute("INSERT INTO users (user_id) SELECT DISTINCT user_id FROM orders ON CONFLICT (user_id) DO NOTHING")
        count = await conn.fetchval("SELECT COUNT(*) FROM products")
        if count == 0:
            await conn.executemany(
                "INSERT INTO products (name, price, photo, category, description) VALUES ($1, $2, $3, $4, $5)",
                [
                    ("Розы 101", 1500, None, "розы", "Классический букет из 101 розы"),
                    ("Тюльпаны 20", 1200, None, "тюльпаны", "Весенние тюльпаны – 20 шт."),
                    ("Сборный букет", 2000, None, "сборные", "Эксклюзивный микс из полевых цветов"),
                ]
            )
    await refresh_cache()

async def refresh_cache():
    global product_cache
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, name, price, photo, category, description FROM products")
        product_cache = {}
        for row in rows:
            product_cache[str(row["id"])] = {
                "id": str(row["id"]),
                "name": row["name"],
                "price": row["price"],
                "photo": row["photo"],
                "category": row["category"],
                "description": row["description"]
            }
    logger.info("Кэш обновлён, %d записей", len(product_cache))

# ---------- Работа с корзиной ----------
async def get_cart(user_id: int):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT product_id, quantity FROM carts WHERE user_id = $1", user_id)
        return {str(r["product_id"]): r["quantity"] for r in rows}

async def update_cart(user_id: int, product_id: int, quantity: int = None):
    async with db_pool.acquire() as conn:
        if quantity is None or quantity == 0:
            await conn.execute("DELETE FROM carts WHERE user_id = $1 AND product_id = $2", user_id, product_id)
        else:
            await conn.execute(
                "INSERT INTO carts (user_id, product_id, quantity) VALUES ($1, $2, $3) "
                "ON CONFLICT (user_id, product_id) DO UPDATE SET quantity = $3",
                user_id, product_id, quantity
            )

async def clear_cart(user_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM carts WHERE user_id = $1", user_id)

async def save_user(user_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING", user_id)

# ---------- Клавиатуры ----------
def main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌺 Розы", callback_data="cat_розы")],
        [InlineKeyboardButton(text="🌷 Тюльпаны", callback_data="cat_тюльпаны")],
        [InlineKeyboardButton(text="💐 Сборные", callback_data="cat_сборные")],
        [InlineKeyboardButton(text="🛒 Корзина", callback_data="show_cart")],
        [InlineKeyboardButton(text="📋 Мои заказы", callback_data="my_orders")],  # НОВАЯ КНОПКА
    ])

def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад в меню", callback_data="back_to_menu")]
    ])

def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Заказы", callback_data="admin_orders")],
        [InlineKeyboardButton(text="📦 Товары", callback_data="admin_products")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="📈 Отчёты", callback_data="admin_reports")],
    ])

def confirm_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да", callback_data="broadcast_yes")],
        [InlineKeyboardButton(text="❌ Нет", callback_data="broadcast_no")],
    ])

def report_period_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Сегодня", callback_data="report_today")],
        [InlineKeyboardButton(text="📅 Вчера", callback_data="report_yesterday")],
        [InlineKeyboardButton(text="📅 Эта неделя", callback_data="report_week")],
        [InlineKeyboardButton(text="📅 Этот месяц", callback_data="report_month")],
        [InlineKeyboardButton(text="🗓️ Произвольный период", callback_data="report_custom")],
        [InlineKeyboardButton(text="🏆 Топ товаров", callback_data="report_top")],
        [InlineKeyboardButton(text="👥 Новые пользователи", callback_data="report_users")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_admin")],
    ])

# ---------- Карточка товара ----------
async def send_product_card(chat_id, product, edit=False, message_id=None):
    text = (
        f"🌸 *{product['name']}*\n"
        f"💰 Цена: *{product['price']} руб.*\n"
        f"📂 Категория: {product['category']}\n"
        f"\n{product.get('description', 'Отличный выбор!')}"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Добавить в корзину", callback_data=f"add_{product['id']}")],
        [InlineKeyboardButton(text="🔙 Назад к списку", callback_data=f"back_to_category_{product['category']}")],
    ])
    photo_url = product.get('photo')
    if photo_url and photo_url not in (None, 'нет') and not photo_url.startswith('https://example.com'):
        try:
            if edit and message_id:
                await bot.edit_message_media(
                    chat_id=chat_id,
                    message_id=message_id,
                    media=types.InputMediaPhoto(media=photo_url, caption=text, parse_mode="Markdown"),
                    reply_markup=keyboard
                )
            else:
                await bot.send_photo(chat_id, photo=photo_url, caption=text, parse_mode="Markdown", reply_markup=keyboard)
            return
        except Exception as e:
            logger.warning(f"Фото не загружено: {e}, отправляю текст")
    if edit and message_id:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, parse_mode="Markdown", reply_markup=keyboard)
    else:
        await bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=keyboard)

# ---------- Отправка списка товаров ----------
async def send_products_list(chat_id, category, edit_message=None):
    products = [p for p in product_cache.values() if p["category"] == category]
    if not products:
        text = "В этой категории пока нет товаров."
        kb = back_kb()
    else:
        text = f"📂 *Категория: {category}*\n\nВыберите товар:"
        buttons = []
        for p in products:
            buttons.append([InlineKeyboardButton(text=f"🌸 {p['name']} — {p['price']} руб.", callback_data=f"view_{p['id']}")])
        buttons.append([InlineKeyboardButton(text="🔙 Назад в меню", callback_data="back_to_menu")])
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    if edit_message:
        try:
            await edit_message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
        except Exception as e:
            logger.warning(f"Не удалось отредактировать сообщение: {e}, отправляю новое")
            await bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)
    else:
        await bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=kb)

# ---------- Обработчики пользователей ----------
@dp.message(Command("start"))
async def start_handler(message: Message):
    await save_user(message.from_user.id)
    await message.answer(
        "🌹 *Добро пожаловать в «Цветочный рай»!* 🌹\n\n"
        "Выберите категорию ниже:",
        reply_markup=main_menu_kb(),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("cat_"))
async def show_products_list_handler(callback: CallbackQuery):
    try:
        category = callback.data.split("_")[1]
        await send_products_list(callback.message.chat.id, category, edit_message=callback.message)
        await callback.answer()
    except Exception as e:
        logger.error("Ошибка списка категории: %s", e)
        await callback.answer("⚠️ Ошибка", show_alert=True)

@dp.callback_query(F.data.startswith("view_"))
async def view_product(callback: CallbackQuery):
    try:
        product_id = callback.data.split("_")[1]
        product = product_cache.get(product_id)
        if not product:
            await callback.answer("Товар не найден", show_alert=True)
            return
        await send_product_card(callback.message.chat.id, product)
        await callback.message.delete()
        await callback.answer()
    except Exception as e:
        logger.error("Ошибка просмотра товара: %s", e)
        await callback.answer("⚠️ Ошибка", show_alert=True)

@dp.callback_query(F.data.startswith("back_to_category_"))
async def back_to_category(callback: CallbackQuery):
    try:
        category = callback.data.split("_")[3]
        await callback.message.delete()
        await send_products_list(callback.message.chat.id, category)
        await callback.answer()
    except Exception as e:
        logger.error("Ошибка возврата к категории: %s", e)
        await callback.answer("⚠️ Ошибка", show_alert=True)

@dp.callback_query(F.data.startswith("add_"))
async def add_to_cart(callback: CallbackQuery):
    try:
        product_id = callback.data.split("_")[1]
        user_id = callback.from_user.id
        cart = await get_cart(user_id)
        new_qty = cart.get(product_id, 0) + 1
        await update_cart(user_id, int(product_id), new_qty)
        await callback.answer("✅ Добавлено в корзину!", show_alert=True)
    except Exception as e:
        logger.error("Ошибка добавления: %s", e)
        await callback.answer("⚠️ Ошибка", show_alert=True)

@dp.callback_query(F.data == "show_cart")
async def show_cart_handler(callback: CallbackQuery):
    try:
        user_id = callback.from_user.id
        cart = await get_cart(user_id)
        if not cart:
            await callback.message.edit_text("🛒 *Корзина пуста*", parse_mode="Markdown", reply_markup=back_kb())
            await callback.answer()
            return
        total = 0
        text = "🛒 *Ваша корзина*\n\n"
        buttons = []
        for pid, qty in cart.items():
            p = product_cache.get(pid)
            if not p:
                continue
            price = p['price'] * qty
            total += price
            text += f"🌸 {p['name']} × {qty} = *{price} руб.*\n"
            buttons.append([
                InlineKeyboardButton(text="➖", callback_data=f"dec_{pid}"),
                InlineKeyboardButton(text="➕", callback_data=f"add_{pid}"),
                InlineKeyboardButton(text="🗑️", callback_data=f"del_{pid}"),
            ])
        text += f"\n━━━━━━━━━━━━━━━━━\n*Итого: {total} руб.*"
        buttons.append([InlineKeyboardButton(text="✅ Оформить", callback_data="checkout")])
        buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")])
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await callback.answer()
    except Exception as e:
        logger.error("Ошибка корзины: %s", e)
        await callback.answer("⚠️ Ошибка", show_alert=True)

@dp.callback_query(F.data.startswith("dec_"))
async def decrease_cart(callback: CallbackQuery):
    try:
        product_id = callback.data.split("_")[1]
        user_id = callback.from_user.id
        cart = await get_cart(user_id)
        if product_id in cart:
            new_qty = cart[product_id] - 1
            if new_qty > 0:
                await update_cart(user_id, int(product_id), new_qty)
            else:
                await update_cart(user_id, int(product_id), None)
            await show_cart_handler(callback)
        else:
            await callback.answer("Товара нет")
    except Exception as e:
        logger.error("Ошибка уменьшения: %s", e)
        await callback.answer("⚠️ Ошибка")

@dp.callback_query(F.data.startswith("del_"))
async def remove_item(callback: CallbackQuery):
    try:
        parts = callback.data.split("_")
        if len(parts) != 2 or not parts[1].isdigit():
            return
        product_id = parts[1]
        user_id = callback.from_user.id
        await update_cart(user_id, int(product_id), None)
        await show_cart_handler(callback)
        await callback.answer("🗑️ Удалено")
    except Exception as e:
        logger.error("Ошибка удаления из корзины: %s", e)
        await callback.answer("⚠️ Ошибка", show_alert=True)

@dp.callback_query(F.data == "checkout")
async def start_order(callback: CallbackQuery, state: FSMContext):
    try:
        user_id = callback.from_user.id
        cart = await get_cart(user_id)
        if not cart:
            await callback.answer("Корзина пуста!", show_alert=True)
            return
        await callback.message.edit_text("📝 Введите адрес доставки (мин. 5 символов):")
        await state.set_state(OrderForm.address)
        await callback.answer()
    except Exception as e:
        logger.error("Ошибка заказа: %s", e)
        await callback.answer("⚠️ Ошибка")

@dp.message(OrderForm.address)
async def get_address(message: Message, state: FSMContext):
    if len(message.text.strip()) < 5:
        await message.answer("❌ Слишком короткий адрес. Введите минимум 5 символов.")
        return
    await state.update_data(address=message.text.strip())
    await message.answer("📞 Введите номер телефона (10–15 цифр, можно с +):")
    await state.set_state(OrderForm.phone)

@dp.message(OrderForm.phone)
async def get_phone(message: Message, state: FSMContext):
    if not re.fullmatch(r'^\+?\d{10,15}$', message.text.strip()):
        await message.answer("❌ Неверный формат. Введите 10–15 цифр, можно с +.")
        return
    phone = message.text.strip()
    data = await state.get_data()
    address = data["address"]
    user_id = message.from_user.id
    cart = await get_cart(user_id)
    if not cart:
        await message.answer("Корзина пуста. Начните заново /start")
        await state.clear()
        return
    total = 0
    order_lines = []
    for pid, qty in cart.items():
        p = product_cache.get(pid)
        if p:
            line = f"🌸 {p['name']} × {qty} = {p['price'] * qty} руб."
            order_lines.append(line)
            total += p['price'] * qty

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO orders (user_id, address, phone, total, status) VALUES ($1, $2, $3, $4, $5) RETURNING id",
                user_id, address, phone, total, 'новый'
            )
            order_id = row["id"]
            await conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING", user_id)

    order_text = (
        f"✅ *Заказ №{order_id} оформлен!*\n\n"
        f"📦 *Состав:*\n" + "\n".join(order_lines) +
        f"\n━━━━━━━━━━━━━━━━━\n"
        f"💰 *Итого: {total} руб.*\n"
        f"📍 *Адрес:* {address}\n"
        f"📞 *Телефон:* {phone}\n\n"
        f"Скоро свяжемся с вами."
    )
    await bot.send_message(message.chat.id, order_text, parse_mode="Markdown")
    await bot.send_message(ADMIN_ID, f"🆕 Новый заказ #{order_id}\nАдрес: {address}\nТелефон: {phone}\nСумма: {total} руб.")
    await clear_cart(user_id)
    await state.clear()

@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery):
    await callback.message.edit_text(
        "🌹 *Главное меню:*",
        reply_markup=main_menu_kb(),
        parse_mode="Markdown"
    )
    await callback.answer()

# ---------- ИСТОРИЯ ЗАКАЗОВ ДЛЯ ПОЛЬЗОВАТЕЛЯ ----------
@dp.message(Command("myorders"))
async def my_orders_command(message: Message):
    user_id = message.from_user.id
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, total, status, created_at FROM orders WHERE user_id = $1 ORDER BY created_at DESC",
            user_id
        )
    if not rows:
        await message.answer("📭 У вас пока нет заказов.")
        return
    text = "📋 *Ваши заказы:*\n\n"
    for row in rows:
        status_emoji = {"новый": "🆕", "в работе": "🔄", "доставлен": "✅", "отменён": "❌"}.get(row['status'], "❓")
        text += f"#{row['id']}  {status_emoji} {row['status']}  {row['created_at'].strftime('%d.%m %H:%M')}  {row['total']} руб.\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад в меню", callback_data="back_to_menu")]
    ])
    await message.answer(text, parse_mode="Markdown", reply_markup=kb)

@dp.callback_query(F.data == "my_orders")
async def my_orders_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, total, status, created_at FROM orders WHERE user_id = $1 ORDER BY created_at DESC",
            user_id
        )
    if not rows:
        await callback.message.edit_text("📭 У вас пока нет заказов.", reply_markup=back_kb())
        await callback.answer()
        return
    text = "📋 *Ваши заказы:*\n\n"
    for row in rows:
        status_emoji = {"новый": "🆕", "в работе": "🔄", "доставлен": "✅", "отменён": "❌"}.get(row['status'], "❓")
        text += f"#{row['id']}  {status_emoji} {row['status']}  {row['created_at'].strftime('%d.%m %H:%M')}  {row['total']} руб.\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад в меню", callback_data="back_to_menu")]
    ])
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    await callback.answer()

# ---------- Админка ----------
@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Доступ запрещён.")
        return
    await message.answer("👋 Админ-панель:", reply_markup=admin_keyboard())

@dp.callback_query(F.data == "admin_orders")
async def admin_show_orders(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, total, status, created_at FROM orders ORDER BY created_at DESC LIMIT 10")
    if not rows:
        await callback.message.edit_text("📭 Заказов пока нет.", reply_markup=back_kb())
        await callback.answer()
        return
    text = "📋 *Последние заказы:*\n\n"
    buttons = []
    for row in rows:
        emoji = {"новый": "🆕", "в работе": "🔄", "доставлен": "✅", "отменён": "❌"}.get(row['status'], "❓")
        text += f"#{row['id']}  {emoji} {row['status']}  {row['created_at'].strftime('%d.%m %H:%M')}  {row['total']} руб.\n"
        status_buttons = [
            InlineKeyboardButton(text="✅ В работу", callback_data=f"set_status_{row['id']}_в работе"),
            InlineKeyboardButton(text="🚚 Доставлен", callback_data=f"set_status_{row['id']}_доставлен"),
            InlineKeyboardButton(text="❌ Отменён", callback_data=f"set_status_{row['id']}_отменён"),
        ]
        buttons.append(status_buttons)
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_admin")])
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()

@dp.callback_query(F.data.startswith("set_status_"))
async def admin_set_status(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    parts = callback.data.split("_")
    order_id = int(parts[2])
    new_status = "_".join(parts[3:])
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE orders SET status = $1 WHERE id = $2", new_status, order_id)
    await callback.answer(f"✅ Статус заказа #{order_id} изменён на «{new_status}»", show_alert=True)
    await admin_show_orders(callback)

# ---------- Управление товарами ----------
@dp.callback_query(F.data == "admin_products")
async def admin_products_menu(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить товар", callback_data="admin_add_product")],
        [InlineKeyboardButton(text="🗑️ Удалить товар", callback_data="admin_del_product")],
        [InlineKeyboardButton(text="✏️ Изменить цену", callback_data="admin_edit_price")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_admin")],
    ])
    try:
        await callback.message.edit_text("📦 *Управление товарами:*", parse_mode="Markdown", reply_markup=kb)
    except Exception:
        await callback.message.answer("📦 *Управление товарами:*", parse_mode="Markdown", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "admin_add_product")
async def admin_add_product_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await callback.message.edit_text("Введите название товара:")
    await state.set_state(AdminProductForm.waiting_for_name)
    await callback.answer()

@dp.message(AdminProductForm.waiting_for_name)
async def admin_add_product_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer("Введите цену (число):")
    await state.set_state(AdminProductForm.waiting_for_price)

@dp.message(AdminProductForm.waiting_for_price)
async def admin_add_product_price(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ Введите число.")
        return
    price = int(message.text)
    data = await state.get_data()
    if "edit_product_id" in data:
        product_id = data["edit_product_id"]
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE products SET price = $1 WHERE id = $2", price, product_id)
        await refresh_cache()
        await message.answer("✅ Цена обновлена!")
        await state.clear()
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить товар", callback_data="admin_add_product")],
            [InlineKeyboardButton(text="🗑️ Удалить товар", callback_data="admin_del_product")],
            [InlineKeyboardButton(text="✏️ Изменить цену", callback_data="admin_edit_price")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_admin")],
        ])
        await message.answer("📦 *Управление товарами:*", parse_mode="Markdown", reply_markup=kb)
    else:
        await state.update_data(price=price)
        await message.answer("Введите категорию (розы, тюльпаны, сборные):")
        await state.set_state(AdminProductForm.waiting_for_category)

@dp.message(AdminProductForm.waiting_for_category)
async def admin_add_product_category(message: Message, state: FSMContext):
    await state.update_data(category=message.text.strip())
    await message.answer("Введите ссылку на фото (или 'нет'):")
    await state.set_state(AdminProductForm.waiting_for_photo)

@dp.message(AdminProductForm.waiting_for_photo)
async def admin_add_product_photo(message: Message, state: FSMContext):
    photo = message.text.strip() if message.text.strip().lower() != "нет" else None
    await state.update_data(photo=photo)
    await message.answer("Введите описание (или 'нет'):")
    await state.set_state(AdminProductForm.waiting_for_description)

@dp.message(AdminProductForm.waiting_for_description)
async def admin_add_product_description(message: Message, state: FSMContext):
    data = await state.get_data()
    description = message.text.strip() if message.text.strip().lower() != "нет" else "Отличный выбор!"
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO products (name, price, photo, category, description) VALUES ($1, $2, $3, $4, $5)",
            data["name"], data["price"], data["photo"], data["category"], description
        )
    await refresh_cache()
    await message.answer(f"✅ Товар «{data['name']}» добавлен!")
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить товар", callback_data="admin_add_product")],
        [InlineKeyboardButton(text="🗑️ Удалить товар", callback_data="admin_del_product")],
        [InlineKeyboardButton(text="✏️ Изменить цену", callback_data="admin_edit_price")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_admin")],
    ])
    await message.answer("📦 *Управление товарами:*", parse_mode="Markdown", reply_markup=kb)

# ---------- Удаление товара ----------
@dp.callback_query(F.data == "admin_del_product")
async def admin_del_product_list(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    if not product_cache:
        await callback.message.edit_text("Товаров нет.", reply_markup=back_kb())
        await callback.answer()
        return
    text = "Выберите товар для удаления:\n"
    buttons = []
    for pid, p in product_cache.items():
        buttons.append([InlineKeyboardButton(text=f"🗑️ {p['name']}", callback_data=f"rmprod_{pid}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_products")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()

@dp.callback_query(F.data.startswith("rmprod_"))
async def admin_del_product_confirm(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    try:
        parts = callback.data.split("_")
        if len(parts) < 2:
            await callback.answer("❌ Неверный формат", show_alert=True)
            return
        product_id_str = parts[1]
        if not product_id_str.isdigit():
            await callback.answer("❌ Неверный ID", show_alert=True)
            return
        if product_id_str not in product_cache:
            await callback.answer("❌ Товар не найден", show_alert=True)
            return
        product_id = int(product_id_str)

        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM products WHERE id = $1", product_id)

        await refresh_cache()
        await callback.answer("✅ Товар удалён!", show_alert=True)
        await callback.message.delete()
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить товар", callback_data="admin_add_product")],
            [InlineKeyboardButton(text="🗑️ Удалить товар", callback_data="admin_del_product")],
            [InlineKeyboardButton(text="✏️ Изменить цену", callback_data="admin_edit_price")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_admin")],
        ])
        await bot.send_message(callback.from_user.id, "📦 *Управление товарами:*", parse_mode="Markdown", reply_markup=kb)
    except Exception as e:
        logger.error(f"Ошибка удаления товара: {e}", exc_info=True)
        await callback.answer("⚠️ Ошибка при удалении", show_alert=True)

# ---------- Изменение цены ----------
@dp.callback_query(F.data == "admin_edit_price")
async def admin_edit_price_list(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    if not product_cache:
        await callback.message.edit_text("Товаров нет.", reply_markup=back_kb())
        await callback.answer()
        return
    text = "Выберите товар для изменения цены:\n"
    buttons = []
    for pid, p in product_cache.items():
        buttons.append([InlineKeyboardButton(text=f"✏️ {p['name']} ({p['price']} руб.)", callback_data=f"edit_price_{pid}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_products")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()

@dp.callback_query(F.data.startswith("edit_price_"))
async def admin_edit_price_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    product_id = int(callback.data.split("_")[2])
    await state.update_data(edit_product_id=product_id)
    await callback.message.edit_text("Введите новую цену (число):")
    await state.set_state(AdminProductForm.waiting_for_price)
    await callback.answer()

# ---------- Статистика ----------
@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    async with db_pool.acquire() as conn:
        total_orders = await conn.fetchval("SELECT COUNT(*) FROM orders")
        total_revenue = await conn.fetchval("SELECT COALESCE(SUM(total), 0) FROM orders WHERE status != 'отменён'")
        new_orders = await conn.fetchval("SELECT COUNT(*) FROM orders WHERE status = 'новый'")
        total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
    text = (
        f"📊 *Общая статистика:*\n\n"
        f"• Заказов: *{total_orders}*\n"
        f"• Выручка: *{total_revenue}* руб.\n"
        f"• Новых заказов: *{new_orders}*\n"
        f"• Пользователей: *{total_users}*"
    )
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=back_kb())
    await callback.answer()

# ---------- ОТЧЁТЫ ----------
@dp.callback_query(F.data == "admin_reports")
async def admin_reports_menu(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await callback.message.edit_text(
        "📈 *Выберите тип отчёта:*",
        parse_mode="Markdown",
        reply_markup=report_period_keyboard()
    )
    await callback.answer()

async def generate_report(callback: CallbackQuery, start_date, end_date, period_name):
    try:
        async with db_pool.acquire() as conn:
            total_orders = await conn.fetchval(
                "SELECT COUNT(*) FROM orders WHERE created_at >= $1 AND created_at < $2",
                start_date, end_date
            )
            total_revenue = await conn.fetchval(
                "SELECT COALESCE(SUM(total), 0) FROM orders WHERE created_at >= $1 AND created_at < $2 AND status != 'отменён'",
                start_date, end_date
            )
            avg_check = total_revenue // total_orders if total_orders else 0

        text = (
            f"📈 *Отчёт за {period_name}*\n\n"
            f"📅 Период: {start_date.strftime('%d.%m.%Y')} – {end_date.strftime('%d.%m.%Y')}\n\n"
            f"📦 Всего заказов: *{total_orders}*\n"
            f"💰 Выручка: *{total_revenue}* руб.\n"
            f"🧾 Средний чек: *{avg_check}* руб."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад к отчётам", callback_data="admin_reports")]
        ])
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    except Exception as e:
        logger.error(f"Ошибка генерации отчёта: {e}", exc_info=True)
        await callback.message.edit_text("⚠️ Ошибка при формировании отчёта. Попробуйте позже.")
    await callback.answer()

@dp.callback_query(F.data == "report_today")
async def report_today(callback: CallbackQuery):
    try:
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow = today + timedelta(days=1)
        await generate_report(callback, today, tomorrow, "сегодня")
    except Exception as e:
        logger.error(f"Ошибка в report_today: {e}", exc_info=True)
        await callback.answer("⚠️ Ошибка", show_alert=True)

@dp.callback_query(F.data == "report_yesterday")
async def report_yesterday(callback: CallbackQuery):
    try:
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday_start = today - timedelta(days=1)
        await generate_report(callback, yesterday_start, today, "вчера")
    except Exception as e:
        logger.error(f"Ошибка в report_yesterday: {e}", exc_info=True)
        await callback.answer("⚠️ Ошибка", show_alert=True)

@dp.callback_query(F.data == "report_week")
async def report_week(callback: CallbackQuery):
    try:
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        week_ago = today - timedelta(days=7)
        await generate_report(callback, week_ago, today, "последние 7 дней")
    except Exception as e:
        logger.error(f"Ошибка в report_week: {e}", exc_info=True)
        await callback.answer("⚠️ Ошибка", show_alert=True)

@dp.callback_query(F.data == "report_month")
async def report_month(callback: CallbackQuery):
    try:
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        month_ago = today - timedelta(days=30)
        await generate_report(callback, month_ago, today, "последние 30 дней")
    except Exception as e:
        logger.error(f"Ошибка в report_month: {e}", exc_info=True)
        await callback.answer("⚠️ Ошибка", show_alert=True)

@dp.callback_query(F.data == "report_custom")
async def report_custom_start(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.message.edit_text("📅 Введите дату начала (в формате ДД.ММ.ГГГГ), например 01.01.2025:")
        await state.set_state(ReportForm.waiting_for_custom_start)
        await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка в report_custom_start: {e}", exc_info=True)
        await callback.answer("⚠️ Ошибка", show_alert=True)

@dp.message(ReportForm.waiting_for_custom_start)
async def report_custom_get_start(message: Message, state: FSMContext):
    try:
        start_date = datetime.strptime(message.text.strip(), "%d.%m.%Y")
        await state.update_data(start_date=start_date)
        await message.answer("📅 Теперь введите дату окончания (в формате ДД.ММ.ГГГГ):")
        await state.set_state(ReportForm.waiting_for_custom_end)
    except ValueError:
        await message.answer("❌ Неверный формат. Введите дату в формате ДД.ММ.ГГГГ, например 01.01.2025.")

@dp.message(ReportForm.waiting_for_custom_end)
async def report_custom_get_end(message: Message, state: FSMContext):
    try:
        end_date = datetime.strptime(message.text.strip(), "%d.%m.%Y")
        end_date = end_date.replace(hour=23, minute=59, second=59)
        data = await state.get_data()
        start_date = data["start_date"]
        if start_date > end_date:
            await message.answer("❌ Дата начала не может быть позже даты окончания.")
            return
        class DummyCallback:
            def __init__(self, msg, user_id):
                self.message = msg
                self.from_user = type('obj', (object,), {'id': user_id})
            async def answer(self, *args, **kwargs):
                pass
        dummy = DummyCallback(message, message.from_user.id)
        await generate_report(dummy, start_date, end_date, f"{start_date.strftime('%d.%m.%Y')} – {end_date.strftime('%d.%m.%Y')}")
        await state.clear()
    except ValueError:
        await message.answer("❌ Неверный формат. Введите дату в формате ДД.ММ.ГГГГ.")

@dp.callback_query(F.data == "report_top")
async def report_top(callback: CallbackQuery):
    text = (
        "🏆 *Топ товаров*\n\n"
        "Функция в разработке. Для её работы нужно хранить состав заказа в отдельной таблице.\n"
        "Скоро добавим!"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад к отчётам", callback_data="admin_reports")]
    ])
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "report_users")
async def report_users(callback: CallbackQuery):
    try:
        async with db_pool.acquire() as conn:
            total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            new_today = await conn.fetchval("SELECT COUNT(*) FROM users WHERE created_at >= $1", today)
            week_ago = today - timedelta(days=7)
            new_week = await conn.fetchval("SELECT COUNT(*) FROM users WHERE created_at >= $1", week_ago)
            month_ago = today - timedelta(days=30)
            new_month = await conn.fetchval("SELECT COUNT(*) FROM users WHERE created_at >= $1", month_ago)
        text = (
            f"👥 *Новые пользователи*\n\n"
            f"• Всего: *{total_users}*\n"
            f"• За сегодня: *{new_today}*\n"
            f"• За неделю: *{new_week}*\n"
            f"• За месяц: *{new_month}*"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад к отчётам", callback_data="admin_reports")]
        ])
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
        await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка в report_users: {e}", exc_info=True)
        await callback.answer("⚠️ Ошибка", show_alert=True)

# ---------- Рассылка ----------
@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await callback.message.edit_text("📢 Введите текст для рассылки (или /cancel):")
    await state.set_state(BroadcastForm.waiting_for_confirm)
    await callback.answer()

@dp.message(BroadcastForm.waiting_for_confirm)
async def broadcast_get_text(message: Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено.")
        await admin_panel(message)
        return
    await state.update_data(broadcast_text=message.text)
    await message.answer(
        f"Отправить всем это?\n\n{message.text}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да", callback_data="broadcast_yes")],
            [InlineKeyboardButton(text="❌ Нет", callback_data="broadcast_no")],
        ])
    )

@dp.callback_query(F.data == "broadcast_yes")
async def broadcast_confirm_yes(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    data = await state.get_data()
    text = data.get("broadcast_text")
    if not text:
        await callback.answer("❌ Текст не найден.", show_alert=True)
        await state.clear()
        await admin_panel(callback.message)
        return
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM users")
    users = [r["user_id"] for r in rows]
    if not users:
        await callback.message.edit_text("Нет пользователей.")
        await state.clear()
        await callback.answer()
        return
    await callback.message.edit_text(f"⏳ Отправка {len(users)} пользователям...")
    sent = 0
    for i, uid in enumerate(users):
        try:
            await bot.send_message(uid, text)
            sent += 1
        except Exception:
            pass
        if i % 30 == 0:
            await asyncio.sleep(1)
    await callback.message.edit_text(f"✅ Отправлено: {sent}")
    await state.clear()

@dp.callback_query(F.data == "broadcast_no")
async def broadcast_confirm_no(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await callback.message.edit_text("❌ Отменено.")
    await state.clear()
    await admin_panel(callback.message)

@dp.callback_query(F.data == "back_to_admin")
async def back_to_admin(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await callback.message.edit_text("👋 Админ-панель:", reply_markup=admin_keyboard())
    await callback.answer()

# ---------- Запуск ----------
async def main():
    await init_db()
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
