# Backend (Django API)

Django 5 + DRF + Channels. Authoritative scoring engine, tournaments, standings, public API, admin-only PDF reports.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # edit values; set DJANGO_DEBUG=true for local development
python manage.py migrate
python manage.py seed_demo                             # optional, local only
daphne -b 127.0.0.1 -p 8000 config.asgi:application    # or: uvicorn config.asgi:application --port 8000
```

* Without `DATABASE_URL` the app uses SQLite (`db.sqlite3`). **Production uses PostgreSQL** (`DATABASE_URL=postgresql://user:pass@host:5432/db`).
* Without `REDIS_URL` the Channels layer is in-memory: WebSocket fan-out then works inside **one** process only (fine for a single worker).
* OpenAPI: `/api/docs/` (Swagger), `/api/schema/`. Health: `/api/health/`.

## Commands

| Command | Purpose |
|---|---|
| `python manage.py migrate` | apply migrations (never deletes data on startup) |
| `python manage.py ensure_admin` | create the first Super Admin from `ADMIN_USERNAME` / `ADMIN_PASSWORD` / `ADMIN_EMAIL` (idempotent, never overwrites) |
| `python manage.py createsuperuser` | alternative: interactive Super Admin |
| `python manage.py seed_demo` | demo data (refuses to run when `DJANGO_DEBUG=false` unless `--force`) |
| `python manage.py dispatch_outbox --loop` | re-publishes WebSocket events that failed to send (transactional outbox) |
| `python -m pytest` | test suite |
| `python tests/generate_scoring_fixtures.py` | regenerate the cross-language scoring contract fixture (then copy it to `admin/tests/fixtures/`) |
| `python tests/perf/loadtest.py --help` | latency / fan-out benchmark against a running server |

## Environment variables

See [`.env.example`](.env.example). Required in production: `DJANGO_SECRET_KEY`, `DATABASE_URL`,
`FRONTEND_URL`, `CORS_ORIGIN` (comma-separated; also used for WebSocket origins), `CLOUDINARY_CLOUD_NAME` / `CLOUDINARY_API_KEY` /
`CLOUDINARY_API_SECRET` (image uploads; without them files are stored on disk under `MEDIA_ROOT`). Important optional ones: `REDIS_URL`,
`CLOUDINARY_FOLDER` (default `Peddel`), `DB_POOL_MAX` (default 20 connections per process), `THROTTLE_LOGIN` (default `10/min`).

## Layout

```
config/                 settings, urls, asgi (HTTP + WebSocket routing, origin validation)
apps/accounts/          users + roles, JWT auth, permissions, error format
apps/players/ teams/    players, teams (exactly two players each)
apps/matches/           matches, courts, configuration, state, participants, dashboard
apps/scoring/           engine.py (pure rules), services.py (transactional actions), consumers.py (WebSocket)
apps/tournaments/       tournaments, registrations, groups, fixtures, bracket reconciliation
apps/standings/         standings projection + configurable ranking
apps/public_api/        read-only public endpoints (explicit field allow-lists), media serving, share pages
apps/reports/           ReportLab PDFs + admin-only routes
apps/notifications/     transactional outbox
apps/audit/             audit log
tests/                  pytest suite (+ fixtures/scoring_contract.json, perf/loadtest.py)
```
