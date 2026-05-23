from pydantic import BaseModel, Field
from typing import Optional


class FoodAnalysis(BaseModel):
    """Structured response from Gemini for food image analysis."""
    food_name: str = Field(description="Name of the food item")
    estimated_calories: float = Field(description="Estimated total calories (kcal)")
    protein_g: float = Field(description="Estimated protein in grams")
    carbs_g: float = Field(description="Estimated carbohydrates in grams")
    fat_g: float = Field(description="Estimated fat in grams")
    serving_size: Optional[str] = Field(
        default=None,
        description="Estimated serving size (e.g., '1 bowl', '200g', '1 slice')",
    )
    confidence: str = Field(
        description="Confidence level: high, medium, or low",
        pattern=r"^(high|medium|low)$",
    )
    reasoning: str = Field(
        default="",
        description="Brief explanation of how the estimate was derived: what ingredients were identified, portion assumptions, and key calculations. 1-3 sentences.",
    )


class FoodEntryResponse(BaseModel):
    """API response for a single food entry."""
    id: int
    date: str
    time: str
    food_name: str
    description: Optional[str]
    calories: float
    protein_g: float
    carbs_g: float
    fat_g: float
    serving_size: Optional[str]
    image_url: Optional[str]
    confidence: str
    reasoning: str


class DaySummary(BaseModel):
    """Daily nutritional summary."""
    date: str
    total_calories: float
    total_protein: float
    total_carbs: float
    total_fat: float
    entries: list[FoodEntryResponse]
