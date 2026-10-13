.DEFAULT_GOAL := help
COMPOSE ?= docker compose
# Kontener deweloperski (profil "dev"): testy, lint, ewaluacja - bez Pythona/uv na hoście.
DEV_RUN := $(COMPOSE) --profile dev run --rm --build api-dev
# Bez uruchamiania zależności (Ollama/Mailpit) - dla testów jednostkowych, lintu i formatowania.
DEV_RUN_ISOLATED := $(COMPOSE) --profile dev run --rm --build --no-deps api-dev

.PHONY: help up down reset logs ps wait test test-integration test-llm e2e smoke eval lint fmt

help: ## Pokaż dostępne komendy
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

up: ## Uruchom całe środowisko (pierwszy start pobiera model ~2,5 GB)
	$(COMPOSE) up -d

down: ## Zatrzymaj środowisko (pobrany model zostaje w wolumenie)
	$(COMPOSE) down

reset: ## Zatrzymaj i USUŃ wolumeny (model zostanie pobrany ponownie)
	$(COMPOSE) down -v

logs: ## Śledź logi wszystkich usług
	$(COMPOSE) logs -f

ps: ## Status usług (także zakończonych, np. ollama-init)
	$(COMPOSE) ps -a

wait: ## Czekaj, aż API zgłosi gotowość (/api/v1/health/ready)
	./scripts/wait-ready.sh

test: ## Testy jednostkowe i API (bez LLM i bez sieci)
	$(DEV_RUN_ISOLATED)

test-integration: ## Testy integracyjne z Mailpit (środowisko musi działać)
	$(DEV_RUN) pytest -m integration

test-llm: ## Testy agenta na prawdziwym modelu (środowisko musi działać)
	$(DEV_RUN) pytest -m llm

e2e: ## Testy end-to-end zgodne z DoD (środowisko musi działać)
	$(DEV_RUN) pytest -m e2e

smoke: ## Test dymny w bashu (curl + jq) na działającym środowisku
	./scripts/smoke-test.sh

eval: ## Ewaluacja trafności routingu na zbiorze wzorcowym (prawdziwy LLM)
	$(DEV_RUN) python -m tests.eval.run_eval $(ARGS)

lint: ## ruff (lint + format check) i mypy
	$(DEV_RUN_ISOLATED) sh -c "ruff check src tests && ruff format --check src tests && mypy src"

fmt: ## Automatyczne formatowanie i poprawki ruff
	$(DEV_RUN_ISOLATED) sh -c "ruff format src tests && ruff check --fix src tests"
