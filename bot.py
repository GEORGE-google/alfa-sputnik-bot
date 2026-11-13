import asyncio
import logging
import os
import re
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

# Состояния ожидания текстового ввода от пользователя
AWAITING_CUSTOM_GOAL = set()
USER_CALC_MODE: dict[int, str] = {}   # user_id -> 'usn6' | 'margin' | 'bep'
TAX_QUIZ_STATE: dict[int, dict] = {}  # user_id -> {'income': ..., 'staff': ...}

# Хранилище ID сообщений для очистки чата по команде /start
CHAT_HISTORY_IDS: dict[int, set[int]] = {}

def track_msg(user_id: int, message_id: int):
    if user_id not in CHAT_HISTORY_IDS:
        CHAT_HISTORY_IDS[user_id] = set()
    CHAT_HISTORY_IDS[user_id].add(message_id)

async def send_and_track(msg: Message, text: str, **kwargs):
    """Отправляет сообщение и запоминает его ID для последующей очистки чата."""
    uid = msg.chat.id
    sent = await msg.answer(text, **kwargs)
    track_msg(uid, sent.message_id)
    return sent

async def clear_user_chat(chat_id: int, current_msg_id: int):
    """Удаляет предыдущие сообщения в чате при вводе /start."""
    ids_to_delete = set(CHAT_HISTORY_IDS.get(chat_id, set()))
    for mid in range(max(1, current_msg_id - 40), current_msg_id + 1):
        ids_to_delete.add(mid)

    tasks = [bot.delete_message(chat_id=chat_id, message_id=mid) for mid in ids_to_delete]
    await asyncio.gather(*tasks, return_exceptions=True)
    CHAT_HISTORY_IDS[chat_id] = set()

# --- СПРАВОЧНИКИ ---
STAGE_MAP = {
    "1": "Планирую открыть дело",
    "2": "Действующий микробизнес",
    "3": "Развиваю бизнес"
}

TAX_MAP = {
    "1": "УСН (Упрощенная система)",
    "2": "НПД (Самозанятый)",
    "3": "Патент (ПСН)",
    "4": "АУСН",
    "5": "ОСНО"
}

GOAL_MAP = {
    "1": "Открытие ИП и регистрация бизнеса",
    "2": "Налоги, учет и отчетность",
    "3": "Маркетинг и привлечение клиентов",
    "4": "Выход на маркетплейсы и продажи",
    "5": "Систематизация и найм команды"
}

SPECIFIC_TAX_REMINDERS = {
    "usn": {
        "name": "УСН (авансы и годовой налог)",
        "regime": "УСН (Упрощенная система)",
        "schedule_info": "Уведомление - до 25 числа, уплата квартальных авансов - до 28 апреля, 28 июля, 28 октября. Годовой налог - до 28 апреля.",
        "reminder_title": "Уплата налога по УСН (уведомление до 25-го, платеж до 28-го числа)"
    },
    "npd": {
        "name": "НПД (Налог на профдоход)",
        "regime": "НПД (Самозанятый)",
        "schedule_info": "Квитанция формируется в «Мой налог» до 12 числа, уплата налога - ежемесячно до 28 числа.",
        "reminder_title": "Уплата налога НПД в приложении «Мой налог» (до 28 числа)"
    },
    "psn": {
        "name": "Патент (ПСН)",
        "regime": "Патент (ПСН)",
        "schedule_info": "Патент до 6 мес. оплачивается целиком до конца срока. От 6 до 12 мес.: 1/3 - в первые 90 дней, 2/3 - до конца действия патента.",
        "reminder_title": "Контроль оплаты Патента (ПСН) и подачи уведомления на уменьшение"
    },
    "vznosy": {
        "name": "Страховые взносы ИП за себя",
        "regime": None,
        "schedule_info": "Фиксированные взносы ИП - до 28 декабря текущего года. Дополнительный взнос 1% с дохода свыше 300 000 ₽ - до 1 июля следующего года.",
        "reminder_title": "Уплата страховых взносов ИП (до 28 декабря / 1% до 1 июля)"
    },
    "ausn": {
        "name": "АУСН (Автоматизированная УСН)",
        "regime": "АУСН",
        "schedule_info": "ФНС рассчитывает налог автоматически до 15 числа, уплата - ежемесячно до 25 числа.",
        "reminder_title": "Ежемесячная уплата налога по АУСН (до 25 числа)"
    }
}

# --- БАЗА ЗНАНИЙ «АЛЬФА-КУРСА» ---
CATALOG = [
    {
        "id": "mat_ip_start",
        "title": "Пошаговый гид: Как открыть ИП с нуля и без госпошлины",
        "category": "Регистрация и ИП",
        "keywords": ["ип", "открыть", "регистрация", "оквэд", "с нуля", "документы", "старт", "госпошлина"],
        "target_stage": "Планирую открыть дело",
        "is_paid": False,
        "summary": "Краткий план регистрации ИП за 0 ₽ и 3 рабочих дня с готовыми шаблонами документов.",
        "full_text": (
            "📌 **Как открыть ИП с нуля и без госпошлины - коротко о главном:**\n\n"
            "• **Подберите ОКВЭД и налоговый режим:** выберите коды деятельности и сразу подайте уведомление на УСН или НПД, чтобы не остаться на сложной ОСНО.\n"
            "• **Подайте заявку онлайн за 0 ₽:** при электронной подаче паспорта и ИНН (через сервис Альфа-Банка, Госуслуги или ФНС) госпошлина 800 ₽ не платится.\n"
            "• **Получите документы и откройте счет:** лист записи ЕГРИП придет на почту за 1-3 дня, после чего можно сразу принимать оплату.\n\n"
            "💡 *Все подробности, разбор частых ошибок при выборе ОКВЭД и готовые шаблоны заявлений смотрите в полном материале по ссылке на «Альфа-Курсе» ниже.*"
        ),
        "url": "https://kurs.alfabank.ru/catalog?type=courses&utm_source=tg_bot&utm_medium=ip_guide"
    },
    {
        "id": "mat_ip_taxes",
        "title": "Как выбрать налоговый режим для ИП и ООО в 2026 году",
        "category": "Налоги и учет",
        "keywords": ["ип", "налог", "налоги", "усн", "патент", "псн", "аусн", "нпд", "режим", "ооо"],
        "target_stage": "Планирую открыть дело",
        "is_paid": False,
        "summary": "Сравнение УСН, НПД, Патента и АУСН с возможностью сразу настроить напоминание по выбранному налогу.",
        "full_text": (
            "📊 **Основные налоговые режимы для бизнеса:**\n\n"
            "• **НПД (4-6%):** до 2,4 млн ₽ в год, без сотрудников и без обязательных взносов. Уплата - до 28 числа.\n"
            "• **УСН «Доходы» (6%):** для услуг и небольших расходов. Уменьшается на взносы ИП. Квартальные авансы - до 28 числа.\n"
            "• **УСН «Доходы минус расходы» (15%):** выгоден, если подтвержденные расходы превышают 60-70% выручки.\n"
            "• **Патент (ПСН):** фиксированный налог для розницы, услуг и общепита (до 60 млн ₽ и до 15 сотрудников).\n"
            "• **АУСН (8% или 20%):** налог рассчитывается автоматически по счету, без деклараций (уплата до 25 числа).\n\n"
            "🔔 *Ниже вы можете сразу включить напоминание по вашему конкретному налогу или подобрать режим за 3 секунды.*"
        ),
        "url": "https://kurs.alfabank.ru/catalog?type=courses&utm_source=tg_bot&utm_medium=ip_taxes"
    },
    {
        "id": "mat_ip_vznosy",
        "title": "Страховые взносы ИП, отчетность и налоговый календарь",
        "category": "Налоги и учет",
        "keywords": ["ип", "взносы", "страховые", "отчетность", "декларация", "календарь", "сроки", "уплата"],
        "target_stage": "Действующий микробизнес",
        "is_paid": False,
        "summary": "Сроки уплаты фиксированных взносов ИП, 1% свыше 300 000 ₽ и правила уменьшения налога УСН/Патента.",
        "full_text": (
            "💳 **Страховые взносы и календарь ИП:**\n\n"
            "• **Фиксированные взносы ИП за себя:** уплачиваются до **28 декабря** текущего года (кроме ИП на НПД и АУСН).\n"
            "• **Дополнительный взнос 1%:** с дохода свыше 300 000 ₽ в год - уплачивается до **1 июля** следующего года.\n"
            "• **Декларация по УСН для ИП:** подается один раз в год до **25 апреля**, уплата годового налога - до **28 апреля**.\n\n"
            "🔔 *Нажмите кнопку «Напомнить по конкретному налогу» ниже, чтобы не пропустить даты платежей.*"
        ),
        "url": "https://kurs.alfabank.ru/catalog?type=courses&utm_source=tg_bot&utm_medium=ip_vznosy"
    },
    {
        "id": "mat_ip_kassa",
        "title": "Расчетный счет, онлайн-касса (54-ФЗ) и эквайринг для ИП",
        "category": "Финансы и операции",
        "keywords": ["ип", "касса", "счет", "эквайринг", "54-фз", "чек", "оплата", "вывод денег", "сбп"],
        "target_stage": "Действующий микробизнес",
        "is_paid": False,
        "summary": "Когда нужна онлайн-касса, как принимать оплату по СБП и легально переводить деньги ИП на личную карту.",
        "full_text": (
            "🧾 **Счет, касса и вывод средств ИП:**\n\n"
            "• **Онлайн-касса (54-ФЗ):** нужна при приеме оплат от физлиц. Освобождены ИП на НПД, часть видов деятельности на Патенте и расчеты только с юрлицами/ИП по счету.\n"
            "• **Экономия на комиссиях:** прием платежей по QR-коду (СБП) обходится в 0,4-0,7% вместо стандартного эквайринга.\n"
            "• **Вывод прибыли:** средства после уплаты налогов можно свободно переводить на личную карту физлица.\n\n"
            "💡 *Подробные инструкции и чек-листы доступны по ссылке на «Альфа-Курсе».*"
        ),
        "url": "https://kurs.alfabank.ru/catalog?type=courses&utm_source=tg_bot&utm_medium=ip_kassa"
    },
    {
        "id": "mat_mp_paid",
        "title": "Практикум: Выход ИП на маркетплейсы и расчет юнит-экономики",
        "category": "Продажи и маркетинг",
        "keywords": ["ип", "маркетплейс", "маркетплейсы", "wb", "wildberries", "ozon", "юнит", "продажи", "селлеры"],
        "target_stage": "Действующий микробизнес",
        "is_paid": True,
        "summary": "Углубленный платный курс с готовыми финансовыми таблицами расчета маржинальности, комиссий, логистики и налогов селлера.",
        "full_text": "ЗАКРЫТЫЙ КОНТЕНТ ПЛАТНОГО КУРСА.",
        "url": "https://kurs.alfabank.ru/catalog?type=courses&utm_source=tg_bot&utm_medium=upsell_mp"
    },
    {
        "id": "mat_sales_paid",
        "title": "Система продаж, CRM и найм первых сотрудников в ИП и ООО",
        "category": "Управление и команда",
        "keywords": ["ип", "продажи", "отдел продаж", "найм", "сотрудники", "команда", "crm", "масштабирование", "управление"],
        "target_stage": "Развиваю бизнес",
        "is_paid": True,
        "summary": "Платная программа по оформлению первых сотрудников, построению отдела продаж, внедрению CRM и скриптов.",
        "full_text": "ЗАКРЫТЫЙ КОНТЕНТ ПЛАТНОГО КУРСА.",
        "url": "https://kurs.alfabank.ru/catalog?type=courses&utm_source=tg_bot&utm_medium=upsell_sales"
    }
]

# --- БАЗА ДАННЫХ (Профили, Напоминания, Изученные материалы) ---
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
                frequency TEXT,
                active INTEGER DEFAULT 1
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS completed_materials (
                user_id INTEGER,
                mat_id TEXT,
                completed_at TEXT,
                PRIMARY KEY (user_id, mat_id)
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

def mark_material_done(user_id: int, mat_id: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO completed_materials (user_id, mat_id, completed_at) VALUES (?, ?, ?)",
            (user_id, mat_id, datetime.now().isoformat())
        )

def get_completed_materials(user_id: int) -> set[str]:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("SELECT mat_id FROM completed_materials WHERE user_id = ?", (user_id,))
        return {r[0] for r in cur.fetchall()}

def get_ordered_route_for_user(user_id: int) -> list[dict]:
    """Строит персональный порядок маршрута в зависимости от стадии и фокуса пользователя."""
    u = get_user(user_id)
    stage = u["stage"]
    goal = u["goal"]

    if "Развиваю" in stage or "команд" in goal.lower():
        order = ["mat_sales_paid", "mat_mp_paid", "mat_ip_kassa", "mat_ip_vznosy", "mat_ip_taxes", "mat_ip_start"]
    elif "микробизнес" in stage or "маркетплейс" in goal.lower():
        order = ["mat_ip_vznosy", "mat_ip_kassa", "mat_mp_paid", "mat_ip_taxes", "mat_sales_paid", "mat_ip_start"]
    else:
        order = ["mat_ip_start", "mat_ip_taxes", "mat_ip_vznosy", "mat_ip_kassa", "mat_mp_paid", "mat_sales_paid"]

    id_map = {item["id"]: item for item in CATALOG}
    return [id_map[mid] for mid in order if mid in id_map]

def get_next_recommended_material(user_id: int) -> dict | None:
    """Находит следующий неизученный материал по маршруту пользователя."""
    completed = get_completed_materials(user_id)
    route = get_ordered_route_for_user(user_id)
    for item in route:
        if item["id"] not in completed:
            return item
    return None

def build_safe_context() -> str:
    """Формирует контекст для YandexGPT: для платных курсов скрывает full_text (Ограничение 06)."""
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

# --- ПОИСК ПО КЛЮЧЕВЫМ СЛОВАМ (неизученные выше) ---
def search_by_keyword(query: str, user_id: int | None = None) -> list[dict]:
    q_words = [w.strip(".,!?\"'()") for w in query.lower().split() if len(w.strip(".,!?\"'()")) >= 2]
    if not q_words:
        return []
    completed = get_completed_materials(user_id) if user_id else set()
    results = []
    for item in CATALOG:
        searchable = (
            f"{item['title']} {item['category']} {' '.join(item.get('keywords', []))} "
            f"{item['summary']} {item['full_text'] if not item['is_paid'] else ''}"
        ).lower()
        score = sum(2 if w in item.get("keywords", []) or w in item["title"].lower() else 1 for w in q_words if w in searchable)
        if score > 0:
            is_unseen = 0 if item["id"] in completed else 1
            results.append((is_unseen, score, item))
    results.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [r[2] for r in results]

def get_material_by_id(mat_id: str) -> dict | None:
    for item in CATALOG:
        if item["id"] == mat_id:
            return item
    return None

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
        "completionOptions": {"stream": False, "temperature": 0.3, "maxTokens": "1000"},
        "messages": [
            {"role": "system", "text": system_prompt},
            {"role": "user", "text": user_text}
        ]
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(YANDEX_API_URL, headers=headers, json=payload, timeout=20) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                text_out = data["result"]["alternatives"][0]["message"]["text"]
                return text_out.replace("—", "-").replace("–", "-")
    except Exception as e:
        logging.error(f"Ошибка при запросе к YandexGPT: {e}")
        return None

# --- АВТОМАТИЧЕСКОЕ ОБНОВЛЕНИЕ ПРОФИЛЯ ---
def auto_extract_profile(user_id: int, text: str):
    t = text.lower()
    if any(w in t for w in ["усн", "упрощенк"]):
        update_user(user_id, "tax_regime", "УСН (Упрощенная система)")
    elif any(w in t for w in ["самозанят", "нпд"]):
        update_user(user_id, "tax_regime", "НПД (Самозанятый)")
    elif "патент" in t or "псн" in t:
        update_user(user_id, "tax_regime", "Патент (ПСН)")
    elif "аусн" in t:
        update_user(user_id, "tax_regime", "АУСН")

    if any(w in t for w in ["открыть ип", "начать", "регистрац", "с нуля"]):
        update_user(user_id, "stage", "Планирую открыть дело")
        update_user(user_id, "goal", "Открытие ИП и регистрация бизнеса")
    elif any(w in t for w in ["маркетплейс", "wb", "ozon", "юнит"]):
        update_user(user_id, "goal", "Выход на маркетплейсы и продажи")
    elif any(w in t for w in ["налог", "взносы", "декларац", "отчетност"]):
        update_user(user_id, "goal", "Налоги, учет и отчетность")

# --- КЛАВИАТУРЫ ---
def start_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Планирую открыть дело", callback_data="stage_1")],
        [InlineKeyboardButton(text="🛠 Решаю задачу в микробизнесе", callback_data="stage_2")],
        [InlineKeyboardButton(text="📈 Хочу системно развить бизнес", callback_data="stage_3")],
        [InlineKeyboardButton(text="🏢 Всё об ИП: открытие с нуля и налоги", callback_data="nav_ip_hub")],
        [
            InlineKeyboardButton(text="⚡ Подбор налога за 3 сек", callback_data="nav_tax_quiz"),
            InlineKeyboardButton(text="🧮 Быстрые расчеты", callback_data="nav_calc")
        ],
        [InlineKeyboardButton(text="🗺 Мой маршрут обучения", callback_data="nav_route")],
        [
            InlineKeyboardButton(text="📋 Мой профиль", callback_data="nav_profile"),
            InlineKeyboardButton(text="⏰ Напоминания", callback_data="nav_reminders")
        ],
        [InlineKeyboardButton(text="❓ Помощь и все функции", callback_data="nav_help")]
    ])

def profile_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎯 Изменить текущий фокус", callback_data="submenu_goal")],
        [InlineKeyboardButton(text="💼 Изменить налоговый режим", callback_data="submenu_tax")],
        [InlineKeyboardButton(text="⚡ Подобрать налог за 3 сек (Квиз)", callback_data="nav_tax_quiz")],
        [InlineKeyboardButton(text="🚀 Изменить стадию бизнеса", callback_data="submenu_stage")],
        [InlineKeyboardButton(text="🗺 Мой маршрут обучения", callback_data="nav_route")],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="nav_start")]
    ])

def goal_selection_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏢 Открытие ИП и регистрация бизнеса", callback_data="goal_1")],
        [InlineKeyboardButton(text="📊 Налоги, учет и отчетность", callback_data="goal_2")],
        [InlineKeyboardButton(text="📣 Маркетинг и привлечение клиентов", callback_data="goal_3")],
        [InlineKeyboardButton(text="🛍 Выход на маркетплейсы и продажи", callback_data="goal_4")],
        [InlineKeyboardButton(text="👥 Систематизация и найм команды", callback_data="goal_5")],
        [InlineKeyboardButton(text="✍️ Ввести свой фокус текстом", callback_data="goal_custom")],
        [InlineKeyboardButton(text="⬅️ Назад в профиль", callback_data="nav_profile")]
    ])

def tax_selection_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="УСН", callback_data="tax_1"),
            InlineKeyboardButton(text="НПД (Самозанятый)", callback_data="tax_2")
        ],
        [
            InlineKeyboardButton(text="Патент (ПСН)", callback_data="tax_3"),
            InlineKeyboardButton(text="АУСН", callback_data="tax_4")
        ],
        [
            InlineKeyboardButton(text="ОСНО", callback_data="tax_5"),
            InlineKeyboardButton(text="⬅️ Назад в профиль", callback_data="nav_profile")
        ]
    ])

def stage_selection_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Планирую открыть дело", callback_data="stage_1")],
        [InlineKeyboardButton(text="🛠 Действующий микробизнес", callback_data="stage_2")],
        [InlineKeyboardButton(text="📈 Развиваю бизнес", callback_data="stage_3")],
        [InlineKeyboardButton(text="⬅️ Назад в профиль", callback_data="nav_profile")]
    ])

def ip_hub_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Как открыть ИП с нуля за 0 ₽", callback_data="mat_mat_ip_start")],
        [InlineKeyboardButton(text="⚡ Калькулятор налогового режима за 3 сек", callback_data="nav_tax_quiz")],
        [InlineKeyboardButton(text="📊 Налоги для ИП: сравнение режимов", callback_data="mat_mat_ip_taxes")],
        [InlineKeyboardButton(text="🔔 Напомнить по конкретному налогу", callback_data="nav_tax_reminders")],
        [InlineKeyboardButton(text="💳 Страховые взносы ИП и отчетность", callback_data="mat_mat_ip_vznosy")],
        [InlineKeyboardButton(text="🧾 Счет, онлайн-касса и вывод денег", callback_data="mat_mat_ip_kassa")],
        [InlineKeyboardButton(text="🧮 Быстрые расчеты (УСН, маржа, ТБУ)", callback_data="nav_calc")],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="nav_start")]
    ])

def specific_tax_reminders_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📌 УСН (авансы и налог - до 28 числа)", callback_data="addtaxrem_usn")],
        [InlineKeyboardButton(text="📌 НПД / Самозанятость (до 28 числа)", callback_data="addtaxrem_npd")],
        [InlineKeyboardButton(text="📌 Патент / ПСН (контроль сроков оплаты)", callback_data="addtaxrem_psn")],
        [InlineKeyboardButton(text="📌 Страховые взносы ИП (28 дек / 1 июля)", callback_data="addtaxrem_vznosy")],
        [InlineKeyboardButton(text="📌 АУСН (ежемесячно до 25 числа)", callback_data="addtaxrem_ausn")],
        [InlineKeyboardButton(text="⏰ Мои активные напоминания", callback_data="nav_reminders")],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="nav_start")]
    ])

def calc_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Налог УСН 6% и вычет взносов ИП", callback_data="calc_usn6")],
        [InlineKeyboardButton(text="📦 Маржинальность и прибыль с товара", callback_data="calc_margin")],
        [InlineKeyboardButton(text="⚖️ Точка безубыточности (ТБУ)", callback_data="calc_bep")],
        [InlineKeyboardButton(text="⚡ Подбор налогового режима за 3 сек", callback_data="nav_tax_quiz")],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="nav_start")]
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
    USER_CALC_MODE.pop(user_id, None)
    get_user(user_id)
    await send_and_track(
        msg,
        "Привет! Я **Альфа-Спутник** - ваш ИИ-помощник по платформе «Альфа-Курс».\n\n"
        "• Ищу материалы по **ключевым словам** (напишите `ИП`, `налоги` или `маркетплейс`)\n"
        "• Подбираю **налоговый режим за 3 секунды** и считаю **налоги, маржу и ТБУ** прямо в чате\n"
        "• Веду ваш **маршрут обучения** со статусом «Изучено» и напоминаю о налогах\n\n"
        "👇 **Выберите нужный раздел или напишите свой вопрос в чат:**",
        reply_markup=start_kb(),
        parse_mode="Markdown"
    )

async def send_stage_response(msg: Message, user_id: int, stage_code: str):
    """Индивидуальный ответ под каждую из 3 ситуаций после /start."""
    stage_title = STAGE_MAP.get(stage_code, "Планирую открыть дело")
    update_user(user_id, "stage", stage_title)

    if stage_code == "1":
        update_user(user_id, "goal", "Открытие ИП и регистрация бизнеса")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📝 Как открыть ИП с нуля и без госпошлины", callback_data="mat_mat_ip_start")],
            [InlineKeyboardButton(text="⚡ Подобрать налоговый режим за 3 сек", callback_data="nav_tax_quiz")],
            [InlineKeyboardButton(text="🗺 Открыть мой маршрут обучения", callback_data="nav_route")],
            [InlineKeyboardButton(text="🏠 В главное меню", callback_data="nav_start")]
        ])
        await send_and_track(
            msg,
            f"🚀 **Стадия «{stage_title}» сохранена в профиль.**\n\n"
            "Для быстрого старта без штрафов рекомендуем:\n"
            "• Подобрать налоговый режим за 3 секунды в нашем экспресс-калькуляторе\n"
            "• Подать заявку на открытие ИП онлайн без уплаты госпошлины 800 ₽\n"
            "• Пройти базовый маршрут начинающего предпринимателя\n\n"
            "👇 **С чего начнем?**",
            reply_markup=kb,
            parse_mode="Markdown"
        )

    elif stage_code == "2":
        update_user(user_id, "goal", "Налоги, учет и отчетность")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🧮 Быстрые расчеты: УСН 6%, маржа и ТБУ", callback_data="nav_calc")],
            [InlineKeyboardButton(text="💳 Страховые взносы ИП и уменьшение налога", callback_data="mat_mat_ip_vznosy")],
            [InlineKeyboardButton(text="🔔 Напомнить по конкретному налогу", callback_data="nav_tax_reminders")],
            [InlineKeyboardButton(text="🗺 Открыть мой маршрут обучения", callback_data="nav_route")],
            [InlineKeyboardButton(text="🏠 В главное меню", callback_data="nav_start")]
        ])
        await send_and_track(
            msg,
            f"🛠 **Стадия «{stage_title}» сохранена.**\n\n"
            "Подборка практических решений для действующего микробизнеса:\n"
            "• Посчитать вычет взносов ИП, маржинальность и точку безубыточности прямо в чате\n"
            "• Настроить напоминание по вашему конкретному налогу\n"
            "• Изучить материалы по кассе и юнит-экономике в персональном маршруте\n\n"
            "👇 **Выберите нужный инструмент:**",
            reply_markup=kb,
            parse_mode="Markdown"
        )

    else:
        update_user(user_id, "goal", "Систематизация и найм команды")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👥 Курс: Построение отдела продаж и найм", callback_data="mat_mat_sales_paid")],
            [InlineKeyboardButton(text="📈 Курс: Масштабирование на маркетплейсах", callback_data="mat_mat_mp_paid")],
            [InlineKeyboardButton(text="🧮 Посчитать маржинальность и ТБУ в чате", callback_data="nav_calc")],
            [InlineKeyboardButton(text="🗺 Открыть мой маршрут обучения", callback_data="nav_route")],
            [InlineKeyboardButton(text="🏠 В главное меню", callback_data="nav_start")]
        ])
        await send_and_track(
            msg,
            f"📈 **Стадия «{stage_title}» сохранена.**\n\n"
            "Материалы «Альфа-Курса» для системного роста:\n"
            "• Оформление первых сотрудников в ИП/ООО и мотивация команды\n"
            "• Внедрение CRM, скриптов продаж и контроль конверсии\n"
            "• Финансовое планирование и масштабирование на маркетплейсах\n\n"
            "👇 **Выберите программу для изучения:**",
            reply_markup=kb,
            parse_mode="Markdown"
        )

# --- 1. МАРШРУТ ОБУЧЕНИЯ И СТАТУС «ИЗУЧЕНО» ---
async def send_learning_route_screen(msg: Message, user_id: int):
    """Показывает персональный маршрут обучения со статусами «Изучено» и следующим шагом."""
    AWAITING_CUSTOM_GOAL.discard(user_id)
    USER_CALC_MODE.pop(user_id, None)

    u = get_user(user_id)
    completed = get_completed_materials(user_id)
    route = get_ordered_route_for_user(user_id)
    next_item = get_next_recommended_material(user_id)

    total = len(route)
    done_cnt = len(completed)

    lines = [
        "🗺 **Ваш персональный маршрут обучения на «Альфа-Курсе»**\n",
        f"• **Профиль:** {u['stage']} | Фокус: {u['goal']}",
        f"• **Прогресс:** изучено **{done_cnt} из {total}** материалов\n"
    ]

    buttons = []
    for item in route:
        is_done = item["id"] in completed
        is_next = next_item and item["id"] == next_item["id"]
        if is_done:
            status_icon = "✅ [Изучено]"
            btn_prefix = "✅"
        elif is_next:
            status_icon = "👉 **[Следующий шаг]**"
            btn_prefix = "👉"
        else:
            status_icon = "⏳ [В плане]"
            btn_prefix = "📖"

        lines.append(f"{status_icon} {item['title']}")
        buttons.append([InlineKeyboardButton(text=f"{btn_prefix} {item['title'][:38]}...", callback_data=f"mat_{item['id']}")])

    if next_item:
        lines.append(f"\n💡 **Рекомендуемый следующий шаг:** «{next_item['title']}»")
    else:
        lines.append("\n🏆 **Поздравляем! Вы изучили все базовые материалы маршрута.**")

    buttons.append([InlineKeyboardButton(text="🏠 В главное меню", callback_data="nav_start")])

    await send_and_track(
        msg,
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown"
    )

# --- 2. КАЛЬКУЛЯТОР НАЛОГОВОГО РЕЖИМА ЗА 3 СЕКУНДЫ ---
async def send_tax_quiz_start(msg: Message, user_id: int):
    """Шаг 1 из 3: выбор годового дохода."""
    AWAITING_CUSTOM_GOAL.discard(user_id)
    USER_CALC_MODE.pop(user_id, None)
    TAX_QUIZ_STATE[user_id] = {}

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="До 2,4 млн ₽ в год (до 200 тыс. ₽/мес)", callback_data="tquiz_inc_low")],
        [InlineKeyboardButton(text="От 2,4 до 60 млн ₽ в год", callback_data="tquiz_inc_mid")],
        [InlineKeyboardButton(text="Свыше 60 млн ₽ в год", callback_data="tquiz_inc_high")],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="nav_start")]
    ])
    await send_and_track(
        msg,
        "⚡ **Подбор налогового режима за 3 секунды (Вопрос 1 из 3)**\n\n"
        "Какой у вас планируемый или текущий **годовой доход (выручка)**?",
        reply_markup=kb,
        parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("tquiz_inc_"))
async def cb_tax_quiz_step2(call: CallbackQuery):
    """Шаг 2 из 3: наличие сотрудников."""
    uid = call.from_user.id
    inc = call.data.replace("tquiz_inc_", "")
    TAX_QUIZ_STATE[uid] = {"income": inc}

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Работаю один (без сотрудников)", callback_data="tquiz_staff_none")],
        [InlineKeyboardButton(text="До 5 сотрудников", callback_data="tquiz_staff_up5")],
        [InlineKeyboardButton(text="От 6 до 15 сотрудников", callback_data="tquiz_staff_up15")],
        [InlineKeyboardButton(text="Более 15 сотрудников", callback_data="tquiz_staff_many")],
        [InlineKeyboardButton(text="⬅️ Начать заново", callback_data="nav_tax_quiz")]
    ])
    await call.message.edit_text(
        "⚡ **Подбор налогового режима за 3 секунды (Вопрос 2 из 3)**\n\n"
        "Планируете ли вы **нанимать сотрудников** по трудовым договорам?",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    await call.answer()

@dp.callback_query(F.data.startswith("tquiz_staff_"))
async def cb_tax_quiz_step3(call: CallbackQuery):
    """Шаг 3 из 3: доля подтвержденных расходов."""
    uid = call.from_user.id
    staff = call.data.replace("tquiz_staff_", "")
    if uid not in TAX_QUIZ_STATE:
        TAX_QUIZ_STATE[uid] = {"income": "mid"}
    TAX_QUIZ_STATE[uid]["staff"] = staff

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Мало расходов (менее 60% - услуги, IT, консультации)", callback_data="tquiz_exp_low")],
        [InlineKeyboardButton(text="Много расходов (более 60% - торговля, закупка, общепит)", callback_data="tquiz_exp_high")],
        [InlineKeyboardButton(text="⬅️ Начать заново", callback_data="nav_tax_quiz")]
    ])
    await call.message.edit_text(
        "⚡ **Подбор налогового режима за 3 секунды (Вопрос 3 из 3)**\n\n"
        "Какая у вашего бизнеса **доля документально подтвержденных расходов** от выручки?",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    await call.answer()

@dp.callback_query(F.data.startswith("tquiz_exp_"))
async def cb_tax_quiz_result(call: CallbackQuery):
    """Мгновенный результат подбора налогового режима + сохранение в профиль."""
    uid = call.from_user.id
    exp = call.data.replace("tquiz_exp_", "")
    state = TAX_QUIZ_STATE.get(uid, {"income": "mid", "staff": "none"})
    inc = state.get("income", "mid")
    staff = state.get("staff", "none")

    if inc == "low" and staff == "none" and exp == "low":
        best_regime = "НПД (Самозанятый)"
        rem_key = "npd"
        explanation = (
            "• **Ставка:** 4% с физлиц и 6% с ИП/юрлиц.\n"
            "• **Почему подходит:** при доходе до 2,4 млн ₽ без сотрудников вы не платите обязательные фиксированные взносы ИП и не сдаете декларации.\n"
            "• **Альтернатива для роста:** ИП на УСН «Доходы» 6%."
        )
    elif exp == "low":
        best_regime = "УСН (Упрощенная система)"
        rem_key = "usn"
        extra_note = " Также рассмотрите **Патент (ПСН)** или **АУСН (8%)**, если у вас до 5 сотрудников." if inc != "high" and staff in ("none", "up5") else ""
        explanation = (
            "• **Рекомендуемый вариант:** **УСН «Доходы» (6%)**.\n"
            "• **Почему подходит:** при небольшой доле расходов выгоднее платить 6% со всей выручки и уменьшать налог до 50-100% за счет страховых взносов ИП.\n"
            f"• **Дополнительно:**{extra_note if extra_note else ' подходит для масштабирования с выручкой свыше 60 млн ₽.'}"
        )
    else:
        best_regime = "УСН (Упрощенная система)"
        rem_key = "usn"
        explanation = (
            "• **Рекомендуемый вариант:** **УСН «Доходы минус расходы» (15%)** (или **Патент** для розницы/услуг до 60 млн ₽).\n"
            "• **Почему подходит:** когда закупка товара, аренда и логистика превышают 60-70% оборота, налог с разницы (доходы минус расходы) экономит до половины налоговой нагрузки."
        )

    update_user(uid, "tax_regime", best_regime)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🔔 Включить напоминание по {best_regime[:12]}", callback_data=f"addtaxrem_{rem_key}")],
        [InlineKeyboardButton(text="🧮 Посчитать сумму налога в чате", callback_data="calc_usn6")],
        [InlineKeyboardButton(text="📖 Подробнее о налогах на «Альфа-Курсе»", callback_data="mat_mat_ip_taxes")],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="nav_start")]
    ])

    await call.message.edit_text(
        f"✅ **Результат подбора: {best_regime}**\n"
        f"_(Режим автоматически сохранен в ваш профиль!)_\n\n"
        f"{explanation}\n\n"
        f"👇 **Выберите следующий шаг:**",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    await call.answer("Режим подобран!")

# --- 3. РАЗДЕЛ С БЫСТРЫМИ РАСЧЕТАМИ ПРЯМО В ЧАТЕ ---
async def send_calc_menu_screen(msg: Message, user_id: int):
    AWAITING_CUSTOM_GOAL.discard(user_id)
    USER_CALC_MODE.pop(user_id, None)
    await send_and_track(
        msg,
        "🧮 **Быстрые финансовые расчеты прямо в чате**\n\n"
        "Выберите нужный калькулятор - вы сможете отправить свои числа в чат или нажать кнопку мгновенного примера:",
        reply_markup=calc_menu_kb(),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("calc_"))
async def cb_select_calculator(call: CallbackQuery):
    uid = call.from_user.id
    mode = call.data.replace("calc_", "")

    if mode == "demo_usn6":
        await process_calculation_input(call.message, uid, "1200000 53658", mode_override="usn6")
        await call.answer()
        return
    elif mode == "demo_margin":
        await process_calculation_input(call.message, uid, "2500 1400", mode_override="margin")
        await call.answer()
        return
    elif mode == "demo_bep":
        await process_calculation_input(call.message, uid, "150000 3000 1800", mode_override="bep")
        await call.answer()
        return

    USER_CALC_MODE[uid] = mode

    if mode == "usn6":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Показать пример (Доход 1 200 000 ₽, взносы 53 658 ₽)", callback_data="calc_demo_usn6")],
            [InlineKeyboardButton(text="⬅️ К списку расчетов", callback_data="nav_calc")]
        ])
        await send_and_track(
            call.message,
            "💰 **Калькулятор УСН 6% и вычета взносов ИП за год**\n\n"
            "Отправьте в чат **2 числа через пробел**:\n"
            "`Годовой_доход Уплаченные_взносы`\n"
            "*(Например: `1200000 53658`)*\n\n"
            "Я рассчитаю налог 6%, дополнительный взнос 1% свыше 300 000 ₽ и итоговую сумму к доплате с учетом вычета:",
            reply_markup=kb,
            parse_mode="Markdown"
        )
    elif mode == "margin":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Показать пример (Цена 2 500 ₽, Расходы 1 400 ₽)", callback_data="calc_demo_margin")],
            [InlineKeyboardButton(text="⬅️ К списку расчетов", callback_data="nav_calc")]
        ])
        await send_and_track(
            call.message,
            "📦 **Калькулятор маржинальности и наценки товара**\n\n"
            "Отправьте в чат **2 числа через пробел**:\n"
            "`Цена_продажи Себестоимость_с_расходами`\n"
            "*(Например: `2500 1400`)*\n\n"
            "Я рассчитаю чистую прибыль с единицы, маржинальность и торговую наценку:",
            reply_markup=kb,
            parse_mode="Markdown"
        )
    elif mode == "bep":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Показать пример (Аренда/ФОТ 150 000, Чек 3 000, Себест. 1 800)", callback_data="calc_demo_bep")],
            [InlineKeyboardButton(text="⬅️ К списку расчетов", callback_data="nav_calc")]
        ])
        await send_and_track(
            call.message,
            "⚖️ **Калькулятор точки безубыточности (ТБУ) на месяц**\n\n"
            "Отправьте в чат **3 числа через пробел**:\n"
            "`Постоянные_расходы Средний_чек Переменные_расходы_на_1_продажу`\n"
            "*(Например: `150000 3000 1800`)*\n\n"
            "Я посчитаю, сколько продаж и на какую выручку нужно делать в месяц для выхода в ноль:",
            reply_markup=kb,
            parse_mode="Markdown"
        )
    await call.answer()

async def process_calculation_input(msg: Message, user_id: int, text: str, mode_override: str | None = None) -> bool:
    """Обрабатывает ввод чисел для выбранного калькулятора."""
    mode = mode_override or USER_CALC_MODE.get(user_id)
    if not mode:
        return False

    nums = [float(x.replace(",", ".")) for x in re.findall(r"\d+(?:[.,]\d+)?", text)]
    if len(nums) < 2:
        return False

    USER_CALC_MODE.pop(user_id, None)

    if mode == "usn6":
        income = nums[0]
        fixed_vznosy = nums[1]
        tax_6 = income * 0.06
        extra_1_percent = max(0.0, (income - 300_000.0) * 0.01)
        total_deduction = fixed_vznosy + extra_1_percent
        tax_to_pay = max(0.0, tax_6 - total_deduction)

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Подробнее о взносах ИП и вычете", callback_data="mat_mat_ip_vznosy")],
            [InlineKeyboardButton(text="🔔 Включить напоминание по УСН", callback_data="addtaxrem_usn")],
            [InlineKeyboardButton(text="🧮 Другой расчет", callback_data="nav_calc")],
            [InlineKeyboardButton(text="🏠 В главное меню", callback_data="nav_start")]
        ])
        await send_and_track(
            msg,
            f"💰 **Расчет налога УСН 6% (для ИП без сотрудников):**\n\n"
            f"• **Годовой доход:** {income:,.0f} ₽\n"
            f"• **Налог УСН (6% до вычета):** {tax_6:,.0f} ₽\n"
            f"• **Фиксированные взносы:** {fixed_vznosy:,.0f} ₽\n"
            f"• **Доп. взнос 1% свыше 300 000 ₽:** {extra_1_percent:,.0f} ₽\n"
            f"• **Всего взносов к вычету:** {total_deduction:,.0f} ₽\n\n"
            f"✅ **Итого налог УСН к доплате:** **{tax_to_pay:,.0f} ₽**\n"
            f"_(Налог уменьшен на сумму взносов на {min(tax_6, total_deduction):,.0f} ₽!)_",
            reply_markup=kb,
            parse_mode="Markdown"
        )
        return True

    elif mode == "margin":
        price = nums[0]
        cost = nums[1]
        profit = price - cost
        margin = (profit / price * 100.0) if price > 0 else 0.0
        markup = (profit / cost * 100.0) if cost > 0 else 0.0

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎓 Курс: Юнит-экономика на маркетплейсах", callback_data="mat_mat_mp_paid")],
            [InlineKeyboardButton(text="🧮 Другой расчет", callback_data="nav_calc")],
            [InlineKeyboardButton(text="🏠 В главное меню", callback_data="nav_start")]
        ])
        await send_and_track(
            msg,
            f"📦 **Расчет юнит-экономики и маржинальности:**\n\n"
            f"• **Цена продажи:** {price:,.0f} ₽\n"
            f"• **Себестоимость и расходы:** {cost:,.0f} ₽\n\n"
            f"✅ **Прибыль с 1 единицы:** **{profit:,.0f} ₽**\n"
            f"📊 **Маржинальность:** **{margin:.1f}%**\n"
            f"📈 **Торговая наценка:** **{markup:.1f}%**\n\n"
            f"💡 *Полные финансовые таблицы с учетом логистики, комиссий и возвратов доступны в практикуме на «Альфа-Курсе».*",
            reply_markup=kb,
            parse_mode="Markdown"
        )
        return True

    elif mode == "bep":
        if len(nums) < 3:
            await send_and_track(msg, "Для расчета точки безубыточности введите 3 числа через пробел: `Постоянные_расходы Средний_чек Переменные_расходы` (например: `150000 3000 1800`).", parse_mode="Markdown")
            USER_CALC_MODE[user_id] = "bep"
            return True
        fixed_costs, avg_check, var_cost = nums[0], nums[1], nums[2]
        unit_margin = avg_check - var_cost
        if unit_margin <= 0:
            await send_and_track(msg, "⚠️ Средний чек должен быть больше переменных расходов на 1 продажу, иначе каждая продажа убыточна.")
            return True
        bep_units = int((fixed_costs / unit_margin) + 0.999)
        bep_revenue = bep_units * avg_check

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎓 Курс: Система продаж и рост выручки", callback_data="mat_mat_sales_paid")],
            [InlineKeyboardButton(text="🧮 Другой расчет", callback_data="nav_calc")],
            [InlineKeyboardButton(text="🏠 В главное меню", callback_data="nav_start")]
        ])
        await send_and_track(
            msg,
            f"⚖️ **Расчет точки безубыточности (ТБУ) на месяц:**\n\n"
            f"• **Постоянные расходы (аренда, ФОТ):** {fixed_costs:,.0f} ₽\n"
            f"• **Средний чек:** {avg_check:,.0f} ₽\n"
            f"• **Маржа с 1 продажи:** {unit_margin:,.0f} ₽\n\n"
            f"✅ **Точка безубыточности в продажах:** **{bep_units} продаж в месяц** (около {max(1, round(bep_units / 30))} в день)\n"
            f"💵 **Минимальная выручка для выхода в ноль:** **{bep_revenue:,.0f} ₽ / мес**\n\n"
            f"💡 *Каждая продажа сверх {bep_units} шт. приносит чистую прибыль {unit_margin:,.0f} ₽.*",
            reply_markup=kb,
            parse_mode="Markdown"
        )
        return True

    return False

# --- ЭКРАН «ВСЁ ОБ ИП» И КАРТОЧКИ МАТЕРИАЛОВ ---
async def send_ip_hub_screen(msg: Message, user_id: int):
    """Упрощенный и наглядный раздел «Всё об ИП» со ссылкой на Альфа-Курс."""
    AWAITING_CUSTOM_GOAL.discard(user_id)
    USER_CALC_MODE.pop(user_id, None)
    await send_and_track(
        msg,
        "🏢 **Всё об ИП: быстрый старт и база «Альфа-Курса»**\n\n"
        "**Как открыть ИП с нуля и без госпошлины - 3 простых шага:**\n"
        "• **Коды и налоги:** выберите ОКВЭД и режим (УСН 6%/15%, Патент или НПД).\n"
        "• **Онлайн-заявка за 0 ₽:** отправьте заявление через сервис Альфа-Банка или Госуслуги без пошлины 800 ₽.\n"
        "• **Счет за 1-3 дня:** получите лист записи ЕГРИП на почту и откройте расчетный счет.\n\n"
        "🔗 Узнать все подробности и скачать шаблоны можно на [платформе «Альфа-Курс»](https://kurs.alfabank.ru/catalog?type=courses&utm_source=tg_bot&utm_medium=ip_hub).\n\n"
        "👇 **Выберите интересующую тему ниже:**",
        reply_markup=ip_hub_kb(),
        parse_mode="Markdown",
        disable_web_page_preview=True
    )

async def send_tax_reminders_menu(msg: Message, user_id: int):
    """Меню выбора напоминания по конкретному налогу."""
    AWAITING_CUSTOM_GOAL.discard(user_id)
    USER_CALC_MODE.pop(user_id, None)
    u = get_user(user_id)
    await send_and_track(
        msg,
        "🔔 **Напоминание по конкретному налогу**\n\n"
        f"Ваш текущий режим в профиле: **{u['tax_regime']}**.\n"
        "Выберите налог или взнос из списка ниже - я сразу покажу точные сроки уплаты и добавлю персональное напоминание:",
        reply_markup=specific_tax_reminders_kb(),
        parse_mode="Markdown"
    )

async def send_material_card(msg: Message, user_id: int, mat_id: str):
    """Показывает карточку материала с кнопкой «Отметить как изученное» и маршрутом."""
    item = get_material_by_id(mat_id)
    if not item:
        await send_and_track(msg, "Материал не найден. Нажмите `/start` для возврата в меню.")
        return

    completed = get_completed_materials(user_id)
    is_done = mat_id in completed
    is_tax_topic = mat_id in ("mat_ip_taxes", "mat_ip_vznosy")

    buttons = [
        [InlineKeyboardButton(text="🔗 Узнать подробнее на «Альфа-Курсе»", url=item["url"])]
    ]
    if is_done:
        buttons.append([InlineKeyboardButton(text="☑️ Уже изучено (Перейти к маршруту)", callback_data="nav_route")])
    else:
        buttons.append([InlineKeyboardButton(text="✅ Отметить как изученное (+ Следующий шаг)", callback_data=f"done_{mat_id}")])

    if is_tax_topic:
        buttons.append([InlineKeyboardButton(text="🔔 Напомнить по конкретному налогу", callback_data="nav_tax_reminders")])
    else:
        buttons.append([InlineKeyboardButton(text="⏰ Напомнить изучить материал", callback_data="nav_reminders")])

    buttons.append([InlineKeyboardButton(text="🗺 Мой маршрут обучения", callback_data="nav_route")])
    buttons.append([InlineKeyboardButton(text="🏠 В главное меню", callback_data="nav_start")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    status_badge = "\n✅ *Статус: Изучено вами*" if is_done else ""

    if item["is_paid"]:
        await send_and_track(
            msg,
            f"🎓 **Платный курс «Альфа-Курса»:**\n**«{item['title']}»**{status_badge}\n\n"
            f"• **Кому подойдет:** {item['target_stage']}\n"
            f"• **Чем полезен курс:** {item['summary']}\n\n"
            f"_🔒 Подробные уроки, видео и расчетные таблицы доступны после приобретения курса на платформе._\n\n"
            f"👇 **Перейдите по ссылке ниже или отметьте материал в маршруте:**",
            reply_markup=kb,
            parse_mode="Markdown"
        )
    else:
        await send_and_track(
            msg,
            f"📖 **{item['title']}**{status_badge}\n\n"
            f"{item['full_text']}\n\n"
            f"👇 **Перейдите на «Альфа-Курс» по кнопке ниже или отметьте тему изученной:**",
            reply_markup=kb,
            parse_mode="Markdown"
        )

@dp.callback_query(F.data.startswith("done_"))
async def cb_mark_material_done(call: CallbackQuery):
    """Отмечает материал изученным и сразу предлагает следующий шаг по маршруту."""
    uid = call.from_user.id
    mat_id = call.data.replace("done_", "", 1)
    mark_material_done(uid, mat_id)

    current_item = get_material_by_id(mat_id)
    completed = get_completed_materials(uid)
    next_item = get_next_recommended_material(uid)
    total = len(CATALOG)

    buttons = []
    if next_item:
        next_Info = f"👉 **Рекомендуемый следующий шаг:**\n**«{next_item['title']}»**\n_{next_item['summary']}_"
        buttons.append([InlineKeyboardButton(text=f"👉 Перейти: {next_item['title'][:34]}...", callback_data=f"mat_{next_item['id']}")])
    else:
        next_Info = "🏆 **Вы полностью прошли все рекомендуемые материалы маршрута!**"

    buttons.append([InlineKeyboardButton(text="🗺 Посмотреть весь маршрут обучения", callback_data="nav_route")])
    buttons.append([InlineKeyboardButton(text="🏠 В главное меню", callback_data="nav_start")])

    title_str = current_item["title"] if current_item else mat_id
    await send_and_track(
        call.message,
        f"✅ **Материал «{title_str}» отмечен как изученный!**\n"
        f"📊 Ваш прогресс на платформе: **{len(completed)} из {total}** тем пройдено.\n\n"
        f"{next_Info}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
        parse_mode="Markdown"
    )
    await call.answer("Отмечено как изученное!")

async def send_profile_screen(msg: Message, user_id: int):
    AWAITING_CUSTOM_GOAL.discard(user_id)
    USER_CALC_MODE.pop(user_id, None)
    u = get_user(user_id)
    completed = get_completed_materials(user_id)
    await send_and_track(
        msg,
        f"📋 **Ваш профиль бизнеса:**\n"
        f"• **Стадия:** {u['stage']}\n"
        f"• **Налоговый режим:** {u['tax_regime']}\n"
        f"• **Текущий фокус:** {u['goal']}\n"
        f"• **Изучено материалов:** {len(completed)} из {len(CATALOG)}\n\n"
        f"_Я учитываю эти данные при построении маршрута обучения и расчете дат налогов._\n"
        f"👇 **Выберите параметр, который хотите изменить:**",
        reply_markup=profile_kb(),
        parse_mode="Markdown"
    )

async def send_help_screen(msg: Message, user_id: int):
    AWAITING_CUSTOM_GOAL.discard(user_id)
    USER_CALC_MODE.pop(user_id, None)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗺 Мой маршрут обучения и статус «Изучено»", callback_data="nav_route")],
        [InlineKeyboardButton(text="⚡ Подбор налогового режима за 3 сек", callback_data="nav_tax_quiz")],
        [InlineKeyboardButton(text="🧮 Быстрые расчеты в чате (УСН, маржа, ТБУ)", callback_data="nav_calc")],
        [InlineKeyboardButton(text="🏢 Всё об ИП: открытие с нуля и гид", callback_data="nav_ip_hub")],
        [InlineKeyboardButton(text="🔔 Напомнить по конкретному налогу", callback_data="nav_tax_reminders")],
        [InlineKeyboardButton(text="📋 Открыть и настроить мой профиль", callback_data="nav_profile")],
        [InlineKeyboardButton(text="⚡ Тест пуш-уведомления (10 сек)", callback_data="nav_demo_push")],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="nav_start")]
    ])
    await send_and_track(
        msg,
        "❓ **Все функции бота «Альфа-Спутник»**\n\n"
        "• **Маршрут обучения (`/route`):** персональный трек материалов со статусом «✅ Изучено» и подсказкой следующего шага.\n"
        "• **Калькулятор налога за 3 сек (`/tax_quiz`):** подбор оптимального налогового режима за 3 клика.\n"
        "• **Быстрые расчеты в чате (`/calc`):** мгновенный расчет налога УСН 6% с вычетом взносов ИП, маржинальности товара и точки безубыточности.\n"
        "• **Поиск по ключевым словам и ИИ-ответы:** напишите слово (`ИП`, `налоги`, `взносы`, `касса`, `маркетплейс`) или свой вопрос.\n"
        "• **Напоминания (`/reminders` и `/demo_push`):** уведомления по конкретным налогам и тест за 10 секунд.\n\n"
        "👇 **Выберите нужный раздел:**",
        reply_markup=kb,
        parse_mode="Markdown"
    )

# --- КОМАНДЫ ---
@dp.message(Command("start"))
async def cmd_start(message: Message):
    track_msg(message.chat.id, message.message_id)
    await clear_user_chat(message.chat.id, message.message_id)
    await send_start_screen(message, message.from_user.id)

@dp.message(Command("route"))
async def cmd_route(message: Message):
    track_msg(message.chat.id, message.message_id)
    await send_learning_route_screen(message, message.from_user.id)

@dp.message(Command("tax_quiz"))
async def cmd_tax_quiz(message: Message):
    track_msg(message.chat.id, message.message_id)
    await send_tax_quiz_start(message, message.from_user.id)

@dp.message(Command("calc"))
async def cmd_calc(message: Message):
    track_msg(message.chat.id, message.message_id)
    await send_calc_menu_screen(message, message.from_user.id)

@dp.message(Command("ip"))
async def cmd_ip(message: Message):
    track_msg(message.chat.id, message.message_id)
    await send_ip_hub_screen(message, message.from_user.id)

@dp.message(Command("profile"))
async def cmd_profile(message: Message):
    track_msg(message.chat.id, message.message_id)
    await send_profile_screen(message, message.from_user.id)

@dp.message(Command("help"))
async def cmd_help(message: Message):
    track_msg(message.chat.id, message.message_id)
    await send_help_screen(message, message.from_user.id)

# --- ОБРАБОТЧИКИ INLINE-КНОПОК НАВИГАЦИИ ---
@dp.callback_query(F.data.startswith("nav_"))
async def cb_navigation(call: CallbackQuery):
    target = call.data.replace("nav_", "")
    uid = call.from_user.id
    if target == "start":
        await send_start_screen(call.message, uid)
    elif target == "ip_hub":
        await send_ip_hub_screen(call.message, uid)
    elif target == "route":
        await send_learning_route_screen(call.message, uid)
    elif target == "tax_quiz":
        await send_tax_quiz_start(call.message, uid)
    elif target == "calc":
        await send_calc_menu_screen(call.message, uid)
    elif target == "tax_reminders":
        await send_tax_reminders_menu(call.message, uid)
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
    if sub == "goal":
        await send_and_track(
            call.message,
            "🎯 **Выберите ваш текущий фокус:**",
            reply_markup=goal_selection_kb(),
            parse_mode="Markdown"
        )
    elif sub == "tax":
        await send_and_track(
            call.message,
            "💼 **Выберите ваш налоговый режим:**",
            reply_markup=tax_selection_kb(),
            parse_mode="Markdown"
        )
    elif sub == "stage":
        await send_and_track(
            call.message,
            "🚀 **Выберите стадию вашего бизнеса:**",
            reply_markup=stage_selection_kb(),
            parse_mode="Markdown"
        )
    await call.answer()

@dp.callback_query(F.data.startswith("stage_"))
async def cb_set_stage(call: CallbackQuery):
    stage_code = call.data.replace("stage_", "")
    await send_stage_response(call.message, call.from_user.id, stage_code)
    await call.answer()

@dp.callback_query(F.data.startswith("tax_"))
async def cb_set_tax(call: CallbackQuery):
    tax_code = call.data.replace("tax_", "")
    uid = call.from_user.id
    tax_name = TAX_MAP.get(tax_code, "УСН")
    update_user(uid, "tax_regime", tax_name)
    await send_and_track(
        call.message,
        f"✅ Налоговый режим обновлен на **{tax_name}**. Теперь я буду учитывать его в подсказках и календаре!",
        parse_mode="Markdown"
    )
    await send_profile_screen(call.message, uid)
    await call.answer()

@dp.callback_query(F.data.startswith("addtaxrem_"))
async def cb_add_specific_tax_reminder(call: CallbackQuery):
    """Создает напоминание по конкретному выбранному налогу."""
    tax_key = call.data.replace("addtaxrem_", "")
    uid = call.from_user.id
    info = SPECIFIC_TAX_REMINDERS.get(tax_key)
    if not info:
        await call.answer("Налог не найден")
        return

    if info["regime"]:
        update_user(uid, "tax_regime", info["regime"])

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT INTO reminders (user_id, title, frequency, active) VALUES (?, ?, ?, 1)",
            (uid, info["reminder_title"], "weekly")
        )
        rem_id = cur.lastrowid

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Раз в день", callback_data=f"freq_{rem_id}_daily"),
            InlineKeyboardButton(text="Раз в 3 дня", callback_data=f"freq_{rem_id}_every_3_days"),
            InlineKeyboardButton(text="Раз в неделю", callback_data=f"freq_{rem_id}_weekly")
        ],
        [InlineKeyboardButton(text="⚡ Проверить отправку сейчас (10 сек)", callback_data="nav_demo_push")],
        [InlineKeyboardButton(text="🔕 Отключить напоминание", callback_data=f"freq_{rem_id}_off")],
        [InlineKeyboardButton(text="⬅️ Выбрать другой налог", callback_data="nav_tax_reminders")]
    ])

    await send_and_track(
        call.message,
        f"✅ **Напоминание по налогу «{info['name']}» включено!**\n\n"
        f"📅 **Сроки уплаты:** {info['schedule_info']}\n"
        f"🔔 **Частота по умолчанию:** Раз в неделю.\n\n"
        f"Вы можете сразу изменить частоту напоминаний или протестировать уведомление кнопками ниже:",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    await call.answer("Напоминание создано!")

@dp.callback_query(F.data == "goal_custom")
async def cb_goal_custom(call: CallbackQuery):
    uid = call.from_user.id
    AWAITING_CUSTOM_GOAL.add(uid)
    await send_and_track(
        call.message,
        "✍️ **Напишите в чат вашу текущую цель или задачу** (например: *«Открыть ИП для кофейни»* или *«Снизить налоги законно»*), и я сохраню её в ваш профиль:",
        parse_mode="Markdown"
    )
    await call.answer()

@dp.callback_query(F.data.startswith("goal_"))
async def cb_set_goal(call: CallbackQuery):
    goal_code = call.data.replace("goal_", "")
    uid = call.from_user.id
    AWAITING_CUSTOM_GOAL.discard(uid)
    goal_name = GOAL_MAP.get(goal_code, "Развитие бизнеса")
    update_user(uid, "goal", goal_name)
    await send_and_track(
        call.message,
        f"✅ Текущий фокус успешно изменен на: **{goal_name}**!",
        parse_mode="Markdown"
    )
    await send_profile_screen(call.message, uid)
    await call.answer()

@dp.callback_query(F.data.startswith("mat_"))
async def cb_open_material(call: CallbackQuery):
    mat_id = call.data.replace("mat_", "", 1)
    await send_material_card(call.message, call.from_user.id, mat_id)
    await call.answer()

# --- СИСТЕМА НАПОМИНАНИЙ ---
async def send_scheduled_reminder(user_id: int, text: str):
    # После срабатывания 10-секундного пуша удаляем (отключаем) напоминание о прохождении обучения
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE reminders SET active = 0, frequency = 'off' WHERE user_id = ? AND title LIKE '%обучен%'",
            (user_id,)
        )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📚 Перейти на Альфа-Курс",
            url="https://kurs.alfabank.ru/catalog?type=courses&utm_source=tg_bot&utm_medium=reminder"
        )]
    ])
    sent = await bot.send_message(
        user_id,
        f"🔔 **Напоминание от Альфа-Спутника:**\n{text}",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    track_msg(user_id, sent.message_id)

async def trigger_demo_push(msg: Message, user_id: int):
    u = get_user(user_id)
    run_date = datetime.now() + timedelta(seconds=10)
    scheduler.add_job(
        send_scheduled_reminder,
        "date",
        run_date=run_date,
        args=[
            user_id,
            f"Пора уделить 15 минут обучению по вашему фокусу «{u['goal']}»! "
            f"Также проверьте календарь отчетности по режиму «{u['tax_regime']}»."
        ]
    )
    await send_and_track(
        msg,
        "⚡ **Демо-режим:** Разовое напоминание запланировано и придет ровно через **10 секунд**!",
        parse_mode="Markdown"
    )

@dp.message(Command("demo_push"))
async def cmd_demo_push(message: Message):
    track_msg(message.chat.id, message.message_id)
    await trigger_demo_push(message, message.from_user.id)

@dp.message(Command("reminders"))
@dp.callback_query(F.data == "menu_reminders")
async def show_reminders(event: Message | CallbackQuery):
    user_id = event.from_user.id
    u = get_user(user_id)
    if isinstance(event, Message):
        track_msg(event.chat.id, event.message_id)
        msg = event
    else:
        msg = event.message

    with sqlite3.connect(DB_PATH) as conn:
        total_cur = conn.execute("SELECT COUNT(*) FROM reminders WHERE user_id = ?", (user_id,))
        total_count = total_cur.fetchone()[0]

        if total_count == 0:
            conn.execute(
                "INSERT INTO reminders (user_id, title, frequency, active) VALUES (?, ?, ?, 1)",
                (user_id, "Продолжить обучение на «Альфа-Курсе» (15 мин)", "every_3_days")
            )
            conn.execute(
                "INSERT INTO reminders (user_id, title, frequency, active) VALUES (?, ?, ?, 1)",
                (user_id, f"Проверка сроков уплаты налога ({u['tax_regime']})", "weekly")
            )

        cur = conn.execute(
            "SELECT id, title, frequency FROM reminders WHERE user_id = ? AND active = 1 AND frequency != 'off'",
            (user_id,)
        )
        rows = cur.fetchall()

    freq_map = {
        "once": "Разовое",
        "daily": "Раз в день",
        "every_3_days": "Раз в 3 дня",
        "weekly": "Раз в неделю"
    }

    if not rows:
        await send_and_track(
            msg,
            "📭 **У вас сейчас нет активных напоминаний.**",
            parse_mode="Markdown"
        )
    else:
        for r_id, title, freq in rows:
            await send_and_track(
                msg,
                f"⏰ **{title}**\nСтатус: 🟢 Активно ({freq_map.get(freq, freq)})",
                reply_markup=reminder_control_kb(r_id),
                parse_mode="Markdown"
            )

    extra_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔔 Добавить напоминание по конкретному налогу", callback_data="nav_tax_reminders")],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="nav_start")]
    ])
    await send_and_track(msg, "Хотите добавить напоминание по конкретному налогу или взносу?", reply_markup=extra_kb)

    if isinstance(event, CallbackQuery):
        await call_answer_safe(event)

async def call_answer_safe(call: CallbackQuery, text: str | None = None):
    try:
        await call.answer(text)
    except Exception:
        pass

@dp.callback_query(F.data.startswith("freq_"))
async def cb_change_freq(call: CallbackQuery):
    _, rem_id, new_freq = call.data.split("_", 2)
    active = 0 if new_freq == "off" else 1
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE reminders SET frequency = ?, active = ? WHERE id = ?", (new_freq, active, int(rem_id)))

    if new_freq == "off":
        try:
            await call.message.delete()
        except Exception:
            await call.message.edit_text("🔕 Напоминание отключено и удалено из списка.")
        await call.answer("Напоминание отключено!")
        return

    freq_map = {"daily": "Раз в день", "every_3_days": "Раз в 3 дня", "weekly": "Раз в неделю"}
    await call.message.edit_text(
        f"✅ Настройки напоминания обновлены!\nНовая частота: **{freq_map.get(new_freq)}**.",
        reply_markup=reminder_control_kb(int(rem_id)),
        parse_mode="Markdown"
    )
    await call.answer("Частота обновлена!")

# --- ГЛАВНЫЙ ОБРАБОТЧИК СООБЩЕНИЙ, КАЛЬКУЛЯТОРОВ И ПОИСКА ---
@dp.message()
async def handle_question(message: Message):
    user_id = message.from_user.id
    track_msg(message.chat.id, message.message_id)
    text = (message.text or "").strip()
    if not text:
        return

    # 1. Если пользователь вводит собственный «Текущий фокус» текстом
    if user_id in AWAITING_CUSTOM_GOAL:
        AWAITING_CUSTOM_GOAL.discard(user_id)
        clean_goal = text[:80].replace("—", "-").replace("–", "-")
        update_user(user_id, "goal", clean_goal)
        await send_and_track(message, f"✅ Ваш персональный фокус сохранен: **{clean_goal}**!", parse_mode="Markdown")
        await send_profile_screen(message, user_id)
        return

    # 2. Если пользователь находится в режиме финансового калькулятора и ввел числа
    if user_id in USER_CALC_MODE:
        handled = await process_calculation_input(message, user_id, text)
        if handled:
            return

    # 3. Автоматическое обогащение профиля
    auto_extract_profile(user_id, text)
    profile = get_user(user_id)
    t_low = text.lower()

    # 4. Быстрые переходы по ключевым командам в тексте
    if t_low in ["ип", "открыть ип", "про ип", "все об ип", "всё об ип"]:
        await send_ip_hub_screen(message, user_id)
        return
    if any(w in t_low for w in ["калькулятор", "посчитай", "расчет", "расчёт", "тбу", "безубыточност"]):
        await send_calc_menu_screen(message, user_id)
        return
    if any(w in t_low for w in ["маршрут", "мой трек", "изучено", "прогресс"]):
        await send_learning_route_screen(message, user_id)
        return

    # 5. Перехват запроса на напоминание о налоге
    if any(w in t_low for w in ["напомни", "напоминание", "срок уплаты"]):
        if "налог" in t_low or "взнос" in t_low or "усн" in t_low or "нпд" in t_low or "патент" in t_low:
            await send_tax_reminders_menu(message, user_id)
            return
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO reminders (user_id, title, frequency, active) VALUES (?, ?, ?, 1)",
                (user_id, f"Задача из чата: {text[:50]}", "once")
            )
        await send_and_track(
            message,
            "✅ Я создал напоминание по вашему запросу.\n"
            "Управлять частотой или отключить его можно через `/reminders`, "
            "а проверить мгновенную отправку - через `/demo_push`.",
            parse_mode="Markdown"
        )
        return

    # 6. ПОИСК ПО КЛЮЧЕВЫМ СЛОВАМ (с учетом изученных материалов)
    matches = search_by_keyword(text, user_id=user_id)
    completed = get_completed_materials(user_id)
    if len(matches) >= 2 and len(text.split()) <= 4:
        buttons = []
        lines = [f"🔎 **По запросу «{text}» найдены материалы в «Альфа-Курсе»:**\n"]
        for item in matches[:5]:
            done_mark = "✅ [Изучено] " if item["id"] in completed else ""
            badge = "🎓 [Платный курс]" if item["is_paid"] else "📖 [Бесплатно]"
            lines.append(f"• {done_mark}{badge} **{item['title']}**\n_{item['summary']}_\n")
            btn_prefix = "✅" if item["id"] in completed else "📖"
            buttons.append([InlineKeyboardButton(text=f"{btn_prefix} {item['title'][:38]}...", callback_data=f"mat_{item['id']}")])

        if any(w in t_low for w in ["налог", "усн", "нпд", "патент", "взнос"]):
            buttons.append([InlineKeyboardButton(text="⚡ Подобрать налог за 3 сек", callback_data="nav_tax_quiz")])
            buttons.append([InlineKeyboardButton(text="🔔 Напомнить по конкретному налогу", callback_data="nav_tax_reminders")])
        buttons.append([InlineKeyboardButton(text="🗺 Мой маршрут обучения", callback_data="nav_route")])

        lines.append("👇 **Выберите подходящий материал кнопкой ниже:**")
        await send_and_track(
            message,
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="Markdown"
        )
        return

    # 7. Обращение к YandexGPT (для развернутых вопросов)
    system_prompt = (
        "Ты - ИИ-помощник образовательной платформы «Альфа-Курс» в Telegram.\n"
        f"Профиль предпринимателя: Стадия={profile['stage']}, Налоги={profile['tax_regime']}, Текущий фокус={profile['goal']}.\n"
        "ПРАВИЛА:\n"
        "1. Отвечай кратко, просто и ТОЛЬКО на основе предоставленной ниже базы знаний.\n"
        "2. Не используй нумерованные списки для вариантов меню и используй только короткое тире (-).\n"
        "3. Если вопрос слишком широкий или неясный - задай 1-2 конкретных уточняющих вопроса.\n"
        "4. СТРОГОЕ ОГРАНИЧЕНИЕ: Для ПЛАТНЫХ курсов никогда не раскрывай внутреннее содержание - "
        "кратко объясни пользу и предложи узнать подробнее по ссылке на «Альфа-Курс».\n"
        "5. В конце ответа всегда предлагай понятный следующий шаг и ссылку на «Альфа-Курс».\n\n"
        f"БАЗА ЗНАНИЙ:\n{build_safe_context()}"
    )

    yandex_answer = await ask_yandex_gpt(system_prompt, text)
    if yandex_answer:
        await send_and_track(message, yandex_answer, parse_mode="Markdown")
        return

    # 8. Если найден 1 точный материал - показываем его
    if len(matches) == 1:
        await send_material_card(message, user_id, matches[0]["id"])
        return

    # 9. Если точного совпадения нет
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ Подбор налогового режима за 3 сек", callback_data="nav_tax_quiz")],
        [InlineKeyboardButton(text="🧮 Быстрые расчеты в чате (УСН, маржа, ТБУ)", callback_data="nav_calc")],
        [InlineKeyboardButton(text="🗺 Мой маршрут обучения", callback_data="nav_route")],
        [InlineKeyboardButton(text="🏢 Всё об ИП: открытие с нуля и гид", callback_data="nav_ip_hub")],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="nav_start")]
    ])
    await send_and_track(
        message,
        f"В базе знаний «Альфа-Курса» нет точного совпадения по запросу «{text}».\n\n"
        "💡 **Попробуйте написать ключевое слово** (например: `ИП`, `налоги`, `взносы`, `касса`, `маркетплейс`) "
        "или воспользуйтесь интерактивными инструментами ниже:",
        reply_markup=kb,
        parse_mode="Markdown"
    )

async def main():
    init_db()
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
