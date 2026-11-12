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

# Память для навигации цифрами и ввода своего фокуса
USER_MENU_STATE = {}          # user_id -> {"1": ("action", "value"), ...}
AWAITING_CUSTOM_GOAL = set()  # user_id пользователей, вводящих свой фокус текстом

# --- КОРОТКИЕ СПРАВОЧНИКИ (защита от лимита 64 байта в callback_data) ---
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

# --- БАЗА ЗНАНИЙ «АЛЬФА-КУРСА» (с подробными материалами по ИП) ---
DEFAULT_CATALOG = [
    {
        "id": "mat_ip_start",
        "title": "Пошаговый гид: Как открыть ИП с нуля и без госпошлины",
        "category": "Регистрация и ИП",
        "keywords": ["ип", "открыть", "регистрация", "оквэд", "с нуля", "документы", "старт"],
        "target_stage": "Планирую открыть дело",
        "is_paid": False,
        "summary": "Полная инструкция по регистрации ИП за 3 дня: подбор кодов ОКВЭД, заявление Р21001, подача онлайн без уплаты госпошлины 800 ₽ и открытие счета.",
        "full_text": (
            "📌 **Как открыть ИП с нуля за 5 шагов (по материалам «Альфа-Курса»):**\n"
            "1. **Выберите коды ОКВЭД:** 1 основной код (от него зависит тариф взносов) и дополнительные (от 4 цифр в коде).\n"
            "2. **Определитесь с налоговым режимом заранее:** подать уведомление о переходе на УСН нужно сразу при регистрации или в течение 30 дней, иначе по умолчанию включится сложная ОСНО.\n"
            "3. **Подготовьте документы:** паспорт РФ, ИНН и заявление по форме № Р21001.\n"
            "4. **Подайте заявку онлайн за 0 ₽:** через сервис регистрации бизнеса в Альфа-Банке, Госуслуги или сайт ФНС с КЭП госпошлина (800 ₽) не платится.\n"
            "5. **Откройте расчетный счет и получите лист записи ЕГРИП:** документы придут на email через 1–3 рабочих дня."
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
        "summary": "Пошаговое сравнение НПД, УСН (Доходы 6% и Доходы минус расходы 15%), АУСН и Патента для ИП. Чек-лист перехода и лимиты.",
        "full_text": (
            "📊 **Налоговые режимы для ИП (база «Альфа-Курса»):**\n"
            "• **ИП на НПД:** 4% с физлиц и 6% с юрлиц/ИП. Лимит дохода — 2,4 млн ₽ в год, нельзя нанимать сотрудников по трудовым договорам, нет обязательных фиксированных взносов.\n"
            "• **УСН «Доходы» (6%):** идеален для услуг и ИП с небольшими расходами. Налог можно уменьшать на сумму страховых взносов ИП (до 100% без сотрудников и до 50% с сотрудниками).\n"
            "• **УСН «Доходы минус расходы» (15%):** выгоден для торговли и производства, если подтвержденные расходы составляют более 60–70% от выручки.\n"
            "• **Патент (ПСН):** фиксированная стоимость налога для розницы, общепита и услуг (до 60 млн ₽ в год и до 15 сотрудников).\n"
            "• **АУСН (8% или 20%):** банк и налоговая сами считают налог по операциям по счету, нет декларации и страховых взносов (при лимите до 60 млн ₽ и до 5 сотрудников)."
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
        "summary": "Когда и сколько платит ИП за себя, как считать 1% с доходов свыше 300 000 ₽ и как уменьшить налог по УСН и Патенту.",
        "full_text": (
            "💳 **Обязательные платежи и отчетность ИП («Альфа-Курс»):**\n"
            "• **Фиксированные взносы ИП за себя:** платятся даже при нулевом доходе (кроме ИП на НПД и АУСН). Срок уплаты — до 28 декабря текущего года.\n"
            "• **Дополнительный взнос 1%:** начисляется на сумму дохода свыше 300 000 ₽ за год. Срок уплаты — до 1 июля следующего года.\n"
            "• **Авансовые платежи по УСН:** платятся ежеквартально до 28 апреля, 28 июля и 28 октября (уведомление об исчисленных суммах подается до 25-го числа).\n"
            "• **Годовая декларация по УСН для ИП:** сдается один раз в год — до 25 апреля, а итоговый налог за год уплачивается до 28 апреля."
        ),
        "url": "https://kurs.alfabank.ru/catalog?type=courses&utm_source=tg_bot&utm_medium=ip_vznosy"
    },
    {
        "id": "mat_ip_kassa",
        "title": "Расчетный счет, онлайн-касса (54-ФЗ) и эквайринг для ИП",
        "category": "Финансы и операции",
        "keywords": ["ип", "касса", "счет", "эквайринг", "54-фз", "чек", "оплата", "вывод денег"],
        "target_stage": "Действующий микробизнес",
        "is_paid": False,
        "summary": "Кому из ИП нужна онлайн-касса, как подключить торговый и интернет-эквайринг или СБП и как ИП легально выводить деньги со счета на личную карту.",
        "full_text": (
            "🧾 **Работа со счетом и кассой для ИП («Альфа-Курс»):**\n"
            "• **Онлайн-касса (54-ФЗ):** обязательна при приеме оплаты от физлиц (наличными, картой, по QR-коду). Освобождены ИП на НПД, ряд видов деятельности на Патенте и расчеты только между ИП/ООО по безналу.\n"
            "• **Прием платежей:** подключение СБП (QR-код) с комиссией 0,4–0,7% выгоднее классического эквайринга на старте.\n"
            "• **Личные деньги ИП:** все средства на расчетном счете после уплаты налогов принадлежат предпринимателю — ИП может свободно переводить их на свою карту физлица с назначением «Перевод собственных средств ИП»."
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
        "summary": "Углубленный платный курс для ИП и селлеров: готовые финансовые таблицы расчета маржинальности, комиссий, логистики, маркировки «Честный ЗНАК» и налогов.",
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
        "summary": "Платная программа по оформлению первых сотрудников в ИП, построению отдела продаж, внедрению CRM, скриптам и системе мотивации.",
        "full_text": "ЗАКРЫТЫЙ КОНТЕНТ ПЛАТНОГО КУРСА.",
        "url": "https://kurs.alfabank.ru/catalog?type=courses&utm_source=tg_bot&utm_medium=upsell_sales"
    }
]

CATALOG = DEFAULT_CATALOG

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

# --- КЛАВИАТУРЫ И НАВИГАЦИЯ ЦИФРАМИ ---
def start_kb(user_id: int):
    USER_MENU_STATE[user_id] = {
        "1": ("stage", "1"),
        "2": ("stage", "2"),
        "3": ("stage", "3"),
        "4": ("nav", "ip_hub"),
        "5": ("nav", "profile"),
        "6": ("nav", "reminders"),
        "7": ("nav", "help")
    }
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1. 🚀 Планирую открыть дело", callback_data="stage_1")],
        [InlineKeyboardButton(text="2. 🛠 Решаю задачу в микробизнесе", callback_data="stage_2")],
        [InlineKeyboardButton(text="3. 📈 Хочу системно развить бизнес", callback_data="stage_3")],
        [InlineKeyboardButton(text="4. 🏢 Всё об ИП: открытие с нуля и гид", callback_data="nav_ip_hub")],
        [
            InlineKeyboardButton(text="5. 📋 Мой профиль", callback_data="nav_profile"),
            InlineKeyboardButton(text="6. ⏰ Напоминания", callback_data="nav_reminders")
        ],
        [InlineKeyboardButton(text="7. ❓ Помощь и все функции", callback_data="nav_help")]
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
        "1": ("goal", "1"),
        "2": ("goal", "2"),
        "3": ("goal", "3"),
        "4": ("goal", "4"),
        "5": ("goal", "5"),
        "6": ("goal_custom", "custom"),
        "7": ("nav", "profile")
    }
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1. 🏢 Открытие ИП и регистрация бизнеса", callback_data="goal_1")],
        [InlineKeyboardButton(text="2. 📊 Налоги, учет и отчетность", callback_data="goal_2")],
        [InlineKeyboardButton(text="3. 📣 Маркетинг и привлечение клиентов", callback_data="goal_3")],
        [InlineKeyboardButton(text="4. 🛍 Выход на маркетплейсы и продажи", callback_data="goal_4")],
        [InlineKeyboardButton(text="5. 👥 Систематизация и найм команды", callback_data="goal_5")],
        [InlineKeyboardButton(text="6. ✍️ Ввести свой фокус текстом", callback_data="goal_custom")],
        [InlineKeyboardButton(text="7. ⬅️ Назад в профиль", callback_data="nav_profile")]
    ])

def tax_selection_kb(user_id: int):
    USER_MENU_STATE[user_id] = {
        "1": ("tax", "1"),
        "2": ("tax", "2"),
        "3": ("tax", "3"),
        "4": ("tax", "4"),
        "5": ("tax", "5"),
        "6": ("nav", "profile")
    }
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="1. УСН", callback_data="tax_1"),
            InlineKeyboardButton(text="2. НПД (Самозанятый)", callback_data="tax_2")
        ],
        [
            InlineKeyboardButton(text="3. Патент (ПСН)", callback_data="tax_3"),
            InlineKeyboardButton(text="4. АУСН", callback_data="tax_4")
        ],
        [
            InlineKeyboardButton(text="5. ОСНО", callback_data="tax_5"),
            InlineKeyboardButton(text="6. ⬅️ Назад в профиль", callback_data="nav_profile")
        ]
    ])

def stage_selection_kb(user_id: int):
    USER_MENU_STATE[user_id] = {
        "1": ("stage", "1"),
        "2": ("stage", "2"),
        "3": ("stage", "3"),
        "4": ("nav", "profile")
    }
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1. 🚀 Планирую открыть дело", callback_data="stage_1")],
        [InlineKeyboardButton(text="2. 🛠 Действующий микробизнес", callback_data="stage_2")],
        [InlineKeyboardButton(text="3. 📈 Развиваю бизнес", callback_data="stage_3")],
        [InlineKeyboardButton(text="4. ⬅️ Назад в профиль", callback_data="nav_profile")]
    ])

def ip_hub_kb(user_id: int):
    USER_MENU_STATE[user_id] = {
        "1": ("mat", "mat_ip_start"),
        "2": ("mat", "mat_ip_taxes"),
        "3": ("mat", "mat_ip_vznosy"),
        "4": ("mat", "mat_ip_kassa"),
        "5": ("mat", "mat_mp_paid"),
        "6": ("nav", "reminders"),
        "7": ("nav", "start")
    }
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1. 📝 Как открыть ИП с нуля за 0 ₽ (Пошагово)", callback_data="mat_mat_ip_start")],
        [InlineKeyboardButton(text="2. 📊 Налоги для ИП: УСН, НПД, Патент, АУСН", callback_data="mat_mat_ip_taxes")],
        [InlineKeyboardButton(text="3. 💳 Страховые взносы ИП и сроки отчетности", callback_data="mat_mat_ip_vznosy")],
        [InlineKeyboardButton(text="4. 🧾 Счет, онлайн-касса (54-ФЗ) и вывод денег ИП", callback_data="mat_mat_ip_kassa")],
        [InlineKeyboardButton(text="5. 🎓 Курс: Выход ИП на маркетплейсы", callback_data="mat_mat_mp_paid")],
        [InlineKeyboardButton(text="6. ⏰ Настроить напоминания о налогах ИП", callback_data="nav_reminders")],
        [InlineKeyboardButton(text="7. 🏠 В главное меню", callback_data="nav_start")]
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
        "• Подбираю пошаговые инструкции и курсы под вашу задачу\n"
        "• Напоминаю об учебе и сроках уплаты налогов\n\n"
        "👇 **Нажмите кнопку или отправьте цифру (1–7) в чат:**",
        reply_markup=start_kb(user_id),
        parse_mode="Markdown"
    )

async def send_stage_response(msg: Message, user_id: int, stage_code: str):
    """Индивидуальный ответ под каждую из 3 ситуаций после /start."""
    stage_title = STAGE_MAP.get(stage_code, "Планирую открыть дело")
    update_user(user_id, "stage", stage_title)

    if stage_code == "1":
        update_user(user_id, "goal", "Открытие ИП и регистрация бизнеса")
        USER_MENU_STATE[user_id] = {
            "1": ("mat", "mat_ip_start"),
            "2": ("mat", "mat_ip_taxes"),
            "3": ("nav", "ip_hub"),
            "4": ("nav", "start")
        }
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="1. 📝 Пошаговый гид: Как открыть ИП с нуля", callback_data="mat_mat_ip_start")],
            [InlineKeyboardButton(text="2. 📊 Сравнение налогов для старта (УСН / НПД)", callback_data="mat_mat_ip_taxes")],
            [InlineKeyboardButton(text="3. 🏢 Открыть раздел «Всё об ИП»", callback_data="nav_ip_hub")],
            [InlineKeyboardButton(text="4. 🏠 В главное меню", callback_data="nav_start")]
        ])
        await msg.answer(
            f"🚀 **Отличный старт! Стадия: «{stage_title}» сохранена в профиль.**\n\n"
            "Для запуска бизнеса без штрафов и лишних расходов на «Альфа-Курсе» подготовлен стартовый маршрут:\n"
            "• **Шаг 1:** Определить форму (Самозанятость или ИП) и коды ОКВЭД.\n"
            "• **Шаг 2:** Сразу подать уведомление на выгодный налоговый режим (например, УСН 6% или АУСН).\n"
            "• **Шаг 3:** Зарегистрировать бизнес онлайн без госпошлины 800 ₽.\n\n"
            "👇 **С чего начнем? Выберите кнопкой или отправьте цифру 1–4:**",
            reply_markup=kb,
            parse_mode="Markdown"
        )

    elif stage_code == "2":
        update_user(user_id, "goal", "Налоги, учет и отчетность")
        USER_MENU_STATE[user_id] = {
            "1": ("mat", "mat_ip_vznosy"),
            "2": ("mat", "mat_ip_kassa"),
            "3": ("mat", "mat_mp_paid"),
            "4": ("nav", "reminders"),
            "5": ("nav", "start")
        }
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="1. 💳 Взносы ИП, уменьшение УСН и сроки", callback_data="mat_mat_ip_vznosy")],
            [InlineKeyboardButton(text="2. 🧾 Онлайн-касса (54-ФЗ), СБП и вывод денег", callback_data="mat_mat_ip_kassa")],
            [InlineKeyboardButton(text="3. 🛍 Курс: Юнит-экономика на маркетплейсах", callback_data="mat_mat_mp_paid")],
            [InlineKeyboardButton(text="4. ⏰ Настроить налоговый календарь", callback_data="nav_reminders")],
            [InlineKeyboardButton(text="5. 🏠 В главное меню", callback_data="nav_start")]
        ])
        await msg.answer(
            f"🛠 **Понял вас! Стадия: «{stage_title}» сохранена.**\n\n"
            "Владельцам действующего микробизнеса важны быстрые прикладные решения и готовые шаблоны:\n"
            "• Как уменьшить налог УСН/Патент за счет страховых взносов ИП\n"
            "• Как работать с онлайн-кассой, эквайрингом и легально выводить прибыль\n"
            "• Как посчитать маржинальность товара перед отгрузкой на маркетплейс\n\n"
            "👇 **Выберите нужный инструмент (кнопкой или цифрой 1–5):**",
            reply_markup=kb,
            parse_mode="Markdown"
        )

    else:
        update_user(user_id, "goal", "Систематизация и найм команды")
        USER_MENU_STATE[user_id] = {
            "1": ("mat", "mat_sales_paid"),
            "2": ("mat", "mat_mp_paid"),
            "3": ("submenu", "goal"),
            "4": ("nav", "start")
        }
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="1. 👥 Курс: Построение отдела продаж и найм", callback_data="mat_mat_sales_paid")],
            [InlineKeyboardButton(text="2. 📈 Курс: Масштабирование на маркетплейсах", callback_data="mat_mat_mp_paid")],
            [InlineKeyboardButton(text="3. 🎯 Настроить свой фокус развития", callback_data="submenu_goal")],
            [InlineKeyboardButton(text="4. 🏠 В главное меню", callback_data="nav_start")]
        ])
        await msg.answer(
            f"📈 **Масштабирование! Стадия: «{stage_title}» сохранена.**\n\n"
            "Когда бизнес перерастает ручное управление, «Альфа-Курс» помогает выстроить систему:\n"
            "• Оформление первых сотрудников в ИП/ООО и система мотивации KPI\n"
            "• Внедрение CRM, скриптов продаж и контроль конверсии\n"
            "• Углубленное обучение финансовому планированию и юнит-экономике\n\n"
            "👇 **Выберите программу для изучения (кнопкой или цифрой 1–4):**",
            reply_markup=kb,
            parse_mode="Markdown"
        )

async def send_ip_hub_screen(msg: Message, user_id: int):
    """Специальный раздел «Всё об ИП» с гайдом открытия с нуля и базой Альфа-Курса."""
    AWAITING_CUSTOM_GOAL.discard(user_id)
    await msg.answer(
        "🏢 **Раздел «Всё об ИП»: открытие с нуля и база знаний «Альфа-Курса»**\n\n"
        "**Краткий чек-лист: как открыть ИП с нуля:**\n"
        "1️⃣ **Подберите коды ОКВЭД** (1 основной + дополнительные по справочнику).\n"
        "2️⃣ **Выберите налоговый режим:** УСН 6% (для услуг), УСН 15% (при больших расходах), Патент или НПД.\n"
        "3️⃣ **Подайте заявление Р21001 онлайн** через сервис Альфа-Банка или Госуслуги — **госпошлина 800 ₽ не взимается**.\n"
        "4️⃣ **Получите лист записи ЕГРИП** через 1–3 дня и откройте расчетный счет для приема оплат.\n\n"
        "📚 **Полезные материалы об ИП на «Альфа-Курсе»:**\n"
        "Выберите интересующую тему ниже (кнопкой или отправьте **цифру 1–7** в чат):",
        reply_markup=ip_hub_kb(user_id),
        parse_mode="Markdown"
    )

async def send_material_card(msg: Message, user_id: int, mat_id: str):
    """Показывает выбранный материал с соблюдением Ограничения 06 (защита платных курсов)."""
    item = get_material_by_id(mat_id)
    if not item:
        await msg.answer("Материал не найден. Нажмите `/start` для возврата в меню.")
        return

    USER_MENU_STATE[user_id] = {
        "1": ("nav", "ip_hub"),
        "2": ("nav", "reminders"),
        "3": ("nav", "start")
    }
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Открыть материал на «Альфа-Курсе»", url=item["url"])],
        [
            InlineKeyboardButton(text="1. 🏢 В раздел об ИП", callback_data="nav_ip_hub"),
            InlineKeyboardButton(text="2. ⏰ Напомнить изучить", callback_data="nav_reminders")
        ],
        [InlineKeyboardButton(text="3. 🏠 В главное меню", callback_data="nav_start")]
    ])

    if item["is_paid"]:
        await msg.answer(
            f"🎓 **Платный курс «Альфа-Курса»:**\n**«{item['title']}»**\n\n"
            f"• **Кому подойдет:** {item['target_stage']}\n"
            f"• **Чем полезен курс:** {item['summary']}\n\n"
            f"_🔒 Подробные уроки, видео и расчетные таблицы доступны после приобретения курса на платформе._\n\n"
            f"➡️ **Следующий шаг:** перейдите по кнопке ниже на страницу курса или выберите действие (цифры 1–3):",
            reply_markup=kb,
            parse_mode="Markdown"
        )
    else:
        await msg.answer(
            f"📖 **Материал «Альфа-Курса»:**\n**«{item['title']}»**\n\n"
            f"{item['full_text']}\n\n"
            f"➡️ **Следующий шаг:** откройте полную версию с шаблонами на платформе по кнопке ниже или отправьте цифру (1–3):",
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
        f"👇 **Выберите, что хотите изменить (кнопкой или цифрой 1–4 в чат):**",
        reply_markup=profile_kb(user_id),
        parse_mode="Markdown"
    )

async def send_help_screen(msg: Message, user_id: int):
    AWAITING_CUSTOM_GOAL.discard(user_id)
    USER_MENU_STATE[user_id] = {
        "1": ("nav", "ip_hub"),
        "2": ("nav", "profile"),
        "3": ("nav", "reminders"),
        "4": ("nav", "demo_push"),
        "5": ("nav", "start")
    }
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1. 🏢 Всё об ИП: открытие с нуля и гид", callback_data="nav_ip_hub")],
        [InlineKeyboardButton(text="2. 📋 Открыть и настроить мой профиль", callback_data="nav_profile")],
        [InlineKeyboardButton(text="3. ⏰ Управление напоминаниями", callback_data="nav_reminders")],
        [InlineKeyboardButton(text="4. ⚡ Тест пуш-уведомления (10 сек)", callback_data="nav_demo_push")],
        [InlineKeyboardButton(text="5. 🏠 В главное меню", callback_data="nav_start")]
    ])
    await msg.answer(
        "❓ **Все функции бота «Альфа-Спутник»**\n\n"
        "🔹 **1. Поиск по ключевым словам и ИИ-ответы**\n"
        "Напишите ключевое слово (например: `ИП`, `налоги`, `взносы`, `касса`, `маркетплейс`) — бот предложит список подходящих тем из «Альфа-Курса» на выбор!\n\n"
        "🔹 **2. Раздел «Всё об ИП» (`/ip`)**\n"
        "Пошаговая инструкция по открытию ИП с нуля за 0 ₽, выбору налогов, уплате взносов и работе со счетом.\n\n"
        "🔹 **3. Настройка профиля и Текущего фокуса (`/profile`)**\n"
        "Вы можете в любой момент изменить свой **Текущий фокус**, стадию и налоговый режим.\n\n"
        "🔹 **4. Напоминания и налоговый календарь (`/reminders` и `/demo_push`)**\n"
        "Гибкая настройка частоты напоминаний об учебе и налогах + мгновенный тест через 10 секунд.\n\n"
        "🔢 **Управление цифрами:** в любом меню можно просто отправить цифру нужного пункта в чат!\n\n"
        "👇 *Выберите раздел кнопкой или цифрой 1–5:*",
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
            "🎯 **Выберите ваш текущий фокус** (нажмите кнопку или отправьте цифру **1–7** в чат):",
            reply_markup=goal_selection_kb(uid),
            parse_mode="Markdown"
        )
    elif sub == "tax":
        await call.message.answer(
            "💼 **Выберите налоговый режим** (нажмите кнопку или отправьте цифру **1–6** в чат):",
            reply_markup=tax_selection_kb(uid),
            parse_mode="Markdown"
        )
    elif sub == "stage":
        await call.message.answer(
            "🚀 **Выберите стадию вашего бизнеса** (нажмите кнопку или отправьте цифру **1–4** в чат):",
            reply_markup=stage_selection_kb(uid),
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
                (user_id, f"Проверка сроков уплаты налога и взносов ({u['tax_regime']})", "weekly")
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

# --- ВЫПОЛНЕНИЕ ДЕЙСТВИЙ ПО ЦИФРАМ ---
async def execute_numbered_action(message: Message, action_type: str, value: str):
    uid = message.from_user.id
    if action_type == "stage":
        await send_stage_response(message, uid, value)
    elif action_type == "tax":
        tax_name = TAX_MAP.get(value, "УСН")
        update_user(uid, "tax_regime", tax_name)
        await message.answer(f"✅ Налоговый режим обновлен на **{tax_name}**!", parse_mode="Markdown")
        await send_profile_screen(message, uid)
    elif action_type == "goal":
        goal_name = GOAL_MAP.get(value, "Развитие бизнеса")
        update_user(uid, "goal", goal_name)
        await message.answer(f"✅ Текущий фокус успешно изменен на: **{goal_name}**!", parse_mode="Markdown")
        await send_profile_screen(message, uid)
    elif action_type == "goal_custom":
        AWAITING_CUSTOM_GOAL.add(uid)
        await message.answer("✍️ Напишите в чат вашу текущую цель или задачу для бизнеса:")
    elif action_type == "mat":
        await send_material_card(message, uid, value)
    elif action_type == "submenu":
        if value == "goal":
            await message.answer(
                "🎯 **Выберите ваш текущий фокус** (кнопкой или цифрой **1–7** в чат):",
                reply_markup=goal_selection_kb(uid),
                parse_mode="Markdown"
            )
        elif value == "tax":
            await message.answer(
                "💼 **Выберите налоговый режим** (кнопкой или цифрой **1–6** в чат):",
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
        elif value == "ip_hub":
            await send_ip_hub_screen(message, uid)
        elif value == "profile":
            await send_profile_screen(message, uid)
        elif value == "help":
            await send_help_screen(message, uid)
        elif value == "reminders":
            await show_reminders(message)
        elif value == "demo_push":
            await trigger_demo_push(message, uid)

# --- ГЛАВНЫЙ ОБРАБОТЧИК СООБЩЕНИЙ, КЛЮЧЕВЫХ СЛОВ И ЦИФР ---
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
            start_kb(user_id)
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

    # 2. Автоматическое обогащение профиля
    auto_extract_profile(user_id, text)
    profile = get_user(user_id)
    t_low = text.lower()

    # 3. Если пользователь написал «ИП» или спрашивает про открытие ИП — показываем варианты по ИП
    if t_low in ["ип", "открыть ип", "про ип", "все об ип", "всё об ип"]:
        await send_ip_hub_screen(message, user_id)
        return

    # 4. Перехват запроса на создание напоминания
    if any(w in t_low for w in ["напомни", "напоминание", "срок уплаты"]):
        if profile["tax_regime"] == "Не указан" and "налог" in t_low:
            USER_MENU_STATE[user_id] = {
                "1": ("tax", "1"),
                "2": ("tax", "2"),
                "3": ("tax", "3")
            }
            await message.answer(
                "Чтобы я настроил точное напоминание о сроках уплаты налогов, уточните ваш режим налогообложения "
                "(нажмите кнопку или отправьте цифру **1–3** в чат):",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(text="1. УСН", callback_data="tax_1"),
                        InlineKeyboardButton(text="2. НПД (Самозанятый)", callback_data="tax_2")
                    ],
                    [InlineKeyboardButton(text="3. Патент (ПСН)", callback_data="tax_3")]
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

    # 5. ПОИСК ПО КЛЮЧЕВЫМ СЛОВАМ (предлагает варианты, где фигурирует слово)
    matches = search_by_keyword(text)
    if len(matches) >= 2 and len(text.split()) <= 4:
        USER_MENU_STATE[user_id] = {}
        buttons = []
        lines = [f"🔎 **По запросу «{text}» найдено несколько материалов в «Альфа-Курсе»:**\n"]
        for idx, item in enumerate(matches[:5], start=1):
            badge = "🎓 [Платный курс]" if item["is_paid"] else "📖 [Бесплатно]"
            lines.append(f"**{idx}.** {badge} {item['title']}\n_{item['summary']}_\n")
            USER_MENU_STATE[user_id][str(idx)] = ("mat", item["id"])
            buttons.append([InlineKeyboardButton(text=f"{idx}. {item['title'][:38]}...", callback_data=f"mat_{item['id']}")])

        next_num = str(len(matches[:5]) + 1)
        USER_MENU_STATE[user_id][next_num] = ("nav", "ip_hub")
        buttons.append([InlineKeyboardButton(text=f"{next_num}. 🏢 Раздел «Всё об ИП с нуля»", callback_data="nav_ip_hub")])

        lines.append(f"👇 **Выберите нужный вариант кнопкой или отправьте цифру (1–{next_num}) в чат:**")
        await message.answer(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="Markdown"
        )
        return

    # 6. Обращение к YandexGPT (для развернутых вопросов, если подключены ключи)
    system_prompt = (
        "Ты — ИИ-помощник образовательной платформы «Альфа-Курс» в Telegram.\n"
        f"Профиль предпринимателя: Стадия={profile['stage']}, Налоги={profile['tax_regime']}, Текущий фокус={profile['goal']}.\n"
        "ПРАВИЛА:\n"
        "1. Отвечай ТОЛЬКО на основе предоставленной ниже базы знаний.\n"
        "2. Если вопрос слишком широкий или неясный — задай 1-2 конкретных уточняющих вопроса.\n"
        "3. Если ответа в базе нет — честно скажи об этом и предложи ближайшую тему из каталога.\n"
        "4. СТРОГОЕ ОГРАНИЧЕНИЕ: Для ПЛАТНЫХ курсов никогда не раскрывай внутреннее содержание — "
        "кратко объясни пользу и дай ссылку.\n"
        "5. В конце ответа всегда предлагай понятный следующий шаг.\n\n"
        f"БАЗА ЗНАНИЙ:\n{build_safe_context()}"
    )

    yandex_answer = await ask_yandex_gpt(system_prompt, text)
    if yandex_answer:
        await message.answer(yandex_answer, parse_mode="Markdown")
        return

    # 7. Если найден ровно 1 точный материал в базе — сразу показываем его карточку
    if len(matches) == 1:
        await send_material_card(message, user_id, matches[0]["id"])
        return

    # 8. Если ничего не найдено или вопрос неоднозначный
    USER_MENU_STATE[user_id] = {
        "1": ("nav", "ip_hub"),
        "2": ("mat", "mat_ip_taxes"),
        "3": ("mat", "mat_mp_paid"),
        "4": ("nav", "start")
    }
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1. 🏢 Всё об ИП: открытие с нуля и гид", callback_data="nav_ip_hub")],
        [InlineKeyboardButton(text="2. 📊 Выбор налогового режима (УСН, НПД, Патент)", callback_data="mat_mat_ip_taxes")],
        [InlineKeyboardButton(text="3. 🛍 Выход на маркетплейсы и юнит-экономика", callback_data="mat_mat_mp_paid")],
        [InlineKeyboardButton(text="4. 🏠 В главное меню", callback_data="nav_start")]
    ])
    await message.answer(
        f"В базе знаний «Альфа-Курса» нет точного совпадения по запросу «{text}».\n\n"
        "💡 **Попробуйте написать ключевое слово** (например: `ИП`, `налоги`, `взносы`, `касса`, `маркетплейс`) "
        "или выберите одну из популярных тем ниже (кнопкой или цифрой **1–4**):",
        reply_markup=kb,
        parse_mode="Markdown"
    )

async def main():
    init_db()
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
