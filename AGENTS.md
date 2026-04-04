# Repository Guidelines

## Project Structure & Module Organization
`src/` contains the application code. Keep FastAPI app wiring, routes, schedulers, and task orchestration in `src/web/`; place registration flows, upload logic, and shared runtime helpers in `src/core/`; keep mail provider integrations in `src/services/`; store config and persistence code in `src/config/` and `src/database/`. UI assets live in `templates/`, `static/js/`, and `static/css/`. Put migrations in `alembic/`, helper scripts in `scripts/`, and tests in `tests/`. Runtime data under `data/` and `logs/` should stay out of commits.

## Build, Test, and Development Commands
Use `uv` for local development.

- `uv sync --extra dev`: install runtime and test dependencies.
- `uv run python webui.py --debug`: start the Web UI locally with reload-friendly settings.
- `uv run pytest -q`: run the full test suite.
- `uv run pytest tests/test_registration_engine.py -q`: run a focused test file while iterating.
- `uv run alembic upgrade head`: apply database migrations.
- `bash build.sh` or `build.bat`: package the app with PyInstaller for the current platform.

## Coding Style & Naming Conventions
Target Python 3.10+ and use 4-space indentation. Follow existing naming: `snake_case` for modules, functions, variables, and test names; `PascalCase` for classes. Keep imports grouped by standard library, third-party, and local modules. Prefer explicit type hints on new route, service, and database-facing code. Keep HTTP handlers thin: move reusable logic into `src/core/` or `src/services/`. For UI work, keep page pairs aligned, for example `templates/payment.html` with `static/js/payment.js`.

## Testing Guidelines
Write tests with `pytest` in `tests/test_*.py`, using descriptive `test_*` function names. Favor isolated tests with `monkeypatch` and `httpx` over live network calls. Add or update tests when changing registration flows, email providers, upload adapters, auth, schedulers, or template/static asset wiring.

## Commit & Pull Request Guidelines
Recent history uses conventional prefixes such as `fix:`, `feat:`, `docs:`, and `chore(ci):`; keep that pattern. Write short, imperative subjects like `fix: stabilize registration pipeline`. PRs should include the goal, implementation summary, impact, verification steps, and rollback notes. Link related issues when present, and attach screenshots for template or UI changes.

## Security & Configuration Tips
Use `.env.example` as the starting point for local configuration, but never commit secrets, real credentials, or populated `data/` databases. Change default access credentials before manual testing, and keep security-sensitive changes consistent with `src/web/auth.py` and `src/config/settings.py`.
