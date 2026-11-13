import asyncio
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

# Пользователи, которые сейчас вводят собственный фокус текстом
AWAITING_CUSTOM_GOAL = set()

# --- СПРАВОЧНИКИ (короткие коды для callback_data) ---
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

# Справочник напоминаний по конкретным налогам
SPECIFIC_TAX_REMINDERS = {
    "usn": {
        "name": "УСН (авансы и годовой налог)",
        "regime": "УСН (Упрощенная система)",
        "schedule_info": "Уведомление — до 25 числа, уплата квартальных авансов — до 28 апреля, 28 июля, 28 октября. Годовой налог — до 28 апреля.",
        "reminder_title": "Уплата налога по УСН (уведомление до 25-го, платеж до 28-го числа)"
    },
    "npd": {
        "name": "НПД (Налог на профдоход)",
        "regime": "НПД (Самозанятый)",
        "schedule_info": "Квитанция формируется в «Мой налог» до 12 числа, уплата налога — ежемесячно до 28 числа.",
        "reminder_title": "Уплата налога НПД в приложении «Мой налог» (до 28 числа)"
    },
    "psn": {
        "name": "Патент (ПСН)",
        "regime": "Патент (ПСН)",
        "schedule_info": "Патент до 6 мес. оплачивается целиком до конца срока. От 6 до 12 мес.: 1/3 — в первые 90 дней, 2/3 — до конца действия патента.",
        "reminder_title": "Контроль оплаты Патента (ПСН) и подачи уведомления на уменьшение"
    },
    "vznosy": {
        "name": "Страховые взносы ИП за себя",
        "regime": None,
        "schedule_info": "Фиксированные взносы ИП — до 28 декабря текущего года. Дополнительный взнос 1% с дохода свыше 300 000 ₽ — до 1 июля следующего года.",
        "reminder_title": "Уплата страховых взносов ИП (до 28 декабря / 1% до 1 июля)"
    },
    "ausn": {
        "name": "АУСН (Автоматизированная УСН)",
        "regime": "АУСН",
        "schedule_info": "ФНС рассчитывает налог автоматически до 15 числа, уплата — ежемесячно до 25 числа.",
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
            "📌 **Как открыть ИП с нуля и без госпошлины — коротко о главном:**\n\n"
            "• **Подберите ОКВЭД и налоговый режим:** выберите коды деятельности и сразу подайте уведомление на УСН или НПД, чтобы не остаться на сложной ОСНО.\n"
            "• **Подайте заявку онлайн за 0 ₽:** при электронной подаче паспорта и ИНН (через сервис Альфа-Банка, Госуслуги или ФНС) госпошлина 800 ₽ не платится.\n"
            "• **Получите документы и откройте счет:** лист записи ЕГРИП придет на почту за 1–3 дня, после чего можно сразу принимать оплату.\n\n"
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
            "• **НПД (4–6%):** до 2,4 млн ₽ в год, без сотрудников и без обязательных взносов. Уплата — до 28 числа.\n"
            "• **УСН «Доходы» (6%):** для услуг и небольших расходов. Уменьшается на взносы ИП. Квартальные авансы — до 28 числа.\n"
            "• **УСН «Доходы минус расходы» (15%):** выгоден, если подтвержденные расходы превышают 60–70% выручки.\n"
            "• **Патент (ПСН):** фиксированный налог для розницы, услуг и общепита (до 60 млн ₽ и до 15 сотрудников).\n"
            "• **АУСН (8% или 20%):** налог рассчитывается автоматически по счету, без деклараций (уплата до 25 числа).\n\n"
            "🔔 *Ниже вы можете сразу включить напоминание по вашему конкретному налогу или изучить подробный разбор на «Альфа-Курсе».*"
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
            "• **Дополнительный взнос 1%:** с дохода свыше 300 000 ₽ в год — уплачивается до **1 июля** следующего года.\n"
            "• **Декларация по УСН для ИП:** подается один раз в год до **25 апреля**, уплата годового налога — до **28 апреля**.\n\n"
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
            "• **Экономия на комиссиях:** прием платежей по QR-коду (СБП) обходится в 0,4–0,7% вместо стандартного эквайринга.\n"
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
                frequency TEXT,
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

# --- ПОИСК ПО КЛЮЧЕВЫМ СЛОВАМ ---
def search_by_keyword(query: str) -> list[dict]:
    q_words = [w.strip(".,!?\"'()") for w in query.lower().split() if len(w.strip(".,!?\"'()")) >= 2]
    if not q_words:
        return []
    results = []
    for item in CATALOG:
        searchable = (
            f"{item['title']} {item['category']} {' '.join(item.get('keywords', []))} "
            f"{item['summary']} {item['full_text'] if not item['is_paid'] else ''}"
        ).lower()
        score = sum(2 if w in item.get("keywords", []) or w in item["title"].lower() else 1 for w in q_words if w in searchable)
        if score > 0:
            results.append((score, item))
    results.sort(key=lambda x: x[0], reverse=True)
    return [r[1] for r in results]

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
                return data["result"]["alternatives"][0]["message"]["text"]
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

# --- КЛАВИАТУРЫ (БЕЗ ЦИФР) ---
def start_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Планирую открыть дело", callback_data="stage_1")],
        [InlineKeyboardButton(text="🛠 Решаю задачу в микробизнесе", callback_data="stage_2")],
        [InlineKeyboardButton(text="📈 Хочу системно развить бизнес", callback_data="stage_3")],
        [InlineKeyboardButton(text="🏢 Всё об ИП: открытие с нуля и налоги", callback_data="nav_ip_hub")],
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
        [InlineKeyboardButton(text="🚀 Изменить стадию бизнеса", callback_data="submenu_stage")],
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
        [InlineKeyboardButton(text="📊 Налоги для ИП: выбор режима", callback_data="mat_mat_ip_taxes")],
        [InlineKeyboardButton(text="🔔 Напомнить по конкретному налогу", callback_data="nav_tax_reminders")],
        [InlineKeyboardButton(text="💳 Страховые взносы ИП и отчетность", callback_data="mat_mat_ip_vznosy")],
        [InlineKeyboardButton(text="🧾 Счет, онлайн-касса и вывод денег", callback_data="mat_mat_ip_kassa")],
        [InlineKeyboardButton(text="🎓 Курс: Выход ИП на маркетплейсы", callback_data="mat_mat_mp_paid")],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="nav_start")]
    ])

def specific_tax_reminders_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📌 УСН (авансы и налог — до 28 числа)", callback_data="addtaxrem_usn")],
        [InlineKeyboardButton(text="📌 НПД / Самозанятость (до 28 числа)", callback_data="addtaxrem_npd")],
        [InlineKeyboardButton(text="📌 Патент / ПСН (контроль сроков оплаты)", callback_data="addtaxrem_psn")],
        [InlineKeyboardButton(text="📌 Страховые взносы ИП (28 дек / 1 июля)", callback_data="addtaxrem_vznosy")],
        [InlineKeyboardButton(text="📌 АУСН (ежемесячно до 25 числа)", callback_data="addtaxrem_ausn")],
        [InlineKeyboardButton(text="⏰ Мои активные напоминания", callback_data="nav_reminders")],
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

# --- ЭКРАНЫ БОТА ---
async def send_start_screen(msg: Message, user_id: int):
    AWAITING_CUSTOM_GOAL.discard(user_id)
    get_user(user_id)
    await msg.answer(
        "Привет! Я **Альфа-Спутник** — ваш ИИ-помощник по платформе «Альфа-Курс».\n\n"
        "• Ищу материалы по **ключевым словам** (напишите, например, `ИП`, `налоги` или `маркетплейс`)\n"
        "• Подбираю короткие инструкции и курсы под вашу задачу\n"
        "• Настраиваю напоминания об учебе и по конкретным налогам\n\n"
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
            [InlineKeyboardButton(text="📊 Сравнение налогов для старта (УСН / НПД)", callback_data="mat_mat_ip_taxes")],
            [InlineKeyboardButton(text="🔔 Настроить напоминание по налогу", callback_data="nav_tax_reminders")],
            [InlineKeyboardButton(text="🏢 Открыть раздел «Всё об ИП»", callback_data="nav_ip_hub")],
            [InlineKeyboardButton(text="🏠 В главное меню", callback_data="nav_start")]
        ])
        await msg.answer(
            f"🚀 **Стадия «{stage_title}» сохранена в профиль.**\n\n"
            "Для быстрого старта без штрафов рекомендуем:\n"
            "• Определить форму бизнеса и коды ОКВЭД\n"
            "• Сразу выбрать выгодный налоговый режим (УСН или НПД)\n"
            "• Подать заявку онлайн без уплаты госпошлины 800 ₽\n\n"
            "👇 **С чего начнем?**",
            reply_markup=kb,
            parse_mode="Markdown"
        )

    elif stage_code == "2":
        update_user(user_id, "goal", "Налоги, учет и отчетность")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Страховые взносы ИП и уменьшение налога", callback_data="mat_mat_ip_vznosy")],
            [InlineKeyboardButton(text="🔔 Напомнить по конкретному налогу", callback_data="nav_tax_reminders")],
            [InlineKeyboardButton(text="🧾 Онлайн-касса (54-ФЗ), СБП и вывод денег", callback_data="mat_mat_ip_kassa")],
            [InlineKeyboardButton(text="🛍 Курс: Юнит-экономика на маркетплейсах", callback_data="mat_mat_mp_paid")],
            [InlineKeyboardButton(text="🏠 В главное меню", callback_data="nav_start")]
        ])
        await msg.answer(
            f"🛠 **Стадия «{stage_title}» сохранена.**\n\n"
            "Подборка практических решений для действующего микробизнеса:\n"
            "• Как уменьшить налог УСН или Патент за счет взносов ИП\n"
            "• Как настроить напоминание по вашему конкретному налогу\n"
            "• Как работать с кассой и считать маржинальность на маркетплейсах\n\n"
            "👇 **Выберите нужную тему:**",
            reply_markup=kb,
            parse_mode="Markdown"
        )

    else:
        update_user(user_id, "goal", "Систематизация и найм команды")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👥 Курс: Построение отдела продаж и найм", callback_data="mat_mat_sales_paid")],
            [InlineKeyboardButton(text="📈 Курс: Масштабирование на маркетплейсах", callback_data="mat_mat_mp_paid")],
            [InlineKeyboardButton(text="🎯 Настроить свой фокус развития", callback_data="submenu_goal")],
            [InlineKeyboardButton(text="🏠 В главное меню", callback_data="nav_start")]
        ])
        await msg.answer(
            f"📈 **Стадия «{stage_title}» сохранена.**\n\n"
            "Материалы «Альфа-Курса» для системного роста:\n"
            "• Оформление первых сотрудников в ИП/ООО и мотивация команды\n"
            "• Внедрение CRM, скриптов продаж и контроль конверсии\n"
            "• Финансовое планирование и масштабирование на маркетплейсах\n\n"
            "👇 **Выберите программу для изучения:**",
            reply_markup=kb,
            parse_mode="Markdown"
        )

async def send_ip_hub_screen(msg: Message, user_id: int):
    """Упрощенный и наглядный раздел «Всё об ИП» со ссылкой на Альфа-Курс."""
    AWAITING_CUSTOM_GOAL.discard(user_id)
    await msg.answer(
        "🏢 **Всё об ИП: быстрый старт и база «Альфа-Курса»**\n\n"
        "**Как открыть ИП с нуля и без госпошлины — 3 простых шага:**\n"
        "• **Коды и налоги:** выберите ОКВЭД и режим (УСН 6%/15%, Патент или НПД).\n"
        "• **Онлайн-заявка за 0 ₽:** отправьте заявление через сервис Альфа-Банка или Госуслуги без пошлины 800 ₽.\n"
        "• **Счет за 1–3 дня:** получите лист записи ЕГРИП на почту и откройте расчетный счет.\n\n"
        "🔗 Узнать все подробности и скачать шаблоны можно на [платформе «Альфа-Курс»](https://kurs.alfabank.ru/catalog?type=courses&utm_source=tg_bot&utm_medium=ip_hub).\n\n"
        "👇 **Выберите интересующую тему ниже:**",
        reply_markup=ip_hub_kb(),
        parse_mode="Markdown",
        disable_web_page_preview=True
    )

async def send_tax_reminders_menu(msg: Message, user_id: int):
    """Меню выбора напоминания по конкретному налогу."""
    AWAITING_CUSTOM_GOAL.discard(user_id)
    u = get_user(user_id)
    await msg.answer(
        "🔔 **Напоминание по конкретному налогу**\n\n"
        f"Ваш текущий режим в профиле: **{u['tax_regime']}**.\n"
        "Выберите налог или взнос из списка ниже — я сразу покажу точные сроки уплаты и добавлю персональное напоминание:",
        reply_markup=specific_tax_reminders_kb(),
        parse_mode="Markdown"
    )

async def send_material_card(msg: Message, user_id: int, mat_id: str):
    """Показывает карточку материала (с кнопкой напоминания по конкретному налогу в налоговых темах)."""
    item = get_material_by_id(mat_id)
    if not item:
        await msg.answer("Материал не найден. Нажмите `/start` для возврата в меню.")
        return

    is_tax_topic = mat_id in ("mat_ip_taxes", "mat_ip_vznosy")
    buttons = [
        [InlineKeyboardButton(text="🔗 Узнать подробнее на «Альфа-Курсе»", url=item["url"])]
    ]
    if is_tax_topic:
        buttons.append([InlineKeyboardButton(text="🔔 Напомнить по конкретному налогу", callback_data="nav_tax_reminders")])
    else:
        buttons.append([InlineKeyboardButton(text="⏰ Напомнить изучить материал", callback_data="nav_reminders")])

    buttons.append([InlineKeyboardButton(text="🏢 В раздел «Всё об ИП»", callback_data="nav_ip_hub")])
    buttons.append([InlineKeyboardButton(text="🏠 В главное меню", callback_data="nav_start")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    if item["is_paid"]:
        await msg.answer(
            f"🎓 **Платный курс «Альфа-Курса»:**\n**«{item['title']}»**\n\n"
            f"• **Кому подойдет:** {item['target_stage']}\n"
            f"• **Чем полезен курс:** {item['summary']}\n\n"
            f"_🔒 Подробные уроки, видео и расчетные таблицы доступны после приобретения курса на платформе._\n\n"
            f"👇 **Перейдите по ссылке ниже, чтобы узнать подробнее:**",
            reply_markup=kb,
            parse_mode="Markdown"
        )
    else:
        await msg.answer(
            f"📖 **{item['title']}**\n\n"
            f"{item['full_text']}\n\n"
            f"👇 **Перейдите на «Альфа-Курс» по кнопке ниже, чтобы узнать подробнее:**",
            reply_markup=kb,
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
        f"_Я учитываю эти данные при подборе курсов и расчете дат налогов._\n"
        f"👇 **Выберите параметр, который хотите изменить:**",
        reply_markup=profile_kb(),
        parse_mode="Markdown"
    )

async def send_help_screen(msg: Message, user_id: int):
    AWAITING_CUSTOM_GOAL.discard(user_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏢 Всё об ИП: открытие с нуля и гид", callback_data="nav_ip_hub")],
        [InlineKeyboardButton(text="🔔 Напомнить по конкретному налогу", callback_data="nav_tax_reminders")],
        [InlineKeyboardButton(text="📋 Открыть и настроить мой профиль", callback_data="nav_profile")],
        [InlineKeyboardButton(text="⏰ Управление напоминаниями", callback_data="nav_reminders")],
        [InlineKeyboardButton(text="⚡ Тест пуш-уведомления (10 сек)", callback_data="nav_demo_push")],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="nav_start")]
    ])
    await msg.answer(
        "❓ **Все функции бота «Альфа-Спутник»**\n\n"
        "• **Поиск по ключевым словам и ИИ-ответы:** напишите слово (например, `ИП`, `налоги`, `взносы`, `касса`, `маркетплейс`) — бот предложит подходящие материалы из «Альфа-Курса».\n"
        "• **Раздел «Всё об ИП» (`/ip`):** краткий гид по открытию ИП с нуля без госпошлины и прямые ссылки на «Альфа-Курс».\n"
        "• **Напоминания по конкретному налогу:** выберите УСН, НПД, Патент, взносы ИП или АУСН, чтобы вовремя получать уведомления о сроках уплаты.\n"
        "• **Профиль бизнеса (`/profile`):** настройка стадии бизнеса, налогового режима и вашего **Текущего фокуса**.\n"
        "• **Демо-пуш (`/demo_push`):** проверка живого уведомления за 10 секунд.\n\n"
        "👇 **Выберите нужный раздел:**",
        reply_markup=kb,
        parse_mode="Markdown"
    )

# --- КОМАНДЫ ---
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await send_start_screen(message, message.from_user.id)

@dp.message(Command("ip"))
async def cmd_ip(message: Message):
    await send_ip_hub_screen(message, message.from_user.id)

@dp.message(Command("profile"))
async def cmd_profile(message: Message):
    await send_profile_screen(message, message.from_user.id)

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await send_help_screen(message, message.from_user.id)

# --- ОБРАБОТЧИКИ INLINE-КНОПОК ---
@dp.callback_query(F.data.startswith("nav_"))
async def cb_navigation(call: CallbackQuery):
    target = call.data.replace("nav_", "")
    uid = call.from_user.id
    if target == "start":
        await send_start_screen(call.message, uid)
    elif target == "ip_hub":
        await send_ip_hub_screen(call.message, uid)
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
        await call.message.answer(
            "🎯 **Выберите ваш текущий фокус:**",
            reply_markup=goal_selection_kb(),
            parse_mode="Markdown"
        )
    elif sub == "tax":
        await call.message.answer(
            "💼 **Выберите ваш налоговый режим:**",
            reply_markup=tax_selection_kb(),
            parse_mode="Markdown"
        )
    elif sub == "stage":
        await call.message.answer(
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
    await call.message.answer(
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

    await call.message.answer(
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
    await call.message.answer(
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
    await call.message.answer(
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
            f"Пора уделить 15 минут обучению по вашему фокусу «{u['goal']}»! "
            f"Также проверьте календарь отчетности по режиму «{u['tax_regime']}»."
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
                (user_id, f"Проверка сроков уплаты налога ({u['tax_regime']})", "weekly")
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

    extra_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔔 Добавить напоминание по конкретному налогу", callback_data="nav_tax_reminders")],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="nav_start")]
    ])
    await msg.answer("Хотите добавить напоминание по конкретному налогу или взносу?", reply_markup=extra_kb)

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

# --- ГЛАВНЫЙ ОБРАБОТЧИК СООБЩЕНИЙ И ПОИСКА ПО КЛЮЧЕВЫМ СЛОВАМ ---
@dp.message()
async def handle_question(message: Message):
    user_id = message.from_user.id
    text = (message.text or "").strip()
    if not text:
        return

    # 1. Если пользователь вводит собственный «Текущий фокус» текстом
    if user_id in AWAITING_CUSTOM_GOAL:
        AWAITING_CUSTOM_GOAL.discard(user_id)
        update_user(user_id, "goal", text[:80])
        await message.answer(f"✅ Ваш персональный фокус сохранен: **{text[:80]}**!", parse_mode="Markdown")
        await send_profile_screen(message, user_id)
        return

    # 2. Автоматическое обогащение профиля
    auto_extract_profile(user_id, text)
    profile = get_user(user_id)
    t_low = text.lower()

    # 3. Если пользователь написал «ИП» — открываем лаконичный хаб по ИП
    if t_low in ["ип", "открыть ип", "про ип", "все об ип", "всё об ип"]:
        await send_ip_hub_screen(message, user_id)
        return

    # 4. Перехват запроса на напоминание о налоге
    if any(w in t_low for w in ["напомни", "напоминание", "срок уплаты"]):
        if "налог" in t_low or "взнос" in t_low or "усн" in t_low or "нпд" in t_low or "патент" in t_low:
            await send_tax_reminders_menu(message, user_id)
            return
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO reminders (user_id, title, frequency) VALUES (?, ?, ?)",
                (user_id, f"Задача из чата: {text[:50]}", "once")
            )
        await message.answer(
            f"✅ Я создал напоминание по вашему запросу.\n"
            "Управлять частотой или отключить его можно через `/reminders`, "
            "а проверить мгновенную отправку — через `/demo_push`.",
            parse_mode="Markdown"
        )
        return

    # 5. ПОИСК ПО КЛЮЧЕВЫМ СЛОВАМ (без цифр в кнопках)
    matches = search_by_keyword(text)
    if len(matches) >= 2 and len(text.split()) <= 4:
        buttons = []
        lines = [f"🔎 **По запросу «{text}» найдены материалы в «Альфа-Курсе»:**\n"]
        for item in matches[:5]:
            badge = "🎓 [Платный курс]" if item["is_paid"] else "📖 [Бесплатно]"
            lines.append(f"• {badge} **{item['title']}**\n_{item['summary']}_\n")
            buttons.append([InlineKeyboardButton(text=f"📖 {item['title'][:40]}...", callback_data=f"mat_{item['id']}")])

        if any(w in t_low for w in ["налог", "усн", "нпд", "патент", "взнос"]):
            buttons.append([InlineKeyboardButton(text="🔔 Напомнить по конкретному налогу", callback_data="nav_tax_reminders")])
        buttons.append([InlineKeyboardButton(text="🏢 Раздел «Всё об ИП с нуля»", callback_data="nav_ip_hub")])

        lines.append("👇 **Выберите подходящий материал кнопкой ниже:**")
        await message.answer(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="Markdown"
        )
        return

    # 6. Обращение к YandexGPT (для развернутых вопросов)
    system_prompt = (
        "Ты — ИИ-помощник образовательной платформы «Альфа-Курс» в Telegram.\n"
        f"Профиль предпринимателя: Стадия={profile['stage']}, Налоги={profile['tax_regime']}, Текущий фокус={profile['goal']}.\n"
        "ПРАВИЛА:\n"
        "1. Отвечай кратко, просто и ТОЛЬКО на основе предоставленной ниже базы знаний.\n"
        "2. Не используй нумерованные списки для вариантов меню.\n"
        "3. Если вопрос слишком широкий или неясный — задай 1-2 конкретных уточняющих вопроса.\n"
        "4. СТРОГОЕ ОГРАНИЧЕНИЕ: Для ПЛАТНЫХ курсов никогда не раскрывай внутреннее содержание — "
        "кратко объясни пользу и предложи узнать подробнее по ссылке на «Альфа-Курс».\n"
        "5. В конце ответа всегда предлагай понятный следующий шаг и ссылку на «Альфа-Курс».\n\n"
        f"БАЗА ЗНАНИЙ:\n{build_safe_context()}"
    )

    yandex_answer = await ask_yandex_gpt(system_prompt, text)
    if yandex_answer:
        await message.answer(yandex_answer, parse_mode="Markdown")
        return

    # 7. Если найден 1 точный материал — показываем его
    if len(matches) == 1:
        await send_material_card(message, user_id, matches[0]["id"])
        return

    # 8. Если точного совпадения нет
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏢 Всё об ИП: открытие с нуля и гид", callback_data="nav_ip_hub")],
        [InlineKeyboardButton(text="📊 Выбор налогового режима", callback_data="mat_mat_ip_taxes")],
        [InlineKeyboardButton(text="🔔 Напомнить по конкретному налогу", callback_data="nav_tax_reminders")],
        [InlineKeyboardButton(text="🛍 Выход на маркетплейсы и юнит-экономика", callback_data="mat_mat_mp_paid")],
        [InlineKeyboardButton(text="🏠 В главное меню", callback_data="nav_start")]
    ])
    await message.answer(
        f"В базе знаний «Альфа-Курса» нет точного совпадения по запросу «{text}».\n\n"
        "💡 **Попробуйте написать ключевое слово** (например: `ИП`, `налоги`, `взносы`, `касса`, `маркетплейс`) "
        "или выберите одну из популярных тем ниже:",
        reply_markup=kb,
        parse_mode="Markdown"
    )

async def main():
    init_db()
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
