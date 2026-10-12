from __future__ import annotations

import asyncio
import html
import json
import logging
import random
import re
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Message, PreCheckoutQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

import database as db
from config import config, MOSCOW_TZ
from data import (
    CAREER_LINKS,
    EXPENSE_CATEGORIES,
    PYTHON_TASKS,
    RECIPES,
    ROADMAP,
    TOPIC_GUIDES,
    WEEKDAY_NAMES,
    WEEKDAYS,
)
from keyboards import (
    backend_menu,
    back_button,
    budget_menu,
    clear_expenses_confirm_menu,
    food_recipes_menu,
    cancel_keyboard,
    career_menu,
    expense_categories,
    freelance_links,
    hours_menu,
    internship_links,
    learning_menu,
    local_menu,
    main_menu,
    study_menu,
    topics_keyboard,
    gift_selection_keyboard,
    gift_confirm_keyboard,
    stars_topup_menu,
    stars_methods_menu,
)
from services import LocationNotFoundError, get_backend_news, get_weather, search_hh_vacancies
from bus_tracker import BusTrackerError, get_stop_forecast, resolve_stop_uuid

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("mai_lifeos")

# Payment is for a digital Supporter status delivered inside MAI LifeOS.
STAR_SUPPORT_PACKAGES = {15, 25, 50, 100, 250, 500}
STAR_SUPPORT_PAYLOAD_PREFIX = "mai_supporter"


class ExpenseForm(StatesGroup):
    amount = State()
    category = State()


class BudgetForm(StatesGroup):
    amount = State()


class LocalWeatherForm(StatesGroup):
    location = State()


class FoodForm(StatesGroup):
    amount = State()


class LessonForm(StatesGroup):
    details = State()


class DeadlineForm(StatesGroup):
    details = State()


class SessionForm(StatesGroup):
    session_date = State()


class BusForm(StatesGroup):
    departure_time = State()


class BusTrackerForm(StatesGroup):
    route_number = State()
    boarding_stop = State()
    direction_stop = State()
    boarding_stop_link = State()


class HoursForm(StatesGroup):
    hours = State()


class GiftForm(StatesGroup):
    recipient_username = State()
    confirm = State()


class UserLimitMiddleware(BaseMiddleware):
    """Allow at most MAX_USERS unique Telegram user IDs to use the bot."""

    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user is None:
            return await handler(event, data)

        # Запомните имя пользователя перед применением глобального ограничения на количество пользователей, чтобы
        # владелец мог позже преобразовать получателя в числовой идентификатор Telegram.
        await db.store_telegram_identity(user.id, user.username, user.first_name)
        allowed = await db.register_user_if_allowed(user.id)
        if allowed:
            return await handler(event, data)

        message = (
            "⛔ <b>Лимит пользователей достигнут.</b>\n\n"
            f"Бот рассчитан максимум на <b>{max(1, config.max_users)} пользователей</b>, "
            "и все места уже заняты. Ваш Telegram ID: "
            f"<code>{user.id}</code>.\n\n"
            "Доступ выдаётся первым уникальным Telegram ID и сохраняется после перезапуска бота."
        )
        if isinstance(event, Message):
            # The bot is intended for private use; avoid flooding group chats.
            if event.chat.type == "private":
                await event.answer(message)
        elif isinstance(event, CallbackQuery):
            await event.answer(
                f"Лимит {max(1, config.max_users)} пользователей достигнут. Доступ закрыт.",
                show_alert=True,
            )
        logger.warning("Denied Telegram user_id=%s: user limit reached", user.id)
        return None


bot = Bot(token=config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML)) if config.bot_token else None
dp = Dispatcher()
dp.message.outer_middleware(UserLimitMiddleware())
dp.callback_query.outer_middleware(UserLimitMiddleware())

BUS_PATH = Path(__file__).with_name("bus_schedule.json")
with BUS_PATH.open("r", encoding="utf-8") as f:
    BUS_SCHEDULE: dict[str, Any] = json.load(f)


def money(amount: float) -> str:
    return f"{amount:,.2f} ₽".replace(",", " ").replace(".00", "")


def moscow_now() -> datetime:
    return datetime.now(MOSCOW_TZ)


def clean_time(value: str) -> str:
    return value.strip().replace(".", ":")


def back_to_career_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back:career")]])


def date_input_help() -> str:
    return "Укажи дату в формате <code>ДД.ММ.ГГГГ</code>, например <code>15.01.2027</code>."


def is_gift_limited(gift: Any) -> bool:
    """True when Telegram marks the gift as having a finite supply."""
    return (
        getattr(gift, "total_count", None) is not None
        or getattr(gift, "remaining_count", None) is not None
    )


def gift_emoji(gift: Any) -> str:
    sticker = getattr(gift, "sticker", None)
    return getattr(sticker, "emoji", None) or "🎁"


async def get_regular_gifts() -> list[Any]:
    """Return basic regular gifts in the requested range; NFT gifts are not selected."""
    if bot is None:
        raise RuntimeError("BOT_TOKEN is not configured")
    catalogue = await bot.get_available_gifts()
    eligible: list[Any] = []
    for gift in catalogue.gifts:
        price = int(getattr(gift, "star_count", 0) or 0)
        if not 15 <= price <= 100:
            continue
        # Avoid offers available only to Premium subscribers.
        if bool(getattr(gift, "is_premium", False)):
            continue
        remaining = getattr(gift, "remaining_count", None)
        personal_remaining = getattr(gift, "personal_remaining_count", None)
        if remaining is not None and int(remaining) <= 0:
            continue
        if personal_remaining is not None and int(personal_remaining) <= 0:
            continue
        eligible.append(gift)
    return sorted(eligible, key=lambda item: (int(item.star_count), str(item.id)))


async def get_bot_star_balance() -> int:
    if bot is None:
        raise RuntimeError("BOT_TOKEN is not configured")
    balance = await bot.get_my_star_balance()
    return int(getattr(balance, "amount", 0))


async def recipient_accepts_gift(recipient_id: int, gift: Any) -> bool | None:
    """Return False only when Telegram explicitly reports the gift type is disabled."""
    if bot is None:
        return None
    try:
        recipient_chat = await bot.get_chat(chat_id=recipient_id)
    except TelegramAPIError as exc:
        logger.info("Could not precheck gift settings for recipient %s: %s", recipient_id, exc)
        return None
    accepted = getattr(recipient_chat, "accepted_gift_types", None)
    if accepted is None:
        return None
    field = "limited_gifts" if is_gift_limited(gift) else "unlimited_gifts"
    value = getattr(accepted, field, None)
    if value is None:
        return None
    return bool(value)


def gift_access_denied_text() -> str:
    return "Ошибка, попроси разрешение у @residentazeroy"


def format_gift_countdown(seconds: int) -> str:
    """Format cooldown seconds as a friendly Russian countdown."""
    seconds = max(0, int(seconds))
    days, remainder = divmod(seconds, 24 * 60 * 60)
    hours, remainder = divmod(remainder, 60 * 60)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days} дн.")
    if hours or days:
        parts.append(f"{hours} ч.")
    if minutes or hours or days:
        parts.append(f"{minutes} мин.")
    parts.append(f"{secs} сек.")
    return " ".join(parts)


async def display_gift_menu(message: Message, user_id: int, edit_existing: bool = False) -> None:
    send = message.edit_text if edit_existing else message.answer
    if not await db.can_use_gift_module(user_id):
        await send(gift_access_denied_text(), reply_markup=back_button("main"))
        return
    if user_id != config.gift_owner_id:
        cooldown = await db.get_gift_cooldown_remaining_seconds(user_id)
        if cooldown > 0:
            await send(
                "⏳ <b>Лимит отправки подарков</b>\n\n"
                "Ты уже отправил один подарок за последние 24 часа.\n"
                f"До следующей отправки осталось: <b>{format_gift_countdown(cooldown)}</b>.",
                reply_markup=back_button("main"),
            )
            return
    if bot is None:
        await send("Бот ещё не настроен: не найден BOT_TOKEN.")
        return
    try:
        balance = await get_bot_star_balance()
        gifts = await get_regular_gifts()
    except Exception as exc:
        logger.exception("Failed to load gift catalogue or Stars balance: %s", exc)
        await send(
            "Не удалось загрузить подарки или баланс Stars. Проверь версию aiogram и доступность Telegram Bot API, затем попробуй снова.",
            reply_markup=back_button("main"),
        )
        return
    if not gifts:
        await send(
            f"🎁 <b>Подарки</b>\n\nБаланс бота: <b>{balance} ⭐</b>\n"
            "Сейчас Telegram не вернул доступных базовых подарков стоимостью от 15 до 100 Stars.",
            reply_markup=back_button("main"),
        )
        return
    await send(
        "🎁 <b>Отправка подарка</b>\n\n"
        f"Баланс бота: <b>{balance} ⭐</b>\n"
        "Выбери обычный подарок стоимостью от <b>15 до 100 Stars</b>. Отправляется базовый подарок\n\n"
        "После выбора введи username получателя. Он должен хотя бы раз написать этому боту, чтобы бот смог сопоставить username с числовым Telegram ID.",
        reply_markup=gift_selection_keyboard(gifts),
    )


async def show_module(callback: CallbackQuery, state: FSMContext, module: str) -> None:
    await state.clear()
    await callback.answer()
    message = callback.message
    if not message:
        return
    if module == "budget":
        budget = await db.get_budget(callback.from_user.id)
        if budget is None:
            await message.edit_text(
                "<b>💰 Мой бюджет</b>\n\n"
                "Месячный бюджет ещё не задан. Сначала укажи сумму, которой хочешь располагать в этом месяце. "
                "До этого момента статистика, расходы и остаток бюджета не рассчитываются.",
                reply_markup=budget_menu(budget_is_set=False),
            )
        else:
            await message.edit_text(
                "<b>💰 Мой бюджет</b>\n\n"
                f"Твой бюджет на этот месяц: <b>{money(budget)}</b>.\n"
                "Теперь можно добавлять расходы, смотреть статистику и подбирать блюда по сумме.",
                reply_markup=budget_menu(),
            )
    elif module == "learning":
        streak = await db.get_streak(callback.from_user.id)
        await message.edit_text(
            "<b>📚 Путь в Backend</b>\n\n" +
            "Темы идут в заданном тобой порядке. Ежедневная учебная активность поддерживает streak.\n\n" +
            f"🔥 Текущая серия: <b>{streak} дн.</b>",
            reply_markup=learning_menu(),
        )
    elif module == "study":
        await message.edit_text("<b>🎓 МАИ-хелпер</b>\n\nРасписание и дедлайны сохраняются отдельно для твоего Telegram-аккаунта. Дату сессии можно задать вручную.", reply_markup=study_menu())
    elif module == "career":
        await message.edit_text("<b>💼 Работа и фриланс</b>\n\nПодборка стартовых вакансий, стажировок, фриланс-площадок и способов заработать без опыта.", reply_markup=career_menu())
    elif module == "local":
        saved_weather_location = await db.get_weather_location(callback.from_user.id)
        if saved_weather_location:
            weather_intro = (
                f"Твой выбранный район для погоды: <b>{html.escape(saved_weather_location)}</b>. "
                "Нажми кнопку погоды, чтобы получить свежие данные, или выбери другой район.\n\n"
            )
        else:
            weather_intro = (
                "Район для погоды ещё не выбран. Нажми кнопку «🌦 Погода по моему району» "
                "и введи свой район или город — по умолчанию никакое место не задано.\n\n"
            )
        await message.edit_text(
            "<b>🌦 Район и транспорт</b>\n\n"
            + weather_intro
            + "Для небольших районов, которые не находятся по одному названию, укажи ещё город. "
            "Можно настроить персональное отслеживание прибытия по данным табло Московского транспорта. Источник неофициальный и может быть недоступен; в таком случае бот сообщит об этом, а не выдаст расписание за GPS.",
            reply_markup=local_menu(BUS_SCHEDULE["yandex_live_url"]),
        )
    elif module == "backend":
        await message.edit_text("<b>📰 Backend-новости и часы кода</b>\n\nЗаголовки новостей будут кликабельными. В трекер можно добавлять несколько сессий кодинга за день.", reply_markup=backend_menu())
    elif module == "topup":
        supporter = await db.get_star_supporter(callback.from_user.id)
        status_text = (
            f"\n\n🎖 Твой статус Supporter активен. Всего оплачено: <b>{supporter['total_stars']} ⭐</b>."
            if supporter else "\n\nПосле оплаты ты получишь постоянный цифровой статус Supporter в MAI LifeOS."
        )
        await message.edit_text(
            "<b>⭐ Поддержать MAI LifeOS</b>\n\n"
            "Выбери сумму оплаты в Telegram Stars. Оплата проходит через официальный счёт Telegram; после успешной оплаты бот фиксирует транзакцию и выдаёт цифровой статус Supporter. Это не перевод звёзд между аккаунтами и не пополнение личного баланса пользователя.\n\n"
            "Перед покупкой будет показано подтверждение условий. Для вопросов по платежам используй /paysupport, условия — /terms."
            + status_text,
            reply_markup=stars_topup_menu(bool(supporter)),
        )
    elif module == "gift":
        if not await db.can_use_gift_module(callback.from_user.id):
            await message.edit_text(gift_access_denied_text(), reply_markup=back_button("main"))
            return
        await display_gift_menu(message, callback.from_user.id, edit_existing=True)


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await db.ensure_user(message.from_user.id)
    supporter = await db.get_star_supporter(message.from_user.id)
    supporter_badge = "\n\n🎖 <b>Статус Supporter активен</b>" if supporter else ""
    await message.answer(
        "<b>Привет! Это MAI LifeOS</b> ✈️\n\n" 
        "Личный Telegram-помощник: бюджет, путь в Backend, учёба в МАИ, работа, транспорт, трекер часов кода, подарки и платежи Stars.\n\n"
        "Выбирай модуль. Данные сохраняются в локальной SQLite-базе."
        + supporter_badge,
        reply_markup=main_menu(),
    )


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "<b>Команды</b>\n/start — открыть меню\n/help — помощь\n/cancel — отменить текущий ввод\n/terms — условия покупок за Stars\n/paysupport — поддержка по платежам\n\n"
        "Почти все действия доступны через кнопки. Токен бота храни только в файле <code>.env</code> и не публикуй его.",
        reply_markup=main_menu(),
    )


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Действие отменено. Главное меню:", reply_markup=main_menu())


@dp.callback_query(F.data.startswith("module:"))
async def module_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.data:
        await show_module(callback, state, callback.data.split(":", 1)[1])


@dp.callback_query(F.data.startswith("back:"))
async def back_callback(callback: CallbackQuery, state: FSMContext) -> None:
    target = callback.data.split(":", 1)[1] if callback.data else "main"
    await state.clear()
    if not callback.message:
        await callback.answer()
        return
    if target == "main":
        await callback.answer()
        await callback.message.edit_text("<b>MAI LifeOS</b> — выбери модуль:", reply_markup=main_menu())
    elif target in {"budget", "learning", "study", "career", "local", "backend", "gift", "topup"}:
        await show_module(callback, state, target)
    else:
        await callback.answer()


@dp.callback_query(F.data == "cancel")
async def cancel_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")
    if callback.message:
        await callback.message.answer("Действие отменено. Главное меню:", reply_markup=main_menu())




# ------------------------------
# Платежи Telegram Stars: цифровой статус Supporter
# ------------------------------

@dp.callback_query(F.data == "stars:menu")
async def stars_menu_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    if not callback.message:
        return
    supporter = await db.get_star_supporter(callback.from_user.id)
    status_text = (
        f"\n\n🎖 Твой статус Supporter активен. Всего оплачено: <b>{supporter['total_stars']} ⭐</b>."
        if supporter else "\n\nПосле успешной оплаты ты получишь цифровой статус Supporter."
    )
    await callback.message.edit_text(
        "<b>⭐ Поддержать MAI LifeOS</b>\n\n"
        "Выбери сумму в Stars. Платёж проводится через официальный интерфейс Telegram, а покупка выдаёт цифровой статус Supporter внутри бота.\n\n"
        "Условия: /terms. Поддержка по платежам: /paysupport."
        + status_text,
        reply_markup=stars_topup_menu(bool(supporter)),
    )


@dp.callback_query(F.data == "stars:methods")
async def stars_methods_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            "<b>🛒 Где приобрести Telegram Stars</b>\n\n"
            "• <b>iPhone/iPad:</b> в приложении Telegram можно купить Stars через встроенную покупку Apple, если она доступна для твоего аккаунта.\n"
            "• <b>Android:</b> покупка внутри Telegram через Google Play, если этот вариант доступен в твоей версии приложения.\n"
            "• <b>@PremiumBot:</b> официальный бот Telegram для покупки Stars; доступность зависит от клиента и региона.\n"
            "• <b>Fragment:</b> официальный сервис, где доступны покупки Stars в поддерживаемых сценариях; может потребоваться TON-кошелёк и дополнительная проверка аккаунта.\n\n"
            "После покупки Stars на свой аккаунт вернись сюда и оплати выбранный счёт. Сам счёт MAI LifeOS принимает оплату в Stars, которые уже доступны на твоём балансе. Способы оплаты и доступность могут отличаться по платформе, региону и аккаунту.",
            reply_markup=stars_methods_menu(),
            disable_web_page_preview=True,
        )


@dp.callback_query(F.data.startswith("stars:prepare:"))
async def stars_prepare_payment(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.message or not callback.data:
        await callback.answer()
        return
    try:
        amount = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректная сумма.", show_alert=True)
        return
    if amount not in STAR_SUPPORT_PACKAGES:
        await callback.answer("Такой пакет недоступен.", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(
        "<b>📜 Подтверждение условий покупки</b>\n\n"
        f"Ты собираешься приобрести цифровой статус Supporter MAI LifeOS за <b>{amount} ⭐</b>.\n\n"
        "После подтверждения Telegram откроет официальный экран оплаты. Статус будет выдан только после получения подтверждения об успешном платеже. Платёж обрабатывается ботом, а по вопросам покупки можно обратиться к владельцу @residentazeroy через /paysupport. Не оплачивай счёт, если не согласен с условиями.\n\n"
        "Нажимая «Принимаю условия», ты подтверждаешь, что прочитал /terms.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"✅ Принимаю условия — {amount} ⭐", callback_data=f"stars:buy:{amount}")],
            [InlineKeyboardButton(text="📜 Открыть условия", callback_data="stars:terms")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="stars:menu")],
        ]),
    )


@dp.callback_query(F.data == "stars:terms")
async def stars_terms_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "<b>Условия покупки в MAI LifeOS</b>\n\n"
            "Платёж проводится в Telegram Stars (XTR) за цифровой статус Supporter, отображаемый ботом. Статус выдаётся после подтверждённого успешного платежа. Стоимость указана в Stars до открытия счёта. Не передавай другим пользователям данные своего аккаунта.\n\n"
            "Владелец бота отвечает за поддержку по платежам и рассмотрение запросов на возврат в соответствии с правилами Telegram. По любым вопросам напиши /paysupport. Покупка Stars у Telegram и платёж за цифровой статус в боте — разные операции.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад к покупке", callback_data="stars:menu")]
            ]),
        )


@dp.callback_query(F.data.startswith("stars:buy:"))
async def stars_buy_invoice(callback: CallbackQuery, state: FSMContext) -> None:
    if bot is None or not callback.data:
        await callback.answer("Бот не настроен.", show_alert=True)
        return
    try:
        amount = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректная сумма.", show_alert=True)
        return
    if amount not in STAR_SUPPORT_PACKAGES:
        await callback.answer("Такой пакет недоступен.", show_alert=True)
        return
    await callback.answer("Готовлю счёт Telegram Stars…")
    payload = f"{STAR_SUPPORT_PAYLOAD_PREFIX}:{callback.from_user.id}:{amount}:{secrets.token_urlsafe(8)}"
    try:
        await db.create_star_support_order(payload, callback.from_user.id, amount)
        await bot.send_invoice(
            chat_id=callback.from_user.id,
            title="Статус Supporter MAI LifeOS",
            description=f"Цифровой статус Supporter в MAI LifeOS. Стоимость: {amount} Telegram Stars.",
            payload=payload,
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label="Цифровой статус Supporter", amount=amount)],
            start_parameter="",
        )
    except Exception as exc:
        logger.exception("Could not create/send Stars invoice: %s", exc)
        await db.cancel_star_support_order(payload)
        if callback.message:
            await callback.message.answer(
                "Не удалось создать счёт Telegram Stars. Проверь обновление aiogram и попробуй ещё раз."
            )


@dp.pre_checkout_query()
async def stars_pre_checkout(pre_checkout_query: PreCheckoutQuery) -> None:
    if bot is None:
        return
    error_message = "Счёт не прошёл проверку. Вернись в меню и создай новый."
    try:
        order = await db.get_star_support_order(pre_checkout_query.invoice_payload)
        parts = pre_checkout_query.invoice_payload.split(":")
        payload_user_id = int(parts[1]) if len(parts) == 4 and parts[0] == STAR_SUPPORT_PAYLOAD_PREFIX else None
        valid = (
            order is not None
            and str(order["status"]) == "pending"
            and int(order["user_id"]) == pre_checkout_query.from_user.id
            and payload_user_id == pre_checkout_query.from_user.id
            and pre_checkout_query.currency == "XTR"
            and int(order["stars"]) == int(pre_checkout_query.total_amount)
            and int(order["stars"]) in STAR_SUPPORT_PACKAGES
        )
    except Exception:
        logger.exception("Unable to validate Stars pre-checkout")
        valid = False
    if valid:
        await pre_checkout_query.answer(ok=True)
    else:
        await pre_checkout_query.answer(ok=False, error_message=error_message)


@dp.message(F.successful_payment)
async def stars_payment_success(message: Message) -> None:
    payment = message.successful_payment
    if payment is None or message.from_user is None:
        return
    payload = payment.invoice_payload
    try:
        parts = payload.split(":")
        expected_user_id = int(parts[1]) if len(parts) == 4 and parts[0] == STAR_SUPPORT_PAYLOAD_PREFIX else None
        valid = (
            expected_user_id == message.from_user.id
            and payment.currency == "XTR"
            and int(payment.total_amount) in STAR_SUPPORT_PACKAGES
        )
        if not valid:
            logger.error("Received unexpected Stars payment payload from user %s", message.from_user.id)
            await message.answer(
                "Платёж получен, но его параметры не прошли проверку автоматически. Напиши владельцу через /paysupport и не отправляй повторный платёж."
            )
            return
        processed = await db.complete_star_support_order(
            payload=payload,
            user_id=message.from_user.id,
            stars=int(payment.total_amount),
            currency=payment.currency,
            telegram_payment_charge_id=payment.telegram_payment_charge_id,
        )
        if not processed:
            await message.answer(
                "Этот платёж уже обработан либо счёт устарел. Если деньги списались, но статус не появился, напиши /paysupport."
            )
            return
        supporter = await db.get_star_supporter(message.from_user.id)
        await message.answer(
            "✅ <b>Оплата прошла успешно!</b>\n\n"
            f"Получено ботом: <b>{payment.total_amount} ⭐</b>\n"
            "🎖 Тебе выдан цифровой статус <b>Supporter MAI LifeOS</b>. Он сохранится за твоим аккаунтом.\n"
            f"Всего оплачено в рамках статуса: <b>{supporter['total_stars'] if supporter else payment.total_amount} ⭐</b>.\n\n"
            "Спасибо за поддержку проекта! По вопросам оплаты используй /paysupport.",
            reply_markup=main_menu(),
        )
        try:
            balance = await get_bot_star_balance()
            await bot.send_message(
                chat_id=config.gift_owner_id,
                text=(f"⭐ Получена оплата MAI LifeOS: {payment.total_amount} Stars от user_id={message.from_user.id}. "
                      f"Текущий баланс бота: {balance} Stars."),
            )
        except Exception:
            logger.exception("Unable to notify owner about successful Stars payment")
    except Exception:
        logger.exception("Failed to process successful Telegram Stars payment")
        await message.answer(
            "Telegram подтвердил платёж, но бот не смог завершить его учёт. Не оплачивай повторно — напиши /paysupport."
        )


@dp.message(Command("terms"))
async def payment_terms_command(message: Message) -> None:
    await message.answer(
        "<b>Условия покупок MAI LifeOS</b>\n\n"
        "Бот принимает Telegram Stars (XTR) за цифровой статус Supporter, который отображается внутри MAI LifeOS. Статус активируется только после подтверждения успешного платежа Telegram. Перед оплатой проверь сумму и описание счёта.\n\n"
        "Владелец бота обрабатывает вопросы по заказам и рассматривает запросы на возврат согласно применимым правилам Telegram. Покупка Stars на личный аккаунт пользователя выполняется отдельно через доступные способы Telegram. По вопросам оплаты: /paysupport или @residentazeroy.",
        reply_markup=back_button("topup"),
    )


@dp.message(Command("paysupport"))
async def payment_support_command(message: Message) -> None:
    await message.answer(
        "<b>Поддержка по платежам</b>\n\n"
        "Если Stars списались, но статус Supporter не появился, не оплачивай повторно. Напиши владельцу @residentazeroy и укажи дату, сумму в Stars и приложи сведения о платеже из истории Telegram. Не отправляй токены или коды входа.",
        reply_markup=back_button("topup"),
    )


# ------------------------------
# Модуль «Подарок» — доступ по ID владельца/разрешённых пользователей
# ------------------------------

@dp.callback_query(F.data == "gift:refresh")
async def gift_refresh(callback: CallbackQuery, state: FSMContext) -> None:
    if not await db.can_use_gift_module(callback.from_user.id):
        await callback.answer(gift_access_denied_text(), show_alert=True)
        return
    await state.clear()
    await callback.answer("Обновляю баланс и каталог подарков…")
    if callback.message:
        await display_gift_menu(callback.message, callback.from_user.id)


@dp.callback_query(F.data.startswith("gift:select:"))
async def gift_select(callback: CallbackQuery, state: FSMContext) -> None:
    if not await db.can_use_gift_module(callback.from_user.id):
        await callback.answer(gift_access_denied_text(), show_alert=True)
        return
    if bot is None or not callback.data:
        await callback.answer("Бот не настроен.", show_alert=True)
        return
    gift_id = callback.data.split(":", 2)[2]
    try:
        gifts = await get_regular_gifts()
        gift = next((item for item in gifts if str(item.id) == gift_id), None)
        balance = await get_bot_star_balance()
    except Exception as exc:
        logger.exception("Unable to load gifts during selection: %s", exc)
        await callback.answer("Не получилось загрузить подарки или баланс.", show_alert=True)
        return
    if gift is None:
        await callback.answer("Этот подарок больше недоступен. Обнови список.", show_alert=True)
        return
    price = int(gift.star_count)
    if balance < price:
        await callback.answer("Недостаточно Stars", show_alert=True)
        if callback.message:
            await callback.message.answer(
                f"❌ <b>Недостаточно Stars.</b>\nСтоимость подарка: <b>{price} ⭐</b>.\n"
                f"Текущий баланс бота: <b>{balance} ⭐</b>.",
                reply_markup=gift_selection_keyboard(gifts),
            )
        return
    await state.clear()
    await state.update_data(gift_id=str(gift.id), gift_price=price, gift_emoji=gift_emoji(gift))
    await state.set_state(GiftForm.recipient_username)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            f"Выбран подарок {gift_emoji(gift)} за <b>{price} ⭐</b>.\n\n"
            "Введи username получателя в формате <code>@username</code> (или ссылку t.me/username).\n"
            "Telegram Bot API не позволяет надёжно получить ID произвольного человека только по username: получатель должен хотя бы раз написать боту.",
            reply_markup=cancel_keyboard(),
        )


@dp.message(GiftForm.recipient_username)
async def gift_recipient_input(message: Message, state: FSMContext) -> None:
    if not await db.can_use_gift_module(message.from_user.id):
        await state.clear()
        await message.answer(gift_access_denied_text(), reply_markup=main_menu())
        return
    raw = (message.text or "").strip()
    match = re.fullmatch(r"(?:https?://)?t\.me/([A-Za-z0-9_]{5,32})/?", raw, flags=re.IGNORECASE)
    username = match.group(1) if match else raw.lstrip("@").strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        await message.answer("Не удалось распознать username. Введи, например, <code>@username</code>.", reply_markup=cancel_keyboard())
        return

    recipient_id = await db.get_user_id_by_username(username)
    if recipient_id is None:
        await message.answer(
            f"Не нашёл Telegram ID для <code>@{html.escape(username)}</code>.\n\n"
            "Попроси получателя открыть этого бота и отправить любое сообщение, например <code>/start</code>, затем повтори ввод username. "
            "Это ограничение Bot API: произвольный username нельзя напрямую преобразовать в числовой user_id.",
            reply_markup=cancel_keyboard(),
        )
        return
    if recipient_id == message.from_user.id:
        await message.answer("Нельзя отправить подарок самому себе через этот модуль. Введи другого получателя.", reply_markup=cancel_keyboard())
        return

    data = await state.get_data()
    gift_id = str(data.get("gift_id", ""))
    try:
        gifts = await get_regular_gifts()
        gift = next((item for item in gifts if str(item.id) == gift_id), None)
    except Exception as exc:
        logger.exception("Unable to verify selected gift: %s", exc)
        await message.answer("Не удалось проверить подарок. Попробуй позже.", reply_markup=cancel_keyboard())
        return
    if gift is None:
        await state.clear()
        await message.answer("Выбранный подарок больше недоступен. Открой модуль «Подарок» и выбери другой.", reply_markup=main_menu())
        return

    accepted = await recipient_accepts_gift(recipient_id, gift)
    if accepted is False:
        await message.answer(
            "❌ Пользователь ограничил принятие подарков этого типа в настройках Telegram. Выбери другого получателя.",
            reply_markup=cancel_keyboard(),
        )
        return

    await state.update_data(
        recipient_id=recipient_id,
        recipient_username=username,
        gift_id=str(gift.id),
        gift_price=int(gift.star_count),
        gift_emoji=gift_emoji(gift),
    )
    await state.set_state(GiftForm.confirm)
    await message.answer(
        "<b>Подтверди отправку подарка</b>\n\n"
        f"Получатель: <b>@{html.escape(username)}</b>\n"
        f"Telegram ID: <code>{recipient_id}</code>\n"
        f"Подарок: {gift_emoji(gift)}\n"
        f"Стоимость: <b>{int(gift.star_count)} ⭐</b>\n\n"
        "После подтверждения Stars будут списаны с баланса бота. Отправленный обычный подарок нельзя отменить этим ботом.",
        reply_markup=gift_confirm_keyboard(),
    )


@dp.callback_query(F.data == "gift:cancel")
async def gift_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    if not await db.can_use_gift_module(callback.from_user.id):
        await callback.answer(gift_access_denied_text(), show_alert=True)
        return
    await state.clear()
    await callback.answer("Отправка отменена")
    if callback.message:
        await callback.message.answer("Отправка подарка отменена.", reply_markup=main_menu())


@dp.callback_query(F.data == "gift:send")
async def gift_send_confirmed(callback: CallbackQuery, state: FSMContext) -> None:
    if not await db.can_use_gift_module(callback.from_user.id):
        await callback.answer(gift_access_denied_text(), show_alert=True)
        return
    if bot is None:
        await callback.answer("Бот не настроен.", show_alert=True)
        return

    data = await state.get_data()
    try:
        recipient_id = int(data["recipient_id"])
        username = str(data["recipient_username"])
        gift_id = str(data["gift_id"])
    except (KeyError, TypeError, ValueError):
        await state.clear()
        await callback.answer("Данные отправки устарели. Начни заново.", show_alert=True)
        return

    if callback.from_user.id != config.gift_owner_id:
        cooldown = await db.get_gift_cooldown_remaining_seconds(callback.from_user.id)
        if cooldown > 0:
            await state.clear()
            countdown = format_gift_countdown(cooldown)
            await callback.answer(f"Следующий подарок через {countdown}", show_alert=True)
            if callback.message:
                await callback.message.answer(
                    "⏳ <b>Лимит отправки подарков</b>\n\n"
                    "Разрешён один подарок за 24 часа.\n"
                    f"Следующий подарок можно отправить через: <b>{countdown}</b>.",
                    reply_markup=main_menu(),
                )
            return

    try:
        gifts = await get_regular_gifts()
        gift = next((item for item in gifts if str(item.id) == gift_id), None)
        balance = await get_bot_star_balance()
    except Exception as exc:
        logger.exception("Unable to check balance/gifts before sending: %s", exc)
        await callback.answer("Не удалось проверить баланс или каталог подарков.", show_alert=True)
        return

    if gift is None:
        await state.clear()
        await callback.answer("Подарок стал недоступен. Выбери другой.", show_alert=True)
        if callback.message:
            await callback.message.answer("Подарок больше недоступен. Открой модуль «Подарок» заново.", reply_markup=main_menu())
        return

    price = int(gift.star_count)
    if balance < price:
        await state.clear()
        await callback.answer("Недостаточно Stars", show_alert=True)
        if callback.message:
            await callback.message.answer(
                "❌ <b>Недостаточно Stars.</b>\n"
                f"Нужно: <b>{price} ⭐</b>. Текущий баланс: <b>{balance} ⭐</b>.",
                reply_markup=main_menu(),
            )
        return

    accepted = await recipient_accepts_gift(recipient_id, gift)
    if accepted is False:
        await state.clear()
        await callback.answer("Получатель не принимает этот тип подарков.", show_alert=True)
        if callback.message:
            await callback.message.answer(
                "❌ Пользователь ограничил принятие подарков этого типа в настройках Telegram. Подарок не отправлен.",
                reply_markup=main_menu(),
            )
        return

    # Владелец не имеет права на получение подарка. Другие разрешенные пользователи получают один успешно отправленный подарок
    # за каждые 24 часа. Бронирование осуществляется атомарно, чтобы предотвратить отправку двух подарков двойными щелчками
    # или одновременными обратными вызовами подтверждения.
    reservation_token: str | None = None
    if callback.from_user.id != config.gift_owner_id:
        reservation_token = secrets.token_urlsafe(18)
        status, remaining = await db.reserve_gift_send_slot(
            callback.from_user.id, reservation_token
        )
        if status != "ready":
            await state.clear()
            countdown = format_gift_countdown(remaining)
            if status == "cooldown":
                response = (
                    "⏳ <b>Лимит отправки подарков</b>\n\n"
                    "Разрешён один подарок за 24 часа.\n"
                    f"До следующей отправки осталось: <b>{countdown}</b>."
                )
                alert = f"Следующий подарок через {countdown}"
            else:
                response = (
                    "⏳ <b>Отправка подарка уже обрабатывается.</b>\n\n"
                    "Не нажимай кнопку повторно. Попробуй снова через "
                    f"<b>{countdown}</b>."
                )
                alert = "Отправка уже обрабатывается"
            await callback.answer(alert, show_alert=True)
            if callback.message:
                await callback.message.answer(response, reply_markup=main_menu())
            return

    try:
        # По умолчанию sendGift отправляет обычный подарок и не оплачивает обновление до NFT.
        await bot.send_gift(
            user_id=recipient_id,
            gift_id=gift_id,
            text="",
        )
    except TelegramBadRequest as exc:
        if reservation_token:
            try:
                await db.release_gift_send_slot(callback.from_user.id, reservation_token)
            except Exception:
                logger.exception("Could not release gift reservation after Telegram rejection")
        error_text = str(exc).lower()
        logger.warning("Telegram rejected gift delivery to %s: %s", recipient_id, exc)
        await state.clear()
        if any(token in error_text for token in ("not enough stars", "insufficient stars", "star balance")):
            try:
                balance_after = await get_bot_star_balance()
            except Exception:
                balance_after = balance
            response = (
                "❌ Недостаточно Stars для отправки.\n"
                f"Стоимость: <b>{price} ⭐</b>. Текущий баланс: <b>{balance_after} ⭐</b>."
            )
        elif any(token in error_text for token in ("gift", "accept", "disallow", "restricted", "not allowed")):
            response = (
                "❌ Telegram отклонил отправку. Возможно, пользователь ограничил принятие таких подарков "
                "или подарок стал недоступен. Проверь настройки получателя и попробуй снова."
            )
        else:
            response = (
                "❌ Не удалось отправить подарок. Возможно, получатель недоступен или Telegram отклонил операцию. "
                "Проверь каталог подарков и попробуй ещё раз."
            )
        if callback.message:
            await callback.message.answer(response, reply_markup=main_menu())
        await callback.answer("Не удалось отправить подарок", show_alert=True)
        return
    except TelegramAPIError as exc:
        if reservation_token:
            try:
                await db.release_gift_send_slot(callback.from_user.id, reservation_token)
            except Exception:
                logger.exception("Could not release gift reservation after Telegram API error")
        logger.exception("Telegram API error when sending gift: %s", exc)
        await state.clear()
        if callback.message:
            await callback.message.answer(
                "❌ Не удалось отправить подарок. Возможно, получатель ограничил приём такого типа подарков "
                "или Telegram временно отклонил операцию. Проверь настройки получателя и текущий баланс Stars.",
                reply_markup=main_menu(),
            )
        await callback.answer("Ошибка Telegram API при отправке подарка", show_alert=True)
        return
    except Exception as exc:
        if reservation_token:
            try:
                await db.release_gift_send_slot(callback.from_user.id, reservation_token)
            except Exception:
                logger.exception("Could not release gift reservation after unexpected error")
        logger.exception("Unexpected error while sending gift: %s", exc)
        await state.clear()
        if callback.message:
            await callback.message.answer(
                "❌ При отправке подарка произошла непредвиденная ошибка. Лимит не будет списан, если Telegram не подтвердил отправку. Попробуй позже.",
                reply_markup=main_menu(),
            )
        await callback.answer("Непредвиденная ошибка", show_alert=True)
        return

    if reservation_token:
        try:
            await db.complete_gift_send(callback.from_user.id, reservation_token)
        except Exception:
            # The reservation remains pending for a short lease, preventing an
            # immediate duplicate if Telegram accepted the gift but DB update failed.
            logger.exception("Gift sent but cooldown commit failed for user %s", callback.from_user.id)
            await state.clear()
            await callback.answer("Подарок отправлен; запись лимита задержалась", show_alert=True)
            if callback.message:
                await callback.message.answer(
                    "✅ Подарок отправлен, но не удалось записать лимит в базу данных. Во избежание повторной отправки попробуй снова не раньше чем через 5 минут.",
                    reply_markup=main_menu(),
                )
            return

    await state.clear()
    await callback.answer("Подарок отправлен")
    if callback.message:
        await callback.message.edit_text(
            "✅ <b>Подарок отправлен!</b>\n\n"
            f"Получатель: <b>@{html.escape(username)}</b> (<code>{recipient_id}</code>)\n"
            f"Стоимость: <b>{price} ⭐</b>.\n"
            "Отправлен обычный подарок.",
            reply_markup=back_button("main"),
        )


# ------------------------------
# Модуль 1. Бюджет
# ------------------------------

async def require_budget(message: Message, user_id: int) -> bool:
    """Ensure budget tools are used only after an explicit monthly budget is set."""
    budget = await db.get_budget(user_id)
    if budget is not None:
        return True
    await message.answer(
        "💰 Сначала добавь бюджет на этот месяц. После этого станут доступны расходы, "
        "статистика и подбор блюд. Открой «Мой бюджет» и нажми «Добавить бюджет на этот месяц».",
        reply_markup=budget_menu(budget_is_set=False),
    )
    return False

@dp.callback_query(F.data == "budget:add")
async def expense_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    if not callback.message:
        return
    if not await require_budget(callback.message, callback.from_user.id):
        return
    await state.set_state(ExpenseForm.amount)
    await callback.message.answer("Введи сумму расхода в рублях, например <code>349.50</code>.", reply_markup=cancel_keyboard())


@dp.message(ExpenseForm.amount)
async def expense_amount(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip().replace(" ", "").replace(",", ".")
    try:
        amount = float(raw)
        if not 0 < amount <= 1_000_000:
            raise ValueError
    except ValueError:
        await message.answer("Не получилось распознать сумму. Введи положительное число, например <code>349.50</code>.")
        return
    await state.update_data(amount=amount)
    await state.set_state(ExpenseForm.category)
    await message.answer(f"Расход: <b>{money(amount)}</b>. Теперь выбери категорию:", reply_markup=expense_categories())


@dp.callback_query(F.data.startswith("exp_cat:"))
async def expense_choose_category(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if await state.get_state() != ExpenseForm.category.state:
        if callback.message:
            await callback.message.answer("Сначала нажми «Добавить расход» в модуле бюджета.")
        return
    try:
        idx = int((callback.data or "").split(":")[1])
        category = EXPENSE_CATEGORIES[idx]
    except (ValueError, IndexError):
        if callback.message:
            await callback.message.answer("Не удалось выбрать категорию. Повтори ввод.")
        return
    if callback.message is None or not await require_budget(callback.message, callback.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    amount = float(data["amount"])
    await db.add_expense(callback.from_user.id, amount, category)
    await state.clear()
    stats = await db.monthly_expense_stats(callback.from_user.id)
    budget = await db.get_budget(callback.from_user.id)
    remaining_line = f"Остаток месячного бюджета: <b>{money(budget - stats['total'])}</b>" if budget is not None else ""
    if callback.message:
        await callback.message.edit_text(
            f"✅ Расход сохранён: <b>{money(amount)}</b> · {html.escape(category)}.\n\n"
            f"Потрачено за {stats['month']}: <b>{money(stats['total'])}</b>\n"
            f"{remaining_line}",
            reply_markup=budget_menu(budget_is_set=budget is not None),
        )


@dp.callback_query(F.data == "budget:set")
async def budget_set_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(BudgetForm.amount)
    current_budget = await db.get_budget(callback.from_user.id)
    await callback.answer()
    if callback.message:
        current_line = (
            f"Сейчас установлено: <b>{money(current_budget)}</b>.\n"
            if current_budget is not None
            else "Бюджет ещё не задан.\n"
        )
        await callback.message.answer(
            "➕ <b>Добавить бюджет на этот месяц</b>\n\n"
            f"{current_line}"
            "Введи сумму, которой планируешь располагать в этом месяце, например <code>32000</code>. "
            "Можно использовать копейки: <code>32500.50</code>.\n\n"
            "Сумма сохранится только для твоего Telegram-аккаунта.",
            reply_markup=cancel_keyboard(),
        )


@dp.message(BudgetForm.amount)
async def budget_set_save(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip().replace(" ", "").replace(",", ".")
    try:
        amount = float(raw)
        if not 0 < amount <= 100_000_000:
            raise ValueError
    except ValueError:
        await message.answer(
            "Введи положительный месячный бюджет числом, например <code>32000</code>. "
            "Максимум — 100 000 000 ₽.",
            reply_markup=cancel_keyboard(),
        )
        return

    await db.set_budget(message.from_user.id, amount)
    await state.clear()
    stats = await db.monthly_expense_stats(message.from_user.id)
    remaining = amount - stats["total"]
    await message.answer(
        f"✅ Твой месячный бюджет сохранён: <b>{money(amount)}</b>.\n"
        f"Уже потрачено в этом месяце: <b>{money(stats['total'])}</b>.\n"
        f"Остаток: <b>{money(remaining)}</b>.",
        reply_markup=budget_menu(budget_is_set=True),
    )


@dp.callback_query(F.data == "budget:stats")
async def expense_stats(callback: CallbackQuery) -> None:
    await callback.answer()
    budget = await db.get_budget(callback.from_user.id)
    if budget is None:
        if callback.message:
            await callback.message.edit_text(
                "💰 Бюджет на этот месяц ещё не задан. Сначала добавь сумму, чтобы видеть статистику.",
                reply_markup=budget_menu(budget_is_set=False),
            )
        return
    stats = await db.monthly_expense_stats(callback.from_user.id)
    remaining = budget - stats["total"]
    percent = (stats["total"] / budget * 100) if budget > 0 else 0
    lines = [
        f"<b>📊 Статистика за {stats['month']}</b>",
        f"Бюджет месяца: <b>{money(budget)}</b>",
        f"Потрачено: <b>{money(stats['total'])}</b> ({percent:.1f}%)",
        f"Остаток: <b>{money(remaining)}</b>",
        "",
        "<b>По категориям:</b>",
    ]
    if stats["by_category"]:
        for category, total in stats["by_category"]:
            share = (total / budget * 100) if budget > 0 else 0
            lines.append(f"• {html.escape(category)} — {money(total)} ({share:.1f}% бюджета)")
    else:
        lines.append("Пока нет расходов за этот месяц. Добавь первую трату через кнопку «Добавить расход».")
    lines.extend(["", "Это твой личный бюджет. Его можно изменить кнопкой «Установить месячный бюджет»; расчёт ведётся за календарный месяц."])
    if callback.message:
        await callback.message.edit_text("\n".join(lines), reply_markup=budget_menu())


@dp.callback_query(F.data == "budget:clear")
async def clear_expenses_start(callback: CallbackQuery) -> None:
    """Ask for confirmation before clearing current-month expenses."""
    await callback.answer()
    if not callback.message:
        return
    if not await require_budget(callback.message, callback.from_user.id):
        return

    stats = await db.monthly_expense_stats(callback.from_user.id)
    await callback.message.edit_text(
        "🗑 <b>Очистить траты за месяц?</b>\n\n"
        f"Сейчас учтено расходов: <b>{money(stats['total'])}</b>.\n"
        "Будут удалены только твои расходы за текущий календарный месяц. "
        "Месячный бюджет и расходы за предыдущие месяцы сохранятся.\n\n"
        "Это действие нельзя отменить.",
        reply_markup=clear_expenses_confirm_menu(),
    )


@dp.callback_query(F.data == "budget:clear:cancel")
async def clear_expenses_cancel(callback: CallbackQuery) -> None:
    await callback.answer("Очистка отменена")
    if not callback.message:
        return
    budget = await db.get_budget(callback.from_user.id)
    await callback.message.edit_text(
        "Очистка расходов отменена.",
        reply_markup=budget_menu(budget_is_set=budget is not None),
    )


@dp.callback_query(F.data == "budget:clear:confirm")
async def clear_expenses_confirm(callback: CallbackQuery) -> None:
    await callback.answer()
    if not callback.message:
        return
    budget = await db.get_budget(callback.from_user.id)
    if budget is None:
        await callback.message.edit_text(
            "💰 Сначала добавь бюджет на этот месяц.",
            reply_markup=budget_menu(budget_is_set=False),
        )
        return

    deleted_count = await db.clear_current_month_expenses(callback.from_user.id)
    stats = await db.monthly_expense_stats(callback.from_user.id)
    remaining = budget - stats["total"]
    await callback.message.edit_text(
        "✅ <b>Расходы за текущий месяц очищены.</b>\n\n"
        f"Удалено записей: <b>{deleted_count}</b>.\n"
        f"Потрачено за {stats['month']}: <b>{money(stats['total'])}</b>.\n"
        f"Остаток бюджета: <b>{money(remaining)}</b>.\n\n"
        "Теперь можешь заново добавлять расходы — статистика будет считаться с нуля.",
        reply_markup=budget_menu(budget_is_set=True),
    )


@dp.callback_query(F.data == "budget:food")
async def food_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    if not callback.message:
        return
    if not await require_budget(callback.message, callback.from_user.id):
        return
    await state.set_state(FoodForm.amount)
    await callback.message.answer("На какую сумму в рублях подобрать еду? Например: <code>200</code>.", reply_markup=cancel_keyboard())


def _valid_recipe_indices(raw: str, recipe_count: int) -> set[int]:
    """Parse index lists from callback data and reject invalid indices."""
    if not raw or raw == "-":
        return set()
    result: set[int] = set()
    for item in raw.split(","):
        try:
            idx = int(item)
        except ValueError:
            continue
        if 0 <= idx < recipe_count:
            result.add(idx)
    return result


def make_food_recommendation(
    amount: float,
    seen_indices: set[int] | None = None,
    previous_indices: set[int] | None = None,
) -> tuple[str, list[int], list[int]]:
    """Pick up to three varied recipes within the requested one-meal budget.

    Prices in data.py are rough per-portion estimates, not live supermarket prices.
    The refresh button excludes the recipes shown in the immediately previous list,
    when there are other candidates available.
    """
    previous = set(previous_indices or set())
    recipe_count = len(RECIPES)
    if recipe_count == 0:
        return "Список рецептов пуст. Проверь файл data.py.", [], []

    affordable = [i for i, recipe in enumerate(RECIPES) if float(recipe["cost"]) <= amount]
    over_budget = not affordable
    if affordable:
        pool = affordable
    else:
        pool = sorted(range(recipe_count), key=lambda i: float(RECIPES[i]["cost"]))[:min(5, recipe_count)]

    available = [i for i in pool if i not in previous]
    restarted_cycle = False
    if not available:
        available = list(pool)
        restarted_cycle = True

    selected = random.sample(available, k=min(3, len(available)))
    selected = sorted(selected, key=lambda i: float(RECIPES[i]["cost"]))

    parts: list[str] = []
    if restarted_cycle:
        parts.append("🔄 <i>Все подходящие варианты уже были в предыдущем списке — подборка обновлена заново.</i>")
    if over_budget:
        parts.append(
            f"⚠️ На сумму до <b>{money(amount)}</b> пока нет рецептов в нашей подборке. "
            "Ниже — самые дешёвые варианты; их примерная стоимость может превысить лимит."
        )
    else:
        parts.append(f"<b>🍲 Идеи блюд до {money(amount)}</b>")

    for idx in selected:
        recipe = RECIPES[idx]
        parts.append(
            f"\n<b>{html.escape(recipe['name'])}</b> — примерно <b>{money(float(recipe['cost']))}</b> за порцию\n"
            f"Продукты: {html.escape(recipe['ingredients'])}.\n"
            f"Приготовление: {html.escape(recipe['steps'])}"
        )
    parts.append(
        "\n<i>Цены ориентировочные: это оценка стоимости использованного количества продуктов в одной порции, "
        "а не цены целых упаковок и не онлайн-сравнение магазинов. Они могут отличаться в зависимости от города, магазина и сезона.</i>"
    )
    if len(pool) <= 3:
        parts.append("\n<i>Для этого бюджета в подборке мало вариантов, поэтому при обновлении некоторые блюда могут повториться.</i>")
    return "\n".join(parts), selected, selected


@dp.message(FoodForm.amount)
async def food_amount(message: Message, state: FSMContext) -> None:
    if not await require_budget(message, message.from_user.id):
        await state.clear()
        return
    raw = (message.text or "").strip().replace(" ", "").replace(",", ".")
    try:
        amount = float(raw)
        if not 0 < amount <= 100_000:
            raise ValueError
    except ValueError:
        await message.answer("Введи положительную сумму числом, например <code>200</code>.")
        return

    await state.clear()
    text, seen_indices, shown_indices = make_food_recommendation(amount)
    await message.answer(
        text,
        reply_markup=food_recipes_menu(amount, seen_indices, shown_indices),
    )


@dp.callback_query(F.data.startswith("food:refresh:"))
async def food_refresh(callback: CallbackQuery) -> None:
    await callback.answer()
    if not callback.message or not callback.data:
        return
    if not await require_budget(callback.message, callback.from_user.id):
        return
    try:
        _, _, amount_raw, previous_raw = callback.data.split(":", 3)
        amount = float(amount_raw)
        if not 0 < amount <= 100_000:
            raise ValueError
        previous_indices = _valid_recipe_indices(previous_raw, len(RECIPES))
    except (ValueError, TypeError):
        await callback.message.answer("Не удалось обновить подборку. Нажми «Что поесть на сумму» ещё раз.")
        return

    text, updated_seen, shown_indices = make_food_recommendation(
        amount,
        previous_indices=previous_indices,
    )
    await callback.message.edit_text(
        text,
        reply_markup=food_recipes_menu(amount, updated_seen, shown_indices),
    )


# ------------------------------
# Модуль 2. Путь в Backend
# ------------------------------

@dp.callback_query(F.data == "learning:topics")
async def topic_list(callback: CallbackQuery) -> None:
    await callback.answer()
    completed = await db.get_completed_topics(callback.from_user.id)
    if callback.message:
        await callback.message.edit_text(
            "<b>✅ Отметить пройденную тему</b>\n\nНажми на тему, чтобы переключить статус. Рекомендация «Что учить сегодня» всегда выбирает первую незакрытую тему по порядку.",
            reply_markup=topics_keyboard(completed),
        )


@dp.callback_query(F.data.startswith("topic:"))
async def topic_toggle(callback: CallbackQuery) -> None:
    await callback.answer()
    try:
        idx = int((callback.data or "").split(":")[1])
        topic = ROADMAP[idx]
    except (ValueError, IndexError):
        if callback.message:
            await callback.message.answer("Не удалось определить тему.")
        return
    completed = await db.get_completed_topics(callback.from_user.id)
    new_status = topic not in completed
    await db.toggle_topic(callback.from_user.id, topic, completed=new_status)
    completed = await db.get_completed_topics(callback.from_user.id)
    streak = await db.get_streak(callback.from_user.id)
    if callback.message:
        status_text = "отмечена пройденной ✅" if new_status else "возвращена в план ⬜️"
        await callback.message.edit_text(
            f"Тема <b>{html.escape(topic)}</b> {status_text}.\n\n🔥 Текущий streak: <b>{streak} дн.</b>",
            reply_markup=topics_keyboard(completed),
        )


@dp.callback_query(F.data == "learning:today")
async def what_to_learn_today(callback: CallbackQuery) -> None:
    await callback.answer()
    completed = await db.get_completed_topics(callback.from_user.id)
    topic = next((item for item in ROADMAP if item not in completed), None)
    if topic is None:
        text = "✅ Все темы в текущем роадмапе отмечены. Следующий шаг — углублять специализацию, решать задачи, разбирать чужие проекты и набирать опыт на реальных задачах."
    else:
        idx = ROADMAP.index(topic)
        text = (
            f"<b>🎯 Что учить сегодня</b>\n\n"
            f"Этап {idx + 1} из {len(ROADMAP)}: <b>{html.escape(topic)}</b>\n\n"
            f"{html.escape(TOPIC_GUIDES[topic])}\n\n"
            "Когда позанимаешься, открой «Отметить пройденную тему». Отмечай тему только после практики, а не после одного просмотра видео."
        )
    if callback.message:
        await callback.message.edit_text(text, reply_markup=learning_menu())


@dp.callback_query(F.data == "learning:task")
async def python_task(callback: CallbackQuery) -> None:
    await callback.answer()
    today = moscow_now().date()
    task_title, task_description = PYTHON_TASKS[today.toordinal() % len(PYTHON_TASKS)]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Задачу сделал", callback_data="learning:taskdone")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:learning")],
    ])
    if callback.message:
        await callback.message.edit_text(
            f"<b>🧩 Задача дня по Python</b> · {today.strftime('%d.%m.%Y')}\n\n"
            f"<b>{html.escape(task_title)}</b>\n{html.escape(task_description)}\n\n"
            "Решение сначала попробуй написать сам. Если застрял — разбей задачу на маленькие шаги и проверь граничные случаи.",
            reply_markup=kb,
        )


@dp.callback_query(F.data == "learning:taskdone")
async def python_task_done(callback: CallbackQuery) -> None:
    await callback.answer("Учебная активность записана")
    await db.record_study_activity(callback.from_user.id)
    streak = await db.get_streak(callback.from_user.id)
    if callback.message:
        await callback.message.edit_text(f"✅ Учебная активность за сегодня отмечена.\n\n🔥 Текущий streak: <b>{streak} дн.</b>", reply_markup=learning_menu())


@dp.callback_query(F.data == "learning:progress")
async def learning_progress(callback: CallbackQuery) -> None:
    await callback.answer()
    completed = await db.get_completed_topics(callback.from_user.id)
    groups = [
        ("Junior · основы", ROADMAP[:8]),
        ("Middle · инструменты и эксплуатация", ROADMAP[8:12]),
        ("Senior · архитектура и production", ROADMAP[12:]),
    ]
    lines = ["<b>📈 Прогресс по дорожной карте</b>", ""]
    for label, topics in groups:
        done = sum(1 for t in topics if t in completed)
        pct = int(done / len(topics) * 100) if topics else 0
        bar = "🟩" * (pct // 10) + "⬜️" * (10 - pct // 10)
        lines.append(f"<b>{label}</b>: {done}/{len(topics)} · {pct}%\n{bar}")
    lines.append(f"\nВсего отмечено: <b>{len(completed)}/{len(ROADMAP)}</b> тем.")
    lines.append("\n<i>Это прогресс чек-листа, а не объективная оценка уровня. Junior/Middle/Senior зависят от качества навыков, практики, самостоятельности и опыта в реальных проектах.</i>")
    if callback.message:
        await callback.message.edit_text("\n".join(lines), reply_markup=learning_menu())


@dp.callback_query(F.data == "learning:streak")
async def learning_streak(callback: CallbackQuery) -> None:
    await callback.answer()
    streak = await db.get_streak(callback.from_user.id)
    completed = await db.get_completed_topics(callback.from_user.id)
    if callback.message:
        await callback.message.edit_text(
            f"<b>🔥 Streak-система</b>\n\nТекущая серия дней с учебной активностью: <b>{streak}</b>.\n"
            f"Отмечено тем: <b>{len(completed)}/{len(ROADMAP)}</b>.\n\n"
            "Серия засчитывается за день, когда ты отметил завершённую тему или нажал «Задачу сделал». Достаточно одной такой активности в сутки по московскому времени.",
            reply_markup=learning_menu(),
        )


# ------------------------------
# Модуль 3. МАИ-хелпер
# ------------------------------

@dp.callback_query(F.data == "study:schedule")
async def show_schedule(callback: CallbackQuery) -> None:
    await callback.answer()
    lessons = await db.get_lessons(callback.from_user.id)
    lines = ["<b>📅 Моё расписание</b>", ""]
    if not lessons:
        lines.append("Расписание пока пустое. Добавь пары вручную — например: <code>Пн 09:00 Математика; ауд. 312</code>.")
    else:
        for weekday in range(7):
            day_lessons = [lesson for lesson in lessons if int(lesson["weekday"]) == weekday]
            if day_lessons:
                lines.append(f"<b>{WEEKDAY_NAMES[weekday]}</b>")
                for lesson in day_lessons:
                    room = f" · {html.escape(str(lesson['room']))}" if lesson["room"] else ""
                    lines.append(f"{html.escape(str(lesson['start_time']))} — {html.escape(str(lesson['title']))}{room}")
                lines.append("")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить пару", callback_data="study:addlesson")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:study")],
    ])
    if callback.message:
        await callback.message.edit_text("\n".join(lines), reply_markup=kb)


@dp.callback_query(F.data == "study:addlesson")
async def add_lesson_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(LessonForm.details)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "Введи пару в формате:\n<code>Пн 09:00 Математика; ауд. 312</code>\n\n"
            "Дни: Пн, Вт, Ср, Чт, Пт, Сб, Вс. Время — московское/университетское, только для подписи.",
            reply_markup=cancel_keyboard(),
        )


@dp.message(LessonForm.details)
async def add_lesson_save(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    parts = raw.split(maxsplit=2)
    if len(parts) < 3 or parts[0] not in WEEKDAYS or not re.fullmatch(r"\d{1,2}:\d{2}", parts[1]):
        await message.answer("Формат не распознан. Пример: <code>Вт 11:30 Физика; ауд. 204</code>.")
        return
    try:
        hour, minute = map(int, parts[1].split(":"))
        if hour > 23 or minute > 59:
            raise ValueError
    except ValueError:
        await message.answer("Укажи корректное время в формате ЧЧ:ММ, например <code>09:00</code>.")
        return
    rest = parts[2].strip()
    if ";" in rest:
        title, room = [x.strip() for x in rest.split(";", maxsplit=1)]
    else:
        match = re.match(r"(.+?)(?:,\s*(ауд\.?\s*.+|каб\.?\s*.+))?$", rest, flags=re.IGNORECASE)
        title = (match.group(1) if match else rest).strip()
        room = (match.group(2) or "").strip() if match else ""
    if not title:
        await message.answer("Добавь название пары.")
        return
    await db.add_lesson(message.from_user.id, WEEKDAYS[parts[0]], f"{hour:02d}:{minute:02d}", title, room)
    await state.clear()
    await message.answer(f"✅ Добавлено: <b>{parts[0]} {hour:02d}:{minute:02d}</b> — {html.escape(title)}{(' · ' + html.escape(room)) if room else ''}.", reply_markup=study_menu())


@dp.callback_query(F.data == "study:deadlines")
async def show_deadlines(callback: CallbackQuery) -> None:
    await callback.answer()
    deadlines = await db.get_deadlines(callback.from_user.id)
    today = moscow_now().date()
    lines = ["<b>🗓 Дедлайны</b>", ""]
    if not deadlines:
        lines.append("Пока нет дедлайнов. Добавь первый в формате <code>ДД.ММ.ГГГГ Название</code>.")
    else:
        for item in deadlines:
            deadline_date = date.fromisoformat(str(item["deadline_date"]))
            delta = (deadline_date - today).days
            if delta > 0:
                status = f"осталось {delta} дн."
            elif delta == 0:
                status = "сегодня"
            else:
                status = f"просрочено на {-delta} дн."
            lines.append(f"• <b>{deadline_date.strftime('%d.%m.%Y')}</b> — {html.escape(str(item['title']))} <i>({status})</i>")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить дедлайн", callback_data="study:adddeadline")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:study")],
    ])
    if callback.message:
        await callback.message.edit_text("\n".join(lines), reply_markup=kb)


@dp.callback_query(F.data == "study:adddeadline")
async def add_deadline_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(DeadlineForm.details)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "Введи дату и название дедлайна:\n<code>25.12.2026 Курсовая по конструкции самолёта</code>",
            reply_markup=cancel_keyboard(),
        )


@dp.message(DeadlineForm.details)
async def add_deadline_save(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    parts = raw.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Нужны дата и название. Пример: <code>25.12.2026 Лабораторная</code>.")
        return
    try:
        deadline_date = datetime.strptime(parts[0], "%d.%m.%Y").date()
    except ValueError:
        await message.answer(date_input_help() + " После даты добавь название дедлайна.")
        return
    await db.add_deadline(message.from_user.id, parts[1].strip(), deadline_date)
    await state.clear()
    delta = (deadline_date - moscow_now().date()).days
    await message.answer(f"✅ Дедлайн сохранён: <b>{deadline_date.strftime('%d.%m.%Y')}</b> — {html.escape(parts[1].strip())}.\nДо него: <b>{delta} дн.</b>", reply_markup=study_menu())


@dp.callback_query(F.data == "study:session")
async def show_session_countdown(callback: CallbackQuery) -> None:
    await callback.answer()
    session_date = await db.get_session_date(callback.from_user.id)
    if session_date is None:
        text = "Дата сессии ещё не установлена. Нажми «Установить дату сессии» и введи дату."
    else:
        delta = (session_date - moscow_now().date()).days
        if delta > 0:
            text = f"До указанной даты сессии <b>{session_date.strftime('%d.%m.%Y')}</b> осталось <b>{delta} дн.</b> 📚"
        elif delta == 0:
            text = f"Указанная дата сессии — <b>сегодня, {session_date.strftime('%d.%m.%Y')}</b>. Удачи!"
        else:
            text = f"Указанная дата сессии <b>{session_date.strftime('%d.%m.%Y')}</b> прошла {-delta} дн. назад. Обнови дату, когда будет известно расписание сессии."
    if callback.message:
        await callback.message.edit_text(text, reply_markup=study_menu())


@dp.callback_query(F.data == "study:setsession")
async def set_session_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(SessionForm.session_date)
    await callback.answer()
    if callback.message:
        await callback.message.answer("Введи дату начала сессии в формате <code>ДД.ММ.ГГГГ</code>.", reply_markup=cancel_keyboard())


@dp.message(SessionForm.session_date)
async def set_session_save(message: Message, state: FSMContext) -> None:
    try:
        session_date = datetime.strptime((message.text or "").strip(), "%d.%m.%Y").date()
    except ValueError:
        await message.answer(date_input_help())
        return
    await db.set_session_date(message.from_user.id, session_date)
    await state.clear()
    delta = (session_date - moscow_now().date()).days
    await message.answer(f"✅ Дата сохранена: <b>{session_date.strftime('%d.%m.%Y')}</b>.\nДо неё: <b>{delta} дн.</b>", reply_markup=study_menu())


# ------------------------------
# Модуль 4. Работа и фриланс
# ------------------------------

async def send_hh_results(message: Message, query: str, internship: bool = False) -> None:
    try:
        vacancies = await search_hh_vacancies(query=query, internship=internship, limit=5)
    except Exception as exc:
        logger.warning("HH API failed: %s", exc)
        vacancies = []
    if not vacancies:
        text = (
            "Не удалось получить выдачу через API (сервис может временно ограничивать анонимные запросы).\n\n"
            "Открой поиск на hh.ru и проверь фильтры вручную: «без опыта», подходящий график и реальную зарплату."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Открыть поиск на hh.ru", url=CAREER_LINKS["hh_intern" if internship else "hh_python"][1])],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:career")],
        ])
        await message.answer(text, reply_markup=kb)
        return
    lines = ["<b>🎓 Найденные вакансии на hh.ru</b>\n<i>Проверь условия, график, требования и формат оплаты в каждой вакансии.</i>\n"]
    for idx, item in enumerate(vacancies, 1):
        title = html.escape(item["name"])
        url = html.escape(item["url"], quote=True)
        employer = html.escape(item["employer"])
        salary = html.escape(item["salary"])
        lines.append(f"{idx}. <a href=\"{url}\"><b>{title}</b></a>\n{employer} · {salary}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Открыть больше вакансий", url=CAREER_LINKS["hh_intern" if internship else "hh_python"][1])],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:career")],
    ])
    await message.answer("\n\n".join(lines), reply_markup=kb, disable_web_page_preview=True)


@dp.callback_query(F.data == "career:python")
async def career_python(callback: CallbackQuery) -> None:
    await callback.answer("Ищу вакансии…")
    if callback.message:
        await callback.message.answer("Ищу удалённые вакансии Python без опыта на hh.ru…")
        await send_hh_results(callback.message, query="Python", internship=False)


@dp.callback_query(F.data == "career:freelance")
async def career_freelance(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            "<b>💻 Фриланс-площадки</b>\n\n"
            "На старте ищи небольшие и понятные задачи: привести данные в таблице в порядок, написать простой скрипт автоматизации, обработать CSV/JSON, оформить технический рисунок или сделать базовую настройку сайта.\n\n"
            "Не плати за «доступ к заказам» неизвестным посредникам и заранее согласуй объём работ.",
            reply_markup=freelance_links(),
        )


@dp.callback_query(F.data == "career:internships")
async def career_internships(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            "<b>🎓 IT-стажировки</b>\n\nСмотри не только Python-разработку, но и QA, автоматизацию, техническую поддержку, инженерное ПО и стажировки для студентов. Проверяй дату публикации, требования к курсу обучения и наличие оплаты.",
            reply_markup=internship_links(),
        )


@dp.callback_query(F.data == "career:resume")
async def career_resume(callback: CallbackQuery) -> None:
    await callback.answer()
    resume = (
        "<b>📄 Черновик резюме начинающего разработчика</b>\n\n"
        "<b>Цель:</b> стажёр / junior Python developer / технический специалист с автоматизацией.\n"
        "<b>О себе:</b> студент МАИ, направление самолётостроения. Изучаю Python и backend-разработку; интересуюсь обработкой данных и автоматизацией инженерных задач.\n"
        "<b>Навыки:</b> Python (базовый синтаксис), Git (если уже освоен), SQL (если изучен), HTTP/API (если изучены). Оставь только то, что можешь объяснить на собеседовании.\n"
        "<b>Проекты:</b> добавь своего Telegram-бота: задачи, технологии, что реализовано, ссылка на GitHub. Не приписывай навыки, которых пока нет.\n"
        "<b>Образование:</b> МАИ, самолётостроение, курс/год обучения.\n"
        "<b>Ссылки:</b> GitHub, контакты, при наличии — демо проекта.\n\n"
        "<b>Мини-чек-лист:</b> без опечаток; 1 страница; конкретные проекты вместо общих слов; у каждой технологии есть подтверждение в коде."
    )
    if callback.message:
        await callback.message.edit_text(resume, reply_markup=back_to_career_keyboard())


@dp.callback_query(F.data == "career:micro")
async def career_micro(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.edit_text(
            "<b>💡 Идеи для первого заработка</b>\n\n"
            "• Репетиторство по школьной математике/информатике, если хорошо знаешь тему.\n"
            "• Оформление и проверка технических чертежей по требованиям заказчика.\n"
            "• Простая автоматизация таблиц, переименование файлов, обработка CSV/JSON.\n"
            "• Настройка компьютеров и помощь с базовыми программами.\n"
            "• Транскрибация, перенос данных, простые задачи на фриланс-биржах.\n"
            "• Небольшие инженерные/IT-проекты для портфолио и студенческих команд.\n\n"
            "Лучше не брать заказы, где нужно сдавать учебную работу за другого студента: это может нарушать правила вуза и плохо развивает твои навыки.",
            reply_markup=back_to_career_keyboard(),
        )


@dp.callback_query(F.data == "career:aero")
async def career_aero(callback: CallbackQuery) -> None:
    await callback.answer("Ищу вакансии…")
    if callback.message:
        await callback.message.answer(
            "<b>✈️ Авиация + IT</b>\n\nИдеи ключевых слов: инженер-программист, Python, автоматизация испытаний, обработка телеметрии, CAE/CAD, моделирование, тестирование ПО, обработка измерений.\n"
            "Попробую получить несколько вакансий через hh.ru; из-за узкого запроса результатов может быть мало."
        )
        try:
            vacancies = await search_hh_vacancies(query="Python инженер автоматизация", internship=True, limit=5)
        except Exception as exc:
            logger.warning("HH aviation search failed: %s", exc)
            vacancies = []
        if vacancies:
            lines = ["<b>Подходящие результаты поиска:</b>"]
            for item in vacancies:
                lines.append(f"• <a href=\"{html.escape(item['url'], quote=True)}\">{html.escape(item['name'])}</a> — {html.escape(item['salary'])}")
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Поиск авиа-IT на hh.ru", url=CAREER_LINKS["aero"][1])],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:career")],
            ])
            await callback.message.answer("\n".join(lines), reply_markup=kb, disable_web_page_preview=True)
        else:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Открыть поиск авиа-IT", url=CAREER_LINKS["aero"][1])],
                [InlineKeyboardButton(text="Центр карьеры МАИ", url=CAREER_LINKS["mai"][1])],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:career")],
            ])
            await callback.message.answer("Через API выдача не получена. Открой поиск по инженерной автоматизации и проверь, какие требования подходят студенту.", reply_markup=kb)


# ------------------------------
# Модуль 5. Погода и автобус с396
# ------------------------------

@dp.callback_query(F.data.in_({"local:weather", "local:weather_change"}))
async def local_weather_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not callback.message:
        return

    force_new_location = callback.data == "local:weather_change"
    saved_location = None if force_new_location else await db.get_weather_location(callback.from_user.id)

    if not saved_location:
        await state.clear()
        await state.set_state(LocalWeatherForm.location)
        await callback.message.edit_text(
            "<b>🌦 Погода по твоему району</b>\n\n"
            "Район пока не задан. Введи название района, города или населённого пункта, "
            "в котором хочешь смотреть погоду. Например: <code>Митино, Москва</code>, "
            "<code>Химки</code> или <code>Казань</code>.\n\n"
            "Если небольшое название не находится, добавь через запятую ближайший город или регион.",
            reply_markup=cancel_keyboard(),
        )
        return

    try:
        weather = await get_weather(saved_location)
    except LocationNotFoundError:
        await state.clear()
        await state.set_state(LocalWeatherForm.location)
        await callback.message.edit_text(
            f"Не удалось снова найти сохранённое место «{html.escape(saved_location)}».\n"
            "Введи район или город заново, чтобы обновить сохранённое местоположение.",
            reply_markup=cancel_keyboard(),
        )
        return
    except Exception as exc:
        logger.warning("Weather refresh failed for saved place %r: %s", saved_location, exc)
        await callback.message.edit_text(
            "⚠️ Не удалось получить погоду: сервис может быть временно недоступен. "
            "Попробуй ещё раз позже или выбери другой район.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Повторить", callback_data="local:weather")],
                [InlineKeyboardButton(text="📍 Изменить район", callback_data="local:weather_change")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:local")],
            ]),
        )
        return

    weather_text, weather_kb = _build_weather_card(weather)
    await callback.message.edit_text(weather_text, reply_markup=weather_kb, disable_web_page_preview=True)


@dp.message(LocalWeatherForm.location)
async def local_weather_lookup(message: Message, state: FSMContext) -> None:
    query = (message.text or "").strip()
    if len(query) < 2:
        await message.answer("Название слишком короткое. Введи район или город ещё раз, например <code>Митино, Москва</code>.")
        return

    try:
        weather = await get_weather(query)
    except LocationNotFoundError:
        await message.answer(
            f"❌ Не удалось найти место «{html.escape(query)}».\n\n"
            "Проверь написание и введи название заново. Для небольшого района добавь город через запятую, "
            "например <code>Митино, Москва</code>.",
            reply_markup=cancel_keyboard(),
        )
        return
    except Exception as exc:
        logger.warning("Weather lookup failed for %r: %s", query, exc)
        await message.answer(
            "⚠️ Не удалось получить данные о погоде: сервис может быть временно недоступен. "
            "Попробуй отправить название ещё раз чуть позже или нажми «Отмена».",
            reply_markup=cancel_keyboard(),
        )
        return

    # Save only after geocoding and weather retrieval succeeded; invalid input
    # never overwrites the user's previous location.
    await db.set_weather_location(message.from_user.id, query)
    await state.clear()
    weather_text, weather_kb = _build_weather_card(weather)
    await message.answer(weather_text, reply_markup=weather_kb, disable_web_page_preview=True)


def _build_weather_card(weather: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    """Build a response using only the explicit/saved location chosen by user."""
    temp = weather["temperature"]
    feels = weather["feels_like"]
    time_text = str(weather["time"]).replace("T", " ")
    timezone_name = weather.get("timezone") or "местное время"
    location_name = html.escape(str(weather["location"]))
    lat = float(weather["latitude"])
    lon = float(weather["longitude"])
    map_url = f"https://www.openstreetmap.org/?mlat={lat:.5f}&mlon={lon:.5f}#map=12/{lat:.5f}/{lon:.5f}"
    text = (
        f"<b>🌦 Погода: {location_name}</b>\n\n"
        f"Сейчас: <b>{temp:+g}°C</b>, {html.escape(str(weather['description']))}\n"
        f"Ощущается как: {feels:+g}°C\n"
        f"Влажность: {weather['humidity']}%\n"
        f"Осадки: {weather['precipitation']} мм\n"
        f"Ветер: {weather['wind']} км/ч\n\n"
        f"<i>Время данных: {html.escape(time_text)} · Часовой пояс: {html.escape(str(timezone_name))}. "
        "Источник погоды: Open-Meteo; координаты места найдены по географическому справочнику.</i>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗺 Открыть место на карте", url=map_url)],
        [InlineKeyboardButton(text="🔄 Обновить погоду", callback_data="local:weather")],
        [InlineKeyboardButton(text="📍 Изменить район", callback_data="local:weather_change")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:local")],
    ])
    return text, kb


def parse_clock(value: str) -> tuple[int, int] | None:
    value = clean_time(value)
    if not re.fullmatch(r"\d{1,2}:\d{2}", value):
        return None
    h, m = map(int, value.split(":"))
    if h > 23 or m > 59:
        return None
    return h, m


def compute_next_bus(target: datetime) -> dict[str, Any]:
    """Find next timetable trip at/after target; timetable is not real-time GPS."""
    choices: list[dict[str, Any]] = []
    now = moscow_now()
    for departure, arrival in zip(BUS_SCHEDULE["departures"], BUS_SCHEDULE["arrivals"]):
        dh, dm = map(int, departure.split(":"))
        ah, am = map(int, arrival.split(":"))
        dep = target.replace(hour=dh, minute=dm, second=0, microsecond=0)
        while dep < target:
            dep += timedelta(days=1)
        arr = dep.replace(hour=ah, minute=am)
        if arr < dep:
            arr += timedelta(days=1)
        wait = int((dep - now).total_seconds() // 60)
        until_arrival = int((arr - now).total_seconds() // 60)
        ride_minutes = int((arr - dep).total_seconds() // 60)
        choices.append({
            "departure": dep,
            "arrival": arr,
            "wait_minutes": wait,
            "until_arrival_minutes": until_arrival,
            "ride_minutes": ride_minutes,
        })
    valid = [c for c in choices if c["departure"] >= target]
    if valid:
        return min(valid, key=lambda c: c["departure"])
    # Should normally not happen; next service day starts at the first timetable departure.
    return min(choices, key=lambda c: c["departure"])


def human_minutes(minutes: int) -> str:
    if minutes < 0:
        return "время прошло"
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours} ч {mins} мин"
    if hours:
        return f"{hours} ч"
    return f"{mins} мин"


async def send_bus_result(message: Message, target: datetime, custom: bool = False) -> None:
    trip = compute_next_bus(target)
    dep = trip["departure"]
    arr = trip["arrival"]
    timing = (
        f"Отправление с Планерной: <b>{dep.strftime('%H:%M')} МСК</b>\n"
        f"Остановка «{html.escape(BUS_SCHEDULE['destination'])}»: <b>{arr.strftime('%H:%M')} МСК</b>\n"
        f"Расчётная поездка: <b>{trip['ride_minutes']} мин.</b>"
    )
    if not custom:
        timing += (
            f"\nДо отправления по расписанию: <b>{human_minutes(max(0, trip['wait_minutes']))}</b>"
            f"\nДо расчётного прибытия от текущего момента: <b>{human_minutes(max(0, trip['until_arrival_minutes']))}</b>"
        )
    else:
        now = moscow_now()
        if dep > now:
            timing += f"\nОт текущего момента до этого отправления: <b>{human_minutes(int((dep - now).total_seconds() // 60))}</b>"
        else:
            timing += "\n<i>Указанное время уже прошло; показан плановый рейс для выбранного времени/следующего совпадения расписания.</i>"
    text = (
        "<b>🚌 Автобус с396: Планерная → «Химико-технологический университет»</b>\n\n"
        + timing + "\n\n"
        + f"<i>{html.escape(BUS_SCHEDULE['note'])}</i>\n"
        + f"Источник расписания: <a href=\"{html.escape(BUS_SCHEDULE['source_url'], quote=True)}\">{html.escape(BUS_SCHEDULE['source_name'])}</a>."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📍 Проверить движение в Яндекс Картах", url=BUS_SCHEDULE["yandex_live_url"])],
        [InlineKeyboardButton(text="🕒 Указать другое время", callback_data="bus:custom")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:local")],
    ])
    await message.answer(text, reply_markup=kb, disable_web_page_preview=True)


async def start_bus_tracker_setup(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(BusTrackerForm.route_number)
    await message.answer(
        "<b>🚌 Настройка отслеживания автобуса</b>\n\n"
        "1/3. Введи номер маршрута, например <code>с396</code> или <code>733к</code>.\n\n"
        "После этого укажешь остановку посадки и остановку, которая определяет нужное направление. Важно: ссылка на остановку должна вести именно на нужную платформу; название второй остановки сохраняется как подсказка направления.",
        reply_markup=cancel_keyboard(),
    )


async def show_live_bus_tracker(message: Message, user_id: int) -> None:
    settings = await db.get_bus_tracker(user_id)
    if not settings:
        # A message can be edited in callback flow, but the setup always needs a normal message.
        await message.answer("Персональный маршрут ещё не настроен. Нажми «Настроить автобус».", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚌 Настроить автобус", callback_data="bus:setup")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:local")],
        ]))
        return

    try:
        result = await get_stop_forecast(settings["boarding_stop_id"], settings["route_number"])
    except BusTrackerError as exc:
        logger.warning("Live bus forecast unavailable for user_id=%s route=%s: %s", user_id, settings["route_number"], exc)
        text = (
            "<b>🚌 Не удалось получить актуальный прогноз</b>\n\n"
            f"Маршрут: <b>{html.escape(settings['route_number'])}</b>\n"
            f"Остановка: <b>{html.escape(settings['boarding_stop_name'])}</b>\n"
            f"Подсказка направления: <b>{html.escape(settings['direction_stop_name'])}</b>\n\n"
            f"{html.escape(str(exc))}\n\n"
            "Время по телеметрии сейчас не подтверждено. Можно повторить запрос или открыть официальное табло/карту. "
            "Справочное расписание в файле проекта содержит только маршрут с396 между Планерной и «Химико-технологическим университетом», "
            "поэтому для произвольной остановки я не буду придумывать ETA."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Повторить запрос", callback_data="bus:live")],
            [InlineKeyboardButton(text="🗺 Открыть Московский транспорт", url="https://moscowtransport.app/")],
            [InlineKeyboardButton(text="🕒 Открыть справочное расписание с396", callback_data="bus:schedule")],
            [InlineKeyboardButton(text="✏️ Изменить маршрут/остановку", callback_data="bus:setup")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:local")],
        ])
        await message.answer(text, reply_markup=kb, disable_web_page_preview=True)
        return

    lines = [
        "<b>🚌 Прогноз прибытия</b>",
        f"Маршрут: <b>{html.escape(settings['route_number'])}</b>",
        f"Остановка: <b>{html.escape(result['stop_name'])}</b>",
        f"Подсказка направления: <b>{html.escape(settings['direction_stop_name'])}</b>",
        "",
    ]
    for forecast in result["forecasts"][:3]:
        source_label = "телеметрия" if forecast["realtime"] else "расписание источника"
        lines.append(
            f"• <b>{forecast['arrival'].strftime('%H:%M')} МСК</b> — примерно через "
            f"<b>{human_minutes(forecast['minutes'])}</b> ({source_label})"
        )
    if any(item["realtime"] for item in result["forecasts"][:3]):
        lines.extend(["", "<i>Есть прогнозы, основанные на телеметрии. Это всё равно прогноз, а не гарантия точного времени.</i>"])
    else:
        lines.extend(["", "<i>Источник вернул расписание, но не подтвердил прибытие по телеметрии.</i>"])
    lines.append("Источник: табло «Московского Транспорта» (неофициальное подключение к API).")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить прогноз", callback_data="bus:live")],
        [InlineKeyboardButton(text="✏️ Изменить маршрут/остановку", callback_data="bus:setup")],
        [InlineKeyboardButton(text="🗺 Открыть Московский транспорт", url="https://moscowtransport.app/")],
        [InlineKeyboardButton(text="🗑 Удалить сохранённый маршрут", callback_data="bus:clear")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:local")],
    ])
    await message.answer("\n".join(lines), reply_markup=kb, disable_web_page_preview=True)


@dp.callback_query(F.data == "bus:now")
async def bus_now(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not callback.message:
        return
    settings = await db.get_bus_tracker(callback.from_user.id)
    if not settings:
        await start_bus_tracker_setup(callback.message, state)
        return
    await show_live_bus_tracker(callback.message, callback.from_user.id)


@dp.callback_query(F.data.in_({"bus:setup", "bus:edit"}))
async def bus_tracker_setup_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if callback.message:
        await start_bus_tracker_setup(callback.message, state)


@dp.message(BusTrackerForm.route_number)
async def bus_tracker_route(message: Message, state: FSMContext) -> None:
    route = " ".join((message.text or "").split()).strip()
    if not route or len(route) > 20 or not re.fullmatch(r"[0-9A-Za-zА-Яа-яЁё№ -]+", route):
        await message.answer("Введи номер маршрута без лишнего текста, например <code>с396</code>.")
        return
    await state.update_data(route_number=route)
    await state.set_state(BusTrackerForm.boarding_stop)
    await message.answer(
        "2/3. Введи название остановки, где садишься. Если знаешь ссылку на эту остановку в приложении «Московский транспорт», можешь отправить её сразу.\n\n"
        "Если отправишь только название, на следующем шаге бот попросит ссылку, чтобы однозначно определить остановочную платформу.",
        reply_markup=cancel_keyboard(),
    )


@dp.message(BusTrackerForm.boarding_stop)
async def bus_tracker_boarding_stop(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if len(value) < 2 or len(value) > 300:
        await message.answer("Введи название остановки или ссылку из приложения «Московский транспорт».")
        return
    stop_id = await resolve_stop_uuid(value)
    stop_name = value if not value.startswith(("http://", "https://")) else "Остановка по ссылке"
    await state.update_data(boarding_stop_name=stop_name, boarding_stop_id=stop_id or "")
    await state.set_state(BusTrackerForm.direction_stop)
    await message.answer(
        "3/3. Введи название остановки, по которой можно определить нужное направление (например, конечную или промежуточную остановку в сторону поездки).",
        reply_markup=cancel_keyboard(),
    )


@dp.message(BusTrackerForm.direction_stop)
async def bus_tracker_direction_stop(message: Message, state: FSMContext) -> None:
    direction_stop = " ".join((message.text or "").split()).strip()
    if len(direction_stop) < 2 or len(direction_stop) > 200:
        await message.answer("Введи название остановки направления, не короче двух символов.")
        return
    await state.update_data(direction_stop_name=direction_stop)
    data = await state.get_data()
    if not data.get("boarding_stop_id"):
        await state.set_state(BusTrackerForm.boarding_stop_link)
        await message.answer(
            "Чтобы получать прогноз, нужно однозначно определить остановку: одного названия недостаточно, потому что рядом могут быть платформы в разных направлениях.\n\n"
            "Открой остановку в приложении «Московский транспорт», нажми «Поделиться» и отправь сюда ссылку. Подойдут короткая ссылка вида <code>https://moscowapp.mos.ru/l/...</code> или ссылка с UUID.",
            reply_markup=cancel_keyboard(),
        )
        return
    await db.set_bus_tracker(
        message.from_user.id, data["route_number"], data["boarding_stop_name"],
        data["boarding_stop_id"], data["direction_stop_name"],
    )
    await state.clear()
    await message.answer("✅ Маршрут сохранён. Проверяю ближайший прогноз…")
    await show_live_bus_tracker(message, message.from_user.id)


@dp.message(BusTrackerForm.boarding_stop_link)
async def bus_tracker_stop_link(message: Message, state: FSMContext) -> None:
    link = (message.text or "").strip()
    stop_id = await resolve_stop_uuid(link)
    if not stop_id:
        await message.answer(
            "Не получилось извлечь ID остановки из этой ссылки. Отправь ссылку «Поделиться» из приложения «Московский транспорт» ещё раз; не отправляй ссылку на главную страницу.",
            reply_markup=cancel_keyboard(),
        )
        return
    data = await state.get_data()
    await db.set_bus_tracker(
        message.from_user.id, data["route_number"], data["boarding_stop_name"],
        stop_id, data["direction_stop_name"],
    )
    await state.clear()
    await message.answer("✅ Остановка распознана, настройки сохранены. Проверяю ближайший прогноз…")
    await show_live_bus_tracker(message, message.from_user.id)


@dp.callback_query(F.data == "bus:live")
async def bus_live_refresh(callback: CallbackQuery) -> None:
    await callback.answer("Обновляю прогноз…")
    if callback.message:
        await show_live_bus_tracker(callback.message, callback.from_user.id)


@dp.callback_query(F.data == "bus:clear")
async def bus_tracker_clear(callback: CallbackQuery) -> None:
    await db.clear_bus_tracker(callback.from_user.id)
    await callback.answer("Маршрут удалён")
    if callback.message:
        await callback.message.answer(
            "🗑 Персональный маршрут удалён. Чтобы настроить его снова, нажми кнопку ниже.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚌 Настроить автобус", callback_data="bus:setup")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="back:local")],
            ]),
        )


@dp.callback_query(F.data == "bus:schedule")
async def bus_schedule_now(callback: CallbackQuery) -> None:
    await callback.answer("Считаю по справочному расписанию…")
    if callback.message:
        await send_bus_result(callback.message, moscow_now(), custom=False)


@dp.callback_query(F.data == "bus:custom")
async def bus_custom_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(BusForm.departure_time)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "Введи желаемое время выезда с Планерной по Москве в формате <code>ЧЧ:ММ</code>, например <code>09:16</code>.\n\n"
            "Бот найдёт ближайший рейс по справочному расписанию и укажет соответствующее время прибытия на остановку «Химико-технологический университет».",
            reply_markup=cancel_keyboard(),
        )


@dp.message(BusForm.departure_time)
async def bus_custom_time(message: Message, state: FSMContext) -> None:
    parsed = parse_clock((message.text or "").strip())
    if not parsed:
        await message.answer("Не распознал время. Введи в формате <code>ЧЧ:ММ</code>, например <code>09:16</code>.")
        return
    now = moscow_now()
    target = now.replace(hour=parsed[0], minute=parsed[1], second=0, microsecond=0)
    if target < now:
        target += timedelta(days=1)
    await state.clear()
    await send_bus_result(message, target, custom=True)


# ------------------------------
# Модуль 6. Новости и часы кода
# ------------------------------

@dp.callback_query(F.data == "backend:news")
async def backend_news(callback: CallbackQuery) -> None:
    await callback.answer("Загружаю RSS…")
    if not callback.message:
        return
    try:
        articles = await get_backend_news(limit=8)
    except Exception as exc:
        logger.warning("RSS request failed: %s", exc)
        articles = []
    if not articles:
        text = "Не удалось получить новости из RSS прямо сейчас. Попробуй позже или открой Habr вручную: https://habr.com/ru/hubs/backend/articles/"
        await callback.message.edit_text(text, reply_markup=backend_menu(), disable_web_page_preview=True)
        return
    lines = ["<b>📰 Короткие заголовки по Python / Backend</b>", ""]
    for article in articles:
        title = html.escape(article["title"][:180])
        url = html.escape(article["url"], quote=True)
        source = html.escape(article["source"])
        lines.append(f"• <a href=\"{url}\">{title}</a> <i>({source})</i>")
    lines.append("\n<i>Нажми на заголовок, чтобы открыть источник. Список формируется из публичных RSS-лент.</i>")
    await callback.message.edit_text("\n".join(lines), reply_markup=backend_menu(), disable_web_page_preview=True)


@dp.callback_query(F.data == "backend:hours")
async def code_hours_show(callback: CallbackQuery) -> None:
    await callback.answer()
    stats = await db.code_hours_stats(callback.from_user.id)
    if callback.message:
        await callback.message.edit_text(
            "<b>⏱ Трекер часов кода</b>\n\n"
            f"Сегодня: <b>{stats['today']:.2f} ч</b>\n"
            f"С начала недели: <b>{stats['week']:.2f} ч</b>\n"
            f"За всё время: <b>{stats['total']:.2f} ч</b>\n\n"
            "Нажми «Добавить часы за сегодня», чтобы записать ещё одну сессию. Если занимался несколько раз за день, значения суммируются.",
            reply_markup=hours_menu(),
        )


@dp.callback_query(F.data == "backend:addhours")
async def code_hours_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(HoursForm.hours)
    await callback.answer()
    if callback.message:
        await callback.message.answer("Сколько часов ты потратил на код сегодня? Можно дробное число, например <code>1.5</code> или <code>0,75</code>.", reply_markup=cancel_keyboard())


@dp.message(HoursForm.hours)
async def code_hours_save(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip().replace(" ", "").replace(",", ".")
    try:
        hours = float(raw)
        if not 0 < hours <= 24:
            raise ValueError
    except ValueError:
        await message.answer("Введи число часов больше 0 и не больше 24, например <code>1.5</code>.")
        return
    await db.add_code_hours(message.from_user.id, hours)
    await state.clear()
    stats = await db.code_hours_stats(message.from_user.id)
    await message.answer(
        f"✅ Добавлено <b>{hours:.2f} ч</b> за сегодня.\n\n"
        f"Сегодня: <b>{stats['today']:.2f} ч</b>\n"
        f"Неделя: <b>{stats['week']:.2f} ч</b>\n"
        f"За всё время: <b>{stats['total']:.2f} ч</b>",
        reply_markup=hours_menu(),
    )


async def main() -> None:
    if not config.bot_token or config.bot_token == "PASTE_YOUR_TELEGRAM_BOT_TOKEN_HERE":
        raise RuntimeError("Не задан BOT_TOKEN. Скопируй .env.example в .env и впиши токен от @BotFather.")
    await db.init_db()
    assert bot is not None
    await bot.set_my_commands([
        BotCommand(command="start", description="Открыть главное меню"),
        BotCommand(command="help", description="Помощь"),
        BotCommand(command="cancel", description="Отменить текущий ввод"),
        BotCommand(command="terms", description="Условия покупок за Stars"),
        BotCommand(command="paysupport", description="Поддержка по платежам"),
    ])
    logger.info("Starting MAI LifeOS bot")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
