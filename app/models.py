from datetime import datetime
from typing import Optional

from sqlalchemy import String, Float, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class WeightEntry(Base):
    __tablename__ = "weight_entries"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    date: Mapped[str] = mapped_column(String(10), index=True)
    weight_kg: Mapped[float] = mapped_column(Float)
    notes: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    def __repr__(self):
        return f"<WeightEntry {self.date} {self.weight_kg}kg>"


class FoodEntry(Base):
    __tablename__ = "food_entries"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    date: Mapped[str] = mapped_column(String(10), index=True)
    time: Mapped[str] = mapped_column(String(5))
    food_name: Mapped[str] = mapped_column(String(200))
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    calories: Mapped[float] = mapped_column(Float)
    protein_g: Mapped[float] = mapped_column(Float)
    carbs_g: Mapped[float] = mapped_column(Float)
    fat_g: Mapped[float] = mapped_column(Float)
    serving_size: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    confidence: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    reasoning: Mapped[Optional[str]] = mapped_column(String(2000), nullable=True)
    image_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    def __repr__(self):
        return f"<FoodEntry {self.date} {self.time} {self.food_name} ({self.calories} kcal)>"
