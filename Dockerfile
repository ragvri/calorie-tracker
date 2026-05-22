# Use uv's official Python image
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# Set working directory
WORKDIR /app

# Copy project files
COPY pyproject.toml uv.lock ./

# Install dependencies using uv
RUN uv sync --frozen --no-dev

# Copy the app source
COPY app/ app/

# Create data directory
RUN mkdir -p /app/data /app/data/images

# Expose the port
EXPOSE 8000

# Run the FastAPI app with uvicorn
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
