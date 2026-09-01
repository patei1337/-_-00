import asyncio
import logging
import re
import sqlite3
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import aiosqlite

# ---------- Конфиг ----------
BOT_TOKEN = "8721155454:AAGZYGedIHyVkzFogM__jSqpItff8oEOaaM"
ADMIN_CHAT_ID = 8791190493
DB_PATH = "shop.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ---------- Инициализация ----------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ---------- Инициализация БД и кэша ----------
product_cache = {}  # {product_id: {name, price, photo, category}}

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                price INTEGER NOT NULL,
                photo TEXT,
                category TEXT NOT NULL
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS carts (
                user_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (user_id, product_id)
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                address TEXT NOT NULL,
                phone TEXT NOT NULL,
                total INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Заполним тестовыми товарами, если пусто
        cursor = await db.execute("SELECT COUNT(*) FROM products")
        count = (await cursor.fetchone())[0]
        if count == 0:
            sample = [
                ("Розы 101", 1500, "https://example.com/rose.jpg", "розы"),
                ("Тюльпаны 20", 1200, "https://example.com/tulip.jpg", "тюльпаны"),
                ("Сборный букет", 2000, "https://example.com/mix.jpg", "сборные"),
            ]
            await db.executemany(
                "INSERT INTO products (name, price, photo, category) VALUES (?, ?, ?, ?)",
                sample
            )
        await db.commit()
    await refresh_cache()

async def refresh_cache():
    global product_cache
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT id, name, price, photo, category FROM products")
        rows = await cursor.fetchall()
        product_cache = {str(row[0]): {"name": row[1], "price": row[2], "photo": row[3], "category": row[4]} for row in rows}
    logger.info("Кэш товаров обновлён, %d записей", len(product_cache))

# ---------- Работа с корзиной (БД) ----------
async def get_cart(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT product_id, quantity FROM carts WHERE user_id = ?",
            (user_id,)
        )
        rows = await cursor.fetchall()
        return {str(pid): qty for pid, qty in rows}

async def update_cart(user_id: int, product_id: int, quantity: int = None):
    """quantity = None означает удалить"""
    async with aiosqlite.connect(DB_PATH) as db:
        if quantity is None or quantity == 0:
            await db.execute(
                "DELETE FROM carts WHERE user_id = ? AND product_id = ?",
                (user_id, product_id)
            )
        else:
            await db.execute(
                "INSERT OR REPLACE INTO carts (user_id, product_id, quantity) VALUES (?, ?, ?)",
                (user_id, product_id, quantity)
            )
        await db.commit()

async def clear_cart(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM carts WHERE user_id = ?", (user_id,))
        await db.commit()

# ---------- FSM для заказа ----------
class OrderForm(StatesGroup):
    address = State()
    phone = State()

# ---------- Валидаторы ----------
def validate_address(address: str) -> bool:
    return len(address.strip()) >= 5

def validate_phone(phone: str) -> bool:
    return bool(re.fullmatch(r'^\+?\d{10,15}$', phone.strip()))

# ---------- Клавиатуры ----------
def main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌺 Розы", callback_data="cat_розы")],
        [InlineKeyboardButton(text="🌷 Тюльпаны", callback_data="cat_тюльпаны")],
        [InlineKeyboardButton(text="💐 Сборные", callback_data="cat_сборные")],
        [InlineKeyboardButton(text="🛒 Корзина", callback_data="show_cart")],
    ])

def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")]
    ])

# ---------- Обработчики ----------
@dp.message(Command("start"))
async def start_handler(message: Message):
    await message.answer("🌹 Добро пожаловать в цветочный магазин!", reply_markup=main_menu_kb())

@dp.callback_query(F.data.startswith("cat_"))
async def show_products(callback: CallbackQuery):
    try:
        category = callback.data.split("_")[1]
        text = f"Категория: {category}\n\n"
        buttons = []
        for pid, p in product_cache.items():
            if p["category"] == category:
                text += f"{p['name']} — {p['price']} руб.\n"
                buttons.append([InlineKeyboardButton(text=f"➕ Добавить {p['name']}", callback_data=f"add_{pid}")])
        buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")])
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await callback.answer()
    except Exception as e:
        logger.error("Ошибка в show_products: %s", e, exc_info=True)
        await callback.answer("⚠️ Ошибка загрузки товаров", show_alert=True)

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
        logger.error("Ошибка добавления: %s", e, exc_info=True)
        await callback.answer("⚠️ Не удалось добавить", show_alert=True)

@dp.callback_query(F.data == "show_cart")
async def show_cart_handler(callback: CallbackQuery):
    try:
        user_id = callback.from_user.id
        cart = await get_cart(user_id)
        if not cart:
            await callback.message.edit_text("🛒 Корзина пуста.", reply_markup=back_kb())
            await callback.answer()
            return
        total = 0
        text = "🛒 Ваша корзина:\n\n"
        buttons = []
        for pid, qty in cart.items():
            p = product_cache.get(pid)
            if not p:
                continue
            text += f"{p['name']} × {qty} = {p['price'] * qty} руб.\n"
            total += p['price'] * qty
            buttons.append([
                InlineKeyboardButton(text=f"➖ {p['name']}", callback_data=f"dec_{pid}"),
                InlineKeyboardButton(text=f"➕", callback_data=f"add_{pid}"),
                InlineKeyboardButton(text="🗑️", callback_data=f"del_{pid}"),
            ])
        text += f"\nИтого: {total} руб."
        buttons.append([InlineKeyboardButton(text="✅ Оформить заказ", callback_data="checkout")])
        buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")])
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await callback.answer()
    except Exception as e:
        logger.error("Ошибка показа корзины: %s", e, exc_info=True)
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
            await show_cart_handler(callback)  # обновить
        else:
            await callback.answer("Товара нет в корзине")
    except Exception as e:
        logger.error("Ошибка уменьшения: %s", e, exc_info=True)
        await callback.answer("⚠️ Ошибка")

@dp.callback_query(F.data.startswith("del_"))
async def remove_item(callback: CallbackQuery):
    try:
        product_id = callback.data.split("_")[1]
        user_id = callback.from_user.id
        await update_cart(user_id, int(product_id), None)
        await show_cart_handler(callback)
        await callback.answer("🗑️ Удалено")
    except Exception as e:
        logger.error("Ошибка удаления: %s", e, exc_info=True)
        await callback.answer("⚠️ Ошибка")

@dp.callback_query(F.data == "checkout")
async def start_order(callback: CallbackQuery, state: FSMContext):
    try:
        user_id = callback.from_user.id
        cart = await get_cart(user_id)
        if not cart:
            await callback.answer("Корзина пуста!", show_alert=True)
            return
        await callback.message.edit_text("📝 Введите адрес доставки (минимум 5 символов):")
        await state.set_state(OrderForm.address)
        await callback.answer()
    except Exception as e:
        logger.error("Ошибка начала заказа: %s", e, exc_info=True)
        await callback.answer("⚠️ Ошибка")

@dp.message(OrderForm.address)
async def get_address(message: Message, state: FSMContext):
    if not validate_address(message.text):
        await message.answer("❌ Адрес слишком короткий. Введите минимум 5 символов:")
        return
    await state.update_data(address=message.text.strip())
    await message.answer("📞 Введите номер телефона (только цифры, можно с +):")
    await state.set_state(OrderForm.phone)

@dp.message(OrderForm.phone)
async def get_phone(message: Message, state: FSMContext):
    if not validate_phone(message.text):
        await message.answer("❌ Неверный формат. Введите 10–15 цифр, можно с +:")
        return
    phone = message.text.strip()
    data = await state.get_data()
    address = data.get("address")
    user_id = message.from_user.id
    cart = await get_cart(user_id)
    if not cart:
        await message.answer("Корзина пуста, начните заново /start")
        await state.clear()
        return
    total = 0
    order_lines = []
    for pid, qty in cart.items():
        p = product_cache.get(pid)
        if p:
            line = f"{p['name']} × {qty} = {p['price'] * qty} руб."
            order_lines.append(line)
            total += p['price'] * qty
    order_text = f"🆕 Новый заказ!\nАдрес: {address}\nТелефон: {phone}\n\n" + "\n".join(order_lines) + f"\nИтого: {total} руб."
    # Сохраняем заказ в БД (опционально)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO orders (user_id, address, phone, total) VALUES (?, ?, ?, ?)",
            (user_id, address, phone, total)
        )
        await db.commit()
    # Отправляем админу
    await bot.send_message(ADMIN_CHAT_ID, order_text)
    await clear_cart(user_id)
    await message.answer("✅ Заказ оформлен! Спасибо, мы свяжемся с вами.")
    await state.clear()

@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery):
    await callback.message.edit_text("🌹 Главное меню:", reply_markup=main_menu_kb())
    await callback.answer()

# ---------- Запуск ----------
async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())