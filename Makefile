.PHONY: help install test lint format typecheck migrate run smoke post-restart pre-commit-install docker-up backup

PYTHON ?= python3
BOT_TOKEN ?= test-token

help:
	@echo "Targets: install test lint format typecheck migrate run smoke post-restart pre-commit-install docker-up backup"
	@echo "  smoke         — migrate (if needed) + pytest (unit smoke)"
	@echo "  post-restart  — read-only DB/schema check + Telegram retest checklist"
	@echo "  pre-commit-install — install local git hooks (optional; CI does not need this)"

install:
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -r requirements.txt
	$(PYTHON) -m pip install -e ".[dev,lint]"

test:
	BOT_TOKEN=$(BOT_TOKEN) \
	SCHEDULER_ENABLED=false \
	PASARGUARD_BASE_URL=$${PASARGUARD_BASE_URL:-https://panel.example.com} \
	PASARGUARD_API_KEY=$${PASARGUARD_API_KEY:-} \
	pytest

lint:
	ruff check .
	black --check .
	mypy bot services api_client database scheduler web

format:
	ruff check . --fix
	black .

typecheck:
	mypy bot services api_client database scheduler web

migrate:
	alembic upgrade head

run:
	$(PYTHON) -m bot.main

smoke: migrate test
	@echo "Smoke OK: migrations applied and pytest passed."

post-restart:
	$(PYTHON) scripts/post_restart_check.py

pre-commit-install:
	pre-commit install

docker-up:
	cd docker && docker compose up --build -d

backup:
	cd docker && docker compose --profile backup run --rm backup
