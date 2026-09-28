"""
Telegram-бот на aiogram 3.x для расширения Selftabs.
- Регистрация / вход через FastAPI-бэкенд
- Привязка Telegram ID к аккаунту
- Оплата подписки через Telegram Stars (XTR) — recurring subscription
- Оплата подписки через USDT (CryptoPay)
- Оплата через СБП (ЮКасса)

Режим работы: WEBHOOK (для Render)
БД: Supabase / PostgreSQL (через asyncpg)

Установка зависимостей:
    pip install aiogram aiohttp aiocryptopay python-dotenv asyncpg

Переменные окружения (.env):
    BOT_TOKEN=...
    API_URL=...
    BOT_SECRET=SLFTBS
    CRYPTO_PAY_TOKEN=...
    ROBOKASSA_LOGIN=...
    ROBOKASSA_PASSWORD1=...
    TELEGRAM_PROXY=          # опционально: socks5://user:pass@host:port
    NOTIFY_TZ_OFFSET=3
    NOTIFY_HOUR=20
    DATABASE_URL=postgresql://user:pass@db.supabase.co:5432/postgres
    WEBHOOK_URL=https://your-app.onrender.com   # без слеша в конце
    WEBHOOK_PATH=/webhook
    PORT=8080
"""

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional
from dotenv import load_dotenv
import aiohttp
from aiohttp_socks import ProxyConnector
from aiohttp import web
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
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

import asyncpg
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

NOTIFY_TZ_OFFSET = int(os.getenv("NOTIFY_TZ_OFFSET", "3"))
NOTIFY_HOUR      = int(os.getenv("NOTIFY_HOUR", "20"))

DATABASE_URL  = os.getenv("DATABASE_URL")           # postgresql://...
WEBHOOK_URL   = os.getenv("WEBHOOK_URL", "").rstrip("/")  # https://your-app.onrender.com
WEBHOOK_PATH  = os.getenv("WEBHOOK_PATH", "/webhook")
PORT          = int(os.getenv("PORT", "8080"))

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

# ── Хранилище токенов (PostgreSQL / Supabase) ─────────────────────────────

class TokenStorage:
    """
    Хранит JWT-токены пользователей в PostgreSQL (Supabase).
    В памяти держит кэш для быстрых проверок — БД используется
    только при старте (загрузка) и при изменениях (запись/удаление).
    """

    def __init__(self):
        self._cache: dict[int, str] = {}
        self._pool: Optional[asyncpg.Pool] = None

    async def init(self, pool: asyncpg.Pool):
        self._pool = pool
        await pool.execute("""
            CREATE TABLE IF NOT EXISTS user_tokens (
                tg_id    BIGINT PRIMARY KEY,
                token    TEXT NOT NULL,
                saved_at DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW())
            )
        """)
        rows = await pool.fetch("SELECT tg_id, token FROM user_tokens")
        self._cache = {r["tg_id"]: r["token"] for r in rows}
        logger.info(f"TokenStorage: загружено {len(self._cache)} сессий из БД")

    def __contains__(self, tg_id: int) -> bool:
        return tg_id in self._cache

    def get(self, tg_id: int, default=None) -> Optional[str]:
        return self._cache.get(tg_id, default)

    async def set(self, tg_id: int, token: str):
        self._cache[tg_id] = token
        await self._pool.execute(
            """
            INSERT INTO user_tokens (tg_id, token, saved_at)
            VALUES ($1, $2, EXTRACT(EPOCH FROM NOW()))
            ON CONFLICT (tg_id) DO UPDATE SET token = $2, saved_at = EXTRACT(EPOCH FROM NOW())
            """,
            tg_id, token,
        )

    async def pop(self, tg_id: int):
        self._cache.pop(tg_id, None)
        await self._pool.execute("DELETE FROM user_tokens WHERE tg_id = $1", tg_id)

    def items(self):
        return list(self._cache.items())

    def all_tg_ids(self) -> list[int]:
        return list(self._cache.keys())


# ── Лог уведомлений (PostgreSQL / Supabase) ───────────────────────────────

class NotificationLog:
    def __init__(self):
        self._pool: Optional[asyncpg.Pool] = None

    async def init(self, pool: asyncpg.Pool):
        self._pool = pool
        await pool.execute("""
            CREATE TABLE IF NOT EXISTS notification_log (
                tg_id      BIGINT,
                notif_type TEXT,
                date_tag   TEXT,
                sent_at    DOUBLE PRECISION DEFAULT EXTRACT(EPOCH FROM NOW()),
                PRIMARY KEY (tg_id, notif_type, date_tag)
            )
        """)

    async def already_sent(self, tg_id: int, notif_type: str, date_tag: str) -> bool:
        row = await self._pool.fetchrow(
            "SELECT 1 FROM notification_log WHERE tg_id=$1 AND notif_type=$2 AND date_tag=$3",
            tg_id, notif_type, date_tag,
        )
        return row is not None

    async def mark_sent(self, tg_id: int, notif_type: str, date_tag: str):
        await self._pool.execute(
            """
            INSERT INTO notification_log (tg_id, notif_type, date_tag)
            VALUES ($1, $2, $3)
            ON CONFLICT DO NOTHING
            """,
            tg_id, notif_type, date_tag,
        )

    async def cleanup_old(self, days: int = 35):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
        await self._pool.execute(
            "DELETE FROM notification_log WHERE sent_at < $1", cutoff
        )


user_tokens = TokenStorage()
notif_log   = NotificationLog()

# ── FSM-состояния ──────────────────────────────────────────────────────────

ADMIN_ID = 5105131373


class AdminStates(StatesGroup):
    waiting_user_id_profile = State()
    waiting_user_id_sub     = State()
    waiting_sub_plan        = State()
    waiting_sub_days        = State()


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
        builder.button(text="🔑 Войти через расширение", callback_data="login_extension")
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
async def cmd_start(message: Message):
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
            await user_tokens.set(tg_id, token)
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
                "Выбери способ оплаты 👇",
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
                "Выбери способ оплаты 👇",
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
            await user_tokens.pop(tg_id)
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
            "🔑 Для входа открой расширение Selftabs и нажми «Открыть бот» 👇"
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
        "Войди в аккаунт для управления подпиской 👇"
    )
    await call.message.edit_text(text, reply_markup=get_main_keyboard(logged_in, tg_id))
    await call.answer()


@dp.callback_query(F.data == "login_extension")
async def cb_login_extension(call: CallbackQuery):
    await call.message.edit_text(
        "🔑 <b>Вход в Selftabs</b>\n\n"
        "Авторизация доступна только через браузерное расширение:\n\n"
        "1️⃣ Открой расширение <b>Selftabs</b> в браузере\n"
        "2️⃣ Войди в аккаунт в расширении\n"
        "3️⃣ Нажми кнопку <b>«Открыть бот»</b> — ты будешь авторизован автоматически\n\n"
        "<i>Telegram будет привязан к аккаунту автоматически.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_main")],
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
        await user_tokens.pop(tg_id)
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
    exp_str   = f"\n📅 До: <b>{expires[:10]}</b>" if expires and plan_key != "standard" else ""

    await call.message.edit_text(
        f"👤 <b>Мой профиль</b>\n\n"
        f"<b>{user.get('name') or 'Без имени'}</b>\n"
        f"📧 {user.get('email')}\n"
        f"🏷 @{user.get('username') or '—'}\n\n"
        f"{plan_emoji} Подписка: <b>{plan_name}</b>{exp_str}\n\n"
        f"🔗 Telegram: {tg_linked}\n"
        f"🔗 Google: {google}\n"
        f"🔒 2FA Telegram: {'✅' if settings.get('2fa_telegram') else '❌'}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Подписка", callback_data="subscription")],
            [InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_main")],
        ]),
    )
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
        await user_tokens.pop(tg_id)
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
async def cb_logout(call: CallbackQuery):
    tg_id = call.from_user.id
    token = user_tokens.get(tg_id)
    if token:
        try:
            await api_delete("/me/integrations/telegram", token)
        except Exception as e:
            logging.warning(f"Ошибка при отвязке Telegram: {e}")
    await user_tokens.pop(tg_id)
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
        await user_tokens.pop(tg_id)
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

    logging.info(
        f"[sessions] tg={tg_id} sess_status={sessions_status} "
        f"data_type={type(sessions_data).__name__} "
        f"len={len(sessions_data) if isinstance(sessions_data, list) else '?'} "
        f"stats_status={stats_status}"
    )

    if sessions_status == 401 or stats_status == 401:
        await user_tokens.pop(tg_id)
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
            tabs_count  = len(tabs_list)
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
        "Нажми на кнопку ниже 👇",
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
    """
    Возвращает звёзды пользователю.
    Используется только во время тестирования!
    Убери вызов перед выходом в прод.
    """
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
# ОПЛАТА — СБП (Robokassa)
# ══════════════════════════════════════════════════════════════════════════

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

    if not ROBOKASSA_LOGIN or not ROBOKASSA_PASSWORD1:
        log_event("SBP_UNAVAILABLE", tg_id, uname, plan=plan_key)
        await call.answer()
        await call.message.edit_text(
            "🏦 <b>Оплата через СБП временно недоступна</b>\n\n"
            "Напиши нам — активируем вручную:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📩 Поддержка", url="https://t.me/selftabs_support")],
                [InlineKeyboardButton(text="🔙 Назад",     callback_data=f"sub_info:{plan_key}")],
            ]),
        )
        return

    log_event("SBP_INVOICE_INIT", tg_id, uname, plan=plan_key, rub=plan["sbp_rub"])
    await call.answer("⏳ Создаём платёж...")

    status, resp = await api_post(
        "/subscription/robokassa/create",
        {"plan": plan_key, "tg_id": tg_id, "bot_secret": BOT_SECRET},
    )

    if status != 200:
        log_error("SBP_INVOICE_FAIL", tg_id, uname, plan=plan_key,
                  http_status=status, detail=resp.get("detail", resp))
        await call.message.edit_text(
            "❌ Не удалось создать платёж. Попробуй позже или напиши в поддержку.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📩 Поддержка", url="https://t.me/selftabs_support")],
                [InlineKeyboardButton(text="🔙 Назад",     callback_data=f"sub_info:{plan_key}")],
            ]),
        )
        return

    pay_url    = resp["payment_url"]
    amount_rub = resp["amount_rub"]
    inv_id     = resp["inv_id"]
    log_event("SBP_INVOICE_CREATED", tg_id, uname, plan=plan_key,
              inv_id=inv_id, amount_rub=amount_rub)

    await call.message.edit_text(
        f"🏦 <b>Оплата через СБП</b>\n\n"
        f"{plan['emoji']} Тариф: <b>{plan['title']}</b>\n"
        f"💰 Сумма: <b>{amount_rub} ₽ / месяц</b>\n"
        f"📅 Срок: 30 дней\n\n"
        "1️⃣ Нажми «Оплатить» — откроется страница Robokassa\n"
        "2️⃣ Выбери СБП или карту и подтверди платёж\n"
        "3️⃣ Вернись и нажми «Я оплатил — проверить»\n\n"
        f"<i>🔐 ID платежа: <code>{inv_id}</code></i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"🏦 Оплатить {amount_rub} ₽", url=pay_url)],
            [InlineKeyboardButton(text="✅ Я оплатил — проверить", callback_data=f"check_sbp:{plan_key}")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data=f"sub_info:{plan_key}")],
        ]),
    )


@dp.callback_query(F.data.startswith("check_sbp:"))
async def check_sbp_payment(call: CallbackQuery):
    plan_key = call.data.split(":", 1)[1]
    tg_id    = call.from_user.id
    uname    = call.from_user.username
    token    = user_tokens.get(tg_id)

    log_event("SBP_CHECK", tg_id, uname, plan=plan_key)
    await call.answer("🔄 Проверяем оплату...")

    status, resp = await api_get("/api/v1/subscription", token=token)

    if status == 200:
        current_plan = resp.get("plan", "standard")
        if current_plan == plan_key:
            expires = (resp.get("subscription_expires_at") or "")[:10]
            plan    = PLANS.get(plan_key, {})
            log_event("SBP_SUB_ACTIVATED", tg_id, uname, plan=plan_key, expires=expires)
            await call.message.edit_text(
                f"🎉 <b>Подписка активирована!</b>\n\n"
                f"{plan.get('emoji', '')} Тариф: <b>{plan.get('title', plan_key)}</b>\n"
                f"🏦 Способ: СБП (Robokassa)\n"
                f"💰 Оплачено: {plan.get('sbp_rub')} ₽\n"
                f"📅 Действует до: <b>{expires}</b>\n\n"
                "Вернись в расширение — статус уже обновлён 🚀",
                reply_markup=get_main_keyboard(True),
            )
            return

    log_event("SBP_NOT_CONFIRMED", tg_id, uname, plan=plan_key, api_status=status)
    await call.message.edit_text(
        "⏳ <b>Оплата ещё не подтверждена.</b>\n\n"
        "Robokassa обычно подтверждает платёж за 10–30 секунд.\n"
        "Подожди немного и попробуй снова:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Проверить снова",  callback_data=f"check_sbp:{plan_key}")],
            [InlineKeyboardButton(text="📩 Поддержка",        url="https://t.me/selftabs_support")],
            [InlineKeyboardButton(text="🔙 Назад",            callback_data="subscription")],
        ]),
    )


# ══════════════════════════════════════════════════════════════════════════
# ОПЛАТА — USDT (CryptoPay)
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

    try:
        invoice = await crypto.create_invoice(
            asset="USDT",
            amount=str(plan["usdt"]),
            description=f"Selftabs {plan['title']} — 30 дней",
            payload=f"{tg_id}_{plan_key}",
        )
        log_event("CRYPTO_INVOICE_CREATED", tg_id, uname, plan=plan_key,
                  invoice_id=invoice.invoice_id, usdt=plan["usdt"])

        await call.message.edit_text(
            f"🪙 <b>Крипто-счёт создан!</b>\n\n"
            f"{plan['emoji']} Тариф: <b>{plan['title']}</b>\n"
            f"💰 Сумма: <b>{plan['usdt']} USDT</b> (~{plan['usdt_rub']} ₽)\n"
            f"📅 Срок: 30 дней\n\n"
            f"🔗 <b>Ссылка для оплаты:</b>\n{invoice.bot_invoice_url}\n\n"
            "После оплаты нажми «✅ Проверить оплату» 👇",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="✅ Проверить оплату",
                    callback_data=f"check_crypto:{invoice.invoice_id}:{plan_key}",
                )],
                [InlineKeyboardButton(text="🔄 Новая ссылка", callback_data=f"buy_crypto:{plan_key}")],
                [InlineKeyboardButton(text="🔙 Назад",        callback_data=f"sub_info:{plan_key}")],
            ]),
        )

    except Exception as e:
        log_error("CRYPTO_INVOICE_FAIL", tg_id, uname, plan=plan_key, error=e)
        await call.message.edit_text(
            f"❌ Ошибка создания счёта: {str(e)[:200]}",
            reply_markup=get_back_main_keyboard(),
        )


@dp.callback_query(F.data.startswith("check_crypto:"))
async def check_crypto_payment(call: CallbackQuery):
    parts      = call.data.split(":")
    invoice_id = int(parts[1])
    plan_key   = parts[2]
    tg_id      = call.from_user.id
    uname      = call.from_user.username

    log_event("CRYPTO_CHECK", tg_id, uname, plan=plan_key, invoice_id=invoice_id)
    await call.answer("🔄 Проверка оплаты...")

    try:
        invoices = await crypto.get_invoices(invoice_ids=str(invoice_id))

        if invoices and len(invoices) > 0:
            invoice = invoices[0]
            status  = invoice.status

            if status == "paid":
                plan      = PLANS.get(plan_key, {})
                plan_name = plan.get("title", plan_key.capitalize())

                token = user_tokens.get(tg_id)
                if token:
                    act_status, act_resp = await api_post(
                        "/subscription/crypto/activate",
                        {
                            "telegram_id": tg_id,
                            "plan":        plan_key,
                            "invoice_id":  str(invoice_id),
                            "asset":       "USDT",
                            "amount":      plan.get("usdt"),
                            "bot_secret":  BOT_SECRET,
                        },
                        token=token,
                    )
                    if act_status == 200:
                        log_event("CRYPTO_SUB_ACTIVATED", tg_id, uname, plan=plan_key,
                                  invoice_id=invoice_id, usdt=plan.get("usdt"))
                    else:
                        log_error("CRYPTO_ACTIVATE_FAIL", tg_id, uname, plan=plan_key,
                                  invoice_id=invoice_id, http_status=act_status,
                                  detail=act_resp.get("detail", "—"))

                await call.message.edit_text(
                    f"🎉 <b>Оплата получена!</b>\n\n"
                    f"{plan.get('emoji', '')} Тариф: <b>{plan_name}</b>\n"
                    f"🪙 Оплачено: <b>{plan.get('usdt')} USDT</b> (~{plan.get('usdt_rub')} ₽)\n"
                    f"📅 Подписка активирована на 30 дней\n\n"
                    "Вернись в расширение — статус уже обновлён 🚀",
                    reply_markup=get_main_keyboard(True),
                )

            elif status == "expired":
                log_event("CRYPTO_INVOICE_EXPIRED", tg_id, uname, plan=plan_key, invoice_id=invoice_id)
                await call.message.edit_text(
                    "⏰ <b>Срок оплаты истёк.</b>\n\n"
                    "Нажми «Новая ссылка» для создания нового счёта.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🔄 Новая ссылка", callback_data=f"buy_crypto:{plan_key}")],
                        [InlineKeyboardButton(text="🔙 Назад",        callback_data="subscription")],
                    ]),
                )
            else:
                log_event("CRYPTO_PENDING", tg_id, uname, plan=plan_key,
                          invoice_id=invoice_id, invoice_status=status)
                await call.message.edit_text(
                    f"⏳ <b>Ожидание оплаты...</b>\n\n"
                    f"Статус: {status}\n\n"
                    "После оплаты нажми «Проверить снова» 👇",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(
                            text="✅ Проверить снова",
                            callback_data=f"check_crypto:{invoice_id}:{plan_key}",
                        )],
                        [InlineKeyboardButton(text="🔄 Новая ссылка", callback_data=f"buy_crypto:{plan_key}")],
                        [InlineKeyboardButton(text="🔙 Назад",        callback_data="subscription")],
                    ]),
                )
        else:
            log_error("CRYPTO_INVOICE_NOT_FOUND", tg_id, uname, plan=plan_key, invoice_id=invoice_id)
            await call.message.edit_text(
                "❌ Счёт не найден. Создайте новый:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Новая ссылка", callback_data=f"buy_crypto:{plan_key}")],
                    [InlineKeyboardButton(text="🔙 Назад",        callback_data="subscription")],
                ]),
            )

    except Exception as e:
        log_error("CRYPTO_CHECK_EXCEPTION", tg_id, uname, plan=plan_key,
                  invoice_id=invoice_id, error=e)
        await call.message.edit_text(
            f"❌ Ошибка проверки: {str(e)[:200]}",
            reply_markup=get_back_main_keyboard(),
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
        await message.answer("⚠️ Введи корректный числовой Telegram ID.",
                             reply_markup=get_admin_cancel_keyboard())
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
        await message.answer(
            f"⚠️ Сессия пользователя <code>{target_id}</code> истекла.",
            reply_markup=get_admin_keyboard(),
        )
        return

    if status != 200:
        await message.answer(
            f"❌ Не удалось загрузить профиль (HTTP {status}).",
            reply_markup=get_admin_keyboard(),
        )
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
        await message.answer("⚠️ Введи корректный числовой Telegram ID.",
                             reply_markup=get_admin_cancel_keyboard())
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
        await message.answer("⚠️ Введи целое положительное число дней.",
                             reply_markup=get_admin_cancel_keyboard())
        return
    data      = await state.get_data()
    target_id = data.get("target_tg_id")
    plan_key  = data.get("plan_key", "pro")
    days      = int(text)
    await state.clear()
    await _admin_apply_sub_message(message, target_id, plan_key, days)


async def _admin_apply_subscription(call: CallbackQuery, state: FSMContext,
                                    target_id: int, plan_key: str, days: int):
    await state.clear()
    await call.answer("⏳ Применяю...")
    success, result_text = await _do_grant_subscription(target_id, plan_key, days)
    await call.message.edit_text(
        result_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Админ-панель", callback_data="admin_panel")],
        ]),
    )


async def _admin_apply_sub_message(message: Message, target_id: int,
                                   plan_key: str, days: int):
    success, result_text = await _do_grant_subscription(target_id, plan_key, days)
    await message.answer(
        result_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Админ-панель", callback_data="admin_panel")],
        ]),
    )


async def _do_grant_subscription(target_tg_id: int, plan_key: str,
                                 days: int) -> tuple[bool, str]:
    plan_emoji, plan_name = PLAN_NAMES.get(plan_key, ("🆓", plan_key))
    try:
        headers = {"Content-Type": "application/json", "X-Bot-Secret": BOT_SECRET}
        payload = {"telegram_id": target_tg_id, "plan": plan_key, "days": days}
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{API_URL}/admin/subscription",
                                    json=payload, headers=headers) as r:
                try:
                    resp = await r.json()
                except Exception:
                    resp = {}
                http_status = r.status

        logger.info(f"[ADMIN_GRANT] target={target_tg_id} plan={plan_key} days={days} "
                    f"http={http_status} resp={resp}")

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
                notify_text = (
                    "ℹ️ <b>Ваша подписка была изменена администратором.</b>\n\n"
                    "Текущий тариф: 🆓 <b>Self Free</b>"
                    if plan_key == "standard" else
                    f"🎉 <b>Вам выдана подписка!</b>\n\n"
                    f"{plan_emoji} Тариф: <b>{plan_name}</b>\n"
                    f"📅 Активна: <b>{days} дней</b>\n\n"
                    "Приятного использования Selftabs! 🚀"
                )
                await bot.send_message(target_tg_id, notify_text)
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
        await bot.send_message(
            chat_id=tg_id, text=text,
            parse_mode="HTML", reply_markup=reply_markup,
        )
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
                await user_tokens.pop(tg_id)
            continue

        plan_key    = user.get("subscription_plan", "standard")
        expires_raw = user.get("subscription_expires_at")

        if plan_key == "standard" or not expires_raw:
            continue

        try:
            expires_str = expires_raw.replace("Z", "+00:00")
            expires_at  = datetime.fromisoformat(expires_str)
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
            if not await notif_log.already_sent(tg_id, key, date_tag):
                await _safe_send(
                    tg_id,
                    f"🔔 <b>Подписка истекает через 7 дней</b>\n\n"
                    f"{plan_emoji} Тариф: <b>{plan_name}</b>\n"
                    f"📅 Истекает: <b>{exp_fmt}</b>\n\n"
                    "Продли заранее — доступ не прервётся 👇",
                    renew_kb,
                )
                await notif_log.mark_sent(tg_id, key, date_tag)

        elif 86400 >= total_secs > 82800:
            key = "sub_1d"
            if not await notif_log.already_sent(tg_id, key, date_tag):
                await _safe_send(
                    tg_id,
                    f"⚠️ <b>Подписка истекает завтра!</b>\n\n"
                    f"{plan_emoji} Тариф: <b>{plan_name}</b>\n"
                    f"📅 Истекает: <b>{exp_fmt}</b>\n\n"
                    "Продли сейчас, чтобы не потерять доступ 👇",
                    renew_kb,
                )
                await notif_log.mark_sent(tg_id, key, date_tag)

        elif 7200 >= total_secs > 6300:
            key = "sub_2h"
            if not await notif_log.already_sent(tg_id, key, date_tag):
                await _safe_send(
                    tg_id,
                    f"🚨 <b>Подписка истекает менее чем через 2 часа!</b>\n\n"
                    f"{plan_emoji} Тариф: <b>{plan_name}</b>\n"
                    f"📅 Истекает: <b>{exp_fmt}</b>\n\n"
                    "Продли прямо сейчас — ещё не поздно 👇",
                    renew_kb,
                )
                await notif_log.mark_sent(tg_id, key, date_tag)


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
        if await notif_log.already_sent(tg_id, "daily", today_tag):
            continue
        plan_emoji, plan_name = PLAN_NAMES[plan_key]
        await _safe_send(
            tg_id,
            f"📰 <b>Твой ежедневный дайджест готов!</b>\n\n"
            f"{plan_emoji} Тариф: <b>{plan_name}</b>\n\n"
            "Selftabs собрал для тебя ключевые обновления по твоим темам 🧠\n\n"
            "Открой расширение, чтобы прочитать AI-сводку за сегодня.",
            InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="📖 Открыть дайджест", url="https://selftabs.com/digest")
            ]]),
        )
        await notif_log.mark_sent(tg_id, "daily", today_tag)
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
        if await notif_log.already_sent(tg_id, "weekly", today_tag):
            continue
        plan_emoji, plan_name = PLAN_NAMES[plan_key]
        await _safe_send(
            tg_id,
            f"📊 <b>Еженедельный AI-дайджест готов!</b>\n\n"
            f"{plan_emoji} Тариф: <b>{plan_name}</b>\n\n"
            "Selftabs подготовил сводку за прошедшую неделю:\n"
            "• 🔥 Главные темы и тренды\n"
            "• 📌 Самые важные вкладки\n"
            "• 🧠 AI-выводы по твоим интересам\n\n"
            "Открой расширение, чтобы прочитать полный отчёт 👇",
            InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="📖 Открыть weekly-дайджест",
                                     url="https://selftabs.com/digest/weekly")
            ]]),
        )
        await notif_log.mark_sent(tg_id, "weekly", today_tag)
        await asyncio.sleep(0.05)


async def digest_scheduler():
    logging.info(f"[scheduler] digest_scheduler запущен (UTC+{NOTIFY_TZ_OFFSET}, {NOTIFY_HOUR}:00)")
    while True:
        now_local = _local_now()
        target    = now_local.replace(hour=NOTIFY_HOUR, minute=0, second=0, microsecond=0)
        if now_local >= target:
            target += timedelta(days=1)
        sleep_secs = (target - now_local).total_seconds()
        logging.info(f"[scheduler] следующий дайджест через {sleep_secs/3600:.1f} ч "
                     f"({target.strftime('%d.%m %H:%M')})")
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
            await notif_log.cleanup_old()
        except Exception:
            pass

        await asyncio.sleep(60)


# ══════════════════════════════════════════════════════════════════════════
# HEALTHCHECK — чтобы Render видел живой HTTP-сервис
# ══════════════════════════════════════════════════════════════════════════

async def health_handler(request: web.Request) -> web.Response:
    return web.Response(text="ok")


# ══════════════════════════════════════════════════════════════════════════
# ЗАПУСК (webhook + aiohttp)
# ══════════════════════════════════════════════════════════════════════════

async def on_startup(app: web.Application):
    global crypto

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ── PostgreSQL (Supabase) ──────────────────────────────────────────────
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL не задан — укажи переменную окружения")

    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    app["db_pool"] = pool

    await user_tokens.init(pool)
    await notif_log.init(pool)

    # ── CryptoPay ─────────────────────────────────────────────────────────
    if CRYPTO_PAY_TOKEN:
        crypto = AioCryptoPay(token=CRYPTO_PAY_TOKEN, network=CRYPTO_NETWORK)
        try:
            me = await crypto.get_me()
            logging.info(f"✅ CryptoPay подключён: {me.name}")
        except Exception as e:
            logging.warning(f"⚠️ CryptoPay ошибка: {e}")
    else:
        logging.warning("⚠️ CRYPTO_PAY_TOKEN не задан — крипто-оплата недоступна")

    # ── Webhook ───────────────────────────────────────────────────────────
    webhook_full_url = f"{WEBHOOK_URL}{WEBHOOK_PATH}"
    await bot.set_webhook(
        url=webhook_full_url,
        drop_pending_updates=True,
        allowed_updates=dp.resolve_used_update_types(),
    )
    logging.info(f"✅ Webhook установлен: {webhook_full_url}")

    # ── Фоновые задачи ────────────────────────────────────────────────────
    asyncio.create_task(expiry_scheduler())
    asyncio.create_task(digest_scheduler())

    print("=" * 50)
    print("🚀 SELFTABS БОТ ЗАПУЩЕН (webhook)")
    print("=" * 50)
    print(f"🌐 API_URL:     {API_URL}")
    print(f"🔗 WEBHOOK_URL: {webhook_full_url}")
    print(f"🏦 СБП:         {'✅' if ROBOKASSA_LOGIN else '❌ не настроен'}")
    print(f"🪙 CryptoPay:   {'✅' if CRYPTO_PAY_TOKEN else '❌ не настроен'}")
    print("=" * 50)


async def on_shutdown(app: web.Application):
    await bot.delete_webhook()
    if crypto:
        await crypto.close()
    pool = app.get("db_pool")
    if pool:
        await pool.close()
    logging.info("Бот остановлен.")


def main():
    app = web.Application()

    # Регистрируем webhook-роут для Telegram
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)

    # Healthcheck для Render (и UptimeRobot при желании)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/", health_handler)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    setup_application(app, dp, bot=bot)

    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()