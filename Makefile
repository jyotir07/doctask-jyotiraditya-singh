ifeq ($(OS),Windows_NT)
PYTHON ?= python
PY := .venv/Scripts/python.exe
else
PYTHON ?= python3
PY := .venv/bin/python
endif

DOCTASK_DSN ?= postgresql://doctask:doctask@localhost:5433/doctask
export DOCTASK_DSN

.PHONY: install up down reset test demo serve mutants

install:
	$(PYTHON) -m venv .venv
	$(PY) -m pip install -e ".[dev]"

up:
	docker compose up -d --wait
	@echo "postgres ready on 5433 (db: doctask, test db: doctask_test)"

down:
	docker compose down

# Empties the demo database so the next run pays for extraction again.
reset:
	$(PY) -m doctask.cli reset

test:
	$(PY) -m pytest -q

demo:
	$(PY) -m doctask.cli demo --corpus $(or $(CORPUS),acme-v1)

serve:
	$(PY) -m uvicorn doctask.asgi:app --port $(or $(PORT),8000)

mutants:
	$(PY) scripts/mutation_check.py
