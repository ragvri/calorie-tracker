import io
from pathlib import Path

from PIL import Image


def compress_image(input_bytes: bytes, output_path: Path, max_width: int = 800, quality: int = 80) -> None:
    """Compress an image and save as JPEG.

    Args:
        input_bytes: Raw image bytes.
        output_path: Where to save the compressed image.
        max_width: Maximum width in pixels (maintains aspect ratio).
        quality: JPEG quality (1-100).
    """
    # Ensure parent directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)

    image = Image.open(io.BytesIO(input_bytes))

    # Convert RGBA/P to RGB for JPEG
    if image.mode in ("RGBA", "P"):
        image = image.convert("RGB")

    # Resize if wider than max_width
    if image.width > max_width:
        ratio = max_width / image.width
        new_height = int(image.height * ratio)
        image = image.resize((max_width, new_height), Image.LANCZOS)

    image.save(output_path, "JPEG", quality=quality, optimize=True)
