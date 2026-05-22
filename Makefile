.PHONY: dev run build docker-run docker-stop clean lint db

# ─── Local Development ───────────────────────────────────────────────

dev:  ## Run with auto-reload for development
	uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

run:  ## Run in production mode
	uv run uvicorn app.main:app --host 0.0.0.0 --port 8000

# ─── Docker ──────────────────────────────────────────────────────────

build:  ## Build Docker image
	docker build -t calpal .

docker-run: build  ## Build and run in Docker
	docker run -d \
		-p 8000:8000 \
		-e GEMINI_API_KEY="${GEMINI_API_KEY}" \
		-v calpal_data:/app/data \
		--name calpal \
		calpal

docker-stop:  ## Stop and remove the container
	docker stop calpal 2>/dev/null; docker rm calpal 2>/dev/null; true

docker-logs:  ## Tail container logs
	docker logs -f calpal

# ─── Dependencies ────────────────────────────────────────────────────

install:  ## Install dependencies
	uv sync

update:  ## Update dependencies and lockfile
	uv sync --upgrade

# ─── Cleanup ─────────────────────────────────────────────────────────

clean:  ## Remove __pycache__ and .venv
	find . -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null; true
	rm -rf .venv

clean-db:  ## Remove the SQLite database (keeps images)
	rm -f data/calorie_tracker.db

clean-all: clean clean-db  ## Full cleanup (deps + db + caches)

# ─── Help ────────────────────────────────────────────────────────────

help:  ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'
