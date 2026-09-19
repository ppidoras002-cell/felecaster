import asyncio
import html
import os
import urllib.parse
import urllib.request
import urllib.error
import json
import random
import re
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, FSInputFile, InlineKeyboardButton
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder

from config import BOT_TOKEN


def _read_env_value(name):
    """Читает значение из окружения или простого .env рядом с bot.py."""
    value = os.getenv(name, "").strip()
    if value:
        return value
    env_path = Path(__file__).resolve().with_name(".env")
    try:
        if env_path.exists():
            for raw_line in env_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, raw_value = line.split("=", 1)
                if key.strip() == name:
                    return raw_value.strip().strip("\"'")
    except Exception:
        pass
    return ""


CRYPTO_PAY_TOKEN = _read_env_value("CRYPTO_PAY_TOKEN")
CRYPTO_PAY_API_URL = "https://pay.crypt.bot/api"


ADMIN_ID = 5083974050
DB_NAME = str(Path(__file__).resolve().with_name("shop.db"))
# Обязательная подписка на официальный канал перед использованием бота.
SUBSCRIPTION_CHANNEL = "@FelecasterNews"
SUBSCRIPTION_CHANNEL_URL = "https://t.me/FelecasterNews"
STARS_CATEGORY_NAME = "⭐ Telegram Stars"
PLAYSTATION_CATEGORY_NAME = "🎮 PlayStation"
PLAYSTATION_DESCRIPTION = "🎮 PlayStation Store\n\nПополнение PS Store в EUR. Перед покупкой обязательно проверь регион своего аккаунта."
STARS_RUB_PRICE = 1.5

# Реквизиты для ручной оплаты. ЗАМЕНИТЕ на свои реальные данные.
RUB_PAYMENT_DETAILS = "СБП/карта: 2204120143193700"
USDT_PAYMENT_ADDRESS = "TDq8JqMVGsEV62jrYcpYb4rFpm1cWiDPe2"
USDT_NETWORK = "TRC20"

# СБП: QR и ссылка для оплаты. Файл sbp_qr.jpg должен лежать рядом с bot.py.
SBP_PAYMENT_URL = "https://yoomoney.ru/to/4100117652568519/0"
SBP_QR_PATH = str(Path(__file__).resolve().with_name("sbp_qr.jpg"))

# Felecaster OS visual identity. This only affects presentation, not business logic.
OS_SIGNATURE = "<code>FELECASTER OS // БЕЗОПАСНО SESSION</code>"

def os_screen(title, body=""):
    return f"🟣 <b>{title}</b>\n\n{body}\n\n{OS_SIGNATURE}"

dp = Dispatcher()


class SubscriptionMiddleware(BaseMiddleware):
    """Не даёт пользоваться ботом, пока пользователь не подписан на канал."""

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if not user or user.id == ADMIN_ID:
            return await handler(event, data)

        if isinstance(event, CallbackQuery) and event.data in ("subscription_check", "subscription_join"):
            return await handler(event, data)

        bot = data.get("bot")
        if bot is None:
            return await handler(event, data)

        try:
            member = await bot.get_chat_member(
                chat_id=SUBSCRIPTION_CHANNEL,
                user_id=user.id,
            )
            status = getattr(member, "status", "")
            is_subscribed = status in {"creator", "administrator", "member"}
            if status == "restricted":
                is_subscribed = bool(getattr(member, "is_member", False))

            print(
                f"[SUBSCRIPTION] user_id={user.id} "
                f"channel={SUBSCRIPTION_CHANNEL!r} status={status!r} "
                f"subscribed={is_subscribed}"
            )
        except Exception as e:
            print(
                f"[SUBSCRIPTION CHECK ERROR] channel={SUBSCRIPTION_CHANNEL!r} "
                f"user_id={user.id}: {type(e).__name__}: {e}"
            )
            is_subscribed = False

        if not is_subscribed:
            text = (
                "🔒 <b>ДОСТУП К FELECASTER SHOP</b>\n\n"
                "Чтобы пользоваться ботом, сначала подпишитесь на наш официальный канал.\n\n"
                "📢 <b>Felecaster News</b>\n"
                "Там публикуются новости, акции, промокоды и обновления магазина.\n\n"
                "После подписки нажмите <b>«✅ Я подписался»</b>."
            )
            builder = InlineKeyboardBuilder()
            builder.button(text="📢 Подписаться на канал", url=SUBSCRIPTION_CHANNEL_URL)
            builder.button(text="✅ Я подписался", callback_data="subscription_check")
            builder.adjust(1)

            if isinstance(event, CallbackQuery):
                await event.answer("Сначала подпишитесь на канал.", show_alert=False)
                if event.message:
                    await event.message.answer(
                        text,
                        reply_markup=builder.as_markup(),
                        parse_mode="HTML",
                    )
            else:
                await event.answer(
                    text,
                    reply_markup=builder.as_markup(),
                    parse_mode="HTML",
                )
            return

        return await handler(event, data)


class SingleMessageMiddleware(BaseMiddleware):
    """Удаляет предыдущее inline-сообщение перед переходом на новый экран."""

    async def __call__(self, handler, event, data):
        if isinstance(event, CallbackQuery) and event.message:
            # Экран обязательной подписки нельзя удалять до проверки:
            # после удаления callback.message становится недействительным,
            # и обработчик subscription_check не сможет показать меню.
            if event.data not in ("subscription_check", "subscription_join"):
                try:
                    await event.message.delete()
                except Exception:
                    # Уже удалено / Telegram не разрешил удаление — продолжаем.
                    pass

        return await handler(event, data)


# Сначала проверяем обязательную подписку, затем работаем с inline-интерфейсом.
dp.message.middleware(SubscriptionMiddleware())
dp.callback_query.middleware(SubscriptionMiddleware())
dp.callback_query.middleware(SingleMessageMiddleware())

db = sqlite3.connect(DB_NAME, check_same_thread=False)
db.execute("PRAGMA foreign_keys = ON")
cursor = db.cursor()


def column_exists(table, column):
    cursor.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cursor.fetchall())


# ==================== DATABASE ====================

cursor.execute("""
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
)
""")

# Миграция старой базы: добавляем поля категории без удаления существующих данных.
if not column_exists("categories", "description"):
    cursor.execute("ALTER TABLE categories ADD COLUMN description TEXT NOT NULL DEFAULT ''")

if not column_exists("categories", "photo_id"):
    cursor.execute("ALTER TABLE categories ADD COLUMN photo_id TEXT")

# Специальная категория для покупки Telegram Stars.
cursor.execute("SELECT id FROM categories WHERE LOWER(name) IN (?, ?, ?)", (
    "⭐ telegram stars",
    "telegram stars",
    "stars",
))
if cursor.fetchone() is None:
    cursor.execute(
        "INSERT INTO categories(name, description) VALUES (?, ?)",
        (
            STARS_CATEGORY_NAME,
            "⭐ Покупка Telegram Stars\n\nВведите любое количество Stars — стоимость рассчитывается автоматически по курсу 1 ⭐ = 1.50 ₽.",
        ),
    )


# PlayStation category.
cursor.execute(
    "SELECT id FROM categories WHERE LOWER(name) = LOWER(?)",
    (PLAYSTATION_CATEGORY_NAME,),
)
if cursor.fetchone() is None:
    cursor.execute(
        "INSERT INTO categories(name, description) VALUES (?, ?)",
        (PLAYSTATION_CATEGORY_NAME, PLAYSTATION_DESCRIPTION),
    )
db.commit()



cursor.execute("""
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    price REAL NOT NULL DEFAULT 0,
    photo_id TEXT,
    stars_price INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS cart (
    user_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (user_id, product_id),
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    username TEXT,
    customer_name TEXT,
    total REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT '🟡 Новый',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    paid INTEGER NOT NULL DEFAULT 0,
    payment_charge_id TEXT,
    currency TEXT,
    total_stars INTEGER
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    product_id INTEGER,
    product_name TEXT NOT NULL,
    price REAL NOT NULL,
    quantity INTEGER NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS inventory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    issued INTEGER NOT NULL DEFAULT 0,
    issued_order_id INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE,
    FOREIGN KEY (issued_order_id) REFERENCES orders(id) ON DELETE SET NULL
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS pending_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    total_stars INTEGER NOT NULL,
    items_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")

# Migrations for an existing shop.db
for table, column, definition in [
    ("products", "stars_price", "INTEGER NOT NULL DEFAULT 0"),
    ("products", "usdt_price", "REAL NOT NULL DEFAULT 0"),
    ("products", "quantity_enabled", "INTEGER NOT NULL DEFAULT 0"),
    ("products", "quantity_limit", "INTEGER NOT NULL DEFAULT 1"),
    ("orders", "paid", "INTEGER NOT NULL DEFAULT 0"),
    ("orders", "payment_charge_id", "TEXT"),
    ("orders", "currency", "TEXT"),
    ("orders", "total_stars", "INTEGER"),
    ("orders", "game_player_tag", "TEXT"),
    ("orders", "crypto_invoice_id", "TEXT"),
    ("order_items", "product_id", "INTEGER"),
]:
    if not column_exists(table, column):
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

if not column_exists("orders", "payment_proof_file_id"):
    cursor.execute("ALTER TABLE orders ADD COLUMN payment_proof_file_id TEXT")

db.commit()

# ==================== AI SUBSCRIPTIONS ====================
# Конкурентные цены на популярные AI-подписки.
# Ориентиры рынка: ChatGPT Plus и Claude Pro — около $20/мес у официальных сервисов.
# В магазине выставляем ниже ориентировочной розничной цены.
cursor.execute("SELECT id FROM categories WHERE LOWER(name) = LOWER(?)", ("🤖 AI Подписки",))
ai_category = cursor.fetchone()

if ai_category is None:
    cursor.execute(
        "INSERT INTO categories(name, description, photo_id) VALUES (?, ?, NULL)",
        (
            "🤖 AI Подписки",
            "🧠 Премиум-доступ к популярным AI-сервисам\n\n"
            "⚡ Быстрая активация\n"
            "💰 Цены ниже стандартной розницы\n"
            "🔒 Без необходимости покупать API отдельно\n"
            "📌 Перед покупкой проверь условия и регион активации.",
        ),
    )
    ai_category_id = cursor.lastrowid
else:
    ai_category_id = ai_category[0]

ai_products = [
    (
        "🤖 ChatGPT Plus — 1 месяц",
        "Премиум-доступ к ChatGPT Plus: расширенные лимиты, продвинутые модели и инструменты.",
        1590,
        15.90,
    ),
    (
        "🧠 Claude Pro — 1 месяц",
        "Премиум-доступ к Claude Pro: больше использования, Claude Code, проекты и дополнительные возможности.",
        1590,
        15.90,
    ),
]

for product_name, product_description, product_price, product_usdt in ai_products:
    cursor.execute(
        "SELECT id FROM products WHERE category_id = ? AND name = ?",
        (ai_category_id, product_name),
    )
    if cursor.fetchone() is None:
        cursor.execute(
            """
            INSERT INTO products(
                category_id, name, description, price, photo_id,
                stars_price, usdt_price, quantity_enabled, quantity_limit
            )
            VALUES (?, ?, ?, ?, NULL, 0, ?, 0, 1)
            """,
            (
                ai_category_id,
                product_name,
                product_description,
                product_price,
                product_usdt,
            ),
        )

db.commit()

# ==================== BRAWL STARS CATALOG ====================
# Готовая категория с конкурентными ценами. Данные создаются только один раз,
# поэтому при каждом запуске бота товары не дублируются.
cursor.execute("SELECT id FROM categories WHERE LOWER(name) = LOWER(?)", ("🎮 Brawl Stars",))
brawl_category = cursor.fetchone()

if brawl_category is None:
    cursor.execute(
        "INSERT INTO categories(name, description, photo_id) VALUES (?, ?, NULL)",
        (
            "🎮 Brawl Stars",
            "💎 Гемы и 🎟️ Brawl Pass\\n\\n"
            "⚡ Быстрое выполнение\\n"
            "💰 Цены ниже большинства актуальных предложений\\n"
            "🔒 Оплата через стандартную систему магазина",
        ),
    )
    brawl_category_id = cursor.lastrowid
else:
    brawl_category_id = brawl_category[0]

brawl_products = [
    ("💎 30 Gems", "30 гемов Brawl Stars. Выгодная цена для небольшой покупки.", 179, 1.79),
    ("💎 80 Gems", "80 гемов Brawl Stars. Оптимальный вариант для небольшого доната.", 449, 4.49),
    ("💎 170 Gems", "170 гемов Brawl Stars. Один из самых популярных пакетов.", 899, 8.99),
    ("💎 360 Gems", "360 гемов Brawl Stars. Выгодный пакет для регулярных покупок.", 1699, 16.99),
    ("💎 950 Gems", "950 гемов Brawl Stars. Большой пакет по сниженной цене.", 4399, 43.99),
    ("💎 2000 Gems", "2000 гемов Brawl Stars. Максимальная выгода за крупный пакет.", 8799, 87.99),
    ("🎟️ Brawl Pass", "Обычный Brawl Pass на текущий сезон с дополнительными наградами.", 649, 6.49),
    ("👑 Brawl Pass Plus", "Brawl Pass Plus: дополнительные награды и ускоренный прогресс.", 899, 11.99),
]

for product_name, product_description, product_price, product_usdt in brawl_products:
    cursor.execute(
        "SELECT id FROM products WHERE category_id = ? AND name = ?",
        (brawl_category_id, product_name),
    )
    if cursor.fetchone() is None:
        cursor.execute(
            """
            INSERT INTO products(
                category_id, name, description, price, photo_id,
                stars_price, usdt_price, quantity_enabled, quantity_limit
            )
            VALUES (?, ?, ?, ?, NULL, 0, ?, 0, 1)
            """,
            (
                brawl_category_id,
                product_name,
                product_description,
                product_price,
                product_usdt,
            ),
        )

db.commit()


# ==================== PLAYSTATION PRODUCTS — POPULAR NOMINALS ====================
# Цены ориентированы ниже найденных предложений для EUR PSN-карт.
# Важно: PSN-коды региональные — покупатель должен выбрать совместимый регион.
cursor.execute(
    "SELECT id FROM categories WHERE LOWER(name) = LOWER(?)",
    (PLAYSTATION_CATEGORY_NAME,),
)
playstation_category = cursor.fetchone()

if playstation_category is None:
    cursor.execute(
        "INSERT INTO categories(name, description, photo_id) VALUES (?, ?, NULL)",
        (
            PLAYSTATION_CATEGORY_NAME,
            "🎮 PlayStation Store\n\n"
            "💳 Цифровые PSN-коды в EUR\n"
            "⚡ Быстрая выдача\n"
            "💰 Цены ниже найденных предложений\n"
            "🌍 Код работает только на аккаунте совместимого региона\n\n"
            "Перед покупкой обязательно проверь регион PSN-аккаунта.",
        ),
    )
    playstation_category_id = cursor.lastrowid
else:
    playstation_category_id = playstation_category[0]

playstation_products = [
    (
        "🎮 PS Store — 10 EUR",
        "Пополнение кошелька PlayStation Store на 10 EUR. Региональный код.",
        990,
        9.90,
    ),
    (
        "🎮 PS Store — 20 EUR",
        "Пополнение кошелька PlayStation Store на 20 EUR. Региональный код.",
        1890,
        18.90,
    ),
    (
        "🎮 PS Store — 50 EUR",
        "Пополнение кошелька PlayStation Store на 50 EUR. Региональный код.",
        4690,
        46.90,
    ),
    (
        "🎮 PS Store — 100 EUR",
        "Пополнение кошелька PlayStation Store на 100 EUR. Региональный код.",
        9390,
        93.90,
    ),
    (
        "⭐ PS Plus Essential — 1 месяц",
        "PlayStation Plus Essential на 1 месяц. Региональная активация.",
        799,
        7.99,
    ),
    (
        "⭐ PS Plus Essential — 3 месяца",
        "PlayStation Plus Essential на 3 месяца. Выгоднее помесячной покупки.",
        2290,
        22.90,
    ),
    (
        "⭐ PS Plus Essential — 12 месяцев",
        "PlayStation Plus Essential на 12 месяцев. Максимальная выгода.",
        6490,
        64.90,
    ),
]

for product_name, product_description, product_price, product_usdt in playstation_products:
    cursor.execute(
        "SELECT id FROM products WHERE category_id = ? AND name = ?",
        (playstation_category_id, product_name),
    )
    if cursor.fetchone() is None:
        cursor.execute(
            """
            INSERT INTO products(
                category_id, name, description, price, photo_id,
                stars_price, usdt_price, quantity_enabled, quantity_limit
            )
            VALUES (?, ?, ?, ?, NULL, 0, ?, 0, 1)
            """,
            (
                playstation_category_id,
                product_name,
                product_description,
                product_price,
                product_usdt,
            ),
        )

db.commit()

# ==================== STEAM TOP-UP CATALOG ====================
# Цены выставлены конкурентно относительно найденных актуальных предложений.
# Перед продажей проверь свою себестоимость/комиссию поставщика, чтобы сохранить маржу.
cursor.execute("SELECT id FROM categories WHERE LOWER(name) = LOWER(?)", ("🎮 Steam",))
steam_category = cursor.fetchone()
if steam_category is None:
    cursor.execute(
        "INSERT INTO categories(name, description, photo_id) VALUES (?, ?, NULL)",
        (
            "🎮 Steam",
            "💳 Пополнение Steam Wallet\n\n"
            "⚡ Быстрое пополнение\n"
            "💰 Конкурентные цены\n"
            "🔐 Нужен только логин Steam — пароль не требуется\n"
            "🌍 Для аккаунтов с валютой RUB",
        ),
    )
    steam_category_id = cursor.lastrowid
else:
    steam_category_id = steam_category[0]

steam_products = []

for product_name, product_description, product_price, product_usdt in steam_products:
    cursor.execute(
        "SELECT id FROM products WHERE category_id = ? AND name = ?",
        (steam_category_id, product_name),
    )
    if cursor.fetchone() is None:
        cursor.execute(
            """
            INSERT INTO products(
                category_id, name, description, price, photo_id,
                stars_price, usdt_price, quantity_enabled, quantity_limit
            )
            VALUES (?, ?, ?, ?, NULL, 0, ?, 0, 1)
            """,
            (steam_category_id, product_name, product_description, product_price, product_usdt),
        )

db.commit()

# ==================== SPOTIFY PREMIUM ====================
# Актуальные ориентиры рынка на 2026 год: у одного из крупных российских
# сервисов Individual стоит 690 ₽/мес, 1690 ₽/3 мес, 2990 ₽/6 мес и 4490 ₽/год.
# Делаем цены Felecaster ниже этих предложений, но перед продажей проверьте
# свою себестоимость и регион активации.
cursor.execute("SELECT id FROM categories WHERE LOWER(name) = LOWER(?)", ("🎵 Spotify Premium",))
spotify_category = cursor.fetchone()
if spotify_category is None:
    cursor.execute(
        "INSERT INTO categories(name, description, photo_id) VALUES (?, ?, NULL)",
        (
            "🎵 Spotify Premium",
            "🎧 Музыка без рекламы и с офлайн-прослушиванием\n\n"
            "⚡ Быстрая активация\n"
            "💰 Цена ниже найденных актуальных предложений\n"
            "🔐 Нужен только аккаунт Spotify — пароль не требуется\n"
            "🌍 Перед покупкой проверь регион аккаунта",
        ),
    )
    spotify_category_id = cursor.lastrowid
else:
    spotify_category_id = spotify_category[0]

spotify_products = [
    ("🎵 Spotify Premium — 1 месяц", "Premium Individual на 1 месяц.", 649, 6.49),
    ("🎵 Spotify Premium — 3 месяца", "Premium Individual на 3 месяца. Выгоднее помесячной покупки.", 1590, 15.90),
    ("🎵 Spotify Premium — 6 месяцев", "Premium Individual на 6 месяцев. Большая экономия.", 2790, 27.90),
    ("🎵 Spotify Premium — 12 месяцев", "Premium Individual на 12 месяцев. Максимальная выгода.", 4190, 41.90),
]

for product_name, product_description, product_price, product_usdt in spotify_products:
    cursor.execute(
        "SELECT id FROM products WHERE category_id = ? AND name = ?",
        (spotify_category_id, product_name),
    )
    if cursor.fetchone() is None:
        cursor.execute(
            """
            INSERT INTO products(
                category_id, name, description, price, photo_id,
                stars_price, usdt_price, quantity_enabled, quantity_limit
            )
            VALUES (?, ?, ?, ?, NULL, 0, ?, 0, 1)
            """,
            (spotify_category_id, product_name, product_description, product_price, product_usdt),
        )

db.commit()

cursor.execute("""
CREATE TABLE IF NOT EXISTS banned_users (
    user_id INTEGER PRIMARY KEY,
    reason TEXT DEFAULT '',
    banned_at TEXT DEFAULT CURRENT_TIMESTAMP
)
""")
db.commit()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    last_seen TEXT DEFAULT CURRENT_TIMESTAMP
)
""")

# Migration for an existing shop.db: older versions may not have last_name.
if not column_exists("users", "last_name"):
    cursor.execute("ALTER TABLE users ADD COLUMN last_name TEXT")
if not column_exists("users", "last_seen"):
    cursor.execute("ALTER TABLE users ADD COLUMN last_seen TEXT")

db.commit()

# Персональные горячие предложения.
# Каждый пользователь получает 1 случайное предложение в случайный момент суток.
cursor.execute("""
CREATE TABLE IF NOT EXISTS user_hot_offers (
    user_id INTEGER NOT NULL,
    offer_date TEXT NOT NULL,
    product_id INTEGER NOT NULL,
    discount INTEGER NOT NULL,
    scheduled_at TEXT NOT NULL,
    expires_at TEXT,
    sent INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, offer_date),
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
)
""")
db.commit()

# Ежедневная крутилка: один бесплатный спин на пользователя в сутки.
# Приз последнего спина хранится как активный результат до следующего спина.
cursor.execute("""
CREATE TABLE IF NOT EXISTS daily_spins (
    user_id INTEGER NOT NULL,
    spin_date TEXT NOT NULL,
    prize TEXT NOT NULL,
    PRIMARY KEY (user_id, spin_date),
    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
)
""")
db.commit()

# Editable /start message
cursor.execute("""CREATE TABLE IF NOT EXISTS bot_settings (key TEXT PRIMARY KEY, value TEXT)""")
cursor.execute("SELECT value FROM bot_settings WHERE key='start_message'")
if cursor.fetchone() is None:
    cursor.execute("INSERT INTO bot_settings(key, value) VALUES (?, ?)", (
        'start_message',
        '🟣 <b>FELECASTER SHOP</b>\n\n'
        'Привет, <b>{name}</b> 👋\n'
        '<i>Цифровые товары и подписки — прямо в Telegram.</i>\n\n'
        '🔥 <b>Горячие предложения</b>\n'
        '🎁 Ежедневная крутилка\n'
        '⚡ Быстрая обработка заказов\n'
        '🛡 Поддержка, если понадобится помощь\n\n'
        '<code>Выберите раздел ниже</code>'
    ))

# Фото для сообщения /start
cursor.execute("SELECT value FROM bot_settings WHERE key='start_photo_id'")
if cursor.fetchone() is None:
    cursor.execute("INSERT INTO bot_settings(key, value) VALUES (?, ?)", ('start_photo_id', ''))

# Редактируемое сообщение каталога
cursor.execute("SELECT value FROM bot_settings WHERE key='catalog_message'")
if cursor.fetchone() is None:
    cursor.execute("INSERT INTO bot_settings(key, value) VALUES (?, ?)", (
        'catalog_message',
        '🛍 <b>КАТАЛОГ FELECASTER</b>\n\n'
        '<i>Выбирай категорию — внутри только актуальные предложения.</i>\n\n'
        '🟣 <b>Быстро</b> • 💳 <b>Удобно</b> • 🛡 <b>Надёжно</b>'
    ))

cursor.execute("SELECT value FROM bot_settings WHERE key='catalog_photo_id'")
if cursor.fetchone() is None:
    cursor.execute("INSERT INTO bot_settings(key, value) VALUES (?, ?)", ('catalog_photo_id', ''))

# Мягкая миграция визуального стиля: старые системные шаблоны заменяются
# только если они совпадают с прежним встроенным дизайном. Пользовательские
# сообщения, изменённые через админку, не трогаем.
legacy_start_markers = ("FELECASTER OS", "СЕССИЯ ЗАПУЩЕНА", "ЯДРО МАГАЗИНА")
legacy_catalog_markers = ("ЯДРО МАГАЗИНА", "FELECASTER OS // ДОСТУП К МАГАЗИНУ")

cursor.execute("SELECT value FROM bot_settings WHERE key='start_message'")
current_start = cursor.fetchone()
if current_start and all(marker in current_start[0] for marker in legacy_start_markers):
    cursor.execute("UPDATE bot_settings SET value=? WHERE key='start_message'", (
        '🟣 <b>FELECASTER SHOP</b>\n\n'
        'Привет, <b>{name}</b> 👋\n'
        '<i>Цифровые товары и подписки — прямо в Telegram.</i>\n\n'
        '🔥 <b>Горячие предложения</b>\n'
        '🎁 Ежедневная крутилка\n'
        '⚡ Быстрая обработка заказов\n'
        '🛡 Поддержка, если понадобится помощь\n\n'
        '<code>Выберите раздел ниже</code>',
    ))

cursor.execute("SELECT value FROM bot_settings WHERE key='catalog_message'")
current_catalog = cursor.fetchone()
if current_catalog and all(marker in current_catalog[0] for marker in legacy_catalog_markers):
    cursor.execute("UPDATE bot_settings SET value=? WHERE key='catalog_message'", (
        '🛍 <b>КАТАЛОГ FELECASTER</b>\n\n'
        '<i>Выбирай категорию — внутри только актуальные предложения.</i>\n\n'
        '🟣 <b>Быстро</b> • 💳 <b>Удобно</b> • 🛡 <b>Надёжно</b>',
    ))

cursor.execute("CREATE TABLE IF NOT EXISTS promo_codes (code TEXT PRIMARY KEY, discount INTEGER NOT NULL DEFAULT 0, max_uses INTEGER DEFAULT 0, uses INTEGER DEFAULT 0, active INTEGER DEFAULT 1)")
cursor.execute("CREATE TABLE IF NOT EXISTS user_promos (user_id INTEGER PRIMARY KEY, code TEXT NOT NULL, discount INTEGER NOT NULL DEFAULT 0)")

# Надёжная миграция промокодов для уже существующего shop.db.
# Старый файл базы мог быть создан до появления отдельных полей max_uses/uses/active.
def _ensure_column(table, column, definition):
    cursor.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cursor.fetchall()}
    if column not in existing:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

_ensure_column("promo_codes", "discount", "INTEGER NOT NULL DEFAULT 0")
_ensure_column("promo_codes", "max_uses", "INTEGER DEFAULT 0")
_ensure_column("promo_codes", "uses", "INTEGER DEFAULT 0")
_ensure_column("promo_codes", "active", "INTEGER DEFAULT 1")
_ensure_column("user_promos", "code", "TEXT NOT NULL DEFAULT ''")
_ensure_column("user_promos", "discount", "INTEGER NOT NULL DEFAULT 0")
db.commit()


# ==================== STEAM ACCOUNTS ====================
# Отдельная категория аккаунтов Steam. Не смешивается с категорией
# "🎮 Steam", которая используется для пополнения Steam Wallet.
# Реальные логины/пароли в код не добавляются — их можно положить в inventory
# через админ-панель для автоматической выдачи после оплаты.
cursor.execute(
    "SELECT id FROM categories WHERE LOWER(name) = LOWER(?)",
    ("🎮 Аккаунты Steam",),
)
steam_accounts_category = cursor.fetchone()

if steam_accounts_category is None:
    cursor.execute(
        "INSERT INTO categories(name, description, photo_id) VALUES (?, ?, NULL)",
        (
            "🎮 Аккаунты Steam",
            "🎮 Готовые игровые аккаунты Steam\n\n"
            "⚡ Быстрая выдача после оплаты\n"
            "🔐 Данные для входа выдаются после покупки\n"
            "🎯 В категории представлены аккаунты с разными играми\n"
            "📌 Перед покупкой внимательно ознакомьтесь с описанием товара.",
        ),
    )
    steam_accounts_category_id = cursor.lastrowid
else:
    steam_accounts_category_id = steam_accounts_category[0]

steam_account_products = [
    (
        "🎮 GTA V Enhanced | Steam | 300₽",
        "🎮 <b>GTA V Enhanced | Steam</b>\n\n"
        "🔥 Готовый аккаунт Steam с GTA V Enhanced.\n"
        "⚡️ Быстрая выдача данных после оплаты.\n\n"
        "📌 <b>Характеристики:</b>\n"
        "• 🎮 Игра: Grand Theft Auto V Enhanced\n"
        "• 🖥 Платформа: Steam\n"
        "• 🔐 Данные для входа выдаются после оплаты\n"
        "• ⚡️ Быстрая выдача\n"
        "• 💰 Цена: 300₽\n\n"
        "❗️ Фактические дополнительные характеристики аккаунта зависят от конкретного лота.\n"
        "❤️ Спасибо за покупку!",
        300,
        3.00,
    ),
    (
        "🦀 Rust | Steam | 550₽",
        "🦀 <b>Rust | Steam</b>\n\n"
        "🔥 Готовый аккаунт Steam с игрой Rust.\n"
        "⚡️ Быстрая выдача данных после оплаты.\n\n"
        "📌 <b>Характеристики:</b>\n"
        "• 🦀 Игра: Rust\n"
        "• 🖥 Платформа: Steam\n"
        "• 🔐 Данные для входа выдаются после оплаты\n"
        "• ⚡️ Быстрая выдача\n"
        "• 💰 Цена: 550₽\n\n"
        "❗️ Фактические дополнительные характеристики аккаунта зависят от конкретного лота.\n"
        "❤️ Спасибо за покупку!",
        550,
        5.50,
    ),
    (
        "🎖 Arma 3 | Steam | 950₽",
        "🎖 <b>Arma 3 | Steam</b>\n\n"
        "🔥 Готовый аккаунт Steam с игрой Arma 3.\n"
        "⚡️ Быстрая выдача данных после оплаты.\n\n"
        "📌 <b>Характеристики:</b>\n"
        "• 🎖 Игра: Arma 3\n"
        "• 🖥 Платформа: Steam\n"
        "• 🔐 Данные для входа выдаются после оплаты\n"
        "• ⚡️ Быстрая выдача\n"
        "• 💰 Цена: 950₽\n\n"
        "❗️ Фактические дополнительные характеристики аккаунта зависят от конкретного лота.\n"
        "❤️ Спасибо за покупку!",
        950,
        9.50,
    ),
]

for product_name, product_description, product_price, product_usdt in steam_account_products:
    cursor.execute(
        "SELECT id FROM products WHERE category_id = ? AND name = ?",
        (steam_accounts_category_id, product_name),
    )
    if cursor.fetchone() is None:
        cursor.execute(
            """
            INSERT INTO products(
                category_id, name, description, price, photo_id,
                stars_price, usdt_price, quantity_enabled, quantity_limit
            )
            VALUES (?, ?, ?, ?, NULL, 0, ?, 0, 1)
            """,
            (
                steam_accounts_category_id,
                product_name,
                product_description,
                product_price,
                product_usdt,
            ),
        )

db.commit()


# ==================== HELPERS ====================

def money(value):
    return f"{value:.2f} ₽"


def esc(value):
    return html.escape(str(value))


def user_menu(user_id):
    builder = InlineKeyboardBuilder()
    buttons = [
        ("🛍 Каталог", "menu_catalog"),
        ("🎟 Промокод", "menu_promo"),
        ("🛒 Корзина", "menu_cart"),
        ("📦 Заказы", "menu_orders"),
        ("🎰 Крутилка", "daily_spin"),
        ("👤 Профиль", "menu_profile"),
        ("🏪 О магазине", "menu_about"),
        ("💬 Поддержка", "menu_support"),
    ]
    if user_id == ADMIN_ID:
        buttons.append(("⚙️ Панель управления", "menu_admin"))
    for text, data in buttons:
        builder.button(text=text, callback_data=data)
    builder.adjust(2, 2, 2, 2, 1)
    return builder.as_markup()


def admin_menu():
    builder = InlineKeyboardBuilder()
    buttons = [
        ("📊 Статистика", "admin_statistics"),
        ("🎟 Промокоды", "admin_promos"),
        ("👥 Пользователи", "admin_users"),
        ("💬 Поддержка", "admin_support"),
        ("📦 Заказы", "admin_orders"),
        ("📁 Добавить категорию", "admin_add_category"),
        ("🛍 Добавить товар", "admin_add_product"),
        ("📋 Товары", "admin_products"),
        ("🎁 Добавить ключи", "admin_add_keys"),
        ("🗂 Категории", "admin_categories"),
        ("✏️ Сообщение /start", "admin_start_message"),
        ("🛍 Сообщение каталога", "admin_catalog_message"),
        ("📢 Рассылка", "admin_broadcast"),
        ("🏠 Главное меню", "menu_home"),
    ]
    for text, data in buttons:
        builder.button(text=text, callback_data=data)
    builder.adjust(2, 2, 2, 2, 2, 1)
    return builder.as_markup()


def categories_keyboard(prefix):
    """Категории: 2 в строке, главное меню отдельной строкой."""
    builder = InlineKeyboardBuilder()
    cursor.execute("SELECT id, name FROM categories ORDER BY id DESC")
    categories = cursor.fetchall()

    for category_id, name in categories:
        builder.button(
            text=f" {name}",
            callback_data=f"{prefix}:{category_id}"
        )

    # Ровно по две категории в каждой строке.
    if categories:
        builder.adjust(2)

    # Главное меню — отдельной строкой на всю ширину.
    builder.row(InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_home"))

    return builder.as_markup()


def admin_products_keyboard(prefix="manage_product"):
    builder = InlineKeyboardBuilder()
    cursor.execute("SELECT id, name FROM products ORDER BY id DESC")
    for product_id, name in cursor.fetchall():
        builder.button(text=f"🛍 {name}", callback_data=f"{prefix}:{product_id}")
    builder.adjust(1)
    return builder.as_markup()


def get_cart(user_id):
    cursor.execute("""
        SELECT c.product_id, p.name, p.price, c.quantity
        FROM cart c
        JOIN products p ON p.id = c.product_id
        WHERE c.user_id = ?
        ORDER BY p.id DESC
    """, (user_id,))
    return cursor.fetchall()


def get_user_promo(user_id):
    cursor.execute("SELECT code, discount FROM user_promos WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    return (row[0], int(row[1])) if row else (None, 0)


def promo_cart_keyboard(items, user_id):
    builder = InlineKeyboardBuilder()
    for product_id, name, price, quantity in items:
        builder.button(text=f"➖ {name}", callback_data=f"cart_minus:{product_id}")
        builder.button(text=f"{quantity} шт.", callback_data="noop")
        builder.button(text="➕", callback_data=f"cart_plus:{product_id}")
        builder.button(text="🗑", callback_data=f"cart_delete:{product_id}")
    code, discount = get_user_promo(user_id)
    builder.button(text=(f"🎟 {code} • −{discount}%" if discount else "🎟 Ввести промокод"), callback_data="promo_enter")
    if discount:
        builder.button(text="❌ Убрать промокод", callback_data="promo_remove")
    builder.button(text="💳 Перейти к оплате", callback_data="checkout")
    builder.button(text="🛍 Вернуться в каталог", callback_data="catalog_back")
    builder.button(text="🏠 Главное меню", callback_data="catalog_home")
    builder.adjust(3, 1)
    return builder.as_markup()


def cart_keyboard(items):
    builder = InlineKeyboardBuilder()
    for product_id, name, price, quantity in items:
        builder.button(text=f"➖ {name}", callback_data=f"cart_minus:{product_id}")
        builder.button(text=f"{quantity} шт.", callback_data="noop")
        builder.button(text="➕", callback_data=f"cart_plus:{product_id}")
        builder.button(text="🗑", callback_data=f"cart_delete:{product_id}")
    builder.button(text="💳 Перейти к оплате", callback_data="checkout")
    builder.button(text="🛍 Вернуться в каталог", callback_data="catalog_back")
    builder.button(text="🏠 Главное меню", callback_data="catalog_home")
    builder.adjust(3, 1)
    return builder.as_markup()


def available_stock(product_id):
    cursor.execute("""
        SELECT COUNT(*)
        FROM inventory
        WHERE product_id = ? AND issued = 0
    """, (product_id,))
    return cursor.fetchone()[0]


async def send_cart(message: Message):
    items = get_cart(message.from_user.id)

    if not items:
        await message.answer(
            "🛒 <b>КОРЗИНА</b>\n\n"
            "Корзина пуста. Загляните в каталог 👇",
            parse_mode="HTML",
        )
        return

    discount = get_active_spin_discount(message.from_user.id)
    subtotal = sum(float(price) * qty for _, _, price, qty in items)
    total = sum(apply_discount(price, discount) * qty for _, _, price, qty in items)

    text = "🛒 <b>ВАША КОРЗИНА</b>\n\n"

    for product_id, name, price, qty in items:
        unit_price = apply_discount(price, discount)
        if discount:
            text += (
                f"• <b>{esc(name)}</b>\n"
                f"  <s>{money(price)}</s> → <b>{money(unit_price)}</b> × {qty}\n\n"
            )
        else:
            text += (
                f"• <b>{esc(name)}</b>\n"
                f"  {money(price)} × {qty}\n\n"
            )

    promo_code, promo_discount = get_user_promo(message.from_user.id)
    if promo_discount:
        promo_total = round(total * (1 - promo_discount / 100), 2)
        text += f"🎟 Промокод <b>{esc(promo_code)}</b>: −{promo_discount}%\n"
        text += f"💰 Было: <s>{money(total)}</s> → <b>{money(promo_total)}</b>\n"
        total = promo_total

    text += "\n🟣 <b>ИТОГ</b>\n"
    if discount:
        text += (
            f"💰 Подытог: <s>{money(subtotal)}</s>\n"
            f"🎉 <b>Скидка {discount}%</b> на весь ассортимент\n"
        )
    text += f"💳 <b>Итого: {money(total)}</b>"

    await message.answer(
        text,
        reply_markup=promo_cart_keyboard(items, message.from_user.id),
        parse_mode="HTML",
    )


# ==================== STATES ====================

class BroadcastState(StatesGroup):
    content = State()
    button_text = State()
    button_url = State()


class EditCategoryState(StatesGroup):
    name = State()
    description = State()
    photo = State()


class AddCategoryState(StatesGroup):
    name = State()


class AddProductState(StatesGroup):
    category = State()
    name = State()
    description = State()
    price = State()
    usdt = State()
    photo = State()


class AddKeysState(StatesGroup):
    product = State()
    codes = State()


class ManualDeliveryState(StatesGroup):
    text = State()


class BanUser(StatesGroup):
    user_id = State()
    reason = State()


class SupportState(StatesGroup):
    message = State()


class PaymentProofState(StatesGroup):
    waiting_screenshot = State()


class BrawlPlayerTagState(StatesGroup):
    player_tag = State()


class SteamLoginState(StatesGroup):
    login = State()

class SteamAmountState(StatesGroup):
    amount = State()


class SpotifyAccountState(StatesGroup):
    account = State()


class StarsPurchaseState(StatesGroup):
    waiting_amount = State()


class PromoCodeState(StatesGroup):
    code = State()


class AdminPromoState(StatesGroup):
    code = State()
    discount = State()
    max_uses = State()


class AdminReplyState(StatesGroup):
    user_id = State()
    message = State()


class StartMessageState(StatesGroup):
    text = State()


class CatalogMessageState(StatesGroup):
    content = State()


# ==================== USER / ПОДДЕРЖКА HELPERS ====================

def register_user(user):
    cursor.execute("""
        INSERT INTO users(user_id, username, first_name, last_name, last_seen)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            last_name=excluded.last_name,
            last_seen=CURRENT_TIMESTAMP
    """, (user.id, user.username, user.first_name, user.last_name))
    db.commit()


def users_keyboard(prefix="user_manage"):
    builder = InlineKeyboardBuilder()
    cursor.execute("""
        SELECT u.user_id, u.username, u.first_name,
               EXISTS(SELECT 1 FROM banned_users b WHERE b.user_id=u.user_id)
        FROM users u
        WHERE u.user_id != ?
        ORDER BY u.last_seen DESC
        LIMIT 50
    """, (ADMIN_ID,))
    rows = cursor.fetchall()
    for user_id, username, first_name, banned in rows:
        name = first_name or username or str(user_id)
        if username:
            name += f" @{username}"
        builder.button(text=f"{'🚫' if banned else '👤'} {name[:45]}", callback_data=f"{prefix}:{user_id}")
    if not rows:
        builder.button(text="Пока нет пользователей", callback_data="noop")
    builder.button(text="⬅️ Админ-панель", callback_data="admin_back")
    builder.adjust(1)
    return builder.as_markup()


def user_manage_keyboard(user_id, banned):
    builder = InlineKeyboardBuilder()
    if banned:
        builder.button(text="🔓 Разблокировать", callback_data=f"unban_user:{user_id}")
    else:
        builder.button(text="🚫 Заблокировать", callback_data=f"ban_selected:{user_id}")
    builder.button(text="💬 Написать", callback_data=f"support_user:{user_id}")
    builder.button(text="⬅️ К пользователям", callback_data="users_list")
    builder.adjust(1)
    return builder.as_markup()


def get_start_message(name):
    cursor.execute("SELECT value FROM bot_settings WHERE key='start_message'")
    row = cursor.fetchone()
    template = row[0] if row else "✨ <b>Felecaster Shop</b>\n\nПривет, {name} 👋\n\nВыберите раздел ниже 👇"
    return template.replace("{name}", name)


def get_start_photo():
    cursor.execute("SELECT value FROM bot_settings WHERE key='start_photo_id'")
    row = cursor.fetchone()
    return row[0] if row and row[0] else None


def get_catalog_message():
    cursor.execute("SELECT value FROM bot_settings WHERE key='catalog_message'")
    row = cursor.fetchone()
    return row[0] if row and row[0] else "🛍 <b>КАТАЛОГ</b>\n\nВыберите категорию — всё необходимое уже здесь."


def get_catalog_photo():
    cursor.execute("SELECT value FROM bot_settings WHERE key='catalog_photo_id'")
    row = cursor.fetchone()
    return row[0] if row and row[0] else None


async def send_catalog(target):
    """Отправляет редактируемое приветственное сообщение каталога и категории."""
    cursor.execute("SELECT COUNT(*) FROM categories")
    has_categories = cursor.fetchone()[0] > 0
    text = get_catalog_message()
    photo = get_catalog_photo()
    markup = categories_keyboard("show_category") if has_categories else None

    if photo:
        await target.answer_photo(
            photo=photo,
            caption=text,
            reply_markup=markup,
            parse_mode="HTML",
        )
    else:
        await target.answer(
            text,
            reply_markup=markup,
            parse_mode="HTML",
        )

    if not has_categories:
        await target.answer("◈ <b>ЯДРО МАГАЗИНА</b>\n\n<code>СТАТУС: ПУСТО</code>\nКаталог пока пуст.")


# ==================== ОБЯЗАТЕЛЬНАЯ ПОДПИСКА ====================

@dp.callback_query(F.data == "subscription_join")
async def subscription_join(callback: CallbackQuery):
    await callback.answer("Откройте канал и подпишитесь на него.", show_alert=True)


@dp.callback_query(F.data == "subscription_check")
async def subscription_check(callback: CallbackQuery):
    """Повторно проверяет подписку напрямую через Telegram Bot API."""
    try:
        member = await callback.bot.get_chat_member(
            chat_id=SUBSCRIPTION_CHANNEL,
            user_id=callback.from_user.id,
        )
        status = getattr(member, "status", "")
        is_subscribed = status in {"creator", "administrator", "member"}
        if status == "restricted":
            is_subscribed = bool(getattr(member, "is_member", False))

        print(
            f"[SUBSCRIPTION BUTTON] user_id={callback.from_user.id} "
            f"channel={SUBSCRIPTION_CHANNEL!r} status={status!r} "
            f"subscribed={is_subscribed}"
        )
    except Exception as e:
        print(
            f"[SUBSCRIPTION BUTTON ERROR] channel={SUBSCRIPTION_CHANNEL!r} "
            f"user_id={callback.from_user.id}: {type(e).__name__}: {e}"
        )
        await callback.answer(
            "⚠️ Telegram не дал проверить подписку.\n\n"
            "Убедись, что бот добавлен администратором в Felecaster News.",
            show_alert=True,
        )
        return

    if not is_subscribed:
        await callback.answer(
            f"❌ Подписка не найдена.\n\nСтатус Telegram: {status}",
            show_alert=True,
        )
        return

    await callback.answer("✅ Подписка подтверждена!")

    # Удаляем экран подписки только после успешной проверки.
    if callback.message:
        try:
            await callback.message.delete()
        except Exception:
            pass

    name = esc(callback.from_user.first_name or "друг")
    start_text = get_start_message(name)
    start_photo = get_start_photo()

    if start_photo:
        await callback.bot.send_photo(
            chat_id=callback.from_user.id,
            photo=start_photo,
            caption=start_text,
            reply_markup=user_menu(callback.from_user.id),
            parse_mode="HTML",
        )
    else:
        await callback.bot.send_message(
            chat_id=callback.from_user.id,
            text=start_text,
            reply_markup=user_menu(callback.from_user.id),
            parse_mode="HTML",
        )


# ==================== START ====================


async def check_banned_user(message: Message) -> bool:
    if message.from_user.id == ADMIN_ID:
        return False

    cursor.execute(
        "SELECT reason FROM banned_users WHERE user_id = ?",
        (message.from_user.id,)
    )
    row = cursor.fetchone()

    if row:
        reason = row[0] or "Причина не указана"
        await message.answer(
            "🚫 <b>Доступ заблокирован</b>\n\n"
            f"Причина: {esc(reason)}",
            parse_mode="HTML"
        )
        return True

    return False


@dp.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    register_user(message.from_user)
    if await check_banned_user(message):
        return

    await state.clear()

    name = esc(message.from_user.first_name or "друг")

    start_text = get_start_message(name)
    start_photo = get_start_photo()

    if start_photo:
        await message.answer_photo(
            photo=start_photo,
            caption=start_text,
            reply_markup=user_menu(message.from_user.id),
            parse_mode="HTML",
        )
    else:
        await message.answer(
            start_text,
            reply_markup=user_menu(message.from_user.id),
            parse_mode="HTML",
        )


@dp.message(F.text == "🏠 Главное меню")
async def home(message: Message):
    register_user(message.from_user)
    if await check_banned_user(message):
        return

    name = esc(message.from_user.first_name or "друг")
    start_text = get_start_message(name)
    start_photo = get_start_photo()

    if start_photo:
        await message.answer_photo(
            photo=start_photo,
            caption=start_text,
            reply_markup=user_menu(message.from_user.id),
            parse_mode="HTML",
        )
    else:
        await message.answer(
            start_text,
            reply_markup=user_menu(message.from_user.id),
            parse_mode="HTML",
        )


# ==================== CATALOG ====================

@dp.message(F.text == "🛍 Каталог")
async def catalog(message: Message):
    register_user(message.from_user)
    if await check_banned_user(message):
        return

    await send_catalog(message)


def products_keyboard(category_id):
    """Показывает товары выбранной категории отдельными кнопками."""
    builder = InlineKeyboardBuilder()

    cursor.execute("""
        SELECT id, name
        FROM products
        WHERE category_id = ?
        ORDER BY id DESC
    """, (category_id,))

    products = cursor.fetchall()

    for product_id, name in products:
        builder.button(
            text=f"{name}",
            callback_data=f"show_product:{product_id}"
        )

    builder.button(
        text="⬅️ Назад к категориям",
        callback_data="catalog_back"
    )
    builder.button(
        text="🏠 Главное меню",
        callback_data="catalog_home"
    )

    builder.adjust(1)
    return builder.as_markup()


@dp.callback_query(F.data.startswith("show_category:"))
async def show_category(callback: CallbackQuery):
    category_id = int(callback.data.split(":")[1])

    cursor.execute(
        "SELECT name, description, photo_id FROM categories WHERE id = ?",
        (category_id,),
    )
    category = cursor.fetchone()

    if not category:
        await callback.answer("Категория не найдена.", show_alert=True)
        return

    name, description, photo_id = category

    cursor.execute("""
        SELECT id, name
        FROM products
        WHERE category_id = ?
        ORDER BY id DESC
    """, (category_id,))
    products = cursor.fetchall()

    text = f"🟣 <b>{esc(name)}</b>"
    if description:
        text += f"\n\n{esc(description)}"
    text += "\n\n🟢 <b>Товар доступен для заказа</b>"

    # Telegram Stars — специальная категория: количество вводит сам пользователь.
    is_stars_category = name.strip().lower().replace("⭐", "").strip() in (
        "telegram stars",
        "stars",
    )
    is_steam_category = name.strip().lower().replace("🎮", "").strip() == "steam"

    builder = InlineKeyboardBuilder()
    if is_steam_category:
        text += (
            "\n\n💳 <b>Пополнение Steam Wallet</b>\n"
            "Введи любую сумму пополнения — бот автоматически рассчитает стоимость.\n"
            "🔐 Нужен только логин Steam, пароль не требуется."
        )
        builder.button(text="💳 Пополнить Steam", callback_data="steam_topup_start")
        builder.button(text="⬅️ Назад к категориям", callback_data="catalog_back")
        builder.button(text="🏠 Главное меню", callback_data="catalog_home")
        builder.adjust(1)
        markup = builder.as_markup()
    elif is_stars_category:
        text += (
            "\n\n⭐ <b>Покупка Stars</b>\n"
            "Введите любое количество — стоимость: <b>1 ⭐ = 1.50 ₽</b>."
        )
        builder.button(text="⭐ Купить Stars", callback_data="stars_buy_start")
        builder.button(text="⬅️ Назад к категориям", callback_data="catalog_back")
        builder.button(text="🏠 Главное меню", callback_data="catalog_home")
        builder.adjust(1)
        markup = builder.as_markup()
    elif products:
        text += "\n\n<b>Выберите товар:</b>"
        # Используем существующую клавиатуру товаров.
        markup = products_keyboard(category_id)
    else:
        text += "\n\nПока здесь пусто."
        builder.button(text="⬅️ Назад к категориям", callback_data="catalog_back")
        builder.button(text="🏠 Главное меню", callback_data="catalog_home")
        builder.adjust(1)
        markup = builder.as_markup()

    if photo_id:
        await callback.message.answer_photo(
            photo=photo_id,
            caption=text,
            reply_markup=markup,
            parse_mode="HTML",
        )
    else:
        await callback.message.answer(
            text,
            reply_markup=markup,
            parse_mode="HTML",
        )

    await callback.answer()


@dp.callback_query(F.data.startswith("show_product:"))
async def show_product(callback: CallbackQuery):
    product_id = int(callback.data.split(":")[1])

    cursor.execute("""
        SELECT id, category_id, name, description, price, photo_id,
               stars_price, usdt_price, quantity_enabled, quantity_limit
        FROM products
        WHERE id = ?
    """, (product_id,))

    product = cursor.fetchone()

    if not product:
        await callback.answer(
            "Товар больше недоступен.",
            show_alert=True,
        )
        return

    (
        product_id,
        category_id,
        name,
        description,
        price,
        photo_id,
        stars_price,
        usdt_price,
        quantity_enabled,
        quantity_limit,
    ) = product

    stock = available_stock(product_id)

    quantity_text = (
        f"🔢 Макс. за заказ: {int(quantity_limit)} шт."
        if quantity_enabled
        else
        "🔢 Количество: 1 шт."
    )

    stock_text = (
        f"🎁 На складе ключей: {stock}"
        if stock
        else
        "🛠 Выдача: вручную"
    )

    hot_discount = get_hot_discount_for_product(callback.from_user.id, product_id)
    if hot_discount:
        hot_price = apply_discount(price, hot_discount)
        hot_usdt = apply_discount(usdt_price, hot_discount) if usdt_price else 0
        price_text = (
            f"🔥 <b>ГОРЯЧАЯ ЦЕНА: {money(hot_price)}</b> "
            f"<s>{money(price)}</s> (-{hot_discount}%)\n"
        )
        usdt_text = (
            f"🔥 USDT: <b>{float(hot_usdt):.2f} USDT</b> "
            f"<s>{float(usdt_price or 0):.2f}</s>\n"
        )
    else:
        price_text = f"💰 <b>{money(price)}</b>\n"
        usdt_text = f"💵 <b>{float(usdt_price or 0):.2f} USDT</b>\n"

    caption = (
        f"🟣 <b>{esc(name)}</b>\n\n"
        f"{esc(description)}\n\n"
        f"{price_text}"
        f"{usdt_text}"
        f"{stock_text}\n"
        f"{quantity_text}"
        "\n\n⚡ <i>После оплаты заказ обрабатывается автоматически или вручную — в зависимости от товара.</i>"
    )

    builder = InlineKeyboardBuilder()
    builder.button(
        text="💳 Купить",
        callback_data=f"buy_one:{product_id}"
    )
    builder.button(
        text="🛒 В корзину",
        callback_data=f"add_cart:{product_id}"
    )
    builder.button(
        text="⬅️ Назад к товарам",
        callback_data=f"product_back:{product_id}"
    )
    builder.button(
        text="🏠 Главное меню",
        callback_data="catalog_home"
    )
    builder.adjust(1)

    if photo_id:
        await callback.message.answer_photo(
            photo=photo_id,
            caption=caption,
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )
    else:
        await callback.message.answer(
            caption,
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )

    await callback.answer()


@dp.callback_query(F.data.startswith("product_back:"))
async def product_back(callback: CallbackQuery):
    product_id = int(callback.data.split(":")[1])

    cursor.execute(
        "SELECT category_id FROM products WHERE id = ?",
        (product_id,)
    )
    row = cursor.fetchone()

    if not row:
        await callback.answer(
            "Товар больше недоступен.",
            show_alert=True,
        )
        return

    category_id = row[0]

    cursor.execute(
        "SELECT name FROM categories WHERE id = ?",
        (category_id,)
    )
    category = cursor.fetchone()

    if not category:
        await callback.answer(
            "Категория больше недоступна.",
            show_alert=True,
        )
        return

    await callback.message.answer(
        f"🟣 <b>{esc(category[0])}</b>\n\n"
        "<code>Выбери товар ниже</code>",
        reply_markup=products_keyboard(category_id),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data == "catalog_back")
async def catalog_back(callback: CallbackQuery):
    await send_catalog(callback.message)
    await callback.answer()


@dp.callback_query(F.data == "catalog_home")
async def catalog_home(callback: CallbackQuery):
    if await check_banned_user(callback.message):
        await callback.answer()
        return

    name = esc(callback.from_user.first_name or "друг")
    start_text = get_start_message(name)
    start_photo = get_start_photo()

    if start_photo:
        await callback.message.answer_photo(
            photo=start_photo,
            caption=start_text,
            reply_markup=user_menu(callback.from_user.id),
            parse_mode="HTML",
        )
    else:
        await callback.message.answer(
            start_text,
            reply_markup=user_menu(callback.from_user.id),
            parse_mode="HTML",
        )
    await callback.answer()


# ==================== CART ====================

@dp.callback_query(F.data.startswith("add_cart:"))
async def add_cart(callback: CallbackQuery):
    product_id = int(callback.data.split(":")[1])
    cursor.execute("SELECT name, quantity_enabled, quantity_limit FROM products WHERE id = ?", (product_id,))
    product = cursor.fetchone()
    if not product:
        await callback.answer("Товар недоступен.", show_alert=True)
        return

    name, enabled, limit = product
    cursor.execute("SELECT quantity FROM cart WHERE user_id = ? AND product_id = ?", (callback.from_user.id, product_id))
    row = cursor.fetchone()
    current = int(row[0]) if row else 0
    if enabled and current >= int(limit or 1):
        await callback.answer(f"Максимум {limit} шт. за заказ.", show_alert=True)
        return

    if row:
        cursor.execute("UPDATE cart SET quantity = quantity + 1 WHERE user_id = ? AND product_id = ?", (callback.from_user.id, product_id))
    else:
        cursor.execute("INSERT INTO cart(user_id, product_id, quantity) VALUES (?, ?, 1)", (callback.from_user.id, product_id))
    db.commit()
    hot_discount = get_hot_discount_for_product(callback.from_user.id, product_id)
    if hot_discount:
        await callback.answer(f"🛒 {name} добавлен! 🔥 Скидка {hot_discount}% применится автоматически.")
    else:
        await callback.answer(f"🛒 {name} добавлен!")


@dp.message(F.text == "🛒 Корзина")
async def cart_handler(message: Message):
    register_user(message.from_user)
    if await check_banned_user(message):
        return

    await send_cart(message)


@dp.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery):
    await callback.answer()


@dp.callback_query(F.data.startswith("cart_plus:"))
async def cart_plus(callback: CallbackQuery):
    product_id = int(callback.data.split(":")[1])
    cursor.execute("SELECT quantity_enabled, quantity_limit FROM products WHERE id = ?", (product_id,))
    row_product = cursor.fetchone()
    if not row_product:
        await callback.answer("Товар не найден.", show_alert=True)
        return
    enabled, limit = int(row_product[0] or 0), int(row_product[1] or 1)
    cursor.execute("SELECT quantity FROM cart WHERE user_id = ? AND product_id = ?", (callback.from_user.id, product_id))
    row = cursor.fetchone()
    if not row:
        await callback.answer("Товар не найден в корзине.", show_alert=True)
        return
    if enabled and int(row[0]) >= limit:
        await callback.answer(f"Максимум {limit} шт. за заказ.", show_alert=True)
        return
    cursor.execute("UPDATE cart SET quantity = quantity + 1 WHERE user_id = ? AND product_id = ?", (callback.from_user.id, product_id))
    db.commit()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await send_cart(callback.message)
    await callback.answer()


@dp.callback_query(F.data.startswith("cart_minus:"))
async def cart_minus(callback: CallbackQuery):
    product_id = int(callback.data.split(":")[1])

    cursor.execute("""
        SELECT quantity
        FROM cart
        WHERE user_id = ? AND product_id = ?
    """, (
        callback.from_user.id,
        product_id,
    ))

    row = cursor.fetchone()

    if not row:
        await callback.answer()
        return

    if row[0] <= 1:
        cursor.execute("""
            DELETE FROM cart
            WHERE user_id = ? AND product_id = ?
        """, (
            callback.from_user.id,
            product_id,
        ))
    else:
        cursor.execute("""
            UPDATE cart
            SET quantity = quantity - 1
            WHERE user_id = ? AND product_id = ?
        """, (
            callback.from_user.id,
            product_id,
        ))

    db.commit()

    await callback.message.delete()
    await send_cart(callback.message)
    await callback.answer()


@dp.callback_query(F.data.startswith("cart_delete:"))
async def cart_delete(callback: CallbackQuery):
    product_id = int(callback.data.split(":")[1])

    cursor.execute("""
        DELETE FROM cart
        WHERE user_id = ? AND product_id = ?
    """, (
        callback.from_user.id,
        product_id,
    ))

    db.commit()

    await callback.message.delete()
    await send_cart(callback.message)
    await callback.answer("Удалено.")


# ==================== PAYMENT HELPERS ====================

def get_active_spin_discount(user_id):
    """Возвращает текущую скидку из последнего спина пользователя.

    Скидка действует до следующего прокрута. Если последний приз не является
    скидкой, активной скидки нет.
    """
    cursor.execute(
        "SELECT prize FROM daily_spins WHERE user_id=? ORDER BY spin_date DESC LIMIT 1",
        (user_id,),
    )
    row = cursor.fetchone()
    if not row:
        return 0

    prize = str(row[0] or "")
    match = re.search(r"скидка\s+(\d+)%", prize, re.IGNORECASE)
    if not match:
        return 0

    discount = int(match.group(1))
    return max(0, min(discount, 100))


def get_random_product():
    cursor.execute("""
        SELECT p.id
        FROM products p
        WHERE EXISTS (
            SELECT 1 FROM inventory i
            WHERE i.product_id = p.id AND i.issued = 0
        )
        ORDER BY RANDOM()
        LIMIT 1
    """)
    row = cursor.fetchone()
    return int(row[0]) if row else None


def get_user_hot_offer(user_id, create=True):
    """Персональное горячее предложение пользователя.

    У каждого пользователя:
    - свой товар;
    - своя скидка 1–20%;
    - своё случайное время отправки в течение суток;
    - после отправки предложение действует случайное время 15 минут–24 часа.
    """
    now = datetime.now()
    today = now.date().isoformat()

    cursor.execute("""
        SELECT product_id, discount, scheduled_at, expires_at, sent
        FROM user_hot_offers
        WHERE user_id=? AND offer_date=?
    """, (user_id, today))
    row = cursor.fetchone()

    if not row and create:
        product_id = get_random_product()
        if product_id is None:
            return None

        # Случайный момент в течение текущих суток.
        start_of_day = datetime.combine(now.date(), datetime.min.time())
        seconds_today = random.randint(0, 23 * 60 * 60 + 59)
        scheduled_at = start_of_day + timedelta(seconds=seconds_today)

        discount = random.randint(1, 20)

        cursor.execute("""
            INSERT OR IGNORE INTO user_hot_offers(
                user_id, offer_date, product_id, discount, scheduled_at, expires_at, sent
            ) VALUES (?, ?, ?, ?, ?, NULL, 0)
        """, (
            user_id,
            today,
            product_id,
            discount,
            scheduled_at.isoformat(timespec="seconds"),
        ))
        db.commit()

        cursor.execute("""
            SELECT product_id, discount, scheduled_at, expires_at, sent
            FROM user_hot_offers
            WHERE user_id=? AND offer_date=?
        """, (user_id, today))
        row = cursor.fetchone()

    if not row:
        return None

    product_id, discount, scheduled_raw, expires_raw, sent = row
    scheduled_at = datetime.fromisoformat(scheduled_raw)
    expires_at = datetime.fromisoformat(expires_raw) if expires_raw else None

    # Если предложение уже отправлено и истекло — сегодня новое не выдаём.
    if sent and expires_at and expires_at <= now:
        return None

    return {
        "product_id": int(product_id),
        "discount": max(1, min(20, int(discount))),
        "scheduled_at": scheduled_at,
        "expires_at": expires_at,
        "sent": int(sent),
        "offer_date": today,
    }


def get_hot_discount_for_product(user_id, product_id):
    """Персональная скидка пользователя только для его активного предложения."""
    offer = get_user_hot_offer(user_id, create=True)
    if not offer or not offer["sent"]:
        return 0
    if not offer["expires_at"] or offer["expires_at"] <= datetime.now():
        return 0
    return offer["discount"] if int(product_id) == offer["product_id"] else 0


def format_hot_remaining(expires_at):
    seconds = max(0, int((expires_at - datetime.now()).total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours} ч. {minutes:02d} мин."
    return f"{minutes} мин. {seconds:02d} сек."


async def personal_hot_offer_worker(bot):
    """Раз в ~30 секунд проверяет, кому пора отправить персональное предложение."""
    while True:
        try:
            now = datetime.now()

            cursor.execute("SELECT user_id, first_name FROM users WHERE user_id != ?", (ADMIN_ID,))
            users = cursor.fetchall()

            for user_id, first_name in users:
                offer = get_user_hot_offer(user_id, create=True)
                if not offer or offer["sent"]:
                    continue
                if offer["scheduled_at"] > now:
                    continue

                # Предложение начинает действовать с момента отправки.
                duration_minutes = random.randint(15, 24 * 60)
                expires_at = now + timedelta(minutes=duration_minutes)

                cursor.execute("""
                    UPDATE user_hot_offers
                    SET sent=1, expires_at=?
                    WHERE user_id=? AND offer_date=? AND sent=0
                """, (
                    expires_at.isoformat(timespec="seconds"),
                    user_id,
                    offer["offer_date"],
                ))
                db.commit()

                cursor.execute("""
                    SELECT name, description, price, photo_id, usdt_price
                    FROM products
                    WHERE id=?
                """, (offer["product_id"],))
                product = cursor.fetchone()
                if not product:
                    continue

                name, description, price, photo_id, usdt_price = product
                discount = offer["discount"]
                hot_price = apply_discount(price, discount)
                hot_usdt = apply_discount(usdt_price, discount) if usdt_price else 0

                msg_text = (
                    "🔥 <b>ТЕБЕ ПРИШЁЛ ПЕРСОНАЛЬНЫЙ ГОРЯЧИЙ ТОВАР!</b>\n\n"
                    f"🛍 <b>{esc(name)}</b>\n\n"
                    f"{esc(description or '')}\n\n"
                    f"💰 Было: <s>{money(price)}</s>\n"
                    f"🔥 Твоя цена: <b>{money(hot_price)}</b> (-{discount}%)\n"
                    f"💵 USDT: <s>{float(usdt_price or 0):.2f}</s> → "
                    f"<b>{float(hot_usdt):.2f} USDT</b>\n\n"
                    f"⏳ Предложение действует: <b>{format_hot_remaining(expires_at)}</b>\n"
                    "⚡ Скидка персональная и автоматически применится при покупке."
                )

                kb = InlineKeyboardBuilder()
                kb.button(
                    text="🔥 Забрать скидку",
                    callback_data=f"show_product:{offer['product_id']}"
                )
                kb.button(text="🏠 Главное меню", callback_data="menu_home")
                kb.adjust(1)

                try:
                    if photo_id:
                        await bot.send_photo(
                            user_id,
                            photo_id,
                            caption=msg_text,
                            reply_markup=kb.as_markup(),
                            parse_mode="HTML",
                        )
                    else:
                        await bot.send_message(
                            user_id,
                            msg_text,
                            reply_markup=kb.as_markup(),
                            parse_mode="HTML",
                        )
                except Exception:
                    # Пользователь мог заблокировать бота — просто не ломаем worker.
                    pass

        except Exception as e:
            print(f"⚠️ Ошибка personal_hot_offer_worker: {e}")

        await asyncio.sleep(30)



def apply_discount(price, discount_percent):
    """Применяет скидку к цене."""
    price = float(price)
    discount_percent = int(discount_percent or 0)
    return round(price * (1 - discount_percent / 100), 2)


def is_brawl_product(product_id):
    cursor.execute("""
        SELECT 1
        FROM products p
        JOIN categories c ON c.id = p.category_id
        WHERE p.id = ? AND LOWER(c.name) = LOWER(?)
    """, (product_id, "🎮 Brawl Stars"))
    return cursor.fetchone() is not None


def cart_has_brawl(user_id):
    cursor.execute("""
        SELECT 1
        FROM cart ca
        JOIN products p ON p.id = ca.product_id
        JOIN categories c ON c.id = p.category_id
        WHERE ca.user_id = ? AND LOWER(c.name) = LOWER(?)
        LIMIT 1
    """, (user_id, "🎮 Brawl Stars"))
    return cursor.fetchone() is not None


def valid_brawl_nickname(value):
    value = value.strip()
    # Никнейм из Supercell ID: не пустой, без служебного тега и с разумным лимитом.
    return 2 <= len(value) <= 30 and not value.startswith("#") and not any(ch in value for ch in "\n\r")


async def ask_brawl_nickname(callback, state, product_id=None):
    await state.update_data(payment_product_id=product_id)
    await state.set_state(BrawlPlayerTagState.player_tag)
    await callback.message.answer(
        "🎮 <b>BRAWL STARS</b>\n\n"
        "Перед оплатой укажи <b>никнейм аккаунта</b>, указанный в Supercell ID, куда нужно оформить товар.\n\n"
        "📌 Введи ник так, как он отображается в игре / Supercell ID.\n"
        "Пример: <code>Max Pro</code>\n\n"
        "⚠️ Не отправляй пароль или другие данные Supercell ID — нужен только никнейм.",
        parse_mode="HTML",
    )


def is_steam_product(product_id):
    cursor.execute("""
        SELECT 1 FROM products p
        JOIN categories c ON c.id = p.category_id
        WHERE p.id = ? AND LOWER(c.name) = LOWER(?)
    """, (product_id, "🎮 Steam"))
    return cursor.fetchone() is not None


def is_playstation_product(product_id):
    cursor.execute("""
        SELECT 1 FROM products p
        JOIN categories c ON c.id = p.category_id
        WHERE p.id = ? AND LOWER(c.name) = LOWER(?)
    """, (product_id, PLAYSTATION_CATEGORY_NAME))
    return cursor.fetchone() is not None


def is_spotify_product(product_id):
    cursor.execute("""
        SELECT 1 FROM products p
        JOIN categories c ON c.id = p.category_id
        WHERE p.id = ? AND LOWER(c.name) = LOWER(?)
    """, (product_id, "🎵 Spotify Premium"))
    return cursor.fetchone() is not None


def cart_has_steam(user_id):
    cursor.execute("""
        SELECT 1 FROM cart ca
        JOIN products p ON p.id = ca.product_id
        JOIN categories c ON c.id = p.category_id
        WHERE ca.user_id = ? AND LOWER(c.name) = LOWER(?)
        LIMIT 1
    """, (user_id, "🎮 Steam"))
    return cursor.fetchone() is not None


def cart_has_spotify(user_id):
    cursor.execute("""
        SELECT 1 FROM cart ca
        JOIN products p ON p.id = ca.product_id
        JOIN categories c ON c.id = p.category_id
        WHERE ca.user_id = ? AND LOWER(c.name) = LOWER(?)
        LIMIT 1
    """, (user_id, "🎵 Spotify Premium"))
    return cursor.fetchone() is not None


def valid_spotify_account(value):
    value = value.strip()
    # Для Spotify нужен именно e-mail, на который оформляется подписка.
    # Пароль и другие данные аккаунта не запрашиваем.
    return (
        5 <= len(value) <= 120
        and "@" in value
        and "." in value.rsplit("@", 1)[-1]
        and not any(ch in value for ch in "\n\r")
    )


async def ask_spotify_account(callback, state, product_id=None):
    await state.update_data(payment_product_id=product_id)
    await state.set_state(SpotifyAccountState.account)
    await callback.message.answer(
        "🎵 <b>SPOTIFY PREMIUM</b>\n\n"
        "Введи <b>e-mail Spotify</b>, на который нужно оформить Premium.\n\n"
        "📩 <b>Именно на эту почту будет оформлена подписка.</b> Проверь адрес перед оплатой.\n"
        "🔐 Пароль и другие данные аккаунта <b>не отправляй</b>.\n"
        "📌 Перед оплатой внимательно проверь e-mail и регион аккаунта.",
        parse_mode="HTML",
    )


@dp.message(SpotifyAccountState.account)
async def spotify_account_received(message: Message, state: FSMContext):
    if await check_banned_user(message):
        return
    account = message.text.strip() if message.text else ""
    if not valid_spotify_account(account):
        await message.answer(
            "❌ Некорректный e-mail. Введи почту Spotify ещё раз, например: <code>name@example.com</code>.",
            parse_mode="HTML",
        )
        return
    data = await state.get_data()
    product_id = data.get("payment_product_id")
    await state.update_data(payment_player_tag=f"Spotify e-mail: {account}")
    await state.set_state(None)
    if product_id is not None:
        cursor.execute("SELECT name FROM products WHERE id = ?", (product_id,))
        row = cursor.fetchone()
        product_name = row[0] if row else "Spotify Premium"
        await message.answer(
            f"✅ E-mail сохранён: <code>{esc(account)}</code>\n\n"
            f"🎵 {esc(product_name)}\n\n"
            "💳 Теперь выбери способ оплаты:",
            reply_markup=payment_methods_keyboard(product_id),
            parse_mode="HTML",
        )
    else:
        await message.answer(
            f"✅ E-mail сохранён: <code>{esc(account)}</code>\n\n"
            "💳 Теперь выбери способ оплаты:",
            reply_markup=payment_methods_keyboard(),
            parse_mode="HTML",
        )


def valid_steam_login(value):
    value = value.strip()
    return 2 <= len(value) <= 100 and not any(ch in value for ch in "\n\r")


async def ask_steam_login(callback, state, product_id=None):
    await state.update_data(payment_product_id=product_id)
    await state.set_state(SteamLoginState.login)
    await callback.message.answer(
        "🎮 <b>STEAM ПОПОЛНЕНИЕ</b>\n\n"
        "Введи <b>логин Steam</b> аккаунта, который нужно пополнить.\n\n"
        "🔐 Нужен только логин — <b>пароль и код Steam Guard не отправляй</b>.\n"
        "📌 Перед оплатой внимательно проверь логин.",
        parse_mode="HTML",
    )


def payment_methods_keyboard(product_id=None):
    builder = InlineKeyboardBuilder()
    suffix = f":{product_id}" if product_id is not None else ""
    builder.button(text="₽  Рубли", callback_data=f"paymethod:rub{suffix}")
    builder.button(text="💵  USDT", callback_data=f"paymethod:usdt{suffix}")
    builder.button(text="🏦  СБП", callback_data=f"paymethod:sbp{suffix}")
    builder.button(text="↩️ Назад", callback_data="menu_cart" if product_id is None else f"product_back:{product_id}")
    builder.adjust(1)
    return builder.as_markup()

def format_price(amount, currency):
    if currency == "RUB" or not currency:
        return f"{float(amount):.2f} ₽"
    if currency == "USDT":
        return f"{float(amount):.2f} USDT"
    return f"{float(amount):.2f} ₽"


async def create_manual_order(user_id, items, currency, total, bot):
    cursor.execute("""
        INSERT INTO orders(
            user_id, username, customer_name, total, status, paid, currency, total_stars
        ) VALUES (?, ?, ?, ?, ?, 0, ?, NULL)
    """, (
        user_id,
        None,
        "",
        float(total),
        "🟡 Ожидает оплаты",
        currency,
    ))
    order_id = cursor.lastrowid

    for product_id, name, unit_price, quantity in items:
        cursor.execute("""
            INSERT INTO order_items(order_id, product_id, product_name, price, quantity)
            VALUES (?, ?, ?, ?, ?)
        """, (order_id, product_id, name, float(unit_price), quantity))

    db.commit()

    # Обновляем данные пользователя.
    cursor.execute("""
        UPDATE orders SET username = ?, customer_name = ? WHERE id = ?
    """, (
        None,
        "",
        order_id,
    ))
    db.commit()
    return order_id


def manual_payment_text(order_id, currency, total):
    if currency == "RUB":
        details = RUB_PAYMENT_DETAILS
        return (
            "₽ <b>ОПЛАТА В РУБЛЯХ</b>\n\n"
            f"Заказ: <b>#{order_id}</b>\n"
            f"Сумма: <b>{format_price(total, 'RUB')}</b>\n\n"
            "Переведите указанную сумму по реквизитам:\n"
            f"<code>{esc(details)}</code>\n\n"
            "📸 <b>Следующий шаг:</b> после перевода отправьте сюда <b>скриншот оплаты</b>.\n"
            "⚠️ <b>Без скриншота заявка на проверку не отправится.</b>\n"
            "После получения фото появится кнопка «Я оплатил»."
        )
    return (
        "💵 <b>ОПЛАТА USDT</b>\n\n"
        f"Заказ: <b>#{order_id}</b>\n"
        f"Сумма: <b>{format_price(total, 'USDT')}</b>\n"
        f"Сеть: <b>{esc(USDT_NETWORK)}</b>\n\n"
        "Отправьте USDT по адресу:\n"
        f"<code>{esc(USDT_PAYMENT_ADDRESS)}</code>\n\n"
        "📸 <b>Следующий шаг:</b> после перевода отправьте сюда <b>скриншот оплаты</b>.\n"
        "⚠️ <b>Без скриншота заявка на проверку не отправится.</b>\n"
        "После получения фото появится кнопка «Я оплатил»."
    )


async def _crypto_pay_request(method, payload=None):
    """Запрос к Crypto Pay API без сторонних библиотек."""
    if not CRYPTO_PAY_TOKEN:
        raise RuntimeError("Не задан CRYPTO_PAY_TOKEN")
    payload = payload or {}

    def _request():
        data = urllib.parse.urlencode(payload).encode("utf-8") if payload else None
        req = urllib.request.Request(
            f"{CRYPTO_PAY_API_URL}/{method}", data=data,
            headers={
                "Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": "FelecasterShop/1.0 (Crypto Pay API client)",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw)
                error = data.get("error") or {}
                raise RuntimeError(
                    f"Crypto Pay HTTP {e.code}: {error.get('name') or error.get('code') or raw}"
                ) from e
            except json.JSONDecodeError:
                raise RuntimeError(f"Crypto Pay HTTP {e.code}: {raw[:500]}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Не удалось подключиться к Crypto Pay: {e.reason}") from e
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Crypto Pay вернул некорректный ответ: {raw[:500]}") from e

    result = await asyncio.to_thread(_request)
    if not result.get("ok"):
        raise RuntimeError(str(result.get("error", {})))
    return result.get("result")


async def crypto_pay_diagnostic():
    """Проверяет доступ к Crypto Pay API и валидность токена через getMe."""
    if not CRYPTO_PAY_TOKEN:
        return {"ok": False, "stage": "config", "message": "CRYPTO_PAY_TOKEN не найден в .env"}
    try:
        app = await _crypto_pay_request("getMe")
        return {
            "ok": True,
            "stage": "getMe",
            "message": "Crypto Pay API доступен, токен принят.",
            "app": app or {},
        }
    except Exception as e:
        text = str(e)
        if "HTTP 403" in text and ("1010" in text or "error code: 1010" in text):
            return {
                "ok": False,
                "stage": "access",
                "message": (
                    "Crypto Pay отклонил HTTP-запрос кодом 403 / 1010. "
                    "Это происходит до проверки createInvoice и обычно означает блокировку/ограничение доступа к API для текущего соединения или IP. "
                    "Сам код бота и формат createInvoice здесь не являются причиной."
                ),
                "error": text,
            }
        return {"ok": False, "stage": "auth_or_api", "message": text}


@dp.message(Command("cryptotest"))
async def crypto_test_command(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    result = await crypto_pay_diagnostic()
    if result["ok"]:
        app = result.get("app") or {}
        name = esc(str(app.get("name") or "—"))
        app_id = esc(str(app.get("app_id") or "—"))
        await message.answer(
            "✅ <b>Crypto Pay API работает</b>\n\n"
            f"📱 Приложение: <b>{name}</b>\n"
            f"🆔 App ID: <code>{app_id}</code>\n"
            "🔐 Токен принят методом <code>getMe</code>.\n\n"
            "Теперь можно проверять создание USDT-счёта.",
            parse_mode="HTML",
        )
        return

    stage = esc(str(result.get("stage") or "unknown"))
    msg = esc(str(result.get("message") or "Неизвестная ошибка"))
    err = esc(str(result.get("error") or ""))
    text = (
        "❌ <b>Диагностика Crypto Pay не пройдена</b>\n\n"
        f"🔎 Этап: <code>{stage}</code>\n"
        f"📌 {msg}"
    )
    if err and err != msg:
        text += f"\n\n<code>{err[:1200]}</code>"
    text += (
        "\n\n💡 Если здесь снова <b>403 / 1010</b>, менять параметры заказа, USDT или промокоды бессмысленно: "
        "нужно решить доступ к самому API."
    )
    await message.answer(text, parse_mode="HTML")


async def create_crypto_invoice(order_id, total):
    invoice = await _crypto_pay_request("createInvoice", {
        "asset": "USDT", "amount": f"{float(total):.2f}",
        "description": f"Felecaster Shop • Заказ #{order_id}",
        "payload": str(order_id), "allow_comments": "false",
        "allow_anonymous": "false", "expires_in": "3600",
    })
    invoice_id = int(invoice["invoice_id"])
    invoice_url = invoice.get("bot_invoice_url") or invoice.get("mini_app_invoice_url") or invoice.get("web_app_invoice_url")
    if not invoice_url:
        raise RuntimeError("Crypto Pay не вернул ссылку на оплату")
    return invoice_id, invoice_url


def crypto_payment_keyboard(invoice_url):
    builder = InlineKeyboardBuilder()
    builder.button(text="💳 Оплатить через Crypto Bot", url=invoice_url)
    builder.button(text="🔄 Проверить оплату", callback_data="crypto_check_payment")
    builder.adjust(1)
    return builder.as_markup()


async def check_crypto_invoice(invoice_id):
    invoices = await _crypto_pay_request("getInvoices", {"invoice_ids": str(invoice_id), "count": "1"})
    return invoices[0] if invoices else None


async def mark_crypto_order_paid(bot, order_id, invoice):
    cursor.execute("SELECT user_id, total, currency, paid FROM orders WHERE id=?", (order_id,))
    order = cursor.fetchone()
    if not order or order[2] != "USDT" or int(order[3] or 0) == 1:
        return False
    cursor.execute("UPDATE orders SET paid=1, status='🟠 Оплата получена — ждёт подтверждения' WHERE id=?", (order_id,))
    db.commit()
    user_id, total, _, _ = order
    await bot.send_message(user_id,
        f"✅ <b>Оплата заказа #{order_id} получена!</b>\n\n"
        f"💵 Сумма: <b>{format_price(total, 'USDT')}</b>\n"
        "⏳ Заказ передан на обработку.\n"
        "🎁 После выдачи данные заказа придут сюда.", parse_mode="HTML")
    await bot.send_message(ADMIN_ID,
        "💰 <b>USDT ОПЛАТА ПОЛУЧЕНА</b>\n\n"
        f"🧾 Заказ: <b>#{order_id}</b>\n"
        f"👤 Покупатель: <code>{user_id}</code>\n"
        f"💵 Сумма: <b>{format_price(total, 'USDT')}</b>\n"
        f"🔗 Invoice: <code>{invoice.get('invoice_id')}</code>\n\n"
        "Откройте заказ в админ-панели и выдайте товар.", parse_mode="HTML")
    return True


async def crypto_payment_worker(bot):
    while True:
        try:
            if CRYPTO_PAY_TOKEN:
                cursor.execute("SELECT id, crypto_invoice_id FROM orders WHERE currency='USDT' AND paid=0 AND crypto_invoice_id IS NOT NULL AND status='🟡 Ожидает оплаты' ORDER BY id ASC LIMIT 50")
                for order_id, invoice_id in cursor.fetchall():
                    try:
                        invoice = await check_crypto_invoice(invoice_id)
                        if invoice and invoice.get("status") == "paid":
                            await mark_crypto_order_paid(bot, int(order_id), invoice)
                    except Exception as e:
                        print(f"[CRYPTO PAY CHECK ERROR] order={order_id}: {e}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[CRYPTO PAY WORKER ERROR] {e}")
        await asyncio.sleep(10)


@dp.callback_query(F.data == "crypto_check_payment")
async def crypto_check_payment(callback: CallbackQuery):
    cursor.execute("SELECT id, crypto_invoice_id, paid FROM orders WHERE user_id=? AND currency='USDT' AND crypto_invoice_id IS NOT NULL AND paid=0 ORDER BY id DESC LIMIT 1", (callback.from_user.id,))
    row = cursor.fetchone()
    if not row:
        await callback.answer("Активный USDT-заказ не найден.", show_alert=True)
        return
    order_id, invoice_id, paid = row
    if int(paid or 0) == 1:
        await callback.answer("Оплата уже получена!", show_alert=True)
        return
    try:
        invoice = await check_crypto_invoice(invoice_id)
    except Exception as e:
        print(f"[CRYPTO PAY MANUAL CHECK ERROR] {e}")
        await callback.answer("Не удалось проверить оплату.", show_alert=True)
        return
    if invoice and invoice.get("status") == "paid":
        await mark_crypto_order_paid(callback.bot, order_id, invoice)
        await callback.answer("Оплата подтверждена!", show_alert=True)
    else:
        await callback.answer("Оплата пока не поступила.", show_alert=True)




def sbp_payment_text(order_id, total):
    return (
        "🏦 <b>ОПЛАТА СБП</b>\n\n"
        f"Заказ: <b>#{order_id}</b>\n"
        f"Сумма: <b>{format_price(total, 'RUB')}</b>\n\n"
        "Отсканируйте QR-код ниже в приложении банка и оплатите указанную сумму.\n"
        "После оплаты отправьте сюда <b>скриншот оплаты</b>.\n"
        "⚠️ <b>Без скриншота заявка на проверку не отправится.</b>"
    )


def sbp_payment_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="🔗 Открыть СБП", url=SBP_PAYMENT_URL)
    return builder.as_markup()


def paid_button(order_id):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Я оплатил — отправить на проверку", callback_data=f"paid_manual:{order_id}")
    return builder.as_markup()


def screenshot_instruction(order_id):
    return (
        "📸 <b>Подтверждение оплаты</b>\n\n"
        f"Заказ: <b>#{order_id}</b>\n\n"
        "Отправьте <b>скриншот успешной оплаты</b> одним сообщением.\n"
        "После получения скриншота появится кнопка <b>«Я оплатил»</b>."
    )

# ==================== TELEGRAM STARS ====================

@dp.callback_query(F.data == "stars_buy_start")
async def stars_buy_start(callback: CallbackQuery, state: FSMContext):
    if await check_banned_user(callback.message):
        await callback.answer()
        return

    await state.set_state(StarsPurchaseState.waiting_amount)
    await callback.message.answer(
        "⭐ <b>ПОКУПКА TELEGRAM STARS</b>\n\n"
        "Введите количество Stars, которое хотите купить.\n\n"
        "💰 Цена: <b>1 ⭐ = 1.50 ₽</b>\n"
        "Например: <code>100</code> → <b>150.00 ₽</b>",
        parse_mode="HTML",
    )
    await callback.answer()


@dp.message(StarsPurchaseState.waiting_amount)
async def stars_amount_received(message: Message, state: FSMContext):
    if await check_banned_user(message):
        return

    raw = (message.text or "").strip().replace(" ", "").replace("_", "")
    try:
        amount = int(raw)
    except (TypeError, ValueError):
        await message.answer("❌ Введите целое число Stars, например <code>500</code>.", parse_mode="HTML")
        return

    if amount < 1:
        await message.answer("❌ Минимальное количество — <b>1 Star</b>.", parse_mode="HTML")
        return
    if amount > 1_000_000:
        await message.answer("❌ Максимальное количество за один заказ — <b>1 000 000 Stars</b>.", parse_mode="HTML")
        return

    total = amount * STARS_RUB_PRICE
    await state.update_data(stars_amount=amount, stars_total=total)

    builder = InlineKeyboardBuilder()
    builder.button(text="💳 Купить", callback_data="stars_buy_confirm")
    builder.button(text="✏️ Изменить количество", callback_data="stars_buy_start")
    builder.button(text="❌ Отмена", callback_data="stars_buy_cancel")
    builder.adjust(1)

    await message.answer(
        "⭐ <b>ВАШ ЗАКАЗ</b>\n\n"
        f"Количество: <b>{amount:,} Stars</b>\n"
        f"Цена: <b>{total:.2f} ₽</b>\n\n"
        "Проверьте данные и нажмите «💳 Купить».",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "stars_buy_cancel")
async def stars_buy_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer(
        "❌ <b>Покупка Stars отменена.</b>",
        reply_markup=user_menu(callback.from_user.id),
        parse_mode="HTML",
    )
    await callback.answer()


async def create_stars_order(user_id, stars_amount, total):
    cursor.execute("""
        INSERT INTO orders(
            user_id, username, customer_name, total, status, paid, currency, total_stars
        ) VALUES (?, ?, ?, ?, ?, 0, 'RUB', ?)
    """, (
        user_id,
        None,
        "",
        float(total),
        "🟡 Ожидает оплаты",
        int(stars_amount),
    ))
    order_id = cursor.lastrowid

    cursor.execute("""
        INSERT INTO order_items(order_id, product_id, product_name, price, quantity)
        VALUES (?, NULL, ?, ?, ?)
    """, (
        order_id,
        "⭐ Telegram Stars",
        float(STARS_RUB_PRICE),
        int(stars_amount),
    ))

    db.commit()
    return order_id


@dp.callback_query(F.data == "stars_buy_confirm")
async def stars_buy_confirm(callback: CallbackQuery, state: FSMContext):
    if await check_banned_user(callback.message):
        await callback.answer()
        return

    data = await state.get_data()
    stars_amount = int(data.get("stars_amount", 0))
    total = float(data.get("stars_total", 0))

    if stars_amount < 1 or total <= 0:
        await state.clear()
        await callback.answer("Сессия покупки истекла. Начните заново.", show_alert=True)
        return

    order_id = await create_stars_order(callback.from_user.id, stars_amount, total)
    cursor.execute(
        "UPDATE orders SET username=?, customer_name=? WHERE id=?",
        (callback.from_user.username, callback.from_user.first_name or "", order_id),
    )
    db.commit()

    await callback.message.answer(
        manual_payment_text(order_id, "RUB", total).replace(
            "₽ <b>ОПЛАТА В РУБЛЯХ</b>",
            "⭐ <b>ОПЛАТА TELEGRAM STARS</b>"
        ).replace(
            "Переведите указанную сумму по реквизитам:",
            f"Количество Stars: <b>{stars_amount:,}</b>\n\nПереведите указанную сумму по реквизитам:"
        ),
        parse_mode="HTML",
    )
    await callback.message.answer(
        screenshot_instruction(order_id),
        parse_mode="HTML",
    )

    await state.set_state(PaymentProofState.waiting_screenshot)
    await state.update_data(payment_order_id=order_id)

    await callback.bot.send_message(
        ADMIN_ID,
        "🧾 <b>НОВЫЙ ЗАКАЗ TELEGRAM STARS</b>\n\n"
        f"Номер: <b>#{order_id}</b>\n"
        f"Покупатель: <code>{callback.from_user.id}</code>\n"
        f"⭐ Stars: <b>{stars_amount:,}</b>\n"
        f"💰 Сумма: <b>{total:.2f} ₽</b>\n\n"
        "Статус: ожидается оплата.",
        parse_mode="HTML",
    )
    await callback.answer("Заказ создан.")


@dp.message(F.text == "🎟 Промокод")
async def promo_menu_message(message: Message, state: FSMContext):
    # Надёжный вход из главного меню. Промокод хранится отдельно от корзины
    # и затем применяется ко всей сумме заказа.
    await state.set_state(PromoCodeState.code)
    current = get_user_promo(message.from_user.id)
    if current[0]:
        text = (f"🎟 <b>ПРОМОКОД УЖЕ ПРИМЕНЁН</b>\n\n"
                f"Код: <b>{esc(current[0])}</b>\n"
                f"Скидка: <b>{current[1]}%</b>\n\n"
                "Отправь новый код, чтобы заменить текущий:")
    else:
        text = "🎟 <b>ПРОМОКОД</b>\n\nОтправь промокод следующим сообщением:"
    await message.answer(text, parse_mode="HTML")

@dp.callback_query(F.data == "menu_promo")
async def menu_promo_callback(callback: CallbackQuery, state: FSMContext):
    await state.set_state(PromoCodeState.code)
    current = get_user_promo(callback.from_user.id)
    if current[0]:
        text = (f"🎟 <b>ПРОМОКОД УЖЕ ПРИМЕНЁН</b>\n\n"
                f"Код: <b>{esc(current[0])}</b>\n"
                f"Скидка: <b>{current[1]}%</b>\n\n"
                "Отправь новый код, чтобы заменить текущий:")
    else:
        text = "🎟 <b>ПРОМОКОД</b>\n\nОтправь промокод следующим сообщением:"
    await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()


@dp.message(PromoCodeState.code)
async def promo_code_received(message: Message, state: FSMContext):
    code = (message.text or "").strip().upper()
    if not code:
        await message.answer("❌ Введи промокод текстом.")
        return
    cursor.execute("SELECT discount, max_uses, uses FROM promo_codes WHERE code=? AND active=1", (code,))
    row = cursor.fetchone()
    if not row:
        await state.clear()
        await message.answer("❌ Такого промокода нет или он отключён.", reply_markup=user_menu(message.from_user.id))
        return
    discount, max_uses, uses = int(row[0]), int(row[1] or 0), int(row[2] or 0)
    if not 1 <= discount <= 100:
        await state.clear(); await message.answer("❌ Промокод настроен некорректно.", reply_markup=user_menu(message.from_user.id)); return
    if max_uses > 0 and uses >= max_uses:
        await state.clear(); await message.answer("❌ Лимит использований промокода исчерпан.", reply_markup=user_menu(message.from_user.id)); return
    # Сохраняем применённый код пользователя. После этого он будет
    # автоматически применён ко всей корзине, а не к одному товару.
    cursor.execute(
        "INSERT INTO user_promos(user_id, code, discount) VALUES(?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET code=excluded.code, discount=excluded.discount",
        (message.from_user.id, code, discount),
    )
    db.commit()
    await state.clear()
    await message.answer(f"✅ Промокод <b>{esc(code)}</b> применён. Скидка: <b>{discount}%</b>.", parse_mode="HTML", reply_markup=user_menu(message.from_user.id))
    await send_cart(message)


# ==================== PAYMENTS ====================

@dp.callback_query(F.data.startswith("buy_one:"))
async def buy_one(callback: CallbackQuery, state: FSMContext):
    product_id = int(callback.data.split(":")[1])
    cursor.execute("SELECT name FROM products WHERE id = ?", (product_id,))
    product = cursor.fetchone()
    if not product:
        await callback.answer("Товар не найден.", show_alert=True)
        return
    if is_brawl_product(product_id):
        await ask_brawl_nickname(callback, state, product_id)
    elif is_steam_product(product_id):
        await ask_steam_login(callback, state, product_id)
    elif is_spotify_product(product_id):
        await ask_spotify_account(callback, state, product_id)
    else:
        await callback.message.answer(
            f"💳 <b>Выберите способ оплаты</b>\n\n🛍 {esc(product[0])}",
            reply_markup=payment_methods_keyboard(product_id),
            parse_mode="HTML",
        )
    await callback.answer()


@dp.callback_query(F.data == "promo_enter")
async def promo_enter(callback: CallbackQuery, state: FSMContext):
    await state.set_state(PromoCodeState.code)
    await callback.message.answer("🎟 <b>ВВОД ПРОМОКОДА</b>\n\nОтправь промокод следующим сообщением:", parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "promo_remove")
async def promo_remove(callback: CallbackQuery):
    cursor.execute("DELETE FROM user_promos WHERE user_id=?", (callback.from_user.id,))
    db.commit()
    await callback.answer("Промокод убран")
    try:
        await callback.message.delete()
    except Exception:
        pass
    await send_cart(callback.message)


@dp.callback_query(F.data == "checkout")
async def checkout(callback: CallbackQuery, state: FSMContext):
    items = get_cart(callback.from_user.id)
    if not items:
        await callback.answer("Корзина пуста.", show_alert=True)
        return
    if cart_has_brawl(callback.from_user.id):
        await ask_brawl_nickname(callback, state, None)
    elif cart_has_steam(callback.from_user.id):
        await ask_steam_login(callback, state, None)
    elif cart_has_spotify(callback.from_user.id):
        await ask_spotify_account(callback, state, None)
    else:
        await callback.message.answer(
            "💳 <b>ОПЛАТА</b>\n\n"
            "Выберите удобный способ оплаты.\n"
            "После перевода платеж будет проверен администратором.",
            reply_markup=payment_methods_keyboard(),
            parse_mode="HTML",
        )
    await callback.answer()


@dp.message(BrawlPlayerTagState.player_tag)
async def brawl_player_tag_received(message: Message, state: FSMContext):
    if await check_banned_user(message):
        return
    nickname = message.text.strip() if message.text else ""
    if not valid_brawl_nickname(nickname):
        await message.answer(
            "❌ Неверный никнейм. Введи ник из Brawl Stars / Supercell ID (от 2 до 30 символов).",
            parse_mode="HTML",
        )
        return

    data = await state.get_data()
    product_id = data.get("payment_product_id")
    await state.update_data(payment_player_tag=nickname)
    await state.set_state(None)

    if product_id is not None:
        cursor.execute("SELECT name FROM products WHERE id = ?", (product_id,))
        row = cursor.fetchone()
        product_name = row[0] if row else "Brawl Stars"
        await message.answer(
            f"✅ Никнейм сохранён: <code>{esc(nickname)}</code>\n\n"
            f"🛍 {esc(product_name)}\n\n"
            "💳 Теперь выбери способ оплаты:",
            reply_markup=payment_methods_keyboard(product_id),
            parse_mode="HTML",
        )
    else:
        await message.answer(
            f"✅ Никнейм сохранён: <code>{esc(nickname)}</code>\n\n"
            "💳 Теперь выбери способ оплаты:",
            reply_markup=payment_methods_keyboard(),
            parse_mode="HTML",
        )


@dp.callback_query(F.data == "steam_topup_start")
async def steam_topup_start(callback: CallbackQuery, state: FSMContext):
    if await check_banned_user(callback.message):
        await callback.answer()
        return
    await state.clear()
    await state.set_state(SteamAmountState.amount)
    await callback.message.answer(
        "╭─ 🎮 <b>STEAM WALLET MODULE</b> ─╮\n\n"
        "<code>INPUT://AMOUNT</code>\n\n"
        "Введи сумму, которую хочешь зачислить на Steam Wallet.\n\n"
        "💰 Минимум: <b>50 ₽</b>\n"
        "💰 Максимум: <b>50 000 ₽</b>\n"
        "🔥 Комиссия магазина: <b>5%</b>\n\n"
        "Например: <code>500</code> или <code>1250.50</code>",
        parse_mode="HTML",
    )
    await callback.answer()

@dp.message(SteamAmountState.amount)
async def steam_amount_received(message: Message, state: FSMContext):
    if await check_banned_user(message):
        return
    raw = (message.text or "").strip().replace(" ", "").replace(",", ".")
    try:
        amount = float(raw)
    except ValueError:
        await message.answer("❌ Введи корректную сумму, например <code>500</code>.", parse_mode="HTML")
        return
    if amount < 50:
        await message.answer("❌ Минимальная сумма пополнения — <b>50 ₽</b>.", parse_mode="HTML")
        return
    if amount > 50000:
        await message.answer("❌ Максимальная сумма пополнения — <b>50 000 ₽</b>.", parse_mode="HTML")
        return

    amount = round(amount, 2)
    total = round(amount * 1.05, 2)
    await state.update_data(steam_amount=amount, steam_total=total)
    builder = InlineKeyboardBuilder()
    builder.button(text="💳 Продолжить", callback_data="steam_amount_confirm")
    builder.button(text="✏️ Изменить сумму", callback_data="steam_topup_start")
    builder.button(text="❌ Отмена", callback_data="steam_cancel")
    builder.adjust(1)
    await message.answer(
        "🎮 <b>ПОПОЛНЕНИЕ STEAM</b>\n\n"
        f"💰 На Steam: <b>{amount:.2f} ₽</b>\n"
        f"💳 К оплате: <b>{total:.2f} ₽</b>\n"
        f"📈 Комиссия: <b>{total-amount:.2f} ₽</b>\n\n"
        "Проверь сумму и продолжи.",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )

@dp.callback_query(F.data == "steam_amount_confirm")
async def steam_amount_confirm(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    amount = float(data.get("steam_amount", 0))
    total = float(data.get("steam_total", 0))
    if amount < 50 or total <= 0:
        await state.clear()
        await callback.answer("Сессия истекла. Начни заново.", show_alert=True)
        return
    await state.set_state(SteamLoginState.login)
    await callback.message.answer(
        "🎮 <b>ЛОГИН STEAM</b>\n\n"
        "Введи логин аккаунта, который нужно пополнить.\n\n"
        "🔐 Нужен только логин. <b>Пароль и код Steam Guard не отправляй.</b>",
        parse_mode="HTML",
    )
    await callback.answer()

@dp.callback_query(F.data == "steam_cancel")
async def steam_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer(
        "❌ Пополнение Steam отменено.",
        reply_markup=user_menu(callback.from_user.id),
        parse_mode="HTML",
    )
    await callback.answer()

@dp.message(SteamLoginState.login)
async def steam_login_received(message: Message, state: FSMContext):
    if await check_banned_user(message):
        return
    login = message.text.strip() if message.text else ""
    if not valid_steam_login(login):
        await message.answer(
            "❌ Неверный логин. Введи логин Steam ещё раз (от 2 до 100 символов).",
            parse_mode="HTML",
        )
        return

    data = await state.get_data()
    product_id = data.get("payment_product_id")
    steam_amount = float(data.get("steam_amount", 0))
    steam_total = float(data.get("steam_total", 0))
    await state.update_data(payment_player_tag=f"Steam login: {login}")
    await state.set_state(None)

    if product_id is None and steam_amount >= 50 and steam_total > 0:
        order_id = await create_manual_order(
            message.from_user.id,
            [(None, f"🎮 Steam Wallet {steam_amount:.2f} ₽", steam_total, 1)],
            "RUB",
            steam_total,
            message.bot,
        )
        cursor.execute(
            "UPDATE orders SET username=?, customer_name=? WHERE id=?",
            (message.from_user.username, message.from_user.first_name or "", order_id),
        )
        db.commit()
        await message.answer(
            "🎮 <b>STEAM ЗАКАЗ СОЗДАН</b>\n\n"
            f"💰 Пополнение: <b>{steam_amount:.2f} ₽</b>\n"
            f"💳 К оплате: <b>{steam_total:.2f} ₽</b>\n"
            f"👤 Логин: <code>{esc(login)}</code>\n\n"
            + manual_payment_text(order_id, "RUB", steam_total)
            + "\n\n"
            + screenshot_instruction(order_id),
            parse_mode="HTML",
        )
        await state.set_state(PaymentProofState.waiting_screenshot)
        await state.update_data(payment_order_id=order_id)
        await message.bot.send_message(
            ADMIN_ID,
            "🧾 <b>НОВЫЙ ЗАКАЗ STEAM</b>\n\n"
            f"Номер: <b>#{order_id}</b>\n"
            f"Покупатель: <code>{message.from_user.id}</code>\n"
            f"💰 Пополнение: <b>{steam_amount:.2f} ₽</b>\n"
            f"💳 К оплате: <b>{steam_total:.2f} ₽</b>\n"
            f"👤 Логин: <code>{esc(login)}</code>\n\n"
            "Статус: ожидается оплата.",
            parse_mode="HTML",
        )
        return

    if product_id is not None:
        cursor.execute("SELECT name FROM products WHERE id = ?", (product_id,))
        row = cursor.fetchone()
        product_name = row[0] if row else "Steam"
        await message.answer(
            f"✅ Логин сохранён: <code>{esc(login)}</code>\n\n"
            f"🛍 {esc(product_name)}\n\n"
            "💳 Теперь выбери способ оплаты:",
            reply_markup=payment_methods_keyboard(product_id),
            parse_mode="HTML",
        )
    else:
        await message.answer(
            f"✅ Логин сохранён: <code>{esc(login)}</code>\n\n"
            "💳 Теперь выбери способ оплаты:",
            reply_markup=payment_methods_keyboard(),
            parse_mode="HTML",
        )


def get_items_for_payment(user_id, product_id=None):
    if product_id is not None:
        cursor.execute("SELECT id, name, price, stars_price, usdt_price FROM products WHERE id = ?", (product_id,))
        row = cursor.fetchone()
        if not row:
            return []
        return [(row[0], row[1], row[2], 1, row[3], row[4])]
    return [(pid, name, price, qty, None, None) for pid, name, price, qty in get_cart(user_id)]


@dp.callback_query(F.data.startswith("paymethod:"))
async def choose_payment_method(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    method = parts[1]
    product_id = int(parts[2]) if len(parts) > 2 else None
    payment_data = await state.get_data()
    player_tag = payment_data.get("payment_player_tag")

    if method not in ("rub", "usdt", "sbp"):
        await callback.answer("Этот способ оплаты недоступен.", show_alert=True)
        return

    if product_id is not None:
        items_raw = get_items_for_payment(callback.from_user.id, product_id)
        if not items_raw:
            await callback.answer("Товар больше недоступен.", show_alert=True)
            return
        pid, name, rub, qty, _, usdt = items_raw[0]
        items = [(pid, name, float(rub), qty)]
    else:
        items_cart = get_cart(callback.from_user.id)
        if not items_cart:
            await callback.answer("Корзина пуста.", show_alert=True)
            return
        items = []
        for pid, name, rub, qty in items_cart:
            cursor.execute("SELECT usdt_price, quantity_enabled, quantity_limit FROM products WHERE id = ?", (pid,))
            row = cursor.fetchone()
            if not row:
                await callback.answer("Один из товаров больше недоступен.", show_alert=True)
                return
            usdt, enabled, limit = row
            if enabled and qty > int(limit or 1):
                await callback.answer(f"Для «{name}» максимум {limit} шт.", show_alert=True)
                return
            items.append((pid, name, float(rub), qty))

    spin_discount = get_active_spin_discount(callback.from_user.id)
    personal_hot = get_user_hot_offer(callback.from_user.id, create=True)
    hot_product_id = personal_hot["product_id"] if personal_hot and personal_hot["sent"] else None
    hot_discount = personal_hot["discount"] if personal_hot and personal_hot["sent"] else 0
    if personal_hot and personal_hot["expires_at"] and personal_hot["expires_at"] <= datetime.now():
        hot_product_id = None
        hot_discount = 0
    total = 0.0
    original_total = 0.0
    priced_items = []
    applied_hot = 0
    applied_spin = 0

    for pid, name, rub, qty in items:
        cursor.execute("SELECT usdt_price FROM products WHERE id = ?", (pid,))
        row = cursor.fetchone()
        if not row:
            continue
        usdt = row[0]
        if method in ("rub", "sbp"):
            unit = float(rub)
            if unit <= 0:
                await callback.answer(f"У «{name}» не задана цена в рублях.", show_alert=True)
                return
        else:
            unit = float(usdt or 0)
            if unit <= 0:
                await callback.answer(f"У «{name}» не задана цена USDT.", show_alert=True)
                return

        original_total += unit * qty

        # Для горячего товара действует его собственная случайная скидка.
        # Для остальных товаров сохраняется прежняя скидка от крутилки.
        if hot_product_id is not None and int(pid) == int(hot_product_id):
            item_discount = hot_discount
            applied_hot = hot_discount
        else:
            item_discount = spin_discount
            if spin_discount:
                applied_spin = spin_discount

        discounted_unit = apply_discount(unit, item_discount)
        priced_items.append((pid, name, discounted_unit, qty))
        total += discounted_unit * qty

    if not priced_items:
        await callback.answer("Нет товаров для оплаты.", show_alert=True)
        return

    promo_code, promo_discount = get_user_promo(callback.from_user.id)
    if promo_discount:
        total = round(total * (1 - promo_discount / 100), 2)

    currency = "USDT" if method == "usdt" else "RUB"
    order_id = await create_manual_order(callback.from_user.id, priced_items, currency, total, callback.bot)
    cursor.execute(
        "UPDATE orders SET username=?, customer_name=?, game_player_tag=? WHERE id=?",
        (callback.from_user.username, callback.from_user.first_name or "", player_tag, order_id),
    )
    db.commit()
    if promo_discount and promo_code:
        cursor.execute("UPDATE promo_codes SET uses=uses+1 WHERE code=?", (promo_code,))
        cursor.execute("DELETE FROM user_promos WHERE user_id=?", (callback.from_user.id,))
        db.commit()
    await state.clear()

    payment_text = sbp_payment_text(order_id, total) if method == "sbp" else manual_payment_text(order_id, currency, total)
    if promo_discount and promo_code:
        payment_text += f"\n\n🎟 <b>Промокод {esc(promo_code)}: −{promo_discount}%</b>"
    if applied_hot:
        payment_text += (f"\n\n🔥 <b>Горячий товар: скидка {applied_hot}% применена автоматически!</b>"
                         f"\nБыло: <s>{format_price(original_total, currency)}</s>"
                         f"\nСтало: <b>{format_price(total, currency)}</b>")
    elif applied_spin:
        payment_text += (f"\n\n🎉 <b>Скидка {applied_spin}% применена ко всему ассортименту.</b>"
                         f"\nБыло: <s>{format_price(original_total, currency)}</s>"
                         f"\nСтало: <b>{format_price(total, currency)}</b>"
                         "\n⏰ Скидка действует до следующего прокрута.")

    if method == "usdt":
        if not CRYPTO_PAY_TOKEN:
            cursor.execute("UPDATE orders SET status=? WHERE id=?", ("🔴 Crypto Bot не настроен", order_id))
            db.commit()
            await callback.message.answer("⚠️ <b>Оплата USDT временно недоступна.</b>\n\nДобавьте <code>CRYPTO_PAY_TOKEN</code> в <code>.env</code>.", parse_mode="HTML")
            await callback.answer()
            return
        try:
            invoice_id, invoice_url = await create_crypto_invoice(order_id, total)
            cursor.execute("UPDATE orders SET crypto_invoice_id=?, status=? WHERE id=?", (str(invoice_id), "🟡 Ожидает оплаты", order_id))
            db.commit()
        except Exception as e:
            cursor.execute("UPDATE orders SET status=? WHERE id=?", ("🔴 Ошибка Crypto Bot", order_id))
            db.commit()
            print(f"[CRYPTO PAY CREATE ERROR] order={order_id}: {e}")
            await callback.message.answer(
                "❌ <b>Не удалось создать счёт Crypto Bot.</b>\n\n"
                f"<code>{esc(str(e))[:500]}</code>\n\n"
                "Проверьте CRYPTO_PAY_TOKEN в .env и перезапустите бота.",
                parse_mode="HTML"
            )
            await callback.answer()
            return
        crypto_text = ("💵 <b>ОПЛАТА USDT ЧЕРЕЗ CRYPTO BOT</b>\n\n"
                       f"🧾 Заказ: <b>#{order_id}</b>\n"
                       f"💰 Сумма: <b>{format_price(total, 'USDT')}</b>\n\n"
                       "👇 Нажми кнопку ниже и оплати счёт в Crypto Bot.\n"
                       "✅ После оплаты бот автоматически проверит платёж.\n"
                       "❌ Скриншот отправлять не нужно.")
        if promo_discount and promo_code:
            crypto_text += f"\n\n🎟 <b>Промокод {esc(promo_code)}: −{promo_discount}%</b>"
        if applied_hot:
            crypto_text += f"\n🔥 <b>Горячая скидка: −{applied_hot}%</b>"
        elif applied_spin:
            crypto_text += f"\n🎉 <b>Скидка от крутилки: −{applied_spin}%</b>"
        await callback.message.answer(crypto_text, reply_markup=crypto_payment_keyboard(invoice_url), parse_mode="HTML")
        await callback.bot.send_message(ADMIN_ID,
            "🧾 <b>НОВЫЙ USDT ЗАКАЗ</b>\n\n"
            f"Номер: <b>#{order_id}</b>\n"
            f"Покупатель: <code>{callback.from_user.id}</code>\n"
            f"Сумма: <b>{format_price(total, 'USDT')}</b>\n"
            f"Crypto Invoice: <code>{invoice_id}</code>\n\nСтатус: ожидается оплата.", parse_mode="HTML")
        await state.clear()
        await callback.answer()
        return

    # RUB и СБП остаются на прежней ручной схеме.
    if method == "sbp" and Path(SBP_QR_PATH).exists():
        await callback.message.answer_photo(photo=FSInputFile(SBP_QR_PATH), caption=payment_text, reply_markup=sbp_payment_keyboard(), parse_mode="HTML")
    else:
        await callback.message.answer(payment_text, reply_markup=sbp_payment_keyboard() if method == "sbp" else None, parse_mode="HTML")
    await callback.message.answer(screenshot_instruction(order_id), parse_mode="HTML")
    await state.set_state(PaymentProofState.waiting_screenshot)
    await state.update_data(payment_order_id=order_id)
    await callback.bot.send_message(ADMIN_ID,
        "🧾 <b>НОВЫЙ ЗАКАЗ</b>\n\n"
        f"Номер: <b>#{order_id}</b>\n"
        f"Покупатель: <code>{callback.from_user.id}</code>\n"
        f"Способ: <b>{'СБП' if method == 'sbp' else currency}</b>\n"
        f"Сумма: <b>{format_price(total, currency)}</b>\n"
        + (f"🎮 Ник Brawl Stars: <code>{esc(player_tag)}</code>\n" if player_tag and player_tag.startswith("Brawl") else "")
        + (f"🎵 Spotify e-mail: <code>{esc(player_tag)}</code>\n" if player_tag and player_tag.startswith("Spotify") else "")
        + "\nСтатус: ожидается оплата.", parse_mode="HTML")
    await callback.answer()


def get_items_for_payment(user_id, product_id=None):
    if product_id is not None:
        cursor.execute("SELECT id, name, price, stars_price, usdt_price FROM products WHERE id = ?", (product_id,))
        row = cursor.fetchone()
        if not row:
            return []
        return [(row[0], row[1], row[2], 1, row[3], row[4])]
    return [(pid, name, price, qty, None, None) for pid, name, price, qty in get_cart(user_id)]


# ==================== ЗАКАЗЫ / PROFILE ====================

@dp.message(F.text == "📦 Мои заказы")
async def my_orders(message: Message):
    register_user(message.from_user)
    if await check_banned_user(message):
        return

    cursor.execute("""
        SELECT id, total, currency, total_stars, status, created_at
        FROM orders
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 20
    """, (message.from_user.id,))

    orders = cursor.fetchall()

    if not orders:
        await message.answer(
            "📦 Заказов пока нет.",
        )
        return

    text = "📦 <b>ЗАКАЗЫ</b>\n\n"

    for order_id, total, currency, stars, status, created_at in orders:
        amount = format_price(total, currency or "RUB")
        text += (
            f"🧾 <b>#{order_id}</b> — {esc(status)}\n"
            f"💰 {amount}\n"
            f"📅 {created_at}\n\n"
        )

    await message.answer(
        text,
        parse_mode="HTML",
    )


def profile_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="📦 Мои заказы", callback_data="menu_orders")
    builder.button(text="🛍 Каталог", callback_data="menu_catalog")
    builder.button(text="💬 Поддержка", callback_data="menu_support")
    builder.button(text="🏠 Главное меню", callback_data="menu_home")
    builder.adjust(2, 2)
    return builder.as_markup()


@dp.message(F.text == "👤 Профиль")
async def profile(message: Message):
    register_user(message.from_user)
    if await check_banned_user(message):
        return

    username = (
        f"@{message.from_user.username}"
        if message.from_user.username
        else "Не указан"
    )

    cursor.execute(
        "SELECT COUNT(*), COALESCE(SUM(CASE WHEN paid = 1 THEN 1 ELSE 0 END), 0), "
        "COALESCE(SUM(CASE WHEN paid = 1 AND currency = 'RUB' THEN total ELSE 0 END), 0) "
        "FROM orders WHERE user_id = ?",
        (message.from_user.id,),
    )
    count, paid_count, rub_spent = cursor.fetchone()

    display_name = esc(message.from_user.first_name or "Покупатель")
    username_text = esc(username)

    profile_text = (
        "🟣 <b>МОЙ ПРОФИЛЬ</b>\n\n"
        f"👋 <b>{display_name}</b>\n"
        f"🔗 {username_text}\n\n"
        "📊 <b>СТАТИСТИКА</b>\n"
        f"📦 Заказов: <b>{count}</b>\n"
        f"✅ Оплачено: <b>{paid_count}</b>\n"
        f"💰 Потрачено в RUB: <b>{float(rub_spent):.2f} ₽</b>\n\n"
        "🛡 <b>Premium-статус</b>\n"
        "⚡ Быстрая обработка\n"
        "💬 Персональная поддержка\n\n"
        "<i>Спасибо, что выбираете Felecaster Shop.</i>"
    )

    await message.answer(
        profile_text,
        parse_mode="HTML",
        reply_markup=profile_keyboard(),
    )


# ==================== ADMIN ====================

@dp.message(F.text == "⚙️ Админ-панель")
async def admin_panel(message: Message):
    if await check_banned_user(message):
        return

    if message.from_user.id != ADMIN_ID:
        return

    cursor.execute("SELECT COUNT(*) FROM products")
    products = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM categories")
    categories = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM orders")
    orders = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COUNT(*)
        FROM inventory
        WHERE issued = 0
    """)
    stock = cursor.fetchone()[0]

    await message.answer(
        "⚙️ <b>FELECASTER CONTROL</b>\n\n"
        f"📁 Категорий: <b>{categories}</b>\n"
        f"🛍 Товаров: <b>{products}</b>\n"
        f"📦 Заказов: <b>{orders}</b>\n"
        f"🎁 Ключей в наличии: <b>{stock}</b>\n\n"
        "Выберите действие 👇",
        reply_markup=admin_menu(),
        parse_mode="HTML",
    )



# =========================================================
# USERS / ПОДДЕРЖКА ADMIN
# =========================================================

@dp.message(F.text == "👥 Пользователи")
async def users_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        "👥 <b>ПОЛЬЗОВАТЕЛИ</b>\n\nВыберите пользователя:",
        reply_markup=users_keyboard(),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "admin_back")
async def admin_back(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("⚙️ <b>АДМИН-ПАНЕЛЬ</b>", reply_markup=admin_menu(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "users_list")
async def users_list(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    await callback.message.answer("👥 <b>ПОЛЬЗОВАТЕЛИ</b>\n\nВыберите пользователя:", reply_markup=users_keyboard(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data.startswith("user_manage:"))
async def user_manage(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    user_id = int(callback.data.split(":")[1])
    cursor.execute("SELECT username, first_name, last_name FROM users WHERE user_id=?", (user_id,))
    user = cursor.fetchone()
    if not user:
        await callback.answer("Пользователь не найден.", show_alert=True)
        return
    cursor.execute("SELECT reason FROM banned_users WHERE user_id=?", (user_id,))
    ban = cursor.fetchone()
    name = esc(" ".join(x for x in [user[1], user[2]] if x) or "Без имени")
    username = esc(f"@{user[0]}" if user[0] else "нет")
    text = (f"👤 <b>ПОЛЬЗОВАТЕЛЬ</b>\n\nИмя: <b>{name}</b>\n"
            f"Username: {username}\nID: <code>{user_id}</code>\n"
            f"Статус: <b>{'🚫 Заблокирован' if ban else '🟢 Активен'}</b>")
    if ban:
        text += f"\nПричина: {esc(ban[0] or 'Причина не указана')}"
    await callback.message.answer(text, reply_markup=user_manage_keyboard(user_id, bool(ban)), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data.startswith("ban_selected:"))
async def ban_selected(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    user_id = int(callback.data.split(":")[1])
    if user_id == ADMIN_ID:
        await callback.answer("Себя заблокировать нельзя.", show_alert=True)
        return
    await state.update_data(user_id=user_id)
    await state.set_state(BanUser.reason)
    await callback.message.answer(f"🚫 Пользователь <code>{user_id}</code>\n\nВведите причину блокировки или отправьте <code>-</code>.", parse_mode="HTML")
    await callback.answer()


@dp.message(BanUser.reason)
async def ban_user_reason(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    user_id = int(data["user_id"])
    reason = (message.text or "").strip() or "Причина не указана"
    if reason == "-":
        reason = "Причина не указана"
    cursor.execute("INSERT OR REPLACE INTO banned_users(user_id, reason) VALUES (?, ?)", (user_id, reason))
    db.commit()
    await state.clear()
    try:
        await message.bot.send_message(user_id, f"🚫 <b>Доступ заблокирован</b>\n\nПричина: {esc(reason)}", parse_mode="HTML")
    except Exception:
        pass
    await message.answer("✅ Пользователь заблокирован.", reply_markup=admin_menu())


@dp.callback_query(F.data.startswith("unban_user:"))
async def unban_user(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    user_id = int(callback.data.split(":")[1])
    cursor.execute("DELETE FROM banned_users WHERE user_id=?", (user_id,))
    db.commit()
    await callback.message.answer(f"🔓 Пользователь <code>{user_id}</code> разблокирован.", parse_mode="HTML", reply_markup=admin_menu())
    await callback.answer()


# ==================== INLINE MAIN MENU ====================

@dp.callback_query(F.data == "menu_home")
async def menu_home_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    register_user(callback.from_user)
    if await check_banned_user(callback.message):
        await callback.answer()
        return
    name = esc(callback.from_user.first_name or "друг")
    start_text = get_start_message(name)
    start_photo = get_start_photo()

    if start_photo:
        await callback.message.answer_photo(
            photo=start_photo,
            caption=start_text,
            reply_markup=user_menu(callback.from_user.id),
            parse_mode="HTML"
        )
    else:
        await callback.message.answer(
            start_text,
            reply_markup=user_menu(callback.from_user.id),
            parse_mode="HTML"
        )
    await callback.answer()


@dp.callback_query(F.data == "daily_spin")
async def daily_spin_callback(callback: CallbackQuery):
    if await check_banned_user(callback.message):
        await callback.answer()
        return

    register_user(callback.from_user)

    today = sqlite3.DateFromTicks(0) if False else None  # keeps logic entirely in SQLite/date strings
    cursor.execute(
        "SELECT prize FROM daily_spins WHERE user_id=? AND spin_date=DATE('now')",
        (callback.from_user.id,),
    )
    existing = cursor.fetchone()

    if existing:
        await callback.message.answer(
            "🎰 <b>ТЫ УЖЕ КРУТИЛ СЕГОДНЯ!</b>\n\n"
            f"Твой сегодняшний приз: <b>{esc(existing[0])}</b>\n\n"
            "⏰ Возвращайся завтра — будет новая попытка!",
            reply_markup=user_menu(callback.from_user.id),
            parse_mode="HTML",
        )
        await callback.answer("Попытка уже использована")
        return

    await callback.message.answer(
        "🎰 <b>ЗАПУСКАЮ КРУТИЛКУ...</b>\n\n"
        "🟦 🟨 🟥 🟩 🟪 🟧\n"
        "⬇️ Система выбирает твой приз...\n"
        "⬆️ Ещё чуть-чуть...\n"
        "🎲 ФИКСИРУЕМ РЕЗУЛЬТАТ!",
        parse_mode="HTML",
    )

    await asyncio.sleep(0.7)

    prizes = [
        ("🔥 Скидка 5%", 32),
        ("💎 Скидка 10%", 22),
        ("⚡ VIP-бонус", 12),
        ("🤑 Скидка 15%", 8),
        ("👑 Джекпот — скидка 25%", 2),
        ("😈 Почти повезло — возвращайся завтра", 8),
    ]

    values = [p[0] for p in prizes]
    weights = [p[1] for p in prizes]
    prize = random.choices(values, weights=weights, k=1)[0]

    cursor.execute(
        "INSERT INTO daily_spins(user_id, spin_date, prize) VALUES (?, DATE('now'), ?)",
        (callback.from_user.id, prize),
    )
    db.commit()

    await callback.message.answer(
        "🎰 <b>КРУТИЛКА ОСТАНОВИЛАСЬ!</b>\n\n"
        "🟣 <b>РЕЗУЛЬТАТ</b>\n\n"
        f"🏆 ТВОЙ ПРИЗ:\n\n"
        f"<b>✨ {esc(prize)} ✨</b>\n\n"
        "🎁 Попытка использована.\n"
        "⏰ Следующий спин — завтра!\n\n"
        "💡 Скидка действует на весь ассортимент до следующего прокрута.",
        reply_markup=user_menu(callback.from_user.id),
        parse_mode="HTML",
    )
    await callback.answer("🎉 Поздравляем!")

@dp.callback_query(F.data == "hot_product")
async def hot_product_callback(callback: CallbackQuery):
    if await check_banned_user(callback.message):
        await callback.answer()
        return

    offer = get_user_hot_offer(callback.from_user.id, create=True)

    if not offer:
        await callback.message.answer(
            "🔥 <b>ГОРЯЧИЙ ТОВАР</b>\n\n"
            "Сегодня персональное предложение уже закончилось.",
            reply_markup=user_menu(callback.from_user.id),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    if not offer["sent"]:
        wait = offer["scheduled_at"] - datetime.now()
        seconds = max(0, int(wait.total_seconds()))
        hours, remainder = divmod(seconds, 3600)
        minutes = remainder // 60
        wait_text = f"{hours} ч. {minutes:02d} мин." if hours else f"{minutes} мин."

        await callback.message.answer(
            "🔥 <b>ТВОЙ ГОРЯЧИЙ ТОВАР</b>\n\n"
            "Персональная скидка придёт тебе автоматически в случайный момент дня.\n\n"
            f"⏰ Ориентировочно через: <b>{wait_text}</b>\n"
            "🎲 Товар и скидка будут выбраны именно для тебя.",
            reply_markup=user_menu(callback.from_user.id),
            parse_mode="HTML",
        )
        await callback.answer("⏳ Жди персональную скидку!")
        return

    product_id = offer["product_id"]
    discount = offer["discount"]
    expires_at = offer["expires_at"]

    cursor.execute("""
        SELECT id, name, description, price, photo_id, stars_price, usdt_price
        FROM products
        WHERE id = ?
    """, (product_id,))
    product = cursor.fetchone()

    if not product or not expires_at or expires_at <= datetime.now():
        await callback.message.answer(
            "🔥 <b>ГОРЯЧИЙ ТОВАР</b>\n\nПерсональная скидка уже закончилась.",
            reply_markup=user_menu(callback.from_user.id),
            parse_mode="HTML",
        )
        await callback.answer()
        return

    product_id, name, description, price, photo_id, stars_price, usdt_price = product
    discounted_price = apply_discount(price, discount)
    discounted_usdt = apply_discount(usdt_price, discount) if usdt_price else 0

    text = (
        "🔥 <b>ТВОЙ ПЕРСОНАЛЬНЫЙ ГОРЯЧИЙ ТОВАР</b> 🔥\n\n"
        f"🛍 <b>{esc(name)}</b>\n\n"
        f"{esc(description or '')}\n\n"
        f"💰 Было: <s>{money(price)}</s>\n"
        f"🔥 Твоя цена: <b>{money(discounted_price)}</b>  (-{discount}%)\n"
        f"💵 USDT: <s>{float(usdt_price or 0):.2f}</s> → "
        f"<b>{float(discounted_usdt):.2f} USDT</b>\n\n"
        f"⏳ <b>До конца: {format_hot_remaining(expires_at)}</b>\n"
        "⚡ Скидка персональная и применяется автоматически."
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="🔥 Купить сейчас", callback_data=f"show_product:{product_id}")
    kb.button(text="🛍 Каталог", callback_data="menu_catalog")
    kb.button(text="🏠 Главное меню", callback_data="menu_home")
    kb.adjust(1)

    if photo_id:
        await callback.message.answer_photo(
            photo_id, caption=text, reply_markup=kb.as_markup(), parse_mode="HTML"
        )
    else:
        await callback.message.answer(
            text, reply_markup=kb.as_markup(), parse_mode="HTML"
        )
    await callback.answer("🔥 Твоя персональная скидка!")


@dp.callback_query(F.data == "menu_catalog")
async def menu_catalog_callback(callback: CallbackQuery):
    if await check_banned_user(callback.message):
        await callback.answer()
        return
    await send_catalog(callback.message)
    await callback.answer()

@dp.callback_query(F.data == "menu_cart")
async def menu_cart_callback(callback: CallbackQuery):
    if await check_banned_user(callback.message): await callback.answer(); return
    await send_cart(callback.message)
    await callback.answer()

@dp.callback_query(F.data == "menu_orders")
async def menu_orders_callback(callback: CallbackQuery):
    if await check_banned_user(callback.message): await callback.answer(); return
    cursor.execute("SELECT id, total, currency, total_stars, status, created_at FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 20", (callback.from_user.id,))
    orders=cursor.fetchall()
    if not orders:
        await callback.message.answer("📦 Заказов пока нет.", reply_markup=user_menu(callback.from_user.id))
    else:
        text="📦 <b>ЗАКАЗЫ</b>\n\n"
        for order_id,total,currency,stars,status,created_at in orders:
            amount = format_price(total, currency or "RUB")
            text += f"🧾 <b>#{order_id}</b> — {esc(status)}\n💰 {amount}\n📅 {created_at}\n\n"
        await callback.message.answer(text, parse_mode="HTML", reply_markup=user_menu(callback.from_user.id))
    await callback.answer()

@dp.callback_query(F.data == "menu_profile")
async def menu_profile_callback(callback: CallbackQuery):
    if await check_banned_user(callback.message):
        await callback.answer()
        return

    username = f"@{callback.from_user.username}" if callback.from_user.username else "Не указан"
    cursor.execute(
        "SELECT COUNT(*), COALESCE(SUM(CASE WHEN paid = 1 THEN 1 ELSE 0 END), 0), "
        "COALESCE(SUM(CASE WHEN paid = 1 AND currency = 'RUB' THEN total ELSE 0 END), 0) "
        "FROM orders WHERE user_id = ?",
        (callback.from_user.id,),
    )
    count, paid_count, rub_spent = cursor.fetchone()

    display_name = esc(callback.from_user.first_name or "Покупатель")
    username_text = esc(username)

    profile_text = (
        "🟣 <b>МОЙ ПРОФИЛЬ</b>\n\n"
        f"👋 <b>{display_name}</b>\n"
        f"🔗 {username_text}\n\n"
        "📊 <b>СТАТИСТИКА</b>\n"
        f"📦 Заказов: <b>{count}</b>\n"
        f"✅ Оплачено: <b>{paid_count}</b>\n"
        f"💰 Потрачено в RUB: <b>{float(rub_spent):.2f} ₽</b>\n\n"
        "🛡 <b>Premium-статус</b>\n"
        "⚡ Быстрая обработка\n"
        "💬 Персональная поддержка\n\n"
        "<i>Спасибо, что выбираете Felecaster Shop.</i>"
    )

    await callback.message.answer(
        profile_text,
        parse_mode="HTML",
        reply_markup=profile_keyboard(),
    )
    await callback.answer()

@dp.callback_query(F.data == "menu_about")
async def menu_about_callback(callback: CallbackQuery):
    if await check_banned_user(callback.message):
        await callback.answer()
        return

    about_text = (
        "🟣 <b>О FELECASTER SHOP</b>\n\n"
        "✦ <b>FELECASTER SHOP</b>\n\n"
        "Добро пожаловать в наш магазин цифровых товаров.\n\n"
        "🛡 <b>НАДЁЖНО И ПРОЗРАЧНО</b>\n\n"
        "• Каждый заказ получает уникальный номер.\n"
        "• История заказов сохраняется в вашем профиле.\n"
        "• Статус заказа можно проверить в разделе «📦 Заказы».\n"
        "• Оплата проверяется перед выдачей товара.\n"
        "• При возникновении проблемы доступна поддержка.\n\n"
        "⚡ <b>КАК ПРОХОДИТ ПОКУПКА</b>\n\n"
        "1️⃣ Выберите товар в каталоге.\n"
        "2️⃣ Нажмите «💳 Купить» или добавьте его в корзину.\n"
        "3️⃣ Выберите способ оплаты.\n"
        "4️⃣ Оплатите указанную сумму.\n"
        "5️⃣ Если требуется, отправьте подтверждение оплаты.\n"
        "6️⃣ После проверки заказ передаётся на выдачу.\n\n"
        "💳 <b>ОПЛАТА</b>\n\n"
        "Доступные способы оплаты отображаются непосредственно при оформлении заказа.\n\n"
        "⚠️ Перед переводом обязательно проверьте номер заказа, сумму и реквизиты, которые показывает бот.\n\n"
        "🔐 <b>БЕЗОПАСНОСТЬ</b>\n\n"
        "Используйте только реквизиты, которые отображаются внутри официального бота. "
        "Не отправляйте оплату по реквизитам от посторонних лиц.\n\n"
        "🎁 <b>ПОЛУЧЕНИЕ ТОВАРА</b>\n\n"
        "В зависимости от товара выдача может происходить автоматически или вручную.\n\n"
        "💬 <b>ПОДДЕРЖКА</b>\n\n"
        "Если возник вопрос по товару, оплате или заказу — откройте «💬 Поддержка».\n\n"
        "📋 <b>УСЛОВИЯ ПОКУПКИ</b>\n\n"
        "Перед оплатой внимательно ознакомьтесь с описанием и стоимостью товара. "
        "По вопросам возврата, замены или спорной ситуации обратитесь в поддержку — каждый случай рассматривается индивидуально.\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🤝 <i>Спасибо, что выбираете FELECASTER SHOP.</i>"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="🛍 Перейти в каталог", callback_data="menu_catalog")
    builder.button(text="💬 Поддержка", callback_data="menu_support")
    builder.button(text="🏠 Главное меню", callback_data="menu_home")
    builder.adjust(1)

    await callback.message.answer(
        about_text,
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "menu_support")
async def menu_support_callback(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id == ADMIN_ID:
        await callback.message.answer("💬 Выберите пользователя в разделе поддержки.", reply_markup=admin_menu())
    elif await check_banned_user(callback.message):
        pass
    else:
        register_user(callback.from_user)
        await state.set_state(SupportState.message)
        await callback.message.answer("💬 <b>ПОДДЕРЖКА</b>\n\nНапишите сообщение администратору.", parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "menu_admin")
async def menu_admin_callback(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: await callback.answer(); return
    await callback.message.answer("⚙️ <b>FELECASTER CONTROL</b>\n\nВыберите действие 👇", reply_markup=admin_menu(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "admin_start_message")
async def admin_start_message(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return

    cursor.execute("SELECT value FROM bot_settings WHERE key='start_message'")
    current = cursor.fetchone()
    cursor.execute("SELECT value FROM bot_settings WHERE key='start_photo_id'")
    photo_row = cursor.fetchone()
    has_photo = bool(photo_row and photo_row[0])

    await state.set_state(StartMessageState.text)
    await callback.message.answer(
        "✏️ <b>РЕДАКТИРОВАНИЕ /start</b>\n\n"
        f"📸 Фото: {'✅ установлено' if has_photo else '❌ отсутствует'}\n\n"
        "📝 Отправьте <b>текст</b> — изменится текст.\n"
        "📸 Отправьте <b>фото с подписью</b> — изменятся фото и текст.\n"
        "🖼 Отправьте <b>только фото</b> — изменится только фото.\n\n"
        "Для имени используйте <code>{name}</code>.\n"
        "Можно использовать HTML: <code>&lt;b&gt;</code>, <code>&lt;i&gt;</code>.\n\n"
        "<b>Текущий текст:</b>\n\n" + (current[0] if current else ""),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.message(StartMessageState.text)
async def save_start_message(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    if message.photo:
        photo_id = message.photo[-1].file_id
        cursor.execute("""
            INSERT INTO bot_settings(key, value) VALUES('start_photo_id', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (photo_id,))

        caption = (message.caption or "").strip()
        if caption:
            cursor.execute("""
                INSERT INTO bot_settings(key, value) VALUES('start_message', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """, (caption,))

        db.commit()
        await state.clear()
        await message.answer(
            "✅ Фото и текст сообщения /start сохранены." if caption else "✅ Фото для сообщения /start сохранено.",
            reply_markup=admin_menu()
        )
        return

    text = (message.text or "").strip()
    if not text:
        await message.answer("❌ Отправьте текст или фотографию.")
        return

    try:
        cursor.execute("""
            INSERT INTO bot_settings(key, value) VALUES('start_message', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (text,))
        db.commit()
        await state.clear()
        await message.answer("✅ Текст сообщения /start сохранён.", reply_markup=admin_menu())
    except Exception as e:
        await message.answer(f"❌ Не удалось сохранить: {esc(e)}", parse_mode="HTML")

@dp.callback_query(F.data == "admin_catalog_message")
async def admin_catalog_message(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return

    current = get_catalog_message()
    has_photo = bool(get_catalog_photo())
    await state.set_state(CatalogMessageState.content)
    await callback.message.answer(
        "✏️ <b>РЕДАКТИРОВАНИЕ КАТАЛОГА</b>\n\n"
        f"📸 Фото: {'✅ установлено' if has_photo else '❌ отсутствует'}\n\n"
        "📝 Отправьте <b>текст</b> — изменится сообщение каталога.\n"
        "📸 Отправьте <b>фото с подписью</b> — изменятся баннер и текст.\n"
        "🖼 Отправьте <b>только фото</b> — изменится только баннер.\n\n"
        "Можно использовать HTML: <code>&lt;b&gt;</code>, <code>&lt;i&gt;</code>.\n\n"
        "<b>Текущий текст:</b>\n\n" + current,
        parse_mode="HTML"
    )
    await callback.answer()


@dp.message(CatalogMessageState.content)
async def save_catalog_message(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    if message.photo:
        photo_id = message.photo[-1].file_id
        cursor.execute("""
            INSERT INTO bot_settings(key, value) VALUES('catalog_photo_id', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (photo_id,))

        caption = (message.caption or "").strip()
        if caption:
            cursor.execute("""
                INSERT INTO bot_settings(key, value) VALUES('catalog_message', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """, (caption,))

        db.commit()
        await state.clear()
        await message.answer(
            "✅ Баннер и текст каталога сохранены." if caption else "✅ Баннер каталога сохранён.",
            reply_markup=admin_menu()
        )
        return

    text = (message.text or "").strip()
    if not text:
        await message.answer("❌ Отправьте текст или фотографию.")
        return

    try:
        cursor.execute("""
            INSERT INTO bot_settings(key, value) VALUES('catalog_message', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (text,))
        db.commit()
        await state.clear()
        await message.answer("✅ Сообщение каталога сохранено.", reply_markup=admin_menu())
    except Exception as e:
        await message.answer(f"❌ Не удалось сохранить: {esc(e)}", parse_mode="HTML")


# ==================== ADMIN INLINE MENU ROUTING ====================

@dp.callback_query(F.data == "admin_promos")
async def admin_promos_callback(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer()
    cursor.execute("SELECT code, discount, max_uses, uses, active FROM promo_codes ORDER BY code")
    rows = cursor.fetchall()
    b = InlineKeyboardBuilder()
    if rows:
        for code, discount, max_uses, uses, active in rows:
            status = "🟢" if active else "🔴"
            limit = "∞" if not max_uses else str(max_uses)
            b.button(text=f"{status} {code} • {discount}% • {uses}/{limit}", callback_data=f"admin_promo_delete:{code}")
    b.button(text="➕ Добавить промокод", callback_data="admin_promo_add")
    b.button(text="⬅️ Админ-панель", callback_data="menu_admin")
    b.adjust(1)
    text = "🎟 <b>ПРОМОКОДЫ</b>\n\n"
    text += "Нажми на промокод, чтобы удалить его.\n" if rows else "Промокодов пока нет.\n"
    text += "\n➕ Создать новый промокод"
    await callback.message.answer(text, reply_markup=b.as_markup(), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "admin_promo_add")
async def admin_promo_add(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer()
    await state.clear()
    await state.set_state(AdminPromoState.code)
    await callback.message.answer("🎟 <b>Добавление промокода</b>\n\nВведи код, например: <code>FELE10</code>", parse_mode="HTML")
    await callback.answer()


@dp.message(AdminPromoState.code)
async def admin_promo_code(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    code = (message.text or "").strip().upper()
    if not code or len(code) > 32 or any(ch.isspace() for ch in code):
        await message.answer("❌ Код должен быть одним словом, до 32 символов. Попробуй ещё раз:")
        return
    await state.update_data(code=code)
    await state.set_state(AdminPromoState.discount)
    await message.answer("💸 Введи скидку в процентах от <b>1</b> до <b>100</b>.", parse_mode="HTML")


@dp.message(AdminPromoState.discount)
async def admin_promo_discount(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        discount = int((message.text or "").strip().replace("%", ""))
    except ValueError:
        await message.answer("❌ Введи целое число от 1 до 100.")
        return
    if not 1 <= discount <= 100:
        await message.answer("❌ Скидка должна быть от 1 до 100%.")
        return
    await state.update_data(discount=discount)
    await state.set_state(AdminPromoState.max_uses)
    await message.answer("🔢 Введи максимальное число использований.\n\n<b>0</b> — без ограничений.", parse_mode="HTML")


@dp.message(AdminPromoState.max_uses)
async def admin_promo_max_uses(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        max_uses = int((message.text or "").strip())
    except ValueError:
        await message.answer("❌ Введи целое число: 0, 1, 2, 3...", parse_mode="HTML")
        return
    if max_uses < 0:
        await message.answer("❌ Лимит не может быть отрицательным.")
        return
    data = await state.get_data()
    code = data.get("code")
    discount = int(data.get("discount"))
    cursor.execute("SELECT 1 FROM promo_codes WHERE code=?", (code,))
    exists = cursor.fetchone() is not None
    cursor.execute("""
        INSERT INTO promo_codes(code, discount, max_uses, uses, active)
        VALUES(?,?,?,0,1)
        ON CONFLICT(code) DO UPDATE SET
            discount=excluded.discount,
            max_uses=excluded.max_uses,
            active=1
    """, (code, discount, max_uses))
    db.commit()
    await state.clear()
    action = "обновлён" if exists else "создан"
    limit_text = "без ограничений" if max_uses == 0 else str(max_uses)
    await message.answer(
        f"✅ Промокод <code>{esc(code)}</code> {action}.\n\n"
        f"💸 Скидка: <b>{discount}%</b>\n"
        f"🔢 Лимит: <b>{limit_text}</b>",
        parse_mode="HTML", reply_markup=admin_menu()
    )


@dp.callback_query(F.data.startswith("admin_promo_delete:"))
async def admin_promo_delete(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer()
    code = callback.data.split(":", 1)[1]
    cursor.execute("DELETE FROM promo_codes WHERE code=?", (code,))
    cursor.execute("DELETE FROM user_promos WHERE code=?", (code,))
    db.commit()
    await callback.answer("Промокод удалён")
    await callback.message.delete()
    await callback.message.answer("🎟 <b>ПРОМОКОДЫ</b>\n\nПромокод удалён. Открой раздел снова.", reply_markup=admin_menu(), parse_mode="HTML")


@dp.callback_query(F.data == "admin_users")
async def admin_users_callback(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return await callback.answer()
    await callback.message.answer("👥 <b>ПОЛЬЗОВАТЕЛИ</b>\n\nВыберите пользователя:", reply_markup=users_keyboard(), parse_mode="HTML"); await callback.answer()

@dp.callback_query(F.data == "admin_support")
async def admin_support_callback(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return await callback.answer()
    await callback.message.answer("💬 <b>ПОДДЕРЖКА</b>\n\nВыберите пользователя:", reply_markup=users_keyboard("support_user"), parse_mode="HTML"); await callback.answer()

@dp.callback_query(F.data == "admin_statistics")
async def admin_statistics_callback(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return await callback.answer()
    cursor.execute("SELECT COUNT(*) FROM users")
    users = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM products")
    cursor.execute("SELECT COUNT(*) FROM products"); products=cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM categories"); categories=cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM orders"); orders=cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM inventory WHERE issued=0"); stock=cursor.fetchone()[0]
    cursor.execute("SELECT COALESCE(SUM(total),0) FROM orders WHERE paid=1 AND currency='RUB'"); revenue_rub=cursor.fetchone()[0]
    cursor.execute("SELECT COALESCE(SUM(total),0) FROM orders WHERE paid=1 AND currency='USDT'"); revenue_usdt=cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM promo_codes WHERE active=1"); active_promos=cursor.fetchone()[0]
    cursor.execute("SELECT COALESCE(SUM(uses),0) FROM promo_codes"); promo_uses=cursor.fetchone()[0]
    await callback.message.answer(f"📊 <b>СТАТИСТИКА</b>\n\n👥 Пользователей: <b>{users}</b>\n📁 Категорий: <b>{categories}</b>\n🛍 Товаров: <b>{products}</b>\n📦 Заказов: <b>{orders}</b>\n🎁 Ключей: <b>{stock}</b>\n🎟 Активных промокодов: <b>{active_promos}</b>\n🎟 Использований промокодов: <b>{promo_uses}</b>\n\n💰 RUB: <b>{revenue_rub:.2f} ₽</b>\n💵 USDT: <b>{revenue_usdt:.2f} USDT</b>", reply_markup=admin_menu(), parse_mode="HTML"); await callback.answer()

@dp.callback_query(F.data == "admin_orders")
async def admin_orders_callback(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return await callback.answer()
    cursor.execute("SELECT id, customer_name, total, currency FROM orders ORDER BY id DESC LIMIT 30"); rows=cursor.fetchall(); b=InlineKeyboardBuilder()
    for oid,name,total,currency in rows: b.button(text=f"#{oid} • {name or 'Без имени'} • {format_price(total,currency or 'RUB')}", callback_data=f"admin_order:{oid}")
    b.button(text="⬅️ Админ-панель", callback_data="menu_admin"); b.adjust(1)
    await callback.message.answer("📦 <b>ЗАКАЗЫ</b>", reply_markup=b.as_markup(), parse_mode="HTML"); await callback.answer()

@dp.callback_query(F.data == "admin_products")
async def admin_products_callback(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return await callback.answer()
    await callback.message.answer("📋 <b>КАТАЛОГ ТОВАРОВ</b>\n\nВыберите позицию:", reply_markup=admin_products_keyboard(), parse_mode="HTML"); await callback.answer()

@dp.callback_query(F.data == "admin_categories")
async def admin_categories_callback(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer()

    cursor.execute("SELECT COUNT(*) FROM categories")
    if cursor.fetchone()[0] == 0:
        await callback.message.answer(
            "🗂 <b>КАТЕГОРИИ</b>\n\nКатегорий пока нет.",
            reply_markup=admin_menu(),
            parse_mode="HTML"
        )
        await callback.answer()
        return

    await callback.message.answer(
        "🗂 <b>РЕДАКТИРОВАНИЕ КАТЕГОРИЙ</b>\n\nВыберите категорию:",
        reply_markup=admin_categories_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("admin_delete_category:"))
async def admin_delete_category_callback(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer()
    category_id = int(callback.data.split(":")[1])
    cursor.execute("SELECT name FROM categories WHERE id=?", (category_id,))
    category = cursor.fetchone()
    if not category:
        await callback.answer("Категория не найдена.", show_alert=True)
        return
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, удалить", callback_data=f"admin_confirm_delete_category:{category_id}")
    builder.button(text="❌ Отмена", callback_data="admin_categories")
    builder.adjust(1)
    await callback.message.answer(
        f"⚠️ <b>Удаление категории</b>\n\nВы действительно хотите удалить категорию <b>{esc(category[0])}</b>?\n\nВсе товары этой категории также будут удалены.",
        parse_mode="HTML", reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("admin_confirm_delete_category:"))
async def admin_confirm_delete_category_callback(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer()
    category_id = int(callback.data.split(":")[1])
    cursor.execute("SELECT name FROM categories WHERE id=?", (category_id,))
    category = cursor.fetchone()
    if not category:
        await callback.answer("Категория уже удалена.", show_alert=True)
        return
    try:
        # Удаляем связанные данные вручную, чтобы удаление работало
        # даже на старых БД, где каскадные FK могли быть созданы без CASCADE.
        cursor.execute("""
            DELETE FROM inventory
            WHERE product_id IN (
                SELECT id FROM products WHERE category_id = ?
            )
        """, (category_id,))

        cursor.execute("""
            DELETE FROM cart
            WHERE product_id IN (
                SELECT id FROM products WHERE category_id = ?
            )
        """, (category_id,))

        cursor.execute(
            "DELETE FROM products WHERE category_id = ?",
            (category_id,)
        )

        cursor.execute(
            "DELETE FROM categories WHERE id = ?",
            (category_id,)
        )

        if cursor.rowcount == 0:
            db.rollback()
            await callback.answer("Категория уже удалена.", show_alert=True)
            return

        db.commit()

    except Exception as e:
        db.rollback()
        await callback.answer(
            f"Ошибка удаления: {str(e)[:150]}",
            show_alert=True
        )
        return

    # После удаления возвращаемся к списку категорий.
    await callback.message.answer(
        f"🗑 <b>Категория удалена</b>\n\n"
        f"Категория <b>{esc(category[0])}</b> и все её товары удалены.",
        parse_mode="HTML",
        reply_markup=admin_categories_keyboard()
    )
    await callback.answer("Категория удалена ✅")

@dp.callback_query(F.data == "admin_add_category")
async def admin_add_category_callback(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return await callback.answer()
    await state.set_state(AddCategoryState.name); await callback.message.answer("📁 Введите название новой категории:"); await callback.answer()

@dp.callback_query(F.data == "admin_add_product")
async def admin_add_product_callback(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return await callback.answer()
    cursor.execute("SELECT COUNT(*) FROM categories")
    if cursor.fetchone()[0] == 0: await callback.message.answer("Сначала создайте категорию.", reply_markup=admin_menu()); await callback.answer(); return
    await state.set_state(AddProductState.category); await callback.message.answer("🛍 Выберите категорию:", reply_markup=categories_keyboard("new_product")); await callback.answer()

@dp.callback_query(F.data == "admin_add_keys")
async def admin_add_keys_callback(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return await callback.answer()
    await state.set_state(AddKeysState.product); await callback.message.answer("🎁 Выберите товар:", reply_markup=admin_products_keyboard("add_keys_product")); await callback.answer()

# ==================== ПОДДЕРЖКА ====================

@dp.message(F.text == "💬 Поддержка", F.from_user.id != ADMIN_ID)
async def support_start(message: Message, state: FSMContext):
    register_user(message.from_user)
    if await check_banned_user(message):
        return
    await state.set_state(SupportState.message)
    await message.answer("💬 <b>ПОДДЕРЖКА</b>\n\nНапишите сообщение администратору. Для выхода нажмите «🏠 Главное меню».", parse_mode="HTML")


@dp.message(SupportState.message)
async def support_receive(message: Message, state: FSMContext):
    if message.text == "🏠 Главное меню":
        await state.clear()
        await home(message)
        return
    if await check_banned_user(message):
        await state.clear()
        return
    register_user(message.from_user)
    label = f"@{message.from_user.username}" if message.from_user.username else (message.from_user.first_name or str(message.from_user.id))
    builder = InlineKeyboardBuilder()
    builder.button(text="↩️ Ответить", callback_data=f"support_reply:{message.from_user.id}")
    builder.adjust(1)
    await message.bot.send_message(ADMIN_ID, f"💬 <b>НОВОЕ ОБРАЩЕНИЕ</b>\n\n👤 {esc(label)}\n🆔 <code>{message.from_user.id}</code>", parse_mode="HTML")
    await message.copy_to(ADMIN_ID, reply_markup=builder.as_markup())
    await message.answer("✅ Сообщение отправлено в поддержку. Ожидайте ответа.", reply_markup=user_menu(message.from_user.id))
    await state.clear()


@dp.message(F.text == "💬 Поддержка", F.from_user.id == ADMIN_ID)
async def support_admin(message: Message):
    await message.answer("💬 <b>ПОДДЕРЖКА</b>\n\nВыберите пользователя:", reply_markup=users_keyboard("support_user"), parse_mode="HTML")


@dp.callback_query(F.data.startswith("support_reply:"))
async def support_reply(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    user_id = int(callback.data.split(":")[1])
    await state.update_data(user_id=user_id)
    await state.set_state(AdminReplyState.message)
    await callback.message.answer(f"↩️ Введите ответ пользователю <code>{user_id}</code>:", parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data.startswith("support_user:"))
async def support_user_select(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    user_id = int(callback.data.split(":")[1])
    await state.update_data(user_id=user_id)
    await state.set_state(AdminReplyState.message)
    await callback.message.answer(f"💬 Введите сообщение пользователю <code>{user_id}</code>:", parse_mode="HTML")
    await callback.answer()


@dp.message(AdminReplyState.message)
async def admin_reply(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    user_id = data.get("user_id")
    if not user_id:
        await state.clear()
        await message.answer("❌ Пользователь не выбран.", reply_markup=admin_menu())
        return
    if message.text == "🏠 Главное меню":
        await state.clear()
        await message.answer("🏠 Главное меню", reply_markup=admin_menu())
        return
    try:
        await message.copy_to(int(user_id))
        await message.answer(f"✅ Ответ отправлен пользователю <code>{user_id}</code>.", parse_mode="HTML", reply_markup=admin_menu())
    except Exception as e:
        await message.answer("❌ Не удалось отправить сообщение. Возможно, пользователь заблокировал бота.", reply_markup=admin_menu())
    await state.clear()


@dp.message(F.text == "📊 Статистика")
async def statistics(message: Message):
    if await check_banned_user(message):
        return

    if message.from_user.id != ADMIN_ID:
        return

    cursor.execute("SELECT COUNT(*) FROM products")
    products = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM categories")
    categories = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM orders")
    orders = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COUNT(*)
        FROM inventory
        WHERE issued = 0
    """)
    stock = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COALESCE(SUM(total_stars), 0)
        FROM orders
        WHERE paid = 1
    """)
    revenue = cursor.fetchone()[0]

    await message.answer(
        "📊 <b>СТАТИСТИКА</b>\n\n"
        f"📁 Категорий: <b>{categories}</b>\n"
        f"🛍 Товаров: <b>{products}</b>\n"
        f"📦 Заказов: <b>{orders}</b>\n"
        f"🎁 Ключей в наличии: <b>{stock}</b>\n"
        f"\n"
        "💳 RUB/USDT учитываются в заказах с соответствующей валютой.",
        reply_markup=admin_menu(),
        parse_mode="HTML",
    )


@dp.message(F.text == "📁 Добавить категорию")
async def add_category_start(
    message: Message,
    state: FSMContext,
):
    if await check_banned_user(message):
        return

    if message.from_user.id != ADMIN_ID:
        return

    await state.set_state(AddCategoryState.name)

    await message.answer(
        "📁 Введите название новой категории:",
    )


@dp.message(AddCategoryState.name)
async def add_category_finish(
    message: Message,
    state: FSMContext,
):
    if await check_banned_user(message):
        return

    if message.from_user.id != ADMIN_ID:
        return

    name = message.text.strip()

    if not name:
        await message.answer(
            "Название не может быть пустым.",
        )
        return

    try:
        cursor.execute(
            "INSERT INTO categories(name, description, photo_id) VALUES (?, '', NULL)",
            (name,),
        )
        db.commit()
    except sqlite3.IntegrityError:
        await message.answer(
            "Такая категория уже существует.",
        )
        return

    await state.clear()

    await message.answer(
        f"✅ Категория <b>{esc(name)}</b> создана.",
        reply_markup=admin_menu(),
        parse_mode="HTML",
    )


# ==================== ADMIN PRODUCTS ====================

@dp.message(F.text == "🛍 Добавить товар")
async def add_product_start(
    message: Message,
    state: FSMContext,
):
    if await check_banned_user(message):
        return

    if message.from_user.id != ADMIN_ID:
        return

    cursor.execute(
        "SELECT COUNT(*) FROM categories"
    )

    if cursor.fetchone()[0] == 0:
        await message.answer(
            "Сначала создайте категорию.",
        )
        return

    await state.set_state(
        AddProductState.category
    )

    await message.answer(
        "🛍 Выберите категорию:",
        reply_markup=categories_keyboard(
            "new_product"
        ),
    )


@dp.callback_query(F.data.startswith("new_product:"))
async def new_product_category(
    callback: CallbackQuery,
    state: FSMContext,
):
    if callback.from_user.id != ADMIN_ID:
        return

    category_id = int(
        callback.data.split(":")[1]
    )

    cursor.execute(
        "SELECT name FROM categories WHERE id = ?",
        (category_id,),
    )

    category = cursor.fetchone()

    if not category:
        await callback.answer(
            "Категория не найдена.",
            show_alert=True,
        )
        return

    await state.update_data(
        category_id=category_id
    )

    await state.set_state(
        AddProductState.name
    )

    await callback.message.answer(
        "Шаг 2/6. Введите название товара:",
    )

    await callback.answer()


@dp.message(AddProductState.name)
async def new_product_name(
    message: Message,
    state: FSMContext,
):
    if await check_banned_user(message):
        return

    if message.from_user.id != ADMIN_ID:
        return

    await state.update_data(
        name=message.text.strip()
    )

    await state.set_state(
        AddProductState.description
    )

    await message.answer(
        "Шаг 3/6. Введите описание товара:",
    )


@dp.message(AddProductState.description)
async def new_product_description(
    message: Message,
    state: FSMContext,
):
    if await check_banned_user(message):
        return

    if message.from_user.id != ADMIN_ID:
        return

    await state.update_data(
        description=message.text.strip()
    )

    await state.set_state(
        AddProductState.price
    )

    await message.answer(
        "Шаг 4/6. Введите цену в рублях.\n\n"
        "Например: <code>799</code>",
        parse_mode="HTML",
    )


@dp.message(AddProductState.price)
async def new_product_price(
    message: Message,
    state: FSMContext,
):
    if await check_banned_user(message):
        return

    if message.from_user.id != ADMIN_ID:
        return

    try:
        price = float(
            message.text.replace(",", ".").strip()
        )

        if price < 0:
            raise ValueError

    except ValueError:
        await message.answer(
            "Введите корректную цену.",
        )
        return

    await state.update_data(
        price=price
    )

    await state.set_state(AddProductState.usdt)
    await message.answer(
        "Шаг 5/6 · USDT\n\nВведите цену в USDT.\n"
        "Можно указать 0, если USDT не нужен.\n"
        "Например: <code>9.99</code>",
        parse_mode="HTML",
    )


@dp.message(AddProductState.usdt)
async def new_product_usdt(
    message: Message,
    state: FSMContext,
):
    if await check_banned_user(message):
        return

    if message.from_user.id != ADMIN_ID:
        return
    try:
        usdt = float(message.text.replace(",", ".").strip())
        if usdt < 0:
            raise ValueError
    except (ValueError, AttributeError):
        await message.answer("Введите корректную цену USDT, например <code>9.99</code>.", parse_mode="HTML")
        return
    await state.update_data(usdt=usdt)
    await state.set_state(AddProductState.photo)
    await message.answer("Шаг 6/6 · Витрина\n\nОтправьте фотографию товара.")


@dp.message(
    AddProductState.photo,
    F.photo,
)
async def new_product_photo(
    message: Message,
    state: FSMContext,
):
    if message.from_user.id != ADMIN_ID:
        return

    data = await state.get_data()

    cursor.execute("""
        INSERT INTO products(
            category_id,
            name,
            description,
            price,
            photo_id,
            stars_price,
            usdt_price
        )
        VALUES (?, ?, ?, ?, ?, 0, ?)
    """, (
        data["category_id"],
        data["name"],
        data["description"],
        data["price"],
        message.photo[-1].file_id,
        data.get("usdt", 0),
    ))

    db.commit()

    product_id = cursor.lastrowid

    await state.clear()

    await message.answer(
        "🎉 <b>ТОВАР СОЗДАН</b>\n\n"
        f"🛍 {esc(data['name'])}\n"
        f"💰 {data['price']:.2f} ₽\n"
        f"💵 {data.get('usdt', 0):.2f} USDT\n\n"
        f"ID товара: <code>{product_id}</code>\n\n"
        "Теперь добавьте ключи через "
        "«🎁 Добавить ключи».",
        reply_markup=admin_menu(),
        parse_mode="HTML",
    )


@dp.message(AddProductState.photo)
async def wrong_product_photo(
    message: Message,
):
    if await check_banned_user(message):
        return

    if message.from_user.id == ADMIN_ID:
        await message.answer(
            "Отправьте именно фотографию товара.",
        )


# ==================== INVENTORY ====================

@dp.message(F.text == "🎁 Добавить ключи")
async def add_keys_start(
    message: Message,
    state: FSMContext,
):
    if await check_banned_user(message):
        return

    if message.from_user.id != ADMIN_ID:
        return

    cursor.execute(
        "SELECT COUNT(*) FROM products"
    )

    if cursor.fetchone()[0] == 0:
        await message.answer(
            "Сначала создайте товар.",
        )
        return

    await state.set_state(
        AddKeysState.product
    )

    await message.answer(
        "🎁 Выберите товар:",
        reply_markup=admin_products_keyboard(
            "stock_product"
        ),
    )


@dp.callback_query(F.data.startswith("stock_product:"))
async def stock_product(
    callback: CallbackQuery,
    state: FSMContext,
):
    if callback.from_user.id != ADMIN_ID:
        return

    product_id = int(
        callback.data.split(":")[1]
    )

    cursor.execute(
        "SELECT name FROM products WHERE id = ?",
        (product_id,),
    )

    product = cursor.fetchone()

    if not product:
        await callback.answer(
            "Товар не найден.",
            show_alert=True,
        )
        return

    await state.update_data(
        product_id=product_id
    )

    await state.set_state(
        AddKeysState.codes
    )

    await callback.message.answer(
        f"🎁 Товар: <b>{esc(product[0])}</b>\n\n"
        "Отправьте ключи одним сообщением — "
        "<b>по одному ключу на строку</b>.\n\n"
        "Пример:\n"
        "<code>AAAA-BBBB-1111</code>\n"
        "<code>CCCC-DDDD-2222</code>",
        parse_mode="HTML",
    )

    await callback.answer()


@dp.message(AddKeysState.codes)
async def add_keys_finish(
    message: Message,
    state: FSMContext,
):
    if await check_banned_user(message):
        return

    if message.from_user.id != ADMIN_ID:
        return

    data = await state.get_data()

    codes = [
        line.strip()
        for line in message.text.splitlines()
        if line.strip()
    ]

    if not codes:
        await message.answer(
            "Не найдено ни одного ключа.",
        )
        return

    for code in codes:
        cursor.execute("""
            INSERT INTO inventory(
                product_id,
                code
            )
            VALUES (?, ?)
        """, (
            data["product_id"],
            code,
        ))

    db.commit()

    await state.clear()

    await message.answer(
        f"✅ Добавлено ключей: <b>{len(codes)}</b>",
        reply_markup=admin_menu(),
        parse_mode="HTML",
    )


# ==================== CATEGORY EDITING ====================

def admin_categories_keyboard():
    builder = InlineKeyboardBuilder()

    cursor.execute("SELECT id, name FROM categories ORDER BY id DESC")
    rows = cursor.fetchall()

    for category_id, name in rows:
        builder.button(
            text=f"✏️ {name}",
            callback_data=f"edit_category:{category_id}"
        )
        builder.button(
            text=f"🗑 Удалить {name}",
            callback_data=f"admin_delete_category:{category_id}"
        )

    builder.button(text="⬅️ Админ-панель", callback_data="admin_back")
    builder.adjust(1)
    return builder.as_markup()


def edit_category_keyboard(category_id, has_description=False, has_photo=False):
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Название", callback_data=f"category_field:name:{category_id}")
    builder.button(text="📝 Добавить/изменить описание", callback_data=f"category_field:description:{category_id}")
    if has_description:
        builder.button(text="🗑 Удалить описание", callback_data=f"category_description_delete:{category_id}")
    builder.button(text="🖼 Добавить/изменить фото", callback_data=f"category_field:photo:{category_id}")
    if has_photo:
        builder.button(text="🗑 Удалить фото", callback_data=f"category_photo_delete:{category_id}")
    builder.button(text="⬅️ Назад", callback_data="category_edit_back")
    builder.adjust(1)
    return builder.as_markup()


@dp.message(F.text == "🗂 Категории")
async def admin_categories(message: Message):
    if await check_banned_user(message):
        return
    if message.from_user.id != ADMIN_ID:
        return

    cursor.execute("SELECT COUNT(*) FROM categories")
    if cursor.fetchone()[0] == 0:
        await message.answer(
            "Категорий пока нет.",
            reply_markup=admin_menu()
        )
        return

    await message.answer(
        "🗂 <b>РЕДАКТИРОВАНИЕ КАТЕГОРИЙ</b>\n\n"
        "Выберите категорию:",
        reply_markup=admin_categories_keyboard(),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "category_edit_back")
async def category_edit_back(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    await callback.message.answer(
        "🗂 <b>РЕДАКТИРОВАНИЕ КАТЕГОРИЙ</b>\n\n"
        "Выберите категорию:",
        reply_markup=admin_categories_keyboard(),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("edit_category:"))
async def edit_category_start(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    category_id = int(callback.data.split(":")[1])

    cursor.execute(
        "SELECT name, description, photo_id FROM categories WHERE id = ?",
        (category_id,)
    )
    category = cursor.fetchone()

    if not category:
        await callback.answer("Категория не найдена.", show_alert=True)
        return

    name, description, photo_id = category

    text = (
        f"🗂 <b>{esc(name)}</b>\n\n"
        "Что хотите изменить?"
    )
    if description:
        text += "\n📝 <b>Описание добавлено</b>"
    else:
        text += "\n📝 <b>Описание не задано</b>"
    if photo_id:
        text += "\n🖼 <b>Фото добавлено</b>"
    else:
        text += "\n🖼 <b>Фото не задано</b>"

    await callback.message.answer(
        text,
        reply_markup=edit_category_keyboard(category_id, bool(description), bool(photo_id)),
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("category_field:"))
async def edit_category_field(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return

    _, field, category_id = callback.data.split(":")
    category_id = int(category_id)

    cursor.execute(
        "SELECT id FROM categories WHERE id = ?",
        (category_id,)
    )
    if not cursor.fetchone():
        await callback.answer("Категория не найдена.", show_alert=True)
        return

    await state.update_data(category_id=category_id, field=field)

    if field == "photo":
        await state.set_state(EditCategoryState.photo)
        await callback.message.answer("🖼 Отправьте новое фото категории.")
    else:
        await state.set_state(
            EditCategoryState.name if field == "name"
            else EditCategoryState.description
        )
        await callback.message.answer(
            "✏️ Введите новое название категории:"
            if field == "name"
            else "📝 Введите описание категории (или отправьте /clear, чтобы удалить):"
        )

    await callback.answer()


@dp.message(EditCategoryState.name)
async def edit_category_name(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    data = await state.get_data()
    category_id = data["category_id"]
    value = (message.text or "").strip()

    if not value:
        await message.answer("❌ Название не может быть пустым.")
        return

    cursor.execute(
        "UPDATE categories SET name = ? WHERE id = ?",
        (value, category_id)
    )
    db.commit()
    await state.clear()

    await message.answer(
        "✅ Название категории изменено.",
        reply_markup=admin_menu()
    )


@dp.message(EditCategoryState.description)
async def edit_category_description(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    data = await state.get_data()
    category_id = data["category_id"]
    value = (message.text or "").strip()
    if value == "/clear":
        value = ""

    cursor.execute(
        "UPDATE categories SET description = ? WHERE id = ?",
        (value, category_id)
    )
    db.commit()
    await state.clear()

    await message.answer(
        "✅ Текст категории изменён.",
        reply_markup=admin_menu()
    )


@dp.message(EditCategoryState.photo)
async def edit_category_photo(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    if not message.photo:
        await message.answer("❌ Отправьте именно фотографию.")
        return

    data = await state.get_data()
    category_id = data["category_id"]
    photo_id = message.photo[-1].file_id

    cursor.execute(
        "UPDATE categories SET photo_id = ? WHERE id = ?",
        (photo_id, category_id)
    )
    db.commit()
    await state.clear()

    await message.answer(
        "✅ Фото категории изменено.",
        reply_markup=admin_menu()
    )


@dp.callback_query(F.data.startswith("category_description_delete:"))
async def category_description_delete(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    category_id = int(callback.data.split(":")[1])
    cursor.execute(
        "UPDATE categories SET description = '' WHERE id = ?",
        (category_id,)
    )
    db.commit()

    await callback.message.answer(
        "✅ Описание категории удалено.",
        reply_markup=admin_menu()
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("category_photo_delete:"))
async def category_photo_delete(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    category_id = int(callback.data.split(":")[1])

    cursor.execute(
        "UPDATE categories SET photo_id = NULL WHERE id = ?",
        (category_id,)
    )
    db.commit()

    await callback.message.answer(
        "✅ Фото категории удалено.",
        reply_markup=admin_menu()
    )
    await callback.answer()


# ==================== ADMIN PRODUCT LIST ====================

@dp.message(F.text == "📋 Товары")
async def admin_products(message: Message):
    if await check_banned_user(message):
        return

    if message.from_user.id != ADMIN_ID:
        return

    cursor.execute(
        "SELECT COUNT(*) FROM products"
    )

    if cursor.fetchone()[0] == 0:
        await message.answer(
            "Товаров пока нет.",
            reply_markup=admin_menu(),
        )
        return

    await message.answer(
        "📋 <b>КАТАЛОГ ТОВАРОВ</b>\n\nВыберите позицию:",
        reply_markup=admin_products_keyboard(),
        parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith("manage_product:"))
async def manage_product(
    callback: CallbackQuery,
):
    if callback.from_user.id != ADMIN_ID:
        return

    product_id = int(
        callback.data.split(":")[1]
    )

    cursor.execute("""
        SELECT
            p.name,
            p.description,
            p.price,
            p.photo_id,
            p.stars_price,
            p.usdt_price,
            p.quantity_enabled,
            p.quantity_limit,
            c.name
        FROM products p
        JOIN categories c
        ON c.id = p.category_id
        WHERE p.id = ?
    """, (product_id,))

    product = cursor.fetchone()

    if not product:
        await callback.answer(
            "Товар не найден.",
            show_alert=True,
        )
        return

    name, desc, price, photo_id, stars, usdt, quantity_enabled, quantity_limit, category = product
    stock = available_stock(product_id)

    builder = InlineKeyboardBuilder()

    builder.button(text="✏️ Редактировать", callback_data=f"edit_product:{product_id}")
    builder.button(text="🎁 Добавить ключи", callback_data=f"stock_product:{product_id}")
    builder.button(text="🔢 Включить/изменить количество", callback_data=f"quantity_on:{product_id}")
    builder.button(text="🚫 Отключить ограничение количества", callback_data=f"quantity_off:{product_id}")
    builder.button(text="🗑 Удалить", callback_data=f"remove_product:{product_id}")

    builder.adjust(1)

    text = (
        "🛍 <b>ТОВАР</b>\n\n"
        f"📁 {esc(category)}\n"
        f"🛍 <b>{esc(name)}</b>\n\n"
        f"📝 {esc(desc)}\n\n"
        f"💰 {money(price)}\n"
        f"💵 {float(usdt or 0):.2f} USDT\n"
        f"🎁 Ключей: <b>{stock}</b>\n"
        f"🔢 Количество: <b>{'включено' if quantity_enabled else 'отключено'}</b>\n"
        f"📦 Лимит: <b>{int(quantity_limit or 1)}</b>"
    )

    if photo_id:
        await callback.message.answer_photo(
            photo=photo_id,
            caption=text,
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )
    else:
        await callback.message.answer(
            text,
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )

    await callback.answer()



class EditProductState(StatesGroup):
    value = State()
    photo = State()
    category = State()


def edit_product_keyboard(product_id):
    builder = InlineKeyboardBuilder()
    builder.button(text="📝 Название", callback_data=f"edit_field:name:{product_id}")
    builder.button(text="📄 Описание", callback_data=f"edit_field:description:{product_id}")
    builder.button(text="💰 Цена ₽", callback_data=f"edit_field:price:{product_id}")
    builder.button(text="💵 Цена USDT", callback_data=f"edit_field:usdt:{product_id}")
    builder.button(text="🖼 Фото", callback_data=f"edit_field:photo:{product_id}")
    builder.button(text="📁 Категория", callback_data=f"edit_field:category:{product_id}")
    builder.button(text="⬅️ Назад", callback_data=f"manage_product:{product_id}")
    builder.adjust(1)
    return builder.as_markup()


@dp.callback_query(F.data.startswith("edit_product:"))
async def edit_product_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return

    product_id = int(callback.data.split(":")[1])
    cursor.execute("SELECT name FROM products WHERE id = ?", (product_id,))
    row = cursor.fetchone()

    if not row:
        await callback.answer("Товар не найден.", show_alert=True)
        return

    await state.clear()
    await callback.message.answer(
        f"✏️ <b>Редактирование товара</b>\n\n"
        f"🛍 {esc(row[0])}\n\n"
        "Выберите, что изменить:",
        reply_markup=edit_product_keyboard(product_id),
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("edit_field:"))
async def edit_product_field(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return

    _, field, product_id = callback.data.split(":")
    product_id = int(product_id)

    cursor.execute("SELECT id FROM products WHERE id = ?", (product_id,))
    if not cursor.fetchone():
        await callback.answer("Товар не найден.", show_alert=True)
        return

    await state.update_data(product_id=product_id, field=field)

    if field == "category":
        await state.set_state(EditProductState.category)
        await callback.message.answer(
            "📁 Выберите новую категорию:",
            reply_markup=categories_keyboard("edit_product_category"),
        )
    elif field == "photo":
        await state.set_state(EditProductState.photo)
        await callback.message.answer("🖼 Отправьте новую фотографию товара.")
    else:
        await state.set_state(EditProductState.value)
        prompts = {
            "name": "📝 Введите новое название товара:",
            "description": "📄 Введите новое описание товара:",
            "price": "💰 Введите новую цену в рублях:",
            "usdt": "💵 Введите новую цену в USDT:",
        }
        await callback.message.answer(prompts[field])

    await callback.answer()


@dp.message(EditProductState.value)
async def edit_product_value(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    data = await state.get_data()
    product_id = data.get("product_id")
    field = data.get("field")
    value = (message.text or "").strip()

    if not product_id or not field:
        await state.clear()
        await message.answer("❌ Сессия редактирования потеряна.", reply_markup=admin_menu())
        return

    if not value:
        await message.answer("❌ Значение не может быть пустым.")
        return

    if field in ("price", "usdt"):
        try:
            number = float(value.replace(",", "."))
            if number < 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ Введите корректное число.")
            return

        column = "price" if field == "price" else "usdt_price"
        cursor.execute(f"UPDATE products SET {column} = ? WHERE id = ?", (number, product_id))
    elif field == "name":
        cursor.execute("UPDATE products SET name = ? WHERE id = ?", (value, product_id))
    elif field == "description":
        cursor.execute("UPDATE products SET description = ? WHERE id = ?", (value, product_id))
    else:
        await message.answer("❌ Неизвестное поле.")
        return

    db.commit()
    await state.clear()
    await message.answer("✅ Товар успешно обновлён.", reply_markup=admin_menu())


@dp.message(EditProductState.photo, F.photo)
async def edit_product_photo(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    data = await state.get_data()
    product_id = data.get("product_id")
    if not product_id:
        await state.clear()
        await message.answer("❌ Сессия редактирования потеряна.", reply_markup=admin_menu())
        return

    cursor.execute(
        "UPDATE products SET photo_id = ? WHERE id = ?",
        (message.photo[-1].file_id, product_id),
    )
    db.commit()
    await state.clear()
    await message.answer("✅ Фото товара обновлено.", reply_markup=admin_menu())


@dp.message(EditProductState.photo)
async def edit_product_photo_wrong(message: Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer("🖼 Отправьте именно фотографию товара.")


@dp.callback_query(F.data.startswith("edit_product_category:"))
async def edit_product_category(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return

    category_id = int(callback.data.split(":")[1])
    data = await state.get_data()
    product_id = data.get("product_id")

    if not product_id:
        await state.clear()
        await callback.answer("Сессия редактирования потеряна.", show_alert=True)
        return

    cursor.execute("SELECT name FROM categories WHERE id = ?", (category_id,))
    category = cursor.fetchone()
    if not category:
        await callback.answer("Категория не найдена.", show_alert=True)
        return

    cursor.execute(
        "UPDATE products SET category_id = ? WHERE id = ?",
        (category_id, product_id),
    )
    db.commit()
    await state.clear()

    await callback.message.answer(
        f"✅ Товар перемещён в категорию <b>{esc(category[0])}</b>.",
        reply_markup=admin_menu(),
        parse_mode="HTML",
    )
    await callback.answer()


class QuantityState(StatesGroup):
    limit = State()


@dp.callback_query(F.data.startswith("quantity_on:"))
async def quantity_on_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    product_id = int(callback.data.split(":")[1])
    await state.update_data(product_id=product_id)
    await state.set_state(QuantityState.limit)
    await callback.message.answer("🔢 Введите максимальное количество товара за один заказ, например <code>5</code>.", parse_mode="HTML")
    await callback.answer()


@dp.message(QuantityState.limit)
async def quantity_limit_save(message: Message, state: FSMContext):
    if await check_banned_user(message):
        return

    if message.from_user.id != ADMIN_ID:
        return
    try:
        limit = int(message.text.strip())
        if limit <= 0:
            raise ValueError
    except (ValueError, AttributeError):
        await message.answer("Введите целое число больше 0.")
        return
    data = await state.get_data()
    cursor.execute("UPDATE products SET quantity_enabled=1, quantity_limit=? WHERE id=?", (limit, data["product_id"]))
    db.commit()
    await state.clear()
    await message.answer(f"✅ Ограничение включено: максимум <b>{limit} шт.</b> за заказ.", reply_markup=admin_menu(), parse_mode="HTML")


@dp.callback_query(F.data.startswith("quantity_off:"))
async def quantity_off(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    product_id = int(callback.data.split(":")[1])
    cursor.execute("UPDATE products SET quantity_enabled=0, quantity_limit=1 WHERE id=?", (product_id,))
    db.commit()
    await callback.answer("Ограничение количества отключено.")
    await callback.message.answer("🚫 Ограничение количества отключено. За один заказ будет доступна 1 единица.")


@dp.callback_query(F.data.startswith("remove_product:"))
async def remove_product(
    callback: CallbackQuery,
):
    if callback.from_user.id != ADMIN_ID:
        return

    product_id = int(
        callback.data.split(":")[1]
    )

    cursor.execute(
        "SELECT name FROM products WHERE id = ?",
        (product_id,),
    )

    row = cursor.fetchone()

    if not row:
        await callback.answer(
            "Товар уже удалён.",
            show_alert=True,
        )
        return

    cursor.execute(
        "DELETE FROM products WHERE id = ?",
        (product_id,),
    )

    db.commit()

    await callback.message.answer(
        f"🗑 Товар <b>{esc(row[0])}</b> удалён.",
        reply_markup=admin_menu(),
        parse_mode="HTML",
    )

    await callback.answer()


# ==================== ADMIN STATS ====================



# ==================== ADMIN ЗАКАЗЫ ====================

@dp.message(F.text == "📦 Заказы")
async def admin_orders(message: Message):
    if await check_banned_user(message):
        return

    if message.from_user.id != ADMIN_ID:
        return

    cursor.execute("""
        SELECT
            id,
            customer_name,
            total,
            currency,
            total_stars,
            status
        FROM orders
        ORDER BY id DESC
        LIMIT 30
    """)

    orders = cursor.fetchall()

    if not orders:
        await message.answer(
            "Заказов пока нет.",
            reply_markup=admin_menu(),
        )
        return

    builder = InlineKeyboardBuilder()

    for order_id, name, total, currency, stars, status in orders:
        amount = format_price(total, currency or "RUB")
        builder.button(
            text=f"#{order_id} • {name or 'Без имени'} • {amount}",
            callback_data=f"admin_order:{order_id}",
        )

    builder.adjust(1)

    await message.answer(
        "📦 <b>ЗАКАЗЫ</b>",
        reply_markup=builder.as_markup(),
        parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith("admin_order:"))
async def admin_order(
    callback: CallbackQuery,
):
    if callback.from_user.id != ADMIN_ID:
        return

    order_id = int(
        callback.data.split(":")[1]
    )

    cursor.execute("""
        SELECT
            user_id,
            username,
            customer_name,
            total,
            currency,
            total_stars,
            status,
            payment_charge_id,
            created_at
        FROM orders
        WHERE id = ?
    """, (order_id,))

    order = cursor.fetchone()

    if not order:
        await callback.answer(
            "Заказ не найден.",
            show_alert=True,
        )
        return

    (
        user_id,
        username,
        customer_name,
        total,
        currency,
        stars,
        status,
        charge_id,
        created_at,
    ) = order

    cursor.execute("""
        SELECT
            product_name,
            quantity
        FROM order_items
        WHERE order_id = ?
    """, (order_id,))

    items = cursor.fetchall()

    text = (
        f"📦 <b>ЗАКАЗ #{order_id}</b>\n\n"
        f"👤 {esc(customer_name or 'Без имени')}\n"
        f"🔗 @{esc(username or 'нет')}\n"
        f"🆔 <code>{user_id}</code>\n"
        f"💰 <b>{format_price(total, currency or 'RUB')}</b>\n"
        + (f"⭐ <b>Telegram Stars: {int(stars):,}</b>\n" if stars else "")
        + f"📌 {esc(status)}\n"
        f"💳 <code>{esc(charge_id or 'нет')}</code>\n\n"
    )

    for name, qty in items:
        text += (
            f"🛍 {esc(name)} × {qty}\n"
        )

    builder = InlineKeyboardBuilder()
    if not order[6].startswith("🟢"):
        builder.button(text="✅ Подтвердить оплату", callback_data=f"confirm_payment:{order_id}")
        builder.button(text="❌ Отклонить", callback_data=f"reject_payment:{order_id}")
    if order[6] in ("🟢 Оплата подтверждена — выдача", "🟠 Оплата получена — ждёт подтверждения") or order[6].startswith("🟢"):
        builder.button(text="🎁 Выдать заказ вручную", callback_data=f"manual_deliver:{order_id}")
    builder.adjust(1)

    await callback.message.answer(
        text,
        parse_mode="HTML",
        reply_markup=builder.as_markup() if builder.buttons else None,
    )

    await callback.answer()


@dp.message(PaymentProofState.waiting_screenshot, F.photo)
async def payment_screenshot_received(message: Message, state: FSMContext):
    if await check_banned_user(message):
        return

    data = await state.get_data()
    order_id = data.get("payment_order_id")
    if not order_id:
        await state.clear()
        await message.answer("⚠️ Сессия оплаты истекла. Оформите заказ заново.")
        return

    cursor.execute(
        "SELECT user_id, total, currency, status FROM orders WHERE id=?",
        (order_id,),
    )
    order = cursor.fetchone()
    if not order or order[0] != message.from_user.id:
        await state.clear()
        await message.answer("⚠️ Заказ не найден. Оформите заказ заново.")
        return

    # Берём самое качественное фото из Telegram-альбома.
    file_id = message.photo[-1].file_id
    cursor.execute(
        "UPDATE orders SET payment_proof_file_id=?, status=? WHERE id=?",
        (file_id, "🟠 Скриншот оплаты получен", order_id),
    )
    db.commit()

    await message.answer(
        "📸 <b>Скриншот принят</b>\n\n"
        f"🧾 Заказ: <b>#{order_id}</b>\n"
        f"💰 Сумма: <b>{format_price(order[1], order[2])}</b>\n\n"
        "Теперь нажмите <b>«Я оплатил»</b>. Только после этого заявка уйдёт администратору.",
        reply_markup=paid_button(order_id),
        parse_mode="HTML",
    )

    # Фото уже сохранено в заказе; состояние можно оставить активным,
    # чтобы повторное фото обновляло доказательство оплаты до отправки заявки.


@dp.message(PaymentProofState.waiting_screenshot)
async def payment_screenshot_required(message: Message):
    await message.answer(
        "📸 <b>Нужен именно скриншот оплаты</b>\n\n"
        "Отправьте изображение фотографией в этот чат.\n"
        "После получения фото появится кнопка <b>«Я оплатил»</b>.",
        parse_mode="HTML",
    )

@dp.callback_query(F.data.startswith("paid_manual:"))
async def paid_manual(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split(":")[1])
    cursor.execute("SELECT user_id, total, currency, status, payment_proof_file_id FROM orders WHERE id=?", (order_id,))
    order = cursor.fetchone()
    if not order or order[0] != callback.from_user.id:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    if not order[4]:
        await callback.answer("Сначала отправьте скриншот оплаты.", show_alert=True)
        return
    if order[3] == "🟠 Пользователь сообщил об оплате":
        await callback.answer("Заявка уже отправлена администратору.", show_alert=True)
        return
    if order[3] != "🟠 Скриншот оплаты получен":
        await callback.answer("Сначала отправьте скриншот оплаты.", show_alert=True)
        return
    cursor.execute("UPDATE orders SET status='🟠 Пользователь сообщил об оплате' WHERE id=?", (order_id,))
    db.commit()
    await callback.message.answer("⏳ <b>Оплата отправлена на проверку</b>\n\nСкриншот передан администратору.", parse_mode="HTML")
    admin_builder = InlineKeyboardBuilder()
    admin_builder.button(text="✅ Подтвердить оплату", callback_data=f"confirm_payment:{order_id}")
    admin_builder.button(text="❌ Отклонить", callback_data=f"reject_payment:{order_id}")
    admin_builder.adjust(1)
    caption=("🔔 <b>ПОДТВЕРЖДЕНИЕ ОПЛАТЫ</b>\n\n"
              f"🧾 Заказ: <b>#{order_id}</b>\n"
              f"👤 ID: <code>{callback.from_user.id}</code>\n"
              f"💰 Сумма: <b>{format_price(order[1], order[2])}</b>\n\n"
              "📸 Скриншот оплаты приложен. Проверьте перевод.")
    await callback.bot.send_photo(ADMIN_ID, photo=order[4], caption=caption, reply_markup=admin_builder.as_markup(), parse_mode="HTML")
    await state.clear()
    await callback.answer("Отправлено администратору.")

@dp.callback_query(F.data.startswith("confirm_payment:"))
async def confirm_payment(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    order_id = int(callback.data.split(":")[1])
    cursor.execute("SELECT user_id FROM orders WHERE id=?", (order_id,))
    order = cursor.fetchone()
    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    cursor.execute("UPDATE orders SET paid=1, status='🟢 Оплата подтверждена — выдача' WHERE id=?", (order_id,))
    db.commit()
    await callback.bot.send_message(order[0], f"✅ <b>Оплата заказа #{order_id} подтверждена.</b>\n\nВаш заказ передан на выдачу.", parse_mode="HTML")
    await callback.answer("Оплата подтверждена.")

@dp.callback_query(F.data.startswith("reject_payment:"))
async def reject_payment(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    order_id = int(callback.data.split(":")[1])
    cursor.execute("SELECT user_id FROM orders WHERE id=?", (order_id,))
    order = cursor.fetchone()
    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    cursor.execute("UPDATE orders SET status='🔴 Оплата отклонена' WHERE id=?", (order_id,))
    db.commit()
    await callback.bot.send_message(order[0], f"❌ <b>Оплата заказа #{order_id} отклонена.</b>\n\nЕсли вы уверены, что оплатили, обратитесь в поддержку.", parse_mode="HTML")
    await callback.answer("Оплата отклонена.")


@dp.callback_query(F.data.startswith("manual_deliver:"))
async def manual_deliver_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return
    order_id = int(callback.data.split(":")[1])
    cursor.execute("SELECT user_id, status FROM orders WHERE id=?", (order_id,))
    order = cursor.fetchone()
    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    if not str(order[1]).startswith("🟢"):
        await callback.answer("Сначала подтвердите оплату.", show_alert=True)
        return
    await state.update_data(order_id=order_id, user_id=order[0])
    await state.set_state(ManualDeliveryState.text)
    await callback.message.answer(
        f"🎁 <b>ВЫДАЧА ЗАКАЗА #{order_id}</b>\n\n"
        "Отправьте данные заказа одним сообщением.\n"
        "Они будут отправлены покупателю.",
        parse_mode="HTML",
    )
    await callback.answer()


@dp.message(ManualDeliveryState.text)
async def manual_deliver_finish(message: Message, state: FSMContext):
    if await check_banned_user(message):
        return

    if message.from_user.id != ADMIN_ID:
        return
    if not message.text:
        await message.answer("Отправьте данные текстом.")
        return
    data = await state.get_data()
    order_id = int(data["order_id"])
    user_id = int(data["user_id"])
    cursor.execute("UPDATE orders SET status='🟢 Заказ выдан' WHERE id=?", (order_id,))
    db.commit()
    await state.clear()
    await message.answer(f"✅ Заказ #{order_id} выдан.", reply_markup=admin_menu())
    await message.bot.send_message(
        user_id,
        f"🎁 <b>ВАШ ЗАКАЗ #{order_id}</b>\n\n"
        f"<code>{esc(message.text)}</code>\n\n"
        "Спасибо за покупку в <b>Felecaster Shop</b> ❤️",
        parse_mode="HTML",
    )


# ==================== ADMIN CATEGORIES ====================



# ==================== FALLBACK ====================


# ==================== ADMIN BROADCAST ====================

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return

    cursor.execute("SELECT COUNT(*) FROM users")
    total = cursor.fetchone()[0]

    await state.clear()
    await state.set_state(BroadcastState.content)
    await callback.message.answer(
        "📢 <b>РАССЫЛКА</b>\n\n"
        f"👥 Получателей: <b>{total}</b>\n\n"
        "Отправьте текст, фото, видео или документ.\n"
        "После получения контента я предложу добавить кнопку.\n\n"
        "Для отмены: /cancel",
        parse_mode="HTML",
    )
    await callback.answer()


@dp.message(BroadcastState.content)
async def admin_broadcast_content(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    if message.text and message.text.strip() == "/cancel":
        await state.clear()
        await message.answer("❌ Рассылка отменена.", reply_markup=admin_menu())
        return

    if message.text is not None:
        data = {"kind": "text", "text": message.text}
    elif message.photo:
        data = {"kind": "photo", "file_id": message.photo[-1].file_id, "caption": message.caption or ""}
    elif message.video:
        data = {"kind": "video", "file_id": message.video.file_id, "caption": message.caption or ""}
    elif message.document:
        data = {"kind": "document", "file_id": message.document.file_id, "caption": message.caption or ""}
    else:
        await message.answer("❌ Поддерживаются текст, фото, видео и документы.")
        return

    await state.update_data(**data)
    await state.set_state(BroadcastState.button_text)

    kb = InlineKeyboardBuilder()
    kb.button(text="🛒 Купить", callback_data="broadcast_buy_button")
    kb.button(text="🎰 Крутить колесо", callback_data="broadcast_spin_button")
    kb.button(text="🔗 Своя кнопка", callback_data="broadcast_add_button")
    kb.button(text="🚫 Без кнопки", callback_data="broadcast_no_button")
    kb.adjust(1)

    await message.answer(
        "✅ Контент принят!\n\n"
        "Выберите кнопку для сообщения рассылки:",
        reply_markup=kb.as_markup(),
    )



@dp.callback_query(F.data == "broadcast_buy_button")
async def broadcast_buy_button(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return

    await state.update_data(button_mode="catalog")
    await callback.answer("🛒 Кнопка «Купить» добавлена")
    await admin_broadcast_execute(callback.message, state)


@dp.callback_query(F.data == "broadcast_spin_button")
async def broadcast_spin_button(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    await state.update_data(button_mode="spin")
    await callback.answer("🎰 Кнопка «Крутить колесо» добавлена")
    await admin_broadcast_execute(callback.message, state)


@dp.callback_query(F.data == "broadcast_no_button")
async def broadcast_no_button(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    await state.update_data(button_text="", button_url="")
    await callback.answer()
    await admin_broadcast_execute(callback.message, state)


@dp.callback_query(F.data == "broadcast_add_button")
async def broadcast_add_button(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer()
        return
    await state.set_state(BroadcastState.button_text)
    await callback.message.answer("🔘 Напишите текст кнопки:")
    await callback.answer()


@dp.message(BroadcastState.button_text)
async def admin_broadcast_button_text(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    if not message.text:
        await message.answer("❌ Отправьте текст кнопки.")
        return
    if message.text.strip() == "/cancel":
        await state.clear()
        await message.answer("❌ Рассылка отменена.", reply_markup=admin_menu())
        return

    value = message.text.strip()
    if len(value) > 64:
        await message.answer("❌ Максимум 64 символа.")
        return

    await state.update_data(button_text=value)
    await state.set_state(BroadcastState.button_url)
    await message.answer(
        "🔗 Отправьте URL кнопки:\n"
        "<code>https://t.me/your_bot</code>",
        parse_mode="HTML",
    )


@dp.message(BroadcastState.button_url)
async def admin_broadcast_button_url(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    if not message.text:
        await message.answer("❌ Отправьте ссылку.")
        return
    if message.text.strip() == "/cancel":
        await state.clear()
        await message.answer("❌ Рассылка отменена.", reply_markup=admin_menu())
        return

    url = message.text.strip()
    if not re.match(r"^https?://\S+$", url):
        await message.answer("❌ Ссылка должна начинаться с http:// или https://")
        return

    await state.update_data(button_url=url)
    await admin_broadcast_execute(message, state)


async def admin_broadcast_execute(message: Message, state: FSMContext):
    data = await state.get_data()

    markup = None

    # Built-in purchase button: opens the same catalog screen used by the bot.
    if data.get("button_mode") == "catalog":
        kb = InlineKeyboardBuilder()
        kb.button(text="🛒 Купить", callback_data="menu_catalog")
        kb.adjust(1)
        markup = kb.as_markup()

    # Spin button: opens the daily wheel.
    elif data.get("button_mode") == "spin":
        kb = InlineKeyboardBuilder()
        kb.button(text="🎰 Крутить колесо", callback_data="daily_spin")
        kb.adjust(1)
        markup = kb.as_markup()

    # Existing custom URL button remains available.
    elif data.get("button_text") and data.get("button_url"): 
        kb = InlineKeyboardBuilder()
        kb.button(text=data["button_text"], url=data["button_url"])
        kb.adjust(1)
        markup = kb.as_markup()

    cursor.execute("SELECT user_id FROM users")
    user_ids = [int(row[0]) for row in cursor.fetchall()]

    progress = await message.answer(
        f"📢 <b>Рассылка началась</b>\n\n👥 Получателей: <b>{len(user_ids)}</b>",
        parse_mode="HTML",
    )

    sent = 0
    failed = 0

    for user_id in user_ids:
        try:
            if data["kind"] == "text":
                await message.bot.send_message(user_id, data["text"], reply_markup=markup)
            elif data["kind"] == "photo":
                await message.bot.send_photo(
                    user_id, data["file_id"],
                    caption=data.get("caption") or None,
                    reply_markup=markup,
                )
            elif data["kind"] == "video":
                await message.bot.send_video(
                    user_id, data["file_id"],
                    caption=data.get("caption") or None,
                    reply_markup=markup,
                )
            elif data["kind"] == "document":
                await message.bot.send_document(
                    user_id, data["file_id"],
                    caption=data.get("caption") or None,
                    reply_markup=markup,
                )
            sent += 1
        except Exception:
            failed += 1

        await asyncio.sleep(0.05)

    await state.clear()

    await progress.edit_text(
        "✅ <b>РАССЫЛКА ЗАВЕРШЕНА</b>\n\n"
        f"👥 Всего: <b>{len(user_ids)}</b>\n"
        f"✅ Доставлено: <b>{sent}</b>\n"
        f"❌ Ошибок: <b>{failed}</b>",
        parse_mode="HTML",
    )
    await message.answer("Админ-панель:", reply_markup=admin_menu())




@dp.message()
async def fallback(message: Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer(
            "Выберите действие:",
            reply_markup=admin_menu(),
        )
    else:
        await message.answer(
            "Выберите раздел 👇",
            reply_markup=user_menu(
                message.from_user.id
            ),
        )

# ==================== RUN ====================

async def main():
    bot = Bot(token=BOT_TOKEN)
    hot_worker_task = asyncio.create_task(personal_hot_offer_worker(bot))
    crypto_worker_task = asyncio.create_task(crypto_payment_worker(bot))
    print("🚀 Felecaster Shop запущен!")
    try:
        await dp.start_polling(bot)
    finally:
        hot_worker_task.cancel()
        crypto_worker_task.cancel()
        for task in (hot_worker_task, crypto_worker_task):
            try:
                await task
            except asyncio.CancelledError:
                pass
        await bot.session.close()
        db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Бот остановлен пользователем.")