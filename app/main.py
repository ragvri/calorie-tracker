import logging
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, UploadFile, File, Form, Request, Depends, HTTPException, Header, Cookie, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session

from .database import SessionLocal, engine, Base, DATA_DIR
from .models import FoodEntry, WeightEntry
from .gemini_client import analyze_food_image, refine_food_estimate
from .image_utils import compress_image

logger = logging.getLogger(__name__)


# --- Client timezone helpers ---

TZ_UTC = ZoneInfo("UTC")


def _get_client_tz(
    x_timezone: str | None = Header(None, alias="X-Timezone"),
    tz_cookie: str | None = Cookie(None, alias="tz"),
) -> ZoneInfo:
    """Parse the client's timezone from the X-Timezone header or tz cookie.

    Priority: header > cookie > UTC fallback.
    """
    candidates = []
    if x_timezone:
        candidates.append(("header", x_timezone))
    if tz_cookie:
        candidates.append(("cookie", tz_cookie))

    for source, tz_str in candidates:
        try:
            return ZoneInfo(tz_str)
        except Exception:
            logger.warning("Invalid timezone from %s: '%s'", source, tz_str)

    return TZ_UTC


def _get_calorie_goal(
    goal_cookie: str | None = Cookie(None, alias="calorie_goal"),
) -> int:
    """Read the daily calorie goal from a cookie, default 2000."""
    if goal_cookie and goal_cookie.lstrip("-").isdigit():
        g = int(goal_cookie)
        if 500 <= g <= 10000:
            return g
    return 2000


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
    # Migration: add reasoning column if it doesn't exist yet
    with engine.connect() as conn:
        cols = [row[1] for row in conn.execute(text("PRAGMA table_info(food_entries)")).fetchall()]
        if "reasoning" not in cols:
            conn.execute(text("ALTER TABLE food_entries ADD COLUMN reasoning TEXT"))
            conn.commit()
            logger.info("Added reasoning column to food_entries")
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
        "reasoning": entry.reasoning or "",
        "image_url": f"/images/{entry.image_path}" if entry.image_path else None,
    }


def _get_recent_dates(db: Session, tz: ZoneInfo) -> list[dict]:
    """Build the date-strip data for the last 7 days in the client's timezone."""
    today = _today(tz)
    result = []
    for i in range(-1, 6):  # -1=tomorrow, 0=today, 1=yesterday, ...
        d = today - timedelta(days=i)
        count = db.query(FoodEntry).filter(FoodEntry.date == d.isoformat()).count()
        if i == -1:
            label = "Tomorrow"
        elif i == 0:
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
async def index(request: Request, db: Session = Depends(_get_db), tz: ZoneInfo = Depends(_get_client_tz), goal: int = Depends(_get_calorie_goal)):
    today_str = _today(tz).isoformat()
    entries, totals = _get_day_data(db, today_str)
    recent_dates = _get_recent_dates(db, tz)

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "today": today_str,
            "goal": goal,
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
    local_date: str | None = Form(None),
    db: Session = Depends(_get_db),
    tz: ZoneInfo = Depends(_get_client_tz),
    goal: int = Depends(_get_calorie_goal),
):
    if not image.content_type or not image.content_type.startswith("image/"):
        raise HTTPException(400, "File must be an image")

    # Determine today's date in the client's timezone.
    # Three layers of fallback: timezone-aware datetime > hidden form field > UTC.
    image_id = str(uuid.uuid4())
    now_dt = datetime.now(tz)
    today_str = now_dt.date().isoformat()

    # If we fell back to UTC and the client sent a local_date form field that differs,
    # trust the client's local date (belt-and-suspenders for CDN / race issues).
    if tz is TZ_UTC and local_date and local_date != today_str:
        logger.info(
            "Overriding UTC date %s with client-reported date %s",
            today_str, local_date,
        )
        today_str = local_date
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
        reasoning=analysis.reasoning or None,
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
                "goal": goal,
                "entries": [_entry_to_dict(e) for e in entries],
                "totals": totals,
            },
        )

    return RedirectResponse(url="/", status_code=303)


@app.get("/day/{day_date}", response_class=HTMLResponse)
async def view_day(request: Request, day_date: str, db: Session = Depends(_get_db), tz: ZoneInfo = Depends(_get_client_tz), goal: int = Depends(_get_calorie_goal)):
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
                "goal": goal,
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
            "goal": goal,
            "entries": [_entry_to_dict(e) for e in entries],
            "totals": totals,
            "recent_dates": recent_dates,
        },
    )


@app.delete("/entries/{entry_id}")
async def delete_entry(entry_id: int, request: Request, db: Session = Depends(_get_db), tz: ZoneInfo = Depends(_get_client_tz), goal: int = Depends(_get_calorie_goal)):
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
                "goal": goal,
                "entries": [_entry_to_dict(e) for e in entries],
                "totals": totals,
            },
        )

    return RedirectResponse(url="/", status_code=303)


# ─── Weight Routes ────────────────────────────────────────────────


@app.post("/weight")
async def log_weight(
    request: Request,
    date: str = Form(...),
    weight_kg: float = Form(...),
    notes: str = Form(""),
    db: Session = Depends(_get_db),
):
    """Log a weight entry."""
    entry = WeightEntry(date=date, weight_kg=weight_kg, notes=notes or None)
    db.add(entry)
    db.commit()
    db.refresh(entry)

    if request.headers.get("hx-request") == "true":
        # Return the weight list partial
        weights = db.query(WeightEntry).order_by(WeightEntry.date.desc()).limit(20).all()
        return templates.TemplateResponse(
            request,
            "partials/_weight_list.html",
            {"weights": weights},
        )

    return RedirectResponse(url="/stats", status_code=303)


@app.delete("/weight/{weight_id}")
async def delete_weight(weight_id: int, request: Request, db: Session = Depends(_get_db)):
    entry = db.query(WeightEntry).filter(WeightEntry.id == weight_id).first()
    if not entry:
        raise HTTPException(404, "Weight entry not found")
    db.delete(entry)
    db.commit()

    if request.headers.get("hx-request") == "true":
        weights = db.query(WeightEntry).order_by(WeightEntry.date.desc()).limit(20).all()
        return templates.TemplateResponse(
            request,
            "partials/_weight_list.html",
            {"weights": weights},
        )

    return {"ok": True}


# ─── Stats / Analytics Routes ────────────────────────────────────


@app.get("/stats", response_class=HTMLResponse)
async def stats_page(
    request: Request,
    db: Session = Depends(_get_db),
    tz: ZoneInfo = Depends(_get_client_tz),
    goal: int = Depends(_get_calorie_goal),
):
    today_str = _today(tz).isoformat()
    recent_dates = _get_recent_dates(db, tz)

    # Get last 10 weight entries for the sidebar
    weights = db.query(WeightEntry).order_by(WeightEntry.date.desc()).limit(20).all()

    return templates.TemplateResponse(
        request,
        "stats.html",
        {
            "today": today_str,
            "goal": goal,
            "recent_dates": recent_dates,
            "weights": weights,
            "weight_count": db.query(WeightEntry).count(),
            "entry_count": db.query(FoodEntry).count(),
            "total_days": len(set(
                row[0] for row in db.query(FoodEntry.date).distinct().all()
            )),
        },
    )


@app.get("/stats/data/daily")
async def stats_daily_data(
    days: int = Query(30, ge=7, le=365),
    db: Session = Depends(_get_db),
):
    """JSON endpoint: daily totals for calories, protein, carbs, fat over N days."""
    from datetime import date as date_type, timedelta
    today = date_type.today()
    start = today - timedelta(days=days - 1)

    # Fetch all entries in date range
    entries = (
        db.query(FoodEntry)
        .filter(FoodEntry.date >= start.isoformat())
        .order_by(FoodEntry.date)
        .all()
    )

    # Aggregate by date
    daily: dict[str, dict] = {}
    for e in entries:
        if e.date not in daily:
            daily[e.date] = {
                "date": e.date,
                "calories": 0.0,
                "protein": 0.0,
                "carbs": 0.0,
                "fat": 0.0,
                "entry_count": 0,
            }
        daily[e.date]["calories"] += e.calories
        daily[e.date]["protein"] += e.protein_g
        daily[e.date]["carbs"] += e.carbs_g
        daily[e.date]["fat"] += e.fat_g
        daily[e.date]["entry_count"] += 1

    # Fill in missing days with zeroes
    result = []
    for i in range(days):
        d = (start + timedelta(days=i)).isoformat()
        if d in daily:
            result.append(daily[d])
        else:
            result.append({
                "date": d,
                "calories": 0.0,
                "protein": 0.0,
                "carbs": 0.0,
                "fat": 0.0,
                "entry_count": 0,
            })

    return {"data": result}


@app.get("/stats/data/weight")
async def stats_weight_data(
    db: Session = Depends(_get_db),
):
    """JSON endpoint: all weight entries sorted by date."""
    weights = (
        db.query(WeightEntry)
        .order_by(WeightEntry.date.asc())
        .all()
    )
    return {
        "data": [
            {
                "id": w.id,
                "date": w.date,
                "weight_kg": w.weight_kg,
                "notes": w.notes or "",
            }
            for w in weights
        ]
    }


@app.get("/stats/data/weekly")
async def stats_weekly_data(
    weeks: int = Query(12, ge=4, le=52),
    db: Session = Depends(_get_db),
):
    """JSON endpoint: weekly averages for macros."""
    from datetime import date as date_type, timedelta
    today = date_type.today()
    # Find the most recent Monday (or today if Monday)
    monday = today - timedelta(days=today.weekday())
    start = monday - timedelta(weeks=weeks - 1)

    entries = (
        db.query(FoodEntry)
        .filter(FoodEntry.date >= start.isoformat())
        .order_by(FoodEntry.date)
        .all()
    )

    # Group by ISO week
    from collections import defaultdict
    weekly: dict = defaultdict(lambda: {"calories": 0.0, "protein": 0.0, "carbs": 0.0, "fat": 0.0, "days": set()})

    for e in entries:
        # Parse date and get ISO week start (Monday)
        parts = e.date.split("-")
        ed = date_type(int(parts[0]), int(parts[1]), int(parts[2]))
        week_start = ed - timedelta(days=ed.weekday())
        key = week_start.isoformat()
        weekly[key]["calories"] += e.calories
        weekly[key]["protein"] += e.protein_g
        weekly[key]["carbs"] += e.carbs_g
        weekly[key]["fat"] += e.fat_g
        weekly[key]["days"].add(e.date)

    result = []
    for i in range(weeks):
        ws = (start + timedelta(weeks=i)).isoformat()
        if ws in weekly:
            w = weekly[ws]
            num_days = len(w["days"]) if w["days"] else 1
            result.append({
                "week_start": ws,
                "avg_calories": round(w["calories"] / num_days, 1),
                "avg_protein": round(w["protein"] / num_days, 1),
                "avg_carbs": round(w["carbs"] / num_days, 1),
                "avg_fat": round(w["fat"] / num_days, 1),
                "total_calories": round(w["calories"], 1),
                "logged_days": num_days,
            })
        else:
            result.append({
                "week_start": ws,
                "avg_calories": 0,
                "avg_protein": 0,
                "avg_carbs": 0,
                "avg_fat": 0,
                "total_calories": 0,
                "logged_days": 0,
            })

    return {"data": result}


@app.get("/stats/data/adherence")
async def stats_adherence_data(
    days: int = Query(30, ge=7, le=365),
    db: Session = Depends(_get_db),
    goal: int = Depends(_get_calorie_goal),
):
    """JSON: how many days hit the calorie goal."""
    from datetime import date as date_type, timedelta
    today = date_type.today()
    start = today - timedelta(days=days - 1)

    entries = (
        db.query(FoodEntry)
        .filter(FoodEntry.date >= start.isoformat())
        .order_by(FoodEntry.date)
        .all()
    )

    daily_totals: dict[str, float] = {}
    for e in entries:
        daily_totals[e.date] = daily_totals.get(e.date, 0) + e.calories

    hit = 0
    missed = 0
    no_data = 0
    for i in range(days):
        d = (start + timedelta(days=i)).isoformat()
        if d in daily_totals:
            if daily_totals[d] <= goal:
                hit += 1
            else:
                missed += 1
        else:
            no_data += 1

    return {
        "data": {
            "hit_goal": hit,
            "over_goal": missed,
            "no_data": no_data,
            "total_days": days,
            "hit_pct": round(hit / days * 100, 1) if days else 0,
        }
    }


# ─── Refine Route ────────────────────────────────────────────────


@app.post("/entries/{entry_id}/refine")
async def refine_entry(
    entry_id: int,
    request: Request,
    message: str = Form(...),
    db: Session = Depends(_get_db),
    tz: ZoneInfo = Depends(_get_client_tz),
    goal: int = Depends(_get_calorie_goal),
):
    """Refine an existing food entry based on user correction."""
    entry = db.query(FoodEntry).filter(FoodEntry.id == entry_id).first()
    if not entry:
        raise HTTPException(404, "Entry not found")

    if not message.strip():
        raise HTTPException(400, "Refinement message is required")

    # Build image path if available
    image_path = IMAGES_DIR / entry.image_path if entry.image_path else None
    if image_path and not image_path.exists():
        image_path = None

    try:
        updated = refine_food_estimate(
            food_name=entry.food_name,
            calories=entry.calories,
            protein_g=entry.protein_g,
            carbs_g=entry.carbs_g,
            fat_g=entry.fat_g,
            serving_size=entry.serving_size or "",
            confidence=entry.confidence or "medium",
            description=entry.description or "",
            user_message=message.strip(),
            image_path=image_path,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Refine failed: %s", e)
        raise HTTPException(500, f"Refinement failed: {str(e)[:200]}")

    # Update the entry in-place
    entry.food_name = updated.food_name
    entry.calories = updated.estimated_calories
    entry.protein_g = updated.protein_g
    entry.carbs_g = updated.carbs_g
    entry.fat_g = updated.fat_g
    entry.serving_size = updated.serving_size
    entry.confidence = updated.confidence
    entry.reasoning = updated.reasoning or None
    db.commit()
    db.refresh(entry)

    # Reload the day's data and return the updated entry list
    today_str = entry.date
    entries, totals = _get_day_data(db, today_str)
    return templates.TemplateResponse(
        request,
        "partials/_entry_list.html",
        {
            "today": today_str,
            "label": "",
            "goal": goal,
            "entries": [_entry_to_dict(e) for e in entries],
            "totals": totals,
        },
    )
