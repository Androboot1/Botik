import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime, date, time, timedelta

import aiosqlite
from aiogram import Bot, Dispatcher, Router, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# =========================================================================================
#                                       КОНФИГУРАЦИЯ
# =========================================================================================

BOT_TOKEN = os.getenv("TOKEN_BOT" "8626189036:AAGohmvmej19pb473UtdJEe0KiH9y1ADVO0")
_admin_ids_raw = os.getenv("ADMIN_IDS", "8192234988").strip()
if _admin_ids_raw:
    ADMIN_IDS = {int(x.strip()) for x in _admin_ids_raw.split(",") if x.strip()}
else:
    ADMIN_IDS = set()

DB_PATH = os.getenv("DB_PATH", "bookings.db")

# Рабочие часы и параметры слотов
WORK_START = time(9, 0)
WORK_END = time(20, 0)
SLOT_STEP_MINUTES = 30
MIN_BOOKING_LEAD_MINUTES = 30
DAYS_AHEAD_CLIENT = 7
DAYS_AHEAD_ADMIN = 14

REMINDER_CHECK_INTERVAL_SECONDS = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("booking_bot")

RU_MONTHS_GEN = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля", 5: "мая", 6: "июня",
    7: "июля", 8: "августа", 9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}
RU_WEEKDAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

STATUS_LABELS = {
    "confirmed": "✅ Подтверждена",
    "completed": "✔️ Завершена",
    "cancelled": "❌ Отменена",
}

PHONE_RE = re.compile(r"^\+?[0-9\s\-\(\)]{10,18}$")

# =========================================================================================
#                                      FSM СОСТОЯНИЯ
# =========================================================================================

class BookingStates(StatesGroup):
    choosing_service = State()
    choosing_master = State()
    choosing_date = State()
    choosing_time = State()
    entering_name = State()
    entering_phone = State()
    confirming = State()


class AdminAddServiceStates(StatesGroup):
    entering_name = State()
    entering_price_ton = State()
    entering_price_uah = State()
    entering_duration = State()


class AdminBlockSlotStates(StatesGroup):
    choosing_master = State()
    choosing_date = State()
    choosing_mode = State()
    entering_start = State()
    entering_end = State()
    entering_reason = State()


# =========================================================================================
#                                  CALLBACK DATA ФАБРИКИ
# =========================================================================================

class ServiceCB(CallbackData, prefix="svc"):
    id: int

class MasterCB(CallbackData, prefix="mst"):
    id: int

class DateCB(CallbackData, prefix="dt"):
    date: str  # YYYY-MM-DD

class TimeCB(CallbackData, prefix="tm"):
    time: str  # HH:MM

class ConfirmBookingCB(CallbackData, prefix="cnf"):
    action: str  # yes / no

class NavCB(CallbackData, prefix="nav"):
    to: str  # main / services / masters / dates / times / mybookings / admin

class MyBookingCB(CallbackData, prefix="mybk"):
    id: int
    action: str  # ask_cancel / do_cancel / keep

class AdminServiceCB(CallbackData, prefix="asvc"):
    id: int
    action: str  # delete

class AdminApptCB(CallbackData, prefix="aapt"):
    id: int
    action: str  # confirm / complete / cancel

class AdminDateCB(CallbackData, prefix="adt"):
    date: str
    scope: str  # view / block

class AdminMasterPickCB(CallbackData, prefix="amp"):
    id: int
    scope: str  # block

class AdminBlockModeCB(CallbackData, prefix="abm"):
    mode: str  # full_day / custom


# =========================================================================================
#                                    БАЗА ДАННЫХ
# =========================================================================================

db: aiosqlite.Connection | None = None

async def init_db() -> None:
    global db
    db = await aiosqlite.connect(DB_PATH)
    await db.execute("PRAGMA journal_mode=WAL;")
    await db.execute("PRAGMA foreign_keys=ON;")
    await db.execute("PRAGMA busy_timeout=5000;")

    await db.execute("""
        CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price_ton REAL NOT NULL,
            price_uah REAL NOT NULL,
            duration_minutes INTEGER NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        );
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS masters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        );
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS schedule_blocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            master_id INTEGER NOT NULL,
            block_date TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            reason TEXT,
            FOREIGN KEY (master_id) REFERENCES masters(id)
        );
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_telegram_id INTEGER NOT NULL,
            client_name TEXT NOT NULL,
            client_phone TEXT NOT NULL,
            service_id INTEGER NOT NULL,
            master_id INTEGER NOT NULL,
            appointment_date TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'confirmed',
            reminder_sent INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (service_id) REFERENCES services(id),
            FOREIGN KEY (master_id) REFERENCES masters(id)
        );
    """)
    await db.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_slot
        ON appointments(master_id, appointment_date, start_time)
        WHERE status != 'cancelled';
    """)
    await db.commit()

    cur = await db.execute("SELECT COUNT(*) FROM services")
    (count,) = await cur.fetchone()
    if count == 0:
        await db.executemany(
            "INSERT INTO services (name, price_ton, price_uah, duration_minutes, active) VALUES (?,?,?,?,1)",
            [
                ("Мужская стрижка", 3.5, 450, 40),
                ("Стрижка + борода", 5.0, 650, 60),
                ("Королевское бритьё", 4.0, 500, 45),
                ("Детская стрижка", 2.5, 300, 30),
            ],
        )
        await db.commit()

    cur = await db.execute("SELECT COUNT(*) FROM masters")
    (count,) = await cur.fetchone()
    if count == 0:
        await db.executemany(
            "INSERT INTO masters (name, active) VALUES (?,1)",
            [("Александр",), ("Дмитрий",), ("Игорь",)],
        )
        await db.commit()

    logger.info("База данных инициализирована: %s", DB_PATH)


async def fetchall(query: str, params: tuple = ()) -> list:
    assert db is not None
    async with db.execute(query, params) as cur:
        return await cur.fetchall()


async def fetchone(query: str, params: tuple = ()):
    assert db is not None
    async with db.execute(query, params) as cur:
        return await cur.fetchone()


async def execute(query: str, params: tuple = ()) -> int:
    assert db is not None
    cur = await db.execute(query, params)
    await db.commit()
    return cur.lastrowid


# =========================================================================================
#                                  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =========================================================================================

def human_date(d: date) -> str:
    return f"{d.day} {RU_MONTHS_GEN[d.month]} ({RU_WEEKDAYS_SHORT[d.weekday()]})"

def parse_time_str(s: str) -> time:
    return datetime.strptime(s, "%H:%M").time()

def parse_date_str(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()

def is_valid_phone(text: str) -> bool:
    return bool(PHONE_RE.match(text.strip()))

def is_valid_name(text: str) -> bool:
    text = text.strip()
    return 2 <= len(text) <= 60 and not text.isdigit()

async def get_available_slots(master_id: int, target_date: date, duration_minutes: int) -> list[str]:
    booked_rows = await fetchall(
        "SELECT start_time, end_time FROM appointments "
        "WHERE master_id=? AND appointment_date=? AND status!='cancelled'",
        (master_id, target_date.isoformat()),
    )
    block_rows = await fetchall(
        "SELECT start_time, end_time FROM schedule_blocks WHERE master_id=? AND block_date=?",
        (master_id, target_date.isoformat()),
    )

    busy_intervals = []
    for r in booked_rows:
        busy_intervals.append((parse_time_str(r[0]), parse_time_str(r[1])))
    for r in block_rows:
        busy_intervals.append((parse_time_str(r[0]), parse_time_str(r[1])))

    slots: list[str] = []
    now_dt = datetime.now()
    is_today = target_date == now_dt.date()

    cursor_dt = datetime.combine(target_date, WORK_START)
    end_dt = datetime.combine(target_date, WORK_END)
    step = timedelta(minutes=SLOT_STEP_MINUTES)
    duration = timedelta(minutes=duration_minutes)

    while cursor_dt + duration <= end_dt:
        slot_start_t = cursor_dt.time()
        slot_end_t = (cursor_dt + duration).time()

        conflict = False
        for b_start, b_end in busy_intervals:
            if slot_start_t < b_end and b_start < slot_end_t:
                conflict = True
                break

        if not conflict:
            if not (is_today and cursor_dt <= now_dt + timedelta(minutes=MIN_BOOKING_LEAD_MINUTES)):
                slots.append(slot_start_t.strftime("%H:%M"))

        cursor_dt += step

    return slots

async def get_service(service_id: int):
    return await fetchone(
        "SELECT id, name, price_ton, price_uah, duration_minutes FROM services WHERE id=? AND active=1",
        (service_id,),
    )

async def get_master(master_id: int):
    return await fetchone("SELECT id, name FROM masters WHERE id=? AND active=1", (master_id,))


# =========================================================================================
#                                       КЛАВИАТУРЫ
# =========================================================================================

def kb_main_menu(is_admin: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📝 Записаться", callback_data=NavCB(to="services"))
    b.button(text="📖 Мои записи", callback_data=NavCB(to="mybookings"))
    if is_admin:
        b.button(text="⚙️ Админ-панель", callback_data=NavCB(to="admin"))
    b.adjust(1)
    return b.as_markup()

async def kb_services() -> InlineKeyboardMarkup:
    rows = await fetchall(
        "SELECT id, name, price_ton, price_uah, duration_minutes FROM services WHERE active=1 ORDER BY id"
    )
    b = InlineKeyboardBuilder()
    for sid, name, ton, uah, dur in rows:
        b.button(
            text=f"{name} — {ton:g} TON / {uah:g} UAH · {dur} мин",
            callback_data=ServiceCB(id=sid),
        )
    b.button(text="◀️ Главное меню", callback_data=NavCB(to="main"))
    b.adjust(1)
    return b.as_markup()

async def kb_masters() -> InlineKeyboardMarkup:
    rows = await fetchall("SELECT id, name FROM masters WHERE active=1 ORDER BY id")
    b = InlineKeyboardBuilder()
    for mid, name in rows:
        b.button(text=f"👤 {name}", callback_data=MasterCB(id=mid))
    b.button(text="◀️ Назад к услугам", callback_data=NavCB(to="services"))
    b.adjust(1)
    return b.as_markup()

def kb_dates_client() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    today = date.today()
    for i in range(DAYS_AHEAD_CLIENT):
        d = today + timedelta(days=i)
        b.button(text=human_date(d), callback_data=DateCB(date=d.isoformat()))
    b.button(text="◀️ Назад к мастерам", callback_data=NavCB(to="masters"))
    b.adjust(2)
    return b.as_markup()

def kb_times(slots: list[str]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for s in slots:
        b.button(text=s, callback_data=TimeCB(time=s))
    b.button(text="◀️ Назад к датам", callback_data=NavCB(to="dates"))
    b.adjust(4)
    return b.as_markup()

def kb_confirm() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Подтвердить запись", callback_data=ConfirmBookingCB(action="yes"))
    b.button(text="❌ Отменить", callback_data=ConfirmBookingCB(action="no"))
    b.adjust(1)
    return b.as_markup()

def kb_phone_request() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить номер телефона", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

def kb_admin_main() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📋 Управление услугами", callback_data=NavCB(to="admin_services"))
    b.button(text="📅 Просмотр записей", callback_data=NavCB(to="admin_appts_menu"))
    b.button(text="🚫 Блокировка слотов", callback_data=NavCB(to="admin_block_start"))
    b.button(text="📊 Статистика", callback_data=NavCB(to="admin_stats"))
    b.button(text="◀️ Главное меню", callback_data=NavCB(to="main"))
    b.adjust(1)
    return b.as_markup()

async def kb_admin_services() -> InlineKeyboardMarkup:
    rows = await fetchall(
        "SELECT id, name, price_ton, price_uah, duration_minutes FROM services WHERE active=1 ORDER BY id"
    )
    b = InlineKeyboardBuilder()
    for sid, name, ton, uah, dur in rows:
        b.button(
            text=f"🗑 {name} ({ton:g} TON / {uah:g} UAH, {dur} мин)",
            callback_data=AdminServiceCB(id=sid, action="delete"),
        )
    b.button(text="➕ Добавить услугу", callback_data=NavCB(to="admin_add_service"))
    b.button(text="◀️ Назад", callback_data=NavCB(to="admin"))
    b.adjust(1)
    return b.as_markup()

def kb_admin_appts_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    today = date.today()
    tomorrow = today + timedelta(days=1)
    b.button(text=f"Сегодня ({human_date(today)})", callback_data=AdminDateCB(date=today.isoformat(), scope="view"))
    b.button(text=f"Завтра ({human_date(tomorrow)})", callback_data=AdminDateCB(date=tomorrow.isoformat(), scope="view"))
    b.button(text="📆 Выбрать дату", callback_data=NavCB(to="admin_pick_date_view"))
    b.button(text="◀️ Назад", callback_data=NavCB(to="admin"))
    b.adjust(1)
    return b.as_markup()

def kb_admin_pick_date(scope: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    today = date.today()
    for i in range(DAYS_AHEAD_ADMIN):
        d = today + timedelta(days=i)
        b.button(text=human_date(d), callback_data=AdminDateCB(date=d.isoformat(), scope=scope))
    b.button(text="◀️ Назад", callback_data=NavCB(to="admin_appts_menu" if scope == "view" else "admin_block_start"))
    b.adjust(2)
    return b.as_markup()

async def kb_admin_pick_master(scope: str) -> InlineKeyboardMarkup:
    rows = await fetchall("SELECT id, name FROM masters WHERE active=1 ORDER BY id")
    b = InlineKeyboardBuilder()
    for mid, name in rows:
        b.button(text=f"👤 {name}", callback_data=AdminMasterPickCB(id=mid, scope=scope))
    b.button(text="◀️ Назад", callback_data=NavCB(to="admin"))
    b.adjust(1)
    return b.as_markup()

def kb_admin_block_mode() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🌑 Заблокировать весь день", callback_data=AdminBlockModeCB(mode="full_day"))
    b.button(text="⏱ Указать период времени", callback_data=AdminBlockModeCB(mode="custom"))
    b.adjust(1)
    return b.as_markup()

def kb_admin_appt_actions(appt_id: int, status: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if status == "confirmed":
        b.button(text="✔️ Завершить", callback_data=AdminApptCB(id=appt_id, action="complete"))
        b.button(text="❌ Отменить", callback_data=AdminApptCB(id=appt_id, action="cancel"))
    elif status in ("completed", "cancelled"):
        b.button(text="↩️ Вернуть в «Подтверждена»", callback_data=AdminApptCB(id=appt_id, action="confirm"))
    b.adjust(1)
    return b.as_markup()

def kb_my_booking_actions(appt_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отменить запись", callback_data=MyBookingCB(id=appt_id, action="ask_cancel"))
    b.adjust(1)
    return b.as_markup()

def kb_confirm_cancel_booking(appt_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Да, отменить", callback_data=MyBookingCB(id=appt_id, action="do_cancel"))
    b.button(text="Нет, оставить", callback_data=MyBookingCB(id=appt_id, action="keep"))
    b.adjust(1)
    return b.as_markup()


# =========================================================================================
#                                        РОУТЕРЫ
# =========================================================================================

router = Router(name="client")
admin_router = Router(name="admin")

# =========================================================================================
#                                  ХЕНДЛЕРЫ КЛИЕНТА
# =========================================================================================

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    is_admin = message.from_user.id in ADMIN_IDS
    await message.answer(
        "👋 **Добро пожаловать в сервис онлайн-записи!**\n\n"
        "Выберите нужное действие в меню ниже:",
        reply_markup=kb_main_menu(is_admin),
        parse_mode=ParseMode.MARKDOWN,
    )

@router.callback_query(NavCB.filter(F.to == "main"))
async def nav_main(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    is_admin = callback.from_user.id in ADMIN_IDS
    await callback.message.edit_text(
        "🏠 **Главное меню**\n\nВыберите действие:",
        reply_markup=kb_main_menu(is_admin),
        parse_mode=ParseMode.MARKDOWN,
    )
    await callback.answer()

@router.callback_query(NavCB.filter(F.to == "services"))
async def process_services_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BookingStates.choosing_service)
    await callback.message.edit_text(
        "📋 **Выберите услугу:**",
        reply_markup=await kb_services(),
        parse_mode=ParseMode.MARKDOWN,
    )
    await callback.answer()

@router.callback_query(ServiceCB.filter(), BookingStates.choosing_service)
async def process_service_choice(callback: CallbackQuery, callback_data: ServiceCB, state: FSMContext) -> None:
    service = await get_service(callback_data.id)
    if not service:
        await callback.answer("❌ Услуга не найдена или неактивна.", show_alert=True)
        return

    await state.update_data(service_id=service[0], service_name=service[1], price_ton=service[2], price_uah=service[3], duration=service[4])
    await state.set_state(BookingStates.choosing_master)
    await callback.message.edit_text(
        f"Выбрана услуга: **{service[1]}**\n\n👤 **Теперь выберите мастера:**",
        reply_markup=await kb_masters(),
        parse_mode=ParseMode.MARKDOWN,
    )
    await callback.answer()

@router.callback_query(MasterCB.filter(), BookingStates.choosing_master)
async def process_master_choice(callback: CallbackQuery, callback_data: MasterCB, state: FS