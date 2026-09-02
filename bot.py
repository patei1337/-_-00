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
    waiting_for_description = State()  # добавлено

# ---------- База данных ----------
async def init_db():
    global db_pool, product_cache
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

    async with db_pool.acquire() as conn:
        # Таблица товаров (без description)
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS products (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                price INTEGER NOT NULL,
                photo TEXT,
                category TEXT NOT NULL
            )
        ''')
        # Добавляем столбец description, если его нет
        await conn.execute('''
            ALTER TABLE products ADD COLUMN IF NOT EXISTS description TEXT DEFAULT 'Отличный выбор!'
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
        # Таблица заказов
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
        # Таблица пользователей
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Заполняем пользователей из заказов
        count = await conn.fetchval("SELECT COUNT(*) FROM users")
        if count == 0:
            await conn.execute("INSERT INTO users (user_id) SELECT DISTINCT user_id FROM orders ON CONFLICT (user_id) DO NOTHING")

        # Заполняем тестовыми товарами
        count = await conn.fetchval("SELECT COUNT(*) FROM products")
        if count == 0:
            await conn.executemany(
                "INSERT INTO products (name, price, photo, category, description) VALUES ($1, $2, $3, $4, $5)",
                [
                    ("Розы 101", 1500, "https://example.com/rose.jpg", "розы", "Классический букет из 101 розы"),
                    ("Тюльпаны 20", 1200, "https://example.com/tulip.jpg", "тюльпаны", "Весенние тюльпаны – 20 шт."),
                    ("Сборный букет", 2000, "https://example.com/mix.jpg", "сборные", "Эксклюзивный микс из полевых цветов"),
                ]
            )
    await refresh_cache()

async def refresh_cache():
    global product_cache
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, name, price, photo, category, description FROM products")
        product_cache = {
            str(row["id"]): {
                "name": row["name"],
                "price": row["price"],
                "photo": row["photo"],
                "category": row["category"],
                "description": row["description"]
            } for row in rows
        }
    logger.info("Кэш товаров обновлён, %d записей", len(product_cache))

# ---------- Работа с корзиной и пользователями ----------
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

async def save_user(user_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
            user_id
        )

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
        [InlineKeyboardButton(text="🔙 Назад в меню", callback_data="back_to_menu")]
    ])

def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Заказы", callback_data="admin_orders")],
        [InlineKeyboardButton(text="📦 Товары", callback_data="admin_products")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
    ])

def confirm_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, отправить", callback_data="broadcast_yes")],
        [InlineKeyboardButton(text="❌ Нет, отменить", callback_data="broadcast_no")],
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
        [InlineKeyboardButton(text="🔙 Назад к категориям", callback_data="back_to_menu")],
    ])
    if product.get('photo') and product['photo'] not in (None, 'нет'):
        if edit and message_id:
            await bot.edit_message_media(
                chat_id=chat_id,
                message_id=message_id,
                media=types.InputMediaPhoto(media=product['photo'], caption=text, parse_mode="Markdown"),
                reply_markup=keyboard
            )
        else:
            await bot.send_photo(chat_id, photo=product['photo'], caption=text, parse_mode="Markdown", reply_markup=keyboard)
    else:
        if edit and message_id:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, parse_mode="Markdown", reply_markup=keyboard)
        else:
            await bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=keyboard)

# ---------- Обработчики пользователей ----------
@dp.message(Command("start"))
async def start_handler(message: Message):
    await save_user(message.from_user.id)
    photo_url = "https://example.com/welcome_bouquet.jpg"  # замените на своё фото
    await bot.send_photo(
        chat_id=message.chat.id,
        photo=photo_url,
        caption=(
            "🌹 *Добро пожаловать в «Цветочный рай»!* 🌹\n\n"
            "Мы дарим радость с 2010 года.\n"
            "Выберите категорию ниже, чтобы начать:"
        ),
        reply_markup=main_menu_kb(),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("cat_"))
async def show_products(callback: CallbackQuery):
    try:
        category = callback.data.split("_")[1]
        products = [p for p in product_cache.values() if p["category"] == category]
        if not products:
            await callback.message.edit_text("В этой категории пока нет товаров.", reply_markup=back_kb())
            await callback.answer()
            return
        # Берём первый товар категории (можно доработать пагинацию)
        product = products[0]
        # Находим его ID
        pid = [k for k, v in product_cache.items() if v['name'] == product['name'] and v['category'] == product['category']][0]
        product['id'] = pid
        await send_product_card(callback.message.chat.id, product)
        await callback.message.delete()
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
            await callback.message.edit_text("🛒 *Ваша корзина пуста*", parse_mode="Markdown", reply_markup=back_kb())
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
        buttons.append([InlineKeyboardButton(text="✅ Оформить заказ", callback_data="checkout")])
        buttons.append([InlineKeyboardButton(text="🔙 Назад в меню", callback_data="back_to_menu")])
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
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
            line = f"🌸 {p['name']} × {qty} = {p['price'] * qty} руб."
            order_lines.append(line)
            total += p['price'] * qty

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "INSERT INTO orders (user_id, address, phone, total, status) VALUES ($1, $2, $3, $4, $5) RETURNING id",
                user_id, address, phone, total, 'новый'
            )
            order_id = row['id']
            await conn.execute(
                "INSERT INTO users (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
                user_id
            )

    order_text = (
        f"✅ *Заказ №{order_id} оформлен!*\n\n"
        f"📦 *Состав:*\n{chr(10).join(order_lines)}\n"
        f"━━━━━━━━━━━━━━━━━\n"
        f"💰 *Итого: {total} руб.*\n"
        f"📍 *Адрес:* {address}\n"
        f"📞 *Телефон:* {phone}\n\n"
        f"Скоро с вами свяжется менеджер."
    )
    await bot.send_message(message.chat.id, order_text, parse_mode="Markdown")

    admin_notify = f"🆕 Новый заказ #{order_id}\nАдрес: {address}\nТелефон: {phone}\nСумма: {total} руб."
    await bot.send_message(ADMIN_ID, admin_notify)

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
    text = "📋 *Последние заказы:*\n\n"
    buttons = []
    for row in rows:
        status_emoji = {"новый": "🆕", "в работе": "🔄", "доставлен": "✅", "отменён": "❌"}.get(row['status'], "❓")
        text += f"#{row['id']}  {status_emoji} *{row['status']}*  {row['created_at'].strftime('%d.%m %H:%M')}  {row['total']} руб.\n"
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
        await conn.execute(
            "UPDATE orders SET status = $1 WHERE id = $2",
            new_status, order_id
        )
    await callback.answer(f"✅ Статус заказа #{order_id} изменён на «{new_status}»", show_alert=True)
    await admin_show_orders(callback)

# ---------- Управление товарами (админ) ----------
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
    await callback.message.edit_text("📦 *Управление товарами:*", parse_mode="Markdown", reply_markup=kb)
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
        product_id = data["edit_product_id"]
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE products SET price = $1 WHERE id = $2", price, product_id)
        await refresh_cache()
        await message.answer("✅ Цена обновлена!")
        await state.clear()
        # Отправляем меню товаров (через callback не получится, поэтому просто новое сообщение)
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
    await message.answer("Введите ссылку на фото (можно просто пропустить, введите 'нет'):")
    await state.set_state(AdminProductForm.waiting_for_photo)

@dp.message(AdminProductForm.waiting_for_photo)
async def admin_add_product_photo(message: Message, state: FSMContext):
    photo = message.text.strip() if message.text.strip().lower() != "нет" else None
    await state.update_data(photo=photo)
    await message.answer("Введите краткое описание товара (можно пропустить, введите 'нет'):")
    await state.set_state(AdminProductForm.waiting_for_description)

@dp.message(AdminProductForm.waiting_for_description)
async def admin_add_product_description(message: Message, state: FSMContext):
    data = await state.get_data()
    description = message.text.strip() if message.text.strip().lower() != "нет" else "Отличный выбор!"
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO products (name, price, category, photo, description) VALUES ($1, $2, $3, $4, $5)",
            data["name"], data["price"], data["category"], data["photo"], description
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

# Удаление товара
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

# Изменение цены
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
        f"📊 *Статистика:*\n\n"
        f"• Всего заказов: *{total_orders}*\n"
        f"• Выручка: *{total_revenue}* руб.\n"
        f"• Новых заказов: *{new_orders}*\n"
        f"• Всего пользователей: *{total_users}*"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_admin")]
    ])
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    await callback.answer()

# ---------- Рассылка ----------
@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await callback.message.edit_text(
        "📢 Введите текст сообщения для рассылки.\n\n"
        "Он будет отправлен *всем* пользователям, которые когда-либо общались с ботом.\n"
        "Для отмены отправьте /cancel",
        parse_mode="Markdown"
    )
    await state.set_state(BroadcastForm.waiting_for_confirm)
    await state.update_data(broadcast_text=None)
    await callback.answer()

@dp.message(BroadcastForm.waiting_for_confirm)
async def broadcast_get_text(message: Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Рассылка отменена.")
        await admin_panel(message)
        return
    await state.update_data(broadcast_text=message.text)
    kb = confirm_keyboard()
    await message.answer(
        f"📢 Вы хотите отправить следующее сообщение ВСЕМ пользователям?\n\n"
        f"«{message.text}»\n\n"
        f"Подтвердите действие:",
        reply_markup=kb
    )

@dp.callback_query(F.data == "broadcast_yes")
async def broadcast_confirm_yes(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    data = await state.get_data()
    text = data.get("broadcast_text")
    if not text:
        await callback.answer("❌ Текст не найден. Начните заново.", show_alert=True)
        await state.clear()
        await admin_panel(callback.message)
        return

    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id FROM users")
    user_ids = [row["user_id"] for row in rows]

    if not user_ids:
        await callback.message.edit_text("❌ Нет пользователей для рассылки.")
        await state.clear()
        await callback.answer()
        return

    await callback.message.edit_text(f"⏳ Начинаю рассылку для {len(user_ids)} пользователей...")
    sent = 0
    failed = 0
    for i, uid in enumerate(user_ids):
        try:
            await bot.send_message(uid, text)
            sent += 1
        except Exception as e:
            logger.error(f"Не удалось отправить пользователю {uid}: {e}")
            failed += 1
        if i % 30 == 0:
            await asyncio.sleep(1)

    await callback.message.edit_text(
        f"✅ Рассылка завершена!\n"
        f"Отправлено: {sent}\n"
        f"Ошибок: {failed}"
    )
    await state.clear()

@dp.callback_query(F.data == "broadcast_no")
async def broadcast_confirm_no(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await callback.message.edit_text("❌ Рассылка отменена.")
    await state.clear()
    await admin_panel(callback.message)
    await callback.answer()

@dp.callback_query(F.data == "back_to_admin")
async def back_to_admin(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await callback.message.edit_text("👋 *Админ-панель:*", parse_mode="Markdown", reply_markup=admin_keyboard())
    await callback.answer()

# ---------- Запуск ----------
async def main():
    await init_db()
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
