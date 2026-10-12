from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Any
from collections.abc import AsyncIterator

import aiosqlite

from config import config, MOSCOW_TZ


@asynccontextmanager
async def connect() -> AsyncIterator[aiosqlite.Connection]:
    """Open one SQLite connection and always close it safely.

    aiosqlite connections are awaitable and their async context manager starts
    the worker thread. Awaiting a connection and then entering it starts that
    thread twice, causing: "threads can only be started once". This context
    manager ensures the connection is started exactly once and closed on exit.
    """
    db = await aiosqlite.connect(config.database_path)
    db.row_factory = aiosqlite.Row
    try:
        await db.execute("PRAGMA foreign_keys = ON")
        yield db
    finally:
        await db.close()


async def init_db() -> None:
    async with connect() as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                monthly_budget REAL NOT NULL DEFAULT 0,
                budget_configured INTEGER NOT NULL DEFAULT 0,
                session_date TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bot_access_users (
                user_id INTEGER PRIMARY KEY,
                first_seen_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            );
            CREATE TABLE IF NOT EXISTS telegram_identities (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_weather_locations (
                user_id INTEGER PRIMARY KEY,
                location_query TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS user_bus_trackers (
                user_id INTEGER PRIMARY KEY,
                route_number TEXT NOT NULL,
                boarding_stop_name TEXT NOT NULL,
                boarding_stop_id TEXT NOT NULL,
                direction_stop_name TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_telegram_identity_username
                ON telegram_identities(username COLLATE NOCASE);
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount REAL NOT NULL CHECK(amount > 0),
                category TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            );
            CREATE TABLE IF NOT EXISTS completed_topics (
                user_id INTEGER NOT NULL,
                topic TEXT NOT NULL,
                completed_date TEXT NOT NULL,
                PRIMARY KEY(user_id, topic),
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            );
            CREATE TABLE IF NOT EXISTS study_activity (
                user_id INTEGER NOT NULL,
                activity_date TEXT NOT NULL,
                PRIMARY KEY(user_id, activity_date),
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            );
            CREATE TABLE IF NOT EXISTS lessons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                weekday INTEGER NOT NULL,
                start_time TEXT NOT NULL,
                title TEXT NOT NULL,
                room TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            );
            CREATE TABLE IF NOT EXISTS deadlines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                deadline_date TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            );
            CREATE TABLE IF NOT EXISTS code_hours (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                activity_date TEXT NOT NULL,
                hours REAL NOT NULL CHECK(hours > 0),
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            );
            CREATE TABLE IF NOT EXISTS star_support_orders (
                payload TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                stars INTEGER NOT NULL CHECK(stars > 0),
                created_at TEXT NOT NULL,
                paid_at TEXT,
                telegram_payment_charge_id TEXT UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending',
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            );
            CREATE TABLE IF NOT EXISTS star_supporters (
                user_id INTEGER PRIMARY KEY,
                total_stars INTEGER NOT NULL DEFAULT 0,
                first_paid_at TEXT NOT NULL,
                last_paid_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            );
            CREATE TABLE IF NOT EXISTS gift_send_limits (
                user_id INTEGER PRIMARY KEY,
                last_sent_at TEXT,
                pending_token TEXT,
                pending_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_expenses_user_date ON expenses(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_deadlines_user_date ON deadlines(user_id, deadline_date);
            CREATE INDEX IF NOT EXISTS idx_code_hours_user_date ON code_hours(user_id, activity_date);
            """
        )
        # Migration: mark existing personalized budgets as configured, while
        # treating the previous implicit default (45,000 RUB) as unset.
        cur = await db.execute("PRAGMA table_info(users)")
        user_columns = {str(row["name"]) for row in await cur.fetchall()}
        if "budget_configured" not in user_columns:
            await db.execute(
                "ALTER TABLE users ADD COLUMN budget_configured INTEGER NOT NULL DEFAULT 0"
            )
            await db.execute(
                "UPDATE users SET budget_configured=1 WHERE monthly_budget != ?",
                (config.monthly_budget_default,),
            )

        # Migration for databases created before the user limit existed:
        # preserve access for at most the first MAX_USERS existing accounts.
        max_users = max(1, config.max_users)
        await db.execute(
            """
            INSERT OR IGNORE INTO bot_access_users(user_id, first_seen_at)
            SELECT user_id, created_at
            FROM users
            ORDER BY created_at, user_id
            LIMIT ?
            """,
            (max_users,),
        )
        await db.commit()



async def store_telegram_identity(user_id: int, username: str | None, first_name: str | None = None) -> None:
    """Remember usernames received from Telegram updates for @username lookup.

    The Bot API's sendGift method needs a numeric user_id. Usernames are not
    globally resolvable through the Bot API, so we can only use usernames that
    Telegram has already sent to this bot in an update.
    """
    normalized_username = username.lstrip("@").strip() if username else None
    if normalized_username:
        normalized_username = normalized_username.lower()
    now = datetime.now(MOSCOW_TZ).isoformat(timespec="seconds")
    async with connect() as db:
        await db.execute(
            """
            INSERT INTO telegram_identities(user_id, username, first_name, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                updated_at=excluded.updated_at
            """,
            (user_id, normalized_username, first_name, now),
        )
        await db.commit()


async def get_user_id_by_username(username: str) -> int | None:
    normalized_username = username.lstrip("@").strip().lower()
    async with connect() as db:
        cur = await db.execute(
            "SELECT user_id FROM telegram_identities WHERE username=? COLLATE NOCASE ORDER BY updated_at DESC LIMIT 1",
            (normalized_username,),
        )
        row = await cur.fetchone()
    return int(row[0]) if row else None


async def register_user_if_allowed(user_id: int) -> bool:
    """Atomically grant persistent access to at most config.max_users Telegram IDs.

    The first unique users to interact with the bot get access. The registration
    survives restarts; a rejected ID does not consume a slot.
    """
    now = datetime.now(MOSCOW_TZ).isoformat(timespec="microseconds")
    max_users = max(1, config.max_users)

    async with connect() as db:
        # BEGIN IMMEDIATE serializes simultaneous first-time registrations so
        # two users cannot both take the last slot.
        await db.execute("BEGIN IMMEDIATE")

        cur = await db.execute(
            "SELECT 1 FROM bot_access_users WHERE user_id=?", (user_id,)
        )
        if await cur.fetchone():
            await db.commit()
            return True

        cur = await db.execute("SELECT COUNT(*) FROM bot_access_users")
        registered_count = int((await cur.fetchone())[0])
        if registered_count >= max_users:
            await db.rollback()
            return False

        await db.execute(
            "INSERT OR IGNORE INTO users(user_id, monthly_budget, created_at) VALUES (?, ?, ?)",
            (user_id, 0, now),
        )
        await db.execute(
            "INSERT INTO bot_access_users(user_id, first_seen_at) VALUES (?, ?)",
            (user_id, now),
        )
        await db.commit()
        return True


async def get_registered_user_count() -> int:
    """Return the number of Telegram IDs occupying an access slot."""
    async with connect() as db:
        cur = await db.execute("SELECT COUNT(*) FROM bot_access_users")
        return int((await cur.fetchone())[0])


async def get_automatic_gift_allowed_user_ids() -> list[int]:
    """Return the first registered non-owner IDs eligible for the Gift module.

    The owner has separate access. With MAX_USERS=10, this allows the first
    nine registered people other than the owner, matching the intended
    owner + nine users arrangement. The list is derived from SQLite so it
    persists across restarts and needs no manual edits to .env.
    """
    limit = max(0, config.max_users - 1)
    if limit == 0:
        return []
    async with connect() as db:
        cur = await db.execute(
            """
            SELECT user_id
            FROM bot_access_users
            WHERE user_id <> ?
            ORDER BY first_seen_at ASC, user_id ASC
            LIMIT ?
            """,
            (config.gift_owner_id, limit),
        )
        rows = await cur.fetchall()
    return [int(row[0]) for row in rows]


async def can_use_gift_module(user_id: int) -> bool:
    """Check explicit owner/manual permissions plus the automatic first-9 list."""
    if config.can_use_gift_module(user_id):
        return True
    automatic_ids = await get_automatic_gift_allowed_user_ids()
    return user_id in automatic_ids


GIFT_COOLDOWN_SECONDS = 24 * 60 * 60
GIFT_PENDING_LEASE_SECONDS = 5 * 60


def _seconds_remaining(target: datetime, now: datetime) -> int:
    seconds = (target - now).total_seconds()
    # Round up so a remaining fractional second is never displayed as zero.
    import math
    return max(0, math.ceil(seconds))


def _parse_stored_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MOSCOW_TZ)
    return parsed


async def get_gift_cooldown_remaining_seconds(user_id: int) -> int:
    """Return seconds until this non-owner can send another gift, or zero."""
    if user_id == config.gift_owner_id:
        return 0
    now = datetime.now(MOSCOW_TZ)
    async with connect() as db:
        cur = await db.execute(
            "SELECT last_sent_at FROM gift_send_limits WHERE user_id=?",
            (user_id,),
        )
        row = await cur.fetchone()
    if not row:
        return 0
    sent_at = _parse_stored_datetime(row[0])
    if sent_at is None:
        return 0
    next_allowed_at = sent_at + timedelta(seconds=GIFT_COOLDOWN_SECONDS)
    return _seconds_remaining(next_allowed_at, now)


async def reserve_gift_send_slot(user_id: int, token: str) -> tuple[str, int]:
    """Atomically reserve a non-owner's send slot. Result is ready/cooldown/busy."""
    if user_id == config.gift_owner_id:
        return ("ready", 0)

    now = datetime.now(MOSCOW_TZ)
    now_text = now.isoformat(timespec="microseconds")
    async with connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            "SELECT last_sent_at, pending_token, pending_at FROM gift_send_limits WHERE user_id=?",
            (user_id,),
        )
        row = await cur.fetchone()

        if row:
            last_sent_at = _parse_stored_datetime(row[0])
            if last_sent_at is not None:
                next_allowed_at = last_sent_at + timedelta(seconds=GIFT_COOLDOWN_SECONDS)
                remaining = _seconds_remaining(next_allowed_at, now)
                if remaining > 0:
                    await db.rollback()
                    return ("cooldown", remaining)

            pending_token = row[1]
            pending_at = _parse_stored_datetime(row[2])
            if pending_token and pending_at is not None:
                lease_remaining = _seconds_remaining(
                    pending_at + timedelta(seconds=GIFT_PENDING_LEASE_SECONDS), now
                )
                if lease_remaining > 0:
                    await db.rollback()
                    return ("busy", lease_remaining)

        await db.execute(
            """INSERT INTO gift_send_limits(user_id, last_sent_at, pending_token, pending_at)
               VALUES (?, NULL, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   pending_token=excluded.pending_token,
                   pending_at=excluded.pending_at""",
            (user_id, token, now_text),
        )
        await db.commit()
    return ("ready", 0)


async def complete_gift_send(user_id: int, token: str) -> None:
    """Persist a successful send and start the 24-hour cooldown."""
    now_text = datetime.now(MOSCOW_TZ).isoformat(timespec="microseconds")
    async with connect() as db:
        await db.execute(
            """UPDATE gift_send_limits
               SET last_sent_at=?, pending_token=NULL, pending_at=NULL
               WHERE user_id=? AND pending_token=?""",
            (now_text, user_id, token),
        )
        await db.commit()


async def release_gift_send_slot(user_id: int, token: str) -> None:
    """Release a reserved send slot after Telegram rejects the gift."""
    async with connect() as db:
        await db.execute(
            """UPDATE gift_send_limits SET pending_token=NULL, pending_at=NULL
               WHERE user_id=? AND pending_token=?""",
            (user_id, token),
        )
        await db.commit()


async def create_star_support_order(payload: str, user_id: int, stars: int) -> None:
    """Create a pending Telegram Stars invoice order before sending the invoice."""
    now = datetime.now(MOSCOW_TZ).isoformat(timespec="seconds")
    async with connect() as db:
        await db.execute(
            "INSERT INTO star_support_orders(payload, user_id, stars, created_at, status) VALUES (?, ?, ?, ?, 'pending')",
            (payload, user_id, stars, now),
        )
        await db.commit()


async def cancel_star_support_order(payload: str) -> None:
    async with connect() as db:
        await db.execute(
            "UPDATE star_support_orders SET status='cancelled' WHERE payload=? AND status='pending'",
            (payload,),
        )
        await db.commit()


async def get_star_support_order(payload: str) -> dict[str, Any] | None:
    async with connect() as db:
        cur = await db.execute(
            "SELECT payload, user_id, stars, status FROM star_support_orders WHERE payload=?",
            (payload,),
        )
        row = await cur.fetchone()
    return dict(row) if row else None


async def complete_star_support_order(
    payload: str,
    user_id: int,
    stars: int,
    currency: str,
    telegram_payment_charge_id: str,
) -> bool:
    """Idempotently record a successful Stars payment and grant Supporter status."""
    if currency != "XTR" or stars <= 0 or not telegram_payment_charge_id:
        return False
    now = datetime.now(MOSCOW_TZ).isoformat(timespec="seconds")
    async with connect() as db:
        await db.execute("BEGIN IMMEDIATE")
        cur = await db.execute(
            "SELECT user_id, stars, status FROM star_support_orders WHERE payload=?",
            (payload,),
        )
        row = await cur.fetchone()
        if (
            row is None
            or int(row["user_id"]) != user_id
            or int(row["stars"]) != stars
            or str(row["status"]) != "pending"
        ):
            await db.rollback()
            return False
        try:
            await db.execute(
                "UPDATE star_support_orders SET status='paid', paid_at=?, telegram_payment_charge_id=? WHERE payload=? AND status='pending'",
                (now, telegram_payment_charge_id, payload),
            )
            await db.execute(
                """INSERT INTO star_supporters(user_id, total_stars, first_paid_at, last_paid_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       total_stars=star_supporters.total_stars + excluded.total_stars,
                       last_paid_at=excluded.last_paid_at""",
                (user_id, stars, now, now),
            )
            await db.commit()
            return True
        except Exception:
            await db.rollback()
            raise


async def get_star_supporter(user_id: int) -> dict[str, Any] | None:
    async with connect() as db:
        cur = await db.execute(
            "SELECT user_id, total_stars, first_paid_at, last_paid_at FROM star_supporters WHERE user_id=?",
            (user_id,),
        )
        row = await cur.fetchone()
    return dict(row) if row else None


async def ensure_user(user_id: int) -> None:
    now = datetime.now(MOSCOW_TZ).isoformat(timespec="seconds")
    async with connect() as db:
        await db.execute(
            "INSERT OR IGNORE INTO users(user_id, monthly_budget, created_at) VALUES (?, ?, ?)",
            (user_id, 0, now),
        )
        await db.commit()


async def get_weather_location(user_id: int) -> str | None:
    """Return a user's explicitly selected weather location, or None if unset."""
    async with connect() as db:
        cur = await db.execute(
            "SELECT location_query FROM user_weather_locations WHERE user_id=?",
            (user_id,),
        )
        row = await cur.fetchone()
    return str(row[0]) if row else None


async def set_weather_location(user_id: int, location_query: str) -> None:
    """Save the location a user explicitly chose after successful geocoding."""
    query = " ".join((location_query or "").split()).strip()
    if not query:
        raise ValueError("location_query must not be empty")
    now = datetime.now(MOSCOW_TZ).isoformat(timespec="seconds")
    async with connect() as db:
        await db.execute(
            "INSERT OR IGNORE INTO users(user_id, monthly_budget, created_at) VALUES (?, 0, ?)",
            (user_id, now),
        )
        await db.execute(
            """INSERT INTO user_weather_locations(user_id, location_query, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   location_query=excluded.location_query,
                   updated_at=excluded.updated_at""",
            (user_id, query, now),
        )
        await db.commit()


async def clear_weather_location(user_id: int) -> None:
    """Remove a saved weather location, returning the weather module to first-use state."""
    async with connect() as db:
        await db.execute("DELETE FROM user_weather_locations WHERE user_id=?", (user_id,))
        await db.commit()


async def get_bus_tracker(user_id: int) -> dict[str, Any] | None:
    """Return the user's saved route and boarding-stop settings."""
    async with connect() as db:
        cur = await db.execute(
            """SELECT route_number, boarding_stop_name, boarding_stop_id, direction_stop_name
               FROM user_bus_trackers WHERE user_id=?""",
            (user_id,),
        )
        row = await cur.fetchone()
    return dict(row) if row else None


async def set_bus_tracker(
    user_id: int, route_number: str, boarding_stop_name: str,
    boarding_stop_id: str, direction_stop_name: str,
) -> None:
    """Save a user's live bus tracker settings in SQLite."""
    now = datetime.now(MOSCOW_TZ).isoformat(timespec="seconds")
    async with connect() as db:
        await db.execute(
            "INSERT OR IGNORE INTO users(user_id, monthly_budget, created_at) VALUES (?, 0, ?)",
            (user_id, now),
        )
        await db.execute(
            """INSERT INTO user_bus_trackers
               (user_id, route_number, boarding_stop_name, boarding_stop_id, direction_stop_name, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   route_number=excluded.route_number,
                   boarding_stop_name=excluded.boarding_stop_name,
                   boarding_stop_id=excluded.boarding_stop_id,
                   direction_stop_name=excluded.direction_stop_name,
                   updated_at=excluded.updated_at""",
            (user_id, route_number, boarding_stop_name, boarding_stop_id, direction_stop_name, now),
        )
        await db.commit()


async def clear_bus_tracker(user_id: int) -> None:
    """Remove a user's saved bus tracker settings."""
    async with connect() as db:
        await db.execute("DELETE FROM user_bus_trackers WHERE user_id=?", (user_id,))
        await db.commit()


async def get_budget(user_id: int) -> float | None:
    """Return the user's monthly budget, or None until they explicitly set it."""
    async with connect() as db:
        cur = await db.execute(
            "SELECT monthly_budget, budget_configured FROM users WHERE user_id=?",
            (user_id,),
        )
        row = await cur.fetchone()
    if not row or not bool(row["budget_configured"]):
        return None
    return float(row["monthly_budget"])


async def set_budget(user_id: int, monthly_budget: float) -> None:
    """Set a user's personal monthly budget in RUB and mark it as configured."""
    if monthly_budget <= 0:
        raise ValueError("monthly_budget must be positive")
    now = datetime.now(MOSCOW_TZ).isoformat(timespec="seconds")
    async with connect() as db:
        await db.execute(
            "INSERT OR IGNORE INTO users(user_id, monthly_budget, budget_configured, created_at) VALUES (?, 0, 0, ?)",
            (user_id, now),
        )
        await db.execute(
            "UPDATE users SET monthly_budget=?, budget_configured=1 WHERE user_id=?",
            (float(monthly_budget), user_id),
        )
        await db.commit()


async def add_expense(user_id: int, amount: float, category: str, note: str = "") -> None:
    now = datetime.now(MOSCOW_TZ).isoformat(timespec="seconds")
    async with connect() as db:
        await db.execute(
            "INSERT INTO expenses(user_id, amount, category, note, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, amount, category, note, now),
        )
        await db.commit()


async def clear_current_month_expenses(user_id: int) -> int:
    """Delete only this user's expenses for the current Moscow calendar month.

    The user's monthly budget and expense history from prior months are preserved.
    Returns the number of deleted expense records.
    """
    month_prefix = datetime.now(MOSCOW_TZ).strftime("%Y-%m")
    async with connect() as db:
        cur = await db.execute(
            "DELETE FROM expenses WHERE user_id=? AND substr(created_at, 1, 7)=?",
            (user_id, month_prefix),
        )
        deleted_count = max(0, int(cur.rowcount or 0))
        await db.commit()
    return deleted_count


async def monthly_expense_stats(user_id: int) -> dict[str, Any]:
    now = datetime.now(MOSCOW_TZ)
    month_prefix = now.strftime("%Y-%m")
    async with connect() as db:
        cur = await db.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM expenses WHERE user_id=? AND substr(created_at, 1, 7)=?",
            (user_id, month_prefix),
        )
        total_row = await cur.fetchone()
        cur = await db.execute(
            "SELECT category, SUM(amount) AS total FROM expenses WHERE user_id=? AND substr(created_at, 1, 7)=? GROUP BY category ORDER BY total DESC",
            (user_id, month_prefix),
        )
        rows = await cur.fetchall()
    return {"month": now.strftime("%m.%Y"), "total": float(total_row[0] or 0), "by_category": [(r[0], float(r[1])) for r in rows]}


async def get_completed_topics(user_id: int) -> set[str]:
    async with connect() as db:
        cur = await db.execute("SELECT topic FROM completed_topics WHERE user_id=?", (user_id,))
        rows = await cur.fetchall()
    return {str(row[0]) for row in rows}


async def _record_study_activity(db: aiosqlite.Connection, user_id: int) -> None:
    today = datetime.now(MOSCOW_TZ).date().isoformat()
    await db.execute("INSERT OR IGNORE INTO study_activity(user_id, activity_date) VALUES (?, ?)", (user_id, today))


async def toggle_topic(user_id: int, topic: str, completed: bool) -> None:
    today = datetime.now(MOSCOW_TZ).date().isoformat()
    async with connect() as db:
        if completed:
            await db.execute(
                "INSERT OR REPLACE INTO completed_topics(user_id, topic, completed_date) VALUES (?, ?, ?)",
                (user_id, topic, today),
            )
            await _record_study_activity(db, user_id)
        else:
            await db.execute("DELETE FROM completed_topics WHERE user_id=? AND topic=?", (user_id, topic))
        await db.commit()


async def record_study_activity(user_id: int) -> None:
    async with connect() as db:
        await _record_study_activity(db, user_id)
        await db.commit()


async def get_streak(user_id: int) -> int:
    async with connect() as db:
        cur = await db.execute("SELECT activity_date FROM study_activity WHERE user_id=? ORDER BY activity_date DESC", (user_id,))
        rows = await cur.fetchall()
    dates = {date.fromisoformat(str(r[0])) for r in rows}
    today = datetime.now(MOSCOW_TZ).date()
    cursor = today if today in dates else today - timedelta(days=1)
    if cursor not in dates:
        return 0
    streak = 0
    while cursor in dates:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


async def add_lesson(user_id: int, weekday: int, start_time: str, title: str, room: str = "") -> None:
    async with connect() as db:
        await db.execute(
            "INSERT INTO lessons(user_id, weekday, start_time, title, room) VALUES (?, ?, ?, ?, ?)",
            (user_id, weekday, start_time, title, room),
        )
        await db.commit()


async def get_lessons(user_id: int) -> list[aiosqlite.Row]:
    async with connect() as db:
        cur = await db.execute(
            "SELECT id, weekday, start_time, title, room FROM lessons WHERE user_id=? ORDER BY weekday, start_time",
            (user_id,),
        )
        return await cur.fetchall()


async def add_deadline(user_id: int, title: str, deadline_date: date) -> None:
    now = datetime.now(MOSCOW_TZ).isoformat(timespec="seconds")
    async with connect() as db:
        await db.execute(
            "INSERT INTO deadlines(user_id, title, deadline_date, created_at) VALUES (?, ?, ?, ?)",
            (user_id, title, deadline_date.isoformat(), now),
        )
        await db.commit()


async def get_deadlines(user_id: int) -> list[aiosqlite.Row]:
    async with connect() as db:
        cur = await db.execute(
            "SELECT id, title, deadline_date FROM deadlines WHERE user_id=? ORDER BY deadline_date",
            (user_id,),
        )
        return await cur.fetchall()


async def set_session_date(user_id: int, session_date: date) -> None:
    async with connect() as db:
        await db.execute("UPDATE users SET session_date=? WHERE user_id=?", (session_date.isoformat(), user_id))
        await db.commit()


async def get_session_date(user_id: int) -> date | None:
    async with connect() as db:
        cur = await db.execute("SELECT session_date FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
    if not row or not row[0]:
        return None
    try:
        return date.fromisoformat(str(row[0]))
    except ValueError:
        return None


async def add_code_hours(user_id: int, hours: float) -> None:
    now = datetime.now(MOSCOW_TZ)
    async with connect() as db:
        await db.execute(
            "INSERT INTO code_hours(user_id, activity_date, hours, created_at) VALUES (?, ?, ?, ?)",
            (user_id, now.date().isoformat(), hours, now.isoformat(timespec="seconds")),
        )
        await db.commit()


async def code_hours_stats(user_id: int) -> dict[str, float]:
    today = datetime.now(MOSCOW_TZ).date()
    week_start = today - timedelta(days=today.weekday())
    async with connect() as db:
        cur = await db.execute(
            "SELECT COALESCE(SUM(hours),0) FROM code_hours WHERE user_id=? AND activity_date=?",
            (user_id, today.isoformat()),
        )
        today_total = float((await cur.fetchone())[0])
        cur = await db.execute(
            "SELECT COALESCE(SUM(hours),0) FROM code_hours WHERE user_id=? AND activity_date>=? AND activity_date<=?",
            (user_id, week_start.isoformat(), today.isoformat()),
        )
        week_total = float((await cur.fetchone())[0])
        cur = await db.execute("SELECT COALESCE(SUM(hours),0) FROM code_hours WHERE user_id=?", (user_id,))
        total = float((await cur.fetchone())[0])
    return {"today": today_total, "week": week_total, "total": total}
