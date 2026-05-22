import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# Use /app/data in Docker, or a local data/ dir for development
_data_dir_env = os.environ.get("DATA_DIR", "")
if _data_dir_env:
    DATA_DIR = Path(_data_dir_env)
else:
    # Fallback for local development
    DATA_DIR = Path(__file__).parent.parent / "data"

DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "calorie_tracker.db"
SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},  # Needed for SQLite
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
