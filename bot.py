"""
Telegram-бот на aiogram 3.x для расширения Selftabs.
- Регистрация / вход по Email+пароль прямо в боте (с подтверждением email)
- Авторизация через deep link из расширения (auth_ токен) — сохранена
- Привязка Telegram ID к аккаунту — сохранена
- Оплата подписки через Telegram Stars (XTR) — recurring subscription
- Оплата подписки через USDT (CryptoPay)
- Оплата через СБП (Robokassa)

Установка зависимостей:
    pip install aiogram aiohttp aiocryptopay python-dotenv

Запуск:
    python bot.py
"""

import asyncio
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional
from dotenv import load_dotenv
import aiohttp
from aiohttp import web
from aiohttp_socks import ProxyConnector
from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums.parse_mode import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, PreCheckoutQuery,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from aiocryptopay import AioCryptoPay, Networks

# ── Конфиг ────────────────────────────────────────────────────────────────

load_dotenv()

BOT_TOKEN        = os.getenv("BOT_TOKEN")
API_URL          = os.getenv("API_URL", "").rstrip("/")
BOT_SECRET       = os.getenv("BOT_SECRET", "SLFTBS")
CRYPTO_PAY_TOKEN = os.getenv("CRYPTO_PAY_TOKEN")
CRYPTO_NETWORK   = Networks.MAIN_NET

ROBOKASSA_LOGIN     = os.getenv("ROBOKASSA_LOGIN", "")
ROBOKASSA_PASSWORD1 = os.getenv("ROBOKASSA_PASSWORD1", "")
TELEGRAM_PROXY      = os.getenv("TELEGRAM_PROXY", "")

# Platega (СБП + Крипта)
PLATEGA_BOT_SECRET = os.getenv("BOT_SECRET", "SLFTBS")  # тот же BOT_SECRET

NOTIFY_TZ_OFFSET = int(os.getenv("NOTIFY_TZ_OFFSET", "3"))
NOTIFY_HOUR      = int(os.getenv("NOTIFY_HOUR", "20"))

# ── Логгер ────────────────────────────────────────────────────────────────

logger = logging.getLogger("selftabs")


def _u(tg_id: int, username: str | None = None) -> str:
    return f"tg={tg_id}" + (f" (@{username})" if username else "")


def log_event(event: str, tg_id: int, username: str | None = None, **kwargs):
    extra = "  ".join(f"{k}={v}" for k, v in kwargs.items() if v is not None)
    logger.info(f"[{event}] {_u(tg_id, username)}" + (f"  {extra}" if extra else ""))


def log_error(event: str, tg_id: int, username: str | None = None, **kwargs):
    extra = "  ".join(f"{k}={v}" for k, v in kwargs.items() if v is not None)
    logger.error(f"[{event}] {_u(tg_id, username)}" + (f"  {extra}" if extra else ""))

# ── Планы и цены ───────────────────────────────────────────────────────────

PLANS = {
    "pro": {
        "title":       "Pro Pass",
        "description": (
            "✅ Безлимитные вкладки и сессии\n"
            "✅ AI 7-дневный дайджест\n"
            "✅ Кастомизация MyWallet\n"
            "✅ Авто-бэкап в облако (JSON/Markdown)\n"
            "✅ Свой API ключ ИИ\n"
            "✅ Статус-кольцо Pro"
        ),
        "stars":    245,
        "usdt":     2.99,
        "usdt_rub": 290,
        "plan_key": "pro",
        "price_rub": "290₽",
        "sbp_rub":  290,
        "emoji":    "🚀",
    },
    "team": {
        "title":       "Team Workspace",
        "description": (
            "✅ До 10 участников (x2 экономия)\n"
            "✅ Общие командные сессии Workspace\n"
            "✅ Шеринг отдельных вкладок\n"
            "✅ Статус-кольцо Team в профиле\n"
            "✅ Приоритетный Cloud Sync"
        ),
        "stars":    770,
        "usdt":     9.99,
        "usdt_rub": 950,
        "plan_key": "team",
        "price_rub": "950₽",
        "sbp_rub":  950,
        "emoji":    "🏢",
    },
}

PLAN_NAMES = {
    "standard": ("🆓", "Self Free"),
    "pro":      ("🚀", "Pro Pass"),
    "team":     ("🏢", "Team Workspace"),
}

# ── Хранилище токенов (SQLite) ────────────────────────────────────────────

class TokenStorage:
    def __init__(self, db_path: str = "bot_tokens.db"):
        self.db_path = db_path
        self._cache: dict[int, str] = {}
        self._init_db()
        self._load_cache()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_tokens (
                    tg_id    INTEGER PRIMARY KEY,
                    token    TEXT NOT NULL,
                    saved_at REAL DEFAULT (unixepoch())
                )
            """)
            conn.commit()

    def _load_cache(self):
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT tg_id, token FROM user_tokens").fetchall()
        self._cache = {tg_id: token for tg_id, token in rows}
        logging.info(f"TokenStorage: загружено {len(self._cache)} сессий из БД")

    def __contains__(self, tg_id: int) -> bool:
        return tg_id in self._cache

    def get(self, tg_id: int, default=None) -> Optional[str]:
        return self._cache.get(tg_id, default)

    def __setitem__(self, tg_id: int, token: str):
        self._cache[tg_id] = token
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO user_tokens (tg_id, token, saved_at) VALUES (?, ?, unixepoch())",
                (tg_id, token),
            )
            conn.commit()

    def pop(self, tg_id: int, *args):
        self._cache.pop(tg_id, None)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM user_tokens WHERE tg_id = ?", (tg_id,))
            conn.commit()

    def items(self):
        return list(self._cache.items())

    def all_tg_ids(self) -> list[int]:
        return list(self._cache.keys())


user_tokens = TokenStorage()

# ── Лог уведомлений ───────────────────────────────────────────────────────

class NotificationLog:
    def __init__(self, db_path: str = "bot_tokens.db"):
        self.db_path = db_path
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS notification_log (
                    tg_id      INTEGER,
                    notif_type TEXT,
                    date_tag   TEXT,
                    sent_at    REAL DEFAULT (unixepoch()),
                    PRIMARY KEY (tg_id, notif_type, date_tag)
                )
            """)
            conn.commit()

    def already_sent(self, tg_id: int, notif_type: str, date_tag: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM notification_log WHERE tg_id=? AND notif_type=? AND date_tag=?",
                (tg_id, notif_type, date_tag),
            ).fetchone()
        return row is not None

    def mark_sent(self, tg_id: int, notif_type: str, date_tag: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO notification_log (tg_id, notif_type, date_tag) VALUES (?,?,?)",
                (tg_id, notif_type, date_tag),
            )
            conn.commit()

    def cleanup_old(self, days: int = 35):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM notification_log WHERE sent_at < ?", (cutoff,))
            conn.commit()


notif_log = NotificationLog()

# ── FSM-состояния ──────────────────────────────────────────────────────────

ADMIN_ID = 5105131373


class AdminStates(StatesGroup):
    waiting_user_id_profile = State()
    waiting_user_id_sub     = State()
    waiting_sub_plan        = State()
    waiting_sub_days        = State()


# ── FSM: Email-авторизация (вход и регистрация) ───────────────────────────

class EmailAuthStates(StatesGroup):
    # Общий шаг — выбор режима (login/register)
    choosing_mode   = State()
    # Вход
    login_email     = State()
    login_password  = State()
    # Регистрация
    reg_email       = State()
    reg_password    = State()
    reg_password2   = State()   # подтверждение пароля
    # Верификация email (после регистрации)
    verify_code     = State()   # ожидаем 6-значный код


# ══════════════════════════════════════════════════════════════════════════
# КЛАВИАТУРЫ
# ══════════════════════════════════════════════════════════════════════════

def get_main_keyboard(logged_in: bool, tg_id: int = 0) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if logged_in:
        builder.button(text="👤 Мой профиль",    callback_data="profile")
        builder.button(text="💳 Подписка",        callback_data="subscription")
        builder.button(text="📰 Daily дайджест",  callback_data="daily_digest")
        builder.button(text="📂 Мои сессии",      callback_data="my_sessions:0")
        builder.button(text="🔗 Привязать TG",    callback_data="link_tg")
        builder.button(text="🚪 Выйти",           callback_data="logout")
        builder.adjust(2)
        if tg_id == ADMIN_ID:
            builder.button(text="🛡 Админ-панель", callback_data="admin_panel")
            builder.adjust(2)
    else:
        builder.button(text="📧 Войти / Зарегистрироваться", callback_data="auth_email")
        builder.button(text="🔑 Войти через расширение",      callback_data="login_extension")
        builder.adjust(1)
    return builder.as_markup()


def get_auth_mode_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔐 Войти в аккаунт",    callback_data="auth_mode:login")
    builder.button(text="📝 Зарегистрироваться", callback_data="auth_mode:register")
    builder.button(text="🔙 Главное меню",        callback_data="back_to_main")
    builder.adjust(1)
    return builder.as_markup()


def get_cancel_auth_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="auth_cancel")
    return builder.as_markup()


def get_resend_code_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Отправить код повторно", callback_data="auth_resend_code")
    builder.button(text="❌ Отмена",                  callback_data="auth_cancel")
    builder.adjust(1)
    return builder.as_markup()


def get_subscription_keyboard(plan: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if plan == "standard":
        builder.button(text="🚀 Pro Pass — 290 ₽/мес",        callback_data="sub_info:pro")
        builder.button(text="🏢 Team Workspace — 950 ₽/мес",  callback_data="sub_info:team")
    elif plan == "pro":
        builder.button(text="🔄 Продлить Pro Pass",  callback_data="sub_info:pro")
        builder.button(text="⬆️ Перейти на Team",    callback_data="sub_info:team")
    elif plan == "team":
        builder.button(text="🔄 Продлить Team Workspace", callback_data="sub_info:team")
    builder.button(text="❓ Подробнее о планах", callback_data="plans:info")
    builder.button(text="🔙 Главное меню",       callback_data="back_to_main")
    builder.adjust(1)
    return builder.as_markup()


def get_plan_payment_keyboard(plan_key: str) -> InlineKeyboardMarkup:
    plan = PLANS[plan_key]
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"💫 Telegram Stars — {plan['stars']} ⭐/мес",
        callback_data=f"buy_stars:{plan_key}",
    )
    builder.button(
        text=f"🏦 СБП — {plan['sbp_rub']} ₽/мес",
        callback_data=f"buy_sbp:{plan_key}",
    )
    builder.button(
        text=f"🪙 USDT — {plan['usdt']}$ (~{plan['usdt_rub']} ₽)/мес",
        callback_data=f"buy_crypto:{plan_key}",
    )
    builder.button(text="🔙 Назад", callback_data="subscription")
    builder.adjust(1)
    return builder.as_markup()


def get_back_main_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Главное меню", callback_data="back_to_main")
    return builder.as_markup()


def get_plans_info_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🚀 Купить Pro Pass",       callback_data="sub_info:pro")
    builder.button(text="🏢 Купить Team Workspace", callback_data="sub_info:team")
    builder.button(text="🔙 Назад",                 callback_data="subscription")
    builder.adjust(1)
    return builder.as_markup()


def get_cancel_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="back_to_main")
    return builder.as_markup()


def get_admin_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="👤 Профиль пользователя",  callback_data="admin_view_profile")
    builder.button(text="🎁 Выдать подписку",        callback_data="admin_grant_sub")
    builder.button(text="🔙 Главное меню",           callback_data="back_to_main")
    builder.adjust(1)
    return builder.as_markup()


def get_admin_cancel_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="admin_panel")
    return builder.as_markup()


def get_admin_plan_keyboard(target_tg_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🚀 Pro Pass",        callback_data=f"admin_plan:{target_tg_id}:pro")
    builder.button(text="🏢 Team Workspace",  callback_data=f"admin_plan:{target_tg_id}:team")
    builder.button(text="🆓 Сбросить (Free)", callback_data=f"admin_plan:{target_tg_id}:standard")
    builder.button(text="❌ Отмена",          callback_data="admin_panel")
    builder.adjust(1)
    return builder.as_markup()


# ── API-хелперы ────────────────────────────────────────────────────────────

async def api_post(path: str, payload: dict, token: str | None = None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{API_URL}{path}", json=payload, headers=headers) as r:
            try:
                data = await r.json()
            except Exception:
                data = {}
            return r.status, data


async def api_get(path: str, token: str) -> tuple[int, dict]:
    headers = {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{API_URL}{path}", headers=headers) as r:
            try:
                data = await r.json()
            except Exception:
                data = {}
            return r.status, data


async def api_delete(path: str, token: str) -> tuple[int, dict]:
    headers = {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession() as session:
        async with session.delete(f"{API_URL}{path}", headers=headers) as r:
            try:
                data = await r.json()
            except Exception:
                data = {}
            return r.status, data


# ── Бот + диспетчер ───────────────────────────────────────────────────────

_tg_session = AiohttpSession(proxy=TELEGRAM_PROXY) if TELEGRAM_PROXY else None

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    session=_tg_session,
)
dp  = Dispatcher(storage=MemoryStorage())
crypto: Optional[AioCryptoPay] = None


# ══════════════════════════════════════════════════════════════════════════
# /start
# ══════════════════════════════════════════════════════════════════════════

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    tg_id     = message.from_user.id
    logged_in = tg_id in user_tokens

    args  = message.text.split(maxsplit=1)
    param = args[1].strip() if len(args) > 1 else None

    # ── Deep link с авторизацией: /start auth_{token}[__buy_pro] ──
    if param and param.startswith("auth_"):
        raw      = param[5:]
        parts    = raw.split("__", 1)
        auth_tok = parts[0]
        plan_key = parts[1][4:] if len(parts) > 1 and parts[1].startswith("buy_") else None

        status, resp = await api_post(
            "/bot/exchange-token",
            {"bot_auth_token": auth_tok, "telegram_id": tg_id, "bot_secret": BOT_SECRET},
        )

        uname = message.from_user.username
        if status == 200:
            token = resp["access_token"]
            user_tokens[tg_id] = token
            logged_in = True
            user_name = resp["user"].get("name") or resp["user"].get("email", "")
            tg_linked = resp.get("tg_linked", False)
            tg_note = "🔗 Telegram привязан к аккаунту." if tg_linked else "🔗 Telegram привязан автоматически."
            log_event("AUTH_OK", tg_id, uname, user=user_name, tg_linked=tg_linked,
                      deeplink_plan=plan_key or "none")

            await message.answer(
                f"✅ <b>Добро пожаловать, {user_name}!</b>\n\n"
                f"{tg_note}\n\n"
                "📂 Управляй сессиями, подпиской и дайджестом прямо здесь.\n\n"
                "Выбери действие 👇",
                reply_markup=get_main_keyboard(True, tg_id),
            )
            if plan_key and plan_key in PLANS:
                await _send_stars_invoice_msg(tg_id, plan_key)

        elif status == 410:
            log_event("AUTH_EXPIRED", tg_id, uname)
            await message.answer(
                "⏱ <b>Ссылка истекла</b> (действует 2 минуты).\n\n"
                "Вернись в расширение и нажми кнопку открытия бота заново.",
                reply_markup=get_main_keyboard(logged_in),
            )
        elif status == 404:
            log_event("AUTH_USED", tg_id, uname)
            await message.answer(
                "⚠️ <b>Ссылка уже использована.</b>\n\n"
                "Вернись в расширение и нажми кнопку открытия бота заново.",
                reply_markup=get_main_keyboard(logged_in),
            )
        else:
            log_error("AUTH_FAIL", tg_id, uname, http_status=status,
                      detail=resp.get("detail", "—"))
            await message.answer(
                f"❌ Ошибка авторизации: {resp.get('detail', 'Неизвестная ошибка')}",
                reply_markup=get_main_keyboard(logged_in),
            )
        return

    # ── Deep link на оплату: /start buy_pro / buy_team ──
    if param and param.startswith("buy_"):
        plan_key = param[4:]
        if plan_key in PLANS:
            if not logged_in:
                await message.answer(
                    "⚠️ <b>Для оформления подписки войди в аккаунт.</b>\n\n"
                    "Вернись в расширение — кнопка «Оплатить» выдаст ссылку с автологином.",
                    reply_markup=get_main_keyboard(False),
                )
                return
            plan = PLANS[plan_key]
            await message.answer(
                f"💳 <b>Оформление подписки</b>\n\n"
                f"{plan['emoji']} <b>{plan['title']}</b> — {plan['price_rub']}/мес\n\n"
                f"{plan['description']}\n\n"
                "<a href=\"https://selftabs.ru/privacy\">Политика конфиденциальности</a> · <a href=\"https://selftabs.ru/terms\">Пользовательское соглашение</a>\n\nВыбери способ оплаты 👇",
                reply_markup=get_plan_payment_keyboard(plan_key),
            )
            return

    # ── Deep link СБП: /start sbp_pro / sbp_team ──
    if param and param.startswith("sbp_"):
        plan_key = param[4:]
        if plan_key in PLANS:
            if not logged_in:
                await message.answer(
                    "⚠️ <b>Для оформления подписки войди в аккаунт.</b>\n\n"
                    "Вернись в расширение — кнопка «Оплатить» выдаст ссылку с автологином.",
                    reply_markup=get_main_keyboard(False),
                )
                return
            plan = PLANS[plan_key]
            await message.answer(
                f"🏦 <b>Оплата через СБП</b>\n\n"
                f"{plan['emoji']} <b>{plan['title']}</b> — {plan['price_rub']}/мес\n\n"
                "<a href=\"https://selftabs.ru/privacy\">Политика конфиденциальности</a> · <a href=\"https://selftabs.ru/terms\">Пользовательское соглашение</a>\n\nВыбери способ оплаты 👇",
                reply_markup=get_plan_payment_keyboard(plan_key),
            )
            return

    # ── Deep link привязки TG ──
    if param:
        await handle_link_token(message, param)
        return

    # Обычный /start
    if logged_in:
        token = user_tokens.get(tg_id)
        status, _ = await api_get("/me", token)
        if status == 401:
            user_tokens.pop(tg_id, None)
            logged_in = False

    if logged_in:
        text = (
            "👋 <b>С возвращением в Selftabs!</b>\n\n"
            "📂 Управляй сессиями, подпиской и дайджестом прямо здесь.\n\n"
            "Выбери действие 👇"
        )
    else:
        text = (
            "🌟 <b>Selftabs — умное расширение для браузера</b>\n\n"
            "Сохраняй вкладки, управляй сессиями и получай AI-дайджесты.\n\n"
            "📋 <b>Тарифы:</b>\n"
            "🚀 <b>Pro Pass</b> — 290 ₽/мес\n"
            "🏢 <b>Team Workspace</b> — 950 ₽/мес\n\n"
            "💳 <b>Способы оплаты:</b>\n"
            "💫 Telegram Stars · 🏦 СБП · 🪙 USDT\n\n"
            "📧 Войди по email или через расширение Selftabs 👇"
        )
    await message.answer(text, reply_markup=get_main_keyboard(logged_in, tg_id))


async def handle_link_token(message: Message, link_token: str):
    await message.answer(
        "🔗 <b>Привязка Telegram к аккаунту</b>\n\n"
        "Ты перешёл по ссылке из браузерного расширения.\n"
        "Нажми кнопку ниже, чтобы подтвердить привязку.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="✅ Подтвердить привязку",
                callback_data=f"tglink:{link_token}",
            )
        ]]),
    )


@dp.callback_query(F.data.startswith("tglink:"))
async def confirm_link(call: CallbackQuery):
    link_token = call.data.split(":", 1)[1]
    tg_id      = call.from_user.id
    uname      = call.from_user.username

    await call.message.edit_reply_markup(reply_markup=None)

    status, resp = await api_post(
        "/integrations/telegram/confirm",
        {"link_token": link_token, "telegram_id": tg_id, "bot_secret": BOT_SECRET},
    )

    if status == 200:
        log_event("TG_LINKED", tg_id, uname)
        await call.message.edit_text(
            "✅ <b>Telegram успешно привязан!</b>\n\n"
            "Можешь вернуться в расширение — страница обновится автоматически.",
            reply_markup=get_back_main_keyboard(),
        )
    elif status == 404:
        log_event("TG_LINK_NOT_FOUND", tg_id, uname)
        await call.message.edit_text("⚠️ Ссылка не найдена или уже использована.", reply_markup=get_back_main_keyboard())
    elif status == 410:
        log_event("TG_LINK_EXPIRED", tg_id, uname)
        await call.message.edit_text("⏱ Ссылка истекла (действует 10 минут).", reply_markup=get_back_main_keyboard())
    elif status == 409:
        log_event("TG_LINK_CONFLICT", tg_id, uname, detail=resp.get("detail"))
        await call.message.edit_text(f"⚠️ {resp.get('detail', 'Конфликт')}", reply_markup=get_back_main_keyboard())
    else:
        log_error("TG_LINK_FAIL", tg_id, uname, http_status=status, detail=resp.get("detail", "—"))
        await call.message.edit_text("❌ Что-то пошло не так. Попробуй позже.", reply_markup=get_back_main_keyboard())
    await call.answer()


# ══════════════════════════════════════════════════════════════════════════
# EMAIL-АВТОРИЗАЦИЯ (FSM)
# ══════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data == "auth_email")
async def cb_auth_email(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(EmailAuthStates.choosing_mode)
    await call.message.edit_text(
        "📧 <b>Вход / Регистрация по Email</b>\n\n"
        "Выбери действие:",
        reply_markup=get_auth_mode_keyboard(),
    )
    await call.answer()


@dp.callback_query(F.data.startswith("auth_mode:"))
async def cb_auth_mode(call: CallbackQuery, state: FSMContext):
    mode = call.data.split(":", 1)[1]  # "login" или "register"
    await state.update_data(auth_mode=mode)

    if mode == "login":
        await state.set_state(EmailAuthStates.login_email)
        await call.message.edit_text(
            "🔐 <b>Вход в аккаунт</b>\n\n"
            "Введи свой <b>Email</b>:",
            reply_markup=get_cancel_auth_keyboard(),
        )
    else:
        await state.set_state(EmailAuthStates.reg_email)
        await call.message.edit_text(
            "📝 <b>Регистрация</b>\n\n"
            "Введи свой <b>Email</b>:",
            reply_markup=get_cancel_auth_keyboard(),
        )
    await call.answer()


# ── ВХОД: ввод email ──────────────────────────────────────────────────────

@dp.message(EmailAuthStates.login_email)
async def login_email_input(message: Message, state: FSMContext):
    email = message.text.strip().lower() if message.text else ""
    if not _is_valid_email(email):
        await message.answer(
            "⚠️ Введи корректный email-адрес:",
            reply_markup=get_cancel_auth_keyboard(),
        )
        return
    await state.update_data(email=email)
    await state.set_state(EmailAuthStates.login_password)
    await message.answer(
        f"📧 Email: <code>{email}</code>\n\n"
        "Введи <b>пароль</b>:",
        reply_markup=get_cancel_auth_keyboard(),
    )


# ── ВХОД: ввод пароля ─────────────────────────────────────────────────────

@dp.message(EmailAuthStates.login_password)
async def login_password_input(message: Message, state: FSMContext):
    password = message.text or ""
    # Удаляем сообщение с паролем для безопасности
    try:
        await message.delete()
    except Exception:
        pass

    data  = await state.get_data()
    email = data.get("email", "")
    tg_id = message.from_user.id
    uname = message.from_user.username

    if len(password) < 8:
        await message.answer(
            "⚠️ Пароль должен содержать минимум 8 символов.\n\nВведи пароль ещё раз:",
            reply_markup=get_cancel_auth_keyboard(),
        )
        return

    status, resp = await api_post(
        "/auth/login",
        {"email": email, "password": password},
    )

    if status == 200:
        token = resp["access_token"]
        user_tokens[tg_id] = token
        await state.clear()

        user_name = resp["user"].get("name") or email
        log_event("EMAIL_LOGIN_OK", tg_id, uname, email=email)

        # Привязываем Telegram автоматически
        await _auto_link_telegram(tg_id, token)

        await message.answer(
            f"✅ <b>Добро пожаловать, {user_name}!</b>\n\n"
            "Выбери действие 👇",
            reply_markup=get_main_keyboard(True, tg_id),
        )

    elif status == 403:
        detail = resp.get("detail", "")
        await state.clear()
        log_event("EMAIL_LOGIN_GOOGLE_ONLY", tg_id, uname, email=email)
        if "google_only" in detail:
            await message.answer(
                "⚠️ <b>Этот аккаунт создан через Google.</b>\n\n"
                "Войди через расширение Selftabs с кнопкой Google,\n"
                "затем используй deep link для авторизации в боте.",
                reply_markup=get_main_keyboard(False),
            )
        else:
            await message.answer(
                f"❌ {detail or 'Ошибка входа.'}",
                reply_markup=get_main_keyboard(False),
            )

    elif status == 401:
        log_event("EMAIL_LOGIN_WRONG_PASS", tg_id, uname, email=email)
        await message.answer(
            "❌ <b>Неверный email или пароль.</b>\n\n"
            "Попробуй ещё раз — введи пароль:",
            reply_markup=get_cancel_auth_keyboard(),
        )
        # Остаёмся в состоянии login_password

    else:
        detail = resp.get("detail", "Неизвестная ошибка")
        log_error("EMAIL_LOGIN_FAIL", tg_id, uname, email=email, http_status=status, detail=detail)
        await state.clear()
        await message.answer(
            f"❌ Ошибка: {detail}",
            reply_markup=get_main_keyboard(False),
        )


# ── РЕГИСТРАЦИЯ: ввод email ───────────────────────────────────────────────

@dp.message(EmailAuthStates.reg_email)
async def reg_email_input(message: Message, state: FSMContext):
    email = message.text.strip().lower() if message.text else ""
    if not _is_valid_email(email):
        await message.answer(
            "⚠️ Введи корректный email-адрес:",
            reply_markup=get_cancel_auth_keyboard(),
        )
        return
    await state.update_data(email=email)
    await state.set_state(EmailAuthStates.reg_password)
    await message.answer(
        f"📧 Email: <code>{email}</code>\n\n"
        "Придумай <b>пароль</b> (минимум 8 символов):",
        reply_markup=get_cancel_auth_keyboard(),
    )


# ── РЕГИСТРАЦИЯ: ввод пароля ──────────────────────────────────────────────

@dp.message(EmailAuthStates.reg_password)
async def reg_password_input(message: Message, state: FSMContext):
    password = message.text or ""
    try:
        await message.delete()
    except Exception:
        pass

    if len(password) < 8:
        await message.answer(
            "⚠️ Пароль должен содержать минимум <b>8 символов</b>.\n\nВведи пароль ещё раз:",
            reply_markup=get_cancel_auth_keyboard(),
        )
        return

    await state.update_data(password=password)
    await state.set_state(EmailAuthStates.reg_password2)
    await message.answer(
        "🔁 Повтори пароль для подтверждения:",
        reply_markup=get_cancel_auth_keyboard(),
    )


# ── РЕГИСТРАЦИЯ: подтверждение пароля ────────────────────────────────────

@dp.message(EmailAuthStates.reg_password2)
async def reg_password2_input(message: Message, state: FSMContext):
    password2 = message.text or ""
    try:
        await message.delete()
    except Exception:
        pass

    data     = await state.get_data()
    email    = data.get("email", "")
    password = data.get("password", "")
    tg_id    = message.from_user.id
    uname    = message.from_user.username

    if password2 != password:
        await message.answer(
            "❌ <b>Пароли не совпадают.</b>\n\nВведи пароль ещё раз:",
            reply_markup=get_cancel_auth_keyboard(),
        )
        await state.set_state(EmailAuthStates.reg_password)
        return

    # Регистрируем через API
    status, resp = await api_post(
        "/auth/register",
        {"email": email, "password": password},
    )

    if status == 200 or status == 201:
        token = resp["access_token"]
        user_tokens[tg_id] = token
        log_event("EMAIL_REGISTER_OK", tg_id, uname, email=email)

        # Привязываем Telegram автоматически
        await _auto_link_telegram(tg_id, token)

        # Запрашиваем код подтверждения email
        verify_status, verify_resp = await api_post(
            "/auth/email/send-code",
            {"email": email},
            token=token,
        )

        await state.update_data(verify_email=email, verify_token=token)
        await state.set_state(EmailAuthStates.verify_code)

        if verify_status == 200:
            expires = verify_resp.get("expires_at", "")[:16].replace("T", " ")
            await message.answer(
                f"🎉 <b>Аккаунт создан!</b>\n\n"
                f"📧 На адрес <code>{email}</code> отправлен <b>6-значный код</b> подтверждения.\n"
                f"⏱ Действителен до: <b>{expires}</b>\n\n"
                "Введи код из письма:",
                reply_markup=get_resend_code_keyboard(),
            )
        else:
            # Аккаунт создан, но письмо не дошло — не критично, просим ввести код
            await message.answer(
                f"🎉 <b>Аккаунт создан!</b>\n\n"
                f"Не удалось отправить код на <code>{email}</code>.\n"
                "Нажми «Отправить повторно» 👇",
                reply_markup=get_resend_code_keyboard(),
            )

    elif status == 409:
        log_event("EMAIL_REGISTER_EXISTS", tg_id, uname, email=email)
        await state.clear()
        await message.answer(
            "⚠️ <b>Email уже зарегистрирован.</b>\n\n"
            "Войди в существующий аккаунт:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔐 Войти", callback_data="auth_mode:login")],
                [InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_main")],
            ]),
        )

    else:
        detail = resp.get("detail", "Неизвестная ошибка")
        log_error("EMAIL_REGISTER_FAIL", tg_id, uname, email=email, http_status=status, detail=detail)
        await state.clear()
        await message.answer(
            f"❌ Ошибка регистрации: {detail}",
            reply_markup=get_main_keyboard(False),
        )


# ── ВЕРИФИКАЦИЯ EMAIL: ввод кода ──────────────────────────────────────────

@dp.message(EmailAuthStates.verify_code)
async def verify_code_input(message: Message, state: FSMContext):
    code  = message.text.strip() if message.text else ""
    tg_id = message.from_user.id
    uname = message.from_user.username

    data  = await state.get_data()
    email = data.get("verify_email", "")
    token = data.get("verify_token", "")

    if not code.isdigit() or len(code) != 6:
        await message.answer(
            "⚠️ Введи <b>6-значный</b> числовой код из письма:",
            reply_markup=get_resend_code_keyboard(),
        )
        return

    verify_status, verify_resp = await api_post(
        "/auth/email/verify",
        {"email": email, "code": code},
        token=token,
    )

    if verify_status == 200:
        await state.clear()
        log_event("EMAIL_VERIFIED", tg_id, uname, email=email)
        await message.answer(
            "✅ <b>Email подтверждён!</b>\n\n"
            "Добро пожаловать в Selftabs 🎉\n\n"
            "Выбери действие 👇",
            reply_markup=get_main_keyboard(True, tg_id),
        )

    elif verify_status == 429:
        detail = verify_resp.get("detail", "Превышено количество попыток.")
        await state.clear()
        log_event("EMAIL_VERIFY_LIMIT", tg_id, uname, email=email)
        await message.answer(
            f"🚫 <b>{detail}</b>\n\n"
            "Запроси новый код:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Запросить новый код", callback_data="auth_resend_code")],
                [InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_main")],
            ]),
        )

    elif verify_status == 400:
        detail = verify_resp.get("detail", "Неверный код.")
        log_event("EMAIL_VERIFY_WRONG", tg_id, uname, email=email)
        await message.answer(
            f"❌ {detail}\n\nВведи код ещё раз:",
            reply_markup=get_resend_code_keyboard(),
        )
        # Остаёмся в состоянии verify_code

    else:
        detail = verify_resp.get("detail", "Неизвестная ошибка")
        log_error("EMAIL_VERIFY_FAIL", tg_id, uname, email=email, http_status=verify_status, detail=detail)
        await state.clear()
        await message.answer(
            f"❌ Ошибка верификации: {detail}",
            reply_markup=get_main_keyboard(True, tg_id),
        )


# ── Переотправить код ─────────────────────────────────────────────────────

@dp.callback_query(F.data == "auth_resend_code")
async def cb_resend_code(call: CallbackQuery, state: FSMContext):
    tg_id = call.from_user.id
    uname = call.from_user.username

    data  = await state.get_data()
    email = data.get("verify_email", "")
    token = data.get("verify_token", user_tokens.get(tg_id))

    if not email:
        await call.answer("⚠️ Email не найден. Начни заново.", show_alert=True)
        await state.clear()
        return

    status, resp = await api_post(
        "/auth/email/send-code",
        {"email": email},
        token=token,
    )

    if status == 200:
        expires = resp.get("expires_at", "")[:16].replace("T", " ")
        log_event("EMAIL_CODE_RESENT", tg_id, uname, email=email)
        await call.message.edit_text(
            f"📨 <b>Новый код отправлен</b> на <code>{email}</code>\n"
            f"⏱ Действителен до: <b>{expires}</b>\n\n"
            "Введи 6-значный код из письма:",
            reply_markup=get_resend_code_keyboard(),
        )
        await state.set_state(EmailAuthStates.verify_code)

    elif status == 429:
        detail = resp.get("detail", "Слишком много запросов.")
        log_event("EMAIL_CODE_RATELIMIT", tg_id, uname, email=email)
        await call.answer(f"🚫 {detail}", show_alert=True)

    elif status == 400:
        # Email уже верифицирован
        await state.clear()
        await call.message.edit_text(
            "✅ <b>Email уже подтверждён!</b>\n\nВыбери действие 👇",
            reply_markup=get_main_keyboard(True, tg_id),
        )

    else:
        detail = resp.get("detail", "Ошибка отправки кода.")
        await call.answer(f"❌ {detail}", show_alert=True)

    await call.answer()


# ── Отмена авторизации ────────────────────────────────────────────────────

@dp.callback_query(F.data == "auth_cancel")
async def cb_auth_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    tg_id = call.from_user.id
    logged_in = tg_id in user_tokens
    await call.message.edit_text(
        "🌟 <b>Selftabs</b>\n\nВыбери действие 👇",
        reply_markup=get_main_keyboard(logged_in, tg_id),
    )
    await call.answer()


# ── Утилиты ───────────────────────────────────────────────────────────────

def _is_valid_email(email: str) -> bool:
    import re
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email))


async def _auto_link_telegram(tg_id: int, token: str):
    """Автоматически привязывает Telegram ID к аккаунту после входа/регистрации."""
    try:
        await api_post(
            "/api/v1/me/integrations/telegram",
            {"telegram_id": tg_id},
            token=token,
        )
    except Exception as e:
        logger.warning(f"[AUTO_LINK_TG] tg={tg_id} error={e}")


# ══════════════════════════════════════════════════════════════════════════
# НАВИГАЦИЯ (inline)
# ══════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data == "back_to_main")
async def back_to_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    tg_id = call.from_user.id
    logged_in = tg_id in user_tokens
    text = (
        "👋 <b>С возвращением в Selftabs!</b>\n\n"
        "📂 Управляй сессиями, подпиской и дайджестом прямо здесь.\n\n"
        "Выбери действие 👇"
        if logged_in else
        "🌟 <b>Selftabs — умное расширение для браузера</b>\n\n"
        "📧 Войди по email или через расширение Selftabs 👇"
    )
    await call.message.edit_text(text, reply_markup=get_main_keyboard(logged_in, tg_id))
    await call.answer()


@dp.callback_query(F.data == "login_extension")
async def cb_login_extension(call: CallbackQuery):
    await call.message.edit_text(
        "🔑 <b>Вход через расширение</b>\n\n"
        "1️⃣ Открой расширение <b>Selftabs</b> в браузере\n"
        "2️⃣ Войди в аккаунт в расширении\n"
        "3️⃣ Нажми кнопку <b>«Открыть бот»</b> — ты будешь авторизован автоматически\n\n"
        "<i>Telegram привяжется к аккаунту автоматически.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📧 Войти по Email", callback_data="auth_email")],
            [InlineKeyboardButton(text="🔙 Главное меню",   callback_data="back_to_main")],
        ]),
    )
    await call.answer()


@dp.callback_query(F.data == "profile")
async def cb_profile(call: CallbackQuery):
    tg_id = call.from_user.id
    token = user_tokens.get(tg_id)
    if not token:
        await call.message.edit_text("⚠️ Ты не авторизован.", reply_markup=get_main_keyboard(False))
        await call.answer()
        return

    status, user = await api_get("/me", token)

    if status == 401:
        user_tokens.pop(tg_id, None)
        await call.message.edit_text("⚠️ Сессия истекла. Войди заново.", reply_markup=get_main_keyboard(False))
        await call.answer()
        return

    if status != 200:
        await call.message.edit_text("❌ Не удалось загрузить профиль.", reply_markup=get_back_main_keyboard())
        await call.answer()
        return

    integrations = user.get("integrations", {})
    settings     = user.get("settings", {})
    auth         = user.get("auth", {})
    plan_key     = user.get("subscription_plan", "standard")
    expires      = user.get("subscription_expires_at")
    plan_emoji, plan_name = PLAN_NAMES.get(plan_key, ("🆓", plan_key.capitalize()))
    tg_linked = "✅ Привязан" if integrations.get("telegram_id") else "❌ Не привязан"
    google    = "✅ Привязан" if auth.get("google_linked") else "❌ Не привязан"
    email_ver = "✅" if auth.get("email_verified") else "⚠️ Не подтверждён"
    exp_str   = f"\n📅 До: <b>{expires[:10]}</b>" if expires and plan_key != "standard" else ""

    keyboard_rows = [
        [InlineKeyboardButton(text="💳 Подписка", callback_data="subscription")],
    ]
    # Если email не верифицирован — предлагаем подтвердить
    if not auth.get("email_verified"):
        keyboard_rows.insert(0, [
            InlineKeyboardButton(text="📧 Подтвердить Email", callback_data="verify_email_start")
        ])
    keyboard_rows.append([InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_main")])

    await call.message.edit_text(
        f"👤 <b>Мой профиль</b>\n\n"
        f"<b>{user.get('name') or 'Без имени'}</b>\n"
        f"📧 {user.get('email')} {email_ver}\n"
        f"🏷 @{user.get('username') or '—'}\n\n"
        f"{plan_emoji} Подписка: <b>{plan_name}</b>{exp_str}\n\n"
        f"🔗 Telegram: {tg_linked}\n"
        f"🔗 Google: {google}\n"
        f"🔒 2FA Telegram: {'✅' if settings.get('2fa_telegram') else '❌'}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
    )
    await call.answer()


# ── Верификация email из профиля (если ещё не верифицирован) ─────────────

@dp.callback_query(F.data == "verify_email_start")
async def cb_verify_email_start(call: CallbackQuery, state: FSMContext):
    tg_id = call.from_user.id
    token = user_tokens.get(tg_id)
    if not token:
        await call.answer("⚠️ Войди в аккаунт.", show_alert=True)
        return

    _, user = await api_get("/me", token)
    email   = user.get("email", "")

    send_status, send_resp = await api_post(
        "/auth/email/send-code",
        {"email": email},
        token=token,
    )

    await state.update_data(verify_email=email, verify_token=token)
    await state.set_state(EmailAuthStates.verify_code)

    if send_status == 200:
        expires = send_resp.get("expires_at", "")[:16].replace("T", " ")
        await call.message.edit_text(
            f"📧 Код отправлен на <code>{email}</code>\n"
            f"⏱ Действителен до: <b>{expires}</b>\n\n"
            "Введи 6-значный код:",
            reply_markup=get_resend_code_keyboard(),
        )
    elif send_status == 400:
        await call.message.edit_text(
            "✅ Email уже подтверждён.",
            reply_markup=get_back_main_keyboard(),
        )
        await state.clear()
    else:
        detail = send_resp.get("detail", "Ошибка отправки кода.")
        await call.answer(f"❌ {detail}", show_alert=True)

    await call.answer()


@dp.callback_query(F.data == "subscription")
async def cb_subscription(call: CallbackQuery):
    tg_id = call.from_user.id
    token = user_tokens.get(tg_id)
    current_plan = "standard"
    expires_at   = None

    if token:
        status, user = await api_get("/me", token)
        if status == 200:
            current_plan = user.get("subscription_plan", "standard")
            expires_at   = user.get("subscription_expires_at")

    plan_emoji, plan_name = PLAN_NAMES.get(current_plan, ("🆓", current_plan.capitalize()))
    exp_line = f"\n📅 Активна до: <b>{expires_at[:10]}</b>" if expires_at and current_plan != "standard" else ""

    await call.message.edit_text(
        f"💳 <b>Подписка Selftabs</b>\n\n"
        f"{plan_emoji} Текущий тариф: <b>{plan_name}</b>{exp_line}\n\n"
        "📋 <b>Доступные тарифы:</b>\n"
        "🚀 <b>Pro Pass</b> — 290 ₽/мес\n"
        "🏢 <b>Team Workspace</b> — 950 ₽/мес\n\n"
        "💳 <b>Способы оплаты:</b>\n"
        "💫 Telegram Stars · 🏦 СБП · 🪙 USDT\n\n"
        "<i>Подписка продлевается автоматически каждые 30 дней</i>",
        reply_markup=get_subscription_keyboard(current_plan),
    )
    await call.answer()


@dp.callback_query(F.data == "link_tg")
async def cb_link_tg(call: CallbackQuery):
    tg_id = call.from_user.id
    uname = call.from_user.username
    token = user_tokens.get(tg_id)
    if not token:
        await call.answer("⚠️ Сначала войди в аккаунт.", show_alert=True)
        return

    status, resp = await api_post(
        "/me/integrations/telegram",
        {"telegram_id": tg_id},
        token=token,
    )

    if status == 200:
        log_event("TG_LINKED_PROFILE", tg_id, uname)
        await call.message.edit_text(
            f"✅ <b>Telegram успешно привязан!</b>\n\n"
            f"Теперь ты будешь получать уведомления о подписке и дайджесты прямо в бот.\n\n"
            f"🆔 Твой Telegram ID: <code>{tg_id}</code>",
            reply_markup=get_back_main_keyboard(),
        )
    elif status == 409:
        log_event("TG_LINK_ALREADY", tg_id, uname)
        await call.message.edit_text(
            "ℹ️ Telegram уже привязан к этому аккаунту.",
            reply_markup=get_back_main_keyboard(),
        )
    elif status == 401:
        log_event("SESSION_EXPIRED", tg_id, uname, handler="link_tg")
        user_tokens.pop(tg_id, None)
        await call.message.edit_text("⚠️ Сессия истекла. Войди заново.", reply_markup=get_main_keyboard(False))
    else:
        log_error("TG_LINK_FAIL_PROFILE", tg_id, uname, http_status=status,
                  detail=resp.get("detail", "—"))
        await call.message.edit_text(
            f"❌ Ошибка: {resp.get('detail', 'Неизвестная ошибка')}",
            reply_markup=get_back_main_keyboard(),
        )
    await call.answer()


@dp.callback_query(F.data == "logout")
async def cb_logout(call: CallbackQuery, state: FSMContext):
    await state.clear()
    tg_id = call.from_user.id
    token = user_tokens.get(tg_id)
    if token:
        try:
            await api_delete("/me/integrations/telegram", token)
        except Exception as e:
            logging.warning(f"Ошибка при отвязке Telegram: {e}")
    user_tokens.pop(tg_id, None)
    await call.message.edit_text(
        "👋 <b>Ты вышел из аккаунта.</b>\n\nДо скорой встречи!",
        reply_markup=get_main_keyboard(False),
    )
    await call.answer()


# ══════════════════════════════════════════════════════════════════════════
# DAILY ДАЙДЖЕСТ
# ══════════════════════════════════════════════════════════════════════════

def _fmt_time(sec: int) -> str:
    if sec >= 3600:
        return f"{sec // 3600}ч {(sec % 3600) // 60}м"
    if sec >= 60:
        return f"{sec // 60}м"
    return "&lt;1м"


def _day_word(n: int) -> str:
    n = abs(n) % 100
    if 11 <= n <= 19:
        return "дней"
    n %= 10
    if n == 1:
        return "день"
    if 2 <= n <= 4:
        return "дня"
    return "дней"


def _sparkline(values: list[int]) -> str:
    bars = " ▁▂▃▄▅▆▇█"
    if not values or max(values) == 0:
        return "▁▁▁▁▁▁▁ (нет данных)"
    mx = max(values)
    return "".join(bars[min(8, round(v / mx * 8))] for v in values)


@dp.callback_query(F.data == "daily_digest")
async def cb_daily_digest(call: CallbackQuery):
    tg_id = call.from_user.id
    token = user_tokens.get(tg_id)
    if not token:
        await call.answer("⚠️ Сначала войди в аккаунт.", show_alert=True)
        return

    await call.answer("⏳ Загружаю дайджест...")

    me_status, me = await api_get("/me", token)
    if me_status == 401:
        user_tokens.pop(tg_id, None)
        await call.message.edit_text("⚠️ Сессия истекла. Войди заново.", reply_markup=get_main_keyboard(False))
        return
    if me_status != 200:
        await call.message.edit_text("❌ Не удалось загрузить профиль.", reply_markup=get_back_main_keyboard())
        return

    plan_key = me.get("subscription_plan", "standard")
    if plan_key not in ("pro", "team"):
        plan_emoji, plan_name = PLAN_NAMES.get(plan_key, ("🆓", plan_key.capitalize()))
        await call.message.edit_text(
            f"🔒 <b>Daily дайджест недоступен</b>\n\n"
            f"{plan_emoji} Текущий тариф: <b>{plan_name}</b>\n\n"
            "Подключи Pro или Team — и каждый день получай AI-сводку "
            "по твоим вкладкам, сессиям и топ-сайтам 🧠\n\n"
            "📋 <b>Тарифы:</b>\n"
            "🚀 <b>Pro Pass</b> — 290 ₽/мес\n"
            "🏢 <b>Team Workspace</b> — 950 ₽/мес",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚀 Купить Pro Pass",       callback_data="sub_info:pro")],
                [InlineKeyboardButton(text="🏢 Купить Team Workspace", callback_data="sub_info:team")],
                [InlineKeyboardButton(text="🔙 Главное меню",          callback_data="back_to_main")],
            ]),
        )
        return

    stats_status, stats = await api_get("/me/stats?period=day", token)
    act_status, activity = await api_get("/me/activity?days=7", token)

    today = datetime.now(timezone(timedelta(hours=NOTIFY_TZ_OFFSET)))
    date_str = today.strftime("%d.%m.%Y")
    weekday_map = {0: "Пн", 1: "Вт", 2: "Ср", 3: "Чт", 4: "Пт", 5: "Сб", 6: "Вс"}
    weekday_str = weekday_map[today.weekday()]
    plan_emoji, plan_name = PLAN_NAMES.get(plan_key, ("🚀", plan_key.capitalize()))

    lines = [
        f"📰 <b>Daily дайджест — {weekday_str}, {date_str}</b>",
        f"{plan_emoji} <i>Тариф: {plan_name}</i>",
        "",
    ]

    if stats_status == 200 and isinstance(stats, dict):
        tabs     = stats.get("tabs_parked", 0)
        sessions = stats.get("sessions_count", 0)
        opens    = stats.get("opens", 0)
        time_sec = stats.get("total_time_sec", 0)
        streak   = stats.get("current_streak", 0)
        top_d    = stats.get("top_domains", [])
        top_t    = stats.get("top_tags", [])

        lines += [
            "📊 <b>За сегодня:</b>",
            f"  📌 Вкладок сохранено:   <b>{tabs}</b>",
            f"  🗂 Сессий запарковано:  <b>{sessions}</b>",
            f"  🚀 Открытий расширения: <b>{opens}</b>",
            f"  ⏱ Время в расширении:  <b>{_fmt_time(time_sec)}</b>",
        ]
        if streak:
            lines.append(f"  🔥 Стрик: <b>{streak} {_day_word(streak)}</b>")

        if top_d:
            lines.append("\n🌐 <b>Топ-сайты сегодня:</b>")
            for i, d in enumerate(top_d[:5], 1):
                lines.append(f"  {i}. <code>{d['domain']}</code> — {d['park_count']} парк.")

        if top_t:
            lines.append("\n🏷 <b>Топ-теги:</b>")
            lines.append("  " + "  ".join(f"#{t['tag']}" for t in top_t[:6]))
    else:
        lines.append("<i>Статистика за сегодня пока недоступна.\nОткрой расширение и запаркуй первые вкладки!</i>")

    if act_status == 200 and isinstance(activity, list) and len(activity) > 1:
        bars = _sparkline([d.get("tabs_parked", 0) for d in activity[-7:]])
        lines += [
            "",
            "📈 <b>Активность за 7 дней (вкладки):</b>",
            f"  {bars}",
        ]

    await call.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить дайджест", callback_data="daily_digest")],
            [InlineKeyboardButton(text="📂 Мои сессии", callback_data="my_sessions:0")],
            [InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_main")],
        ]),
    )


# ══════════════════════════════════════════════════════════════════════════
# МОИ СЕССИИ (список с пагинацией)
# ══════════════════════════════════════════════════════════════════════════

SESSIONS_PAGE_SIZE = 5


@dp.callback_query(F.data.startswith("my_sessions:"))
async def cb_my_sessions(call: CallbackQuery):
    tg_id = call.from_user.id
    token = user_tokens.get(tg_id)
    if not token:
        await call.answer("⚠️ Сначала войди в аккаунт.", show_alert=True)
        return

    try:
        page = int(call.data.split(":")[1])
    except (IndexError, ValueError):
        page = 0

    await call.answer("⏳ Загружаю сессии...")

    sessions_status, sessions_data = await api_get("/sessions", token)
    stats_status, stats = await api_get("/me/stats?period=all", token)

    if sessions_status == 401 or stats_status == 401:
        user_tokens.pop(tg_id, None)
        await call.message.edit_text("⚠️ Сессия истекла. Войди заново.", reply_markup=get_main_keyboard(False))
        return

    api_errors = []
    if sessions_status != 200:
        detail = sessions_data.get("detail", str(sessions_data)) if isinstance(sessions_data, dict) else str(sessions_data)
        api_errors.append(f"⚠️ /api/v1/sessions → HTTP {sessions_status}: {detail}")
    if stats_status != 200:
        detail = stats.get("detail", str(stats)) if isinstance(stats, dict) else str(stats)
        api_errors.append(f"⚠️ /api/v1/me/stats → HTTP {stats_status}: {detail}")

    lines = ["📂 <b>Мои сохранённые сессии</b>"]
    if api_errors:
        lines += [""] + api_errors

    if stats_status == 200 and isinstance(stats, dict):
        total_tabs     = stats.get("tabs_parked", 0)
        total_sessions = stats.get("sessions_count", 0)
        total_time     = stats.get("total_time_sec", 0)
        streak_cur     = stats.get("current_streak", 0)
        streak_max     = stats.get("longest_streak", 0)

        lines += [
            "",
            "📊 <b>Итого за всё время:</b>",
            f"📌 Вкладок: <b>{total_tabs}</b> · 🗂 Сессий: <b>{total_sessions}</b>",
            f"⏱ Время: <b>{_fmt_time(total_time)}</b> · 🔥 Стрик: <b>{streak_cur}/{streak_max} {_day_word(streak_max)}</b>",
        ]

    keyboard_rows = []

    if sessions_status == 200 and isinstance(sessions_data, list):
        all_sessions = sessions_data
    elif sessions_status == 200 and isinstance(sessions_data, dict):
        all_sessions = sessions_data.get("items", sessions_data.get("sessions", []))
    else:
        all_sessions = []

    total_count   = len(all_sessions)
    start         = page * SESSIONS_PAGE_SIZE
    end           = start + SESSIONS_PAGE_SIZE
    sessions_page = all_sessions[start:end]

    if sessions_page:
        total_pages = max(1, (total_count + SESSIONS_PAGE_SIZE - 1) // SESSIONS_PAGE_SIZE)
        lines.append(f"\n🗂 <b>Сессии (стр. {page + 1}/{total_pages}):</b>")

        for s in sessions_page:
            tag         = s.get("context_tag") or "—"
            tabs_list   = s.get("tabs") or []
            created_raw = s.get("created_at") or ""

            try:
                created_dt  = datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
                created_str = created_dt.strftime("%d.%m.%Y %H:%M")
            except Exception:
                created_str = str(created_raw)[:10] if created_raw else "—"

            lines.append(f"\n📁 <b>{tag}</b> · {created_str}")

            for i, tab in enumerate(tabs_list[:10], 1):
                title = (tab.get("title") or "").strip()
                url   = tab.get("url") or ""
                label = title[:50] + "…" if len(title) > 50 else title
                if not label:
                    from urllib.parse import urlparse
                    label = urlparse(url).netloc or url[:40]
                lines.append(f"  {i}. <a href=\"{url}\">{label}</a>")

            if len(tabs_list) > 10:
                lines.append(f"  <i>...и ещё {len(tabs_list) - 10} вкладок</i>")

        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"my_sessions:{page - 1}"))
        if end < total_count:
            nav_buttons.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"my_sessions:{page + 1}"))
        if nav_buttons:
            keyboard_rows.append(nav_buttons)
    else:
        lines += [
            "",
            "🗂 <b>Сессии</b>",
            "<i>Пока нет сохранённых сессий.\nОткрой расширение Selftabs и запаркуй первые вкладки!</i>",
        ]

    keyboard_rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="my_sessions:0")])
    keyboard_rows.append([InlineKeyboardButton(text="📰 Daily дайджест", callback_data="daily_digest")])
    keyboard_rows.append([InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_main")])

    await call.message.edit_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
        disable_web_page_preview=True,
    )


# ── Описание планов ───────────────────────────────────────────────────────

@dp.callback_query(F.data == "plans:info")
async def plans_info(call: CallbackQuery):
    pro  = PLANS["pro"]
    team = PLANS["team"]
    await call.message.edit_text(
        f"📋 <b>Тарифы Selftabs</b>\n\n"
        f"🚀 <b>Pro Pass</b> — {pro['price_rub']}/мес\n"
        f"{pro['description']}\n\n"
        f"🏢 <b>Team Workspace</b> — {team['price_rub']}/мес\n"
        f"{team['description']}\n\n"
        "<i>Подписка автоматически продлевается каждые 30 дней.\n"
        "Отменить можно в любое время.</i>",
        reply_markup=get_plans_info_keyboard(),
    )
    await call.answer()


@dp.callback_query(F.data.startswith("sub_info:"))
async def show_plan_info(call: CallbackQuery):
    plan_key = call.data.split(":", 1)[1]
    plan = PLANS.get(plan_key)
    if not plan:
        await call.answer("Неизвестный план.", show_alert=True)
        return

    tg_id = call.from_user.id
    if tg_id not in user_tokens:
        await call.answer("⚠️ Сначала войди в аккаунт.", show_alert=True)
        return

    await call.message.edit_text(
        f"{plan['emoji']} <b>{plan['title']}</b> — {plan['price_rub']}/мес\n\n"
        f"{plan['description']}\n\n"
        "💳 <b>Выбери способ оплаты:</b>\n"
        f"💫 Telegram Stars — {plan['stars']} ⭐/мес\n"
        f"🏦 СБП — {plan['sbp_rub']} ₽/мес\n"
        f"🪙 USDT — {plan['usdt']}$ (~{plan['usdt_rub']} ₽)/мес\n\n"
        "<a href=\"https://selftabs.ru/privacy\">Политика конфиденциальности</a> · <a href=\"https://selftabs.ru/terms\">Пользовательское соглашение</a>\n\nНажми на кнопку ниже 👇",
        reply_markup=get_plan_payment_keyboard(plan_key),
    )
    await call.answer()


# ══════════════════════════════════════════════════════════════════════════
# ОПЛАТА — TELEGRAM STARS
# ══════════════════════════════════════════════════════════════════════════

async def _make_stars_invoice_markup(chat_id: int, plan_key: str) -> tuple[str, InlineKeyboardMarkup]:
    plan = PLANS[plan_key]
    invoice_link = await bot.create_invoice_link(
        title=plan["title"],
        description=plan["description"],
        payload=f"sub:{plan_key}:{chat_id}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=plan["title"], amount=plan["stars"])],
        subscription_period=2592000,
    )
    text = (
        f"💫 <b>Оплата через Telegram Stars</b>\n\n"
        f"{plan['emoji']} Тариф: <b>{plan['title']}</b>\n"
        f"💰 Стоимость: <b>{plan['stars']} ⭐/мес</b> (~{plan['price_rub']})\n"
        f"📅 Срок: 30 дней\n\n"
        "Нажми кнопку ниже — оплата в один клик 👇"
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💫 Оплатить Stars", url=invoice_link)],
        [InlineKeyboardButton(text="🔙 Назад", callback_data=f"sub_info:{plan_key}")],
    ])
    return text, markup


async def _send_stars_invoice_msg(chat_id: int, plan_key: str):
    text, markup = await _make_stars_invoice_markup(chat_id, plan_key)
    await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)


@dp.callback_query(F.data.startswith("buy_stars:"))
async def initiate_stars_purchase(call: CallbackQuery):
    plan_key = call.data.split(":", 1)[1]
    tg_id    = call.from_user.id
    uname    = call.from_user.username

    if tg_id not in user_tokens:
        await call.answer("⚠️ Сначала войди в аккаунт.", show_alert=True)
        return

    plan = PLANS.get(plan_key)
    if not plan:
        await call.answer("Неизвестный план.", show_alert=True)
        return

    log_event("STARS_INVOICE_INIT", tg_id, uname, plan=plan_key, stars=plan["stars"])
    await call.answer("⏳ Создаём счёт...")
    try:
        text, markup = await _make_stars_invoice_markup(tg_id, plan_key)
        await call.message.edit_text(text, reply_markup=markup)
    except Exception as e:
        log_error("STARS_INVOICE_FAIL", tg_id, uname, plan=plan_key, error=e)
        raise


@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    tg_id = query.from_user.id
    uname = query.from_user.username
    log_event("STARS_PRE_CHECKOUT", tg_id, uname,
              payload=query.invoice_payload, amount=query.total_amount)
    await query.answer(ok=True)


async def refund_stars(tg_id: int, charge_id: str) -> bool:
    try:
        await bot.refund_star_payment(
            user_id=tg_id,
            telegram_payment_charge_id=charge_id,
        )
        logging.info(f"[ТЕСТ] ⭐ Возврат звёзд выполнен: tg_id={tg_id}, charge_id={charge_id}")
        return True
    except Exception as e:
        logging.warning(f"[ТЕСТ] ⭐ Ошибка возврата звёзд: tg_id={tg_id}, charge_id={charge_id}, error={e}")
        return False


@dp.message(F.successful_payment)
async def payment_success(message: Message):
    tg_id   = message.from_user.id
    uname   = message.from_user.username
    payment = message.successful_payment

    payload_parts = payment.invoice_payload.split(":")
    if len(payload_parts) != 3 or payload_parts[0] != "sub":
        log_error("STARS_BAD_PAYLOAD", tg_id, uname,
                  payload=payment.invoice_payload,
                  charge_id=payment.telegram_payment_charge_id)
        await message.answer(
            "⚠️ Не удалось определить план.\n"
            "Сохрани ID платежа и свяжись с поддержкой:\n"
            f"<code>{payment.telegram_payment_charge_id}</code>",
            reply_markup=get_back_main_keyboard(),
        )
        return

    plan_key  = payload_parts[1]
    stars     = payment.total_amount
    charge_id = payment.telegram_payment_charge_id

    log_event("STARS_PAYMENT_RECEIVED", tg_id, uname, plan=plan_key,
              stars=stars, charge_id=charge_id)

    status, resp = await api_post(
        "/subscription/telegram-stars/activate",
        {
            "telegram_id":  tg_id,
            "plan":         plan_key,
            "stars_amount": stars,
            "charge_id":    charge_id,
            "bot_secret":   BOT_SECRET,
        },
    )

    if status == 200:
        plan_name  = PLANS[plan_key]["title"] if plan_key in PLANS else plan_key.capitalize()
        expires_at = resp.get("expires_at", "")[:10]
        log_event("STARS_SUB_ACTIVATED", tg_id, uname, plan=plan_key,
                  stars=stars, expires=expires_at, charge_id=charge_id)
        await message.answer(
            f"🎉 <b>Подписка активирована!</b>\n\n"
            f"📦 Тариф: <b>{plan_name}</b>\n"
            f"⭐ Списано: <b>{stars} Stars</b>\n"
            f"📅 Действует до: <b>{expires_at}</b>\n"
            f"🔄 Продление — автоматически через 30 дней\n\n"
            "Вернись в расширение — статус уже обновлён 🚀",
            reply_markup=get_main_keyboard(True),
        )

        # ── 🧪 ТОЛЬКО ДЛЯ ТЕСТИРОВАНИЯ — убрать перед продом ──────────────
        refunded = await refund_stars(tg_id, charge_id)
        if refunded:
            await message.answer(
                f"🧪 <b>[ТЕСТ] Звёзды возвращены</b>\n\n"
                f"⭐ Возврат: <b>{stars} Stars</b>\n"
                f"🔖 Charge ID: <code>{charge_id}</code>\n\n"
                "<i>Это сообщение видно только во время тестирования.\n"
                "Удали вызов refund_stars() перед выходом в прод.</i>",
            )
        # ───────────────────────────────────────────────────────────────────

    else:
        detail = resp.get("detail", "Неизвестная ошибка")
        log_error("STARS_ACTIVATE_FAIL", tg_id, uname, plan=plan_key,
                  stars=stars, charge_id=charge_id, http_status=status, detail=detail)
        await message.answer(
            f"⚠️ <b>Оплата прошла, но возникла ошибка активации.</b>\n\n"
            f"Причина: {detail}\n\n"
            "Сохрани ID платежа и свяжись с поддержкой:\n"
            f"<code>{charge_id}</code>",
            reply_markup=get_back_main_keyboard(),
        )


# ══════════════════════════════════════════════════════════════════════════
# ОПЛАТА — СБП (Platega)
# ══════════════════════════════════════════════════════════════════════════

async def _platega_create(plan_key: str, method: str, user_id: str | None) -> dict:
    """
    Создаёт транзакцию через /api/v1/payments/platega/create-internal.
    Возвращает dict с transaction_id и redirect_url.
    """
    headers = {
        "Content-Type":  "application/json",
        "X-Bot-Secret":  PLATEGA_BOT_SECRET,
    }
    payload = {"plan": plan_key, "method": method}
    if user_id:
        payload["user_id"] = user_id

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{API_URL}/payments/platega/create-internal",
            json=payload,
            headers=headers,
        ) as r:
            try:
                data = await r.json()
            except Exception:
                data = {}
            if r.status not in (200, 201):
                detail = data.get("detail", str(data))[:200]
                raise RuntimeError(f"Бэк вернул {r.status}: {detail}")
            return data


async def _platega_check_internal(transaction_id: str) -> str:
    """
    Опрашивает /api/v1/payments/platega/internal/{txn_id}.
    Возвращает status: PENDING | CONFIRMED | CANCELED
    """
    headers = {"X-Bot-Secret": PLATEGA_BOT_SECRET}
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{API_URL}/payments/platega/internal/{transaction_id}",
            headers=headers,
        ) as r:
            try:
                data = await r.json()
            except Exception:
                data = {}
            return data.get("status", "PENDING")


@dp.callback_query(F.data.startswith("buy_sbp:"))
async def initiate_sbp_purchase(call: CallbackQuery):
    plan_key = call.data.split(":", 1)[1]
    tg_id    = call.from_user.id
    uname    = call.from_user.username

    if tg_id not in user_tokens:
        await call.answer("⚠️ Сначала войди в аккаунт.", show_alert=True)
        return

    plan = PLANS.get(plan_key)
    if not plan:
        await call.answer("Неизвестный план.", show_alert=True)
        return

    log_event("SBP_INVOICE_INIT", tg_id, uname, plan=plan_key, rub=plan["sbp_rub"])
    await call.answer("⏳ Создаём платёж...")

    # Получаем user_id бэкенда из токена для корректной привязки
    token   = user_tokens.get(tg_id)
    user_id = None
    if token:
        s, u = await api_get("/me", token)
        if s == 200:
            user_id = u.get("id")

    try:
        resp = await _platega_create(plan_key, "sbp", user_id)
    except Exception as e:
        log_error("SBP_INVOICE_FAIL", tg_id, uname, plan=plan_key, error=str(e))
        await call.message.edit_text(
            f"❌ Ошибка создания платежа: {str(e)[:200]}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📩 Поддержка", url="https://t.me/selftabs_support")],
                [InlineKeyboardButton(text="🔙 Назад",     callback_data=f"sub_info:{plan_key}")],
            ]),
        )
        return

    txn_id      = resp["transaction_id"]
    pay_url     = resp["redirect_url"]
    amount_rub  = int(resp.get("amount", plan["sbp_rub"]))
    log_event("SBP_INVOICE_CREATED", tg_id, uname, plan=plan_key,
              txn_id=txn_id, amount_rub=amount_rub)

    await call.message.edit_text(
        f"🏦 <b>Оплата через СБП</b>\n\n"
        f"{plan['emoji']} Тариф: <b>{plan['title']}</b>\n"
        f"💰 Сумма: <b>{amount_rub} ₽ / месяц</b>\n"
        f"📅 Срок: 30 дней\n\n"
        "1️⃣ Нажми «Оплатить» — откроется страница оплаты\n"
        "2️⃣ Оплати через СБП и вернись сюда\n"
        "3️⃣ Нажми «✅ Я оплатил — проверить»\n\n"
        f"<i>🔐 ID транзакции: <code>{txn_id}</code></i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"🏦 Оплатить {amount_rub} ₽", url=pay_url)],
            [InlineKeyboardButton(
                text="✅ Я оплатил — проверить",
                callback_data=f"check_sbp:{plan_key}:{txn_id}",
            )],
            [InlineKeyboardButton(text="🔙 Назад", callback_data=f"sub_info:{plan_key}")],
        ]),
    )


@dp.callback_query(F.data.startswith("check_sbp:"))
async def check_sbp_payment(call: CallbackQuery):
    parts    = call.data.split(":")
    plan_key = parts[1]
    txn_id   = parts[2] if len(parts) > 2 else ""
    tg_id    = call.from_user.id
    uname    = call.from_user.username
    plan     = PLANS.get(plan_key, {})

    log_event("SBP_CHECK", tg_id, uname, plan=plan_key, txn_id=txn_id)
    await call.answer("🔄 Проверяем оплату...")

    if not txn_id:
        await call.message.edit_text(
            "❌ Не удалось определить ID транзакции. Обратись в поддержку.",
            reply_markup=get_back_main_keyboard(),
        )
        return

    try:
        status = await _platega_check_internal(txn_id)
    except Exception as e:
        log_error("SBP_CHECK_FAIL", tg_id, uname, plan=plan_key, txn_id=txn_id, error=str(e))
        await call.message.edit_text(
            f"❌ Ошибка проверки статуса: {str(e)[:200]}",
            reply_markup=get_back_main_keyboard(),
        )
        return

    if status == "CONFIRMED":
        log_event("SBP_SUB_ACTIVATED", tg_id, uname, plan=plan_key, txn_id=txn_id)
        await call.message.edit_text(
            f"🎉 <b>Подписка активирована!</b>\n\n"
            f"{plan.get('emoji', '')} Тариф: <b>{plan.get('title', plan_key)}</b>\n"
            f"🏦 Способ: СБП (Platega)\n"
            f"💰 Оплачено: {plan.get('sbp_rub')} ₽\n"
            f"📅 Срок: 30 дней\n\n"
            "Вернись в расширение — статус уже обновлён 🚀",
            reply_markup=get_main_keyboard(True, tg_id),
        )
    elif status == "CANCELED":
        log_event("SBP_CANCELED", tg_id, uname, plan=plan_key, txn_id=txn_id)
        await call.message.edit_text(
            "❌ <b>Платёж отменён или не прошёл.</b>\n\n"
            "Попробуй ещё раз или выбери другой способ оплаты.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Попробовать снова", callback_data=f"buy_sbp:{plan_key}")],
                [InlineKeyboardButton(text="📩 Поддержка",         url="https://t.me/selftabs_support")],
                [InlineKeyboardButton(text="🔙 Назад",             callback_data="subscription")],
            ]),
        )
    else:  # PENDING
        log_event("SBP_NOT_CONFIRMED", tg_id, uname, plan=plan_key, txn_id=txn_id)
        await call.message.edit_text(
            "⏳ <b>Оплата ещё не подтверждена.</b>\n\n"
            "Platega обычно подтверждает платёж за 10–30 секунд.\n"
            "Подожди немного и попробуй снова:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="🔄 Проверить снова",
                    callback_data=f"check_sbp:{plan_key}:{txn_id}",
                )],
                [InlineKeyboardButton(text="📩 Поддержка", url="https://t.me/selftabs_support")],
                [InlineKeyboardButton(text="🔙 Назад",     callback_data="subscription")],
            ]),
        )


# ══════════════════════════════════════════════════════════════════════════
# ОПЛАТА — КРИПТА (Platega)
# ══════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data.startswith("buy_crypto:"))
async def initiate_crypto_purchase(call: CallbackQuery):
    plan_key = call.data.split(":", 1)[1]
    tg_id    = call.from_user.id
    uname    = call.from_user.username

    if tg_id not in user_tokens:
        await call.answer("⚠️ Сначала войди в аккаунт.", show_alert=True)
        return

    plan = PLANS.get(plan_key)
    if not plan:
        await call.answer("Неизвестный план.", show_alert=True)
        return

    log_event("CRYPTO_INVOICE_INIT", tg_id, uname, plan=plan_key, usdt=plan["usdt"])
    await call.answer("🔄 Создание крипто-счёта...")

    # Получаем user_id бэкенда для привязки платежа
    token   = user_tokens.get(tg_id)
    user_id = None
    if token:
        s, u = await api_get("/me", token)
        if s == 200:
            user_id = u.get("id")

    try:
        resp = await _platega_create(plan_key, "crypto", user_id)
    except Exception as e:
        log_error("CRYPTO_INVOICE_FAIL", tg_id, uname, plan=plan_key, error=str(e))
        await call.message.edit_text(
            f"❌ Ошибка создания счёта: {str(e)[:200]}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📩 Поддержка", url="https://t.me/selftabs_support")],
                [InlineKeyboardButton(text="🔙 Назад",     callback_data=f"sub_info:{plan_key}")],
            ]),
        )
        return

    txn_id   = resp["transaction_id"]
    pay_url  = resp["redirect_url"]
    log_event("CRYPTO_INVOICE_CREATED", tg_id, uname, plan=plan_key,
              txn_id=txn_id, usdt=plan["usdt"])

    await call.message.edit_text(
        f"🪙 <b>Оплата криптовалютой</b>\n\n"
        f"{plan['emoji']} Тариф: <b>{plan['title']}</b>\n"
        f"💰 Сумма: <b>~{plan['usdt']}$</b> (~{plan['usdt_rub']} ₽)\n"
        f"📅 Срок: 30 дней\n\n"
        "1️⃣ Нажми «Оплатить» — откроется страница Platega\n"
        "2️⃣ Выбери криптовалюту и отправь платёж\n"
        "3️⃣ Вернись и нажми «✅ Проверить оплату»\n\n"
        f"<i>🔐 ID транзакции: <code>{txn_id}</code></i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"🪙 Оплатить ~{plan['usdt']}$", url=pay_url)],
            [InlineKeyboardButton(
                text="✅ Проверить оплату",
                callback_data=f"check_crypto:{txn_id}:{plan_key}",
            )],
            [InlineKeyboardButton(text="🔄 Новая ссылка", callback_data=f"buy_crypto:{plan_key}")],
            [InlineKeyboardButton(text="🔙 Назад",        callback_data=f"sub_info:{plan_key}")],
        ]),
    )


@dp.callback_query(F.data.startswith("check_crypto:"))
async def check_crypto_payment(call: CallbackQuery):
    parts    = call.data.split(":")
    txn_id   = parts[1]
    plan_key = parts[2] if len(parts) > 2 else ""
    tg_id    = call.from_user.id
    uname    = call.from_user.username
    plan     = PLANS.get(plan_key, {})

    log_event("CRYPTO_CHECK", tg_id, uname, plan=plan_key, txn_id=txn_id)
    await call.answer("🔄 Проверка оплаты...")

    try:
        status = await _platega_check_internal(txn_id)
    except Exception as e:
        log_error("CRYPTO_CHECK_EXCEPTION", tg_id, uname, plan=plan_key, txn_id=txn_id, error=str(e))
        await call.message.edit_text(
            f"❌ Ошибка проверки: {str(e)[:200]}",
            reply_markup=get_back_main_keyboard(),
        )
        return

    if status == "CONFIRMED":
        log_event("CRYPTO_SUB_ACTIVATED", tg_id, uname, plan=plan_key, txn_id=txn_id)
        await call.message.edit_text(
            f"🎉 <b>Оплата получена!</b>\n\n"
            f"{plan.get('emoji', '')} Тариф: <b>{plan.get('title', plan_key)}</b>\n"
            f"🪙 Оплачено: <b>~{plan.get('usdt')}$</b> (~{plan.get('usdt_rub')} ₽)\n"
            f"📅 Подписка активирована на 30 дней\n\n"
            "Вернись в расширение — статус уже обновлён 🚀",
            reply_markup=get_main_keyboard(True, tg_id),
        )
    elif status == "CANCELED":
        log_event("CRYPTO_CANCELED", tg_id, uname, plan=plan_key, txn_id=txn_id)
        await call.message.edit_text(
            "❌ <b>Платёж отменён или истёк.</b>\n\nВернись назад и создай новый счёт.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Новая ссылка", callback_data=f"buy_crypto:{plan_key}")],
                [InlineKeyboardButton(text="🔙 Назад",        callback_data="subscription")],
            ]),
        )
    else:  # PENDING
        log_event("CRYPTO_PENDING", tg_id, uname, plan=plan_key, txn_id=txn_id)
        await call.message.edit_text(
            f"⏳ <b>Ожидание оплаты...</b>\n\n"
            "Крипто-переводы могут подтверждаться 1–5 минут.\n\n"
            f"<i>ID: <code>{txn_id}</code></i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="🔄 Проверить снова",
                    callback_data=f"check_crypto:{txn_id}:{plan_key}",
                )],
                [InlineKeyboardButton(text="🔄 Новая ссылка", callback_data=f"buy_crypto:{plan_key}")],
                [InlineKeyboardButton(text="📩 Поддержка",    url="https://t.me/selftabs_support")],
                [InlineKeyboardButton(text="🔙 Назад",        callback_data="subscription")],
            ]),
        )


# ══════════════════════════════════════════════════════════════════════════
# АДМИН-ПАНЕЛЬ
# ══════════════════════════════════════════════════════════════════════════

def _is_admin(tg_id: int) -> bool:
    return tg_id == ADMIN_ID


@dp.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer(
        "🛡 <b>Админ-панель Selftabs</b>\n\nВыбери действие:",
        reply_markup=get_admin_keyboard(),
    )


@dp.callback_query(F.data == "admin_panel")
async def cb_admin_panel(call: CallbackQuery, state: FSMContext):
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Доступ запрещён.", show_alert=True)
        return
    await state.clear()
    await call.message.edit_text(
        "🛡 <b>Админ-панель Selftabs</b>\n\nВыбери действие:",
        reply_markup=get_admin_keyboard(),
    )
    await call.answer()


@dp.callback_query(F.data == "admin_view_profile")
async def cb_admin_view_profile_ask(call: CallbackQuery, state: FSMContext):
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Доступ запрещён.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_user_id_profile)
    await call.message.edit_text(
        "👤 <b>Просмотр профиля</b>\n\nВведи <b>Telegram ID</b> пользователя:",
        reply_markup=get_admin_cancel_keyboard(),
    )
    await call.answer()


@dp.message(AdminStates.waiting_user_id_profile)
async def admin_view_profile_handle(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    text = message.text.strip() if message.text else ""
    if not text.lstrip("-").isdigit():
        await message.answer("⚠️ Введи корректный числовой Telegram ID.", reply_markup=get_admin_cancel_keyboard())
        return

    target_id = int(text)
    await state.clear()

    token = user_tokens.get(target_id)
    if not token:
        await message.answer(
            f"❌ Пользователь <code>{target_id}</code> не найден в боте (не авторизован).\n\n"
            "Для выдачи подписки токен не нужен — используй «🎁 Выдать подписку».",
            reply_markup=get_admin_keyboard(),
        )
        return

    status, user = await api_get("/me", token)
    if status == 401:
        await message.answer(f"⚠️ Сессия пользователя <code>{target_id}</code> истекла.", reply_markup=get_admin_keyboard())
        return
    if status != 200:
        await message.answer(f"❌ Не удалось загрузить профиль (HTTP {status}).", reply_markup=get_admin_keyboard())
        return

    integrations = user.get("integrations", {})
    settings     = user.get("settings", {})
    auth         = user.get("auth", {})
    plan_key     = user.get("subscription_plan", "standard")
    expires      = user.get("subscription_expires_at")
    plan_emoji, plan_name = PLAN_NAMES.get(plan_key, ("🆓", plan_key.capitalize()))
    tg_linked = "✅" if integrations.get("telegram_id") else "❌"
    google    = "✅" if auth.get("google_linked") else "❌"
    exp_str   = f"\n📅 До: <b>{expires[:10]}</b>" if expires and plan_key != "standard" else ""

    await message.answer(
        f"👤 <b>Профиль пользователя</b>\n"
        f"🆔 TG ID: <code>{target_id}</code>\n\n"
        f"<b>{user.get('name') or 'Без имени'}</b>\n"
        f"📧 {user.get('email', '—')}\n"
        f"🏷 @{user.get('username') or '—'}\n\n"
        f"{plan_emoji} Подписка: <b>{plan_name}</b>{exp_str}\n\n"
        f"🔗 Telegram: {tg_linked}  |  Google: {google}\n"
        f"🔒 2FA TG: {'✅' if settings.get('2fa_telegram') else '❌'}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎁 Выдать подписку этому пользователю",
                                  callback_data=f"admin_grant_to:{target_id}")],
            [InlineKeyboardButton(text="🔙 Админ-панель", callback_data="admin_panel")],
        ]),
    )


@dp.callback_query(F.data == "admin_grant_sub")
async def cb_admin_grant_sub_ask(call: CallbackQuery, state: FSMContext):
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Доступ запрещён.", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_user_id_sub)
    await call.message.edit_text(
        "🎁 <b>Выдать подписку</b>\n\nВведи <b>Telegram ID</b> пользователя:",
        reply_markup=get_admin_cancel_keyboard(),
    )
    await call.answer()


@dp.callback_query(F.data.startswith("admin_grant_to:"))
async def cb_admin_grant_to(call: CallbackQuery, state: FSMContext):
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Доступ запрещён.", show_alert=True)
        return
    target_id = int(call.data.split(":")[1])
    await state.update_data(target_tg_id=target_id)
    await state.set_state(AdminStates.waiting_sub_plan)
    await call.message.edit_text(
        f"🎁 <b>Выдать подписку</b>\nПользователь: <code>{target_id}</code>\n\nВыбери план:",
        reply_markup=get_admin_plan_keyboard(target_id),
    )
    await call.answer()


@dp.message(AdminStates.waiting_user_id_sub)
async def admin_grant_sub_user_handle(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    text = message.text.strip() if message.text else ""
    if not text.lstrip("-").isdigit():
        await message.answer("⚠️ Введи корректный числовой Telegram ID.", reply_markup=get_admin_cancel_keyboard())
        return

    target_id = int(text)
    await state.update_data(target_tg_id=target_id)
    await state.set_state(AdminStates.waiting_sub_plan)
    await message.answer(
        f"🎁 <b>Выдать подписку</b>\nПользователь: <code>{target_id}</code>\n\nВыбери план:",
        reply_markup=get_admin_plan_keyboard(target_id),
    )


@dp.callback_query(F.data.startswith("admin_plan:"))
async def cb_admin_plan_selected(call: CallbackQuery, state: FSMContext):
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Доступ запрещён.", show_alert=True)
        return
    parts = call.data.split(":")
    target_id = int(parts[1])
    plan_key  = parts[2]

    await state.update_data(target_tg_id=target_id, plan_key=plan_key)

    if plan_key == "standard":
        await _admin_apply_subscription(call, state, target_id, plan_key, days=0)
        return

    await state.set_state(AdminStates.waiting_sub_days)
    plan_emoji, plan_name = PLAN_NAMES.get(plan_key, ("🚀", plan_key))
    await call.message.edit_text(
        f"🎁 <b>Выдать подписку</b>\n"
        f"Пользователь: <code>{target_id}</code>\n"
        f"Тариф: {plan_emoji} <b>{plan_name}</b>\n\n"
        "На сколько дней? Введи число (например, <b>30</b>):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="30 дней",  callback_data=f"admin_days:{target_id}:{plan_key}:30")],
            [InlineKeyboardButton(text="90 дней",  callback_data=f"admin_days:{target_id}:{plan_key}:90")],
            [InlineKeyboardButton(text="365 дней", callback_data=f"admin_days:{target_id}:{plan_key}:365")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_panel")],
        ]),
    )
    await call.answer()


@dp.callback_query(F.data.startswith("admin_days:"))
async def cb_admin_days_quick(call: CallbackQuery, state: FSMContext):
    if not _is_admin(call.from_user.id):
        await call.answer("⛔ Доступ запрещён.", show_alert=True)
        return
    parts     = call.data.split(":")
    target_id = int(parts[1])
    plan_key  = parts[2]
    days      = int(parts[3])
    await _admin_apply_subscription(call, state, target_id, plan_key, days)


@dp.message(AdminStates.waiting_sub_days)
async def admin_sub_days_handle(message: Message, state: FSMContext):
    if not _is_admin(message.from_user.id):
        return
    text = message.text.strip() if message.text else ""
    if not text.isdigit() or int(text) <= 0:
        await message.answer("⚠️ Введи целое положительное число дней.", reply_markup=get_admin_cancel_keyboard())
        return
    data      = await state.get_data()
    target_id = data.get("target_tg_id")
    plan_key  = data.get("plan_key", "pro")
    days      = int(text)
    await state.clear()
    await _admin_apply_sub_message(message, target_id, plan_key, days)


async def _admin_apply_subscription(call: CallbackQuery, state: FSMContext, target_id: int, plan_key: str, days: int):
    await state.clear()
    await call.answer("⏳ Применяю...")
    success, result_text = await _do_grant_subscription(target_id, plan_key, days)
    await call.message.edit_text(
        result_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Админ-панель", callback_data="admin_panel")],
        ]),
    )


async def _admin_apply_sub_message(message: Message, target_id: int, plan_key: str, days: int):
    success, result_text = await _do_grant_subscription(target_id, plan_key, days)
    await message.answer(
        result_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Админ-панель", callback_data="admin_panel")],
        ]),
    )


async def _do_grant_subscription(target_tg_id: int, plan_key: str, days: int) -> tuple[bool, str]:
    plan_emoji, plan_name = PLAN_NAMES.get(plan_key, ("🆓", plan_key))
    try:
        headers = {"Content-Type": "application/json", "X-Bot-Secret": BOT_SECRET}
        payload = {"telegram_id": target_tg_id, "plan": plan_key, "days": days}
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{API_URL}/admin/subscription", json=payload, headers=headers) as r:
                try:
                    resp = await r.json()
                except Exception:
                    resp = {}
                http_status = r.status

        if http_status in (200, 201):
            if plan_key == "standard":
                text = (
                    f"✅ <b>Подписка сброшена</b>\n\n"
                    f"👤 Пользователь: <code>{target_tg_id}</code>\n"
                    f"🆓 Тариф: <b>Self Free</b>"
                )
            else:
                expires_at = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%d.%m.%Y")
                text = (
                    f"✅ <b>Подписка выдана</b>\n\n"
                    f"👤 Пользователь: <code>{target_tg_id}</code>\n"
                    f"{plan_emoji} Тариф: <b>{plan_name}</b>\n"
                    f"📅 Дней: <b>{days}</b>\n"
                    f"⏳ До: <b>{expires_at}</b>"
                )
            try:
                user_notify_text = (
                    "ℹ️ <b>Ваша подписка была изменена администратором.</b>\n\n"
                    "Текущий тариф: 🆓 <b>Self Free</b>"
                    if plan_key == "standard" else
                    f"🎉 <b>Вам выдана подписка!</b>\n\n"
                    f"{plan_emoji} Тариф: <b>{plan_name}</b>\n"
                    f"📅 Активна: <b>{days} дней</b>\n\n"
                    "Приятного использования Selftabs! 🚀"
                )
                await bot.send_message(target_tg_id, user_notify_text)
            except Exception as e:
                logger.warning(f"[ADMIN_GRANT] не удалось уведомить {target_tg_id}: {e}")
                text += "\n\n⚠️ <i>Не удалось отправить уведомление пользователю.</i>"
            return True, text
        else:
            detail = resp.get("detail", str(resp))
            return False, (
                f"❌ <b>Ошибка API</b> (HTTP {http_status})\n\n"
                f"👤 Пользователь: <code>{target_tg_id}</code>\n"
                f"Детали: <code>{detail}</code>"
            )
    except Exception as e:
        logger.error(f"[ADMIN_GRANT] exception target={target_tg_id}: {e}")
        return False, f"❌ <b>Исключение при запросе к API:</b>\n<code>{str(e)[:300]}</code>"


# ══════════════════════════════════════════════════════════════════════════
# ПЛАНИРОВЩИК УВЕДОМЛЕНИЙ
# ══════════════════════════════════════════════════════════════════════════

def _local_now() -> datetime:
    tz = timezone(timedelta(hours=NOTIFY_TZ_OFFSET))
    return datetime.now(tz).replace(tzinfo=None)


async def _safe_send(tg_id: int, text: str, reply_markup=None):
    try:
        await bot.send_message(chat_id=tg_id, text=text, parse_mode="HTML", reply_markup=reply_markup)
    except Exception as e:
        logging.warning(f"[scheduler] не удалось отправить {tg_id}: {e}")


async def check_subscription_expiry():
    now = datetime.now(timezone.utc)
    for tg_id, token in user_tokens.items():
        try:
            status, user = await api_get("/me", token)
        except Exception:
            continue
        if status != 200:
            if status == 401:
                user_tokens.pop(tg_id, None)
            continue

        plan_key    = user.get("subscription_plan", "standard")
        expires_raw = user.get("subscription_expires_at")
        if plan_key == "standard" or not expires_raw:
            continue

        try:
            expires_at = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
        except Exception:
            continue

        delta      = expires_at - now
        total_secs = delta.total_seconds()
        plan_emoji, plan_name = PLAN_NAMES.get(plan_key, ("🚀", plan_key.capitalize()))
        exp_fmt  = expires_at.strftime("%d.%m.%Y %H:%M")
        date_tag = expires_at.strftime("%Y-%m-%d")

        renew_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔄 Продлить подписку", callback_data=f"sub_info:{plan_key}")
        ]])

        if 604800 >= total_secs > 600800:
            key = "sub_7d"
            if not notif_log.already_sent(tg_id, key, date_tag):
                await _safe_send(tg_id,
                    f"🔔 <b>Подписка истекает через 7 дней</b>\n\n"
                    f"{plan_emoji} Тариф: <b>{plan_name}</b>\n"
                    f"📅 Истекает: <b>{exp_fmt}</b>\n\n"
                    "Продли заранее — доступ не прервётся 👇", renew_kb)
                notif_log.mark_sent(tg_id, key, date_tag)

        elif 86400 >= total_secs > 82800:
            key = "sub_1d"
            if not notif_log.already_sent(tg_id, key, date_tag):
                await _safe_send(tg_id,
                    f"⚠️ <b>Подписка истекает завтра!</b>\n\n"
                    f"{plan_emoji} Тариф: <b>{plan_name}</b>\n"
                    f"📅 Истекает: <b>{exp_fmt}</b>\n\n"
                    "Продли сейчас, чтобы не потерять доступ 👇", renew_kb)
                notif_log.mark_sent(tg_id, key, date_tag)

        elif 7200 >= total_secs > 6300:
            key = "sub_2h"
            if not notif_log.already_sent(tg_id, key, date_tag):
                await _safe_send(tg_id,
                    f"🚨 <b>Подписка истекает менее чем через 2 часа!</b>\n\n"
                    f"{plan_emoji} Тариф: <b>{plan_name}</b>\n"
                    f"📅 Истекает: <b>{exp_fmt}</b>\n\n"
                    "Продли прямо сейчас — ещё не поздно 👇", renew_kb)
                notif_log.mark_sent(tg_id, key, date_tag)


async def expiry_scheduler():
    logging.info("[scheduler] expiry_scheduler запущен")
    while True:
        try:
            await check_subscription_expiry()
        except Exception as e:
            logging.error(f"[scheduler] ошибка expiry: {e}")
        await asyncio.sleep(15 * 60)


async def send_daily_digest():
    today_tag = _local_now().strftime("%Y-%m-%d")
    for tg_id, token in user_tokens.items():
        try:
            status, user = await api_get("/me", token)
        except Exception:
            continue
        if status != 200:
            continue
        plan_key = user.get("subscription_plan", "standard")
        if plan_key not in ("pro", "team"):
            continue
        if notif_log.already_sent(tg_id, "daily", today_tag):
            continue
        plan_emoji, plan_name = PLAN_NAMES[plan_key]
        await _safe_send(tg_id,
            f"📰 <b>Твой ежедневный дайджест готов!</b>\n\n"
            f"{plan_emoji} Тариф: <b>{plan_name}</b>\n\n"
            "Selftabs собрал для тебя ключевые обновления по твоим темам 🧠\n\n"
            "Открой расширение, чтобы прочитать AI-сводку за сегодня.",
            InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="📖 Открыть дайджест", url="https://selftabs.com/digest")
            ]]),
        )
        notif_log.mark_sent(tg_id, "daily", today_tag)
        await asyncio.sleep(0.05)


async def send_weekly_digest():
    today_tag = _local_now().strftime("%Y-%W")
    for tg_id, token in user_tokens.items():
        try:
            status, user = await api_get("/me", token)
        except Exception:
            continue
        if status != 200:
            continue
        plan_key = user.get("subscription_plan", "standard")
        if plan_key not in ("pro", "team"):
            continue
        if notif_log.already_sent(tg_id, "weekly", today_tag):
            continue
        plan_emoji, plan_name = PLAN_NAMES[plan_key]
        await _safe_send(tg_id,
            f"📊 <b>Еженедельный AI-дайджест готов!</b>\n\n"
            f"{plan_emoji} Тариф: <b>{plan_name}</b>\n\n"
            "Selftabs подготовил сводку за прошедшую неделю:\n"
            "• 🔥 Главные темы и тренды\n"
            "• 📌 Самые важные вкладки\n"
            "• 🧠 AI-выводы по твоим интересам\n\n"
            "Открой расширение, чтобы прочитать полный отчёт 👇",
            InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="📖 Открыть weekly-дайджест", url="https://selftabs.com/digest/weekly")
            ]]),
        )
        notif_log.mark_sent(tg_id, "weekly", today_tag)
        await asyncio.sleep(0.05)


async def digest_scheduler():
    logging.info(f"[scheduler] digest_scheduler запущен (UTC+{NOTIFY_TZ_OFFSET}, {NOTIFY_HOUR}:00)")
    while True:
        now_local = _local_now()
        target    = now_local.replace(hour=NOTIFY_HOUR, minute=0, second=0, microsecond=0)
        if now_local >= target:
            target += timedelta(days=1)
        sleep_secs = (target - now_local).total_seconds()
        logging.info(f"[scheduler] следующий дайджест через {sleep_secs/3600:.1f} ч ({target.strftime('%d.%m %H:%M')})")
        await asyncio.sleep(sleep_secs)

        try:
            await send_daily_digest()
        except Exception as e:
            logging.error(f"[scheduler] ошибка daily digest: {e}")

        if _local_now().weekday() == 6:
            try:
                await send_weekly_digest()
            except Exception as e:
                logging.error(f"[scheduler] ошибка weekly digest: {e}")

        try:
            notif_log.cleanup_old()
        except Exception:
            pass

        await asyncio.sleep(60)


# ── Запуск ────────────────────────────────────────────────────────────────

async def main():
    global crypto
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if CRYPTO_PAY_TOKEN:
        crypto = AioCryptoPay(token=CRYPTO_PAY_TOKEN, network=CRYPTO_NETWORK)
        try:
            me = await crypto.get_me()
            logging.info(f"✅ CryptoPay подключён: {me.name}")
        except Exception as e:
            logging.warning(f"⚠️ CryptoPay ошибка: {e}")
    else:
        logging.warning("⚠️ CRYPTO_PAY_TOKEN не задан — крипто-оплата недоступна")

    # ── Health-check HTTP-сервер для Render ───────────────────────────────
    async def _health(request):
        return web.Response(text="OK")

    _app = web.Application()
    _app.router.add_get("/",       _health)
    _app.router.add_get("/health", _health)

    _runner = web.AppRunner(_app)
    await _runner.setup()

    port = int(os.getenv("PORT", 10000))
    _site = web.TCPSite(_runner, "0.0.0.0", port)
    await _site.start()
    logging.info(f"✅ Health-check сервер запущен на порту {port}")
    # ─────────────────────────────────────────────────────────────────────

    print("=" * 50)
    print("🚀 SELFTABS БОТ ЗАПУЩЕН")
    print("=" * 50)
    print(f"🌐 API_URL: {API_URL}")
    print(f"📋 Планы: {list(PLANS.keys())}")
    print(f"🏦 СБП (Platega): {'✅' if PLATEGA_BOT_SECRET else '❌ BOT_SECRET не задан'}")
    print(f"🪙 Крипта (Platega): {'✅' if PLATEGA_BOT_SECRET else '❌ BOT_SECRET не задан'}")
    print(f"📧 Email-авторизация: ✅")
    print("=" * 50)

    await bot.delete_webhook(drop_pending_updates=True)

    asyncio.create_task(expiry_scheduler())
    asyncio.create_task(digest_scheduler())

    # ── Graceful shutdown при SIGTERM (Render останавливает именно так) ───
    import signal
    loop       = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _handle_sigterm():
        logging.info("SIGTERM получен — начинаем graceful shutdown")
        stop_event.set()

    loop.add_signal_handler(signal.SIGTERM, _handle_sigterm)
    loop.add_signal_handler(signal.SIGINT,  _handle_sigterm)
    # ─────────────────────────────────────────────────────────────────────

    polling_task = asyncio.create_task(dp.start_polling(bot))
    await stop_event.wait()

    logging.info("Останавливаем polling...")
    polling_task.cancel()
    try:
        await polling_task
    except asyncio.CancelledError:
        pass

    if crypto:
        await crypto.close()

    await dp.storage.close()
    await bot.session.close()
    await _runner.cleanup()
    logging.info("✅ Бот остановлен чисто")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n❌ Бот остановлен")