from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from data import EXPENSE_CATEGORIES, ROADMAP, CAREER_LINKS


def main_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💰 Мой бюджет", callback_data="module:budget")
    b.button(text="📚 Путь в Backend", callback_data="module:learning")
    b.button(text="🎓 МАИ-хелпер", callback_data="module:study")
    b.button(text="💼 Работа и фриланс", callback_data="module:career")
    b.button(text="🌦 Район и транспорт", callback_data="module:local")
    b.button(text="📰 Backend-новости и часы", callback_data="module:backend")
    b.button(text="🎁 Подарок", callback_data="module:gift")
    b.button(text="⭐ Поддержать проект", callback_data="module:topup")
    b.adjust(2, 2, 2, 2)
    return b.as_markup()


def back_button(target: str = "main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=f"back:{target}")]])


def budget_menu(budget_is_set: bool = True) -> InlineKeyboardMarkup:
    """Show only budget setup until the user sets a monthly amount."""
    b = InlineKeyboardBuilder()
    if not budget_is_set:
        b.button(text="➕ Добавить бюджет на этот месяц", callback_data="budget:set")
        b.button(text="🏠 Главное меню", callback_data="back:main")
        b.adjust(1)
        return b.as_markup()

    b.button(text="➕ Добавить расход", callback_data="budget:add")
    b.button(text="📊 Статистика за месяц", callback_data="budget:stats")
    b.button(text="⚙️ Изменить бюджет на месяц", callback_data="budget:set")
    b.button(text="🍲 Что поесть на сумму", callback_data="budget:food")
    b.button(text="🗑 Очистить траты за месяц", callback_data="budget:clear")
    b.button(text="🏠 Главное меню", callback_data="back:main")
    b.adjust(1, 1, 1, 1, 1, 1)
    return b.as_markup()


def clear_expenses_confirm_menu() -> InlineKeyboardMarkup:
    """Require explicit confirmation before deleting this month's expenses."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, очистить траты за месяц", callback_data="budget:clear:confirm")],
        [InlineKeyboardButton(text="↩️ Отмена", callback_data="budget:clear:cancel")],
    ])


def expense_categories() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for idx, category in enumerate(EXPENSE_CATEGORIES):
        b.button(text=category, callback_data=f"exp_cat:{idx}")
    b.button(text="❌ Отмена", callback_data="cancel")
    b.adjust(3, 3, 3, 1)
    return b.as_markup()


def learning_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Отметить пройденную тему", callback_data="learning:topics")
    b.button(text="🎯 Что учить сегодня", callback_data="learning:today")
    b.button(text="🧩 Задача дня по Python", callback_data="learning:task")
    b.button(text="📈 Прогресс Junior/Middle/Senior", callback_data="learning:progress")
    b.button(text="🔥 Мой streak", callback_data="learning:streak")
    b.button(text="🏠 Главное меню", callback_data="back:main")
    b.adjust(1, 1, 1, 1, 1, 1)
    return b.as_markup()


def topics_keyboard(completed: set[str]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for idx, topic in enumerate(ROADMAP):
        mark = "✅" if topic in completed else "▫️"
        label = f"{mark} {idx + 1}. {topic}"
        b.button(text=label[:60], callback_data=f"topic:{idx}")
    b.button(text="⬅️ Назад", callback_data="back:learning")
    b.adjust(1)
    return b.as_markup()


def study_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📅 Показать расписание", callback_data="study:schedule")
    b.button(text="🗓 Дедлайны", callback_data="study:deadlines")
    b.button(text="🎓 Сколько до сессии", callback_data="study:session")
    b.button(text="📍 Установить дату сессии", callback_data="study:setsession")
    b.button(text="🏠 Главное меню", callback_data="back:main")
    b.adjust(1)
    return b.as_markup()


def career_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔍 Вакансии Junior Python", callback_data="career:python")
    b.button(text="💻 Фриланс-заказы (Kwork / FL.ru)", callback_data="career:freelance")
    b.button(text="🎓 IT-стажировки", callback_data="career:internships")
    b.button(text="📄 Собери резюме", callback_data="career:resume")
    b.button(text="💡 Идеи микрозаработка", callback_data="career:micro")
    b.button(text="✈️ Работа на стыке авиации и IT", callback_data="career:aero")
    b.button(text="🏫 Центр карьеры МАИ", url=CAREER_LINKS["mai"][1])
    b.button(text="🏠 Главное меню", callback_data="back:main")
    b.adjust(1)
    return b.as_markup()


def freelance_links() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Kwork — проекты", url=CAREER_LINKS["kwork"][1])
    b.button(text="FL.ru — проекты", url=CAREER_LINKS["fl"][1])
    b.button(text="⬅️ Назад", callback_data="back:career")
    b.adjust(1)
    return b.as_markup()


def internship_links() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Поиск IT-стажировок на hh.ru", url=CAREER_LINKS["hh_intern"][1])
    b.button(text="Стажировки Яндекса", url=CAREER_LINKS["yandex_intern"][1])
    b.button(text="Карьера Сбера", url=CAREER_LINKS["sber"][1])
    b.button(text="Карьера VK", url=CAREER_LINKS["vk"][1])
    b.button(text="⬅️ Назад", callback_data="back:career")
    b.adjust(1)
    return b.as_markup()


def local_menu(yandex_url: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🌦 Погода по моему району", callback_data="local:weather")
    b.button(text="🚌 С396 — ближайший рейс", callback_data="bus:now")
    b.button(text="🕒 Рассчитать по времени выезда", callback_data="bus:custom")
    b.button(text="🗺 Открыть движение в Яндекс Картах", url=yandex_url)
    b.button(text="🏠 Главное меню", callback_data="back:main")
    b.adjust(1)
    return b.as_markup()


def backend_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📰 Новости Backend", callback_data="backend:news")
    b.button(text="⏱ Трекер часов кода", callback_data="backend:hours")
    b.button(text="➕ Добавить часы за сегодня", callback_data="backend:addhours")
    b.button(text="🏠 Главное меню", callback_data="back:main")
    b.adjust(1)
    return b.as_markup()


def hours_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="➕ Добавить часы за сегодня", callback_data="backend:addhours")
    b.button(text="🔄 Обновить статистику", callback_data="backend:hours")
    b.button(text="⬅️ Назад", callback_data="back:backend")
    b.adjust(1)
    return b.as_markup()


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]])


def food_recipes_menu(amount: float, seen_indices: list[int], shown_indices: list[int]) -> InlineKeyboardMarkup:
    """Refresh food suggestions, excluding the currently shown recipe IDs when possible."""
    shown_text = ",".join(str(i) for i in sorted(set(shown_indices))) or "-"
    # Only up to three shown indices are stored, staying below Telegram's 64-byte limit.
    refresh_data = f"food:refresh:{amount:g}:{shown_text}"
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Обновить список рецептов", callback_data=refresh_data)
    b.button(text="💸 Подобрать на другую сумму", callback_data="budget:food")
    b.button(text="⬅️ Вернуться в бюджет", callback_data="back:budget")
    b.adjust(1)
    return b.as_markup()



def gift_selection_keyboard(gifts) -> InlineKeyboardMarkup:
    """Show currently available regular gifts priced from 15 to 100 Stars."""
    b = InlineKeyboardBuilder()
    for gift in gifts:
        sticker = getattr(gift, "sticker", None)
        emoji = getattr(sticker, "emoji", None) or "🎁"
        price = int(getattr(gift, "star_count", 0) or 0)
        gift_id = str(getattr(gift, "id", ""))
        callback_data = f"gift:select:{gift_id}"
        # Telegram callback_data is limited to 64 bytes.
        if gift_id and len(callback_data.encode("utf-8")) <= 64:
            b.button(text=f"{emoji}  {price} ⭐", callback_data=callback_data)
    b.button(text="🔄 Обновить баланс и подарки", callback_data="gift:refresh")
    b.button(text="🏠 Главное меню", callback_data="back:main")
    b.adjust(2, 1, 1)
    return b.as_markup()


def gift_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить отправку", callback_data="gift:send")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="gift:cancel")],
    ])


def stars_topup_menu(is_supporter: bool = False) -> InlineKeyboardMarkup:
    """Menu for purchasing the digital Supporter status via Telegram Stars."""
    b = InlineKeyboardBuilder()
    for amount in (15, 25, 50, 100, 250, 500):
        b.button(text=f"⭐ {amount} Stars", callback_data=f"stars:prepare:{amount}")
    b.button(text="🛒 Где приобрести Stars?", callback_data="stars:methods")
    b.button(text="🏠 Главное меню", callback_data="back:main")
    b.adjust(2, 2, 2, 1, 1)
    return b.as_markup()


def stars_methods_menu() -> InlineKeyboardMarkup:
    """Official Telegram routes for acquiring Stars, where available."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ @PremiumBot", url="https://t.me/PremiumBot")],
        [InlineKeyboardButton(text="🌐 Fragment", url="https://fragment.com/stars")],
        [InlineKeyboardButton(text="⬅️ К вариантам поддержки", callback_data="stars:menu")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back:main")],
    ])
