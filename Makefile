.PHONY: ci lint format

format:
	python -m ruff format --check .

lint:
	python -m ruff check . --select E9,F

ci: lint
	@echo "[OK] lint passed"
