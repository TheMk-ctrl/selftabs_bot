"""
bot_example.py — демо-бот Selftabs для прохождения модерации банка.

Отличия от основного бота:
  • Авторизация убрана полностью — бот доступен сразу после /start
  • При оформлении любого тарифа запрашивается email покупателя
  • После успешной оплаты (Stars / СБП / Крипта) на email отправляется:
      — чек с суммой и ID платежа
      — название купленной подписки и срок действия
  • СБП и Крипта — через Platega.io (реальные платежи)
  • Активация подписки на бэке — через /api/v1/payments/platega/callback (автоматически)
  • Возврат Stars после оплаты — включён (тест)

Запуск:
    pip install aiogram aiohttp python-dotenv httpx
    python bot_example.py

Нужные переменные окружения (.env):
    BOT_TOKEN
    RESEND_API_KEY          — ключ Resend для отправки писем
    EMAIL_FROM              — адрес отправителя (напр. SelfTabs <noreply@selftabs.app>)
    BACKEND_URL             — базовый URL бэкенда (напр. https://api.selftabs.app)
    BACKEND_BOT_SECRET      — BOT_SECRET из .env бэкенда (для авторизации запросов)
    TELEGRAM_PROXY          — опционально (socks5://...)
"""

import asyncio
import logging
import os
import re as _re
from typing import Optional

import httpx
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums.parse_mode import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()

# ── Конфиг ────────────────────────────────────────────────────────────────

BOT_TOKEN           = os.getenv("BOT_TOKEN", "")
RESEND_API_KEY      = os.getenv("RESEND_API_KEY", "")
EMAIL_FROM          = os.getenv("EMAIL_FROM", "SelfTabs <noreply@selftabs.app>")
BACKEND_URL         = os.getenv("BACKEND_URL", "").rstrip("/")   # https://api.selftabs.app
BACKEND_BOT_SECRET  = os.getenv("BACKEND_BOT_SECRET", "")        # BOT_SECRET из .env бэкенда
TELEGRAM_PROXY      = os.getenv("TELEGRAM_PROXY", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("selftabs_demo")

# ── Тарифы ────────────────────────────────────────────────────────────────

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
        "stars":     245,
        "usdt":      2.99,
        "usdt_rub":  270,
        "sbp_rub":   270,
        "price_rub": "270 ₽",
        "emoji":     "🚀",
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
        "stars":     770,
        "usdt":      9.99,
        "usdt_rub":  900,
        "sbp_rub":   900,
        "price_rub": "900 ₽",
        "emoji":     "🏢",
    },
}

# ── FSM ───────────────────────────────────────────────────────────────────

class CheckoutStates(StatesGroup):
    waiting_email = State()   # ждём email перед оплатой


# ── Бот ───────────────────────────────────────────────────────────────────

_session = AiohttpSession(proxy=TELEGRAM_PROXY) if TELEGRAM_PROXY else None
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    session=_session,
)
dp  = Dispatcher(storage=MemoryStorage())

# ── Клавиатуры ────────────────────────────────────────────────────────────

def kb_main() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🚀 Pro Pass — 270 ₽/мес",       callback_data="plan:pro")
    builder.button(text="🏢 Team Workspace — 900 ₽/мес", callback_data="plan:team")
    builder.button(text="📋 Сравнить тарифы",             callback_data="plans_info")
    builder.button(text="📩 Поддержка",                   url="https://t.me/selftabs_support")
    builder.adjust(1)
    return builder.as_markup()


def kb_payment(plan_key: str) -> InlineKeyboardMarkup:
    plan = PLANS[plan_key]
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"💫 Telegram Stars — {plan['stars']} ⭐/мес",
        callback_data=f"pay_stars:{plan_key}",
    )
    builder.button(
        text=f"🏦 СБП — {plan['sbp_rub']} ₽/мес",
        callback_data=f"pay_sbp:{plan_key}",
    )
    builder.button(
        text=f"🪙 Крипта — {plan['usdt']}$ (~{plan['usdt_rub']} ₽)/мес",
        callback_data=f"pay_crypto:{plan_key}",
    )
    builder.button(text="📩 Поддержка", url="https://t.me/selftabs_support")
    builder.button(text="🔙 Назад",     callback_data="back_main")
    builder.adjust(1)
    return builder.as_markup()


def kb_back_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_main")],
        [InlineKeyboardButton(text="📩 Поддержка",    url="https://t.me/selftabs_support")],
    ])


LEGAL_LINKS = (
    '<a href="https://selftabs.ru/privacy">Политика конфиденциальности</a>'
    " · "
    '<a href="https://selftabs.ru/terms">Пользовательское соглашение</a>'
)


def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отмена", callback_data="back_main")
    ]])


# ── Email-отправка через Resend ───────────────────────────────────────────

async def send_receipt_email(
    to_email: str,
    plan_key: str,
    payment_method: str,
    amount_str: str,
    charge_id: str,
):
    """Отправляет чек и данные подписки на email покупателя."""
    plan      = PLANS[plan_key]
    plan_name = plan["title"]
    plan_desc = plan["description"].replace("\n", "<br>")

    html = f"""
    <div style="font-family:sans-serif;max-width:520px;margin:0 auto;padding:32px 24px;color:#0A0A0A;">
        <h2 style="font-size:22px;font-weight:700;margin-bottom:4px;">
            🎉 Спасибо за покупку!
        </h2>
        <p style="color:#555;font-size:14px;margin-bottom:28px;">
            Ваша подписка <b>Selftabs</b> активирована.
        </p>

        <div style="background:#F4F4F8;border-radius:12px;padding:20px 24px;margin-bottom:24px;">
            <p style="margin:0 0 8px;font-size:16px;font-weight:700;">{plan['emoji']} {plan_name}</p>
            <p style="margin:0;font-size:13px;color:#555;line-height:1.7;">{plan_desc}</p>
        </div>

        <table style="width:100%;font-size:14px;border-collapse:collapse;margin-bottom:24px;">
            <tr>
                <td style="padding:8px 0;color:#555;border-bottom:1px solid #eee;">Способ оплаты</td>
                <td style="padding:8px 0;text-align:right;font-weight:600;border-bottom:1px solid #eee;">{payment_method}</td>
            </tr>
            <tr>
                <td style="padding:8px 0;color:#555;border-bottom:1px solid #eee;">Сумма</td>
                <td style="padding:8px 0;text-align:right;font-weight:600;border-bottom:1px solid #eee;">{amount_str}</td>
            </tr>
            <tr>
                <td style="padding:8px 0;color:#555;border-bottom:1px solid #eee;">Срок подписки</td>
                <td style="padding:8px 0;text-align:right;font-weight:600;border-bottom:1px solid #eee;">30 дней</td>
            </tr>
            <tr>
                <td style="padding:8px 0;color:#999;font-size:12px;">ID транзакции</td>
                <td style="padding:8px 0;text-align:right;color:#999;font-size:12px;font-family:monospace;">{charge_id}</td>
            </tr>
        </table>

        <p style="font-size:13px;color:#555;line-height:1.6;">
            Установите расширение <b>Selftabs</b> и войдите по этому email —
            подписка будет активирована автоматически.
        </p>

        <p style="font-size:12px;color:#aaa;margin-top:28px;">
            Если у вас возникли вопросы, напишите нам:
            <a href="https://t.me/selftabs_support" style="color:#6c47ff;">@selftabs_support</a>
        </p>
    </div>
    """

    payload = {
        "from":    EMAIL_FROM,
        "to":      [to_email],
        "subject": f"Чек — {plan_name} · Selftabs",
        "html":    html,
    }

    if not RESEND_API_KEY:
        logger.warning("[EMAIL] RESEND_API_KEY не задан. to=%s plan=%s charge=%s",
                       to_email, plan_key, charge_id)
        return

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                json=payload,
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type":  "application/json",
                },
            )
            resp.raise_for_status()
            logger.info("[EMAIL] Чек отправлен: to=%s id=%s", to_email, resp.json().get("id"))
    except httpx.HTTPStatusError as exc:
        logger.error("[EMAIL] HTTP-ошибка %s: %s", exc.response.status_code, exc.response.text)
    except Exception as exc:
        logger.error("[EMAIL] Ошибка отправки: %s", exc)


# ── Platega helpers ──────────────────────────────────────────────────────

async def _platega_create(plan_key: str, method: str) -> dict:
    """
    Создаёт транзакцию через наш бэк (/api/v1/payments/platega/create).
    Возвращает { transaction_id, redirect_url, amount, currency, status }.
    Бросает исключение при ошибке.

    Примечание: бот не авторизован как конкретный пользователь — он дёргает
    специальный внутренний эндпоинт с bot_secret вместо session_token.
    Бэк должен либо принимать bot_secret, либо иметь отдельный эндпоинт.
    Пока используем прямой запрос к Platega из бота (без бэка) — так проще
    для теста. После прохождения модерации переключить на бэк-эндпоинт.
    """
    if not BACKEND_URL:
        raise RuntimeError("BACKEND_URL не задан в .env")

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{BACKEND_URL}/api/v1/payments/platega/create-internal",
            json={"plan": plan_key, "method": method},
            headers={
                "X-Bot-Secret": BACKEND_BOT_SECRET,
                "Content-Type": "application/json",
            },
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Бэк вернул {resp.status_code}: {resp.text[:200]}")
        return resp.json()


async def _platega_check(transaction_id: str) -> str:
    """
    Проверяет статус транзакции через бэк.
    Возвращает строку: "PENDING" | "CONFIRMED" | "CANCELED".
    """
    if not BACKEND_URL:
        return "PENDING"

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{BACKEND_URL}/api/v1/payments/platega/internal/{transaction_id}",
            headers={"X-Bot-Secret": BACKEND_BOT_SECRET},
        )
        if resp.status_code != 200:
            logger.warning("[PLATEGA] check status %s: %s", resp.status_code, resp.text[:100])
            return "PENDING"
        return resp.json().get("status", "PENDING")


# ── /start ────────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🌟 <b>Selftabs — умное расширение для браузера</b>\n\n"
        "Сохраняй вкладки, управляй сессиями и получай AI-дайджесты.\n\n"
        "📋 <b>Тарифы:</b>\n"
        "🚀 <b>Pro Pass</b> — 270 ₽/мес\n"
        "🏢 <b>Team Workspace</b> — 900 ₽/мес\n\n"
        "💳 <b>Способы оплаты:</b>\n"
        "💫 Telegram Stars · 🏦 СБП · 🪙 Крипта\n\n"
        f"{LEGAL_LINKS}\n\n"
        "Выбери тариф 👇",
        reply_markup=kb_main(),
    )


@dp.callback_query(F.data == "back_main")
async def cb_back_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text(
        "🌟 <b>Selftabs</b>\n\nВыбери тариф 👇",
        reply_markup=kb_main(),
    )
    await call.answer()


@dp.callback_query(F.data == "plans_info")
async def cb_plans_info(call: CallbackQuery):
    pro  = PLANS["pro"]
    team = PLANS["team"]
    await call.message.edit_text(
        f"📋 <b>Тарифы Selftabs</b>\n\n"
        f"🚀 <b>Pro Pass</b> — {pro['price_rub']}/мес\n"
        f"{pro['description']}\n\n"
        f"🏢 <b>Team Workspace</b> — {team['price_rub']}/мес\n"
        f"{team['description']}\n\n"
        "<i>Подписка продлевается каждые 30 дней.\n"
        f"Отменить можно в любое время.</i>\n\n"
        f"{LEGAL_LINKS}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Купить Pro Pass",       callback_data="plan:pro")],
            [InlineKeyboardButton(text="🏢 Купить Team Workspace", callback_data="plan:team")],
            [InlineKeyboardButton(text="📩 Поддержка",             url="https://t.me/selftabs_support")],
            [InlineKeyboardButton(text="🔙 Назад",                 callback_data="back_main")],
        ]),
    )
    await call.answer()


# ── Выбор тарифа → запрос email ──────────────────────────────────────────

@dp.callback_query(F.data.startswith("plan:"))
async def cb_plan(call: CallbackQuery, state: FSMContext):
    plan_key = call.data.split(":", 1)[1]
    plan     = PLANS.get(plan_key)
    if not plan:
        await call.answer("Неизвестный тариф.", show_alert=True)
        return

    await state.update_data(plan_key=plan_key)
    await call.message.edit_text(
        f"{plan['emoji']} <b>{plan['title']}</b> — {plan['price_rub']}/мес\n\n"
        f"{plan['description']}\n\n"
        "💳 <b>Выбери способ оплаты:</b>\n"
        f"💫 Telegram Stars — {plan['stars']} ⭐/мес\n"
        f"🏦 СБП — {plan['sbp_rub']} ₽/мес\n"
        f"🪙 Крипта — {plan['usdt']}$ (~{plan['usdt_rub']} ₽)/мес\n\n"
        f"{LEGAL_LINKS}\n\n"
        "Нажми на кнопку ниже 👇",
        reply_markup=kb_payment(plan_key),
    )
    await call.answer()


# ── Запрос email перед оплатой ────────────────────────────────────────────

async def _ask_email(call: CallbackQuery, state: FSMContext, pay_method: str, plan_key: str):
    """Общий шаг: сохраняем метод оплаты и просим email."""
    await state.update_data(pay_method=pay_method, plan_key=plan_key)
    await state.set_state(CheckoutStates.waiting_email)
    plan = PLANS[plan_key]
    await call.message.edit_text(
        f"{plan['emoji']} <b>{plan['title']}</b> · {pay_method}\n\n"
        "📧 Введи свой <b>email</b> — туда отправим чек и данные подписки:",
        reply_markup=kb_cancel(),
    )
    await call.answer()


@dp.callback_query(F.data.startswith("pay_stars:"))
async def cb_pay_stars(call: CallbackQuery, state: FSMContext):
    plan_key = call.data.split(":", 1)[1]
    await _ask_email(call, state, "stars", plan_key)


@dp.callback_query(F.data.startswith("pay_sbp:"))
async def cb_pay_sbp(call: CallbackQuery, state: FSMContext):
    plan_key = call.data.split(":", 1)[1]
    await _ask_email(call, state, "sbp", plan_key)


@dp.callback_query(F.data.startswith("pay_crypto:"))
async def cb_pay_crypto(call: CallbackQuery, state: FSMContext):
    plan_key = call.data.split(":", 1)[1]
    await _ask_email(call, state, "crypto", plan_key)


# ── Получаем email → переходим к конкретной оплате ───────────────────────

_EMAIL_RE = _re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dp.message(CheckoutStates.waiting_email)
async def handle_email_input(message: Message, state: FSMContext):
    email = (message.text or "").strip().lower()
    if not _EMAIL_RE.match(email):
        await message.answer(
            "⚠️ Некорректный email. Попробуй ещё раз:",
            reply_markup=kb_cancel(),
        )
        return

    data       = await state.get_data()
    plan_key   = data.get("plan_key", "pro")
    pay_method = data.get("pay_method", "stars")

    await state.update_data(email=email)
    await state.clear()   # FSM выполнил своё — дальше callback-хендлеры

    plan  = PLANS[plan_key]
    tg_id = message.from_user.id

    # ── Stars ──────────────────────────────────────────────────────────────
    if pay_method == "stars":
        try:
            invoice_link = await bot.create_invoice_link(
                title=plan["title"],
                description=plan["description"],
                payload=f"demo:{plan_key}:{tg_id}:{email}",
                provider_token="",
                currency="XTR",
                prices=[LabeledPrice(label=plan["title"], amount=plan["stars"])],
                subscription_period=2592000,
            )
            await message.answer(
                f"💫 <b>Оплата через Telegram Stars</b>\n\n"
                f"{plan['emoji']} Тариф: <b>{plan['title']}</b>\n"
                f"💰 Стоимость: <b>{plan['stars']} ⭐/мес</b> (~{plan['price_rub']})\n"
                f"📅 Срок: 30 дней\n"
                f"📧 Чек: <code>{email}</code>\n\n"
                "Нажми кнопку ниже — оплата в один клик 👇",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💫 Оплатить Stars", url=invoice_link)],
                    [InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_main")],
                ]),
            )
        except Exception as e:
            logger.error("[STARS] Ошибка создания счёта: %s", e)
            await message.answer(
                f"❌ Ошибка создания счёта: {str(e)[:200]}",
                reply_markup=kb_back_main(),
            )

    # ── СБП (Platega) ────────────────────────────────────────────────────
    elif pay_method == "sbp":
        try:
            result = await _platega_create(plan_key, "sbp")
        except Exception as e:
            logger.error("[SBP] Ошибка создания транзакции: %s", e)
            await message.answer(
                f"❌ Ошибка создания платежа: {str(e)[:200]}",
                reply_markup=kb_back_main(),
            )
            return

        txn_id   = result["transaction_id"]
        pay_url  = result["redirect_url"]

        await message.answer(
            f"🏦 <b>Оплата через СБП</b>\n\n"
            f"{plan['emoji']} Тариф: <b>{plan['title']}</b>\n"
            f"💰 Сумма: <b>{plan['sbp_rub']} ₽ / мес</b>\n"
            f"📅 Срок: 30 дней\n"
            f"📧 Чек: <code>{email}</code>\n\n"
            "1️⃣ Нажми «Оплатить» — откроется QR-код СБП\n"
            "2️⃣ Отсканируй QR в приложении банка\n"
            "3️⃣ Вернись и нажми «Я оплатил — проверить»\n\n"
            f"<i>🔐 ID транзакции: <code>{txn_id}</code></i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"🏦 Оплатить {plan['sbp_rub']} ₽", url=pay_url)],
                [InlineKeyboardButton(
                    text="✅ Я оплатил — проверить",
                    callback_data=f"check_sbp:{plan_key}:{email}:{txn_id}",
                )],
                [InlineKeyboardButton(text="📩 Поддержка",    url="https://t.me/selftabs_support")],
                [InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_main")],
            ]),
        )

    # ── Крипта (Platega) ──────────────────────────────────────────────────
    elif pay_method == "crypto":
        try:
            result = await _platega_create(plan_key, "crypto")
        except Exception as e:
            logger.error("[CRYPTO] Ошибка создания транзакции: %s", e)
            await message.answer(
                f"❌ Ошибка создания платежа: {str(e)[:200]}",
                reply_markup=kb_back_main(),
            )
            return

        txn_id  = result["transaction_id"]
        pay_url = result["redirect_url"]

        await message.answer(
            f"🪙 <b>Крипто-оплата (Platega)</b>\n\n"
            f"{plan['emoji']} Тариф: <b>{plan['title']}</b>\n"
            f"💰 Сумма: <b>~{plan['usdt']}$</b> (~{plan['usdt_rub']} ₽)\n"
            f"📅 Срок: 30 дней\n"
            f"📧 Чек: <code>{email}</code>\n\n"
            "1️⃣ Нажми «Оплатить» — откроется страница оплаты\n"
            "2️⃣ Выбери крипто-кошелёк и подтверди перевод\n"
            "3️⃣ Вернись и нажми «Я оплатил — проверить»\n\n"
            f"<i>🔐 ID транзакции: <code>{txn_id}</code></i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"🪙 Оплатить ~{plan['usdt']}$", url=pay_url)],
                [InlineKeyboardButton(
                    text="✅ Я оплатил — проверить",
                    callback_data=f"check_crypto:{txn_id}:{plan_key}:{email}",
                )],
                [InlineKeyboardButton(text="📩 Поддержка",    url="https://t.me/selftabs_support")],
                [InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_main")],
            ]),
        )


# ══════════════════════════════════════════════════════════════════════════
# ОПЛАТА — TELEGRAM STARS
# ══════════════════════════════════════════════════════════════════════════

@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    logger.info("[STARS] pre_checkout payload=%s amount=%s",
                query.invoice_payload, query.total_amount)
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def payment_success(message: Message):
    tg_id   = message.from_user.id
    payment = message.successful_payment

    # payload формат: demo:{plan_key}:{tg_id}:{email}
    parts = payment.invoice_payload.split(":")
    if len(parts) != 4 or parts[0] != "demo":
        logger.error("[STARS] Неизвестный payload: %s", payment.invoice_payload)
        await message.answer(
            "⚠️ Оплата прошла, но не удалось определить тариф.\n"
            "Сохрани ID и свяжись с поддержкой:\n"
            f"<code>{payment.telegram_payment_charge_id}</code>",
            reply_markup=kb_back_main(),
        )
        return

    _, plan_key, _, email = parts
    plan      = PLANS.get(plan_key, {})
    plan_name = plan.get("title", plan_key.capitalize())
    stars     = payment.total_amount
    charge_id = payment.telegram_payment_charge_id

    logger.info("[STARS] payment OK tg=%s plan=%s stars=%s charge=%s email=%s",
                tg_id, plan_key, stars, charge_id, email)

    await message.answer(
        f"🎉 <b>Подписка активирована!</b>\n\n"
        f"{plan.get('emoji', '')} Тариф: <b>{plan_name}</b>\n"
        f"⭐ Списано: <b>{stars} Stars</b>\n"
        f"📅 Срок: 30 дней\n"
        f"📧 Чек отправлен на: <code>{email}</code>\n\n"
        "Вернись в расширение — статус уже обновлён 🚀",
        reply_markup=kb_back_main(),
    )

    # Отправляем чек на email
    asyncio.create_task(send_receipt_email(
        to_email=email,
        plan_key=plan_key,
        payment_method="Telegram Stars",
        amount_str=f"{stars} Stars (~{plan.get('price_rub', '')})",
        charge_id=charge_id,
    ))

    # 🧪 ТЕСТ — возвращаем Stars обратно
    try:
        await bot.refund_star_payment(
            user_id=tg_id,
            telegram_payment_charge_id=charge_id,
        )
        await message.answer(
            f"🧪 <b>[ТЕСТ] Звёзды возвращены</b>\n"
            f"⭐ Возврат: <b>{stars} Stars</b>\n"
            f"<code>{charge_id}</code>\n\n"
            "<i>Убери refund_stars() перед продом.</i>",
        )
    except Exception as e:
        logger.warning("[STARS] Ошибка возврата: %s", e)


# ══════════════════════════════════════════════════════════════════════════
# ОПЛАТА — СБП (Platega)
# ══════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data.startswith("check_sbp:"))
async def check_sbp(call: CallbackQuery):
    # check_sbp:{plan_key}:{email}:{transaction_id}
    parts  = call.data.split(":")
    plan_key = parts[1]
    email    = parts[2]
    txn_id   = parts[3] if len(parts) > 3 else ""
    tg_id    = call.from_user.id
    plan     = PLANS.get(plan_key, {})

    await call.answer("🔄 Проверяем оплату...")

    status = await _platega_check(txn_id)
    logger.info("[SBP] check tg=%s plan=%s txn=%s status=%s", tg_id, plan_key, txn_id, status)

    if status == "CONFIRMED":
        await call.message.edit_text(
            f"🎉 <b>Подписка активирована!</b>\n\n"
            f"{plan.get('emoji', '')} Тариф: <b>{plan.get('title', plan_key)}</b>\n"
            f"🏦 Способ: СБП\n"
            f"💰 Оплачено: {plan.get('sbp_rub')} ₽\n"
            f"📅 Срок: 30 дней\n"
            f"📧 Чек отправлен на: <code>{email}</code>\n\n"
            "Вернись в расширение — статус уже обновлён 🚀",
            reply_markup=kb_back_main(),
        )
        asyncio.create_task(send_receipt_email(
            to_email=email,
            plan_key=plan_key,
            payment_method="СБП (Platega)",
            amount_str=f"{plan.get('sbp_rub')} ₽",
            charge_id=txn_id,
        ))

    elif status == "CANCELED":
        await call.message.edit_text(
            "❌ <b>Платёж отменён или не прошёл.</b>\n\n"
            "Попробуй ещё раз или выбери другой способ оплаты.",
            reply_markup=kb_back_main(),
        )

    else:  # PENDING
        await call.message.edit_text(
            f"⏳ <b>Ожидаем оплату...</b>\n\n"
            f"Статус ещё не подтверждён. Если ты уже оплатил — подожди "
            f"10–30 секунд и проверь снова.\n\n"
            f"<i>ID: <code>{txn_id}</code></i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="🔄 Проверить снова",
                    callback_data=f"check_sbp:{plan_key}:{email}:{txn_id}",
                )],
                [InlineKeyboardButton(text="📩 Поддержка",    url="https://t.me/selftabs_support")],
                [InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_main")],
            ]),
        )


# ══════════════════════════════════════════════════════════════════════════
# ОПЛАТА — USDT (CryptoPay)
# ══════════════════════════════════════════════════════════════════════════

@dp.callback_query(F.data.startswith("check_crypto:"))
async def check_crypto(call: CallbackQuery):
    # check_crypto:{transaction_id}:{plan_key}:{email}
    parts    = call.data.split(":")
    txn_id   = parts[1]
    plan_key = parts[2]
    email    = parts[3] if len(parts) > 3 else ""
    tg_id    = call.from_user.id
    plan     = PLANS.get(plan_key, {})

    await call.answer("🔄 Проверка оплаты...")

    status = await _platega_check(txn_id)
    logger.info("[CRYPTO] check tg=%s plan=%s txn=%s status=%s", tg_id, plan_key, txn_id, status)

    if status == "CONFIRMED":
        await call.message.edit_text(
            f"🎉 <b>Оплата получена!</b>\n\n"
            f"{plan.get('emoji', '')} Тариф: <b>{plan.get('title', plan_key)}</b>\n"
            f"🪙 Оплачено: <b>~{plan.get('usdt')}$</b> (~{plan.get('usdt_rub')} ₽)\n"
            f"📅 Срок: 30 дней\n"
            f"📧 Чек отправлен на: <code>{email}</code>\n\n"
            "Вернись в расширение — статус уже обновлён 🚀",
            reply_markup=kb_back_main(),
        )
        asyncio.create_task(send_receipt_email(
            to_email=email,
            plan_key=plan_key,
            payment_method="Крипта (Platega)",
            amount_str=f"~{plan.get('usdt')}$ (~{plan.get('usdt_rub')} ₽)",
            charge_id=txn_id,
        ))

    elif status == "CANCELED":
        await call.message.edit_text(
            "❌ <b>Платёж отменён или истёк.</b>\n\nВернись назад и создай новый счёт.",
            reply_markup=kb_back_main(),
        )

    else:  # PENDING
        await call.message.edit_text(
            f"⏳ <b>Ожидание оплаты...</b>\n\n"
            f"Крипто-переводы могут подтверждаться 1–5 минут.\n\n"
            f"<i>ID: <code>{txn_id}</code></i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="🔄 Проверить снова",
                    callback_data=f"check_crypto:{txn_id}:{plan_key}:{email}",
                )],
                [InlineKeyboardButton(text="📩 Поддержка",    url="https://t.me/selftabs_support")],
                [InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_main")],
            ]),
        )


# ── Запуск ────────────────────────────────────────────────────────────────

async def main():

    # ── Health-check сервер для Render ────────────────────────────────────
    async def health(request):
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", 10000))   # Render использует 10000 по умолчанию
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("✅ Health-check сервер запущен на порту %s", port)
    # ─────────────────────────────────────────────────────────────────────

    print("=" * 55)
    print("🚀 SELFTABS DEMO-БОТ ЗАПУЩЕН (без авторизации)")
    print("=" * 55)
    print(f"📧 Email-чеки: {'✅ Resend' if RESEND_API_KEY else '⚠️  RESEND_API_KEY не задан (логи)'}")
    print(f"🏦 СБП:        {'✅ Platega' if BACKEND_URL else '❌ BACKEND_URL не задан'}")
    print(f"🪙 Крипта:     {'✅ Platega' if BACKEND_URL else '❌ BACKEND_URL не задан'}")
    print("=" * 55)

    # ── Graceful shutdown при SIGTERM (Render останавливает именно так) ───
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _handle_sigterm():
        logger.info("SIGTERM получен — начинаем graceful shutdown")
        stop_event.set()

    import signal
    loop.add_signal_handler(signal.SIGTERM, _handle_sigterm)
    loop.add_signal_handler(signal.SIGINT,  _handle_sigterm)
    # ─────────────────────────────────────────────────────────────────────

    await bot.delete_webhook(drop_pending_updates=True)

    # Запускаем polling в фоне, ждём сигнала остановки
    polling_task = asyncio.create_task(dp.start_polling(bot))
    await stop_event.wait()

    # Корректно завершаем всё
    logger.info("Останавливаем polling...")
    polling_task.cancel()
    try:
        await polling_task
    except asyncio.CancelledError:
        pass

    await dp.storage.close()
    await bot.session.close()

    await runner.cleanup()
    logger.info("✅ Бот остановлен чисто")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n❌ Бот остановлен")