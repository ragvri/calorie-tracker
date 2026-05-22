import json
import logging
import os
import re
import time
from pathlib import Path

from google import genai
from google.genai import types

from .schemas import FoodAnalysis

logger = logging.getLogger(__name__)

GEMINI_MODELS = [
    os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview"),
    "gemini-3-flash-preview",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
]

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY environment variable is not set")
        _client = genai.Client(api_key=api_key)
    return _client


def _extract_retry_delay(error_str: str) -> float:
    match = re.search(r"retry in ([\d.]+)s", error_str)
    if match:
        return float(match.group(1))
    return 5.0


FOOD_ANALYSIS_PROMPT = """You are a precise and conservative nutritionist. Analyze this food image and estimate its nutritional content.

## ESTIMATION RULES (critical — follow these exactly):

1. **When uncertain, use the HIGHER estimate.** If a dish could be 300-500 kcal, report 500. If it could be 200-300 kcal, report 300. Always round up when in doubt. This is for weight tracking — underestimation defeats the purpose.

2. **Identify every visible component.** List ingredients you can see: proteins (chicken, beef, fish, tofu, eggs), carbs (rice, bread, pasta, potatoes), fats (oil, butter, cheese, avocado, cream), vegetables, sauces, toppings.

3. **Estimate portion sizes from visual cues:**
   - Use the plate/bowl as reference (a full dinner plate ≈ 10-12 inches)
   - Compare to recognizable objects (utensils, hands if visible, standard cup/bowl sizes)
   - Rice/bowl: 1 cup ≈ 200g cooked ≈ 240 kcal
   - Bread: 1 slice ≈ 30g ≈ 80 kcal
   - Meat: palm-sized portion ≈ 100g ≈ 200-250 kcal (varies by type)
   - Pasta: 1 cup cooked ≈ 200g ≈ 220 kcal
   - Oil/butter: 1 tbsp ≈ 120 kcal (assume at least 1-2 tbsp for sauteed foods)

4. **Macronutrient reference table (per 100g unless noted):**
   - Chicken breast (cooked): 165 kcal, 31g P, 0g C, 3.6g F
   - Chicken thigh (cooked): 209 kcal, 26g P, 0g C, 11g F  
   - Beef (lean, cooked): 250 kcal, 26g P, 0g C, 15g F
   - Salmon (cooked): 208 kcal, 20g P, 0g C, 13g F
   - White rice (cooked): 130 kcal, 2.7g P, 28g C, 0.3g F
   - Brown rice (cooked): 123 kcal, 2.7g P, 26g C, 1g F
   - Pasta (cooked): 158 kcal, 5.8g P, 31g C, 0.9g F
   - Bread (white): 265 kcal, 9g P, 49g C, 3.2g F
   - Potato (boiled): 87 kcal, 1.9g P, 20g C, 0.1g F
   - French fries: 312 kcal, 3.4g P, 41g C, 15g F
   - Egg (large, 1): 78 kcal, 6.3g P, 0.6g C, 5.3g F
   - Cheese (cheddar, 1 slice/28g): 113 kcal, 7g P, 0.4g C, 9.3g F
   - Olive oil (1 tbsp/15ml): 119 kcal, 0g P, 0g C, 13.5g F
   - Butter (1 tbsp/14g): 102 kcal, 0.1g P, 0g C, 11.5g F
   - Avocado (1/2 medium): 160 kcal, 2g P, 8.5g C, 14.7g F
   - Banana (1 medium): 105 kcal, 1.3g P, 27g C, 0.4g F
   - Apple (1 medium): 95 kcal, 0.5g P, 25g C, 0.3g F
   - Milk (whole, 1 cup): 149 kcal, 8g P, 12g C, 8g F
   - Yogurt (plain, 1 cup): 154 kcal, 13g P, 17g C, 4g F
   - Pizza (1 slice, 14"): 285 kcal, 12g P, 36g C, 10g F
   - Burger (fast food): 540 kcal, 34g P, 40g C, 27g F
   - Salad dressing (2 tbsp): 145 kcal, 0g P, 2g C, 15g F

5. **Account for hidden calories:**
   - Oils, butter, and sauces used in cooking add significant calories
   - Fried foods: add ~100-150 kcal per serving for oil absorption
   - Sauces and dressings: don't forget these — mayo, ketchup, soy sauce add up

6. **Confidence levels:**
   - "high": Clear, single dish with visible, recognizable ingredients
   - "medium": Mixed dish, slightly obscured, or requires some guesswork
   - "low": Image is blurry, poorly lit, contains ambiguous food, or is not food

7. **serving_size:** Use real-world descriptions like "1 bowl", "2 slices", "~300g", "1 plate". Be as specific as possible.

8. **food_name:** Use clear, descriptive names: "Grilled chicken with rice and broccoli", not just "chicken". 

If there is NO food in the image, set calories/macros to 0 and confidence to "low". Otherwise, always provide a non-zero estimate."""  # noqa: E501


def analyze_food_image(image_path: Path, description: str = "") -> FoodAnalysis:
    """Send a food image to Gemini and get structured nutritional analysis.

    Uses a detailed prompt that instructs the model to use the higher end of
    any uncertainty range, with a nutrition reference table and specific
    portion-size estimation rules.
    """
    client = _get_client()

    with open(image_path, "rb") as f:
        image_bytes = f.read()

    prompt = FOOD_ANALYSIS_PROMPT
    if description:
        prompt += f"\n\n## USER NOTES\n{description}\n\nUse this description to refine your estimate. If the user mentions specific ingredients or cooking methods not visible in the image, incorporate them."

    last_error = None
    for model_idx in range(len(GEMINI_MODELS)):
        model = GEMINI_MODELS[model_idx]
        if model_idx > 0:
            logger.info("Trying fallback model: %s", model)

        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=[
                        prompt,
                        types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                    ],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=FoodAnalysis,
                        temperature=0.1,  # Lower = more consistent estimates
                    ),
                )

                if hasattr(response, "parsed") and response.parsed is not None:
                    logger.info("Gemini (%s) returned: %s", model, response.parsed.model_dump())
                    return response.parsed

                text = response.text
                logger.info("Gemini (%s) raw: %s", model, text[:200])
                data = json.loads(text)
                return FoodAnalysis(**data)

            except Exception as e:
                error_str = str(e)
                last_error = e
                logger.warning("Gemini (%s) attempt %d: %s", model, attempt + 1, error_str[:200])

                if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                    retry_sec = _extract_retry_delay(error_str)
                    if attempt < 2:
                        wait = min(retry_sec + 1, 30)
                        logger.info("Rate limited, waiting %.0fs...", wait)
                        time.sleep(wait)
                        continue
                else:
                    break

    raise last_error or RuntimeError("All Gemini models failed")
