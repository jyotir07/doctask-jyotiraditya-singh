PY := .venv/Scripts/python.exe

.PHONY: up down test demo fmt

up:
	docker compose up -d --wait
	@echo "postgres ready on 5433 (db: doctask, test db: doctask_test)"

down:
	docker compose down

test:
	$(PY) -m pytest -q

demo:
	$(PY) -m doctask.cli demo --corpus $(or $(CORPUS),acme-v1)

mutants:
	$(PY) scripts/mutation_check.py
