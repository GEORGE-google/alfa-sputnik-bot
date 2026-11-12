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
YANDEX_MODEL = os.getenv("YANDEX_MODEL", "yandexgpt")
YANDEX_API_URL = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

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
    """Отправляет запрос в YandexGPT API и возвращает текст ответа."""
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

# --- СБОР ПРОФИЛЯ ИЗ ТЕКСТА ДИАЛОГА ---
def auto_extract_profile(user_id: int, text: str):
    t = text.lower()
    if any(w in t for w in ["усн", "упрощенк"]):
        update_user(user_id, "tax_regime", "УСН")
    elif any(w in t for w in ["самозанят", "нпд"]):
        update_user(user_id, "tax_regime", "НПД (Самозанятый)")
    elif "патент" in t or "псн" in t:
        update_user(user_id, "tax_regime", "Патент (ПСН)")
    elif "осно" in t or "ндс" in t:
        update_user(user_id, "tax_regime", "ОСНО")

    if any(w in t for w in ["открыть", "начать", "идея", "регистрац"]):
        update_user(user_id, "stage", "Планирую открыть бизнес")
    elif any(w in t for w in ["масштаб", "отдел продаж", "найм", "систем"]):
        update_user(user_id, "stage", "Развиваю бизнес")

# --- КЛАВИАТУРЫ ---
def start_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Планирую открыть дело", callback_data="stage_Планирую открыть")],
        [InlineKeyboardButton(text="🛠 Решаю задачу в микробизнесе", callback_data="stage_Действующий микробизнес")],
        [InlineKeyboardButton(text="📈 Хочу системно развить бизнес", callback_data="stage_Развиваю бизнес")],
        [InlineKeyboardButton(text="⏰ Настроить напоминания / Налоги", callback_data="menu_reminders")]
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

# --- ОБРАБОТЧИКИ КОМАНД ---
@dp.message(Command("start"))
async def cmd_start(message: Message):
    get_user(message.from_user.id)
    await message.answer(
        "Привет! Я **Альфа-Спутник** — ваш ИИ-помощник по платформе «Альфа-Курс» (на базе YandexGPT).\n\n"
        "Помогу быстро найти ответ по базе знаний, подберу бесплатные шаблоны или курсы под ваш уровень "
        "и вовремя напомню об учебе или уплате налогов.\n\n"
        "Выберите вашу ситуацию (или просто напишите свой вопрос в чат):",
        reply_markup=start_kb(),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("stage_"))
async def cb_set_stage(call: CallbackQuery):
    stage = call.data.replace("stage_", "")
    update_user(call.from_user.id, "stage", stage)
    await call.message.answer(
        f"✅ Запомнил вашу стадию: **{stage}**.\n"
        "Какой вопрос сейчас самый актуальный? Например:\n"
        "• *Какой налоговый режим выбрать для ИП?*\n"
        "• *Как рассчитать юнит-экономику на маркетплейсах?*\n"
        "• *Напомни мне про уплату налога по УСН*",
        parse_mode="Markdown"
    )
    await call.answer()

@dp.message(Command("profile"))
async def cmd_profile(message: Message):
    u = get_user(message.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Налог: УСН", callback_data="tax_УСН"),
            InlineKeyboardButton(text="Налог: НПД", callback_data="tax_НПД"),
            InlineKeyboardButton(text="Налог: Патент", callback_data="tax_Патент")
        ]
    ])
    await message.answer(
        f"📋 **Ваш профиль бизнеса:**\n"
        f"• Стадия: {u['stage']}\n"
        f"• Налоговый режим: {u['tax_regime']}\n"
        f"• Текущий фокус: {u['goal']}\n\n"
        f"_Я обновляю эти данные прямо во время диалога или по кнопкам ниже:_",
        reply_markup=kb,
        parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("tax_"))
async def cb_set_tax(call: CallbackQuery):
    tax = call.data.replace("tax_", "")
    update_user(call.from_user.id, "tax_regime", tax)
    await call.message.answer(
        f"✅ Налоговый режим обновлен на **{tax}**. Теперь я смогу точно подсказывать даты отчетности и уплаты!",
        parse_mode="Markdown"
    )
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

@dp.message(Command("demo_push"))
async def cmd_demo_push(message: Message):
    u = get_user(message.from_user.id)
    run_date = datetime.now() + timedelta(seconds=10)
    scheduler.add_job(
        send_scheduled_reminder,
        "date",
        run_date=run_date,
        args=[
            message.from_user.id,
            f"Пора уделить 15 минут обучению! Также проверьте календарь отчетности ({u['tax_regime']}). "
            f"Рекомендуем продолжить изучение вашего трека на платформе."
        ]
    )
    await message.answer(
        "⚡ **Демо-режим:** Разовое напоминание запланировано и придет ровно через **10 секунд**!",
        parse_mode="Markdown"
    )

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

# --- УМНЫЙ ОТВЕТ ПО БАЗЕ ЗНАНИЙ (YandexGPT + Fallback) ---
@dp.message()
async def handle_question(message: Message):
    user_id = message.from_user.id
    text = message.text
    auto_extract_profile(user_id, text)
    profile = get_user(user_id)

    # 1. Перехват запроса на создание напоминания
    if any(w in text.lower() for w in ["напомни", "напоминание", "срок уплаты"]):
        if profile["tax_regime"] == "Не указан" and "налог" in text.lower():
            await message.answer(
                "Чтобы я настроил точное напоминание о сроках уплаты налогов, уточните, пожалуйста, "
                "ваш режим налогообложения (например, УСН, НПД или Патент)?",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(text="УСН (до 28 числа)", callback_data="tax_УСН"),
                        InlineKeyboardButton(text="НПД (до 28 числа)", callback_data="tax_НПД")
                    ]
                ])
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

    # 2. Обращение к YandexGPT (если ключи подключены в .env)
    system_prompt = (
        "Ты — ИИ-помощник образовательной платформы «Альфа-Курс» в Telegram.\n"
        f"Профиль предпринимателя: Стадия={profile['stage']}, Налоги={profile['tax_regime']}.\n"
        "ПРАВИЛА:\n"
        "1. Отвечай ТОЛЬКО на основе предоставленной ниже базы знаний.\n"
        "2. Если вопрос слишком широкий или неясный — задай 1-2 конкретных уточняющих вопроса.\n"
        "3. Если ответа в базе нет — честно скажи об этом и предложи ближайшую тему из каталога.\n"
        "4. СТРОГОЕ ОГРАНИЧЕНИЕ: Для ПЛАТНЫХ курсов никогда не выдумывай и не раскрывай внутреннее содержание — "
        "кратко объясни, почему этот курс полезен пользователю на его стадии бизнеса, и дай ссылку на страницу курса.\n"
        "5. В конце ответа всегда предлагай понятный следующий шаг.\n\n"
        f"БАЗА ЗНАНИЙ:\n{build_safe_context()}"
    )

    yandex_answer = await ask_yandex_gpt(system_prompt, text)
    if yandex_answer:
        await message.answer(yandex_answer, parse_mode="Markdown")
        return

    # 3. Fallback-режим (срабатывает автоматически, если ключи YandexGPT еще не указаны)
    t_low = text.lower()
    if len(t_low.split()) <= 2 and t_low not in ["усн", "налоги"]:
        await message.answer(
            "Ваш запрос звучит довольно широко. Уточните, пожалуйста:\n"
            "1. Вы только планируете запуск или уже ведете бизнес?\n"
            "2. Вам нужен короткий ответ на сегодня, рабочий шаблон или системный курс?"
        )
    elif any(w in t_low for w in ["налог", "усн", "ип", "самозанят", "режим"]):
        m = CATALOG[0]
        await message.answer(
            f"💡 **Ответ по базе «Альфа-Курса» (с учетом профиля: {profile['stage']}):**\n"
            f"{m['full_text']}\n\n"
            f"📌 **Рекомендуемый бесплатный материал:** [{m['title']}]({m['url']})\n"
            f"*Почему подходит:* поможет за 10 минут сверить лимиты и не переплачивать на старте.\n\n"
            f"➡️ **Следующий шаг:** изучите чек-лист по ссылке или нажмите `/demo_push`, чтобы я напомнил вам о сроках уплаты.",
            parse_mode="Markdown"
        )
    elif any(w in t_low for w in ["маркетплейс", "продаж", "юнит", "wb", "ozon"]):
        m = CATALOG[1]
        await message.answer(
            f"По теме маркетплейсов и расчета прибыльности в каталоге есть профильная программа.\n\n"
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