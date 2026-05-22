import logging
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, UploadFile, File, Form, Request, Depends, HTTPException, Header
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .database import SessionLocal, engine, Base, DATA_DIR
from .models import FoodEntry
from .gemini_client import analyze_food_image
from .image_utils import compress_image

logger = logging.getLogger(__name__)


# --- Client timezone helpers ---

TZ_UTC = ZoneInfo("UTC")


def _get_client_tz(x_timezone: str | None = Header(None, alias="X-Timezone")) -> ZoneInfo:
    """Parse the client's timezone from the X-Timezone header.

    Falls back to UTC if not provided or invalid.
    """
    if x_timezone:
        try:
            return ZoneInfo(x_timezone)
        except Exception:
            logger.warning("Invalid timezone '%s', falling back to UTC", x_timezone)
    return TZ_UTC


def _today(tz: ZoneInfo) -> date:
    """Get today's date in the given timezone."""
    return datetime.now(tz).date()


def _now_str(tz: ZoneInfo) -> str:
    """Get the current time as HH:MM in the given timezone."""
    return datetime.now(tz).strftime("%H:%M")

# --- Paths ---
BASE_DIR = Path(__file__).parent
IMAGES_DIR = DATA_DIR / "images"
IMAGES_DIR.mkdir(parents=True, exist_ok=True)

# --- FastAPI ---
app = FastAPI(title="Calorie Tracker")

# Mount static files (for served images)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.mount("/images", StaticFiles(directory=str(IMAGES_DIR)), name="images")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# --- Startup ---
@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables created")


# --- Helpers ---
def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _get_day_data(db: Session, day: str) -> tuple[list[FoodEntry], dict]:
    """Get entries and totals for a given date string (YYYY-MM-DD)."""
    entries = (
        db.query(FoodEntry)
        .filter(FoodEntry.date == day)
        .order_by(FoodEntry.time)
        .all()
    )
    totals = {
        "calories": sum(e.calories for e in entries),
        "protein": sum(e.protein_g for e in entries),
        "carbs": sum(e.carbs_g for e in entries),
        "fat": sum(e.fat_g for e in entries),
    }
    return entries, totals


def _entry_to_dict(entry: FoodEntry) -> dict:
    return {
        "id": entry.id,
        "date": entry.date,
        "time": entry.time,
        "food_name": entry.food_name,
        "description": entry.description or "",
        "calories": entry.calories,
        "protein_g": entry.protein_g,
        "carbs_g": entry.carbs_g,
        "fat_g": entry.fat_g,
        "serving_size": entry.serving_size or "",
        "confidence": entry.confidence or "unknown",
        "image_url": f"/images/{entry.image_path}" if entry.image_path else None,
    }


def _get_recent_dates(db: Session, tz: ZoneInfo) -> list[dict]:
    """Build the date-strip data for the last 7 days in the client's timezone."""
    today = _today(tz)
    result = []
    for i in range(7):
        d = today - timedelta(days=i)
        count = db.query(FoodEntry).filter(FoodEntry.date == d.isoformat()).count()
        if i == 0:
            label = "Today"
        elif i == 1:
            label = "Yesterday"
        elif i <= 3:
            label = d.strftime("%A")  # Monday, Tuesday, etc.
        else:
            label = d.strftime("%b ") + str(d.day)  # May 19
        result.append({
            "date": d.isoformat(),
            "label": label,
            "has_entries": count > 0,
        })
    return result


# --- Routes ---

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, db: Session = Depends(_get_db), tz: ZoneInfo = Depends(_get_client_tz)):
    today_str = _today(tz).isoformat()
    entries, totals = _get_day_data(db, today_str)
    recent_dates = _get_recent_dates(db, tz)

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "today": today_str,
            "entries": [_entry_to_dict(e) for e in entries],
            "totals": totals,
            "recent_dates": recent_dates,
        },
    )


@app.post("/upload")
async def upload_food(
    request: Request,
    image: UploadFile = File(...),
    description: str = Form(""),
    db: Session = Depends(_get_db),
    tz: ZoneInfo = Depends(_get_client_tz),
):
    if not image.content_type or not image.content_type.startswith("image/"):
        raise HTTPException(400, "File must be an image")

    # Save compressed image
    image_id = str(uuid.uuid4())
    now_dt = datetime.now(tz)
    today_str = now_dt.date().isoformat()
    day_dir = IMAGES_DIR / today_str
    day_dir.mkdir(parents=True, exist_ok=True)

    compressed_path = day_dir / f"{image_id}.jpg"
    contents = await image.read()
    compress_image(contents, compressed_path)

    # Analyze with Gemini
    try:
        analysis = analyze_food_image(compressed_path, description)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Gemini analysis failed: %s", e)
        error_str = str(e)
        if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str or "quota" in error_str.lower():
            raise HTTPException(
                429,
                "⚠️ Gemini API quota exceeded. The free tier resets daily. "
                "Wait a bit and try again, or enable billing at https://aistudio.google.com/apikey",
            )
        raise HTTPException(500, f"AI analysis failed: {error_str[:200]}")

    # Save to database
    entry = FoodEntry(
        date=today_str,
        time=_now_str(tz),
        food_name=analysis.food_name,
        description=description or None,
        calories=analysis.estimated_calories,
        protein_g=analysis.protein_g,
        carbs_g=analysis.carbs_g,
        fat_g=analysis.fat_g,
        serving_size=analysis.serving_size,
        confidence=analysis.confidence,
        image_path=f"{today_str}/{image_id}.jpg",
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)

    # Check if HTMX request — return partial if so
    if request.headers.get("hx-request") == "true":
        entries, totals = _get_day_data(db, today_str)
        return templates.TemplateResponse(
            request,
            "partials/_entry_list.html",
            {
                "entries": [_entry_to_dict(e) for e in entries],
                "totals": totals,
            },
        )

    return RedirectResponse(url="/", status_code=303)


@app.get("/day/{day_date}", response_class=HTMLResponse)
async def view_day(request: Request, day_date: str, db: Session = Depends(_get_db), tz: ZoneInfo = Depends(_get_client_tz)):
    entries, totals = _get_day_data(db, day_date)
    today_str = _today(tz).isoformat()
    yesterday_str = (_today(tz) - timedelta(days=1)).isoformat()
    label = "Today" if day_date == today_str else "Yesterday" if day_date == yesterday_str else day_date

    # HTMX: return only the entry list partial (no header)
    if request.headers.get("hx-request") == "true":
        return templates.TemplateResponse(
            request,
            "partials/_entry_list.html",
            {
                "today": day_date,
                "label": label,
                "entries": [_entry_to_dict(e) for e in entries],
                "totals": totals,
            },
        )

    # Full page load: render with base template
    recent_dates = _get_recent_dates(db, tz)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "today": day_date,
            "label": label,
            "entries": [_entry_to_dict(e) for e in entries],
            "totals": totals,
            "recent_dates": recent_dates,
        },
    )


@app.delete("/entries/{entry_id}")
async def delete_entry(entry_id: int, request: Request, db: Session = Depends(_get_db), tz: ZoneInfo = Depends(_get_client_tz)):
    entry = db.query(FoodEntry).filter(FoodEntry.id == entry_id).first()
    if not entry:
        raise HTTPException(404, "Entry not found")

    # Delete the image file
    if entry.image_path:
        img_file = IMAGES_DIR / entry.image_path
        if img_file.exists():
            img_file.unlink()

    db.delete(entry)
    db.commit()

    if request.headers.get("hx-request") == "true":
        # Determine which day we're viewing from the referrer URL
        referer = request.headers.get("referer", "")
        if "/day/" in referer:
            current_day = referer.rsplit("/day/", 1)[-1].split("?")[0]
        else:
            current_day = _today(tz).isoformat()
        entries, totals = _get_day_data(db, current_day)
        return templates.TemplateResponse(
            request,
            "partials/_entry_list.html",
            {
                "today": current_day,
                "label": "",
                "entries": [_entry_to_dict(e) for e in entries],
                "totals": totals,
            },
        )

    return RedirectResponse(url="/", status_code=303)
