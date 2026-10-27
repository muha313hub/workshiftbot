import os
import json
import hmac
import hashlib
import logging
from urllib.parse import parse_qsl
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo

import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram import Bot, Dispatcher, Router
from aiogram.filters import CommandStart
from aiogram.types import Message, WebAppInfo, KeyboardButton, ReplyKeyboardMarkup
from aiogram.exceptions import TelegramAPIError
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("workshift")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
APP_URL = os.getenv("APP_URL", "").rstrip("/")
DEFAULT_TZ = os.getenv("TIMEZONE", "Europe/Warsaw")
DB_PATH = os.getenv("DATABASE_PATH", "data/workshift.sqlite3")
if "/" in DB_PATH:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

bot = Bot(BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()
router = Router()
dp.include_router(router)
app = FastAPI(title="WorkShift Telegram Mini App")
scheduler = AsyncIOScheduler(timezone="UTC")


def now_for(tz_name: str) -> datetime:
    try:
        return datetime.now(ZoneInfo(tz_name))
    except Exception:
        return datetime.now(ZoneInfo(DEFAULT_TZ))


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            schedule_type TEXT NOT NULL DEFAULT '2/2',
            anchor_date TEXT NOT NULL DEFAULT '2026-01-01',
            timezone TEXT NOT NULL DEFAULT 'Europe/Warsaw',
            reminder_evening INTEGER NOT NULL DEFAULT 1,
            reminder_hours INTEGER NOT NULL DEFAULT 2,
            reminder_hour INTEGER NOT NULL DEFAULT 20,
            reminder_minute INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL
        )""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS manual_days (
            user_id INTEGER NOT NULL,
            work_date TEXT NOT NULL,
            PRIMARY KEY (user_id, work_date)
        )""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS sent_reminders (
            user_id INTEGER NOT NULL,
            work_date TEXT NOT NULL,
            reminder_type TEXT NOT NULL,
            PRIMARY KEY (user_id, work_date, reminder_type)
        )""")
        await db.commit()


def validate_init_data(init_data: str) -> dict:
    if not BOT_TOKEN or not init_data:
        raise HTTPException(401, "Telegram Mini App data missing")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise HTTPException(401, "Invalid Telegram initData")
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        raise HTTPException(401, "Telegram initData signature check failed")
    auth_date = int(pairs.get("auth_date", "0"))
    if abs(int(datetime.now().timestamp()) - auth_date) > 86400:
        raise HTTPException(401, "Telegram initData expired; reopen the app")
    try:
        user = json.loads(pairs["user"])
        user_id = int(user["id"])
    except Exception:
        raise HTTPException(401, "Telegram user missing")
    return {"user_id": user_id, "username": user.get("username", "")}


async def get_settings(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM settings WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if row:
            return dict(row)
        stamp = datetime.utcnow().isoformat()
        await db.execute(
            "INSERT INTO settings(user_id, timezone, updated_at) VALUES(?,?,?)",
            (user_id, DEFAULT_TZ, stamp),
        )
        await db.commit()
        cur = await db.execute("SELECT * FROM settings WHERE user_id=?", (user_id,))
        return dict(await cur.fetchone())


def is_workday(day: date, settings: dict, manual_days: set[str]) -> bool:
    kind = settings["schedule_type"]
    if kind == "manual":
        return day.isoformat() in manual_days
    anchor = date.fromisoformat(settings["anchor_date"])
    offset = (day - anchor).days
    if kind == "2/2":
        return offset % 4 in (0, 1)
    if kind == "5/2":
        return offset % 7 in (0, 1, 2, 3, 4)
    if kind == "1/3":
        return offset % 4 == 0
    return False


async def get_manual_days(user_id: int, start: date, end: date) -> set[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT work_date FROM manual_days WHERE user_id=? AND work_date BETWEEN ? AND ?",
            (user_id, start.isoformat(), end.isoformat()),
        )
        return {r[0] for r in await cur.fetchall()}


async def save_settings(user_id: int, username: str, payload: dict):
    allowed_types = {"2/2", "5/2", "1/3", "manual"}
    kind = payload.get("schedule_type", "2/2")
    if kind not in allowed_types:
        raise HTTPException(400, "Unsupported schedule type")
    try:
        anchor = date.fromisoformat(payload.get("anchor_date", date.today().isoformat()))
        tz_name = payload.get("timezone", DEFAULT_TZ)
        ZoneInfo(tz_name)
        reminder_hours = int(payload.get("reminder_hours", 2))
        reminder_hour = int(payload.get("reminder_hour", 20))
        reminder_minute = int(payload.get("reminder_minute", 0))
    except Exception:
        raise HTTPException(400, "Invalid date, timezone, or reminder time")
    if not 0 <= reminder_hours <= 48 or not 0 <= reminder_hour <= 23 or not 0 <= reminder_minute <= 59:
        raise HTTPException(400, "Invalid reminder settings")
    evening = 1 if payload.get("reminder_evening", True) else 0
    enabled = 1 if payload.get("enabled", True) else 0
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        INSERT INTO settings(user_id, username, schedule_type, anchor_date, timezone,
          reminder_evening, reminder_hours, reminder_hour, reminder_minute, enabled, updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
          username=excluded.username, schedule_type=excluded.schedule_type,
          anchor_date=excluded.anchor_date, timezone=excluded.timezone,
          reminder_evening=excluded.reminder_evening, reminder_hours=excluded.reminder_hours,
          reminder_hour=excluded.reminder_hour, reminder_minute=excluded.reminder_minute,
          enabled=excluded.enabled, updated_at=excluded.updated_at
        """, (user_id, username, kind, anchor.isoformat(), tz_name, evening,
              reminder_hours, reminder_hour, reminder_minute, enabled, datetime.utcnow().isoformat()))
        await db.commit()


@router.message(CommandStart())
async def start(message: Message):
    if not APP_URL:
        await message.answer("Бот запущен, но администратор ещё не настроил APP_URL.")
        return
    keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📅 Открыть мой график",
                                  web_app=WebAppInfo(url=APP_URL))]],
        resize_keyboard=True,
    )
    await message.answer(
        "Привет! Это WorkShift — личный график работы.\n"
        "Открой мини-приложение, выбери схему, дату первой рабочей смены "
        "и напоминания. Настройки будут сохранены для твоего аккаунта.",
        reply_markup=keyboard,
    )


@app.on_event("startup")
async def startup():
    await init_db()
    if bot:
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.startup()
        # Polling runs as a background task alongside the web app.
        import asyncio
        asyncio.create_task(dp.start_polling(bot, handle_signals=False))
        scheduler.add_job(check_reminders, "interval", seconds=30, id="reminders",
                          max_instances=1, coalesce=True)
        scheduler.start()
    else:
        log.warning("BOT_TOKEN is empty; API health endpoint only")


@app.on_event("shutdown")
async def shutdown():
    if scheduler.running:
        scheduler.shutdown(wait=False)
    if bot:
        await bot.session.close()


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/api/settings")
async def api_get_settings(request: Request):
    auth = validate_init_data(request.headers.get("X-Telegram-Init-Data", ""))
    settings = await get_settings(auth["user_id"])
    return settings


@app.post("/api/settings")
async def api_save_settings(request: Request):
    auth = validate_init_data(request.headers.get("X-Telegram-Init-Data", ""))
    payload = await request.json()
    await save_settings(auth["user_id"], auth["username"], payload)
    return {"ok": True}


@app.get("/api/calendar")
async def api_calendar(request: Request, month: str):
    auth = validate_init_data(request.headers.get("X-Telegram-Init-Data", ""))
    try:
        first = date.fromisoformat(month + "-01")
    except Exception:
        raise HTTPException(400, "month must be YYYY-MM")
    if first.month == 12:
        next_month = date(first.year + 1, 1, 1)
    else:
        next_month = date(first.year, first.month + 1, 1)
    last = next_month - timedelta(days=1)
    settings = await get_settings(auth["user_id"])
    manual = await get_manual_days(auth["user_id"], first, last)
    days = []
    day = first
    while day <= last:
        days.append({"date": day.isoformat(), "work": is_workday(day, settings, manual)})
        day += timedelta(days=1)
    return {"month": month, "days": days, "settings": settings}


@app.post("/api/manual-day")
async def api_manual_day(request: Request):
    auth = validate_init_data(request.headers.get("X-Telegram-Init-Data", ""))
    payload = await request.json()
    try:
        day = date.fromisoformat(payload["date"])
    except Exception:
        raise HTTPException(400, "Invalid date")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT schedule_type FROM settings WHERE user_id=?", (auth["user_id"],))
        row = await cur.fetchone()
        if not row:
            await get_settings(auth["user_id"])
            cur = await db.execute("SELECT schedule_type FROM settings WHERE user_id=?", (auth["user_id"],))
            row = await cur.fetchone()
        if row[0] != "manual":
            raise HTTPException(400, "Switch to manual schedule first")
        cur = await db.execute("SELECT 1 FROM manual_days WHERE user_id=? AND work_date=?",
                               (auth["user_id"], day.isoformat()))
        exists = await cur.fetchone()
        if exists:
            await db.execute("DELETE FROM manual_days WHERE user_id=? AND work_date=?",
                             (auth["user_id"], day.isoformat()))
        else:
            await db.execute("INSERT INTO manual_days(user_id, work_date) VALUES(?,?)",
                             (auth["user_id"], day.isoformat()))
        await db.commit()
    return {"ok": True}


async def check_reminders():
    if not bot:
        return
    # Query users with enabled notifications.
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM settings WHERE enabled=1")
        users = [dict(r) for r in await cur.fetchall()]
    for settings in users:
        user_id = settings["user_id"]
        try:
            local_now = now_for(settings["timezone"])
            today = local_now.date()
            start = today - timedelta(days=1)
            end = today + timedelta(days=2)
            manual = await get_manual_days(user_id, start, end)
            # Evening reminder uses configurable local time.
            if settings["reminder_evening"] and (
                local_now.hour == settings["reminder_hour"]
                and local_now.minute == settings["reminder_minute"]
                and local_now.second < 30
            ):
                tomorrow = today + timedelta(days=1)
                if is_workday(tomorrow, settings, manual):
                    await send_once(user_id, tomorrow, "evening",
                                    "🌙 Напоминание: завтра рабочая смена. Проверь время выхода и подготовься.")
            if local_now.minute < 1:
                target = local_now + timedelta(hours=settings["reminder_hours"])
                if target.date() == today and is_workday(today, settings, manual):
                    if target.hour == 8 and target.minute < 1:
                        await send_once(user_id, today, "before",
                                        f"⏰ Напоминание: смена сегодня начинается в 08:00. До начала примерно {settings['reminder_hours']} ч.")
        except Exception:
            log.exception("Reminder check failed for user %s", user_id)


async def send_once(user_id: int, work_date: date, kind: str, text: str):
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute("INSERT INTO sent_reminders(user_id, work_date, reminder_type) VALUES(?,?,?)",
                             (user_id, work_date.isoformat(), kind))
            await db.commit()
        except aiosqlite.IntegrityError:
            return
    try:
        await bot.send_message(user_id, text)
    except TelegramAPIError:
        log.exception("Could not send reminder to %s", user_id)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
