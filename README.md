# CalPal — AI-Powered Calorie Tracker

## Why I built this

Manually logging calories in existing apps was too much work. I'd forget a meal, then have to guess later. I figured LLMs are now good enough to give rough-but-useful estimates from a single photo — good enough for tracking trends, even if not lab-accurate. So I built this.

Snap a photo. Get an estimate. Move on with your day.

### Everything is free

The entire stack costs nothing:
- **Google Gemini API** — free tier, no billing enabled required
- **Oracle Cloud VPS** — free VM with plenty of resources for a single-user app
- **Duck DNS** — free dynamic DNS
- **Let's Encrypt** — free SSL certificates (via Traefik)
- **Authelia** — free, open-source auth

Built with Python (FastAPI), SQLite, HTMX, and Tailwind CSS. Mobile-first, zero JavaScript frameworks.

## How It Works

1. **Take a photo** of your meal using the in-app camera or gallery
2. **Gemini AI** analyzes the image and returns structured nutritional data:
   - Food name, serving size, calories, protein, carbs, fat
   - Confidence level (high / medium / low)
3. **Daily summary** shows a calorie ring with macro progress bars
4. **Browse history** via the 7-day date strip — tap any day to see entries
5. **Delete entries** with a single tap (HTMX-powered, no page reload)

### Client Timezone Support

CalPal automatically detects your browser's timezone and uses it for all dates and times. Log a meal at 11:30 PM in NYC? It gets recorded as today, not tomorrow — even if the server runs in UTC.

## Quick Start

### Prerequisites

- Python 3.11+
- A [Google Gemini API key](https://aistudio.google.com/apikey) (free tier available)

### Local Development

```bash
# Clone and enter the project
git clone <your-repo-url>
cd calorie-tracker

# Install dependencies
uv sync

# Set your API key
export GEMINI_API_KEY="your-key-here"

# Run the app
uv run uvicorn app.main:app --reload --port 8000
```

Open [http://localhost:8000](http://localhost:8000) and tap the 📸 button to start logging.

### Using Docker

```bash
# Build the image
docker build -t calpal .

# Run it
docker run -d \
  -p 8000:8000 \
  -e GEMINI_API_KEY="your-key-here" \
  -v calpal_data:/app/data \
  --name calpal \
  calpal
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Main dashboard (today's entries) |
| `POST` | `/upload` | Upload a food photo for analysis |
| `GET` | `/day/{date}` | View entries for a specific date (YYYY-MM-DD) |
| `DELETE` | `/entries/{id}` | Delete a food entry |

The entire UI is server-rendered HTML (Jinja2 templates). HTMX handles dynamic updates without a JSON API.

## Deployment

This app is deployed on a home server behind **Traefik** (reverse proxy with auto Let's Encrypt SSL) with **Duck DNS** for free dynamic DNS.

### Stack used here

```
Duck DNS (calories.crazystremio.duckdns.org)
    ↓
Traefik (auto HTTPS via Let's Encrypt)
    ↓
Authelia (authentication portal)
    ↓
Calorie Tracker container (port 8000)
```

**Authelia** is an open-source authentication server that adds a login portal in front of protected apps. When you visit the site, Authelia intercepts the request first and presents a login page. Only authenticated users reach CalPal. It uses its own PostgreSQL database and Redis for sessions.

Traefik routes requests through Authelia via a middleware (`authelia@docker`). The relevant Docker Compose labels:

```yaml
labels:
  - "traefik.enable=true"
  - "traefik.http.routers.calorie-tracker.rule=Host(`calories.crazystremio.duckdns.org`)"
  - "traefik.http.routers.calorie-tracker.entrypoints=websecure"
  - "traefik.http.routers.calorie-tracker.tls.certresolver=letsencrypt"
  - "traefik.http.routers.calorie-tracker.middlewares=authelia@docker"
  - "traefik.http.services.calorie-tracker.loadbalancer.server.port=8000"
```

### Duck DNS setup (self-hosted)

1. Sign up at [duckdns.org](https://duckdns.org), create a subdomain
2. Install the update script:
   ```bash
   mkdir -p ~/duckdns
   cat > ~/duckdns/duck.sh << 'EOF'
   echo url="https://www.duckdns.org/update?domains=YOURDOMAIN&token=YOURTOKEN&ip=" | curl -k -o ~/duckdns/duck.log -K -
   EOF
   chmod +x ~/duckdns/duck.sh
   ```
3. Add a cron job (runs every 5 minutes):
   ```bash
   crontab -e
   # Add:
   */5 * * * * ~/duckdns/duck.sh >/dev/null 2>&1
   ```
4. Set up **Traefik** (or Caddy) as a reverse proxy with a `docker-compose.yml` label

### Environment Variables

Set `GEMINI_API_KEY` in the Docker Compose or Traefik config. Optionally set `TZ` as a server fallback timezone.

## Project Structure

```
calorie-tracker/
├── app/
│   ├── main.py              # FastAPI routes and app logic
│   ├── models.py            # SQLAlchemy FoodEntry model
│   ├── database.py          # SQLite engine and session
│   ├── schemas.py           # Pydantic models (FoodAnalysis, etc.)
│   ├── gemini_client.py     # Gemini API integration + prompt
│   ├── image_utils.py       # Image compression utilities
│   ├── templates/
│   │   ├── base.html        # Base layout + Tailwind + HTMX
│   │   ├── index.html       # Dashboard page
│   │   └── partials/
│   │       └── _entry_list.html  # HTMX partial (daily summary + entries)
│   └── static/              # Static assets
├── data/                    # SQLite DB + uploaded images (gitignored)
├── Dockerfile               # Container build
├── pyproject.toml           # Project metadata + dependencies
└── Makefile                 # Dev shortcuts
```

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GEMINI_API_KEY` | Yes | — | Google Gemini API key |
| `GEMINI_MODEL` | No | `gemini-3-flash-preview` | AI model to use |
| `DATA_DIR` | No | `./data/` (dev) or `/app/data` (Docker) | Data directory |
| `TZ` | No | — | Server fallback timezone |

## How This Was Built

This app was built as a solo project using AI-assisted development (Claude Code / Codex). The core architecture decisions:

- **HTMX over a JS framework** — keeps the frontend simple; the server returns HTML fragments that HTMX swaps into the DOM
- **Gemini structured output** — the AI is forced to return valid JSON matching a Pydantic schema via `response_mime_type` + `response_schema`
- **SQLite single-file DB** — zero infrastructure; the entire database is one file in `data/`
- **uv** for dependency management — faster than pip/poetry, uses a lockfile

## License

MIT
