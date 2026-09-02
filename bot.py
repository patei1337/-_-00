import asyncio
import logging
import re
import os
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
    raise ValueError("BOT_TOKEN не задан в переменных окружения")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL не задан в переменных окружения")
if not ADMIN_ID:
    raise ValueError("ADMIN_ID не задан в переменных окружения")

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

db_pool = None
product_cache = {}

# ---------- База данных ----------
async def init_db():
    global db_pool, product_cache
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

    async with db_pool.acquire() as conn:
        # Таблица товаров
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS products (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                price INTEGER NOT NULL,
                photo TEXT,
                category TEXT NOT NULL
            )
        ''')
        # Таблица корзин
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS carts (
                user_id BIGINT NOT NULL,
                product_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (user_id, product_id)
            )
        ''')
        # Таблица заказов с полем status
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
        # Если таблица уже существовала без status — добавляем
        await conn.execute('''
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'новый'
        ''')

        # Заполняем тестовыми товарами, если пусто
        count = await conn.fetchval("SELECT COUNT(*) FROM products")
        if count == 0:
            await conn.executemany(
                "INSERT INTO products (name, price, photo, category) VALUES ($1, $2, $3, $4)",
                [
                    ("Розы 101", 1500, "https://example.com/rose.jpg", "розы"),
                    ("Тюльпаны 20", 1200, "https://example.com/tulip.jpg", "тюльпаны"),
                    ("Сборный букет", 2000, "https://example.com/mix.jpg", "сборные"),
                ]
            )
    await refresh_cache()

async def refresh_cache():
    global product_cache
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, name, price, photo, category FROM products")
        product_cache = {str(row["id"]): {"name": row["name"], "price": row["price"], "photo": row["photo"], "category": row["category"]} for row in rows}
    logger.info("Кэш товаров обновлён, %d записей", len(product_cache))

# ---------- Работа с корзиной ----------
async def get_cart(user_id: int):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT product_id, quantity FROM carts WHERE user_id = $1",
            user_id
        )
        return {str(row["product_id"]): row["quantity"] for row in rows}

async def update_cart(user_id: int, product_id: int, quantity: int = None):
    async with db_pool.acquire() as conn:
        if quantity is None or quantity == 0:
            await conn.execute(
                "DELETE FROM carts WHERE user_id = $1 AND product_id = $2",
                user_id, product_id
            )
        else:
            await conn.execute(
                "INSERT INTO carts (user_id, product_id, quantity) VALUES ($1, $2, $3) "
                "ON CONFLICT (user_id, product_id) DO UPDATE SET quantity = $3",
                user_id, product_id, quantity
            )

async def clear_cart(user_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM carts WHERE user_id = $1", user_id)

# ---------- FSM для заказа ----------
class OrderForm(StatesGroup):
    address = State()
    phone = State()

# ---------- FSM для управления товарами ----------
class AdminProductForm(StatesGroup):
    waiting_for_name = State()
    waiting_for_price = State()
    waiting_for_category = State()
    waiting_for_photo = State()

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

def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Заказы", callback_data="admin_orders")],
        [InlineKeyboardButton(text="📦 Товары", callback_data="admin_products")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
    ])

# ---------- Обработчики пользователей ----------
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
            await show_cart_handler(callback)
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

    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO orders (user_id, address, phone, total, status) VALUES ($1, $2, $3, $4, $5)",
            user_id, address, phone, total, 'новый'
        )

    await bot.send_message(ADMIN_ID, order_text)
    await clear_cart(user_id)
    await message.answer("✅ Заказ оформлен! Спасибо, мы свяжемся с вами.")
    await state.clear()

@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery):
    await callback.message.edit_text("🌹 Главное меню:", reply_markup=main_menu_kb())
    await callback.answer()

# ---------- Админ-панель ----------
@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ У вас нет прав администратора.")
        return
    await message.answer("👋 Добро пожаловать в админ-панель!", reply_markup=admin_keyboard())

@dp.callback_query(F.data == "admin_orders")
async def admin_show_orders(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, user_id, address, phone, total, status, created_at FROM orders ORDER BY created_at DESC LIMIT 10"
        )
    if not rows:
        await callback.message.edit_text("📭 Заказов пока нет.", reply_markup=back_kb())
        await callback.answer()
        return
    text = "📋 Последние заказы:\n\n"
    buttons = []
    for row in rows:
        text += f"#{row['id']} | {row['created_at'].strftime('%d.%m %H:%M')} | {row['status']} | {row['total']} руб.\n"
        status_buttons = [
            InlineKeyboardButton(text="✅ В работу", callback_data=f"set_status_{row['id']}_в работе"),
            InlineKeyboardButton(text="🚚 Доставлен", callback_data=f"set_status_{row['id']}_доставлен"),
            InlineKeyboardButton(text="❌ Отменён", callback_data=f"set_status_{row['id']}_отменён"),
        ]
        buttons.append(status_buttons)
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_admin")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
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
        await conn.execute(
            "UPDATE orders SET status = $1 WHERE id = $2",
            new_status, order_id
        )
    await callback.answer(f"✅ Статус заказа #{order_id} изменён на «{new_status}»", show_alert=True)
    await admin_show_orders(callback)

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
    await callback.message.edit_text("📦 Управление товарами:", reply_markup=kb)
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
    await message.answer("Введите цену (только число):")
    await state.set_state(AdminProductForm.waiting_for_price)

@dp.message(AdminProductForm.waiting_for_price)
async def admin_add_product_price(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ Введите число без букв.")
        return
    price = int(message.text)
    data = await state.get_data()
    if "edit_product_id" in data:
        # Редактирование цены
        product_id = data["edit_product_id"]
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE products SET price = $1 WHERE id = $2", price, product_id)
        await refresh_cache()
        await message.answer("✅ Цена обновлена!")
        await state.clear()
        # Показываем меню товаров
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить товар", callback_data="admin_add_product")],
            [InlineKeyboardButton(text="🗑️ Удалить товар", callback_data="admin_del_product")],
            [InlineKeyboardButton(text="✏️ Изменить цену", callback_data="admin_edit_price")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_admin")],
        ])
        await message.answer("📦 Управление товарами:", reply_markup=kb)
    else:
        # Добавление нового товара
        await state.update_data(price=price)
        await message.answer("Введите категорию (розы, тюльпаны, сборные):")
        await state.set_state(AdminProductForm.waiting_for_category)

@dp.message(AdminProductForm.waiting_for_category)
async def admin_add_product_category(message: Message, state: FSMContext):
    await state.update_data(category=message.text.strip())
    await message.answer("Введите ссылку на фото (можно просто пропустить, введите 'нет'):")
    await state.set_state(AdminProductForm.waiting_for_photo)

@dp.message(AdminProductForm.waiting_for_photo)
async def admin_add_product_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    photo = message.text.strip() if message.text.strip().lower() != "нет" else None
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO products (name, price, category, photo) VALUES ($1, $2, $3, $4)",
            data["name"], data["price"], data["category"], photo
        )
    await refresh_cache()
    await message.answer(f"✅ Товар «{data['name']}» добавлен!")
    await state.clear()
    # Показываем меню товаров
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить товар", callback_data="admin_add_product")],
        [InlineKeyboardButton(text="🗑️ Удалить товар", callback_data="admin_del_product")],
        [InlineKeyboardButton(text="✏️ Изменить цену", callback_data="admin_edit_price")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_admin")],
    ])
    await message.answer("📦 Управление товарами:", reply_markup=kb)

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
        buttons.append([InlineKeyboardButton(text=f"🗑️ {p['name']}", callback_data=f"del_product_{pid}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_products")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()

@dp.callback_query(F.data.startswith("del_product_"))
async def admin_del_product_confirm(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    product_id = int(callback.data.split("_")[2])
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM products WHERE id = $1", product_id)
    await refresh_cache()
    await callback.answer("✅ Товар удалён", show_alert=True)
    await admin_products_menu(callback)

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
    await callback.message.edit_text("Введите новую цену (только число):")
    await state.set_state(AdminProductForm.waiting_for_price)
    await callback.answer()

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    async with db_pool.acquire() as conn:
        total_orders = await conn.fetchval("SELECT COUNT(*) FROM orders")
        total_revenue = await conn.fetchval("SELECT COALESCE(SUM(total), 0) FROM orders WHERE status != 'отменён'")
        new_orders = await conn.fetchval("SELECT COUNT(*) FROM orders WHERE status = 'новый'")
    text = f"📊 Статистика:\n\n• Всего заказов: {total_orders}\n• Выручка: {total_revenue} руб.\n• Новых заказов: {new_orders}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_admin")]
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()

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
