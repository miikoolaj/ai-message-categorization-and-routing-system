# Runbook — utrzymanie AI Message Router

Procedury operacyjne: diagnostyka, reakcja na alerty, aktualizacje modelu i obrazów, kopie zapasowe
oraz obsługa wniosków o usunięcie danych. Polecenia uruchamia się w katalogu repozytorium; przy
nakładce obserwowalności trzeba dodać `-f docker-compose.yml -f docker-compose.observability.yml`
(cele `make` robią to same).

## 1. Mapa systemu

| Element | Gdzie | Uwagi |
|---|---|---|
| API | kontener `api`, port hosta 8080 | Jeden proces: HTTP, agent, worker trybu async, dyspozytor outboxu, konserwacja |
| Baza | wolumen `api_data` → `/app/data/ai-router.sqlite3` | Zgłoszenia, outbox, klucze idempotencji, informacja zwrotna, sekret podpisów |
| Model | wolumen `ollama_data` → `/root/.ollama` | Ok. 2,5 GB; `make model-export` robi kopię |
| Ollama | kontener `ollama`, port hosta 11435 | `OLLAMA_KEEP_ALIVE=-1`: model zostaje w pamięci |
| Mailpit | kontener `mailpit`, UI 8025 | Środowisko testowe; produkcyjnie `SMTP_HOST` wskazuje serwer firmowy |
| Metryki | http://localhost:8080/metrics | Prometheus :9090, Grafana :3000, Jaeger :16686 (nakładka) |

Zadania w tle w procesie API: dyspozytor outboxu (wysyłka i ponowienia), worker trybu async,
konserwacja (metryki co 15 s, retencja co godzinę), rozgrzewka modelu i strażnik modelu (co 30 s
sprawdza `/api/ps` Ollamy i rozgrzewa model ponownie, gdy go zabraknie).

## 2. Szybka diagnoza

```bash
docker compose ps -a                                        # stan usług (ollama-init powinien mieć Exited (0))
curl -s localhost:8080/api/v1/health/ready | jq             # która zależność nie działa
docker compose logs --no-log-prefix --since 15m api | jq -cR 'fromjson? | select(.level != "INFO")'  # ostrzeżenia i błędy
docker compose exec api python -m ai_router.admin stats     # zgłoszenia i outbox według statusu
curl -s localhost:8080/metrics | grep -E '^ai_router_(outbox_pending|tickets|fallback_total)'
```

Readiness (`/api/v1/health/ready`) sprawdza: Ollamę (`ollama`), obecność modelu i obsługę narzędzi
(`model`), rozgrzewkę (`model_warm`), banner SMTP (`smtp`) i bazę (`storage`). Szczegóły błędów są
celowo ogólne (`timeout`, `connection failed`, `HTTP 404`); pełne komunikaty są w logach API.

Poza Docker Compose (np. Kubernetes): `livenessProbe` → `GET /api/v1/health/live` (proces żyje; restart
tylko przy zawieszeniu), `readinessProbe` → `GET /api/v1/health/ready` (odcięcie ruchu, gdy brakuje
zależności, bez restartu). Nie podpinaj readiness pod liveness: chwilowa niedostępność Ollamy
restartowałaby wtedy zdrowe API.

Każde zgłoszenie ma `request_id` (nagłówek `X-Request-ID`, nagłówek maila, logi) i numer `AIR-…`.
Przebieg jednego zgłoszenia:

```bash
docker compose logs --no-log-prefix api | grep '<request_id>' | jq -c '{ts, event, department, routed_by, duration_ms}'
```

W Jaegerze (http://localhost:16686, usługa `ai-router`) ten sam przebieg widać jako drzewo: HTTP →
`invoke_agent` → `chat <model>` (zapytania do LLM z liczbą tokenów) → `execute_tool send_email`.

## 3. Alerty

Reguły: [observability/prometheus/alerts.yml](../observability/prometheus/alerts.yml). Widoczne w
Prometheusie (zakładka *Alerts*); powiadomienia wymagają podłączenia Alertmanagera.

### AiRouterDown (critical)

Prometheus nie może pobrać `/metrics` od 2 minut.

1. `docker compose ps api`: czy kontener działa i czy jest `healthy`?
2. `docker compose logs --tail 100 api`: błąd startu? Najczęstsze: błąd walidacji konfiguracji (komunikat
   wskazuje zmienną) albo brak dostępu do bazy (uprawnienia wolumenu, pełny dysk: `df -h`).
3. Restart: `docker compose up -d api`. Przerwane zgłoszenia asynchroniczne wrócą do kolejki,
   niewysłane maile do outboxu.

### AiRouterFallbackRateHigh (warning)

Ponad 10% zgłoszeń przez 15 minut trafia do działu fallback, czyli agent zawodzi.

1. Przyczyna fallbacków: panel *Fallbacks by cause* albo
   `curl -s localhost:8080/metrics | grep ai_router_fallback_total`.
   - `llm_unavailable`: Ollama nie działa albo nie odpowiada → sekcja [Ollama](#ollama-nie-działa-lub-jest-zabijana).
   - `llm_misconfigured`: zła nazwa modelu albo model bez obsługi narzędzi (readiness, komponent `model`).
   - `timeout`: agent przekracza `AGENT_TIMEOUT_SECONDS` → alert *SlowRouting*.
   - `model_behavior` / `no_tool_call` / `usage_limit`: model nie wywołuje narzędzia poprawnie. Typowo po zmianie modelu,
     promptu lub polityki. Sprawdź `prompt_version` w `ai_router_info` i porównaj z ostatnią dobrą
     wersją; uruchom `make eval`.
2. Zgłoszenia z fallbacku nie giną: leżą w skrzynce `other@` z oznaczeniem `routed_by: fallback`.

### AiRouterSlowRouting (warning)

p95 czasu agenta powyżej 30 s przez 15 minut.

1. Kolejka czy model? Panel *Agent duration* pokazuje osobno czas agenta i czas oczekiwania na slot
   (`ai_router_queue_wait_seconds`). Długie oczekiwanie oznacza więcej zgłoszeń, niż model nadąża
   obsłużyć (na CPU ok. 5/min).
2. `docker stats ai-router-ollama-1`: czy Ollama dostaje CPU? Czy host nie jest przeciążony?
3. Czy model jest w pamięci? `curl -s localhost:11435/api/ps | jq '.models[].name'`. Jeśli nie,
   strażnik powinien go rozgrzać w ciągu 30 s (log `llm.model_unloaded`).
4. Doraźnie: tryb asynchroniczny po stronie klientów (`Prefer: respond-async`), `AGENT_MODE=output_function`
   (jedno zapytanie do LLM zamiast dwóch), docelowo GPU (`docker-compose.gpu.yml`).

### AiRouterOutboxBacklog (warning)

Ponad 20 maili czeka w outboxie od 10 minut, więc najpewniej nie działa SMTP.

1. Readiness, komponent `smtp`; zdarzenia `smtp.retry`, `smtp.failed` i `outbox.retry_scheduled` w
   `docker compose logs api`.
2. Sprawdź serwer SMTP (`docker compose ps mailpit` albo serwer firmowy: DNS, port, TLS, hasło).
3. Po naprawie nic nie trzeba robić: dyspozytor wyśle zaległe maile sam (ponowienia z wykładniczym
   odstępem, najdłuższa przerwa `OUTBOX_RETRY_MAX_SECONDS`, domyślnie 5 min).

### AiRouterOutboxDeliveryFailed (critical)

Co najmniej jeden mail wyczerpał wszystkie próby (`OUTBOX_MAX_ATTEMPTS`, domyślnie 12, czyli ok. 25 minut
prób). Zgłoszenie nie dotarło do działu.

1. Przyczyna (pole `error`): `docker compose logs api | grep outbox.failed`.
2. Po naprawie SMTP ponów wysyłkę:
   ```bash
   docker compose exec api python -m ai_router.admin retry-failed
   ```
3. Gdy przyczyną jest sam mail (np. serwer odrzuca adres odbiorcy), popraw politykę lub konfigurację,
   a zgłoszenie obsłuż ręcznie.

### AiRouterLowConfidenceRateHigh (info)

Ponad 30% decyzji agenta w ciągu godziny ma pewność poniżej `CONFIDENCE_THRESHOLD`.

To sygnał jakościowy, nie awaria: użytkownicy piszą o sprawach, których polityka nie opisuje dobrze,
albo zmienił się model. Przejrzyj korekty działów (`GET /api/v1/feedback/export`) i maile oznaczone
`[Do weryfikacji]`, uzupełnij `scope` / `examples` w polityce i sprawdź zmianę przez `make eval`.

## 4. Typowe awarie

### Instalacja i pierwsze uruchomienie

| Objaw | Przyczyna i rozwiązanie |
|---|---|
| `docker compose up -d` długo czeka przy pierwszym uruchomieniu | Pobieranie modelu (ok. 2,5 GB); postęp: `docker compose logs -f ollama-init` |
| `port is already allocated` | Port zajęty przez inną usługę; zmiana w `.env`, np. `API_PORT=8090` |
| `/bin/sh^M: not found` w `ollama-init` (Windows) | Końce linii CRLF w skryptach; `git config core.autocrlf false` i ponowne sklonowanie |
| Readiness zwraca 503 | `curl -s localhost:8080/api/v1/health/ready \| jq` wskazuje niedziałającą zależność |

### Ollama nie działa lub jest zabijana

```bash
docker compose ps ollama; docker compose logs --tail 50 ollama
docker inspect ai-router-ollama-1 --format '{{.State.OOMKilled}}'   # true = zabrakło pamięci
```

- **OOMKilled**: zwiększ `OLLAMA_MEM_LIMIT` (domyślnie 8g) albo pamięć Dockera; zmniejszenie
  `OLLAMA_CONTEXT_LENGTH` zmniejsza bufor KV (8192 tokenów to ok. 1,2 GB).
- Po restarcie Ollamy API samo rozgrzeje model (log `llm.model_unloaded`, metryka
  `ai_router_model_rewarm_total`). Do tego czasu zapytania działają, tylko pierwsze trwa dłużej.

### Model zniknął lub ma inne wagi

```bash
docker compose exec ollama ollama list
docker compose up -d                       # ollama-init pobierze brakujący model
```

Aby wymusić dokładnie te wagi, które przeszły ewaluację, ustaw `OLLAMA_MODEL_DIGEST` (pierwsze 12 znaków
z `ollama list`). `ollama-init` zatrzyma wtedy start, gdy wagi się różnią.

### 429 Too Many Requests

- `detail` mówi, który limit zadziałał: limit klienta (`RATE_LIMIT_PER_MINUTE`, `RATE_LIMIT_BURST`) czy
  brak wolnego slotu agenta (`AGENT_QUEUE_TIMEOUT_SECONDS`).
- Klienci powinni respektować `Retry-After`. Dla wsadów najlepszy jest tryb asynchroniczny, w którym
  zgłoszenia czekają w kolejce zamiast dostawać 429.

### API nie startuje po zmianie konfiguracji

Konfiguracja jest walidowana przy starcie. Komunikat w `docker compose logs api` wskazuje zmienną, np.
`AUTH_MODE=api_key requires API_KEYS` albo za małe `OLLAMA_CONTEXT_LENGTH` względem
`MAX_MESSAGE_CHARS`. Własna polityka routingu też jest walidowana (unikalne adresy, dokładnie jeden
fallback).

### Baza danych

- `storage` w readiness = `down`: sprawdź miejsce na dysku (`df -h`, `docker system df`) i uprawnienia
  wolumenu (`docker compose exec api ls -la /app/data`).
- Uszkodzony plik bazy (bardzo rzadkie przy WAL): zatrzymaj API, odtwórz kopię
  ([sekcja 6](#6-kopie-zapasowe-i-odtwarzanie)).

## 5. Zmiany i aktualizacje

### Zmiana modelu lub wersji Ollamy

1. Ustaw w `.env` nowy `OLLAMA_MODEL` (ewentualnie `LLM_THINKING=false` dla modeli hybrydowych) albo
   `OLLAMA_IMAGE`.
2. `docker compose up -d`: `ollama-init` pobierze model, API wystartuje z nowym.
3. **Zanim wpuścisz ruch**: `make eval ARGS="--min-accuracy 0.9 --min-tool-compliance 0.98 --max-duplicate-rate 0.05"`.
   Kod wyjścia 1 oznacza, że model nie spełnia progów. Wtedy wróć do poprzedniej wartości i uruchom
   `docker compose up -d`.
4. Zapisz digest wag (`ollama list`) w `OLLAMA_MODEL_DIGEST`.

Workflow *Model evaluation* (GitHub Actions → Run workflow) sprawdza przypiętą i poprzednią wersję
Ollamy, więc regresja po aktualizacji obrazu wychodzi przed wdrożeniem.

### Zmiana polityki działów

1. `cp api/src/ai_router/routing/policy.yaml config/routing-policy.yaml` i edycja.
2. `.env`: `ROUTING_POLICY_FILE=/config/routing-policy.yaml`, następnie `docker compose up -d api`.
3. Sprawdź `make eval` (z `ROUTING_POLICY_FILE` w `.env` ewaluacja użyje tej samej polityki).
   Przykłady few-shot w polityce nie mogą powtarzać przypadków ze zbioru ewaluacyjnego (skrypt oznacza
   takie przypadki jako `in-prompt`).
4. Nowa wersja polityki i nowy `prompt_version` są widoczne w `ai_router_info`, w logu startowym i w
   nagłówkach maili, więc łatwo powiązać zmianę jakości ze zmianą polityki.

### Aktualizacja obrazów i zależności

Renovate otwiera PR-y z nowymi digestami obrazów i wersjami pakietów. Przed scaleniem: zielone CI (lint,
testy, E2E na fałszywym LLM, skan Trivy), a dla zmian Ollamy lub modelu także workflow *Model evaluation*. Ręcznie:
`make scan`.

### Klucze API i sekrety

- Nowy klient: `make api-key` wypisuje klucz (dla klienta) i wpis `nazwa:sha256` do `API_KEYS`
  (wiele wpisów rozdzielaj przecinkami). Po zmianie `docker compose up -d api`.
- Unieważnienie: usuń wpis z `API_KEYS` i zrestartuj API.
- Sekret podpisów linków informacji zwrotnej: jawnie `FEEDBACK_SECRET` albo generowany raz i trzymany w
  bazie. Zmiana sekretu unieważnia linki w już wysłanych mailach.
- `ADMIN_TOKEN` chroni eksport informacji zwrotnej (wymagany przy `AUTH_MODE` innym niż `none`).

## 6. Kopie zapasowe i odtwarzanie

Kopia bazy na działającym systemie (SQLite backup API, spójna mimo trwających zapisów):

```bash
docker compose exec api python -m ai_router.admin backup /app/data/backup-$(date +%F).sqlite3
docker compose cp api:/app/data/backup-$(date +%F).sqlite3 ./backups/
```

Odtworzenie:

```bash
docker compose stop api
docker compose cp ./backups/backup-2026-10-13.sqlite3 api:/app/data/ai-router.sqlite3
docker compose start api      # przy starcie: przerwane zgłoszenia wracają do kolejki, outbox wznawia wysyłkę
```

Usuń wtedy pliki `ai-router.sqlite3-wal` i `-shm` z poprzedniej bazy, jeśli zostały
(`docker compose run --rm --no-deps --entrypoint sh api -c 'rm -f /app/data/ai-router.sqlite3-wal /app/data/ai-router.sqlite3-shm'`).

Model nie wymaga kopii (da się go pobrać ponownie), ale dla instalacji bez internetu:
`make model-export` (plik `ollama-models.tgz`) i `make model-import FILE=ollama-models.tgz`.

## 7. Dane osobowe (RODO)

| Dane | Gdzie | Retencja |
|---|---|---|
| Treść zgłoszenia, adres nadawcy, wynik | tabela `tickets` | `TICKET_RETENTION_DAYS` (30 dni), usuwanie co godzinę |
| Gotowe maile do wysłania | tabela `outbox` | jak zgłoszenia (po wysłaniu lub porażce) |
| Korekty działów (z treścią wiadomości) | tabela `feedback` | `FEEDBACK_RETENTION_DAYS` (180 dni) |
| Zapisane odpowiedzi dla `Idempotency-Key` | tabela `idempotency` | `IDEMPOTENCY_TTL_SECONDS` (24 h) |
| Maile w Mailpit | wolumen kontenera `mailpit` | `MAILPIT_MAX_AGE` (7 dni) |
| Logi | sterownik `json-file` Dockera | rotacja 3 × 10 MB na usługę; treść wiadomości tylko przy `LOG_MESSAGE_CONTENT=true` |
| Ślady (Jaeger) | pamięć Jaegera | do restartu; treść promptów tylko przy `TRACE_INCLUDE_CONTENT=true` |

Model działa lokalnie, więc treść zgłoszeń nie opuszcza infrastruktury.

**Wniosek o usunięcie danych** konkretnej osoby:

```bash
docker compose exec api python -m ai_router.admin forget jan.nowak@example.com
# Mailpit (środowisko testowe): usuń maile z tym adresem w Reply-To
curl -s -X DELETE 'http://localhost:8025/api/v1/search?query=reply-to:"jan.nowak@example.com"'
```

Polecenie usuwa zgłoszenia osoby, ich maile z outboxu, powiązane korekty i zapisane odpowiedzi
idempotencji. Nie sięga do skrzynek działów, do których maile już dotarły; to osobna procedura w
systemie pocztowym. Korekty starsze niż retencja zgłoszeń (30–180 dni) nie mają już powiązania z adresem
nadawcy. Zawierają jednak treść wiadomości, więc przy wniosku przejrzyj eksport
(`/api/v1/feedback/export`).

## 8. Wydajność i pojemność

| Parametr | Wartość (CPU Ryzen 7 5800H, 8 wątków) |
|---|---|
| Czas zgłoszenia (`AGENT_MODE=tool`) | mediana 12,7 s, p95 14,0 s |
| Przepustowość | ok. 5 zgłoszeń/min (jeden slot Ollamy) |
| Rozgrzewka po starcie | ok. 40 s (w tle) |
| Pamięć | Ollama ok. 3,7 GB (model + KV cache 8192), API ok. 100 MB |

Co zwiększa przepustowość, od najtańszego: `AGENT_MODE=output_function` (jedno zapytanie do LLM),
tryb async (kolejka wygładza szczyty), GPU (`docker-compose.gpu.yml`, typowo kilka–kilkanaście razy
szybciej), `OLLAMA_NUM_PARALLEL` > 1 razem z `MAX_CONCURRENT_AGENT_RUNS` (sensowne na GPU), większa
liczba replik API (wymaga PostgreSQL zamiast SQLite i współdzielonego limitu zapytań).
