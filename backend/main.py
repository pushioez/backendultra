from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager, asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Literal, Optional

import httpx
import threading
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, constr
from dotenv import load_dotenv


load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "salon_bookings.sqlite3")))
FRONTEND_DIR = BASE_DIR / "frontend"
HAS_FRONTEND = FRONTEND_DIR.exists()

# Ensure DB directory exists (helps on Render when using mounted disks)
try:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
except Exception:
    pass


SLOT_RANGES: List[str] = [
    "11:00-12:00",
    "12:00-13:00",
    "13:00-14:00",
    "14:00-15:00",
]


@contextmanager
def get_db():
    # Render may have restricted write access in the project folder.
    # If the configured DB_PATH is not writable, fall back to /tmp.
    candidate_paths = [DB_PATH, Path("/tmp/salon_bookings.sqlite3")]
    last_err: Optional[Exception] = None
    conn = None
    for p in candidate_paths:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(p)
            break
        except Exception as e:  # pragma: no cover
            last_err = e
            conn = None
    if conn is None:
        raise RuntimeError(f"Could not open SQLite DB. Last error: {last_err}")
    try:
        conn.row_factory = sqlite3.Row
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                booking_date TEXT NOT NULL,
                time_range TEXT NOT NULL,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                tg_user_id INTEGER,
                created_at TEXT NOT NULL,
                UNIQUE (booking_date, time_range)
            )
            """
        )
        # Migration for older databases created before tg_user_id existed
        try:
            conn.execute("ALTER TABLE bookings ADD COLUMN tg_user_id INTEGER")
        except sqlite3.OperationalError:
            pass


# ✅ Современная инициализация вместо on_event
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # If Background Worker is not available on Render (paid option),
    # we can run the Telegram bot inside the web service process.
    should_run_bot = bool(TELEGRAM_BOT_TOKEN) and os.getenv(
        "RUN_TELEGRAM_BOT_IN_WEB", "1"
    ).lower() not in {"0", "false", "no"}
    if should_run_bot and not getattr(app.state, "bot_thread_started", False):
        try:
            try:
                from backend.bot import run_polling_blocking
            except Exception:
                # When Render Root Directory is set to "backend",
                # the module is imported as "bot" instead.
                from bot import run_polling_blocking

            def _bot_runner():
                try:
                    run_polling_blocking()
                except Exception:
                    # Don't crash the web service if bot fails.
                    return

            t = threading.Thread(target=_bot_runner, daemon=True)
            t.start()
            app.state.bot_thread_started = True
        except Exception:
            # Ignore bot start errors; /start won't work then.
            pass
    yield


app = FastAPI(
    title="Beauty Salon MiniApp Backend",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


if HAS_FRONTEND:
    app.mount(
        "/static",
        StaticFiles(directory=FRONTEND_DIR),
        name="static",
    )


class SlotStatus(BaseModel):
    time_range: str
    status: Literal["free", "booked"]


class SlotsResponse(BaseModel):
    date: date
    slots: List[SlotStatus]


class BookingCreate(BaseModel):
    booking_date: date = Field(..., alias="date")
    time_range: str
    name: constr(strip_whitespace=True, min_length=1)
    phone: constr(pattern=r"^\+7\d{10}$")
    tg_user_id: Optional[int] = None


class Booking(BaseModel):
    id: int
    date: date
    time_range: str
    name: str
    phone: str
    tg_user_id: Optional[int] = None


if HAS_FRONTEND:

    @app.get("/", response_class=FileResponse)
    def serve_frontend_root():
        index_path = FRONTEND_DIR / "index.html"
        if not index_path.exists():
            raise HTTPException(status_code=404, detail="Frontend not found.")
        return FileResponse(index_path)


@app.get("/api/slots", response_model=SlotsResponse)
def get_slots(date_str: str = Query(..., alias="date")):
    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format.")

    today = date.today()
    if target_date < today or target_date > today + timedelta(days=30):
        raise HTTPException(status_code=400, detail="Bookings only up to 1 month ahead.")
    if target_date.weekday() >= 5:
        raise HTTPException(status_code=400, detail="Only Mon-Fri available.")

    with get_db() as conn:
        rows = conn.execute(
            "SELECT time_range FROM bookings WHERE booking_date = ?",
            (target_date.isoformat(),),
        ).fetchall()
        booked = {row["time_range"] for row in rows}

    slots = [
        SlotStatus(time_range=slot, status="booked" if slot in booked else "free")
        for slot in SLOT_RANGES
    ]

    return SlotsResponse(date=target_date, slots=slots)


@app.post("/api/book", response_model=Booking)
def create_booking(payload: BookingCreate):
    booking_date = payload.booking_date
    today = date.today()

    if booking_date < today or booking_date > today + timedelta(days=30):
        raise HTTPException(status_code=400, detail="Bookings only up to 1 month ahead.")
    if booking_date.weekday() >= 5:
        raise HTTPException(status_code=400, detail="Only Mon-Fri available.")
    if payload.time_range not in SLOT_RANGES:
        raise HTTPException(status_code=400, detail="Invalid time range.")

    with get_db() as conn:
        now_str = datetime.utcnow().isoformat()
        try:
            cursor = conn.execute(
                """
                INSERT INTO bookings (booking_date, time_range, name, phone, tg_user_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    booking_date.isoformat(),
                    payload.time_range,
                    payload.name,
                    payload.phone,
                    payload.tg_user_id,
                    now_str,
                ),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Slot already booked.")

        booking_id = cursor.lastrowid
        row = conn.execute(
            "SELECT id, booking_date, time_range, name, phone, tg_user_id FROM bookings WHERE id = ?",
            (booking_id,),
        ).fetchone()

    booking = Booking(
        id=row["id"],
        date=datetime.fromisoformat(row["booking_date"]).date(),
        time_range=row["time_range"],
        name=row["name"],
        phone=row["phone"],
        tg_user_id=row["tg_user_id"],
    )
    if booking.tg_user_id:
        _send_telegram_message(
            booking.tg_user_id,
            f"Booking confirmed ✅\nDate: {booking.date.strftime('%d.%m.%Y')}\nTime: {booking.time_range}",
        )
    return booking


@app.get("/api/bookings", response_model=List[Booking])
def list_bookings(phone: Optional[str] = None):
    params = []
    query = "SELECT id, booking_date, time_range, name, phone, tg_user_id FROM bookings"

    if phone:
        query += " WHERE phone = ?"
        params.append(phone)

    with get_db() as conn:
        rows = conn.execute(query, params).fetchall()

    return [
        Booking(
            id=row["id"],
            date=datetime.fromisoformat(row["booking_date"]).date(),
            time_range=row["time_range"],
            name=row["name"],
            phone=row["phone"],
            tg_user_id=row["tg_user_id"],
        )
        for row in rows
    ]


@app.delete("/api/bookings/{booking_id}")
def cancel_booking(booking_id: int, phone: Optional[str] = None):
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id, booking_date, time_range, tg_user_id FROM bookings WHERE id = ?",
            (booking_id,),
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Booking not found.")

        if phone:
            row = conn.execute(
                "SELECT id FROM bookings WHERE id = ? AND phone = ?",
                (booking_id, phone),
            ).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Booking not found for this phone.")
            conn.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
        else:
            deleted = conn.execute(
                "DELETE FROM bookings WHERE id = ?",
                (booking_id,),
            )
            if deleted.rowcount == 0:
                raise HTTPException(status_code=404, detail="Booking not found.")

    tg_user_id = existing["tg_user_id"]
    if tg_user_id:
        d = datetime.fromisoformat(existing["booking_date"]).date()
        _send_telegram_message(
            tg_user_id,
            f"Booking canceled ❌\nDate: {d.strftime('%d.%m.%Y')}\nTime: {existing['time_range']}",
        )
    return {"status": "ok"}


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MINIAPP_URL = os.getenv("MINIAPP_URL", "http://127.0.0.1:8000/")


def _send_telegram_message(tg_user_id: int, text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not tg_user_id:
        return
    try:
        with httpx.Client(timeout=5.0) as client:
            client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": tg_user_id, "text": text},
            )
    except Exception:
        return


@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "telegram_configured": bool(TELEGRAM_BOT_TOKEN),
        "miniapp_url": MINIAPP_URL,
    }