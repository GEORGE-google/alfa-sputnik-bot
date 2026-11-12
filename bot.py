import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta
import aiohttp
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler

load_dotenv()
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY", "")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID", "")
YANDEX_MODEL = os.getenv("YANDEX_MODEL", "yandexgpt-lite")
YANDEX_API_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

# Хранилище контекста меню для навигации цифрами и ввода своего фокуса
USER_MENU_STATE = {}       # user_id -> dict {"1": ("action", "value"), ...}
AWAITING_CUSTOM_GOAL = set()  # user_id пользователей, которые вводят свой фокус текстом

# --- БАЗА ДАННЫХ (Профили и Напоминания) ---
DB_PATH = "sputnik.db"

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                stage TEXT DEFAULT 'Не указана',
                tax_regime TEXT DEFAULT 'Не указан',
                goal TEXT DEFAULT 'Не указана'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                title TEXT,
                frequency TEXT, -- 'once', 'daily', 'every_3_days', 'weekly', 'off'
                active INTEGER DEFAULT 1
            )
        """)

def get_user(user_id: int) -> dict:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("SELECT stage, tax_regime, goal FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if not row:
            conn.execute("INSERT INTO users (user_id) VALUES (?)", (user_id,))
            return {"stage": "Не указана", "tax_regime": "Не указан", "goal": "Не указана"}
        return {"stage": row[0], "tax_regime": row[1], "goal": row[2]}

def update_user(user_id: int, field: str, value: str):
    get_user(user_id)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(f"UPDATE users SET {field} = ? WHERE user_id = ?", (value, user_id))

# --- ЗАГРУЗКА КАТАЛОГА И ЗАЩИТА ПЛАТНОГО КОНТЕНТА (Ограничение 06) ---
with open("sample_catalog.json", "r", encoding="utf-8") as f:
    CATALOG = json.load(f)

def build_safe_context() -> str:
    """Формирует контекст для YandexGPT: для платных курсов скрывает full_text!"""
    blocks = []
    for item in CATALOG:
        if item["is_paid"]:
            blocks.append(
                f"[ПЛАТНЫЙ КУРС] ID: {item['id']} | Название: {item['title']} | "
                f"Для кого: {item['target_stage']} | Краткая польза: {item['summary']} | Ссылка: {item['url']}"
            )
        else:
            blocks.append(
                f"[БЕСПЛАТНЫЙ МАТЕРИАЛ] ID: {item['id']} | Название: {item['title']} | "
                f"Для кого: {item['target_stage']} | Содержание: {item['full_text']} | Ссылка: {item['url']}"
            )
    return "\n\n".join(blocks)

# --- КЛИЕНТ YANDEX GPT ---
async def ask_yandex_gpt(system_prompt: str, user_text: str) -> str | None:
    if not YANDEX_API_KEY or not YANDEX_FOLDER_ID:
        return None
    if YANDEX_API_KEY.startswith("your_") or YANDEX_API_KEY.startswith("insert_"):
        return None

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Api-Key {YANDEX_API_KEY}",
        "x-folder-id": YANDEX_FOLDER_ID
    }
    payload = {
        "modelUri": f"gpt://{YANDEX_FOLDER_ID}/{YANDEX_MODEL}/latest",
        "completionOptions": {
            "stream": False,
            "temperature": 0.3,
            "maxTokens": "1000"
        },
        "messages": [
            {"role": "system", "text": system_prompt},
            {"role": "user", "text": user_text}
        ]
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(YANDEX_API_URL, headers=headers, json=payload, timeout=20) as resp:
                if resp.status != 200:
                    err_text = await resp.text()
                    logging.error(f"YandexGPT API Error ({resp.status}): {err_text}")
                    return None
                data = await resp.json()
                return data["result"]["alternatives"][0]["message"]["text"]
    except Exception as e:
        logging.error(f"Ошибка при запросе к YandexGPT: {e}")
        return None

# --- АВТОМАТИЧЕСКОЕ ОБНОВЛЕНИЕ ПРОФИЛЯ ИЗ ДИАЛОГА ---
def auto_extract_profile(user_id: int, text: str):
    t = text.lower()
    # Налоги
    if any(w in t for w in ["усн", "упрощенк"]):
        update_user(user_id, "tax_regime", "УСН")
    elif any(w in t for w in ["самозанят", "нпд"]):
        update_user(user_id, "tax_regime", "НПД (Самозанятый)")
    elif "патент" in t or "псн" in t:
        update_user(user_id, "tax_regime", "Патент (ПСН)")
    elif "осно" in t or "ндс" in t:
        update_user(user_id, "tax_regime", "ОСНО")

    # Стадия бизнеса
    if any(w in t for w in ["открыть", "начать", "идея", "регистрац"]):
        update_user(user_id, "stage", "Планирую открыть")
    elif any(w in t for w in ["масштаб", "отдел продаж", "найм", "систем"]):
        update_user(user_id, "stage", "Развиваю бизнес")

    # Текущий фокус (goal)
    if any(w in t for w in ["маркетплейс", "wb", "ozon", "юнит"]):
        update_user(user_id, "goal", "Выход на маркетплейсы и продажи")
    elif any(w in t for w in ["налог", "декларац", "отчетност", "бухгалтер"]):
        update_user(user_id, "goal", "Налоги, учет и отчетность")
    elif any(w in t for w in ["маркетинг", "реклам", "клиент", "продвиж"]):
        update_user(user_id, "goal", "Маркетинг и привлечение клиентов")
    elif any(w in t for w in ["команд", "менеджер", "найм", "crm"]):
        update_user(user_id, "goal", "Систематизация и найм команды")

# --- МЕНЮ И КЛАВИАТУРЫ С ПОДДЕРЖКОЙ ЦИФР ---
def set_main_menu_state(user_id: int):
    USER_MENU_STATE[user_id] = {
        "1": ("stage", "Планирую открыть"),
        "2": ("stage", "Действующий микробизнес"),
        "3": ("stage", "Развиваю бизнес"),
        "4": ("nav", "profile"),
        "5": ("nav", "reminders"),
        "6": ("nav", "help")
    }

def start_kb(user_id: int):
    set_main_menu_state(user_id)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1. 🚀 Планирую открыть дело", callback_data="stage_Планирую открыть")],
        [InlineKeyboardButton(text="2. 🛠 Решаю задачу в микробизнесе", callback_data="stage_Действующий микробизнес")],
        [InlineKeyboardButton(text="3. 📈 Хочу системно развить бизнес", callback_data="stage_Развиваю бизнес")],
        [
            InlineKeyboardButton(text="4. 📋 Мой профиль", callback_data="nav_profile"),
            InlineKeyboardButton(text="5. ⏰ Напоминания", callback_data="nav_reminders")
        ],
        [InlineKeyboardButton(text="6. ❓ Помощь и все функции", callback_data="nav_help")]
    ])

def profile_kb(user_id: int):
    USER_MENU_STATE[user_id] = {
        "1": ("submenu", "goal"),
        "2": ("submenu", "tax"),
        "3": ("submenu", "stage"),
        "4": ("nav", "start")
    }
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1. 🎯 Изменить текущий фокус", callback_data="submenu_goal")],
        [InlineKeyboardButton(text="2. 💼 Изменить налоговый режим", callback_data="submenu_tax")],
        [InlineKeyboardButton(text="3. 🚀 Изменить стадию бизнеса", callback_data="submenu_stage")],
        [InlineKeyboardButton(text="4. 🏠 В главное меню", callback_data="nav_start")]
    ])

def goal_selection_kb(user_id: int):
    USER_MENU_STATE[user_id] = {
        "1": ("goal", "Запуск бизнеса и регистрация"),
        "2": ("goal", "Налоги, учет и отчетность"),
        "3": ("goal", "Маркетинг и привлечение клиентов"),
        "4": ("goal", "Выход на маркетплейсы и продажи"),
        "5": ("goal", "Систематизация и найм команды"),
        "6": ("goal_custom", "custom"),
        "7": ("nav", "profile")
    }
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1. 🚀 Запуск бизнеса и регистрация", callback_data="goal_Запуск бизнеса и регистрация")],
        [InlineKeyboardButton(text="2. 📊 Налоги, учет и отчетность", callback_data="goal_Налоги, учет и отчетность")],
        [InlineKeyboardButton(text="3. 📣 Маркетинг и привлечение клиентов", callback_data="goal_Маркетинг и привлечение клиентов")],
        [InlineKeyboardButton(text="4. 🛍 Выход на маркетплейсы и продажи", callback_data="goal_Выход на маркетплейсы и продажи")],
        [InlineKeyboardButton(text="5. 👥 Систематизация и найм команды", callback_data="goal_Систематизация и найм команды")],
        [InlineKeyboardButton(text="6. ✍️ Ввести свой фокус текстом", callback_data="goal_custom")],
        [InlineKeyboardButton(text="7. ⬅️ Назад в профиль", callback_data="nav_profile")]
    ])

def tax_selection_kb(user_id: int):
    USER_MENU_STATE[user_id] = {
        "1": ("tax", "УСН"),
        "2": ("tax", "НПД (Самозанятый)"),
        "3": ("tax", "Патент (ПСН)"),
        "4": ("tax", "ОСНО"),
        "5": ("nav", "profile")
    }
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="1. УСН", callback_data="tax_УСН"),
            InlineKeyboardButton(text="2. НПД (Самозанятый)", callback_data="tax_НПД (Самозанятый)")
        ],
        [
            InlineKeyboardButton(text="3. Патент (ПСН)", callback_data="tax_Патент (ПСН)"),
            InlineKeyboardButton(text="4. ОСНО", callback_data="tax_ОСНО")
        ],
        [InlineKeyboardButton(text="5. ⬅️ Назад в профиль", callback_data="nav_profile")]
    ])

def stage_selection_kb(user_id: int):
    USER_MENU_STATE[user_id] = {
        "1": ("stage", "Планирую открыть"),
        "2": ("stage", "Действующий микробизнес"),
        "3": ("stage", "Развиваю бизнес"),
        "4": ("nav", "profile")
    }
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1. 🚀 Планирую открыть", callback_data="stage_Планирую открыть")],
        [InlineKeyboardButton(text="2. 🛠 Действующий микробизнес", callback_data="stage_Действующий микробизнес")],
        [InlineKeyboardButton(text="3. 📈 Развиваю бизнес", callback_data="stage_Развиваю бизнес")],
        [InlineKeyboardButton(text="4. ⬅️ Назад в профиль", callback_data="nav_profile")]
    ])

def reminder_control_kb(rem_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Раз в день", callback_data=f"freq_{rem_id}_daily"),
            InlineKeyboardButton(text="Раз в 3 дня", callback_data=f"freq_{rem_id}_every_3_days"),
            InlineKeyboardButton(text="Раз в неделю", callback_data=f"freq_{rem_id}_weekly")
        ],
        [InlineKeyboardButton(text="🔕 Отключить напоминание", callback_data=f"freq_{rem_id}_off")]
    ])

# --- ОСНОВНЫЕ ЭКРАНЫ БОТА ---
async def send_start_screen(msg: Message, user_id: int):
    AWAITING_CUSTOM_GOAL.discard(user_id)
    get_user(user_id)
    await msg.answer(
        "Привет! Я **Альфа-Спутник** — ваш ИИ-помощник по платформе «Альфа-Курс» (на базе YandexGPT).\n\n"
        "Помогу быстро найти ответ по базе знаний, подберу бесплатные шаблоны или курсы под ваш уровень "
        "и вовремя напомню об учебе или уплате налогов.\n\n"
        "👇 **Нажмите кнопку или отправьте цифру (1–6) в чат**, либо сразу напишите свой вопрос:",
        reply_markup=start_kb(user_id),
        parse_mode="Markdown"
    )

async def send_profile_screen(msg: Message, user_id: int):
    AWAITING_CUSTOM_GOAL.discard(user_id)
    u = get_user(user_id)
    await msg.answer(
        f"📋 **Ваш профиль бизнеса:**\n"
        f"• **Стадия:** {u['stage']}\n"
        f"• **Налоговый режим:** {u['tax_regime']}\n"
        f"• **Текущий фокус:** {u['goal']}\n\n"
        f"_Я учитываю эти данные при подборе курсов и расчете дат налогов. "
        f"Выберите, что хотите изменить (кнопкой или цифрой 1–4 в чат):_",
        reply_markup=profile_kb(user_id),
        parse_mode="Markdown"
    )

async def send_help_screen(msg: Message, user_id: int):
    AWAITING_CUSTOM_GOAL.discard(user_id)
    USER_MENU_STATE[user_id] = {
        "1": ("nav", "profile"),
        "2": ("nav", "reminders"),
        "3": ("nav", "demo_push"),
        "4": ("nav", "start")
    }
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1. 📋 Открыть мой профиль", callback_data="nav_profile")],
        [InlineKeyboardButton(text="2. ⏰ Управление напоминаниями", callback_data="nav_reminders")],
        [InlineKeyboardButton(text="3. ⚡ Тест пуш-уведомления (10 сек)", callback_data="nav_demo_push")],
        [InlineKeyboardButton(text="4. 🏠 В главное меню", callback_data="nav_start")]
    ])
    await msg.answer(
        "❓ **Справка по всем функциям «Альфа-Спутника»**\n\n"
        "🔹 **1. Умные ответы по базе «Альфа-Курса» (YandexGPT)**\n"
        "Просто напишите свой вопрос в чат (например: *«Какой налоговый режим выбрать для ИП?»* или *«Как посчитать юнит-экономику на маркетплейсах?»*). "
        "Я дам ответ и порекомендую бесплатные материалы или профильные платные курсы.\n\n"
        "🔹 **2. Персонализация и Профиль (`/profile`)**\n"
        "Я запоминаю вашу стадию бизнеса, налоговый режим и **текущий фокус** прямо из диалога или через меню профиля.\n\n"
        "🔹 **3. Напоминания и налоговый календарь (`/reminders`)**\n"
        "Помогаю не забывать про обучение и сроки уплаты налогов. Можно настроить частоту (раз в день / раз в 3 дня / раз в неделю) или отключить их.\n\n"
        "🔹 **4. Демо-пуш для быстрой проверки (`/demo_push`)**\n"
        "Отправляет тестовое напоминание ровно через 10 секунд со ссылкой на платформу.\n\n"
        "🔢 **Быстрое управление цифрами:**\n"
        "В любом меню вы можете не только нажимать на кнопки, но и просто **отправить цифру нужного пункта (1, 2, 3...)** в чат!\n\n"
        "👇 *Выберите действие кнопкой или отправьте цифру 1–4:*",
        reply_markup=kb,
        parse_mode="Markdown"
    )

# --- ОБРАБОТЧИКИ КОМАНД ---
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await send_start_screen(message, message.from_user.id)

@dp.message(Command("profile"))
async def cmd_profile(message: Message):
    await send_profile_screen(message, message.from_user.id)

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await send_help_screen(message, message.from_user.id)

# --- ОБРАБОТЧИКИ НАВИГАЦИИ И НАСТРОЕК ПРОФИЛЯ ---
@dp.callback_query(F.data.startswith("nav_"))
async def cb_navigation(call: CallbackQuery):
    target = call.data.replace("nav_", "")
    uid = call.from_user.id
    if target == "start":
        await send_start_screen(call.message, uid)
    elif target == "profile":
        await send_profile_screen(call.message, uid)
    elif target == "help":
        await send_help_screen(call.message, uid)
    elif target == "reminders":
        await show_reminders(call)
    elif target == "demo_push":
        await trigger_demo_push(call.message, uid)
    await call.answer()

@dp.callback_query(F.data.startswith("submenu_"))
async def cb_submenu(call: CallbackQuery):
    sub = call.data.replace("submenu_", "")
    uid = call.from_user.id
    if sub == "goal":
        await call.message.answer(
            "🎯 **Выберите ваш текущий фокус** (кнопкой или отправьте цифру **1–7** в чат):",
            reply_markup=goal_selection_kb(uid),
            parse_mode="Markdown"
        )
    elif sub == "tax":
        await call.message.answer(
            "💼 **Выберите налоговый режим** (кнопкой или отправьте цифру **1–5** в чат):",
            reply_markup=tax_selection_kb(uid),
            parse_mode="Markdown"
        )
    elif sub == "stage":
        await call.message.answer(
            "🚀 **Выберите стадию вашего бизнеса** (кнопкой или отправьте цифру **1–4** в чат):",
            reply_markup=stage_selection_kb(uid),
            parse_mode="Markdown"
        )
    await call.answer()

@dp.callback_query(F.data.startswith("stage_"))
async def cb_set_stage(call: CallbackQuery):
    stage = call.data.replace("stage_", "")
    uid = call.from_user.id
    update_user(uid, "stage", stage)
    await call.message.answer(
        f"✅ Запомнил вашу стадию: **{stage}**.\n\n"
        "Какой вопрос сейчас самый актуальный? Например:\n"
        "• *Какой налоговый режим выбрать для ИП?*\n"
        "• *Как рассчитать юнит-экономику на маркетплейсах?*\n"
        "• *Напомни мне про уплату налога по УСН*",
        parse_mode="Markdown"
    )
    await call.answer()

@dp.callback_query(F.data.startswith("tax_"))
async def cb_set_tax(call: CallbackQuery):
    tax = call.data.replace("tax_", "")
    uid = call.from_user.id
    update_user(uid, "tax_regime", tax)
    await call.message.answer(
        f"✅ Налоговый режим обновлен на **{tax}**. Теперь я смогу точно подсказывать даты отчетности и уплаты!",
        parse_mode="Markdown"
    )
    await send_profile_screen(call.message, uid)
    await call.answer()

@dp.callback_query(F.data == "goal_custom")
async def cb_goal_custom(call: CallbackQuery):
    uid = call.from_user.id
    AWAITING_CUSTOM_GOAL.add(uid)
    await call.message.answer(
        "✍️ **Напишите в чат вашу текущую цель или задачу** (например: *«Открыть кофейню»* или *«Снизить расходы на логистику»*), и я сохраню её в ваш профиль:",
        parse_mode="Markdown"
    )
    await call.answer()

@dp.callback_query(F.data.startswith("goal_"))
async def cb_set_goal(call: CallbackQuery):
    goal = call.data.replace("goal_", "")
    uid = call.from_user.id
    AWAITING_CUSTOM_GOAL.discard(uid)
    update_user(uid, "goal", goal)
    await call.message.answer(
        f"✅ Текущий фокус обновлен: **{goal}**!\n"
        "Теперь рекомендации материалов будут в первую очередь опираться на эту задачу.",
        parse_mode="Markdown"
    )
    await send_profile_screen(call.message, uid)
    await call.answer()

# --- СИСТЕМА НАПОМИНАНИЙ ---
async def send_scheduled_reminder(user_id: int, text: str):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📚 Перейти на Альфа-Курс",
            url="https://kurs.alfabank.ru/catalog?type=courses&utm_source=tg_bot&utm_medium=reminder"
        )]
    ])
    await bot.send_message(
        user_id,
        f"🔔 **Напоминание от Альфа-Спутника:**\n{text}",
        reply_markup=kb,
        parse_mode="Markdown"
    )

async def trigger_demo_push(msg: Message, user_id: int):
    u = get_user(user_id)
    run_date = datetime.now() + timedelta(seconds=10)
    scheduler.add_job(
        send_scheduled_reminder,
        "date",
        run_date=run_date,
        args=[
            user_id,
            f"Пора уделить 15 минут обучению по теме «{u['goal']}»! "
            f"Также проверьте календарь отчетности ({u['tax_regime']})."
        ]
    )
    await msg.answer(
        "⚡ **Демо-режим:** Разовое напоминание запланировано и придет ровно через **10 секунд**!",
        parse_mode="Markdown"
    )

@dp.message(Command("demo_push"))
async def cmd_demo_push(message: Message):
    await trigger_demo_push(message, message.from_user.id)

@dp.message(Command("reminders"))
@dp.callback_query(F.data == "menu_reminders")
async def show_reminders(event: Message | CallbackQuery):
    user_id = event.from_user.id
    u = get_user(user_id)
    msg = event.message if isinstance(event, CallbackQuery) else event

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("SELECT id, title, frequency, active FROM reminders WHERE user_id = ?", (user_id,))
        rows = cur.fetchall()

    if not rows:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO reminders (user_id, title, frequency) VALUES (?, ?, ?)",
                (user_id, "Продолжить обучение на «Альфа-Курсе» (15 мин)", "every_3_days")
            )
            conn.execute(
                "INSERT INTO reminders (user_id, title, frequency) VALUES (?, ?, ?)",
                (user_id, f"Проверка сроков уплаты налога ({u['tax_regime']} — до 28 числа)", "weekly")
            )
            cur = conn.execute("SELECT id, title, frequency, active FROM reminders WHERE user_id = ?", (user_id,))
            rows = cur.fetchall()

    freq_map = {
        "once": "Разовое",
        "daily": "Раз в день",
        "every_3_days": "Раз в 3 дня",
        "weekly": "Раз в неделю",
        "off": "Отключено"
    }
    for r_id, title, freq, active in rows:
        status = "🟢 Активно" if active and freq != "off" else "🔴 Отключено"
        await msg.answer(
            f"⏰ **{title}**\nСтатус: {status} ({freq_map.get(freq, freq)})",
            reply_markup=reminder_control_kb(r_id),
            parse_mode="Markdown"
        )
    if isinstance(event, CallbackQuery):
        await event.answer()

@dp.callback_query(F.data.startswith("freq_"))
async def cb_change_freq(call: CallbackQuery):
    _, rem_id, new_freq = call.data.split("_", 2)
    active = 0 if new_freq == "off" else 1
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE reminders SET frequency = ?, active = ? WHERE id = ?", (new_freq, active, int(rem_id)))
    freq_map = {"daily": "Раз в день", "every_3_days": "Раз в 3 дня", "weekly": "Раз в неделю", "off": "Отключено"}
    await call.message.edit_text(
        f"✅ Настройки напоминания #{rem_id} обновлены!\nНовая частота: **{freq_map.get(new_freq)}**.",
        parse_mode="Markdown"
    )
    await call.answer("Частота обновлена!")

# --- ОБРАБОТКА ВВОДА ЦИФР И ВОПРОСОВ В ЧАТЕ ---
async def execute_numbered_action(message: Message, action_type: str, value: str):
    uid = message.from_user.id
    if action_type == "stage":
        update_user(uid, "stage", value)
        await message.answer(
            f"✅ Выбран пункт: **{value}** (стадия сохранена в профиль).\n"
            "Напишите ваш вопрос или отправьте `/profile` для настройки фокуса.",
            parse_mode="Markdown"
        )
    elif action_type == "tax":
        update_user(uid, "tax_regime", value)
        await message.answer(f"✅ Налоговый режим обновлен на **{value}**!", parse_mode="Markdown")
        await send_profile_screen(message, uid)
    elif action_type == "goal":
        update_user(uid, "goal", value)
        await message.answer(f"✅ Текущий фокус обновлен: **{value}**!", parse_mode="Markdown")
        await send_profile_screen(message, uid)
    elif action_type == "goal_custom":
        AWAITING_CUSTOM_GOAL.add(uid)
        await message.answer("✍️ Напишите в чат вашу текущую цель или задачу для бизнеса:")
    elif action_type == "submenu":
        if value == "goal":
            await message.answer(
                "🎯 **Выберите ваш текущий фокус** (кнопкой или цифрой **1–7** в чат):",
                reply_markup=goal_selection_kb(uid),
                parse_mode="Markdown"
            )
        elif value == "tax":
            await message.answer(
                "💼 **Выберите налоговый режим** (кнопкой или цифрой **1–5** в чат):",
                reply_markup=tax_selection_kb(uid),
                parse_mode="Markdown"
            )
        elif value == "stage":
            await message.answer(
                "🚀 **Выберите стадию вашего бизнеса** (кнопкой или цифрой **1–4** в чат):",
                reply_markup=stage_selection_kb(uid),
                parse_mode="Markdown"
            )
    elif action_type == "nav":
        if value == "start":
            await send_start_screen(message, uid)
        elif value == "profile":
            await send_profile_screen(message, uid)
        elif value == "help":
            await send_help_screen(message, uid)
        elif value == "reminders":
            await show_reminders(message)
        elif value == "demo_push":
            await trigger_demo_push(message, uid)

@dp.message()
async def handle_question(message: Message):
    user_id = message.from_user.id
    text = (message.text or "").strip()
    if not text:
        return

    # 0. Если пользователь вводит собственный «Текущий фокус» текстом
    if user_id in AWAITING_CUSTOM_GOAL and not text.isdigit():
        AWAITING_CUSTOM_GOAL.discard(user_id)
        update_user(user_id, "goal", text[:80])
        await message.answer(f"✅ Ваш персональный фокус сохранен: **{text[:80]}**!", parse_mode="Markdown")
        await send_profile_screen(message, user_id)
        return

    # 1. Навигация по боту при помощи цифр (1, 2, 3...)
    if text.isdigit():
        if user_id not in USER_MENU_STATE:
            set_main_menu_state(user_id)
        menu_map = USER_MENU_STATE.get(user_id, {})
        if text in menu_map:
            action_type, val = menu_map[text]
            await execute_numbered_action(message, action_type, val)
            return
        else:
            valid_keys = ", ".join(menu_map.keys())
            await message.answer(
                f"🔢 В текущем меню доступны цифры: **{valid_keys}**.\n"
                f"Отправьте одну из них или нажмите `/start` для главного меню.",
                parse_mode="Markdown"
            )
            return

    # 2. Автоматическое обогащение профиля из текста вопроса
    auto_extract_profile(user_id, text)
    profile = get_user(user_id)

    # 3. Перехват запроса на создание напоминания
    if any(w in text.lower() for w in ["напомни", "напоминание", "срок уплаты"]):
        if profile["tax_regime"] == "Не указан" and "налог" in text.lower():
            USER_MENU_STATE[user_id] = {
                "1": ("tax", "УСН"),
                "2": ("tax", "НПД (Самозанятый)"),
                "3": ("tax", "Патент (ПСН)")
            }
            await message.answer(
                "Чтобы я настроил точное напоминание о сроках уплаты налогов, уточните ваш режим налогообложения "
                "(нажмите кнопку или отправьте цифру **1–3** в чат):",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(text="1. УСН (до 28 числа)", callback_data="tax_УСН"),
                        InlineKeyboardButton(text="2. НПД (до 28 числа)", callback_data="tax_НПД (Самозанятый)")
                    ],
                    [InlineKeyboardButton(text="3. Патент (ПСН)", callback_data="tax_Патент (ПСН)")]
                ]),
                parse_mode="Markdown"
            )
            return
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO reminders (user_id, title, frequency) VALUES (?, ?, ?)",
                (user_id, f"Задача из чата: {text[:50]}", "once")
            )
        await message.answer(
            f"✅ Я создал напоминание по вашему запросу (Режим: **{profile['tax_regime']}**).\n"
            "Управлять частотой или отключить его можно через `/reminders`, "
            "а проверить мгновенную отправку — через `/demo_push`.",
            parse_mode="Markdown"
        )
        return

    # 4. Обращение к YandexGPT (если ключи подключены в .env)
    system_prompt = (
        "Ты — ИИ-помощник образовательной платформы «Альфа-Курс» в Telegram.\n"
        f"Профиль предпринимателя: Стадия={profile['stage']}, Налоги={profile['tax_regime']}, Текущий фокус={profile['goal']}.\n"
        "ПРАВИЛА:\n"
        "1. Отвечай ТОЛЬКО на основе предоставленной ниже базы знаний.\n"
        "2. Если вопрос слишком широкий или неясный — задай 1-2 конкретных уточняющих вопроса.\n"
        "3. Если ответа в базе нет — честно скажи об этом и предложи ближайшую тему из каталога.\n"
        "4. СТРОГОЕ ОГРАНИЧЕНИЕ: Для ПЛАТНЫХ курсов никогда не выдумывай и не раскрывай внутреннее содержание — "
        "кратко объясни, почему этот курс полезен пользователю на его стадии бизнеса и под его текущий фокус, и дай ссылку.\n"
        "5. В конце ответа всегда предлагай понятный следующий шаг.\n\n"
        f"БАЗА ЗНАНИЙ:\n{build_safe_context()}"
    )

    yandex_answer = await ask_yandex_gpt(system_prompt, text)
    if yandex_answer:
        await message.answer(yandex_answer, parse_mode="Markdown")
        return

    # 5. Fallback-режим (срабатывает автоматически, если ключи YandexGPT не указаны)
    t_low = text.lower()
    if len(t_low.split()) <= 2 and t_low not in ["усн", "налоги"]:
        USER_MENU_STATE[user_id] = {
            "1": ("stage", "Планирую открыть"),
            "2": ("stage", "Действующий микробизнес"),
            "3": ("stage", "Развиваю бизнес")
        }
        await message.answer(
            "Ваш запрос звучит довольно широко. Уточните, пожалуйста, вашу ситуацию "
            "(нажмите кнопку, отправьте цифру **1–3** или распишите вопрос подробнее):\n"
            "1. Только планирую запуск бизнеса\n"
            "2. Ищу быстрый ответ или шаблон для микробизнеса\n"
            "3. Хочу системно перестроить процессы и продажи",
            reply_markup=stage_selection_kb(user_id),
            parse_mode="Markdown"
        )
    elif any(w in t_low for w in ["налог", "усн", "ип", "самозанят", "режим"]):
        m = CATALOG[0]
        await message.answer(
            f"💡 **Ответ по базе «Альфа-Курса» (Стадия: {profile['stage']}, Фокус: {profile['goal']}):**\n"
            f"{m['full_text']}\n\n"
            f"📌 **Рекомендуемый бесплатный материал:** [{m['title']}]({m['url']})\n"
            f"*Почему подходит:* поможет за 10 минут сверить лимиты и не переплачивать на старте.\n\n"
            f"➡️ **Следующий шаг:** изучите чек-лист по ссылке или нажмите `/demo_push`, чтобы я напомнил вам о сроках уплаты.",
            parse_mode="Markdown"
        )
    elif any(w in t_low for w in ["маркетплейс", "продаж", "юнит", "wb", "ozon"]):
        m = CATALOG[1]
        await message.answer(
            f"По теме маркетплейсов и расчета прибыльности в каталоге есть профильная программа (под ваш фокус: **{profile['goal']}**).\n\n"
            f"🎓 **Платный курс:** [{m['title']}]({m['url']})\n"
            f"*Краткая польза (почему рекомендую):* {m['summary']} "
            f"Подробные материалы и расчетные таблицы доступны после приобретения курса на платформе.\n\n"
            f"➡️ **Следующий шаг:** перейдите на страницу курса на «Альфа-Курсе», чтобы ознакомиться с программой.",
            parse_mode="Markdown"
        )
    else:
        await message.answer(
            "В базе знаний «Альфа-Курса» пока нет точного ответа по этому узкому запросу.\n\n"
            "Могу предложить вам материалы по смежным темам:\n"
            f"• Бесплатно: [{CATALOG[0]['title']}]({CATALOG[0]['url']})\n"
            f"• Углубленный курс: [{CATALOG[1]['title']}]({CATALOG[1]['url']})",
            parse_mode="Markdown"
        )

async def main():
    init_db()
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
