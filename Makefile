.DEFAULT_GOAL := help
COMPOSE ?= docker compose
OBS := -f docker-compose.yml -f docker-compose.observability.yml
CI := -f docker-compose.yml -f docker-compose.ci.yml
# Files written by the dev container (formatting, eval reports) belong to the host user.
export DEV_UID := $(shell id -u)
export DEV_GID := $(shell id -g)
# Kontener deweloperski (profil "dev"): testy, lint, ewaluacja - bez Pythona/uv na hoście.
DEV_RUN := $(COMPOSE) --profile dev run --rm --build api-dev
# Bez uruchamiania zależności (Ollama/Mailpit) - dla testów jednostkowych, lintu i formatowania.
DEV_RUN_ISOLATED := $(COMPOSE) --profile dev run --rm --build --no-deps api-dev
TRIVY_IMAGE ?= aquasec/trivy:0.75.0

.PHONY: help up down reset logs ps wait test test-integration test-llm e2e e2e-fake smoke \
	eval load bench lint fmt scan up-observability down-observability model-export \
	model-import api-key

help: ## Pokaż dostępne komendy
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

up: ## Uruchom całe środowisko (pierwszy start pobiera model ~2,5 GB)
	$(COMPOSE) up -d

down: ## Zatrzymaj środowisko (model i dane zostają w wolumenach)
	$(COMPOSE) down

reset: ## Zatrzymaj i USUŃ wolumeny (model, zgłoszenia, outbox)
	$(COMPOSE) down -v

logs: ## Śledź logi wszystkich usług
	$(COMPOSE) logs -f

ps: ## Status usług (także zakończonych, np. ollama-init)
	$(COMPOSE) ps -a

wait: ## Czekaj, aż API zgłosi gotowość (/api/v1/health/ready)
	./scripts/wait-ready.sh

up-observability: ## Środowisko + Prometheus, Grafana (:3000) i Jaeger (:16686)
	$(COMPOSE) $(OBS) up -d

down-observability: ## Zatrzymaj środowisko z nakładką obserwowalności
	$(COMPOSE) $(OBS) down

test: ## Testy jednostkowe, API i kontraktowe (bez LLM i bez sieci)
	$(DEV_RUN_ISOLATED)

test-integration: ## Testy integracyjne z Mailpit (środowisko musi działać)
	$(DEV_RUN) pytest -m integration

test-llm: ## Testy agenta na prawdziwym modelu (środowisko musi działać)
	$(DEV_RUN) pytest -m llm

e2e: ## Testy end-to-end (DoD) na działającym środowisku z prawdziwym modelem
	$(DEV_RUN) pytest -m e2e

e2e-fake: ## E2E na deterministycznym fałszywym LLM (bez Ollamy - jak w CI)
	$(COMPOSE) $(CI) up -d --build mailpit api api-ready
	$(COMPOSE) $(CI) --profile dev run --rm --build api-dev pytest -m e2e

smoke: ## Test dymny w bashu (curl + jq) na działającym środowisku
	./scripts/smoke-test.sh

eval: ## Ewaluacja trafności (prawdziwy LLM); ARGS="--tags pl", AGENT_MODE=output_function
	$(COMPOSE) --profile dev run --rm --build $(if $(AGENT_MODE),-e AGENT_MODE=$(AGENT_MODE)) \
		api-dev python -m tests.eval.run_eval $(ARGS)

load: ## Test obciążeniowy na działającym API; ARGS="--concurrency 4 --requests 20"
	$(DEV_RUN) python -m tests.perf.loadtest $(ARGS)

bench: ## Benchmark: 10 kolejnych zgłoszeń, p50/p95 + opis sprzętu
	$(DEV_RUN) python -m tests.perf.loadtest --concurrency 1 --requests 10 --markdown

lint: ## ruff (lint + format check) i mypy --strict
	$(DEV_RUN_ISOLATED) sh -c "ruff check src tests && ruff format --check src tests && mypy src"

fmt: ## Automatyczne formatowanie i poprawki ruff
	$(DEV_RUN_ISOLATED) sh -c "ruff format src tests && ruff check --fix src tests"

scan: ## Skan podatności obrazu API (Trivy, HIGH/CRITICAL)
	$(COMPOSE) build api
	docker run --rm -v /var/run/docker.sock:/var/run/docker.sock $(TRIVY_IMAGE) image \
		--severity HIGH,CRITICAL --ignore-unfixed ai-router-api:local

model-export: ## Eksport pobranego modelu do ollama-models.tgz (instalacje offline)
	./scripts/model-export.sh

model-import: ## Import modelu: make model-import FILE=ollama-models.tgz
	./scripts/model-import.sh $(FILE)

api-key: ## Wygeneruj klucz API i wpis do API_KEYS (AUTH_MODE=api_key)
	@python3 -c "import secrets,hashlib; k=secrets.token_urlsafe(32); print('Klucz (daj klientowi):', k); print('Wpis do .env:      API_KEYS=client:' + hashlib.sha256(k.encode()).hexdigest())"
