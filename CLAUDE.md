# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**codex-console** is an OpenAI account management console — a FastAPI web application for automated account registration, lifecycle management, email integration, and payment binding. It is an enhanced fork of `cnlimiter/codex-manager`.

## Commands

```bash
# Install runtime + dev dependencies
uv sync --extra dev

# Start Web UI locally with hot reload
uv run python webui.py --debug

# Run all tests
uv run pytest -q

# Run a single test file
uv run pytest tests/test_registration_engine.py -q

# Apply database migrations
uv run alembic upgrade head

# Package as a standalone executable (current platform)
bash build.sh      # macOS/Linux
build.bat          # Windows
```

Tests live in `tests/test_*.py`. The entry point is `webui.py`.

## Architecture

The app is layered: routes → core logic → services → database.

```
webui.py                        # CLI entry: starts Uvicorn, inits DB and config
src/web/
  app.py                        # FastAPI app factory + lifespan
  routes/                       # HTTP handlers (thin — delegate to core/)
  task_manager.py               # Unified async task orchestration
src/core/
  register.py                   # Main registration pipeline (~4500 lines)
  auto_registration.py          # Batch coordinator with concurrency control
  system_selfcheck.py           # Health diagnostics
  anyauto/                      # Sentinel POW solver + OAuth flows
  openai/                       # OpenAI API wrappers
  upload/                       # Multi-target account upload adapters
src/services/                   # Email provider backends (Tempmail, Outlook,
                                #   CloudMail, IMAP catchall, LuckMail, etc.)
src/database/
  models.py                     # SQLAlchemy ORM (Account, RegistrationTask,
                                #   MailboxConfig, BindCardTask, …)
  crud.py                       # All DB read/write operations
  session.py                    # Async session factory (SQLite or PostgreSQL)
src/config/
  settings.py                   # Runtime settings loaded from DB (50+ keys)
  constants.py                  # Enums: AccountLabel, PoolState, RoleTag, …
templates/                      # Jinja2 HTML (one file per page)
static/js/                      # Per-page JS controllers (paired with templates)
alembic/                        # Migration scripts
```

**Key data flow:**
1. **Registration**: Email acquisition → Sentinel POW → OAuth → token capture → stored in `Account` table.
2. **Batch registration**: `auto_registration.py` coordinates concurrent tasks, state tracked in `RegistrationTask`.
3. **Real-time UI**: WebSocket endpoint in `src/web/routes/websocket.py` pushes task progress.
4. **Email services**: All providers share a common interface; configuration lives in `MailboxConfig` (DB).

## Coding Conventions

- Python 3.10+, 4-space indentation.
- `snake_case` for modules/functions/variables/tests; `PascalCase` for classes.
- Imports: standard library → third-party → local.
- Explicit type hints on route handlers, service functions, and DB-facing code.
- Keep HTTP handlers thin — business logic belongs in `src/core/` or `src/services/`.
- Keep template/JS pairs aligned: `templates/foo.html` ↔ `static/js/foo.js`.

## Testing

- Use `pytest` with `monkeypatch` and `httpx` — avoid live network calls.
- Cover registration flows, email providers, upload adapters, auth, and schedulers when changing them.

## Commit Style

Follow conventional commits: `fix:`, `feat:`, `docs:`, `chore(ci):`. Short imperative subjects, e.g. `fix: stabilize registration pipeline`.

## Security Notes

- Copy `.env.example` for local config; never commit secrets or populated `data/` databases.
- `data/` and `logs/` are runtime-only — keep them out of commits.
- Auth logic lives in `src/web/auth.py`; settings validation in `src/config/settings.py`.
